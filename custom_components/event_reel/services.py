"""Services: describe a camera, add or remove an event, and read events."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
import uuid

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from ._schema import vol
from .analyzer import Job
from .const import ANALYSIS_NONE, DEFAULT_FRAMES, DOMAIN, MAX_FRAMES
from .runtime import EventReelData
from .store import select_events

SERVICE_DESCRIBE = "describe"
SERVICE_ADD_EVENT = "add_event"
SERVICE_REMOVE_EVENT = "remove_event"
SERVICE_GET_EVENTS = "get_events"

DESCRIBE_SCHEMA = vol.Schema(
    {
        vol.Required("camera"): cv.entity_id,
        vol.Optional("name"): cv.string,
        vol.Exclusive("video", "media"): cv.string,
        vol.Exclusive("image", "media"): cv.string,
        vol.Optional("frames", default=DEFAULT_FRAMES): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_FRAMES)
        ),
        vol.Optional("instructions"): cv.string,
        vol.Optional("labels", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("store", default=True): cv.boolean,
    }
)

ADD_EVENT_SCHEMA = vol.Schema(
    {
        vol.Required("title"): cv.string,
        vol.Optional("description"): cv.string,
        vol.Optional("camera"): cv.entity_id,
        vol.Optional("start"): cv.datetime,
        vol.Optional("end"): cv.datetime,
        vol.Optional("category", default="custom"): cv.string,
        vol.Optional("labels", default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("image"): cv.string,
    }
)

REMOVE_EVENT_SCHEMA = vol.Schema({vol.Required("id"): cv.string})

GET_EVENTS_SCHEMA = vol.Schema(
    {
        vol.Optional("start"): cv.datetime,
        vol.Optional("end"): cv.datetime,
        vol.Optional("cameras"): vol.All(cv.ensure_list, [cv.entity_id]),
        vol.Optional("categories"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("limit", default=100): vol.All(vol.Coerce(int), vol.Range(min=1, max=1000)),
    }
)


def _runtime(hass: HomeAssistant) -> EventReelData:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            return cast(EventReelData, entry.runtime_data)
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")


def _local(value: datetime | None) -> datetime | None:
    """A naive datetime from a service call is in HA's time zone."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt_util.get_default_time_zone())


def _camera_name(hass: HomeAssistant, camera: str, name: str | None) -> str:
    if name:
        return name
    state = hass.states.get(camera)
    return str(state.attributes.get("friendly_name", camera)) if state else camera


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the services (once, for the integration)."""

    async def describe(call: ServiceCall) -> ServiceResponse:
        data = _runtime(hass)
        camera = call.data["camera"]
        labels = list(call.data["labels"])
        job = Job(
            camera=camera,
            camera_name=_camera_name(hass, camera, call.data.get("name")),
            start=dt_util.utcnow(),
            source="service",
            frames=call.data["frames"],
            labels=labels,
            instructions=call.data.get("instructions"),
            video=call.data.get("video"),
            image=call.data.get("image"),
            snapshot=not (call.data.get("video") or call.data.get("image")),
            keep_without_activity="chime" in labels,
            store=call.data["store"],
        )
        event = await data.analyzer.async_describe(job)
        return {"event": event}

    async def add_event(call: ServiceCall) -> ServiceResponse:
        data = _runtime(hass)
        start = _local(call.data.get("start")) or dt_util.now()
        end = _local(call.data.get("end"))
        if end is not None and end < start:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="end_before_start"
            )
        event: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "start": dt_util.as_local(start).isoformat(),
            "end": dt_util.as_local(end).isoformat() if end else None,
            "camera": call.data.get("camera"),
            "title": call.data["title"],
            "description": call.data.get("description"),
            "category": call.data["category"],
            "labels": list(call.data["labels"]),
            "key_frame": call.data.get("image"),
            "source": "service",
            "analysis": ANALYSIS_NONE,
        }
        data.store.async_add(event)
        return {"id": event["id"]}

    async def remove_event(call: ServiceCall) -> None:
        data = _runtime(hass)
        if not await data.store.async_remove(call.data["id"]):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_event",
                translation_placeholders={"id": call.data["id"]},
            )

    async def get_events(call: ServiceCall) -> ServiceResponse:
        data = _runtime(hass)
        events = select_events(
            data.store.all(),
            start=_local(call.data.get("start")),
            end=_local(call.data.get("end")),
            cameras=call.data.get("cameras"),
            categories=call.data.get("categories"),
            limit=call.data["limit"],
        )
        return {"events": events}

    hass.services.async_register(
        DOMAIN, SERVICE_DESCRIBE, describe, DESCRIBE_SCHEMA, SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_EVENT, add_event, ADD_EVENT_SCHEMA, SupportsResponse.OPTIONAL
    )
    hass.services.async_register(DOMAIN, SERVICE_REMOVE_EVENT, remove_event, REMOVE_EVENT_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_GET_EVENTS, get_events, GET_EVENTS_SCHEMA, SupportsResponse.ONLY
    )
