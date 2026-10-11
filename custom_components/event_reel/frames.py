"""Getting still frames for the AI: from a clip, an image, or camera snapshots."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import tempfile

from homeassistant.components import camera
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import FRAME_MAX_EDGE, SNAPSHOT_INTERVAL

_LOGGER = logging.getLogger(__name__)

FFMPEG_TIMEOUT = 60
# Frames per second taken from a clip before picking the ones to send.
CLIP_SAMPLE_FPS = 2
# Upper bound on sampled frames, so a long video can't fill the disk.
CLIP_SAMPLE_MAX = 120


def pick_evenly(count: int, wanted: int) -> list[int]:
    """Return `wanted` indexes spread evenly over `count` items, first and last included.

    With fewer items than wanted, every index is returned once.
    """
    if count <= 0 or wanted <= 0:
        return []
    if wanted >= count:
        return list(range(count))
    if wanted == 1:
        return [count // 2]
    step = (count - 1) / (wanted - 1)
    return sorted({round(i * step) for i in range(wanted)})


def _scale_filter() -> str:
    # Shrink the longest edge to FRAME_MAX_EDGE, never enlarge, keep even dimensions.
    edge = FRAME_MAX_EDGE
    return f"scale='if(gte(iw,ih),min({edge},iw),-2)':'if(gte(iw,ih),-2,min({edge},ih))'"


async def _run_ffmpeg(binary: str, args: list[str], stdin: bytes | None = None) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), FFMPEG_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise HomeAssistantError("ffmpeg took too long") from None
    if proc.returncode != 0:
        raise HomeAssistantError(f"ffmpeg failed: {err.decode(errors='replace').strip()}")
    return out


async def frames_from_video(binary: str, video: bytes, wanted: int, workdir: Path) -> list[Path]:
    """Sample a video and return `wanted` evenly spread JPEG frames written to `workdir`."""
    source = workdir / "source.mp4"
    await asyncio.get_running_loop().run_in_executor(None, source.write_bytes, video)
    await _run_ffmpeg(
        binary,
        [
            "-i",
            str(source),
            "-vf",
            f"fps={CLIP_SAMPLE_FPS},{_scale_filter()}",
            "-frames:v",
            str(CLIP_SAMPLE_MAX),
            "-q:v",
            "3",
            str(workdir / "sample_%03d.jpg"),
        ],
    )
    samples = sorted(workdir.glob("sample_*.jpg"))
    if not samples:
        raise HomeAssistantError("The video has no frames")
    return [samples[i] for i in pick_evenly(len(samples), wanted)]


async def frame_from_image(binary: str, image: bytes, workdir: Path, name: str) -> Path:
    """Re-encode an image of any type as a scaled JPEG in `workdir`."""
    out = await _run_ffmpeg(
        binary,
        [
            "-i",
            "pipe:0",
            "-vf",
            _scale_filter(),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            "-f",
            "mjpeg",
            "pipe:1",
        ],
        stdin=image,
    )
    path = workdir / name
    await asyncio.get_running_loop().run_in_executor(None, path.write_bytes, out)
    return path


async def frames_from_camera(
    hass: HomeAssistant, binary: str, entity_id: str, wanted: int, workdir: Path
) -> list[Path]:
    """Take `wanted` snapshots from a camera, SNAPSHOT_INTERVAL seconds apart."""
    paths: list[Path] = []
    for i in range(wanted):
        if i:
            await asyncio.sleep(SNAPSHOT_INTERVAL)
        image = await camera.async_get_image(hass, entity_id)
        paths.append(await frame_from_image(binary, image.content, workdir, f"snap_{i:02d}.jpg"))
    return paths


def make_workdir() -> Path:
    """A private temporary folder for one analysis; the caller removes it."""
    return Path(tempfile.mkdtemp(prefix="event_reel_"))
