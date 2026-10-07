"""Install one of the two Home Assistant stacks the tests run on.

    python tools/test_stack.py oldest   # the oldest Home Assistant supported
    python tools/test_stack.py newest   # what the test plugin tracks today

It installs into the environment of the Python that runs it, so make a
virtualenv first. CI runs this very script for each of its two legs: what CI
tests is what this installs.

Home Assistant comes with the test plugin, which carries one exact release of
it. Unpinned, the plugin is the newest release, pre-releases included; the
oldest stack holds it to the release named in constraints-oldest.txt.

What Home Assistant's own Bluetooth and USB integrations require is not part
of that distribution: Home Assistant installs it at run time, at versions it
dictates. Those versions are read here from the Home Assistant just installed
(its package_constraints.txt) and are written nowhere in this repository. So
nothing has to be edited when Home Assistant moves, and the tests run on the
libraries a user of that release has - not on the newest of each, which no
installation runs.
"""

from __future__ import annotations

from collections.abc import Callable
import importlib
import importlib.metadata
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements-test.txt"
OLDEST = ROOT / "constraints-oldest.txt"
STACKS = ("oldest", "newest")

# What Home Assistant dictates the version of, and the tests lean on: the
# libraries this integration reaches the lamp through - and reaches into, to
# close a client's bus - and the two that the usb integration needs before
# bluetooth can be imported at all.
DICTATED = (
    "aiousbwatcher",
    "serialx",
    "dbus-fast",
    "bleak",
    "bleak-retry-connector",
    "habluetooth",
)
# Needed as well, since serialx imports it, and not in Home Assistant's list.
COMPANIONS = ("aioesphomeapi",)


def _name(spelt: str) -> str:
    """Return a package name as pip compares it."""
    return re.sub(r"[-_.]+", "-", spelt).lower()


def dictated(constraints: str) -> dict[str, str]:
    """Return the version Home Assistant dictates for each of ``DICTATED``.

    ``constraints`` is the text of its package_constraints.txt. A library the
    list does not hold to one exact version means this script is out of date;
    it says which, and installs nothing in its place.
    """
    exact: dict[str, str] = {}
    for line in constraints.splitlines():
        pin = re.fullmatch(r"([A-Za-z0-9._-]+)==([^\s;#]+)", line.strip())
        if pin:
            exact[_name(pin[1])] = pin[2]
    missing = [name for name in DICTATED if name not in exact]
    if missing:
        raise SystemExit(
            "Home Assistant's package_constraints.txt holds no exact version of "
            f"{', '.join(missing)}. tools/test_stack.py has to be brought up to "
            "date with what this Home Assistant requires"
        )
    return {name: exact[name] for name in DICTATED}


def astray(pins: dict[str, str], installed: Callable[[str], str | None]) -> list[str]:
    """Return a line for each library that is not at its dictated version."""
    said = []
    for name, wanted in pins.items():
        found = installed(name)
        if found != wanted:
            has = "is not installed" if found is None else f"is {found}"
            said.append(f"{name} {has}, and Home Assistant dictates {wanted}")
    return said


def first_install(stack: str) -> list[str]:
    """Return what pip is asked for first: the plugin, and Home Assistant with it."""
    if stack == "newest":
        # Upgrading: an environment built earlier moves on to today's newest.
        return ["--upgrade", "-r", str(REQUIREMENTS)]
    if stack == "oldest":
        return ["-r", str(REQUIREMENTS), "-c", str(OLDEST)]
    raise SystemExit(f"usage: python tools/test_stack.py {'|'.join(STACKS)}")


def _pip_install(*what: str) -> None:
    command = [sys.executable, "-m", "pip", "install", *what]
    subprocess.run(command, check=True)  # noqa: S603 - pip, and arguments built here


def _installed(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _constraints_of_home_assistant() -> str:
    importlib.invalidate_caches()
    found = importlib.util.find_spec("homeassistant")
    if found is None or found.origin is None:
        raise SystemExit("the test plugin did not bring Home Assistant with it")
    return Path(found.origin).with_name("package_constraints.txt").read_text()


def _report(stack: str, pins: dict[str, str]) -> None:
    """Say which Home Assistant this is - in the run's summary too, under CI."""
    home_assistant = _installed("homeassistant")
    print(f"\n{stack}: Home Assistant {home_assistant}")
    for name in pins:
        print(f"  {name} {_installed(name)}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as out:
            out.write(f"### {stack}: Home Assistant {home_assistant}\n\n")
            out.writelines(f"- `{name}` {_installed(name)}\n" for name in pins)


def main(argv: list[str]) -> int:
    """Install the stack named on the command line, and check what came of it."""
    first = first_install(argv[1] if len(argv) == 2 else "")  # noqa: PLR2004
    stack = argv[1]
    _pip_install(*first)
    pins = dictated(_constraints_of_home_assistant())
    _pip_install(*(f"{name}=={version}" for name, version in pins.items()), *COMPANIONS)
    importlib.invalidate_caches()
    _report(stack, pins)
    wrong = astray(pins, _installed)
    for line in wrong:
        print(f"!! {line}")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
