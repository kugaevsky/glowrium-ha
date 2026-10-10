"""Tests for the per-model profiles and the names their presets go by."""

import json
from pathlib import Path
import re

import pytest

from custom_components.glowrium.models import (
    DEFAULT_MODEL,
    MODELS,
    GlowriumModel,
    has_profile,
    resolve_model,
)

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


@pytest.mark.parametrize(
    ("pkey", "has_one"),
    [
        pytest.param("Glowrium-C051", True, id="the G7"),
        pytest.param("Glowrium-C064", False, id="a model nobody has captured"),
    ],
)
def test_a_model_id_has_a_profile_of_its_own_or_has_none(
    pkey: str, has_one: bool
) -> None:
    """Whether its presets are a model's own is asked of the registry.

    ``resolve_model`` answers every model id with a profile - a generic one,
    under the family name, where the model has none of its own - so that the
    lamp can be controlled. That the presets are then the G7's, and
    unconfirmed for that model, is asked apart, and is not read off the name.
    """
    assert has_profile(pkey) is has_one


def test_a_profile_is_a_profile_whatever_it_is_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whether a model has a profile is not read off the name it goes by.

    The generic profile is shown under the family's name. A model whose own
    profile was given that name would have presets of its own all the same.
    """
    plain = GlowriumModel(
        pkey="Glowrium-C099", name="Glowrium", lighting_modes={"sun_sync": 1}
    )
    monkeypatch.setitem(MODELS, plain.pkey, plain)

    assert has_profile("Glowrium-C099")
    assert resolve_model("Glowrium-C099") is plain
