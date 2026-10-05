"""Tests for the per-model profiles and the names their presets go by."""

import json
from pathlib import Path
import re

import pytest

from custom_components.glowrium.models import DEFAULT_MODEL, MODELS, resolve_model

INTEGRATION = Path(__file__).parent.parent / "custom_components" / "glowrium"
NAMED_IN = [
    INTEGRATION / "strings.json",
    *sorted(INTEGRATION.glob("translations/*.json")),
]


def _preset_keys() -> set[str]:
    """Return every preset key any model offers."""
    profiles = [*MODELS.values(), DEFAULT_MODEL, resolve_model(None)]
    return {key for profile in profiles for key in profile.lighting_modes}


def test_preset_keys_are_keys() -> None:
    """A preset is stored by a key: lower case, no spaces, safe to write down."""
    for key in _preset_keys():
        assert re.fullmatch(r"[a-z][a-z0-9_]*", key), key


@pytest.mark.parametrize("path", NAMED_IN, ids=lambda path: path.name)
def test_every_preset_has_a_name_in_every_language(path: Path) -> None:
    """A preset added to a profile needs a name wherever names are kept.

    Without one the select shows the bare key, in that language only - the
    kind of gap nobody notices who does not read it.
    """
    names = json.loads(path.read_text())["entity"]["select"]["lighting_mode"]["state"]

    assert _preset_keys() <= set(names)
    assert all(names[key].strip() for key in _preset_keys())


def test_an_unknown_model_gets_the_reference_presets_under_the_family_name() -> None:
    """A lamp without a profile is controllable, and is not passed off as a G7."""
    unknown = resolve_model("Glowrium-C064")

    assert unknown.name == "Glowrium"
    assert unknown.pkey == "Glowrium-C064"
    assert unknown.lighting_modes == DEFAULT_MODEL.lighting_modes
