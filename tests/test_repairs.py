"""A repair issue says so when the AI keeps failing, and goes away when it answers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.components.camera import Image
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir, selector
import pytest

from custom_components.event_reel.const import DOMAIN
from custom_components.event_reel.diagnostics import async_get_config_entry_diagnostics

from .conftest import FakeAI, SetUp, camera_subentry

CAMERA = "camera.front_door"
TRIGGER = "binary_sensor.front_door_person"
AI_ENTITY = "ai_task.local_model"
ANSWER = {"activity": True, "subject": "person", "title": "T", "description": "D."}


@pytest.fixture(autouse=True)
def snapshot() -> Any:
    with patch(
        "custom_components.event_reel.frames.camera.async_get_image",
        AsyncMock(return_value=Image("image/jpeg", b"snapshot-bytes")),
    ) as mock:
        yield mock


async def _look(hass: HomeAssistant, entry: Any) -> None:
    """One analysis: the trigger turns on (no cooldown) and the AI is asked."""
    hass.states.async_set(TRIGGER, "off")
    await hass.async_block_till_done()
    hass.states.async_set(TRIGGER, "on")
    await hass.async_block_till_done()
    await entry.runtime_data.analyzer.async_join()


def _issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, "ai_failing")


async def test_three_failures_in_a_row_raise_an_issue_and_an_answer_clears_it(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    entry = await set_up(
        options={"ai_task_entity": AI_ENTITY},
        cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=0)],
    )
    fake_ai.answer = HomeAssistantError("Model llava is not loaded")

    await _look(hass, entry)
    await _look(hass, entry)
    assert _issue(hass) is None

    await _look(hass, entry)
    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == "ai_failing"
    assert issue.severity == ir.IssueSeverity.ERROR
    assert not issue.is_fixable
    assert issue.translation_placeholders == {
        "entity": AI_ENTITY,
        "count": "3",
        "error": "Model llava is not loaded",
    }

    # A fourth failure keeps it, with the newest count and error.
    fake_ai.answer = HomeAssistantError("Connection refused")
    await _look(hass, entry)
    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_placeholders["count"] == "4"
    assert issue.translation_placeholders["error"] == "Connection refused"

    fake_ai.answer = ANSWER
    await _look(hass, entry)
    assert _issue(hass) is None


async def test_a_job_counts_once_and_is_not_asked_again_once_the_issue_is_up(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=0)])
    fake_ai.answer = HomeAssistantError("busy")

    async def failures() -> int:
        diag = await async_get_config_entry_diagnostics(hass, entry)
        return int(diag["analyzer"]["ai_failures_in_a_row"])

    for _ in range(3):
        await _look(hass, entry)
    # Each look asked twice and counted once.
    assert len(fake_ai.calls) == 6
    assert await failures() == 3
    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_placeholders["count"] == "3"

    # With the issue up, a failure isn't asked again.
    await _look(hass, entry)
    assert len(fake_ai.calls) == 7
    assert await failures() == 4

    # An answer clears it, and the next failure is asked again.
    fake_ai.answer = ANSWER
    await _look(hass, entry)
    assert await failures() == 0
    fake_ai.answer = HomeAssistantError("busy")
    await _look(hass, entry)
    assert len(fake_ai.calls) == 10
    assert await failures() == 1


async def test_failures_must_be_in_a_row(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=0)])
    for answer in (HomeAssistantError("busy"), HomeAssistantError("busy"), ANSWER):
        fake_ai.answer = answer
        await _look(hass, entry)
    fake_ai.answer = HomeAssistantError("busy")
    await _look(hass, entry)
    await _look(hass, entry)

    assert _issue(hass) is None


async def test_the_preferred_ai_task_gets_its_own_text(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=0)])
    fake_ai.answer = HomeAssistantError("No preferred AI Task")
    for _ in range(3):
        await _look(hass, entry)

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == "ai_failing_preferred"
    assert issue.translation_placeholders == {"count": "3", "error": "No preferred AI Task"}


async def test_a_timeout_counts_as_a_failure(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=0)])
    # The AI never answers.
    fake_ai.gate = asyncio.Event()
    with patch("custom_components.event_reel.analyzer.AI_TIMEOUT", 0.01):
        for _ in range(3):
            await _look(hass, entry)

    issue = _issue(hass)
    assert issue is not None
    # Its own text, translated, with the time limit.
    assert issue.translation_key == "ai_timeout_preferred"
    assert issue.translation_placeholders == {"count": "3", "seconds": "0.01"}


async def test_a_timeout_names_the_chosen_ai_task(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    entry = await set_up(
        options={"ai_task_entity": AI_ENTITY},
        cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=0)],
    )
    fake_ai.gate = asyncio.Event()
    with patch("custom_components.event_reel.analyzer.AI_TIMEOUT", 0.01):
        for _ in range(3):
            await _look(hass, entry)

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == "ai_timeout"
    assert issue.translation_placeholders == {"entity": AI_ENTITY, "count": "3", "seconds": "0.01"}


async def test_an_unexpected_error_counts_and_the_event_is_not_lost(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=0)])
    fake_ai.answer = RuntimeError("socket closed")
    for _ in range(3):
        await _look(hass, entry)

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_placeholders["error"] == "RuntimeError: socket closed"
    events = entry.runtime_data.store.all()
    assert len(events) == 3
    assert {event["analysis"] for event in events} == {"failed"}


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        # One line, and markdown shows as written.
        ("Bad\n\n  *request*: <b>[x](y)</b>", r"Bad \*request\*: \<b\>\[x\](y)\</b\>"),
        # No credentials or query from a URL, and no link made of it.
        (
            "Can't reach http://user:secret@ollama.local:11434/api/chat?key=abc#top now",
            r"Can't reach http\://ollama.local:11434/api/chat now",
        ),
        ("https://api.example/v1?token=abc", r"https\://api.example/v1"),
        ("https://user:pa@ss@api.example/v1", r"https\://api.example/v1"),
        ("https://api.example?email=me@mail.example", r"https\://api.example"),
        # A path or an address without a scheme.
        ("Request failed: /v1/generate?api_key=abc", "Request failed: /v1/generate"),
        ("host.example:8080/v1?token=abc failed", "host.example:8080/v1 failed"),
        # Only the query goes, not what follows it.
        (
            '{"url":"https://x.example/v1?key=abc","error":"model not found"}',
            r'{"url":"https\://x.example/v1","error":"model not found"}',
        ),
        ("(https://x.example/a?b=c).", r"(https\://x.example/a)."),
        ("https://x.example/a?sig=abc)PRIVATE now", r"https\://x.example/a now"),
        (
            '{"url":"https://x.example","error":"model@v2"}',
            r'{"url":"https\://x.example","error":"model@v2"}',
        ),
        # Keys and tokens by name or shape.
        ("Authorization: Bearer abc.def", r"Authorization: \*\*\*"),
        ("Authorization: Basic dXNlcjpwYXNz", r"Authorization: \*\*\*"),
        ("Authorization: ApiKey abcdEFGH1234", r"Authorization: \*\*\*"),
        ('Authorization: Digest username="u", response="abc"', r"Authorization: \*\*\*"),
        (
            '{"Authorization": "Bearer abc def", "error": "bad"}',
            r'{"Authorization": "\*\*\*", "error": "bad"}',
        ),
        ("Proxy-Authorization: Digest a=1, b=2", r"Proxy-Authorization: \*\*\*"),
        ("Cookie: session=abc123", r"Cookie: \*\*\*"),
        ("Wrong key: sk-proj-abcdefghij", r"Wrong key: \*\*\*"),
        ("Not sk-proj-abcdefghij", r"Not sk-\*\*\*"),
        ("Bearer authentication failed", "Bearer authentication failed"),
        ("Bearer abc.def rejected", r"Bearer \*\*\* rejected"),
        ("I'm the bearer of bad news", "I'm the bearer of bad news"),
        ('{"api_key":"abc def"}', r'{"api\_key":"\*\*\*"}'),
        ("secret: 'my secret words' leaked", r"secret: '\*\*\*' leaked"),
        # A quote escaped inside the value, or none to close it.
        ('{"password":"abc\\"def"}', r'{"password":"\*\*\*"}'),
        ("api_key='abc123def456", r"api\_key='\*\*\*'"),
        ('token="' + "a" * 1100 + '"', r'token="\*\*\*"'),
        ("pwd=a pass=b passphrase: c", r"pwd=\*\*\* pass=\*\*\* passphrase: \*\*\*"),
        ("Bad eyJhbGciOi.eyJzdWIi.c2lnbmF0dXJl", r"Bad eyJ\*\*\*"),
        ("Key AIzaSyA1234567890abcdefgh denied", r"Key AIza\*\*\* denied"),
        ("ghp_abcdefghijklmnop1234 and hf_abcdefghijklmnopqrst", r"ghp\_\*\*\* and hf\_\*\*\*"),
        ("Invalid api_key=abc123, try again", r"Invalid api\_key=\*\*\*, try again"),
        ('{"password": "hunter2"}', r'{"password": "\*\*\*"}'),
        (
            "Incorrect API key provided: sk-proj-abcdefghij",
            r"Incorrect API key provided: \*\*\*",
        ),
        # A question is not a query.
        ("Is the model loaded? It says no", "Is the model loaded? It says no"),
        ("Is it loaded/pulled? Try again", "Is it loaded/pulled? Try again"),
        # A bare address isn't made a link either.
        ("See www.example.com", r"See www\.example.com"),
        ("See WWW.EXAMPLE.COM", r"See WWW\.EXAMPLE.COM"),
        # Control characters go.
        ("bad\x1b[31m red\x00", r"bad\[31m red"),
        # Long errors are cut, then escaped.
        ("x" * 500, "x" * 199 + "…"),
        ("_" * 500, r"\_" * 199 + "…"),
        # A huge error without spaces, such as one that echoes the pictures, is quick.
        ("a://" * 250_000, "a\\://" * 49 + "a:/…"),
        ("sk-" * 300_000, r"sk-\*\*\*"),
        ("a-" * 500_000, "a-" * 99 + "a…"),
        ("https://host/" + "a/" * 100_000, "https\\://host/" + "a/" * 93 + "…"),
        # Braces and apostrophes are text.
        ("Can't parse {'a': 1}", "Can't parse {'a': 1}"),
    ],
)
async def test_the_error_is_shown_safely(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, error: str, shown: str
) -> None:
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=0)])
    fake_ai.answer = HomeAssistantError(error)
    for _ in range(3):
        await _look(hass, entry)

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_placeholders["error"] == shown


async def test_events_without_frames_neither_count_nor_reset(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI, snapshot: AsyncMock
) -> None:
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=0)])
    fake_ai.answer = HomeAssistantError("busy")
    await _look(hass, entry)
    await _look(hass, entry)

    # The camera is offline: the AI isn't asked.
    snapshot.side_effect = HomeAssistantError("Camera offline")
    calls = len(fake_ai.calls)
    await _look(hass, entry)
    assert len(fake_ai.calls) == calls
    assert _issue(hass) is None

    snapshot.side_effect = None
    await _look(hass, entry)
    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_placeholders["count"] == "3"


async def test_reloading_removes_the_issue_and_starts_counting_again(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=0)])
    fake_ai.answer = HomeAssistantError("busy")
    for _ in range(3):
        await _look(hass, entry)
    assert _issue(hass) is not None

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _issue(hass) is None

    await _look(hass, entry)
    await _look(hass, entry)
    assert _issue(hass) is None
    await _look(hass, entry)
    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_placeholders["count"] == "3"


async def test_a_timeout_of_the_ai_integration_shows_its_error(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=0)])
    # Its own HTTP time limit, long before Event Reel's.
    fake_ai.answer = TimeoutError("Read timed out")
    for _ in range(3):
        await _look(hass, entry)

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == "ai_failing_preferred"
    assert issue.translation_placeholders == {"count": "3", "error": "Read timed out"}


async def test_an_error_without_text_shows_its_kind(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=0)])
    fake_ai.answer = HomeAssistantError()
    for _ in range(3):
        await _look(hass, entry)

    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_placeholders["error"] == "HomeAssistantError"


async def test_an_answer_that_saw_nothing_clears_the_issue(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=0)])
    fake_ai.answer = HomeAssistantError("busy")
    for _ in range(3):
        await _look(hass, entry)
    assert _issue(hass) is not None

    # No event is kept, but the AI answered.
    fake_ai.answer = {"activity": False, "subject": "other", "title": "", "description": ""}
    await _look(hass, entry)
    assert _issue(hass) is None
    assert len(entry.runtime_data.store.all()) == 3


async def test_the_issue_follows_the_newest_kind_of_failure(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=0)])
    fake_ai.answer = HomeAssistantError("busy")
    for _ in range(3):
        await _look(hass, entry)

    fake_ai.gate = asyncio.Event()
    with patch("custom_components.event_reel.analyzer.AI_TIMEOUT", 0.01):
        await _look(hass, entry)
    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == "ai_timeout_preferred"
    assert issue.translation_placeholders == {"count": "4", "seconds": "0.01"}

    fake_ai.gate = None
    await _look(hass, entry)
    issue = _issue(hass)
    assert issue is not None
    assert issue.translation_key == "ai_failing_preferred"
    assert issue.translation_placeholders == {"count": "5", "error": "busy"}


async def test_removing_event_reel_removes_the_issue(
    hass: HomeAssistant, set_up: SetUp, fake_ai: FakeAI
) -> None:
    entry = await set_up(cameras=[camera_subentry(CAMERA, triggers=[TRIGGER], cooldown=0)])
    fake_ai.answer = HomeAssistantError("busy")
    for _ in range(3):
        await _look(hass, entry)
    assert _issue(hass) is not None

    await hass.config_entries.async_remove(entry.entry_id)
    assert _issue(hass) is None


# What each text says, written out by hand: the code must pass exactly these.
PLACEHOLDERS = {
    "ai_failing": {"entity", "count", "error"},
    "ai_failing_preferred": {"count", "error"},
    "ai_timeout": {"entity", "count", "seconds"},
    "ai_timeout_preferred": {"count", "seconds"},
}


@pytest.mark.parametrize("language", ["strings", "translations/en", "translations/nb"])
def test_every_issue_text_has_its_placeholders(language: str) -> None:
    path = Path(__file__).parents[1] / "custom_components" / DOMAIN / f"{language}.json"
    issues = json.loads(path.read_text(encoding="utf-8"))["issues"]
    assert set(issues) == set(PLACEHOLDERS)
    for key, issue in issues.items():
        assert set(re.findall(r"\{(\w+)\}", issue["description"])) == PLACEHOLDERS[key], key
        assert issue["title"]


def test_the_english_translation_is_a_copy_of_the_strings() -> None:
    folder = Path(__file__).parents[1] / "custom_components" / DOMAIN
    strings = json.loads((folder / "strings.json").read_text(encoding="utf-8"))
    english = json.loads((folder / "translations" / "en.json").read_text(encoding="utf-8"))
    assert english == strings


@pytest.mark.parametrize("flow", ["config", "options"])
async def test_the_ai_task_choice_offers_only_tasks_that_take_pictures(
    hass: HomeAssistant, set_up: SetUp, flow: str
) -> None:
    if flow == "config":
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    else:
        entry = await set_up()
        result = await hass.config_entries.options.async_init(entry.entry_id)
    field = next(key for key in result["data_schema"].schema if key == "ai_task_entity")
    entity_selector = result["data_schema"].schema[field]
    assert isinstance(entity_selector, selector.EntitySelector)
    # SUPPORT_ATTACHMENTS (2). The frontend lists an entity with any bit of a mask, so
    # requiring GENERATE_DATA (1) as well isn't possible here.
    assert entity_selector.config["filter"] == [{"domain": ["ai_task"], "supported_features": [2]}]
