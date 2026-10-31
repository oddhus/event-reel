"""Shared fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.event_reel.const import DOMAIN, SUBENTRY_CAMERA


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture(autouse=True)
async def core(hass: HomeAssistant) -> None:
    """conversation, which ai_task needs, expects the core integration."""
    assert await async_setup_component(hass, "homeassistant", {})


@pytest.fixture
def media_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    path = tmp_path / "media"
    path.mkdir()
    hass.config.media_dirs = {"local": str(path)}
    return path


class FakeAI:
    """Stands in for ai_task.generate_data and records what it was asked."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answer: Any = {
            "activity": True,
            "subject": "person",
            "title": "Person rings the bell",
            "description": "One person in a red jacket waits at the door.",
        }
        self.attachment_files: list[list[Path]] = []
        # What each call's frames contained, read before they're cleaned up.
        self.attachments: list[list[bytes]] = []
        # When set, every answer waits for it, like a busy local model.
        self.gate: asyncio.Event | None = None

    async def handle(self, call: ServiceCall) -> dict[str, Any]:
        from homeassistant.components import media_source

        self.calls.append(dict(call.data))
        if self.gate is not None:
            await self.gate.wait()
        # The attachments must resolve to local files, as the real AI Task requires.
        files = []
        for attachment in call.data.get("attachments", []):
            resolved = await media_source.async_resolve_media(
                call.hass, attachment["media_content_id"], None
            )
            assert resolved.path is not None and resolved.path.is_file()
            files.append(resolved.path)
        self.attachment_files.append(files)
        self.attachments.append([path.read_bytes() for path in files])
        if isinstance(self.answer, Exception):
            raise self.answer
        return {"conversation_id": "test", "data": self.answer}


@pytest.fixture
def fake_ai(hass: HomeAssistant) -> FakeAI:
    ai = FakeAI()
    return ai


def register_fake_ai(hass: HomeAssistant, ai: FakeAI) -> None:
    """Replace the real service after ai_task has set up."""
    hass.services.async_register(
        "ai_task", "generate_data", ai.handle, supports_response=SupportsResponse.ONLY
    )


class FakeFFmpeg:
    """Stands in for ffmpeg: a video is a clip `clip_seconds` long.

    Sampling it at `fps=N` writes a frame for every 1/N s, each saying which moment it shows.
    """

    def __init__(self) -> None:
        self.clip_seconds = 4.0

    async def run(self, binary: str, args: list[str], stdin: bytes | None = None) -> bytes:
        if stdin is not None:
            return b"jpeg:" + stdin[:16]
        fps = float(args[args.index("-vf") + 1].split(",")[0].removeprefix("fps="))
        limit = int(args[args.index("-frames:v") + 1])
        for i in range(min(int(self.clip_seconds * fps), limit)):
            Path(args[-1] % (i + 1)).write_bytes(f"frame at {i / fps:.1f} s".encode())
        return b""


@pytest.fixture
def fake_ffmpeg() -> FakeFFmpeg:
    return FakeFFmpeg()


@pytest.fixture
def no_ffmpeg(fake_ffmpeg: FakeFFmpeg) -> Any:
    with (
        patch("custom_components.event_reel.frames._run_ffmpeg", fake_ffmpeg.run),
        patch("custom_components.event_reel.frames.SNAPSHOT_INTERVAL", 0),
        patch("custom_components.event_reel.analyzer.get_ffmpeg_manager") as manager,
    ):
        manager.return_value.binary = "ffmpeg"
        yield


def camera_subentry(camera: str, **data: Any) -> ConfigSubentryData:
    return ConfigSubentryData(
        data={"camera": camera, "frames": 3, "cooldown": 90, **data},
        subentry_type=SUBENTRY_CAMERA,
        title=camera,
        unique_id=camera,
    )


SetUp = Callable[..., Any]


async def wait_until(check: Callable[[], bool], timeout: float = 2) -> None:
    """Let background tasks run until `check` holds (they aren't awaited by HA's helpers)."""
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


@pytest.fixture
def set_up(hass: HomeAssistant, media_dir: Path, fake_ai: FakeAI, no_ffmpeg: None) -> SetUp:
    """Set up Event Reel with the given options and camera subentries."""

    async def _set_up(
        options: dict[str, Any] | None = None,
        cameras: list[ConfigSubentryData] | None = None,
        *,
        replace_ai: bool = True,
    ) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Event Reel",
            options={"language": "nb", **(options or {})},
            subentries_data=cameras or [],
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        if replace_ai:
            register_fake_ai(hass, fake_ai)
        return entry

    return _set_up
