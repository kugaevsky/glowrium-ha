"""Tests for tools/stack.py, which installs what the tests run on.

It is run by CI before anything else and by whoever sets up a checkout. What
it installs cannot be tried here; how it reads what Home Assistant's own
integrations require, and what it says when the result is not that, can.
"""

import importlib.metadata
import json
import os
from pathlib import Path
import re
from types import ModuleType
from typing import Any

import pytest
import yaml

from .conftest import ROOT, load_tool

_PLUGIN = "pytest-homeassistant-custom-component"

# The shape of three Home Assistant manifests, with versions that are nobody's.
_MANIFESTS: dict[str, dict[str, Any]] = {
    "bluetooth": {
        "domain": "bluetooth",
        "requirements": ["bleak==0.1.1", "Dbus_Fast==0.2.2", "habluetooth==0.3.3"],
    },
    "usb": {
        "domain": "usb",
        "requirements": ["aiousbwatcher==0.4.4", "serialx==0.5.5"],
    },
    "esphome": {
        "domain": "esphome",
        "requirements": ["aioesphomeapi==0.6.6", "bleak-esphome==0.7.7"],
    },
}
_REQUIRED = {
    "bleak": "0.1.1",
    "dbus-fast": "0.2.2",
    "habluetooth": "0.3.3",
    "aiousbwatcher": "0.4.4",
    "serialx": "0.5.5",
    "aioesphomeapi": "0.6.6",
}


@pytest.fixture(scope="module")
def stack() -> ModuleType:
    """Return tools/stack.py as a module."""
    return load_tool("stack")


def _requirements(name: str) -> list[str]:
    """Return the lines of a requirements file that name something."""
    lines = (ROOT / name).read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and line[0] != "#"]


def _pinned_plugin() -> str:
    """Return the release of the test plugin the oldest stack is held to."""
    (pin,) = _requirements("constraints-oldest.txt")
    held = re.fullmatch(rf"{_PLUGIN}==(\d+\.\d+\.\d+)", pin)
    assert held, "the oldest stack is one exact release of the test plugin"
    return held[1]


def test_what_the_tests_need_is_what_home_assistants_integrations_require(
    stack: ModuleType,
) -> None:
    """All of bluetooth and usb, and of esphome the one library serialx imports.

    Read from the manifests, so that a library Home Assistant starts to
    require tomorrow is installed tomorrow, with nothing edited here.
    """
    assert stack.required(_MANIFESTS.__getitem__) == _REQUIRED


def test_a_requirement_that_is_not_one_exact_version_is_named(
    stack: ModuleType,
) -> None:
    """Home Assistant pins what its integrations require; a range is news."""
    loose = _MANIFESTS | {"usb": {"requirements": ["serialx>=0.5"]}}
    with pytest.raises(SystemExit, match=r"usb.*serialx>=0\.5"):
        stack.required(loose.__getitem__)


def test_the_library_serialx_imports_has_to_be_among_what_esphome_requires(
    stack: ModuleType,
) -> None:
    """If it is not, the script is out of date - and says so, guessing nothing."""
    without = _MANIFESTS | {"esphome": {"requirements": ["bleak-esphome==0.7.7"]}}
    with pytest.raises(SystemExit, match=r"esphome.*aioesphomeapi"):
        stack.required(without.__getitem__)


def test_a_library_not_at_the_required_version_is_named(stack: ModuleType) -> None:
    """Which one, what it is, and what it should be: the run fails on this."""
    assert stack.astray(_REQUIRED, _REQUIRED.get) == []

    newer = _REQUIRED | {"bleak": "0.1.2"}
    assert stack.astray(_REQUIRED, newer.get) == [
        "bleak is 0.1.2, and this Home Assistant requires 0.1.1"
    ]

    absent = dict.fromkeys(_REQUIRED) | {"bleak": "0.1.1"}
    said = stack.astray(_REQUIRED, absent.get)
    assert len(said) == len(_REQUIRED) - 1
    assert "serialx is not installed, and this Home Assistant requires 0.5.5" in said


def test_the_oldest_stack_is_one_exact_release_of_the_test_plugin() -> None:
    """The one version written by hand: Home Assistant, by the plugin's number."""
    assert _pinned_plugin()


def test_the_oldest_stack_is_the_minimum_this_project_declares() -> None:
    """What hacs.json promises is what the oldest leg tests - checked on that leg.

    The plugin's number says nothing of the Home Assistant inside it, so the
    pin cannot be held to the promise by reading files. Where the installed
    plugin is the pinned one, the Home Assistant beside it can be. CI says
    which leg it is running, and its oldest leg may not pass this by.
    """
    if importlib.metadata.version(_PLUGIN) != _pinned_plugin():
        assert os.environ.get("GLOWRIUM_STACK") != "oldest"
        pytest.skip("this is not the oldest stack")

    declared = json.loads((ROOT / "hacs.json").read_text())["homeassistant"]
    assert importlib.metadata.version("homeassistant") == declared


def test_no_library_home_assistant_requires_is_named_in_the_requirements(
    stack: ModuleType,
) -> None:
    """A version written here goes stale the day Home Assistant moves.

    That is what made collection fail with an ImportError until a file was
    edited by hand - and what had CI test versions of the Bluetooth libraries
    that no installation of that Home Assistant runs. Held against what the
    Home Assistant these tests run on requires.
    """
    required = set(stack.required(stack.manifest_of))
    assert {"bleak", "habluetooth", "dbus-fast", "serialx"} <= required

    for name in ("requirements-test.txt", "constraints-oldest.txt"):
        named = {
            stack.canonical(re.split(r"[=<>~!\[; ]", line, maxsplit=1)[0])
            for line in _requirements(name)
        }
        assert not named & required, name


def test_this_environment_is_one_the_script_would_have_left(
    stack: ModuleType,
) -> None:
    """The tests run on the libraries this Home Assistant requires, not newer ones.

    An environment built by ``pip install -r requirements-test.txt`` alone, or
    one that something was upgraded in since, is not what a user of this Home
    Assistant has; tests that pass in it say less than they seem to.
    """
    astray = stack.astray(stack.required(stack.manifest_of), stack.installed)
    assert not astray, (
        "\n".join(astray)
        + "\nRun `python tools/stack.py oldest` (or `newest`) in this environment."
    )


def test_each_stack_is_installed_from_the_same_requirements(
    stack: ModuleType,
) -> None:
    """The oldest is the newest with one constraint more - and nothing else."""
    assert stack.STACKS == ("oldest", "newest")
    oldest, newest = stack.FIRST["oldest"], stack.FIRST["newest"]
    assert str(ROOT / "requirements-test.txt") in newest
    assert str(ROOT / "requirements-test.txt") in oldest
    assert oldest[oldest.index("-c") + 1] == str(ROOT / "constraints-oldest.txt")
    assert "-c" not in newest
    # An environment that has an older Home Assistant moves on to the newest.
    assert "--upgrade" in newest
    assert "--upgrade" not in oldest


def test_a_stack_it_does_not_know_is_refused_before_anything_is_installed(
    stack: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slip of the hand must not leave an environment half built."""
    asked: list[tuple[str, ...]] = []
    monkeypatch.setattr(stack, "_pip_install", lambda *what: asked.append(what))

    with pytest.raises(SystemExit):
        stack.main(["latest"])

    assert not asked


def test_the_libraries_are_installed_the_way_home_assistant_installs_them(
    stack: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each at its version, and what comes with it within Home Assistant's limits.

    Left to itself pip takes the newest of whatever a library depends on,
    and a library under a Home Assistant then runs on dependencies no
    installation of that release has. Which Home Assistant came of it is
    written where the run shows it; a library that came out at another
    version fails the run.
    """
    asked: list[tuple[str, ...]] = []
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(stack, "_pip_install", lambda *what: asked.append(what))
    monkeypatch.setattr(stack, "manifest_of", _MANIFESTS.__getitem__)
    monkeypatch.setattr(stack, "_home_assistant", lambda: tmp_path)
    there = _REQUIRED | {"homeassistant": "0.9.9"}
    monkeypatch.setattr(stack, "installed", there.get)

    assert stack.main(["oldest"]) == 0

    first, second = asked
    assert first == stack.FIRST["oldest"]
    assert second == (
        *(f"{name}=={version}" for name, version in _REQUIRED.items()),
        "-c",
        str(tmp_path / "package_constraints.txt"),
    )
    assert "### oldest: Home Assistant 0.9.9" in summary.read_text()
    assert "- `bleak` 0.1.1" in summary.read_text()

    monkeypatch.setattr(stack, "installed", (there | {"bleak": "0.1.2"}).get)
    assert stack.main(["newest"]) == 1
    assert asked[2] == stack.FIRST["newest"]


def _yaml(path: str) -> Any:
    return yaml.safe_load((ROOT / path).read_text())


_ONCE = (
    "github.event_name != 'pull_request' || "
    "github.event.pull_request.head.repo.full_name != github.repository"
)


def test_ci_runs_every_stack_the_script_installs(stack: ModuleType) -> None:
    """Both legs, each with the script, and neither let off if it fails."""
    job = _yaml(".github/workflows/test.yml")["jobs"]["test"]

    assert job["strategy"]["matrix"]["stack"] == list(stack.STACKS)
    assert job["strategy"]["fail-fast"] is False  # both answers are wanted
    runs = [step.get("run") for step in job["steps"]]
    assert runs.count("python tools/stack.py ${{ matrix.stack }}") == 1
    assert "pip install -r requirements-test.txt" not in runs
    assert "continue-on-error" not in job
    assert not [step for step in job["steps"] if "continue-on-error" in step]
    # The tests are told which leg they run on (see the declared minimum above).
    assert job["env"] == {"GLOWRIUM_STACK": "${{ matrix.stack }}"}


def test_dependabot_leaves_the_test_plugin_alone() -> None:
    """Its one pin is the oldest Home Assistant supported, not a version to raise.

    Dependabot takes constraints-oldest.txt for a requirements file. Nothing
    else that Home Assistant decides is written where it reads, so the plugin
    is all there is to tell it about.
    """
    updates = _yaml(".github/dependabot.yml")["updates"]
    (pip,) = [entry for entry in updates if entry["package-ecosystem"] == "pip"]

    assert [entry["dependency-name"] for entry in pip["ignore"]] == [_PLUGIN]


@pytest.mark.parametrize(
    ("workflow", "jobs"),
    [("test.yml", ["test"]), ("validate.yml", ["hassfest", "hacs"])],
)
def test_a_commit_is_checked_once_whichever_way_it_arrived(
    workflow: str, jobs: list[str]
) -> None:
    """A push and a pull request for the same commit are one run, not two.

    The pull_request run is the one skipped, and only when its branch is in
    this repository: a branch nobody opened a pull request for has only the
    push, and a fork has only the pull request.
    """
    loaded = _yaml(f".github/workflows/{workflow}")

    assert list(loaded["jobs"]) == jobs
    for name in jobs:
        assert " ".join(loaded["jobs"][name]["if"].split()) == _ONCE
