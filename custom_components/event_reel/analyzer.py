"""Turns a trigger into a stored event: frames, then the AI, then the store."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import ipaddress
import logging
from pathlib import Path
import re
import shutil
import time
from typing import Any
import uuid

from aiohttp import ClientError, ClientTimeout
from homeassistant.components import media_source
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
    AI_FAILURES_FOR_ISSUE,
    AI_TIMEOUT,
    ANALYSIS_FAILED,
    ANALYSIS_NONE,
    ANALYSIS_OK,
    DOMAIN,
    ISSUE_AI_FAILING,
    MAX_MEDIA_BYTES,
    QUEUE_SIZE,
)
from .frames import frame_from_image, frames_from_camera, frames_from_video
from .prompt import STRUCTURE, Answer, build_instructions, category_for, parse_answer
from .store import EventStore, ReelEvent

_LOGGER = logging.getLogger(__name__)

# Waits before fetching a Nest clip again: the clip can arrive a little after the event.
MEDIA_RETRY_DELAYS = (3, 6, 12)
FETCH_TIMEOUT = ClientTimeout(total=60)
# Seconds a camera's cooldown lasts at most after a look that found nothing and kept no
# event ends.
EMPTY_LOOK_COOLDOWN = 10
# Times the AI is asked when it fails with an error, and the wait between them.
AI_ATTEMPTS = 2
AI_RETRY_DELAY = 10
# Seconds within which an error must come for the AI to be asked again. A model that takes
# longer to fail would hold up every camera twice.
AI_RETRY_WITHIN = 60
# What getting frames can fail with; the event is then kept without a description.
FRAME_ERRORS = (HomeAssistantError, ClientError, TimeoutError, OSError)
# Longest AI error shown in Repairs.
MAX_ERROR_LENGTH = 200
# How much of an error is searched for secrets; the rest is cut anyway. The searches take
# time that grows with the square of the length, on the event loop.
_MAX_SCANNED = 1000
# A value in quotes, to its closing quote (one escaped with `\` doesn't close it) or, cut
# short, to the end. Its quotes are kept around the mask.
_QUOTED = r"(\")(?:[^\"\\]|\\.)*(?:\"|$)|(')(?:[^'\\]|\\.)*(?:'|$)"
# Names whose value is a secret. A name is a whole word or the end of one joined with `-`
# or `_` (`x-api-key`, `client_secret`), which also takes words like `monkey`, the safe side.
_SECRET_NAMES = (
    r"key(?:\s+provided)?|token|secret|pass(?:word|wd|phrase)?|pwd|cookie|sig(?:nature)?"
    r"|credentials?|auth|session_?id"
)
# What may carry a secret in an error message, and what it is replaced with.
_SECRETS = (
    # A URL's user and password, up to the last `@` before the path, query or fragment.
    (re.compile(r"\b([a-z][a-z0-9+.-]*://)[^/\s?#\"'<>]*@", re.IGNORECASE), r"\1"),
    # The query and fragment of a URL or path, as far as quotes or space; brackets and
    # punctuation right before those are kept. A `?` with nothing after it ends a question.
    (
        re.compile(
            r"((?:\b[a-z][a-z0-9+.-]*://|/)[^\s?#\"'<>]*)[?#][^\s\"'<>]+?"
            r"(?=[)\]}.,;:!]*(?:[\s\"'<>]|$))",
            re.IGNORECASE,
        ),
        r"\1",
    ),
    # An `Authorization` header's value, whatever its scheme: the quoted value, or all the
    # rest of the text (Digest's goes on with commas).
    (
        re.compile(
            r"(?<![\w-])((?:[\w-]*-)?authorization)([\"']?\s*[=:]\s*)(?:" + _QUOTED + r"|.*)",
            re.IGNORECASE,
        ),
        lambda m: f"{m[1]}{m[2]}{m[3] or m[4] or ''}***{m[3] or m[4] or ''}",
    ),
    # `api_key=…`, `token: …`, `password="…"` and the like.
    (
        re.compile(
            rf"(?<![\w-])([\w-]*(?:{_SECRET_NAMES}))([\"']?\s*[=:]\s*)"
            r"(?:(?:bearer|basic|token)\s+)?(?:" + _QUOTED + r"|[^\s\"'&,;]+)",
            re.IGNORECASE,
        ),
        lambda m: f"{m[1]}{m[2]}{m[3] or m[4] or ''}***{m[3] or m[4] or ''}",
    ),
    # A bearer token on its own: one with a digit or token punctuation, not a word.
    (
        re.compile(r"\b(bearer)\s+(?=[\w.~+/=-]*[\d.~+/=_-])[\w.~+/=-]{6,}", re.IGNORECASE),
        r"\1 ***",
    ),
    # Keys and tokens by their shape: OpenAI's and many others', Google's, GitHub's, Hugging
    # Face's, Groq's, xAI's, NVIDIA's, Perplexity's, and JSON web tokens.
    (re.compile(r"\bsk-[\w-]{8,}"), "sk-***"),
    (
        re.compile(r"\b(AIza|ya29\.|gh[pousr]_|github_pat_|hf_|gsk_|xai-|nvapi-|pplx-)[\w.-]{16,}"),
        r"\1***",
    ),
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), "eyJ***"),
)
_MARKDOWN = re.compile(r"([\\`*_\[\]<>~|])")
# A bare address GitHub-flavored markdown would make a link of.
_WWW = re.compile(r"\b(www)\.", re.IGNORECASE)


def safe_error(text: str) -> str:
    """An error message fit for a Repairs text: one short line, no secrets, no markdown.

    The text comes from the AI integration, so this is a best effort: a secret it doesn't
    mark as one, such as a token in a URL path, still shows.
    """
    # One line without control characters, short enough to search.
    text = "".join(c for c in text[: _MAX_SCANNED * 4] if c.isprintable() or c.isspace())
    text = " ".join(text.split())[:_MAX_SCANNED]
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    if len(text) > MAX_ERROR_LENGTH:
        text = text[: MAX_ERROR_LENGTH - 1].rstrip() + "…"
    # Markdown shows as written, and an address isn't made a link (`://` and `www.` are no
    # longer one).
    text = _MARKDOWN.sub(r"\\\1", text)
    return _WWW.sub(r"\1\\.", text.replace("://", "\\://"))


class MediaNotReady(HomeAssistantError):
    """The media isn't available yet (404)."""


@dataclass(slots=True)
class Job:
    """One thing to describe."""

    camera: str | None
    camera_name: str
    start: datetime
    source: str
    frames: int
    labels: list[str] = field(default_factory=list)
    instructions: str | None = None
    # Where the frames come from: a video, an image, or snapshots of `camera`.
    video: str | None = None
    image: str | None = None
    snapshot: bool = False
    nest: dict[str, Any] | None = None
    trigger: str | None = None
    # Keep the event even when the AI sees nothing (a doorbell ring is worth showing).
    keep_without_activity: bool = False
    store: bool = True
    result: asyncio.Future[ReelEvent | None] | None = None
    # Snapshots are taken when the job is submitted, not when the worker gets to it.
    workdir: Path | None = None
    capture: asyncio.Task[list[Path]] | None = None
    # Its outcome is decided: a ring that comes now can no longer join it.
    finished: bool = False


@dataclass(slots=True)
class Settings:
    """Options that apply to every job."""

    ai_task_entity: str | None
    language: str
    instructions: str | None
    keep_no_activity: bool


class Analyzer:
    """Runs jobs one at a time, so a local model is never asked twice at once."""

    def __init__(self, hass: HomeAssistant, store: EventStore, settings: Settings) -> None:
        self.hass = hass
        self.store = store
        self.settings = settings
        self._queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._worker: asyncio.Task[None] | None = None
        self._cooldown_until: dict[str, float] = {}
        # Per camera: the job of the current cooldown until it makes an event, then that
        # event's id and whether the AI saw nothing at it.
        self._recent_job: dict[str, Job] = {}
        self._recent_event: dict[str, tuple[str, bool]] = {}
        # Failed answers in a row.
        self._ai_failures = 0

    @callback
    def async_start(self) -> None:
        self._worker = self.hass.async_create_background_task(self._run(), "event_reel analyzer")

    async def async_stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        # A reload may well be the fix, such as another AI Task; the count starts over. After
        # the worker, so a last failure while it stops can't raise the issue again.
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_AI_FAILING)
        waiting: list[Job] = []
        while not self._queue.empty():
            job = self._queue.get_nowait()
            self._queue.task_done()
            _discard(job.capture)
            _fail(job, HomeAssistantError("Event Reel stopped before the analysis ran"))
            waiting.append(job)
        # Their snapshots may already be on disk: let the captures stop, then remove them.
        await asyncio.gather(
            *(job.capture for job in waiting if job.capture), return_exceptions=True
        )
        for job in waiting:
            if job.workdir:
                await self.hass.async_add_executor_job(shutil.rmtree, job.workdir, True)

    @callback
    def async_submit(self, job: Job, cooldown: float = 0) -> bool:
        """Queue a job unless its camera is cooling down. Returns whether it was queued.

        A doorbell ring during the cooldown still marks the moment as a ring.
        """
        now = time.monotonic()
        key = job.camera
        if key and cooldown and self._cooldown_until.get(key, 0) > now:
            if "chime" in job.labels:
                self._add_chime(job, cooldown)
            else:
                _LOGGER.debug("Skipping %s: cooling down", key)
            return False
        if not self._enqueue(job):
            return False
        if key and cooldown:
            self._cooldown_until[key] = now + cooldown
            self._recent_job[key] = job
            self._recent_event.pop(key, None)
        return True

    @callback
    def _enqueue(self, job: Job) -> bool:
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            _LOGGER.warning("Too many waiting analyses; skipped one for %s", job.camera_name)
            return False
        if job.snapshot and job.camera:
            job.workdir = self._new_workdir()
            job.capture = self.hass.async_create_background_task(
                self._capture(job, job.workdir), f"event_reel snapshots {job.camera}"
            )
        return True

    @property
    def ai_failures(self) -> int:
        """How many jobs in a row the AI gave no answer for."""
        return self._ai_failures

    @property
    def waiting(self) -> int:
        """How many jobs wait for the AI."""
        return self._queue.qsize()

    def cooldowns(self) -> dict[str, float]:
        """Seconds left of each camera's cooldown, for the cameras cooling down."""
        now = time.monotonic()
        return {
            camera: round(until - now, 1)
            for camera, until in self._cooldown_until.items()
            if until > now
        }

    async def async_join(self) -> None:
        """Wait until every queued job has finished."""
        await self._queue.join()

    async def async_describe(self, job: Job) -> ReelEvent | None:
        """Queue a job and wait for its event (None when nothing was seen)."""
        job.result = self.hass.loop.create_future()
        if not self.async_submit(job):
            raise HomeAssistantError("Too many waiting analyses")
        return await job.result

    @callback
    def _add_chime(self, chime: Job, cooldown: float) -> None:
        camera = chime.camera
        assert camera
        job = self._recent_job.get(camera)
        if job is not None and not job.finished:
            # The ring belongs to the moment that is still being described.
            if "chime" not in job.labels:
                job.labels.append("chime")
            job.keep_without_activity = True
            return
        recent = self._recent_event.get(camera)
        if recent is not None:
            event_id, nothing_seen = recent
            event = self.store.get(event_id)
            if event is not None and "chime" not in event["labels"]:
                changes: dict[str, Any] = {
                    "labels": [*event["labels"], "chime"],
                    "category": "doorbell",
                }
                if nothing_seen:
                    # As at any ring the AI saw nobody at, the ring is the event (_make_event).
                    changes["title"] = ""
                self.store.async_update(event_id, changes)
            return
        # The moment's look ended without an event: it crashed, or it found nothing and this is
        # the short cooldown after it (_end_cooldown). The ring is still worth its own event,
        # and the visit it starts gets a whole cooldown, as any new look does.
        if self._enqueue(chime):
            self._recent_job[camera] = chime
            self._cooldown_until[camera] = time.monotonic() + cooldown

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                event = await self._process(job)
            except asyncio.CancelledError:
                _fail(job, HomeAssistantError("Event Reel stopped during the analysis"))
                raise
            except Exception as err:  # keep the worker alive whatever one job does
                _LOGGER.exception("Analysis for %s failed", job.camera_name)
                _fail(job, err)
            else:
                if job.result and not job.result.done():
                    job.result.set_result(event)
            finally:
                job.finished = True
                self._queue.task_done()

    def _new_workdir(self) -> Path:
        return self.store.media.path_for(f"_work/{uuid.uuid4().hex}")

    async def _process(self, job: Job) -> ReelEvent | None:
        workdir = job.workdir or self._new_workdir()
        try:
            await self.hass.async_add_executor_job(
                lambda: workdir.mkdir(parents=True, exist_ok=True)
            )
            frames: list[Path] = []
            try:
                frames = await self._frames(job, workdir)
            except FRAME_ERRORS as err:
                _LOGGER.warning("No frames for %s: %s", job.camera_name, err)

            answer = await self._describe(job, frames, workdir) if frames else None

            if (
                answer is not None
                and answer.activity is False
                and not job.keep_without_activity
                and not self.settings.keep_no_activity
            ):
                _LOGGER.debug("No event for %s: the AI saw nothing", job.camera_name)
                self._end_cooldown(job)
                return None
            return await self._make_event(job, frames, answer)
        finally:
            # Whatever happened, a ring during the cleanup must not join this job.
            job.finished = True
            _discard(job.capture)
            await self.hass.async_add_executor_job(shutil.rmtree, workdir, True)

    @callback
    def _end_cooldown(self, job: Job) -> None:
        """Shorten the cooldown after a look that found nothing and kept no event.

        Otherwise a false alarm would hide whoever arrives a few seconds later for the rest
        of the cooldown. The camera still waits up to EMPTY_LOOK_COOLDOWN after the look,
        so a trigger that keeps flapping leaves a local model idle in between. A shorter
        configured cooldown is never lengthened.
        """
        camera = job.camera
        if camera and self._recent_job.get(camera) is job:
            del self._recent_job[camera]
            now = time.monotonic()
            until = min(self._cooldown_until.get(camera, 0), now + EMPTY_LOOK_COOLDOWN)
            if until > now:
                self._cooldown_until[camera] = until
            else:
                self._cooldown_until.pop(camera, None)

    async def _describe(self, job: Job, frames: list[Path], workdir: Path) -> Answer | None:
        """The AI's answer, asking once more after an error. None when it gave none.

        An error is often passing: a local model that is restarting or still loading. A
        request the AI Task rejects (ServiceValidationError, which includes a missing
        service) or a timeout isn't asked again: the same request would be rejected again,
        and a stuck model would hold up every camera twice. Nor is an error that took long
        to come, for the same reason, or an AI integration's own bug. A job counts once
        towards the Repairs issue, however often it was asked; an answer clears the issue.
        """
        for attempt in range(1, AI_ATTEMPTS + 1):
            started = time.monotonic()
            try:
                async with asyncio.timeout(AI_TIMEOUT) as deadline:
                    data = await self._ask(job, frames, workdir)
                _LOGGER.debug(
                    "Answer for %s %s from %s frames: %s",
                    job.camera_name,
                    job.labels,
                    len(frames),
                    data,
                )
                answer = parse_answer(data)
            except TimeoutError as err:
                if deadline.expired():
                    _LOGGER.warning(
                        "The AI took more than %s s to describe %s", AI_TIMEOUT, job.camera_name
                    )
                    self._ai_failed(None)
                    return None
                # The AI integration's own time limit, such as an HTTP timeout: an error
                # like any other.
                error = str(err) or type(err).__name__
            except ServiceValidationError as err:
                _LOGGER.warning(
                    "The AI Task rejected the request to describe %s: %s", job.camera_name, err
                )
                self._ai_failed(str(err) or type(err).__name__)
                return None
            except HomeAssistantError as err:
                error = str(err) or type(err).__name__
            except Exception as err:  # an AI integration's own bug is a failed answer too
                _LOGGER.exception("The AI couldn't describe %s", job.camera_name)
                self._ai_failed(f"{type(err).__name__}: {err}" if str(err) else type(err).__name__)
                return None
            else:
                self._ai_failures = 0
                ir.async_delete_issue(self.hass, DOMAIN, ISSUE_AI_FAILING)
                return answer
            took = time.monotonic() - started
            # Once the repair is up, the AI Task is broken rather than busy: asking twice
            # would only hold up the other cameras.
            if (
                attempt == AI_ATTEMPTS
                or took > AI_RETRY_WITHIN
                or self._ai_failures >= AI_FAILURES_FOR_ISSUE
            ):
                _LOGGER.warning(
                    "The AI couldn't describe %s (try %s, after %.0f s): %s",
                    job.camera_name,
                    attempt,
                    took,
                    error,
                )
                self._ai_failed(error)
                return None
            _LOGGER.info(
                "The AI couldn't describe %s (%s); asking again in %s s",
                job.camera_name,
                error,
                AI_RETRY_DELAY,
            )
            await asyncio.sleep(AI_RETRY_DELAY)
        return None  # not reached

    @callback
    def _ai_failed(self, error: str | None) -> None:
        """Count a failed answer, and say so in Repairs once they keep failing.

        `error` is None when the AI didn't answer in time.
        """
        self._ai_failures += 1
        if self._ai_failures < AI_FAILURES_FOR_ISSUE:
            return
        entity = self.settings.ai_task_entity
        placeholders = {"count": str(self._ai_failures)}
        if entity:
            placeholders["entity"] = entity
        if error is None:
            key = "ai_timeout"
            placeholders["seconds"] = str(AI_TIMEOUT)
        else:
            key = ISSUE_AI_FAILING
            placeholders["error"] = safe_error(error)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_AI_FAILING,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=key if entity else f"{key}_preferred",
            translation_placeholders=placeholders,
        )

    async def _capture(self, job: Job, workdir: Path) -> list[Path]:
        assert job.camera
        await self.hass.async_add_executor_job(lambda: workdir.mkdir(parents=True, exist_ok=True))
        binary = get_ffmpeg_manager(self.hass).binary
        return await frames_from_camera(self.hass, binary, job.camera, job.frames, workdir)

    async def _frames(self, job: Job, workdir: Path) -> list[Path]:
        if job.capture:
            return await job.capture
        binary = get_ffmpeg_manager(self.hass).binary
        if job.video:
            data = await self._load_media(job.video, retry=job.nest is not None)
            return await frames_from_video(binary, data, job.frames, workdir)
        if job.image:
            data = await self._load_media(job.image, retry=job.nest is not None)
            return [await frame_from_image(binary, data, workdir, "image.jpg")]
        if job.snapshot and job.camera:
            return await frames_from_camera(self.hass, binary, job.camera, job.frames, workdir)
        return []

    async def _load_media(self, ref: str, *, retry: bool) -> bytes:
        """Bytes of an HA path (/api/...), a media-source id or a local media file."""
        if ref.startswith("media-source://"):
            resolved = await media_source.async_resolve_media(self.hass, ref, None)
            if resolved.path is not None:
                return await self.hass.async_add_executor_job(_read_limited, resolved.path)
            ref = resolved.url
        for delay in (*MEDIA_RETRY_DELAYS, None) if retry else (None,):
            try:
                return await self._fetch(ref)
            except MediaNotReady:
                if delay is None:
                    raise
                # Never log a signature.
                _LOGGER.debug("%s isn't ready; trying again in %s s", ref.split("?")[0], delay)
                await asyncio.sleep(delay)
        raise MediaNotReady(ref)  # not reached

    async def _fetch(self, url: str) -> bytes:
        """GET an HA path (signed for the read-only content user) or a resolved media URL."""
        shown = url.split("?")[0]
        if url.startswith("/"):
            signed = async_sign_path(self.hass, url, timedelta(minutes=5), use_content_user=True)
            # HA's own certificate doesn't match the loopback address.
            session = async_get_clientsession(self.hass, verify_ssl=False)
            url = f"{self._local_base()}{signed}"
        elif url.startswith(("http://", "https://")):
            session = async_get_clientsession(self.hass)
        else:
            raise HomeAssistantError(f"Unsupported media address: {shown}")
        try:
            async with session.get(url, timeout=FETCH_TIMEOUT) as resp:
                if resp.status == 404:
                    raise MediaNotReady(f"Not found: {shown}")
                if resp.status >= 400:
                    raise HomeAssistantError(f"HTTP {resp.status} for {shown}")
                body = bytearray()
                async for chunk in resp.content.iter_chunked(1 << 16):
                    body += chunk
                    if len(body) > MAX_MEDIA_BYTES:
                        raise HomeAssistantError(f"Too large: {shown}")
                return bytes(body)
        except (ClientError, TimeoutError) as err:
            # Never log the signed address.
            raise HomeAssistantError(f"Couldn't fetch {shown}: {type(err).__name__}") from None

    def _local_base(self) -> str:
        """Where HA's own HTTP server answers on this machine."""
        api = self.hass.config.api
        if api is None:
            return "http://127.0.0.1:8123"
        # `host` is the first `server_host`, or the local IP when HA listens everywhere.
        host = api.host or "127.0.0.1"
        with contextlib.suppress(ValueError):
            address = ipaddress.ip_address(host)
            if address.is_unspecified:
                host = "127.0.0.1"
            elif address.version == 6:
                host = f"[{host}]"
        return f"{'https' if api.use_ssl else 'http'}://{host}:{api.port}"

    async def _ask(self, job: Job, frames: list[Path], workdir: Path) -> Any:
        extra = [t for t in (self.settings.instructions, job.instructions) if t]
        work_rel = workdir.relative_to(self.store.media.root).as_posix()
        data: dict[str, Any] = {
            "task_name": f"Event Reel: {job.camera_name}",
            "instructions": build_instructions(
                camera_name=job.camera_name,
                frame_count=len(frames),
                language=self.settings.language,
                labels=job.labels,
                extra=extra,
            ),
            "structure": STRUCTURE,
            "attachments": [
                {
                    "media_content_id": self.store.media.media_id(f"{work_rel}/{frame.name}"),
                    "media_content_type": "image/jpeg",
                }
                for frame in frames
            ],
        }
        if self.settings.ai_task_entity:
            data["entity_id"] = self.settings.ai_task_entity
        response = await self.hass.services.async_call(
            "ai_task", "generate_data", data, blocking=True, return_response=True
        )
        return (response or {}).get("data")

    async def _make_event(self, job: Job, frames: list[Path], answer: Answer | None) -> ReelEvent:
        event_id = uuid.uuid4().hex
        key_frame = None
        if frames and job.store:
            try:
                key_frame = await self.store.async_keep_frame(
                    frames[len(frames) // 2], event_id, job.start
                )
            except OSError as err:
                _LOGGER.warning("Couldn't keep a key frame for %s: %s", job.camera_name, err)
        # For a ring the AI saw nobody at, the ring is the event: an empty title lets the card
        # and automations say "the doorbell rang" instead of the AI's "no activity" title.
        # The description still says what the clip showed.
        nothing_seen = answer is not None and answer.activity is False
        quiet_ring = nothing_seen and "chime" in job.labels
        event: ReelEvent = {
            "id": event_id,
            "start": dt_util.as_local(job.start).isoformat(),
            "end": None,
            "camera": job.camera,
            "title": answer.title if answer and not quiet_ring else "",
            "description": answer.description if answer else None,
            "category": category_for(answer, job.labels),
            "labels": list(job.labels),
            "key_frame": key_frame,
            "source": job.source,
            "analysis": self._analysis(job, answer),
        }
        if job.nest:
            event["nest"] = dict(job.nest)
        if job.trigger:
            event["trigger"] = job.trigger
        if job.store:
            self.store.async_add(event)
            # A later ring in this cooldown marks this event, unless a newer job owns it.
            if job.camera and self._recent_job.get(job.camera) is job:
                del self._recent_job[job.camera]
                self._recent_event[job.camera] = (event_id, nothing_seen)
        return event

    @staticmethod
    def _analysis(job: Job, answer: Answer | None) -> str:
        if answer is not None:
            return ANALYSIS_OK
        has_media = bool(job.video or job.image or job.snapshot)
        return ANALYSIS_FAILED if has_media else ANALYSIS_NONE


def _fail(job: Job, err: BaseException) -> None:
    if job.result and not job.result.done():
        job.result.set_exception(err)


def _discard(task: asyncio.Task[Any] | None) -> None:
    """Stop a snapshot task that is no longer needed, without leaving its error unread."""
    if task is None:
        return
    if not task.done():
        task.cancel()
    elif not task.cancelled():
        task.exception()


def _read_limited(path: Path) -> bytes:
    if path.stat().st_size > MAX_MEDIA_BYTES:
        raise HomeAssistantError(f"Too large: {path.name}")
    return path.read_bytes()
