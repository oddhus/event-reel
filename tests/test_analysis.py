"""From a trigger or a Nest event to a stored event."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import shutil
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from aiohttp import ClientConnectionError
from homeassistant.components import media_source
from homeassistant.components.camera import Image
from homeassistant.components.http import ApiConfig
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.event_reel.analyzer import MediaNotReady
from custom_components.event_reel.const import EVENT_EVENT_REEL
from custom_components.event_reel.prompt import STRUCTURE

from .conftest import FakeAI, FakeFFmpeg, SetUp, camera_subentry, wait_until

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


OCCUPANCY = {"device_class": "occupancy"}
MOTION = {"device_class": "motion"}
SOUND = {"device_class": "sound"}
# What the AI is told, and the category when the AI names no subject, per label.
HINTS = {
    "person": ("a person", "person"),
    "vehicle": ("a vehicle", "vehicle"),
    "animal": ("an animal", "animal"),
    "package": ("a package", "package"),
    "sound": ("a sound", "motion"),
    "motion": ("motion", "motion"),
}


@pytest.mark.parametrize(
    ("entity_id", "attributes", "label"),
    [
        # Frigate: every object sensor has the occupancy device class.
        ("binary_sensor.drive_car_occupancy", OCCUPANCY, "vehicle"),
        ("binary_sensor.garden_deer_occupancy", OCCUPANCY, "animal"),
        ("binary_sensor.drive_license_plate_occupancy", OCCUPANCY, "vehicle"),
        ("binary_sensor.step_amazon_occupancy", OCCUPANCY, "package"),
        # `all` is any object.
        ("binary_sensor.drive_all_occupancy", OCCUPANCY, "motion"),
        # Frigate's audio sensors hear a dog; they don't see one.
        ("binary_sensor.garden_dog_sound", SOUND, "sound"),
        ("binary_sensor.nursery_noise", SOUND, "sound"),
        ("binary_sensor.nursery_noise", {}, "sound"),
        # The sound class decides, whatever the words.
        ("binary_sensor.drive_car", SOUND, "sound"),
        # The camera's own name comes first and doesn't decide.
        ("binary_sensor.car_port_person_occupancy", OCCUPANCY, "person"),
        ("binary_sensor.car_port_all_occupancy", OCCUPANCY, "motion"),
        ("binary_sensor.bus_stop_motion", MOTION, "motion"),
        ("binary_sensor.cat_flap_motion", MOTION, "motion"),
        ("binary_sensor.car_port_occupancy", OCCUPANCY, "person"),
        ("binary_sensor.car_port_presence", {"device_class": "presence"}, "person"),
        ("binary_sensor.cat_flap_baby_cry_detected", {}, "sound"),
        ("binary_sensor.cat_flap_glass_break_detected", {}, "sound"),
        ("binary_sensor.car_port_sensor_2", {"friendly_name": "Car port person"}, "person"),
        # Dahua: a sensor that sees doesn't hear.
        ("binary_sensor.gate_motion_alarm", MOTION, "motion"),
        ("binary_sensor.gate_cross_line_alarm", MOTION, "motion"),
        # Reolink and UniFi Protect.
        ("binary_sensor.garden_pet", {}, "animal"),
        ("binary_sensor.step_package_detected", {}, "package"),
        ("binary_sensor.drive_car_alarm_detected", {}, "sound"),
        ("binary_sensor.garden_audio_object_detected", {}, "sound"),
        ("binary_sensor.garden_object_detected", {}, "motion"),
        # Reolink's dual-lens cameras.
        ("binary_sensor.gate_vehicle_lens_1", {}, "vehicle"),
        # Other cameras.
        ("binary_sensor.gate_smart_motion_human", {}, "person"),
        ("binary_sensor.gate_persons", {}, "person"),
        ("binary_sensor.drive_bike_detector", {}, "vehicle"),
        ("binary_sensor.drive_lorry_detection", {}, "vehicle"),
        ("binary_sensor.drive_cat_triggered", {}, "animal"),
        ("binary_sensor.drive_dog_occupancy_sensor_2", OCCUPANCY, "animal"),
        # `person` anywhere when the last words name nothing: no camera is named for a
        # person. A person's motion is a person, but a place is no person.
        ("binary_sensor.person_front_door", {}, "person"),
        ("binary_sensor.front_person_motion", MOTION, "person"),
        ("binary_sensor.person_door_car", {}, "vehicle"),
        ("binary_sensor.human_resources_motion", MOTION, "motion"),
        ("binary_sensor.person_cam_all_occupancy", OCCUPANCY, "motion"),
        ("binary_sensor.front_person_all_occupancy", OCCUPANCY, "motion"),
        ("binary_sensor.front_people_all_occupancy", OCCUPANCY, "motion"),
        ("binary_sensor.garden_dog_presence", {"device_class": "presence"}, "animal"),
        # A sensor that sees doesn't hear, so a sound names nothing.
        ("binary_sensor.gate_dog_bark", MOTION, "motion"),
        ("binary_sensor.gate_car_alarm", MOTION, "motion"),
        ("binary_sensor.car_audio", MOTION, "motion"),
        ("binary_sensor.drive_person_sound", MOTION, "person"),
        ("binary_sensor.gate_alarm", OCCUPANCY, "person"),
        ("binary_sensor.gate_dog_bark", OCCUPANCY, "person"),
        ("binary_sensor.gate_dog_bark", {"device_class": "presence"}, "person"),
        ("binary_sensor.gate_car_alarm", {"device_class": "moving"}, "motion"),
        # Nor does one that smells: a smoke or gas detector, or a fire alarm.
        ("binary_sensor.kitchen_smoke_alarm", {"device_class": "smoke"}, "motion"),
        ("binary_sensor.boiler_co_alarm", {"device_class": "gas"}, "motion"),
        ("binary_sensor.hall_fire_alarm", {"device_class": "safety"}, "motion"),
        # Others may: UniFi Protect's CO alarm sensor has the carbon monoxide class.
        ("binary_sensor.hall_co_alarm_detected", {"device_class": "carbon_monoxide"}, "sound"),
        (
            "binary_sensor.sensor_5",
            {"device_class": "carbon_monoxide", "friendly_name": "Hall CO alarm detected"},
            "sound",
        ),
        ("binary_sensor.window_glass_break", {"device_class": "window"}, "sound"),
        ("binary_sensor.front_door_alarm", {"device_class": "door"}, "motion"),
        ("binary_sensor.cam_fire_alarm_detected", {}, "sound"),
        ("binary_sensor.sensor_4", MOTION | {"friendly_name": "Gate car alarm"}, "motion"),
        ("binary_sensor.hall_co_alarm", {}, "sound"),
        # `alarm`, `break` and other trailing words say nothing on their own.
        ("binary_sensor.drive_motion_alarm", {}, "motion"),
        ("binary_sensor.house_alarm_triggered", {}, "motion"),
        ("binary_sensor.gate_beam_break", {}, "motion"),
        ("binary_sensor.drive_vehicle_alert", {}, "vehicle"),
        ("binary_sensor.drive_vehicle_alarm", {}, "vehicle"),
        ("binary_sensor.garden_dog_detect", {}, "animal"),
        ("binary_sensor.drive_car_active", OCCUPANCY, "vehicle"),
        ("binary_sensor.hall_smoke_alarm_detected", {}, "sound"),
        ("binary_sensor.drive_vehicle_state", {}, "vehicle"),
        # Tapo and Eufy.
        ("binary_sensor.porch_package_delivery", MOTION, "package"),
        ("binary_sensor.porch_package_delivered", {}, "package"),
        ("binary_sensor.porch_package_pickup", {}, "package"),
        ("binary_sensor.porch_package_taken", {}, "package"),
        ("binary_sensor.porch_package_stranded", {}, "package"),
        # Reolink names its bicycle sensor `non_motor_vehicle`.
        ("binary_sensor.front_non_motor_vehicle", MOTION, "vehicle"),
        ("binary_sensor.porch_dog_lick_detected", {}, "animal"),
        ("binary_sensor.lawn_dog_poop", {}, "animal"),
        # Frigate objects of two words.
        ("binary_sensor.step_canada_post_occupancy", OCCUPANCY, "package"),
        ("binary_sensor.step_royal_mail_occupancy", OCCUPANCY, "package"),
        ("binary_sensor.step_an_post_occupancy", OCCUPANCY, "package"),
        ("binary_sensor.room_teddy_bear_occupancy", OCCUPANCY, "person"),
        ("binary_sensor.room_hot_dog_occupancy", OCCUPANCY, "person"),
        # A face is a person as the last word, but anywhere else it can be a place.
        ("binary_sensor.door_face_detected", {}, "person"),
        ("binary_sensor.inngang_ansikt", {}, "person"),
        ("binary_sensor.north_face_motion", MOTION, "motion"),
        ("binary_sensor.north_face_gate", {}, "motion"),
        ("binary_sensor.nord_ansikt_port", {}, "motion"),
        # The entity id goes first: a renamed sensor's name is often just a place.
        ("binary_sensor.front_motion", MOTION | {"friendly_name": "Front vehicle"}, "motion"),
        ("binary_sensor.north_face_motion", MOTION | {"friendly_name": "North Face"}, "motion"),
        ("binary_sensor.car_port_motion", MOTION | {"friendly_name": "Car"}, "motion"),
        ("binary_sensor.drive_motion", MOTION | {"friendly_name": "Human-free zone"}, "motion"),
        ("binary_sensor.sensor_3", {"friendly_name": "Person at gate"}, "person"),
        # A person word anywhere counts only when neither text names a detector.
        ("binary_sensor.person_front_door", {"friendly_name": "Gate vehicle"}, "vehicle"),
        # An event entity.
        ("event.drive_vehicle", {}, "vehicle"),
        # An entity id made in Norwegian, and Norwegian names.
        ("binary_sensor.innkjorsel_kjoretoy", {}, "vehicle"),
        ("binary_sensor.innkjorsel_sykkel_oppdaget", {}, "vehicle"),
        ("binary_sensor.innkjorsel_varebil", {}, "vehicle"),
        ("binary_sensor.innkjorsel_bilalarm_oppdaget", {}, "sound"),
        ("binary_sensor.hage_hund_tilstedevaerelse", OCCUPANCY, "animal"),
        (
            "binary_sensor.detector_7",
            OCCUPANCY | {"friendly_name": "Kjøretøy tilstedeværelse"},
            "vehicle",
        ),
        (
            "binary_sensor.detector_8",
            OCCUPANCY | {"friendly_name": "Hage: fugl (belegg)"},
            "animal",
        ),
        ("binary_sensor.detector_9", {"friendly_name": "Gate light 2"}, "motion"),
        # Only the name says it; the entity id goes first when both do.
        ("binary_sensor.sensor_7", {"friendly_name": "Garden dog"}, "animal"),
        ("binary_sensor.drive_car", {"friendly_name": "Drive person"}, "vehicle"),
        # Neither says it.
        ("binary_sensor.hall_pir", MOTION, "motion"),
        ("binary_sensor.hall", OCCUPANCY, "person"),
        ("binary_sensor.hall", {"device_class": "presence"}, "person"),
        ("binary_sensor.hall", {}, "motion"),
    ],
)
async def test_a_trigger_is_labelled_with_what_it_looks_for(
    hass: HomeAssistant,
    set_up: SetUp,
    fake_ai: FakeAI,
    snapshot: AsyncMock,
    entity_id: str,
    attributes: dict[str, str],
    label: str,
) -> None:
    hint, category = HINTS[label]
    # The AI names no subject, so the category comes from the detector.
    fake_ai.answer = {"activity": True, "subject": "other", "title": "T", "description": "D."}
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[entity_id])])

    # An event entity's state is the time of its last event.
    off, on = (
        ("2026-11-01T10:00:00+00:00", "2026-11-01T10:05:00+00:00")
        if entity_id.startswith("event.")
        else ("off", "on")
    )
    hass.states.async_set(entity_id, off, attributes)
    await hass.async_block_till_done()
    hass.states.async_set(entity_id, on, attributes)
    await hass.async_block_till_done()

    (event,) = await _events(hass, entry)
    assert event["labels"] == [label]
    assert event["category"] == category
    instructions = fake_ai.calls[0]["instructions"]
    assert f"A detector reported {hint}. The detector can be wrong." in instructions
    assert instructions.count("A detector reported") == 1


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


DOORBELL = "event.entrance_doorbell"


async def _ring(hass: HomeAssistant, at: str = "2026-10-08T12:00:00+00:00") -> None:
    """A doorbell event entity that never fired before fires now."""
    if hass.states.get(DOORBELL) is None:
        hass.states.async_set(DOORBELL, "unknown", {"device_class": "doorbell"})
        await hass.async_block_till_done()
    hass.states.async_set(DOORBELL, at, {"device_class": "doorbell", "event_type": "ring"})
    await hass.async_block_till_done()


async def test_a_doorbell_event_entity_is_a_ring(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    fake_ai.answer = {
        "activity": False,
        "subject": "other",
        "title": "No activity",
        "description": "The porch is empty.",
    }
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[DOORBELL])])

    await _ring(hass)
    (event,) = await _events(hass, entry)

    # Kept although the AI saw nobody, and titled like a Nest ring nobody was seen at.
    assert event["category"] == "doorbell"
    assert event["labels"] == ["chime"]
    assert event["title"] == ""
    assert event["description"] == "The porch is empty."
    assert event["source"] == "trigger"
    assert event["trigger"] == DOORBELL
    (call,) = fake_ai.calls
    assert "pressed the doorbell" in call["instructions"]
    assert "can be wrong" not in call["instructions"]


async def test_a_doorbell_event_entity_is_a_ring_whatever_its_name(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    doorbell = "event.entrance_person"
    fake_ai.answer = NOTHING
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[doorbell])])

    hass.states.async_set(doorbell, "unknown", {"device_class": "doorbell"})
    await hass.async_block_till_done()
    hass.states.async_set(doorbell, "2026-10-08T12:00:00+00:00", {"device_class": "doorbell"})
    await hass.async_block_till_done()
    (event,) = await _events(hass, entry)

    assert event["labels"] == ["chime"]
    assert event["category"] == "doorbell"


async def test_a_doorbell_ring_during_a_trigger_cooldown_marks_the_event(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    entry = await set_up(
        cameras=[camera_subentry(CAMERA, triggers=[TRIGGER, DOORBELL], cooldown=90)]
    )

    await _trigger(hass)
    (first,) = await _events(hass, entry)
    await _ring(hass)
    events = await _events(hass, entry)

    assert [e["id"] for e in events] == [first["id"]]
    assert events[0]["labels"] == ["person", "chime"]
    assert events[0]["category"] == "doorbell"
    assert events[0]["title"] == "Person rings the bell"
    assert len(fake_ai.calls) == 1


NOTHING = {"activity": False, "subject": "other", "title": "Nothing", "description": "Empty."}


async def test_a_doorbell_ring_while_the_look_runs_makes_it_a_ring(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    fake_ai.answer = NOTHING
    fake_ai.gate = asyncio.Event()
    entry = await set_up(
        cameras=[camera_subentry(CAMERA, triggers=[TRIGGER, DOORBELL], cooldown=90)]
    )

    await _trigger(hass)
    await wait_until(lambda: len(fake_ai.calls) == 1)
    await _ring(hass)
    fake_ai.gate.set()
    (event,) = await _events(hass, entry)

    # Kept although the AI saw nobody, because the bell rang during the look.
    assert event["labels"] == ["person", "chime"]
    assert event["category"] == "doorbell"
    assert event["title"] == ""
    assert event["trigger"] == TRIGGER
    assert len(fake_ai.calls) == 1
    assert snapshot.await_count == 3


async def test_a_doorbell_ring_after_an_empty_look_gets_its_own_event(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    fake_ai.answer = NOTHING
    snapshot.side_effect = [Image("image/jpeg", f"shot-{i}".encode()) for i in range(6)]
    entry = await set_up(
        cameras=[camera_subentry(CAMERA, triggers=[TRIGGER, DOORBELL], cooldown=90)]
    )

    await _trigger(hass)
    assert await _events(hass, entry) == []
    await _ring(hass)
    (event,) = await _events(hass, entry)

    assert event["labels"] == ["chime"]
    assert event["category"] == "doorbell"
    assert event["title"] == ""
    assert event["trigger"] == DOORBELL
    # The ring was described from snapshots of its own moment.
    assert fake_ai.attachments == [
        [b"jpeg:shot-0", b"jpeg:shot-1", b"jpeg:shot-2"],
        [b"jpeg:shot-3", b"jpeg:shot-4", b"jpeg:shot-5"],
    ]


async def test_a_ring_clears_the_title_of_a_kept_empty_look(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    fake_ai.answer = NOTHING
    entry = await set_up(
        options={"keep_no_activity": True},
        cameras=[camera_subentry(CAMERA, triggers=[TRIGGER, DOORBELL], cooldown=90)],
    )

    await _trigger(hass)
    (kept,) = await _events(hass, entry)
    assert kept["title"] == "Nothing"
    await _ring(hass)
    (event,) = await _events(hass, entry)

    # The kept event was marked, not described again.
    assert event["id"] == kept["id"]
    assert len(fake_ai.calls) == 1
    assert event["labels"] == ["person", "chime"]
    assert event["category"] == "doorbell"
    assert event["title"] == ""
    assert event["description"] == "Empty."


async def test_other_event_entities_and_binary_sensors_are_not_rings(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    motion_event = "event.entrance_motion"
    doorbell_sensor = "binary_sensor.entrance_doorbell"
    fake_ai.answer = NOTHING
    entry = await set_up(
        cameras=[camera_subentry(CAMERA, triggers=[motion_event, doorbell_sensor], cooldown=0)]
    )

    hass.states.async_set(motion_event, "unknown", {"device_class": "motion"})
    hass.states.async_set(doorbell_sensor, "off")
    await hass.async_block_till_done()
    hass.states.async_set(motion_event, "2026-10-08T12:00:00+00:00", {"device_class": "motion"})
    hass.states.async_set(doorbell_sensor, "on")
    await hass.async_block_till_done()

    # Neither is a ring, so a look that finds nothing leaves no event.
    assert await _events(hass, entry) == []
    assert len(fake_ai.calls) == 2
    for call in fake_ai.calls:
        assert "can be wrong" in call["instructions"]
        assert "pressed the doorbell" not in call["instructions"]


COURIER = {
    "activity": True,
    "subject": "person",
    "title": "Courier walks up",
    "description": "A courier walks up to the door.",
}


@pytest.fixture
def clock() -> Any:
    """The analyzer's clock, in seconds; asyncio's own keeps running."""
    now = [1000.0]
    with patch(
        "custom_components.event_reel.analyzer.time", SimpleNamespace(monotonic=lambda: now[0])
    ):
        yield now


async def test_a_look_that_finds_nothing_shortens_the_cooldown(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock, clock: list[float]
) -> None:
    fake_ai.answer = NOTHING
    fake_ai.gate = asyncio.Event()
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=90)])

    await _trigger(hass)
    await wait_until(lambda: len(fake_ai.calls) == 1)
    # While the first look is still running, the camera is cooling down.
    await _trigger(hass)
    # A slow model takes half a minute.
    clock[0] += 30
    fake_ai.gate.set()
    assert await _events(hass, entry) == []
    # Within 10 s of the empty look's end, the camera still waits, so a flapping trigger
    # leaves the AI idle in between.
    clock[0] += 9
    await _trigger(hass)
    await entry.runtime_data.analyzer.async_join()
    assert len(fake_ai.calls) == 1

    # Someone who arrives after that is described, long before the 90 s are over.
    clock[0] += 2
    fake_ai.answer = COURIER
    await _trigger(hass)
    assert [e["title"] for e in await _events(hass, entry)] == ["Courier walks up"]
    assert len(fake_ai.calls) == 2
    # That look made an event, so its whole cooldown holds.
    clock[0] += 60
    await _trigger(hass)
    await entry.runtime_data.analyzer.async_join()
    assert len(fake_ai.calls) == 2


async def test_a_kept_empty_look_keeps_the_whole_cooldown(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock, clock: list[float]
) -> None:
    fake_ai.answer = NOTHING
    entry = await set_up(
        options={"keep_no_activity": True},
        cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=90)],
    )

    await _trigger(hass)
    assert len(await _events(hass, entry)) == 1
    clock[0] += 60
    await _trigger(hass)
    await entry.runtime_data.analyzer.async_join()
    assert len(fake_ai.calls) == 1


async def test_an_old_empty_look_leaves_a_newer_cooldown_alone(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock, clock: list[float]
) -> None:
    fake_ai.answer = NOTHING
    fake_ai.gate = asyncio.Event()
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=90)])

    await _trigger(hass)
    await wait_until(lambda: len(fake_ai.calls) == 1)
    # The first look is still running when its cooldown ends and a second one starts.
    clock[0] += 120
    await _trigger(hass)
    first_gate, fake_ai.gate = fake_ai.gate, asyncio.Event()
    first_gate.set()
    # The first look finds nothing while the second is being described.
    await wait_until(lambda: len(fake_ai.calls) == 2)
    # Past the short wait an empty look would leave, but within the second look's cooldown.
    clock[0] += 15
    await _trigger(hass)
    fake_ai.gate.set()
    await entry.runtime_data.analyzer.async_join()
    # The second look's cooldown still held.
    assert len(fake_ai.calls) == 2


async def test_an_empty_look_never_lengthens_a_shorter_cooldown(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock, clock: list[float]
) -> None:
    fake_ai.answer = NOTHING
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=5)])

    await _trigger(hass)
    assert await _events(hass, entry) == []
    clock[0] += 6
    fake_ai.answer = COURIER
    await _trigger(hass)
    assert [e["title"] for e in await _events(hass, entry)] == ["Courier walks up"]


async def test_without_a_cooldown_an_empty_look_adds_none(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock, clock: list[float]
) -> None:
    fake_ai.answer = NOTHING
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=0)])

    await _trigger(hass)
    assert await _events(hass, entry) == []
    await _trigger(hass)
    await entry.runtime_data.analyzer.async_join()
    assert len(fake_ai.calls) == 2


async def test_failed_ai_still_stores_the_event(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    fake_ai.answer = HomeAssistantError("Connection refused")
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER])])

    await _trigger(hass)

    (event,) = await _events(hass, entry)
    assert event["analysis"] == "failed"
    assert event["title"] == ""
    assert event["category"] == "person"
    assert event["key_frame"] is not None
    # It was asked twice: an error is often passing.
    assert len(fake_ai.calls) == 2


async def test_an_ai_error_is_asked_again_with_the_same_frames(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    fake_ai.answers = [HomeAssistantError("Unexpected error talking to the server")]
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER])])

    await _trigger(hass)
    (event,) = await _events(hass, entry)

    assert event["analysis"] == "ok"
    assert event["title"] == "Person rings the bell"
    assert len(fake_ai.calls) == 2
    # The same three frames, and the camera wasn't asked for new snapshots.
    assert len(fake_ai.attachment_files[1]) == 3
    assert fake_ai.attachment_files[1] == fake_ai.attachment_files[0]
    assert fake_ai.attachments[1] == fake_ai.attachments[0] == [b"jpeg:snapshot-bytes"] * 3
    assert snapshot.await_count == 3


async def test_a_timeout_of_the_ai_integration_is_asked_again(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    """An HTTP client's time limit is the AI integration's error, not Event Reel's 3 minutes."""
    fake_ai.answers = [TimeoutError("Read timed out")]
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER])])

    await _trigger(hass)
    (event,) = await _events(hass, entry)

    assert event["analysis"] == "ok"
    assert event["title"] == "Person rings the bell"
    assert len(fake_ai.calls) == 2


async def test_a_ring_while_the_ai_is_asked_again_makes_the_event_a_ring(
    hass: HomeAssistant,
    set_up: SetUp,
    fake_ai: FakeAI,
    snapshot: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.event_reel")
    fake_ai.answers = [HomeAssistantError("Connection refused")]
    fake_ai.answer = NOTHING
    entry = await set_up(
        cameras=[camera_subentry(CAMERA, triggers=[TRIGGER, DOORBELL], cooldown=90)]
    )
    with patch("custom_components.event_reel.analyzer.AI_RETRY_DELAY", 0.5):
        await _trigger(hass)
        await wait_until(lambda: "asking again in 0.5 s" in caplog.text)
        await _ring(hass)
        (event,) = await _events(hass, entry)

    # The second ask knew of the ring, and the empty answer is kept as one.
    assert len(fake_ai.calls) == 2
    assert "pressed the doorbell" not in fake_ai.calls[0]["instructions"]
    assert "pressed the doorbell" in fake_ai.calls[1]["instructions"]
    assert event["labels"] == ["person", "chime"]
    assert event["category"] == "doorbell"
    assert event["title"] == ""
    assert event["analysis"] == "ok"


async def test_other_cameras_wait_for_the_retry_and_are_described_after_it(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    garden_trigger = "binary_sensor.garden_person"
    fake_ai.answers = [HomeAssistantError("Connection refused")] * 2
    fake_ai.gate = asyncio.Event()
    entry = await set_up(
        cameras=[
            camera_subentry(CAMERA, triggers=[TRIGGER]),
            camera_subentry("camera.garden", triggers=[garden_trigger]),
        ]
    )

    await _trigger(hass)
    await wait_until(lambda: len(fake_ai.calls) == 1)
    # The garden is queued while the entrance's first try is still running.
    hass.states.async_set(garden_trigger, "on", {"device_class": "occupancy"})
    await hass.async_block_till_done()
    await wait_until(lambda: snapshot.await_count == 6)
    fake_ai.gate.set()
    events = await _events(hass, entry)

    # One job at a time: both tries for the entrance, then the garden.
    cameras = [
        "Entrance" if 'camera "Entrance"' in call["instructions"] else "Garden"
        for call in fake_ai.calls
    ]
    assert cameras == ["Entrance", "Entrance", "Garden"]
    assert {e["camera"]: e["analysis"] for e in events} == {
        CAMERA: "failed",
        "camera.garden": "ok",
    }


async def test_a_rejected_request_is_not_asked_again(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    fake_ai.answer = ServiceValidationError("This model can't read images")
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER])])

    await _trigger(hass)
    (event,) = await _events(hass, entry)

    assert event["analysis"] == "failed"
    assert len(fake_ai.calls) == 1


@pytest.mark.parametrize(("seconds", "calls"), [(60, 2), (61, 1), (90, 1)])
async def test_an_error_that_took_over_a_minute_is_not_asked_again(
    hass: HomeAssistant,
    set_up: SetUp,
    fake_ai: FakeAI,
    snapshot: AsyncMock,
    seconds: int,
    calls: int,
) -> None:
    """A model that is slow to fail would hold up every camera twice."""
    fake_ai.answers = [HomeAssistantError("Read timed out")]
    fake_ai.gate = asyncio.Event()
    now = [1000.0]
    # Only the analyzer's clock: asyncio's own must keep running.
    with patch(
        "custom_components.event_reel.analyzer.time", SimpleNamespace(monotonic=lambda: now[0])
    ):
        entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER])])
        await _trigger(hass)
        await wait_until(lambda: len(fake_ai.calls) == 1)
        now[0] += seconds
        fake_ai.gate.set()
        (event,) = await _events(hass, entry)

    assert event["analysis"] == ("ok" if calls == 2 else "failed")
    assert len(fake_ai.calls) == calls


async def test_stopping_during_the_wait_ends_a_describe_call(
    hass: HomeAssistant,
    set_up: SetUp,
    fake_ai: FakeAI,
    snapshot: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.event_reel")
    fake_ai.answer = HomeAssistantError("Connection refused")
    hass.states.async_set(CAMERA, "idle")
    entry = await set_up()
    with patch("custom_components.event_reel.analyzer.AI_RETRY_DELAY", 3600):
        call = hass.async_create_task(
            hass.services.async_call(
                "event_reel",
                "describe",
                {"camera": CAMERA, "frames": 1},
                blocking=True,
                return_response=True,
            )
        )
        # The first try has failed and the analyzer is waiting to ask again.
        await wait_until(lambda: "asking again in 3600 s" in caplog.text)
        assert await asyncio.wait_for(hass.config_entries.async_unload(entry.entry_id), 5)
        with pytest.raises(HomeAssistantError, match="stopped during the analysis"):
            await asyncio.wait_for(call, 5)
    assert len(fake_ai.calls) == 1


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
    # The fake clip lasts 4 s. The middles of its three equal parts are at 0.7, 2 and 3.3 s:
    # the AI sees the half-second samples there, not the clip's first and last moments.
    assert fake_ai.attachments[0] == [b"frame at 0.5 s", b"frame at 2.0 s", b"frame at 3.0 s"]
    # The middle one is kept.
    assert await _file(hass, event["key_frame"]) == b"frame at 2.0 s"


async def test_a_one_second_clip_still_gives_three_frames(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, fake_ffmpeg: FakeFFmpeg
) -> None:
    """Sampled every half second, a clip of one second would give only two frames."""
    fake_ffmpeg.clip_seconds = 1.0
    device_id = _nest_camera(hass)
    with patch(
        "custom_components.event_reel.analyzer.Analyzer._fetch", AsyncMock(return_value=b"mp4")
    ):
        entry = await set_up(cameras=[camera_subentry("camera.doorbell", frames=3)])
        hass.bus.async_fire("nest_event", _nest_event(device_id, "doorbell_chime", "T1"))
        await hass.async_block_till_done()
        await _events(hass, entry)
    # The middles of three equal parts of one second are at 0.17, 0.5 and 0.83 s.
    assert fake_ai.attachments[0] == [b"frame at 0.1 s", b"frame at 0.5 s", b"frame at 0.8 s"]


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


async def test_a_ring_after_an_empty_look_starts_a_whole_cooldown(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, clock: list[float]
) -> None:
    """The visitor who rang is still at the door: their next person event isn't a new one."""
    fake_ai.answer = NOTHING
    device_id = _nest_camera(hass)
    with patch(
        "custom_components.event_reel.analyzer.Analyzer._fetch", AsyncMock(return_value=b"x")
    ):
        entry = await set_up(cameras=[camera_subentry("camera.doorbell", cooldown=90)])
        hass.bus.async_fire("nest_event", _nest_event(device_id, "camera_person", "T1"))
        await hass.async_block_till_done()
        assert await _events(hass, entry) == []
        # The bell rings within the short wait after the empty look.
        clock[0] += 5
        fake_ai.answer = COURIER
        hass.bus.async_fire("nest_event", _nest_event(device_id, "doorbell_chime", "T2"))
        await hass.async_block_till_done()
        assert len(await _events(hass, entry)) == 1
        # Long after that short wait, but well within a cooldown from the ring.
        clock[0] += 20
        hass.bus.async_fire("nest_event", _nest_event(device_id, "camera_person", "T3"))
        await hass.async_block_till_done()
        (event,) = await _events(hass, entry)
        assert event["nest"]["event_token"] == "T2"
        assert len(fake_ai.calls) == 2
        # The cooldown runs a whole 90 s from the ring: 1 s before its end, then 1 s after.
        clock[0] += 69
        hass.bus.async_fire("nest_event", _nest_event(device_id, "camera_person", "T4"))
        await hass.async_block_till_done()
        assert len(await _events(hass, entry)) == 1
        clock[0] += 2
        hass.bus.async_fire("nest_event", _nest_event(device_id, "camera_person", "T5"))
        await hass.async_block_till_done()
        assert len(await _events(hass, entry)) == 2
    assert len(fake_ai.calls) == 3


async def test_a_ring_while_an_empty_look_cleans_up_gets_its_own_event(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    """The look has found nothing but is still deleting its frames when the bell rings."""
    fake_ai.answer = {"activity": False, "subject": "other", "title": "Empty", "description": None}
    device_id = _nest_camera(hass)
    real_rmtree = shutil.rmtree
    rang: list[bool] = []

    async def ring() -> None:
        hass.bus.async_fire("nest_event", _nest_event(device_id, "doorbell_chime", "T2"))

    def rmtree_then_ring(path: Any, ignore_errors: bool = False) -> None:
        # Runs in the executor while the loop is free: the ring is handled before the delete.
        if not rang:
            rang.append(True)
            asyncio.run_coroutine_threadsafe(ring(), hass.loop).result(timeout=5)
        real_rmtree(path, ignore_errors)

    with patch(
        "custom_components.event_reel.analyzer.Analyzer._fetch", AsyncMock(return_value=b"x")
    ):
        entry = await set_up(cameras=[camera_subentry("camera.doorbell", cooldown=90)])
        # This replaces shutil.rmtree everywhere, so only after set_up has used its own.
        with patch("custom_components.event_reel.analyzer.shutil.rmtree", rmtree_then_ring):
            hass.bus.async_fire("nest_event", _nest_event(device_id, "camera_person", "T1"))
            await hass.async_block_till_done()
            await entry.runtime_data.analyzer.async_join()
            (event,) = await _events(hass, entry)
    assert rang
    assert event["category"] == "doorbell"
    assert event["nest"]["event_token"] == "T2"
    assert len(fake_ai.calls) == 2


async def test_a_ring_while_a_crashed_look_cleans_up_gets_its_own_event(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    """The look failed in an unexpected way and is deleting its frames when the bell rings.

    (An AI that crashes is a failed answer, which makes an event; the clip's download
    crashing is not.)
    """
    fake_ai.answer = COURIER
    device_id = _nest_camera(hass)
    real_rmtree = shutil.rmtree
    rang: list[bool] = []

    async def ring() -> None:
        hass.bus.async_fire("nest_event", _nest_event(device_id, "doorbell_chime", "T2"))

    def rmtree_then_ring(path: Any, ignore_errors: bool = False) -> None:
        if not rang:
            rang.append(True)
            asyncio.run_coroutine_threadsafe(ring(), hass.loop).result(timeout=5)
        real_rmtree(path, ignore_errors)

    with patch(
        "custom_components.event_reel.analyzer.Analyzer._fetch",
        AsyncMock(side_effect=[RuntimeError("the download crashed"), b"x"]),
    ):
        entry = await set_up(cameras=[camera_subentry("camera.doorbell", cooldown=90)])
        with patch("custom_components.event_reel.analyzer.shutil.rmtree", rmtree_then_ring):
            hass.bus.async_fire("nest_event", _nest_event(device_id, "camera_person", "T1"))
            await hass.async_block_till_done()
            await entry.runtime_data.analyzer.async_join()
            (event,) = await _events(hass, entry)
    assert rang
    assert event["category"] == "doorbell"
    assert event["title"] == "Courier walks up"
    assert event["nest"]["event_token"] == "T2"


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
    # A stuck model isn't asked again: it would hold up every camera twice as long.
    assert len(fake_ai.calls) == 1


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
