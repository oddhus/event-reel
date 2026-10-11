"""Stored events and their key frames."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import contextlib
from datetime import datetime
import logging
from pathlib import Path
import shutil
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import MEDIA_SUBDIR, STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

SAVE_DELAY = 10

type ReelEvent = dict[str, Any]
type Listener = Callable[[str, ReelEvent], None]


def event_ts(event: ReelEvent) -> float:
    """Epoch seconds of an event's start."""
    parsed = dt_util.parse_datetime(event["start"])
    if parsed is None:
        raise ValueError(f"Invalid start: {event['start']!r}")
    return parsed.timestamp()


def select_events(
    events: Iterable[ReelEvent],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    cameras: list[str] | None = None,
    categories: list[str] | None = None,
    limit: int | None = None,
) -> list[ReelEvent]:
    """Events inside [start, end] that match the filters, newest first."""
    lo = start.timestamp() if start else float("-inf")
    hi = end.timestamp() if end else float("inf")
    wanted_cameras = set(cameras) if cameras else None
    wanted_categories = {c.lower() for c in categories} if categories else None
    found = [
        e
        for e in events
        if lo <= event_ts(e) <= hi
        and (wanted_cameras is None or e.get("camera") in wanted_cameras)
        and (wanted_categories is None or str(e.get("category", "")).lower() in wanted_categories)
    ]
    found.sort(key=event_ts, reverse=True)
    return found[:limit] if limit else found


class MediaFolder:
    """Where key frames live: a folder in one of HA's media directories."""

    def __init__(self, hass: HomeAssistant) -> None:
        dirs = hass.config.media_dirs
        if not dirs:
            raise HomeAssistantError("Home Assistant has no media directory")
        self.dir_id = "local" if "local" in dirs else next(iter(dirs))
        self.root = Path(dirs[self.dir_id]) / MEDIA_SUBDIR

    def path_for(self, relative: str) -> Path:
        return self.root / relative

    def media_id(self, relative: str) -> str:
        return f"media-source://media_source/{self.dir_id}/{MEDIA_SUBDIR}/{relative}"

    def relative_from_media_id(self, media_id: str) -> str | None:
        prefix = f"media-source://media_source/{self.dir_id}/{MEDIA_SUBDIR}/"
        return media_id.removeprefix(prefix) if media_id.startswith(prefix) else None


class EventStore:
    """Keeps events in HA storage, newest last, and tells listeners about changes."""

    def __init__(self, hass: HomeAssistant, media: MediaFolder) -> None:
        self.hass = hass
        self.media = media
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._events: dict[str, ReelEvent] = {}
        self._listeners: set[Listener] = set()

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._events = {e["id"]: e for e in data.get("events", [])}

    @callback
    def _schedule_save(self) -> None:
        self._store.async_delay_save(
            lambda: {"events": sorted(self._events.values(), key=event_ts)}, SAVE_DELAY
        )

    async def async_flush(self) -> None:
        await self._store.async_save({"events": sorted(self._events.values(), key=event_ts)})

    @callback
    def async_listen(self, listener: Listener) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def _notify(self, action: str, event: ReelEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(action, event)
            except Exception:
                _LOGGER.exception("Event Reel listener failed")

    def get(self, event_id: str) -> ReelEvent | None:
        return self._events.get(event_id)

    def all(self) -> list[ReelEvent]:
        return list(self._events.values())

    @callback
    def async_add(self, event: ReelEvent) -> ReelEvent:
        event_ts(event)  # reject an unreadable start before storing it
        self._events[event["id"]] = event
        self._schedule_save()
        self._notify("added", event)
        return event

    @callback
    def async_update(self, event_id: str, changes: dict[str, Any]) -> ReelEvent | None:
        event = self._events.get(event_id)
        if event is None:
            return None
        event.update(changes)
        self._schedule_save()
        self._notify("updated", event)
        return event

    async def async_remove(self, event_id: str) -> bool:
        event = self._events.pop(event_id, None)
        if event is None:
            return False
        await self._delete_files([event])
        self._schedule_save()
        self._notify("removed", event)
        return True

    async def async_purge(self, before: datetime) -> int:
        """Remove events that started before `before`, with their key frames."""
        cutoff = before.timestamp()
        old = [e for e in self._events.values() if event_ts(e) < cutoff]
        for e in old:
            del self._events[e["id"]]
        if old:
            await self._delete_files(old)
            self._schedule_save()
        return len(old)

    async def _delete_files(self, events: list[ReelEvent]) -> None:
        paths = [
            self.media.path_for(rel)
            for e in events
            if e.get("key_frame") and (rel := self.media.relative_from_media_id(e["key_frame"]))
        ]

        def delete() -> None:
            for path in paths:
                path.unlink(missing_ok=True)
                # Drop a day folder once it is empty.
                with contextlib.suppress(OSError):
                    path.parent.rmdir()

        if paths:
            await self.hass.async_add_executor_job(delete)

    async def async_keep_frame(self, frame: Path, event_id: str, start: datetime) -> str:
        """Copy a frame into the media folder and return its media-source id."""
        relative = (
            f"{dt_util.as_local(start).date().isoformat()}/{event_id}{frame.suffix or '.jpg'}"
        )
        target = self.media.path_for(relative)

        def copy() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(frame, target)

        await self.hass.async_add_executor_job(copy)
        return self.media.media_id(relative)
