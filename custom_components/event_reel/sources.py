"""Camera sources: when to describe a camera, and where its frames come from."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
import logging
from typing import Any

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .analyzer import Analyzer, Job
from .const import (
    CONF_CAMERA,
    CONF_COOLDOWN,
    CONF_FRAMES,
    CONF_INSTRUCTIONS,
    CONF_NAME,
    CONF_NEST_EVENTS,
    CONF_TRIGGERS,
    DEFAULT_COOLDOWN,
    DEFAULT_FRAMES,
    DEFAULT_NEST_EVENTS,
    NEST_EVENT_TYPES,
)

_LOGGER = logging.getLogger(__name__)

NEST_EVENT = "nest_event"
NEST_DOMAIN = "nest"
LABEL_BY_NEST_TYPE = {nest_type: label for label, nest_type in NEST_EVENT_TYPES.items()}


def nest_device_id(hass: HomeAssistant, camera: str) -> str | None:
    """The HA device id of a Nest camera entity, or None for any other camera."""
    entry = er.async_get(hass).async_get(camera)
    if entry is None or entry.device_id is None:
        return None
    device = dr.async_get(hass).async_get(entry.device_id)
    if device is None or not any(domain == NEST_DOMAIN for domain, _ in device.identifiers):
        return None
    return device.id


def is_trigger(old_state: str | None, new_state: str | None, domain: str) -> bool:
    """Whether a state change of a trigger entity should start an analysis."""
    if new_state is None or new_state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return False
    if domain == "event":
        # An event entity's state is the time of its last event: any new time is an event,
        # except the first state after startup or after it was unavailable. "unknown" means
        # it never fired, so its first event counts.
        return old_state not in (None, STATE_UNAVAILABLE) and new_state != old_state
    return new_state == STATE_ON and old_state != STATE_ON


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and (parsed := dt_util.parse_datetime(value)):
        return parsed
    return dt_util.utcnow()


class CameraSource:
    """One configured camera: Nest events for Nest cameras, and optional trigger entities."""

    def __init__(self, hass: HomeAssistant, analyzer: Analyzer, data: Mapping[str, Any]) -> None:
        self.hass = hass
        self.analyzer = analyzer
        self.camera: str = data[CONF_CAMERA]
        self.name: str = data.get(CONF_NAME) or self._friendly_name()
        self.triggers: list[str] = list(data.get(CONF_TRIGGERS) or [])
        self.nest_events: list[str] = list(data.get(CONF_NEST_EVENTS, DEFAULT_NEST_EVENTS))
        self.frames = int(data.get(CONF_FRAMES, DEFAULT_FRAMES))
        self.cooldown = float(data.get(CONF_COOLDOWN, DEFAULT_COOLDOWN))
        self.instructions: str | None = data.get(CONF_INSTRUCTIONS) or None
        self.nest_device: str | None = None
        self._unsubs: list[Callable[[], None]] = []

    def _friendly_name(self) -> str:
        state = self.hass.states.get(self.camera)
        if state and (name := state.attributes.get("friendly_name")):
            return str(name)
        return self.camera.split(".", 1)[-1].replace("_", " ").capitalize()

    @callback
    def async_start(self) -> None:
        self.nest_device = nest_device_id(self.hass, self.camera)
        if self.nest_device and self.nest_events:
            self._unsubs.append(self.hass.bus.async_listen(NEST_EVENT, self._on_nest_event))
        if self.triggers:
            self._unsubs.append(
                async_track_state_change_event(self.hass, self.triggers, self._on_trigger)
            )

    @callback
    def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    def _job(self, **kwargs: Any) -> Job:
        return Job(
            camera=self.camera,
            camera_name=self.name,
            frames=self.frames,
            instructions=self.instructions,
            **kwargs,
        )

    @callback
    def _on_nest_event(self, event: Event) -> None:
        data = event.data
        if data.get("device_id") != self.nest_device:
            return
        label = LABEL_BY_NEST_TYPE.get(data.get("type", ""))
        if label is None or label not in self.nest_events:
            return
        attachment = data.get("attachment") or {}
        token = data.get("nest_event_id")
        if not token:
            return
        job = self._job(
            start=_parse_time(data.get("timestamp")),
            source="nest",
            labels=[label],
            video=attachment.get("video"),
            image=None if attachment.get("video") else attachment.get("image"),
            nest={"device_id": self.nest_device, "event_token": token},
            keep_without_activity=label == "chime",
        )
        self.analyzer.async_submit(job, self.cooldown)

    @callback
    def _on_trigger(self, event: Event[EventStateChangedData]) -> None:
        old = event.data["old_state"]
        new = event.data["new_state"]
        entity_id = event.data["entity_id"]
        if not is_trigger(
            old.state if old else None, new.state if new else None, entity_id.split(".")[0]
        ):
            return
        job = self._job(
            start=dt_util.utcnow(),
            source="trigger",
            labels=[self._trigger_label(entity_id)],
            snapshot=True,
            trigger=entity_id,
        )
        self.analyzer.async_submit(job, self.cooldown)

    def _trigger_label(self, entity_id: str) -> str:
        """'person' for person/occupancy sensors, otherwise 'motion'."""
        state = self.hass.states.get(entity_id)
        device_class = state.attributes.get("device_class") if state else None
        if device_class in ("occupancy", "presence") or "person" in entity_id:
            return "person"
        return "motion"
