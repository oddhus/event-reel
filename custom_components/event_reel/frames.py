"""Getting still frames for the AI: from a clip, an image, or camera snapshots."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from homeassistant.components import camera
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import FRAME_MAX_EDGE, SNAPSHOT_INTERVAL

_LOGGER = logging.getLogger(__name__)

FFMPEG_TIMEOUT = 60
# Frames per second taken from a clip before picking the ones to send.
CLIP_SAMPLE_FPS = 2
# For a clip too short to give enough frames at CLIP_SAMPLE_FPS, such as a ring of a second.
SHORT_CLIP_SAMPLE_FPS = 10
# Upper bound on sampled frames, so a long video can't fill the disk.
CLIP_SAMPLE_MAX = 120


def pick_evenly(count: int, wanted: int) -> list[int]:
    """Return `wanted` indexes from the middles of `wanted` equal parts of `count` items.

    A clip's first and last moments are often empty: before someone steps into view and
    after they leave. With fewer items than wanted, every index is returned once.
    """
    if count <= 0 or wanted <= 0:
        return []
    if wanted >= count:
        return list(range(count))
    return [(2 * i + 1) * count // (2 * wanted) for i in range(wanted)]


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
    except (TimeoutError, asyncio.CancelledError) as stop:
        proc.kill()
        await proc.wait()
        if isinstance(stop, asyncio.CancelledError):
            raise
        raise HomeAssistantError("ffmpeg took too long") from None
    if proc.returncode != 0:
        raise HomeAssistantError(f"ffmpeg failed: {err.decode(errors='replace').strip()}")
    return out


async def _sample(binary: str, source: Path, fps: int, workdir: Path) -> list[Path]:
    """Write `fps` frames per second of a clip to `workdir`, and return them in order."""
    prefix = f"sample_{fps}fps"
    await _run_ffmpeg(
        binary,
        [
            # A playlist disguised as a clip must not make ffmpeg open other files or URLs.
            "-protocol_whitelist",
            "file",
            "-i",
            str(source),
            "-vf",
            f"fps={fps},{_scale_filter()}",
            "-frames:v",
            str(CLIP_SAMPLE_MAX),
            "-q:v",
            "3",
            str(workdir / f"{prefix}_%03d.jpg"),
        ],
    )
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: sorted(workdir.glob(f"{prefix}_*.jpg"))
    )


async def frames_from_video(binary: str, video: bytes, wanted: int, workdir: Path) -> list[Path]:
    """Sample a video and return `wanted` evenly spread JPEG frames written to `workdir`."""
    source = workdir / "source.mp4"
    await asyncio.get_running_loop().run_in_executor(None, source.write_bytes, video)
    fps = CLIP_SAMPLE_FPS
    samples = await _sample(binary, source, fps, workdir)
    if len(samples) < wanted:
        fps = SHORT_CLIP_SAMPLE_FPS
        samples = await _sample(binary, source, fps, workdir)
    if not samples:
        raise HomeAssistantError("The video has no frames")
    picked = pick_evenly(len(samples), wanted)
    _LOGGER.debug(
        "Clip of about %.1f s; sending its frames at %s s",
        len(samples) / fps,
        ", ".join(f"{i / fps:.1f}" for i in picked),
    )
    return [samples[i] for i in picked]


async def frame_from_image(binary: str, image: bytes, workdir: Path, name: str) -> Path:
    """Re-encode an image of any type as a scaled JPEG in `workdir`."""
    out = await _run_ffmpeg(
        binary,
        [
            "-protocol_whitelist",
            "pipe",
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
