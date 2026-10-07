"""Common fixtures for the Glowrium tests."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading custom integrations in all tests."""


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
