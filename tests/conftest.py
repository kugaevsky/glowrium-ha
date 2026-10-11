"""Common fixtures for the Glowrium tests."""

from collections.abc import Iterator
import importlib.util
import logging
from pathlib import Path
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent

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
    WARNING for each step that took longer than a tenth of a second. That is
    a fact about the machine and what else it was doing, not about the code,
    and a test that holds the integration to having warned of nothing reads
    every WARNING there is: one failed for a step of 0.2 s, once in 22 runs.
    Only that note is dropped. Whatever else asyncio logs is heard.
    """
    asyncio_says = logging.getLogger("asyncio")
    asyncio_says.addFilter(_is_not_about_a_slow_step)
    yield
    asyncio_says.removeFilter(_is_not_about_a_slow_step)


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
