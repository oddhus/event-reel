"""Turns a trigger into a stored event: frames, then the AI, then the store."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import ipaddress
import logging
from pathlib import Path
import shutil
import time
from typing import Any
import uuid

from aiohttp import ClientError, ClientTimeout
from homeassistant.components import media_source
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
    AI_TIMEOUT,
    ANALYSIS_FAILED,
    ANALYSIS_NONE,
    ANALYSIS_OK,
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
# What getting frames can fail with; the event is then kept without a description.
FRAME_ERRORS = (HomeAssistantError, ClientError, TimeoutError, OSError)


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
        # Per camera: the job of the current cooldown until it makes an event, then that event.
        self._recent_job: dict[str, Job] = {}
        self._recent_event: dict[str, str] = {}

    @callback
    def async_start(self) -> None:
        self._worker = self.hass.async_create_background_task(self._run(), "event_reel analyzer")

    async def async_stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        while not self._queue.empty():
            job = self._queue.get_nowait()
            _discard(job.capture)
            _fail(job, HomeAssistantError("Event Reel stopped before the analysis ran"))

    @callback
    def async_submit(self, job: Job, cooldown: float = 0) -> bool:
        """Queue a job unless its camera is cooling down. Returns whether it was queued.

        A doorbell ring during the cooldown still marks the moment as a ring.
        """
        now = time.monotonic()
        key = job.camera
        if key and cooldown and self._cooldown_until.get(key, 0) > now:
            if "chime" in job.labels:
                self._add_chime(job)
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
    def _add_chime(self, chime: Job) -> None:
        camera = chime.camera
        assert camera
        job = self._recent_job.get(camera)
        if job is not None and not job.finished:
            # The ring belongs to the moment that is still being described.
            if "chime" not in job.labels:
                job.labels.append("chime")
            job.keep_without_activity = True
            return
        event_id = self._recent_event.get(camera)
        if event_id is not None:
            event = self.store.get(event_id)
            if event is not None and "chime" not in event["labels"]:
                self.store.async_update(
                    event_id, {"labels": [*event["labels"], "chime"], "category": "doorbell"}
                )
            return
        # The moment was described without making an event (nobody seen, or it failed):
        # the ring is still worth its own event.
        if self._enqueue(chime):
            self._recent_job[camera] = chime

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

            answer: Answer | None = None
            if frames:
                try:
                    async with asyncio.timeout(AI_TIMEOUT):
                        data = await self._ask(job, frames, workdir)
                    _LOGGER.debug(
                        "Answer for %s %s from %s frames: %s",
                        job.camera_name,
                        job.labels,
                        len(frames),
                        data,
                    )
                    answer = parse_answer(data)
                except TimeoutError:
                    _LOGGER.warning(
                        "The AI took more than %s s to describe %s", AI_TIMEOUT, job.camera_name
                    )
                except HomeAssistantError as err:
                    _LOGGER.warning("The AI couldn't describe %s: %s", job.camera_name, err)

            if (
                answer is not None
                and answer.activity is False
                and not job.keep_without_activity
                and not self.settings.keep_no_activity
            ):
                _LOGGER.debug("No event for %s: the AI saw nothing", job.camera_name)
                return None
            return await self._make_event(job, frames, answer)
        finally:
            _discard(job.capture)
            await self.hass.async_add_executor_job(shutil.rmtree, workdir, True)

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
        quiet_ring = answer is not None and answer.activity is False and "chime" in job.labels
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
                self._recent_event[job.camera] = event_id
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
