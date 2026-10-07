"""Tests for what tools/bench.py prints.

The bench drives a real lamp and is not run here. What it prints is: its
report is what one looks at next to a lamp, and what gets shown to somebody
else when the lamp does something nobody expected.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from custom_components.glowrium.const import (
    KEY_BRIGHTNESS,
    KEY_LATITUDE,
    KEY_LONGITUDE,
    KEY_POWER,
)
from custom_components.glowrium.coordinator import (
    GlowriumCoordinator,
    _parse_device_info,
)

_CURVE_KEY = 0x34
# Seven times, seconds from midnight: 06:00 ... 18:00.
_CURVE = b"".join(
    seconds.to_bytes(4, "big")
    for seconds in (21600, 25200, 30000, 40000, 50000, 60000, 64800)
)
_SERIAL = "CST-0001"
_ADDRESS = "AA:BB:CC:DD:EE:FF"
_IN_INFO = "A1B2C3D4E5F6"


@pytest.fixture(scope="module")
def bench() -> ModuleType:
    """Load tools/bench.py, which is a script and not part of any package."""
    path = Path(__file__).resolve().parent.parent / "tools" / "bench.py"
    spec = importlib.util.spec_from_file_location("glowrium_bench", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _a_lamp() -> GlowriumCoordinator:
    """Return a coordinator that has heard everything a lamp says."""
    coordinator = GlowriumCoordinator(None, _ADDRESS, "bench")
    coordinator.state.update(
        {
            KEY_POWER: True,
            KEY_BRIGHTNESS: 70,
            KEY_LATITUDE: 12.3456,
            KEY_LONGITUDE: 65.4321,
            _CURVE_KEY: _CURVE,
        }
    )
    coordinator.device_info = _parse_device_info(
        f"brand:INLEDCO;pkey:Glowrium-C051;devid:{_SERIAL};"
        f"mac:{_IN_INFO};version:4;;".encode()
    )
    return coordinator


_PRIVATE = ("12.3456", "65.4321", _CURVE.hex(), "06:00", "18:00", _SERIAL, _IN_INFO)


def test_the_report_leaves_out_what_places_or_identifies_the_lamp(
    bench: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """A report can be shown to somebody else as it is.

    Where the lamp is, the sunrise and sunset times it works out from that,
    its serial number and its address are not in it. That they are there is:
    whoever reads the report can see the lamp has them.
    """
    bench._report(_a_lamp(), None)
    bench._settle_curve({}, _CURVE, _CURVE)

    printed = capsys.readouterr().out
    for private in _PRIVATE:
        assert private not in printed
    assert "latitude" in printed
    assert "longitude" in printed
    assert "0x34" in printed
    # And the rest is as useful as it was.
    assert "brightness     70" in printed
    assert "model Glowrium-C051, firmware 4" in printed


def test_the_report_shows_everything_when_asked_to(
    bench: ModuleType,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Next to one's own lamp, the coordinates are what one came to see."""
    monkeypatch.setattr(bench._Private, "shown", True)

    bench._report(_a_lamp(), None)
    bench._settle_curve({}, _CURVE, _CURVE)

    printed = capsys.readouterr().out
    for private in _PRIVATE:
        assert private in printed


def test_an_address_is_printed_by_its_end(
    bench: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enough to tell two lamps apart in a scan, and not the address."""
    assert bench._address(_ADDRESS) == "…EE:FF"
    monkeypatch.setattr(bench._Private, "shown", True)
    assert bench._address(_ADDRESS) == _ADDRESS


def test_a_model_or_firmware_with_something_glued_to_it_is_not_printed(
    bench: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """The device-info string is the lamp's, and so is where its fields end."""
    coordinator = _a_lamp()
    coordinator.device_info = _parse_device_info(
        f"pkey:Glowrium-C051,devid:{_SERIAL};version:4,mac:{_IN_INFO};;".encode()
    )

    bench._report(coordinator, None)

    printed = capsys.readouterr().out
    assert _SERIAL not in printed
    assert _IN_INFO not in printed
    assert "model not as expected, firmware not as expected" in printed
