"""Tests that every language says what strings.json says, and no less.

hassfest checks this in CI, on a push. These check it here, where a missing
key is one line of output instead of a failed workflow.
"""

import json
from pathlib import Path
import re
from typing import Any

import pytest

INTEGRATION = Path(__file__).parent.parent / "custom_components" / "glowrium"
TRANSLATIONS = sorted(INTEGRATION.glob("translations/*.json"))


def _leaves(node: Any, path: str = "") -> dict[str, str]:
    """Return every string in ``node`` by its dotted path."""
    if isinstance(node, str):
        return {path: node}
    found: dict[str, str] = {}
    for key, value in node.items():
        found |= _leaves(value, f"{path}.{key}" if path else key)
    return found


def _placeholders(text: str) -> set[str]:
    return set(re.findall(r"{(\w+)}", text))


STRINGS = _leaves(json.loads((INTEGRATION / "strings.json").read_text()))


def test_the_six_languages_are_all_there() -> None:
    """A language dropped by accident would otherwise fail nothing."""
    assert [path.stem for path in TRANSLATIONS] == [
        "de",
        "en",
        "es",
        "fr",
        "ru",
        "zh-Hans",
    ]


@pytest.mark.parametrize("path", TRANSLATIONS, ids=lambda path: path.stem)
def test_a_language_has_every_string_and_no_string_of_its_own(path: Path) -> None:
    """Each translation carries exactly the keys of strings.json."""
    assert set(_leaves(json.loads(path.read_text()))) == set(STRINGS)


@pytest.mark.parametrize("path", TRANSLATIONS, ids=lambda path: path.stem)
def test_a_translation_keeps_the_placeholders_of_what_it_translates(
    path: Path,
) -> None:
    """A placeholder lost or misspelt in translation shows as a raw brace.

    Or, where Home Assistant fills it in strictly, as an error in place of the
    message.
    """
    for key, text in _leaves(json.loads(path.read_text())).items():
        assert _placeholders(text) == _placeholders(STRINGS[key]), key


def test_english_is_strings_json_word_for_word() -> None:
    """en.json is the copy Home Assistant serves; it must not drift."""
    english = json.loads((INTEGRATION / "translations" / "en.json").read_text())

    assert _leaves(english) == STRINGS
