"""From a trigger or a Nest event to a stored event."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from aiohttp import ClientConnectionError
from homeassistant.components import media_source
from homeassistant.components.camera import Image
from homeassistant.components.http import ApiConfig
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.event_reel.analyzer import MediaNotReady
from custom_components.event_reel.const import EVENT_EVENT_REEL
from custom_components.event_reel.prompt import STRUCTURE

from .conftest import FakeAI, SetUp, camera_subentry, wait_until

CAMERA = "camera.entrance"
TRIGGER = "binary_sensor.entrance_person"


@pytest.fixture
def snapshot() -> Any:
    with patch(
        "custom_components.event_reel.frames.camera.async_get_image",
        AsyncMock(return_value=Image("image/jpeg", b"snapshot-bytes")),
    ) as mock:
        yield mock


async def _events(hass: HomeAssistant, entry: MockConfigEntry) -> list[dict[str, Any]]:
    data = entry.runtime_data
    await data.analyzer.async_join()
    return sorted(data.store.all(), key=lambda e: e["start"])


async def _file(hass: HomeAssistant, media_id: str) -> bytes:
    resolved = await media_source.async_resolve_media(hass, media_id, None)
    assert resolved.path is not None
    return resolved.path.read_bytes()


async def _trigger(hass: HomeAssistant) -> None:
    hass.states.async_set(TRIGGER, "off", {"device_class": "occupancy"})
    await hass.async_block_till_done()
    hass.states.async_set(TRIGGER, "on", {"device_class": "occupancy"})
    await hass.async_block_till_done()


async def test_trigger_snapshots_are_described_and_stored(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock, media_dir: Path
) -> None:
    hass.states.async_set(CAMERA, "idle", {"friendly_name": "Entrance"})
    bus_events = []
    hass.bus.async_listen(EVENT_EVENT_REEL, bus_events.append)
    entry = await set_up(
        options={"ai_task_entity": "ai_task.local"},
        cameras=[camera_subentry(CAMERA, triggers=[TRIGGER])],
    )

    await _trigger(hass)
    events = await _events(hass, entry)

    assert len(events) == 1
    event = events[0]
    assert event["camera"] == CAMERA
    assert event["title"] == "Person rings the bell"
    assert event["description"] == "One person in a red jacket waits at the door."
    assert event["category"] == "person"
    assert event["labels"] == ["person"]
    assert event["source"] == "trigger"
    assert event["trigger"] == TRIGGER
    assert event["analysis"] == "ok"
    assert await _file(hass, event["key_frame"]) == b"jpeg:snapshot-bytes"

    assert snapshot.await_count == 3
    (call,) = fake_ai.calls
    assert call["entity_id"] == "ai_task.local"
    assert call["structure"] == STRUCTURE
    assert len(call["attachments"]) == 3
    assert 'from the camera "Entrance"' in call["instructions"]
    assert "in Norwegian Bokmål" in call["instructions"]
    # The frames sent to the AI are cleaned up afterwards; only the key frame stays.
    assert not any(path.exists() for path in fake_ai.attachment_files[0])
    assert [p.name for p in media_dir.rglob("*.jpg")] == [f"{event['id']}.jpg"]

    await hass.async_block_till_done()
    assert [e.data["action"] for e in bus_events] == ["added"]
    assert bus_events[0].data["event"]["id"] == event["id"]


async def test_nothing_seen_is_not_stored(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    fake_ai.answer = {
        "activity": False,
        "subject": "other",
        "title": "Nothing",
        "description": "Empty.",
    }
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER])])

    await _trigger(hass)

    assert await _events(hass, entry) == []
    assert len(fake_ai.calls) == 1


async def test_nothing_seen_is_kept_when_asked(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    fake_ai.answer = {
        "activity": False,
        "subject": "other",
        "title": "Nothing",
        "description": "Empty.",
    }
    entry = await set_up(
        options={"keep_no_activity": True}, cameras=[camera_subentry(CAMERA, triggers=[TRIGGER])]
    )

    await _trigger(hass)

    assert [e["title"] for e in await _events(hass, entry)] == ["Nothing"]


async def test_cooldown_skips_repeated_triggers(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=90)])

    await _trigger(hass)
    await _trigger(hass)

    assert len(await _events(hass, entry)) == 1
    assert len(fake_ai.calls) == 1


async def test_failed_ai_still_stores_the_event(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    fake_ai.answer = HomeAssistantError("model not found")
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER])])

    await _trigger(hass)

    (event,) = await _events(hass, entry)
    assert event["analysis"] == "failed"
    assert event["title"] == ""
    assert event["category"] == "person"
    assert event["key_frame"] is not None


def _nest_camera(hass: HomeAssistant) -> str:
    nest_entry = MockConfigEntry(domain="nest")
    nest_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=nest_entry.entry_id, identifiers={("nest", "enterprises/x/devices/y")}
    )
    er.async_get(hass).async_get_or_create(
        "camera", "nest", "doorbell-1", suggested_object_id="doorbell", device_id=device.id
    )
    return device.id


def _nest_event(device_id: str, kind: str, token: str) -> dict[str, Any]:
    return {
        "device_id": device_id,
        "type": kind,
        "timestamp": "2026-10-08T11:44:55+00:00",
        "nest_event_id": token,
        "attachment": {
            "image": f"/api/nest/event_media/{device_id}/{token}/thumbnail",
            "video": f"/api/nest/event_media/{device_id}/{token}",
        },
    }


async def test_nest_event_is_described_from_its_clip(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    device_id = _nest_camera(hass)
    fetch = AsyncMock(return_value=b"mp4-bytes")
    with patch("custom_components.event_reel.analyzer.Analyzer._fetch", fetch):
        entry = await set_up(cameras=[camera_subentry("camera.doorbell", frames=3)])
        hass.bus.async_fire("nest_event", _nest_event(device_id, "camera_person", "TOKEN1"))
        await hass.async_block_till_done()
        events = await _events(hass, entry)

    fetch.assert_awaited_once_with(f"/api/nest/event_media/{device_id}/TOKEN1")
    (event,) = events
    assert event["nest"] == {"device_id": device_id, "event_token": "TOKEN1", "video": True}
    assert event["camera"] == "camera.doorbell"
    assert event["source"] == "nest"
    assert event["start"].startswith("2026-10-08T")
    # The fake video has 8 samples; 3 evenly spread are 1, 5 and 8, and the middle one is kept.
    assert await _file(hass, event["key_frame"]) == b"frame 5"
    assert len(fake_ai.calls[0]["attachments"]) == 3


async def test_nest_events_of_other_devices_and_types_are_ignored(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    device_id = _nest_camera(hass)
    with patch(
        "custom_components.event_reel.analyzer.Analyzer._fetch", AsyncMock(return_value=b"x")
    ):
        entry = await set_up(cameras=[camera_subentry("camera.doorbell", nest_events=["chime"])])
        hass.bus.async_fire("nest_event", _nest_event("other-device", "doorbell_chime", "T1"))
        hass.bus.async_fire("nest_event", _nest_event(device_id, "camera_person", "T2"))
        await hass.async_block_till_done()
        assert await _events(hass, entry) == []
    assert fake_ai.calls == []


async def test_a_ring_is_kept_even_when_nobody_is_seen(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    fake_ai.answer = {
        "activity": False,
        "subject": "other",
        "title": "No visible activity",
        "description": "Nobody is on the steps.",
    }
    device_id = _nest_camera(hass)
    with patch(
        "custom_components.event_reel.analyzer.Analyzer._fetch", AsyncMock(return_value=b"x")
    ):
        entry = await set_up(cameras=[camera_subentry("camera.doorbell")])
        hass.bus.async_fire("nest_event", _nest_event(device_id, "doorbell_chime", "T1"))
        await hass.async_block_till_done()
        (event,) = await _events(hass, entry)
    assert event["category"] == "doorbell"
    assert event["labels"] == ["chime"]
    # The ring is the event: no "no activity" title, but the clip's description stays.
    assert event["title"] == ""
    assert event["description"] == "Nobody is on the steps."
    assert event["analysis"] == "ok"


async def test_a_ring_during_the_cooldown_marks_the_event_as_a_ring(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    device_id = _nest_camera(hass)
    with patch(
        "custom_components.event_reel.analyzer.Analyzer._fetch", AsyncMock(return_value=b"x")
    ):
        entry = await set_up(cameras=[camera_subentry("camera.doorbell")])
        hass.bus.async_fire("nest_event", _nest_event(device_id, "camera_person", "T1"))
        await hass.async_block_till_done()
        await entry.runtime_data.analyzer.async_join()
        hass.bus.async_fire("nest_event", _nest_event(device_id, "doorbell_chime", "T2"))
        await hass.async_block_till_done()
        (event,) = await _events(hass, entry)
    assert event["category"] == "doorbell"
    assert event["labels"] == ["person", "chime"]
    assert event["nest"]["event_token"] == "T1"
    assert len(fake_ai.calls) == 1


async def test_a_ring_after_an_empty_look_gets_its_own_event(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    """Nobody was seen at the person event, so the ring in its cooldown is described itself."""
    fake_ai.answer = {"activity": False, "subject": "other", "title": "Empty", "description": None}
    device_id = _nest_camera(hass)
    with patch(
        "custom_components.event_reel.analyzer.Analyzer._fetch", AsyncMock(return_value=b"x")
    ):
        entry = await set_up(cameras=[camera_subentry("camera.doorbell", cooldown=90)])
        hass.bus.async_fire("nest_event", _nest_event(device_id, "camera_person", "T1"))
        await hass.async_block_till_done()
        assert await _events(hass, entry) == []
        hass.bus.async_fire("nest_event", _nest_event(device_id, "doorbell_chime", "T2"))
        await hass.async_block_till_done()
        (event,) = await _events(hass, entry)
    assert event["category"] == "doorbell"
    assert event["labels"] == ["chime"]
    assert event["nest"]["event_token"] == "T2"
    assert len(fake_ai.calls) == 2


async def test_a_ring_marks_only_the_moment_of_its_own_cooldown(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    """An older job that finishes late isn't taken for the newer moment the ring belongs to."""
    device_id = _nest_camera(hass)
    clock = [1000.0]
    fake_ai.gate = asyncio.Event()
    with (
        patch(
            "custom_components.event_reel.analyzer.Analyzer._fetch", AsyncMock(return_value=b"x")
        ),
        # Only the analyzer's clock: asyncio's own must keep running.
        patch(
            "custom_components.event_reel.analyzer.time",
            SimpleNamespace(monotonic=lambda: clock[0]),
        ),
    ):
        entry = await set_up(cameras=[camera_subentry("camera.doorbell", cooldown=90)])
        hass.bus.async_fire("nest_event", _nest_event(device_id, "camera_person", "T1"))
        await hass.async_block_till_done()
        await wait_until(lambda: len(fake_ai.calls) == 1)
        # The first description is still running when its cooldown ends and a new one starts.
        clock[0] += 120
        hass.bus.async_fire("nest_event", _nest_event(device_id, "camera_person", "T2"))
        await hass.async_block_till_done()
        first_gate, fake_ai.gate = fake_ai.gate, asyncio.Event()
        first_gate.set()
        # T1 is stored and T2 is being described when the ring comes.
        await wait_until(lambda: len(fake_ai.calls) == 2)
        hass.bus.async_fire("nest_event", _nest_event(device_id, "doorbell_chime", "T3"))
        await hass.async_block_till_done()
        fake_ai.gate.set()
        events = await _events(hass, entry)
    rings = {e["nest"]["event_token"]: "chime" in e["labels"] for e in events}
    assert rings == {"T1": False, "T2": True}


async def test_snapshots_are_taken_when_the_trigger_fires(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    """A second camera's snapshots don't wait for the AI to finish with the first camera."""
    other_trigger = "binary_sensor.garden_person"
    fake_ai.gate = asyncio.Event()
    entry = await set_up(
        cameras=[
            camera_subentry(CAMERA, triggers=[TRIGGER]),
            camera_subentry("camera.garden", triggers=[other_trigger]),
        ]
    )
    hass.states.async_set(other_trigger, "off")
    await _trigger(hass)
    hass.states.async_set(other_trigger, "on")
    await hass.async_block_till_done()

    await wait_until(lambda: snapshot.await_count == 6)
    assert len(fake_ai.calls) == 1  # the first answer is still pending
    assert {c.args[1] for c in snapshot.await_args_list} == {CAMERA, "camera.garden"}

    fake_ai.gate.set()
    events = await _events(hass, entry)
    assert sorted(e["camera"] for e in events) == ["camera.entrance", "camera.garden"]


async def test_an_ai_that_never_answers_is_given_up_on(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    fake_ai.gate = asyncio.Event()  # never set
    with patch("custom_components.event_reel.analyzer.AI_TIMEOUT", 0.05):
        entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER])])
        await _trigger(hass)
        (event,) = await _events(hass, entry)
    assert event["analysis"] == "failed"
    assert event["key_frame"] is not None


async def test_a_network_error_keeps_the_ring_without_text(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    device_id = _nest_camera(hass)
    with patch(
        "custom_components.event_reel.analyzer.Analyzer._fetch",
        AsyncMock(side_effect=ClientConnectionError("refused")),
    ):
        entry = await set_up(cameras=[camera_subentry("camera.doorbell")])
        hass.bus.async_fire("nest_event", _nest_event(device_id, "doorbell_chime", "T1"))
        await hass.async_block_till_done()
        (event,) = await _events(hass, entry)
    assert event["analysis"] == "failed"
    assert event["category"] == "doorbell"
    assert fake_ai.calls == []


async def test_a_nest_camera_without_clips_is_described_from_its_full_image(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    device_id = _nest_camera(hass)
    nest_event = _nest_event(device_id, "camera_person", "T1")
    del nest_event["attachment"]["video"]
    fetch = AsyncMock(return_value=b"jpeg")
    with patch("custom_components.event_reel.analyzer.Analyzer._fetch", fetch):
        entry = await set_up(cameras=[camera_subentry("camera.doorbell")])
        hass.bus.async_fire("nest_event", nest_event)
        await hass.async_block_till_done()
        (event,) = await _events(hass, entry)
    fetch.assert_awaited_once_with(f"/api/nest/event_media/{device_id}/T1")
    assert event["nest"]["video"] is False
    assert len(fake_ai.calls[0]["attachments"]) == 1


async def test_fetch_signs_the_path_and_hides_the_signature_on_errors(
    hass: HomeAssistant, set_up: SetUp, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await set_up()
    hass.config.api = ApiConfig("192.0.2.10", "0.0.0.0", 8123, False)
    analyzer = entry.runtime_data.analyzer
    aioclient_mock.get("http://127.0.0.1:8123/api/nest/event_media/d/ok", content=b"clip")
    aioclient_mock.get("http://127.0.0.1:8123/api/nest/event_media/d/late", status=404)
    aioclient_mock.get(
        "http://127.0.0.1:8123/api/nest/event_media/d/down", exc=ClientConnectionError()
    )

    assert await analyzer._fetch("/api/nest/event_media/d/ok") == b"clip"
    (_method, url, _data, _headers) = aioclient_mock.mock_calls[0]
    assert "authSig=" in str(url)
    with pytest.raises(MediaNotReady):
        await analyzer._fetch("/api/nest/event_media/d/late")
    with pytest.raises(HomeAssistantError) as err:
        await analyzer._fetch("/api/nest/event_media/d/down")
    assert "authSig" not in str(err.value)


async def test_nest_clip_that_is_late_is_fetched_again(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    device_id = _nest_camera(hass)
    fetch = AsyncMock(side_effect=[MediaNotReady("404"), MediaNotReady("404"), b"mp4"])
    with (
        patch("custom_components.event_reel.analyzer.Analyzer._fetch", fetch),
        patch("custom_components.event_reel.analyzer.MEDIA_RETRY_DELAYS", (0, 0, 0)),
    ):
        entry = await set_up(cameras=[camera_subentry("camera.doorbell")])
        hass.bus.async_fire("nest_event", _nest_event(device_id, "camera_person", "T1"))
        await hass.async_block_till_done()
        (event,) = await _events(hass, entry)
    assert fetch.await_count == 3
    assert event["analysis"] == "ok"


async def test_structure_passes_the_ai_task_service_schema(
    hass: HomeAssistant, set_up: SetUp
) -> None:
    """HA's own validator accepts what we send; only the missing entity fails."""
    await set_up(replace_ai=False)
    with pytest.raises(HomeAssistantError, match="not found"):
        await hass.services.async_call(
            "ai_task",
            "generate_data",
            {
                "task_name": "test",
                "entity_id": "ai_task.missing",
                "instructions": "Describe.",
                "structure": STRUCTURE,
            },
            blocking=True,
            return_response=True,
        )
