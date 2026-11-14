"""Diagnostics show how Event Reel runs, but not what the cameras saw."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.components.camera import Image
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.event_reel.const import DOMAIN, STORAGE_KEY, STORAGE_VERSION

from .conftest import FakeAI, SetUp, camera_subentry, wait_until

CAMERA = "camera.front_door"
TRIGGER = "binary_sensor.front_door_person"
OTHER_CAMERA = "camera.garden"
OTHER_TRIGGER = "binary_sensor.garden_motion"
QUIET_CAMERA = "camera.shed"
AI_ENTITY = "ai_task.local_model"
REDACTED = "**REDACTED**"
# Recent enough that the 30-day purge at setup keeps it.
NOW = dt_util.utcnow().isoformat()


@pytest.fixture(autouse=True)
def snapshot() -> Any:
    with patch(
        "custom_components.event_reel.frames.camera.async_get_image",
        AsyncMock(return_value=Image("image/jpeg", b"snapshot-bytes")),
    ) as mock:
        yield mock


async def _trigger(hass: HomeAssistant, entity_id: str) -> None:
    hass.states.async_set(entity_id, "off")
    await hass.async_block_till_done()
    hass.states.async_set(entity_id, "on")
    await hass.async_block_till_done()


async def test_diagnostics_describe_setup_and_events_without_their_content(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, set_up: SetUp, fake_ai: FakeAI
) -> None:
    hass.states.async_set(AI_ENTITY, "unknown", {"supported_features": 3})
    hass.states.async_set(CAMERA, "idle")
    entry = await set_up(
        options={"ai_task_entity": AI_ENTITY, "instructions": "Our dog is called Rex."},
        cameras=[
            camera_subentry(CAMERA, triggers=[TRIGGER], instructions="Ignore the neighbour Ola.")
        ],
    )
    await _trigger(hass, TRIGGER)
    await entry.runtime_data.analyzer.async_join()
    (event,) = entry.runtime_data.store.all()

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    # What the cameras saw and what the user told the AI stay out.
    dumped = json.dumps(diag)
    for private in (
        "Rex",
        "Ola",
        "Person rings the bell",
        "red jacket",
        "media-source://",
        event["id"],
    ):
        assert private not in dumped
    assert diag["state"] == "loaded"
    assert diag["options"] == {
        "language": "nb",
        "ai_task_entity": AI_ENTITY,
        "instructions": REDACTED,
    }
    assert diag["ai_task"] == {
        "entity": AI_ENTITY,
        "state": "unknown",
        "generate_data": True,
        "attachments": True,
    }
    (camera,) = diag["cameras"]
    assert camera["subentry"]["camera"] == CAMERA
    assert camera["subentry"]["instructions"] == REDACTED
    assert camera["camera_state"] == "idle"
    (source,) = diag["sources"]
    assert source["camera"] == CAMERA
    assert source["nest"] is False
    assert source["triggers"] == {TRIGGER: "on"}
    assert source["listening"] is True
    # The camera cools down for its 90 s after the trigger.
    assert diag["analyzer"]["waiting"] == 0
    assert diag["analyzer"]["queue_size"] == 10
    assert diag["analyzer"]["ai_failures_in_a_row"] == 0
    assert list(diag["analyzer"]["cooldowns"]) == [CAMERA]
    assert 0 < diag["analyzer"]["cooldowns"][CAMERA] <= 90
    assert diag["events"] == {
        "count": 1,
        "unreadable_start": 0,
        "by_camera": {CAMERA: 1},
        "by_source": {"trigger": 1},
        "by_analysis": {"ok": 1},
        "recent": [
            {
                "start": event["start"],
                "camera": CAMERA,
                "source": "trigger",
                "category": "person",
                "labels": ["person"],
                "analysis": "ok",
                "has_title": True,
                "has_description": True,
                "has_key_frame": True,
            }
        ],
    }
    assert diag["media_dir"] == "local"


async def test_diagnostics_show_waiting_jobs_and_the_preferred_ai_task(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, set_up: SetUp, fake_ai: FakeAI
) -> None:
    fake_ai.gate = asyncio.Event()
    entry = await set_up(
        cameras=[
            camera_subentry(CAMERA, triggers=[TRIGGER]),
            camera_subentry(OTHER_CAMERA, triggers=[OTHER_TRIGGER]),
            # Neither a Nest camera nor given a trigger: nothing starts an analysis.
            camera_subentry(QUIET_CAMERA),
        ]
    )
    await _trigger(hass, TRIGGER)
    await wait_until(lambda: len(fake_ai.calls) == 1)
    # The AI is busy with the first camera, so the second one waits.
    await _trigger(hass, OTHER_TRIGGER)

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    fake_ai.gate.set()
    await entry.runtime_data.analyzer.async_join()
    assert diag["ai_task"] == {"entity": "preferred"}
    assert {source["camera"]: source["listening"] for source in diag["sources"]} == {
        CAMERA: True,
        OTHER_CAMERA: True,
        QUIET_CAMERA: False,
    }
    assert diag["analyzer"]["waiting"] == 1
    assert sorted(diag["analyzer"]["cooldowns"]) == [CAMERA, OTHER_CAMERA]
    assert diag["events"]["count"] == 0
    assert diag["events"]["recent"] == []


async def test_free_text_from_add_event_stays_out_and_only_the_newest_ten_are_listed(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, set_up: SetUp
) -> None:
    entry = await set_up()
    for day in range(1, 13):
        await hass.services.async_call(
            DOMAIN,
            "add_event",
            {
                "title": f"Visitor {day}",
                "camera": CAMERA,
                "start": f"2026-10-{day:02d}T12:00:00+00:00",
                "category": "Ola came home",
                "labels": ["person", "neighbour Ola"],
                "image": "media-source://media_source/local/private.jpg",
            },
            blocking=True,
        )

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    dumped = json.dumps(diag)
    for private in ("Ola", "Visitor", "private.jpg"):
        assert private not in dumped
    recent = diag["events"]["recent"]
    # Newest first: October 12 back to October 3.
    assert [event["start"][:10] for event in recent] == [
        f"2026-10-{day:02d}" for day in range(12, 2, -1)
    ]
    assert recent[0]["category"] == "(other)"
    assert recent[0]["labels"] == ["person", "(other)"]
    assert recent[0]["has_key_frame"] is True
    assert diag["events"]["count"] == 12
    assert diag["events"]["by_camera"] == {CAMERA: 12}
    assert diag["events"]["by_source"] == {"service": 12}
    assert diag["events"]["by_analysis"] == {"none": 12}


@pytest.mark.parametrize(
    ("attributes", "generate_data", "attachments"),
    [
        ({"supported_features": 1}, True, False),
        ({"supported_features": 2}, False, True),
        ({}, False, False),
        ({"supported_features": None}, None, None),
        ({"supported_features": "unknown"}, None, None),
        ({"supported_features": float("inf")}, None, None),
    ],
)
async def test_what_the_ai_task_can_do(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    set_up: SetUp,
    attributes: dict[str, Any],
    generate_data: bool | None,
    attachments: bool | None,
) -> None:
    hass.states.async_set(AI_ENTITY, "unknown", attributes)
    entry = await set_up(options={"ai_task_entity": AI_ENTITY})

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diag["ai_task"] == {
        "entity": AI_ENTITY,
        "state": "unknown",
        "generate_data": generate_data,
        "attachments": attachments,
    }


async def test_a_removed_ai_task_is_unknown_not_unable(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, set_up: SetUp
) -> None:
    entry = await set_up(options={"ai_task_entity": AI_ENTITY})

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diag["ai_task"] == {
        "entity": AI_ENTITY,
        "state": None,
        "generate_data": None,
        "attachments": None,
    }


async def test_an_unloaded_entry_still_shows_its_settings(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, set_up: SetUp
) -> None:
    entry = await set_up(
        options={"instructions": "Our dog is called Rex."},
        cameras=[camera_subentry(CAMERA, triggers=[TRIGGER])],
    )
    assert await hass.config_entries.async_unload(entry.entry_id)

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diag["state"] == "not_loaded"
    assert diag["options"] == {"language": "nb", "instructions": REDACTED}
    assert [camera["subentry"]["camera"] for camera in diag["cameras"]] == [CAMERA]
    assert "events" not in diag
    assert "Rex" not in json.dumps(diag)


async def test_unknown_fields_are_redacted(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, set_up: SetUp
) -> None:
    entry = await set_up(
        options={"future_prompt": "Ola lives here."},
        cameras=[
            camera_subentry(CAMERA, name="Front door", future_note="Ola's room"),
            # A subentry of a kind a later version may add.
            ConfigSubentryData(
                data={"note": "Ola's car"}, subentry_type="zone", title="Ola", unique_id=None
            ),
        ],
    )

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diag["options"]["future_prompt"] == REDACTED
    (camera,) = diag["cameras"]
    assert camera["subentry"]["future_note"] == REDACTED
    # Camera names are settings and are shown.
    assert camera["subentry"]["name"] == "Front door"
    assert "Ola" not in json.dumps(diag)


def _stored(hass_storage: dict[str, Any], *events: dict[str, Any]) -> None:
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "key": STORAGE_KEY,
        "data": {"events": list(events)},
    }


async def test_odd_stored_values_are_shown_as_other(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_storage: dict[str, Any],
    set_up: SetUp,
) -> None:
    # A store edited by hand, or written by a later version.
    _stored(
        hass_storage,
        {"id": "a" * 32, "start": NOW, "category": ["person"], "labels": [{"a": 1}, "person"]},
        {"id": "b" * 32, "start": NOW, "camera": CAMERA, "labels": "person"},
    )
    entry = await set_up()

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diag["events"]["by_camera"] == {"(none)": 1, CAMERA: 1}
    assert sorted(
        (event["category"], str(event["labels"])) for event in diag["events"]["recent"]
    ) == [("(other)", "(other)"), ("(other)", "['(other)', 'person']")]


async def test_a_store_that_breaks_setup_still_gives_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_storage: dict[str, Any],
    media_dir: Path,
) -> None:
    _stored(
        hass_storage,
        {"id": "a" * 32, "camera": CAMERA},
        {"id": "b" * 32, "start": "yesterday"},
        {"id": "c" * 32, "start": NOW},
    )
    entry = MockConfigEntry(domain=DOMAIN, title="Event Reel", options={"language": "nb"})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)

    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)

    assert diag["state"] == "setup_error"
    assert diag["options"] == {"language": "nb"}
    # Setup got as far as loading the store before the purge broke on these events.
    assert diag["events"]["count"] == 3
    assert diag["events"]["unreadable_start"] == 2
    assert [event["start"] for event in diag["events"]["recent"]] == [NOW]
