"""Paths a normal day rarely takes: a full queue, a stop mid-work, odd media, a changed camera."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.components.camera import Image
from homeassistant.components.http import ApiConfig
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.event_reel.const import DOMAIN

from .conftest import FakeAI, SetUp, camera_subentry, wait_until

CAMERA = "camera.garden"
OTHER_CAMERA = "camera.drive"
CLIP = "/api/nest/event_media/device/clip"


@pytest.fixture(autouse=True)
def snapshot() -> Any:
    with patch(
        "custom_components.event_reel.frames.camera.async_get_image",
        AsyncMock(return_value=Image("image/jpeg", b"snapshot-bytes")),
    ) as mock:
        yield mock


async def _describe(hass: HomeAssistant, **data: Any) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN,
        "describe",
        {"camera": CAMERA, "frames": 2, **data},
        blocking=True,
        return_response=True,
    )
    return response["event"]


def _work_files(media_dir: Path) -> list[Path]:
    """Frames left behind in Event Reel's scratch folder."""
    return [path for path in (media_dir / "event_reel" / "_work").rglob("*") if path.is_file()]


async def test_a_full_queue_refuses_one_and_runs_the_rest(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    fake_ai.gate = asyncio.Event()
    entry = await set_up()
    first = hass.async_create_task(_describe(hass))
    await wait_until(lambda: len(fake_ai.calls) == 1)

    # The AI is busy with the first; ten more may wait, the eleventh may not.
    waiting = [hass.async_create_task(_describe(hass)) for _ in range(11)]
    await wait_until(lambda: any(task.done() for task in waiting))
    fake_ai.gate.set()
    async with asyncio.timeout(5):
        results = await asyncio.gather(first, *waiting, return_exceptions=True)

    refused = [r for r in results if isinstance(r, Exception)]
    assert [str(r) for r in refused] == ["Too many waiting analyses"]
    assert results[-1] is refused[0]
    assert len(fake_ai.calls) == 11
    assert len(entry.runtime_data.store.all()) == 11


async def test_stopping_fails_the_running_and_waiting_jobs_and_leaves_no_frames(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, media_dir: Path
) -> None:
    fake_ai.gate = asyncio.Event()
    entry = await set_up()
    analyzer = entry.runtime_data.analyzer
    running = hass.async_create_task(_describe(hass))
    await wait_until(lambda: len(fake_ai.calls) == 1)
    waiting = [
        hass.async_create_task(_describe(hass, camera=camera))
        for camera in (OTHER_CAMERA, "camera.shed")
    ]
    # The waiting jobs take their snapshots right away.
    await wait_until(lambda: len(_work_files(media_dir)) >= 3 * 2)

    assert await hass.config_entries.async_unload(entry.entry_id)
    # No caller is left waiting forever.
    async with asyncio.timeout(2):
        results = await asyncio.gather(running, *waiting, return_exceptions=True)

    assert all(isinstance(result, HomeAssistantError) for result in results)
    assert [str(result) for result in results] == [
        "Event Reel stopped during the analysis",
        "Event Reel stopped before the analysis ran",
        "Event Reel stopped before the analysis ran",
    ]
    await hass.async_block_till_done()
    # No frames and no folders are left, and the queue counts every job as done.
    assert list((media_dir / "event_reel" / "_work").iterdir()) == []
    async with asyncio.timeout(1):
        await analyzer.async_join()
    fake_ai.gate.set()


@pytest.mark.parametrize(
    ("api", "base"),
    [
        # No http integration set up.
        (None, "http://127.0.0.1:8123"),
        # Listening on every IPv6 address.
        (ApiConfig("192.0.2.10", "::", 8123, False), "http://127.0.0.1:8123"),
        # `server_host` set to one address.
        (ApiConfig("192.0.2.10", "192.0.2.10", 8124, False), "http://192.0.2.10:8124"),
        (ApiConfig("192.0.2.10", "fd00::10", 8123, True), "https://[fd00::10]:8123"),
        (ApiConfig("192.0.2.10", "ha.example", 8443, True), "https://ha.example:8443"),
        # No address known.
        (ApiConfig("192.0.2.10", "", 8123, False), "http://127.0.0.1:8123"),
    ],
)
async def test_home_assistant_media_is_fetched_where_home_assistant_listens(
    hass: HomeAssistant,
    set_up: SetUp,
    fake_ai: FakeAI,
    aioclient_mock: AiohttpClientMocker,
    api: ApiConfig | None,
    base: str,
) -> None:
    await set_up()
    hass.config.api = api
    aioclient_mock.get(f"{base}{CLIP}", content=b"clip-bytes")

    event = await _describe(hass, video=CLIP)

    assert event["analysis"] == "ok"
    (_method, url, _data, _headers) = aioclient_mock.mock_calls[0]
    assert str(url).startswith(f"{base}{CLIP}?authSig=")
    assert len(fake_ai.calls) == 1


# Larger than one 64 KiB read, so the limit must count across reads.
LIMIT = 100_000


@pytest.mark.parametrize(("size", "analysis"), [(LIMIT, "ok"), (LIMIT + 1, "failed")])
async def test_media_over_the_limit_is_not_described(
    hass: HomeAssistant,
    set_up: SetUp,
    fake_ai: FakeAI,
    aioclient_mock: AiohttpClientMocker,
    media_dir: Path,
    size: int,
    analysis: str,
) -> None:
    entry = await set_up()
    hass.config.api = None
    aioclient_mock.get(f"http://127.0.0.1:8123{CLIP}", content=b"x" * size)
    (media_dir / "clip.mp4").write_bytes(b"x" * size)

    with patch("custom_components.event_reel.analyzer.MAX_MEDIA_BYTES", LIMIT):
        fetched = await _describe(hass, video=CLIP)
        local = await _describe(hass, video="media-source://media_source/local/clip.mp4")

    # Kept, but not sent to the AI.
    assert fetched["analysis"] == local["analysis"] == analysis
    stored = entry.runtime_data.store.all()
    assert [event["analysis"] for event in stored] == [analysis, analysis]
    assert len(fake_ai.calls) == (2 if analysis == "ok" else 0)


async def test_reconfiguring_a_camera_changes_what_starts_it(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    hass.states.async_set(CAMERA, "idle", {"friendly_name": "Garden"})
    entry = await set_up(
        cameras=[
            camera_subentry(CAMERA, triggers=["binary_sensor.garden_old"]),
            camera_subentry(OTHER_CAMERA),
        ]
    )
    (subentry_id,) = [
        subentry_id
        for subentry_id, subentry in entry.subentries.items()
        if subentry.data["camera"] == CAMERA
    ]

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["type"] is FlowResultType.FORM
    changed = {
        "camera": CAMERA,
        "name": "Back garden",
        "triggers": ["binary_sensor.garden_new"],
        "nest_events": [],
        "frames": 2.0,
        "cooldown": 30.0,
    }
    # Another camera's entity is taken.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**changed, "camera": OTHER_CAMERA}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"camera": "already_configured"}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], changed)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    subentry = entry.subentries[subentry_id]
    assert subentry.title == "Back garden"
    assert subentry.data == {
        "camera": CAMERA,
        "name": "Back garden",
        "triggers": ["binary_sensor.garden_new"],
        "nest_events": [],
        "frames": 2,
        "cooldown": 30,
    }
    # Whole numbers, not the floats the form gave.
    assert type(subentry.data["frames"]) is int
    assert type(subentry.data["cooldown"]) is int

    async def fire(trigger: str) -> None:
        hass.states.async_set(trigger, "off")
        await hass.async_block_till_done()
        hass.states.async_set(trigger, "on")
        await hass.async_block_till_done()
        await entry.runtime_data.analyzer.async_join()

    # The reload follows the new trigger, not the old one.
    await fire("binary_sensor.garden_old")
    assert fake_ai.calls == []
    await fire("binary_sensor.garden_new")
    assert len(fake_ai.calls) == 1
    (event,) = entry.runtime_data.store.all()
    assert event["trigger"] == "binary_sensor.garden_new"
    assert 'from the camera "Back garden"' in fake_ai.calls[0]["instructions"]
    assert len(fake_ai.attachments[0]) == 2


async def test_the_same_camera_can_be_kept_when_reconfiguring(
    hass: HomeAssistant, set_up: SetUp
) -> None:
    entry = await set_up(cameras=[camera_subentry(CAMERA)])
    (subentry_id,) = entry.subentries

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"camera": CAMERA, "frames": 4, "cooldown": 90}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[subentry_id].data["frames"] == 4
