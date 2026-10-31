"""Frame picking, event selection and trigger rules."""

from datetime import UTC, datetime, timedelta, timezone

from custom_components.event_reel.frames import pick_evenly
from custom_components.event_reel.sources import is_trigger
from custom_components.event_reel.store import select_events

TZ = timezone(timedelta(hours=2))


def test_pick_evenly() -> None:
    # The middles of three equal parts of 10 items are at 1.7, 5 and 8.3: items 1, 5 and 8,
    # not the first and the last.
    assert pick_evenly(10, 3) == [1, 5, 8]
    assert pick_evenly(9, 3) == [1, 4, 7]  # middles at 1.5, 4.5 and 7.5
    assert pick_evenly(20, 3) == [3, 10, 16]
    assert pick_evenly(5, 1) == [2]
    # Too few to stay away from the ends.
    assert pick_evenly(4, 3) == [0, 2, 3]
    assert pick_evenly(2, 4) == [0, 1]
    assert pick_evenly(0, 3) == []
    assert pick_evenly(4, 0) == []


def _event(eid: str, start: str, camera: str | None = None, category: str = "person") -> dict:
    return {"id": eid, "start": start, "camera": camera, "category": category}


EVENTS = [
    _event("a", "2026-10-08T08:00:00+02:00", "camera.entrance"),
    _event("b", "2026-10-08T12:30:00+02:00", "camera.garden", "animal"),
    _event("c", "2026-10-08T12:00:00+02:00", "camera.entrance", "doorbell"),
    _event("d", "2026-10-09T07:00:00+02:00", None, "custom"),
]


def test_select_events_newest_first_in_range() -> None:
    found = select_events(
        EVENTS,
        start=datetime(2026, 10, 8, 9, 0, tzinfo=TZ),
        end=datetime(2026, 10, 8, 23, 59, tzinfo=TZ),
    )
    assert [e["id"] for e in found] == ["b", "c"]


def test_select_events_compares_across_time_zones() -> None:
    # 10:00 UTC is 12:00 at +02:00, so "c" is included and "b" (12:30) is not.
    found = select_events(EVENTS, end=datetime(2026, 10, 8, 10, 0, tzinfo=UTC))
    assert [e["id"] for e in found] == ["c", "a"]


def test_select_events_filters_and_limit() -> None:
    assert [e["id"] for e in select_events(EVENTS, cameras=["camera.entrance"])] == ["c", "a"]
    assert [e["id"] for e in select_events(EVENTS, categories=["Doorbell", "animal"])] == ["b", "c"]
    assert [e["id"] for e in select_events(EVENTS, limit=2)] == ["d", "b"]


def test_trigger_rules() -> None:
    assert is_trigger("off", "on", "binary_sensor")
    assert not is_trigger("on", "on", "binary_sensor")
    assert not is_trigger("on", "off", "binary_sensor")
    assert is_trigger(None, "on", "binary_sensor")
    assert not is_trigger("off", "unavailable", "binary_sensor")
    # Event entities: a new timestamp is an event, the first state after start is not.
    assert is_trigger("2026-10-08T12:00:00+00:00", "2026-10-08T12:05:00+00:00", "event")
    assert not is_trigger(None, "2026-10-08T12:05:00+00:00", "event")
    assert not is_trigger("unavailable", "2026-10-08T12:05:00+00:00", "event")
    # "unknown": the entity never fired before, so this is its first event.
    assert is_trigger("unknown", "2026-10-08T12:05:00+00:00", "event")
