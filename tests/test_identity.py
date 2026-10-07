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


@pytest.mark.parametrize(
    ("claimed", "is_one"),
    [
        ("Glowrium-C051", True),
        ("Glowrium-C064", True),
        ("Glowrium-C051;devid:CST-0001", False),  # with the serial glued on
        ("Glowrium-C0511", False),
        ("x Glowrium-C051", False),
        ("Glowrium-c051", False),
        ("Glowrium-G7", False),  # the marketing name is not the id
        ("", False),
        (None, False),
        (51, False),
    ],
)
def test_a_model_id_is_the_family_a_dash_a_letter_and_three_digits(
    claimed: object, is_one: bool
) -> None:
    """From end to end: a field with something glued to it is not a model id."""
    assert identity.as_model_id(claimed) == (claimed if is_one else None)


@pytest.mark.parametrize(
    ("claimed", "is_one"),
    [
        ("4", True),
        ("1.10", True),
        ("1.10.2", True),
        ("4,mac:A1B2C3", False),
        ("1.2.3.4", False),
        ("123", False),  # a number that long is no part of a version
        ("v4", False),
        ("", False),
        (None, False),
        (4, False),
    ],
)
def test_a_firmware_is_one_to_three_small_numbers(
    claimed: object, is_one: bool
) -> None:
    """From end to end, with dots between them and nothing else."""
    assert identity.as_firmware(claimed) == (claimed if is_one else None)


@pytest.mark.parametrize(
    ("model", "firmware", "said"),
    [
        ("Glowrium-C051", "4", "model Glowrium-C051, firmware 4"),
        (None, None, "model unknown, firmware unknown"),  # not read yet
        ("", "", "model unknown, firmware unknown"),
        ("Glowrium-C051", None, "model Glowrium-C051, firmware unknown"),
        (
            "Glowrium-C051;devid:CST-0001",
            "4,mac:A1B2C3",
            "model not as expected, firmware not as expected",
        ),
    ],
)
def test_a_line_meant_for_posting_says_three_things_of_a_model_or_a_firmware(
    model: str | None, firmware: str | None, said: str
) -> None:
    """What it is, that it is not known yet, or that it is not what it claims."""
    assert identity.described(model, firmware) == said
