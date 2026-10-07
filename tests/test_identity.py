"""Tests for what is shown of the lamp's own words about itself."""

import pytest

from custom_components.glowrium import identity


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("Glowrium-G7_1234", "Glowrium-G7_1234"),
        ("Glowrium   G7", "Glowrium G7"),  # and no room made by what was taken out
        ("  Glowrium G7  ", "Glowrium G7"),
        ("Glowrium www.lamps.example", "Glowrium www lamps example"),
        ("Glowrium [x](y) <b> `z` *w*", "Glowrium x y b z w"),
        ("Glowrium Лампа-7", "Glowrium Лампа-7"),  # letters of any script
        ("G" * 60, "G" * 48),  # enough to recognise the lamp by
        ("![](//.)", "the lamp"),  # nothing left to call it by
        ("", "the lamp"),
    ],
)
def test_a_name_is_shown_as_text_and_nothing_more(name: str, shown: str) -> None:
    """Letters, digits, spaces, dashes and underscores - not even a dot."""
    assert identity.as_text(name) == shown
