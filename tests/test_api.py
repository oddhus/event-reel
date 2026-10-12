"""Services and websocket commands."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.components.camera import Image
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import CLIENT_ID, MockUser

from custom_components.event_reel._schema import vol
from custom_components.event_reel.const import DOMAIN, EVENT_EVENT_REEL

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


async def test_an_added_image_is_never_deleted(
    hass: HomeAssistant, set_up: SetUp, media_dir: Path
) -> None:
    """Removing or purging an event deletes only the key frame Event Reel saved for it."""
    outside = media_dir.parent / "secrets.yaml"
    outside.write_text("keep me")
    entry = await set_up()
    frames = media_dir / "event_reel" / "2026-10-08"
    frames.mkdir(parents=True)
    other_id = "0" * 32
    (frames / f"{other_id}.jpg").write_bytes(b"another event's frame")
    prefix = "media-source://media_source/local/event_reel/"

    escaping = await _add(hass, title="A", image=f"{prefix}../../secrets.yaml")
    absolute = await _add(hass, title="B", image=f"{prefix}/{outside}")
    borrowed = await _add(hass, title="C", image=f"{prefix}2026-10-08/{other_id}.jpg")
    for event_id in (escaping, absolute, borrowed):
        await hass.services.async_call(DOMAIN, "remove_event", {"id": event_id}, blocking=True)
    await _add(hass, title="Old", start="2020-01-01 00:00:00", image=f"{prefix}../../secrets.yaml")
    await entry.runtime_data.store.async_purge(dt_util.utcnow())

    assert outside.read_text() == "keep me"
    assert (frames / f"{other_id}.jpg").read_bytes() == b"another event's frame"
    assert entry.runtime_data.store.all() == []


async def test_describe_accepts_only_home_assistant_media(
    hass: HomeAssistant, set_up: SetUp
) -> None:
    await set_up()
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "describe",
            {"camera": "camera.garden", "video": "http://192.0.2.1/clip.mp4"},
            blocking=True,
            return_response=True,
        )


async def test_only_admins_and_automations_remove_events(
    hass: HomeAssistant, set_up: SetUp
) -> None:
    await set_up()
    event_id = await _add(hass, title="A")
    user = MockUser().add_to_hass(hass)

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN, "remove_event", {"id": event_id}, blocking=True, context=Context(user.id)
        )
    # No user: an automation.
    await hass.services.async_call(DOMAIN, "remove_event", {"id": event_id}, blocking=True)


async def test_purge_announces_each_removal(hass: HomeAssistant, set_up: SetUp) -> None:
    entry = await set_up()
    old = await _add(hass, title="Old", start=(dt_util.now() - timedelta(days=40)).isoformat())
    await _add(hass, title="New")
    bus_events = []
    hass.bus.async_listen(EVENT_EVENT_REEL, bus_events.append)

    assert await entry.runtime_data.store.async_purge(dt_util.utcnow() - timedelta(days=30)) == 1
    await hass.async_block_till_done()

    assert [(e.data["action"], e.data["event"]["id"]) for e in bus_events] == [("removed", old)]


async def test_removing_the_integration_deletes_its_events_and_frames(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, media_dir: Path
) -> None:
    entry = await set_up()
    with patch(
        "custom_components.event_reel.frames.camera.async_get_image",
        AsyncMock(return_value=Image("image/jpeg", b"img")),
    ):
        await hass.services.async_call(
            DOMAIN, "describe", {"camera": "camera.garden", "frames": 1}, blocking=True
        )
    assert list(media_dir.rglob("*.jpg"))
    await entry.runtime_data.store.async_flush()

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert not (media_dir / "event_reel").exists()
    entry = await set_up()
    assert entry.runtime_data.store.all() == []


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


async def test_users_see_only_events_of_cameras_they_may_read(
    hass: HomeAssistant, set_up: SetUp, hass_ws_client: Any
) -> None:
    await set_up()
    garden = await _add(hass, title="Garden", camera="camera.garden")
    await _add(hass, title="Hidden", camera="camera.bedroom")
    note = await _add(hass, title="No camera")
    user = MockUser().add_to_hass(hass)
    user.mock_policy({"entities": {"entity_ids": {"camera.garden": True}}})
    refresh_token = await hass.auth.async_create_refresh_token(user, CLIENT_ID)
    client = await hass_ws_client(hass, hass.auth.async_create_access_token(refresh_token))

    await client.send_json_auto_id({"type": "event_reel/events/list"})
    msg = await client.receive_json()
    assert sorted(e["id"] for e in msg["result"]["events"]) == sorted([garden, note])

    await client.send_json_auto_id({"type": "event_reel/events/subscribe"})
    assert (await client.receive_json())["success"]
    await _add(hass, title="Hidden again", camera="camera.bedroom")
    shown = await _add(hass, title="Garden again", camera="camera.garden")
    msg = await client.receive_json()
    assert msg["event"]["event"]["id"] == shown

    response = await hass.services.async_call(
        DOMAIN, "get_events", {}, blocking=True, return_response=True, context=Context(user.id)
    )
    assert sorted(e["id"] for e in response["events"]) == sorted([garden, note, shown])


async def test_websocket_reads_times_without_zone_in_home_assistant_time(
    hass: HomeAssistant, set_up: SetUp, hass_ws_client: Any
) -> None:
    await hass.config.async_set_time_zone("Europe/Oslo")
    await set_up()
    morning = await _add(hass, title="Morning", start="2026-10-08T08:30:00+02:00")
    client = await hass_ws_client(hass)

    # 08:00 Oslo is 06:00 UTC: read as UTC, the window would end before the event.
    await client.send_json_auto_id(
        {
            "type": "event_reel/events/list",
            "start_time": "2026-10-08T08:00:00",
            "end_time": "2026-10-08T09:00:00",
        }
    )
    msg = await client.receive_json()
    assert [e["id"] for e in msg["result"]["events"]] == [morning]


async def test_websocket_rejects_bad_time(
    hass: HomeAssistant, set_up: SetUp, hass_ws_client: Any
) -> None:
    await set_up()
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "event_reel/events/list", "start_time": "yesterday"})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_format"
