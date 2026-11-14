"""Diagnostics for a bug report: how Event Reel is set up and how it runs.

What the cameras saw (titles, descriptions, key frames, and the free text `add_event` can
put in a category or label) and the user's own instructions to the AI stay out. Only
fields known to be safe are shown as they are; any other value is redacted.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from homeassistant.components.ai_task import AITaskEntityFeature
from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant

from . import EventReelConfigEntry
from .const import (
    CONF_AI_TASK_ENTITY,
    CONF_CAMERA,
    CONF_COOLDOWN,
    CONF_FRAMES,
    CONF_KEEP_NO_ACTIVITY,
    CONF_LANGUAGE,
    CONF_NAME,
    CONF_NEST_EVENTS,
    CONF_RETENTION_DAYS,
    CONF_TRIGGERS,
    QUEUE_SIZE,
    SUBENTRY_CAMERA,
)
from .runtime import EventReelData
from .store import ReelEvent, event_ts

RECENT_EVENTS = 10
SAFE_OPTIONS = {CONF_AI_TASK_ENTITY, CONF_LANGUAGE, CONF_RETENTION_DAYS, CONF_KEEP_NO_ACTIVITY}
SAFE_CAMERA_DATA = {
    CONF_CAMERA,
    CONF_NAME,
    CONF_TRIGGERS,
    CONF_NEST_EVENTS,
    CONF_FRAMES,
    CONF_COOLDOWN,
}
# What Event Reel itself writes in an event's category and labels. Anything else came from
# `add_event` and can be any text.
OWN_CATEGORIES = {"person", "vehicle", "animal", "package", "doorbell", "motion"}
OWN_LABELS = {"person", "vehicle", "animal", "package", "sound", "motion", "chime"}
OTHER = "(other)"
# Events from `add_event` without a camera.
NO_CAMERA = "(none)"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EventReelConfigEntry
) -> dict[str, Any]:
    cameras = [
        subentry.data
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_CAMERA
    ]
    diagnostics: dict[str, Any] = {
        "state": entry.state.value,
        "options": _safe(entry.options, SAFE_OPTIONS),
        "ai_task": _ai_task(hass, entry.options.get(CONF_AI_TASK_ENTITY) or None),
        "cameras": [
            {
                "subentry": _safe(data, SAFE_CAMERA_DATA),
                "camera_state": _state(hass, data.get(CONF_CAMERA)),
            }
            for data in cameras
        ],
    }
    # Unloaded (disabled, or setup failed) entries have no runtime data.
    data: EventReelData | None = getattr(entry, "runtime_data", None)
    if data is not None:
        diagnostics |= _running(hass, data)
    return diagnostics


def _running(hass: HomeAssistant, data: EventReelData) -> dict[str, Any]:
    analyzer = data.analyzer
    events = data.store.all()
    readable = [(ts, event) for event in events if (ts := _ts(event)) is not None]
    readable.sort(key=lambda pair: pair[0], reverse=True)
    return {
        "sources": [
            {
                "camera": source.camera,
                "nest": source.nest_device is not None,
                "nest_events": source.nest_events,
                "triggers": {trigger: _state(hass, trigger) for trigger in source.triggers},
                "listening": source.listening,
            }
            for source in data.sources
        ],
        "analyzer": {
            "waiting": analyzer.waiting,
            "queue_size": QUEUE_SIZE,
            "cooldowns": analyzer.cooldowns(),
            "ai_failures_in_a_row": analyzer.ai_failures,
        },
        "events": {
            "count": len(events),
            "unreadable_start": len(events) - len(readable),
            "by_camera": dict(Counter(str(event.get("camera") or NO_CAMERA) for event in events)),
            "by_source": dict(Counter(str(event.get("source")) for event in events)),
            "by_analysis": dict(Counter(str(event.get("analysis")) for event in events)),
            "recent": [_summary(event) for _, event in readable[:RECENT_EVENTS]],
        },
        "media_dir": data.store.media.dir_id,
    }


def _safe(data: Mapping[str, Any], safe: set[str]) -> dict[str, Any]:
    return {key: value if key in safe else REDACTED for key, value in data.items()}


def _state(hass: HomeAssistant, entity_id: Any) -> str | None:
    if not isinstance(entity_id, str):
        return None
    state = hass.states.get(entity_id)
    return state.state if state else None


def _ai_task(hass: HomeAssistant, entity_id: Any) -> dict[str, Any]:
    """The chosen AI Task entity and what it can do; None where that isn't known."""
    if entity_id is None:
        # The one set as preferred under Settings → AI Task, which no public API names.
        return {"entity": "preferred"}
    state = hass.states.get(entity_id) if isinstance(entity_id, str) else None
    features: int | None = None
    if state is not None:
        try:
            features = int(state.attributes.get(ATTR_SUPPORTED_FEATURES, 0))
        except (TypeError, ValueError, OverflowError):
            features = None
    return {
        "entity": entity_id,
        "state": state.state if state else None,
        "generate_data": None
        if features is None
        else bool(features & AITaskEntityFeature.GENERATE_DATA),
        "attachments": None
        if features is None
        else bool(features & AITaskEntityFeature.SUPPORT_ATTACHMENTS),
    }


def _ts(event: ReelEvent) -> float | None:
    try:
        return event_ts(event)
    except (KeyError, TypeError, ValueError):
        return None


def _summary(event: ReelEvent) -> dict[str, Any]:
    """An event without what it shows: no text, no picture and no free-text labels."""
    category = event.get("category")
    labels = event.get("labels")
    return {
        "start": event.get("start"),
        "camera": event.get("camera"),
        "source": event.get("source"),
        "category": _own(category, OWN_CATEGORIES),
        "labels": [_own(label, OWN_LABELS) for label in labels]
        if isinstance(labels, list)
        else OTHER,
        "analysis": event.get("analysis"),
        "has_title": bool(event.get("title")),
        "has_description": bool(event.get("description")),
        "has_key_frame": bool(event.get("key_frame")),
    }


def _own(value: Any, own: set[str]) -> str:
    """`value` if it is one of Event Reel's own words, else "(other)"."""
    return value if isinstance(value, str) and value in own else OTHER
