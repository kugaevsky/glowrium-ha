"""Tests for quality_scale.yaml, where the integration says how it stands.

Home Assistant keeps such a file beside the manifest of each of its own
integrations, and hassfest holds it to a schema there. Of a custom
integration hassfest reads no such file, so nothing would notice a record
that had stopped saying anything: a rule gone from it, a status misspelt, an
exemption without its reason. These tests are what notices. Whether what the
record claims is true is not something a test can know.
"""

from typing import Any

import yaml

from .conftest import ROOT

RECORD = ROOT / "custom_components" / "glowrium" / "quality_scale.yaml"

# The rules of the Integration Quality Scale as Home Assistant published them
# on 2026-10-07, by tier and in the order its own integrations list them.
# Written out, not fetched: a rule the scale gains or drops is then a change
# somebody makes by hand, to this list and to the record together.
RULES: dict[str, tuple[str, ...]] = {
    "bronze": (
        "action-setup",
        "appropriate-polling",
        "brands",
        "common-modules",
        "config-flow-test-coverage",
        "config-flow",
        "dependency-transparency",
        "docs-actions",
        "docs-conditions",
        "docs-high-level-description",
        "docs-installation-instructions",
        "docs-removal-instructions",
        "docs-triggers",
        "entity-event-setup",
        "entity-unique-id",
        "has-entity-name",
        "runtime-data",
        "test-before-configure",
        "test-before-setup",
        "unique-config-entry",
    ),
    "silver": (
        "action-exceptions",
        "config-entry-unloading",
        "docs-configuration-parameters",
        "docs-installation-parameters",
        "entity-unavailable",
        "integration-owner",
        "log-when-unavailable",
        "parallel-updates",
        "reauthentication-flow",
        "test-coverage",
    ),
    "gold": (
        "devices",
        "diagnostics",
        "discovery-update-info",
        "discovery",
        "docs-data-update",
        "docs-examples",
        "docs-known-limitations",
        "docs-supported-devices",
        "docs-supported-functions",
        "docs-troubleshooting",
        "docs-use-cases",
        "dynamic-devices",
        "entity-category",
        "entity-device-class",
        "entity-disabled-by-default",
        "entity-translations",
        "exception-translations",
        "icon-translations",
        "reconfiguration-flow",
        "repair-issues",
        "stale-devices",
    ),
    "platinum": (
        "async-dependency",
        "inject-websession",
        "strict-typing",
    ),
}
_ALL = [rule for tier in RULES.values() for rule in tier]

STATUSES = ("done", "todo", "exempt")


def _entries() -> dict[str, Any]:
    """Return what the record says of each rule, as it is written."""
    record = yaml.safe_load(RECORD.read_text())

    assert isinstance(record, dict)
    assert list(record) == ["rules"]
    assert isinstance(record["rules"], dict)
    return record["rules"]


def _read(entry: Any) -> tuple[Any, Any]:
    """Return the status and the comment of an entry, in either of its forms."""
    if isinstance(entry, dict):
        return entry.get("status"), entry.get("comment")
    return entry, None


def _written_twice(node: yaml.Node) -> list[str]:
    """Return every key that a mapping of the document holds more than once.

    Read from the document as it is written: loaded, a mapping keeps the
    last of them and says nothing, so a rule entered twice would stand as
    whichever entry came second.
    """
    if not isinstance(node, yaml.MappingNode):
        return []
    keys = [key.value for key, _ in node.value]
    found = sorted({key for key in keys if keys.count(key) > 1})
    for _, value in node.value:
        found += _written_twice(value)
    return found


def test_the_list_held_here_is_the_scale_as_it_was_read() -> None:
    """Fifty-four rules in four tiers, and none of them twice."""
    assert {tier: len(rules) for tier, rules in RULES.items()} == {
        "bronze": 20,
        "silver": 10,
        "gold": 21,
        "platinum": 3,
    }
    assert len(set(_ALL)) == len(_ALL) == 54


def test_the_record_names_every_rule_of_the_scale_once_and_no_other() -> None:
    """One entry for each rule: none left out, none twice, none not a rule."""
    named = set(_entries())

    assert set(_ALL) - named == set(), "rules the record leaves out"
    assert named - set(_ALL) == set(), "entries that are no rule of the scale"
    written = yaml.compose(RECORD.read_text(), Loader=yaml.SafeLoader)
    assert _written_twice(written) == [], "keys the record has twice"


def test_every_entry_gives_one_of_the_three_statuses() -> None:
    """In the form hassfest holds Home Assistant's own files to.

    `done` or `todo` alone, or a mapping of `status` and `comment` and nothing
    else. `exempt` alone is not a form: it would be an exemption that cannot
    have a reason.
    """
    for rule, entry in _entries().items():
        status, _ = _read(entry)

        assert status in STATUSES, rule
        if isinstance(entry, dict):
            assert set(entry) <= {"status", "comment"}, rule
        else:
            assert entry in ("done", "todo"), rule


def test_every_exemption_gives_its_reason() -> None:
    """An exemption without one says only that somebody decided.

    And a comment, on any entry that has one, is a sentence: an empty one is
    a reason that went missing.
    """
    for rule, entry in _entries().items():
        status, comment = _read(entry)

        if status == "exempt" or (isinstance(entry, dict) and "comment" in entry):
            assert isinstance(comment, str), rule
            assert comment.strip(), rule
