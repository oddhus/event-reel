"""Services and websocket commands."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.components.camera import Image
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest

from custom_components.event_reel.const import DOMAIN

from .conftest import FakeAI, SetUp


async def _add(hass: HomeAssistant, **data: Any) -> str:
    response = await hass.services.async_call(
        DOMAIN, "add_event", data, blocking=True, return_response=True
    )
    return response["id"]


async def test_add_and_get_events(hass: HomeAssistant, set_up: SetUp) -> None:
    await hass.config.async_set_time_zone("Europe/Oslo")
    await set_up()
    first = await _add(hass, title="Morning", start="2026-10-08 08:00:00", camera="camera.garden")
    second = await _add(hass, title="Noon", start="2026-10-08 12:00:00", category="animal")
    await _add(hass, title="Next day", start="2026-10-09 08:00:00")

    response = await hass.services.async_call(
        DOMAIN,
        "get_events",
        {"start": "2026-10-08 00:00:00", "end": "2026-10-08 23:59:59"},
        blocking=True,
        return_response=True,
    )

    events = response["events"]
    assert [e["id"] for e in events] == [second, first]
    assert events[1]["start"] == "2026-10-08T08:00:00+02:00"
    assert events[1]["camera"] == "camera.garden"
    assert events[0]["category"] == "animal"
    assert events[1]["category"] == "custom"


async def test_get_events_filters_by_camera_and_category(
    hass: HomeAssistant, set_up: SetUp
) -> None:
    await set_up()
    await _add(hass, title="A", camera="camera.garden", category="person")
    garden_animal = await _add(hass, title="B", camera="camera.garden", category="animal")
    await _add(hass, title="C", camera="camera.entrance", category="animal")

    response = await hass.services.async_call(
        DOMAIN,
        "get_events",
        {"cameras": ["camera.garden"], "categories": ["animal"]},
        blocking=True,
        return_response=True,
    )
    assert [e["id"] for e in response["events"]] == [garden_animal]


async def test_add_event_rejects_end_before_start(hass: HomeAssistant, set_up: SetUp) -> None:
    await set_up()
    with pytest.raises(ServiceValidationError):
        await _add(hass, title="X", start="2026-10-08 12:00:00", end="2026-10-08 11:00:00")


async def test_remove_event_deletes_its_key_frame(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, media_dir: Path
) -> None:
    hass.states.async_set("camera.garden", "idle", {"friendly_name": "Garden"})
    await set_up()
    with patch(
        "custom_components.event_reel.frames.camera.async_get_image",
        AsyncMock(return_value=Image("image/jpeg", b"img")),
    ):
        response = await hass.services.async_call(
            DOMAIN,
            "describe",
            {"camera": "camera.garden", "frames": 1},
            blocking=True,
            return_response=True,
        )
    event = response["event"]
    assert event["title"] == "Person rings the bell"
    assert len(list(media_dir.rglob("*.jpg"))) == 1

    await hass.services.async_call(DOMAIN, "remove_event", {"id": event["id"]}, blocking=True)

    assert list(media_dir.rglob("*.jpg")) == []
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "remove_event", {"id": event["id"]}, blocking=True)


async def test_describe_without_storing(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, media_dir: Path
) -> None:
    entry = await set_up()
    with patch(
        "custom_components.event_reel.frames.camera.async_get_image",
        AsyncMock(return_value=Image("image/png", b"png")),
    ):
        response = await hass.services.async_call(
            DOMAIN,
            "describe",
            {"camera": "camera.garden", "frames": 2, "store": False, "name": "Back garden"},
            blocking=True,
            return_response=True,
        )
    assert response["event"]["key_frame"] is None
    assert 'from the camera "Back garden"' in fake_ai.calls[0]["instructions"]
    assert entry.runtime_data.store.all() == []
    assert list(media_dir.rglob("*.jpg")) == []


async def test_old_events_are_purged_at_start(hass: HomeAssistant, set_up: SetUp) -> None:
    entry = await set_up(options={"retention_days": 30})
    old = (dt_util.now() - timedelta(days=31)).isoformat()
    recent = (dt_util.now() - timedelta(days=2)).isoformat()
    await _add(hass, title="Old", start=old)
    keep = await _add(hass, title="Recent", start=recent)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert [e["id"] for e in entry.runtime_data.store.all()] == [keep]


async def test_websocket_list_and_subscribe(
    hass: HomeAssistant, set_up: SetUp, hass_ws_client: Any
) -> None:
    await set_up()
    first = await _add(hass, title="One", start="2026-10-08T10:00:00+00:00")
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": "event_reel/events/list", "start_time": "2026-10-08T00:00:00+00:00"}
    )
    msg = await client.receive_json()
    assert msg["success"]
    assert [e["id"] for e in msg["result"]["events"]] == [first]

    await client.send_json_auto_id({"type": "event_reel/events/subscribe"})
    msg = await client.receive_json()
    assert msg["success"]

    second = await _add(hass, title="Two")
    msg = await client.receive_json()
    assert msg["type"] == "event"
    assert msg["event"]["action"] == "added"
    assert msg["event"]["event"]["id"] == second
    assert msg["event"]["event"]["title"] == "Two"


async def test_websocket_rejects_bad_time(
    hass: HomeAssistant, set_up: SetUp, hass_ws_client: Any
) -> None:
    await set_up()
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "event_reel/events/list", "start_time": "yesterday"})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_format"
