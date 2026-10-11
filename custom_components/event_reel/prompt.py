"""What we ask the AI, and how we read its answer. Pure functions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .const import SUBJECTS

LANGUAGE_NAMES = {
    "da": "Danish",
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fi": "Finnish",
    "fr": "French",
    "it": "Italian",
    "nb": "Norwegian Bokmål",
    "nl": "Dutch",
    "nn": "Norwegian Nynorsk",
    "no": "Norwegian Bokmål",
    "pl": "Polish",
    "pt": "Portuguese",
    "sv": "Swedish",
}

# The answer we ask for, in the format of `ai_task.generate_data`'s `structure`.
STRUCTURE: dict[str, Any] = {
    "activity": {
        "description": (
            "True only when a real person, vehicle, animal or delivery is clearly visible."
        ),
        "required": True,
        "selector": {"boolean": {}},
    },
    "subject": {
        "description": "The main subject that is clearly visible.",
        "required": True,
        "selector": {"select": {"options": SUBJECTS}},
    },
    "title": {
        "description": "3 to 7 words naming the subject and what it does.",
        "required": True,
        "selector": {"text": {}},
    },
    "description": {
        "description": "1 to 2 sentences about what is clearly visible.",
        "required": True,
        "selector": {"text": {}},
    },
}

TRIGGER_HINTS = {
    "chime": "Someone pressed the doorbell.",
    "person": "A detector reported a person.",
    "motion": "A detector reported motion.",
    "sound": "A detector reported a sound.",
}


def language_name(language: str) -> str:
    """'nb' or 'nb-NO' → 'Norwegian Bokmål'. A longer value is taken as a name already."""
    code = language.strip()
    base = code.split("-")[0].split("_")[0].lower()
    if base in LANGUAGE_NAMES:
        return LANGUAGE_NAMES[base]
    return code if len(code) > 3 else f"the language with code '{code}'"


def build_instructions(
    *,
    camera_name: str,
    frame_count: int,
    language: str,
    labels: list[str],
    extra: list[str],
) -> str:
    """The instructions for one analysis."""
    frames = "one frame" if frame_count == 1 else f"{frame_count} frames, in time order,"
    hints = " ".join(TRIGGER_HINTS[label] for label in labels if label in TRIGGER_HINTS)
    lines = [
        f'You see {frames} from the camera "{camera_name}".',
        *([hints + " The detector can be wrong."] if hints else []),
        "Describe only what is actually visible.",
        "First decide whether there is real activity: a person, vehicle, animal or delivery "
        "that is clearly visible. Shadows, reflections, plants, furniture, text, image noise "
        "and blurry shapes are not activity.",
        "- activity: true only when real activity is clearly visible.",
        "- subject: the main subject.",
        "- title: 3 to 7 words naming the subject and what it does. Don't name the camera, "
        "and don't use generic titles such as 'Person detected'.",
        "- description: 1 to 2 natural sentences (15 to 45 words): how many, visible clothes "
        "and colours, what they actually do, direction only when it is clear, and items "
        "they clearly carry.",
        "Don't say that something was detected or recorded. Don't mention images, frames "
        "or clips. Don't identify anyone. Ignore the background and fixed objects. Don't guess.",
        f"Write the title and the description in {language_name(language)}.",
        *[text.strip() for text in extra if text and text.strip()],
    ]
    return "\n".join(lines)


@dataclass(slots=True, frozen=True)
class Answer:
    """The AI's answer, cleaned up. `activity` is None when the answer didn't say."""

    activity: bool | None
    subject: str | None
    title: str
    description: str | None


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def parse_answer(data: Any) -> Answer:
    """Read the structured answer, tolerating missing or loosely typed fields."""
    if not isinstance(data, dict):
        text = _text(data)
        return Answer(activity=None, subject=None, title="", description=text or None)
    activity = data.get("activity")
    if isinstance(activity, str):
        activity = {"true": True, "false": False}.get(activity.strip().lower())
    elif not isinstance(activity, bool):
        activity = None
    subject = _text(data.get("subject")).lower() or None
    if subject not in SUBJECTS:
        subject = None
    description = _text(data.get("description"))
    return Answer(
        activity=activity,
        subject=subject,
        title=_text(data.get("title")),
        description=description or None,
    )


def category_for(answer: Answer | None, labels: list[str]) -> str:
    """The event category, in the names the card uses."""
    if "chime" in labels:
        return "doorbell"
    if answer and answer.subject and answer.subject != "other":
        return answer.subject
    if "person" in labels:
        return "person"
    return "motion"
