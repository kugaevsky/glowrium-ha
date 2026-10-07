"""What the lamp says of itself, held to a shape before it is shown.

The lamp's name comes off the air: whatever advertises one beginning with
"Glowrium" is offered for set-up, and can be set up. It ends up where people
read it - in a dialog and in a repair, both of which Home Assistant renders
as Markdown. So it passes through here first, and both show the same thing.
"""

from __future__ import annotations

from typing import Final

# How much of the lamp's name is shown: enough to recognise the lamp by.
_NAME_SHOWN: Final = 48


def as_text(name: str) -> str:
    """Return ``name`` with nothing in it that Markdown or HTML would act on.

    Letters and digits of any script, spaces, dashes and underscores are kept.
    Not a dot: that is all it takes to make an address clickable.
    """
    kept = "".join(char if char.isalnum() or char in " -_" else " " for char in name)
    return " ".join(kept.split())[:_NAME_SHOWN] or "the lamp"
