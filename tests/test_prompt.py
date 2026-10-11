"""The prompt, the answer parser and the category rules."""

from custom_components.event_reel.prompt import (
    Answer,
    build_instructions,
    category_for,
    language_name,
    parse_answer,
)


def test_language_name_from_codes_and_names() -> None:
    assert language_name("nb") == "Norwegian Bokmål"
    assert language_name("nb-NO") == "Norwegian Bokmål"
    assert language_name("en_GB") == "English"
    assert language_name("Norwegian Nynorsk") == "Norwegian Nynorsk"
    assert language_name("xx") == "the language with code 'xx'"


def test_instructions_name_camera_frames_language_and_hints() -> None:
    text = build_instructions(
        camera_name="Front door",
        frame_count=3,
        language="nb",
        labels=["chime"],
        extra=["Packages are often left on the step.", "  "],
    )
    assert 'You see 3 frames, in time order, from the camera "Front door".' in text
    assert "Someone pressed the doorbell. The detector can be wrong." in text
    assert "Write the title and the description in Norwegian Bokmål." in text
    assert text.endswith("Packages are often left on the step.")


def test_instructions_for_one_frame_without_hints() -> None:
    text = build_instructions(
        camera_name="Garden", frame_count=1, language="en", labels=[], extra=[]
    )
    assert 'You see one frame from the camera "Garden".' in text
    assert "detector" not in text.split("\n")[1]


def test_parse_answer_cleans_whitespace_and_types() -> None:
    answer = parse_answer(
        {
            "activity": "false",
            "subject": " Person ",
            "title": "  Person  at   the door ",
            "description": "",
        }
    )
    assert answer == Answer(
        activity=False, subject="person", title="Person at the door", description=None
    )


def test_parse_answer_ignores_unknown_subject_and_missing_activity() -> None:
    answer = parse_answer({"subject": "spaceship", "title": "Something", "description": "Text."})
    assert answer.activity is None
    assert answer.subject is None
    assert answer.description == "Text."


def test_parse_answer_keeps_plain_text() -> None:
    answer = parse_answer("A cat crosses the lawn.")
    assert answer == Answer(
        activity=None, subject=None, title="", description="A cat crosses the lawn."
    )


def test_category_rules() -> None:
    person = Answer(activity=True, subject="person", title="t", description=None)
    other = Answer(activity=True, subject="other", title="t", description=None)
    assert category_for(person, ["chime"]) == "doorbell"
    assert category_for(person, []) == "person"
    assert category_for(other, ["person"]) == "person"
    assert category_for(other, ["motion"]) == "motion"
    assert category_for(None, ["person"]) == "person"
    assert category_for(None, []) == "motion"
