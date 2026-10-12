"""Common fixtures for the Glowrium tests."""

from collections.abc import Iterator
import importlib.util
import logging
from pathlib import Path
import re
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The daylight-saving slot switched on and switched off, as the vendor app
# writes it (from a capture): the flag, then an hour in seconds.
DST_ON = bytes.fromhex("0100000e10")
DST_OFF = bytes.fromhex("0000000e10")

# What asyncio's debug mode says of a step of the loop that took long, as it
# words it before the step and the time are filled in.
_A_STEP_WAS_SLOW = "Executing %s took %.3f seconds"


def _is_not_about_a_slow_step(record: logging.LogRecord) -> bool:
    """Tell asyncio's note on a slow step from everything else it logs."""
    return record.msg != _A_STEP_WAS_SLOW


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading custom integrations in all tests."""


@pytest.fixture(autouse=True)
def slow_steps_go_unsaid() -> Iterator[None]:
    """Keep asyncio's note on a slow step of the loop out of every test's log.

    The test plugin runs the loop in debug mode, where asyncio writes a
    WARNING for each step that took longer than a tenth of a second. A step
    takes that long when code blocks the loop, and as well when the machine
    was busy with something else: the note does not say which. Tests that
    read every WARNING there is, whoever wrote it, cannot have it - one
    failed for a step of 0.2 s, once in 22 runs, with neither the test nor
    the code it holds changed.

    So the note is dropped, and that has its price: a step this
    integration's own code made slow goes unsaid in the tests as well, in
    the log of a failing test too. Only that note is dropped. Whatever else
    asyncio logs is heard.
    """
    asyncio_says = logging.getLogger("asyncio")
    asyncio_says.addFilter(_is_not_about_a_slow_step)
    yield
    asyncio_says.removeFilter(_is_not_about_a_slow_step)


def anchors(markdown: str) -> set[str]:
    """Return the anchors GitHub gives the headings of a Markdown document."""
    titles = (
        line.lstrip("#").strip().lower()
        for line in markdown.splitlines()
        if line.startswith("#")
    )
    return {re.sub(r"[^\w -]", "", title).replace(" ", "-") for title in titles}


def load_tool(name: str) -> ModuleType:
    """Load tools/<name>.py, which is a script and not part of any package."""
    spec = importlib.util.spec_from_file_location(
        f"glowrium_tool_{name}", ROOT / "tools" / f"{name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # A dataclass looks its own module up by name while it is being defined.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    return module
