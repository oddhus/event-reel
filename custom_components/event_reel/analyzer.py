"""Turns a trigger into a stored event: frames, then the AI, then the store."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from pathlib import Path
import shutil
import time
from typing import Any
import uuid

from aiohttp import ClientTimeout
from homeassistant.components import media_source
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import ANALYSIS_FAILED, ANALYSIS_NONE, ANALYSIS_OK, QUEUE_SIZE
from .frames import frame_from_image, frames_from_camera, frames_from_video
from .prompt import STRUCTURE, Answer, build_instructions, category_for, parse_answer
from .store import EventStore, ReelEvent

_LOGGER = logging.getLogger(__name__)

# Waits before fetching a Nest clip again: the clip can arrive a little after the event.
MEDIA_RETRY_DELAYS = (3, 6, 12)
FETCH_TIMEOUT = ClientTimeout(total=60)


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
    nest: dict[str, str] | None = None
    trigger: str | None = None
    # Keep the event even when the AI sees nothing (a doorbell ring is worth showing).
    keep_without_activity: bool = False
    store: bool = True
    result: asyncio.Future[ReelEvent | None] | None = None


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
        # Per camera: the job waiting or running in its cooldown, then the event it made.
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
            if job.result and not job.result.done():
                job.result.cancel()

    @callback
    def async_submit(self, job: Job, cooldown: float = 0) -> bool:
        """Queue a job unless its camera is cooling down. Returns whether it was queued.

        A doorbell ring during the cooldown still marks the moment as a ring.
        """
        now = time.monotonic()
        key = job.camera
        if key and cooldown and self._cooldown_until.get(key, 0) > now:
            if "chime" in job.labels:
                self._add_chime(key)
            _LOGGER.debug("Skipping %s: cooling down", key)
            return False
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            _LOGGER.warning("Too many waiting analyses; skipped one for %s", job.camera_name)
            return False
        if key and cooldown:
            self._cooldown_until[key] = now + cooldown
            self._recent_job[key] = job
            self._recent_event.pop(key, None)
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
    def _add_chime(self, camera: str) -> None:
        if (job := self._recent_job.get(camera)) and "chime" not in job.labels:
            job.labels.append("chime")
            job.keep_without_activity = True
        event_id = self._recent_event.get(camera)
        if event_id and (event := self.store.get(event_id)) and "chime" not in event["labels"]:
            self.store.async_update(
                event_id, {"labels": [*event["labels"], "chime"], "category": "doorbell"}
            )

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                event = await self._process(job)
            except asyncio.CancelledError:
                if job.result and not job.result.done():
                    job.result.cancel()
                raise
            except Exception as err:  # keep the worker alive whatever one job does
                _LOGGER.exception("Analysis for %s failed", job.camera_name)
                if job.result and not job.result.done():
                    job.result.set_exception(err)
            else:
                if job.result and not job.result.done():
                    job.result.set_result(event)
            finally:
                self._queue.task_done()

    async def _process(self, job: Job) -> ReelEvent | None:
        work_rel = f"_work/{uuid.uuid4().hex}"
        workdir = self.store.media.path_for(work_rel)
        await self.hass.async_add_executor_job(lambda: workdir.mkdir(parents=True))
        try:
            frames: list[Path] = []
            try:
                frames = await self._frames(job, workdir)
            except HomeAssistantError as err:
                _LOGGER.warning("No frames for %s: %s", job.camera_name, err)

            answer: Answer | None = None
            if frames:
                try:
                    answer = parse_answer(await self._ask(job, frames, work_rel))
                except HomeAssistantError as err:
                    _LOGGER.warning("The AI couldn't describe %s: %s", job.camera_name, err)

            if (
                answer is not None
                and answer.activity is False
                and not job.keep_without_activity
                and not self.settings.keep_no_activity
            ):
                _LOGGER.debug("Nothing to see at %s", job.camera_name)
                return None
            return await self._make_event(job, frames, answer)
        finally:
            await self.hass.async_add_executor_job(shutil.rmtree, workdir, True)

    async def _frames(self, job: Job, workdir: Path) -> list[Path]:
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
                return await self.hass.async_add_executor_job(resolved.path.read_bytes)
            ref = resolved.url
        for delay in (*MEDIA_RETRY_DELAYS, None) if retry else (None,):
            try:
                return await self._fetch(ref)
            except MediaNotReady:
                if delay is None:
                    raise
                await asyncio.sleep(delay)
        raise MediaNotReady(ref)  # not reached

    async def _fetch(self, url: str) -> bytes:
        if url.startswith("/"):
            signed = async_sign_path(self.hass, url, timedelta(minutes=5), use_content_user=True)
            url = f"{self._local_base()}{signed}"
        session = async_get_clientsession(self.hass, verify_ssl=False)
        async with session.get(url, timeout=FETCH_TIMEOUT) as resp:
            if resp.status == 404:
                raise MediaNotReady(f"Not found: {url.split('?')[0]}")
            if resp.status >= 400:
                raise HomeAssistantError(f"HTTP {resp.status} for {url.split('?')[0]}")
            return await resp.read()

    def _local_base(self) -> str:
        api = self.hass.config.api
        if api is None:
            return "http://127.0.0.1:8123"
        # With server_host set, HA may not listen on loopback.
        host = "127.0.0.1" if api.host == api.local_ip else api.host
        return f"{'https' if api.use_ssl else 'http'}://{host}:{api.port}"

    async def _ask(self, job: Job, frames: list[Path], work_rel: str) -> Any:
        extra = [t for t in (self.settings.instructions, job.instructions) if t]
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
            key_frame = await self.store.async_keep_frame(
                frames[len(frames) // 2], event_id, job.start
            )
        event: ReelEvent = {
            "id": event_id,
            "start": dt_util.as_local(job.start).isoformat(),
            "end": None,
            "camera": job.camera,
            "title": answer.title if answer else "",
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
            if job.camera:
                self._recent_event[job.camera] = event_id
                if self._recent_job.get(job.camera) is job:
                    del self._recent_job[job.camera]
        return event

    @staticmethod
    def _analysis(job: Job, answer: Answer | None) -> str:
        if answer is not None:
            return ANALYSIS_OK
        has_media = bool(job.video or job.image or job.snapshot)
        return ANALYSIS_FAILED if has_media else ANALYSIS_NONE
