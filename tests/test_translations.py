"""The translations agree with strings.json."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import pytest

COMPONENT = Path(__file__).parent.parent / "custom_components" / "event_reel"
TRANSLATIONS = COMPONENT / "translations"
# AGENTS.md: every user-facing string is in English and Norwegian Bokmål.
REQUIRED = {"en.json", "nb.json"}
# `{name}`, with the spaces ICU allows inside the braces.
PLACEHOLDER = re.compile(r"\{\s*([A-Za-z_][\w-]*)\s*\}")


def _load(path: Path) -> dict[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in pairs]
        duplicates = {key for key in keys if keys.count(key) > 1}
        assert not duplicates, f"{path.name}: duplicate keys {sorted(duplicates)}"
        return dict(pairs)

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)


def _texts(tree: Any, path: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """Every leaf of a translation tree, by its path of keys. An empty object is a leaf."""
    if isinstance(tree, dict) and tree:
        texts: dict[tuple[str, ...], Any] = {}
        for key, value in tree.items():
            texts |= _texts(value, (*path, key))
        return texts
    return {path: tree}


def _placeholders(text: str) -> set[str]:
    """The placeholders in a string, which must each be a plain `{name}`.

    The frontend formats these strings as ICU messages. Every other brace is an error
    there or shows up as text: `{{id}}`, `{}`, `{id!s}`, `{id:>5}`, and `'{id}'`, which an
    apostrophe quotes (`''` is a plain apostrophe). A tag such as `<b>` is a rich-text
    argument with no value. Plural and select messages would need ICU's parser, so this
    integration doesn't use them.
    """
    if re.search(r"'[{}<]", text.replace("''", "")):
        raise ValueError("an apostrophe before a brace or tag quotes it")
    if re.search(r"</?[A-Za-z]", text):
        raise ValueError("a tag, which the frontend reads as a rich-text argument")
    if "{" in PLACEHOLDER.sub("", text) or "}" in PLACEHOLDER.sub("", text):
        raise ValueError("a brace that isn't part of a plain {name} placeholder")
    return set(PLACEHOLDER.findall(text))


def test_the_required_translations_exist() -> None:
    assert {path.name for path in TRANSLATIONS.glob("*.json")} >= REQUIRED


def test_norwegian_is_not_a_copy_of_english() -> None:
    assert _load(TRANSLATIONS / "nb.json") != _load(TRANSLATIONS / "en.json")


def test_english_is_a_copy_of_strings() -> None:
    assert _load(TRANSLATIONS / "en.json") == _load(COMPONENT / "strings.json")


@pytest.mark.parametrize("name", sorted(path.name for path in TRANSLATIONS.glob("*.json")))
def test_a_translation_has_the_same_strings_and_placeholders(name: str) -> None:
    source = _texts(_load(COMPONENT / "strings.json"))
    texts = _texts(_load(TRANSLATIONS / name))
    problems = [f"missing {'.'.join(key)}" for key in sorted(source.keys() - texts.keys())]
    problems += [f"extra {'.'.join(key)}" for key in sorted(texts.keys() - source.keys())]
    for key in sorted(texts.keys() & source.keys()):
        text, where = texts[key], ".".join(key)
        # An empty section, such as `"error": {}`, is valid where strings.json has one.
        if text == {} and source[key] == {}:
            continue
        if not isinstance(text, str) or not text.strip():
            problems.append(f"{where}: empty or not a string")
            continue
        if text != text.strip():
            problems.append(f"{where}: leading or trailing whitespace")
        # A custom integration's references aren't resolved: users would see them raw.
        if "[%key:" in text:
            problems.append(f"{where}: a reference")
        try:
            fields = _placeholders(text)
            expected = _placeholders(str(source[key]))
        except ValueError as err:
            problems.append(f"{where}: {err}")
            continue
        if fields != expected:
            problems.append(f"{where}: placeholders {sorted(fields)}, not {sorted(expected)}")
    assert not problems, "\n".join(f"{name}: {problem}" for problem in problems)
