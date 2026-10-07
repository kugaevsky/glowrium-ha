"""Install one of the two Home Assistant stacks the tests run on.

    python tools/stack.py oldest   # the oldest Home Assistant supported
    python tools/stack.py newest   # what the test plugin tracks today

It installs into the environment of the Python that runs it, so make a
virtualenv first. CI runs this very script for each of its two legs: what CI
tests is what this installs.

Home Assistant comes with the test plugin, which carries one exact release of
it. Unpinned, the plugin is the newest release, pre-releases included; the
oldest stack holds it to the release named in constraints-oldest.txt.

What Home Assistant's own integrations require is not part of that
distribution: Home Assistant installs it at run time, each requirement at the
exact version its manifest names and everything under it within Home
Assistant's package_constraints.txt. This script does the same, for the
integrations the tests cannot be imported without, reading both from the Home
Assistant it has just installed. None of those versions is written in this
repository. So nothing has to be edited when Home Assistant moves, and the
tests run on the libraries a user of that release has - not on the newest of
each, which no installation runs.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements-test.txt"
OLDEST = ROOT / "constraints-oldest.txt"

# What pip is asked for first, for each stack: the test plugin, and Home
# Assistant with it.
FIRST = {
    "oldest": ("-r", str(REQUIREMENTS), "-c", str(OLDEST)),
    # Upgrading, so that an environment built earlier moves on to today's.
    "newest": ("--upgrade", "-r", str(REQUIREMENTS)),
}
STACKS = tuple(FIRST)

# The Home Assistant integrations whose requirements the tests need, whole:
# this one reaches the lamp through bluetooth - and reaches into its
# libraries, to close a client's bus - and bluetooth depends on usb.
INTEGRATIONS = ("bluetooth", "usb")
# And one requirement of a third: serialx, which usb requires, imports it.
ALSO = (("esphome", "aioesphomeapi"),)


def canonical(name: str) -> str:
    """Return a package name as pip compares it."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _exact(integration: str, manifest: dict[str, Any]) -> dict[str, str]:
    """Return the version of each library ``integration`` requires."""
    pins: dict[str, str] = {}
    for requirement in manifest["requirements"]:
        pin = re.fullmatch(r"([A-Za-z0-9._-]+)==([^\s;#]+)", requirement)
        if not pin:
            raise SystemExit(
                f"Home Assistant's {integration} requires {requirement}, which is "
                "not one exact version; tools/stack.py does not know what to "
                "install for it"
            )
        pins[canonical(pin[1])] = pin[2]
    return pins


def required(manifest_of: Callable[[str], dict[str, Any]]) -> dict[str, str]:
    """Return the version of each library the tests need, by its name.

    ``manifest_of`` gives the manifest of a Home Assistant integration. A
    requirement that is not one exact version, or the lack of one this script
    counts on, means the script is out of date with that Home Assistant; it
    says which, and installs nothing in its place.
    """
    pins: dict[str, str] = {}
    for integration in INTEGRATIONS:
        pins |= _exact(integration, manifest_of(integration))
    for integration, library in ALSO:
        of_it = _exact(integration, manifest_of(integration))
        if library not in of_it:
            raise SystemExit(
                f"Home Assistant's {integration} no longer requires {library}; "
                "tools/stack.py has to be brought up to date"
            )
        pins[library] = of_it[library]
    return pins


def astray(pins: dict[str, str], installed: Callable[[str], str | None]) -> list[str]:
    """Return a line for each library that is not at its required version."""
    said = []
    for name, wanted in pins.items():
        found = installed(name)
        if found != wanted:
            has = "is not installed" if found is None else f"is {found}"
            said.append(f"{name} {has}, and this Home Assistant requires {wanted}")
    return said


def installed(name: str) -> str | None:
    """Return the version of ``name`` in this environment, or None."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _home_assistant() -> Path:
    """Return where the installed Home Assistant package lives."""
    importlib.invalidate_caches()
    found = importlib.util.find_spec("homeassistant")
    if found is None or found.origin is None:
        raise SystemExit("the test plugin did not bring Home Assistant with it")
    return Path(found.origin).parent


def manifest_of(integration: str) -> dict[str, Any]:
    """Return the manifest of one of the installed Home Assistant's integrations."""
    path = _home_assistant() / "components" / integration / "manifest.json"
    manifest: dict[str, Any] = json.loads(path.read_text())
    return manifest


def _pip_install(*what: str) -> None:
    command = [sys.executable, "-m", "pip", "install", *what]
    subprocess.run(command, check=True)  # noqa: S603 - pip, and arguments built here


def _report(stack: str, pins: dict[str, str]) -> None:
    """Say which Home Assistant this is - in the run's summary too, under CI."""
    home_assistant = installed("homeassistant")
    print(f"\n{stack}: Home Assistant {home_assistant}")
    for name in pins:
        print(f"  {name} {installed(name)}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as out:
            out.write(f"### {stack}: Home Assistant {home_assistant}\n\n")
            out.writelines(f"- `{name}` {installed(name)}\n" for name in pins)


def main(argv: list[str] | None = None) -> int:
    """Install the stack named on the command line, and check what came of it."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("stack", choices=STACKS, help="the Home Assistant to install")
    stack = parser.parse_args(argv).stack

    _pip_install(*FIRST[stack])
    pins = required(manifest_of)
    # As Home Assistant installs them: each at its version, and whatever it
    # brings with it within Home Assistant's own constraints.
    constraints = _home_assistant() / "package_constraints.txt"
    _pip_install(
        *(f"{name}=={version}" for name, version in pins.items()),
        "-c",
        str(constraints),
    )
    importlib.invalidate_caches()
    _report(stack, pins)
    wrong = astray(pins, installed)
    for line in wrong:
        print(f"!! {line}")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
