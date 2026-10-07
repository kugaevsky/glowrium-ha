"""What the lamp says of itself, held to a shape before it is shown.

The lamp's name comes off the air: whatever advertises one beginning with
"Glowrium" is offered for set-up, and can be set up. It ends up where people
read it - in a dialog and in a repair, both of which Home Assistant renders
as Markdown. So it passes through here first, and both show the same thing.

The model id and the firmware come out of the device-info string, where the
serial number and the address sit beside them. They end up in warnings that
ask to be reported and in the diagnostics file, both of which get posted.
Each is passed on only when it is what it claims to be; what to say of one
that is not is up to whoever shows it.
"""

from __future__ import annotations

import re
from typing import Final

from .const import NAME_PREFIX

# How much of the lamp's name is shown: enough to recognise the lamp by.
_NAME_SHOWN: Final = 48


def as_text(name: str) -> str:
    """Return ``name`` with nothing in it that Markdown or HTML would act on.

    Letters and digits of any script, spaces, dashes and underscores are kept.
    Not a dot: that is all it takes to make an address clickable.
    """
    kept = "".join(char if char.isalnum() or char in " -_" else " " for char in name)
    return " ".join(kept.split())[:_NAME_SHOWN] or "the lamp"


# Where one field of the device-info string ends is only what the parser made
# of it: a lamp that separates its fields differently hands over one long
# field with the others inside it. So each is taken only when it is, from end
# to end, what it claims to be - a model id of this family is its name, a
# dash, a letter and three digits; a version is one to three small numbers
# with dots between them.
_MODEL_ID: Final = re.compile(rf"{NAME_PREFIX}-[A-Z][0-9]{{3}}")
_FIRMWARE: Final = re.compile(r"[0-9]{1,2}(?:\.[0-9]{1,2}){0,2}")


def _wholly(pattern: re.Pattern[str], claimed: object) -> str | None:
    if isinstance(claimed, str) and pattern.fullmatch(claimed):
        return claimed
    return None


def model_id(claimed: object) -> str | None:
    """Return ``claimed`` if it is a model id of this family and nothing else."""
    return _wholly(_MODEL_ID, claimed)


def firmware(claimed: object) -> str | None:
    """Return ``claimed`` if it is a firmware version and nothing else."""
    return _wholly(_FIRMWARE, claimed)
