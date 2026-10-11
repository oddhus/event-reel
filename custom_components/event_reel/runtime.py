"""What a loaded config entry keeps at runtime."""

from __future__ import annotations

from dataclasses import dataclass, field

from .analyzer import Analyzer
from .sources import CameraSource
from .store import EventStore


@dataclass(slots=True)
class EventReelData:
    store: EventStore
    analyzer: Analyzer
    sources: list[CameraSource] = field(default_factory=list)
