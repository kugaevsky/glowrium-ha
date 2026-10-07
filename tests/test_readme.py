"""Tests that the automations the README shows can be run as they are written.

The examples are found by a rule, so that one added later is held to the same.
The section runs from the heading ``## Automation examples`` to the next
heading of that level, and every fenced block in it is one automation, written
as the automation editor shows it and marked ``yaml``. A block outside the
section is not looked at; one inside it that is marked as anything else fails.

An example names the lamp's entities by placeholder ids that end the way the
real ones do - ``select.glowrium_g7_operating_mode`` - and that ending is how
the two selects are told apart here.
"""

from collections.abc import Iterator
import re
from typing import Any

import pytest
import yaml

from custom_components.glowrium.const import OPERATING_MODES
from custom_components.glowrium.models import DEFAULT_MODEL, MODELS

from .conftest import ROOT

README = ROOT / "README.md"
HEADING = "## Automation examples"

# What Home Assistant has for the kinds of entity the lamp has. The integration
# registers no action of its own, and an example must not suggest one.
ACTIONS = {
    "light.turn_on",
    "light.turn_off",
    "light.toggle",
    "select.select_option",
    "switch.turn_on",
    "switch.turn_off",
    "switch.toggle",
    "number.set_value",
    "button.press",
    "time.set_value",
}

# What each select offers, by the ending of its entity id. A lighting mode is
# a key, and the keys are those of every model there is a profile for.
OFFERED = {
    "_operating_mode": set(OPERATING_MODES),
    "_lighting_mode": {
        key
        for model in (*MODELS.values(), DEFAULT_MODEL)
        for key in model.lighting_modes
    },
}

# Where a trigger, a condition or an action says which option it means.
NAMES_AN_OPTION = ("option", "to", "from", "state")


def _section() -> str:
    """Return the examples section of the README, or nothing if it is gone."""
    found = re.search(
        rf"^{re.escape(HEADING)}\n(.*?)(?=^## |\Z)",
        README.read_text(encoding="utf-8"),
        re.MULTILINE | re.DOTALL,
    )
    return found[1] if found else ""


SECTION = _section()
EXAMPLES = re.findall(r"^```yaml\n(.*?)^```$", SECTION, re.MULTILINE | re.DOTALL)


def _first_line(source: str) -> str:
    return source.partition("\n")[0]


def _automation(source: str) -> dict[str, Any]:
    """Read an example as the one automation it is meant to be."""
    automation = yaml.safe_load(source)
    assert isinstance(automation, dict), "an example is one automation: a mapping"
    return automation


def _listed(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _mappings(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every mapping in an automation: each trigger, action and branch."""
    if isinstance(node, dict):
        yield node
        children = list(node.values())
    elif isinstance(node, list):
        children = node
    else:
        return
    for child in children:
        yield from _mappings(child)


def _entities(mapping: dict[str, Any]) -> list[str]:
    """Return the entity ids a trigger, a condition or an action is about."""
    target = mapping.get("target")
    about = target if isinstance(target, dict) else mapping
    return _listed(about.get("entity_id", []))


def _actions(source: str) -> list[dict[str, Any]]:
    """Return every action an example performs, wherever it is nested."""
    return [
        mapping
        for mapping in _mappings(_automation(source).get("actions"))
        if "action" in mapping
    ]


def test_the_readme_shows_a_light_a_mode_and_a_setting() -> None:
    """The section is there, and has the three things one does with the lamp.

    Every other test here runs once for each example. With the section gone,
    or its heading reworded - the heading is how the examples are found -
    they would all pass for having nothing to look at. So would an example
    in a block that is not marked ``yaml``.
    """
    assert len(EXAMPLES) >= 3, f"fewer than three examples under {HEADING!r}"
    fences = re.findall(r"^```", SECTION, re.MULTILINE)
    assert len(fences) == 2 * len(EXAMPLES), "a block there that is not marked yaml"

    platforms = {
        action["action"].partition(".")[0]
        for source in EXAMPLES
        for action in _actions(source)
    }

    assert "light" in platforms
    assert "select" in platforms
    assert platforms & {"switch", "number"}, "no example uses a setting"


@pytest.mark.parametrize("source", EXAMPLES, ids=_first_line)
def test_an_example_is_an_automation(source: str) -> None:
    """It parses, and has what an automation has: a name, a trigger, an action.

    In the words Home Assistant uses now - ``triggers``, ``actions`` - since
    those are the ones the rest of this file reads.
    """
    automation = _automation(source)

    assert isinstance(automation.get("alias"), str)
    assert isinstance(automation.get("triggers"), list)
    assert automation["triggers"]
    assert isinstance(automation.get("actions"), list)
    assert _actions(source)


@pytest.mark.parametrize("source", EXAMPLES, ids=_first_line)
def test_an_example_calls_only_what_home_assistant_has_for_the_lamp(
    source: str,
) -> None:
    """Each action is one of the platform's own, on an entity of that platform.

    ``glowrium.set_mode`` would read well in an example and does not exist.
    """
    for action in _actions(source):
        name = action["action"]

        assert name in ACTIONS, name
        assert _entities(action), f"{name} is aimed at nothing"
        for entity in _entities(action):
            assert entity.startswith(name.partition(".")[0] + "."), (name, entity)


@pytest.mark.parametrize("source", EXAMPLES, ids=_first_line)
def test_an_example_names_only_options_the_selects_offer(source: str) -> None:
    """An operating mode is one of the three; a lighting mode is a preset's key.

    The name the select shows for a preset - ``Sunrise Sync`` - is a
    translation and not an option; until 0.3.0 it was, and that is what an
    example copied from an old automation would carry. Held wherever a select
    is named: in an action, and in a trigger or a condition on its state.
    """
    for mapping in _mappings(_automation(source)):
        data = mapping.get("data")
        said = {**mapping, **(data if isinstance(data, dict) else {})}
        named = [
            option
            for key in NAMES_AN_OPTION
            if said.get(key) is not None
            for option in _listed(said[key])
        ]
        if mapping.get("action") == "select.select_option":
            assert named, "select.select_option without an option"
        for entity in _entities(mapping):
            if not entity.startswith("select."):
                continue
            offered = next(
                (
                    options
                    for ending, options in OFFERED.items()
                    if entity.endswith(ending)
                ),
                None,
            )

            assert offered is not None, f"{entity} is neither of the lamp's selects"
            assert set(named) <= offered, f"{entity} does not offer {named}"
