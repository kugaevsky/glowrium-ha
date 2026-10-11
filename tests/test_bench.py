"""Tests for what tools/bench.py prints.

The bench drives a real lamp and is not run here. What it prints is: its
report is what one looks at next to a lamp, and what gets shown to somebody
else when the lamp does something nobody expected.
"""

import ast
import asyncio
import gc
import io
import logging
from types import ModuleType
import weakref

import pytest

from custom_components.glowrium import cbor
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

from .conftest import ROOT, load_tool
from .lamp import ScriptedKeeper

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
    """Return tools/bench.py as a module."""
    return load_tool("bench")


def _a_lamp() -> GlowriumCoordinator:
    """Return a coordinator that has heard everything a lamp says."""
    coordinator = GlowriumCoordinator(
        None, _ADDRESS, "bench", keeper=ScriptedKeeper(None)
    )
    coordinator._mirror.echo(
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


def _running(name: str) -> weakref.ref[asyncio.Task[None]]:
    """Return the task called ``name`` without becoming what holds it."""
    (ref,) = (
        weakref.ref(task) for task in asyncio.all_tasks() if task.get_name() == name
    )
    return ref


def _wake(waited: weakref.ref[asyncio.Future[None]]) -> None:
    """Give the work what it waits for, and hold on to nothing of it."""
    future = waited()
    assert future is not None
    future.set_result(None)


@pytest.mark.parametrize("asked_as", ["spawn", "run_lasting"])
async def test_work_the_bench_is_asked_to_start_is_held_until_it_is_done(
    bench: ModuleType, asked_as: str
) -> None:
    """What the device half has started on the bench runs to its end.

    A reconnect, a priming, a probe, a hang-up. Home Assistant holds such a
    task on its entry or on itself; the bench has neither, and the loop
    holds a task only weakly: one nobody else refers to can be collected
    half-way through. The bench's keeper holds it for as long as it runs,
    and lets go of it once it is done.
    """
    waits_for: list[weakref.ref[asyncio.Future[None]]] = []
    finished: list[int] = []

    async def _work() -> None:
        # Held by the work alone, as an answer from a lamp is: whoever
        # held it would be holding the work that waits for it.
        waited = asyncio.get_running_loop().create_future()
        waits_for.append(weakref.ref(waited))
        await waited
        finished.append(1)

    keeper = bench.BenchKeeper()
    getattr(keeper, asked_as)(_work(), "glowrium probe of the bench")
    await asyncio.sleep(0)  # it starts, and waits
    running = _running("glowrium probe of the bench")

    gc.collect()
    assert running() is not None  # still there to be woken

    _wake(waits_for[0])
    for _ in range(3):  # the task ends, then its done-callback runs
        await asyncio.sleep(0)
    gc.collect()

    assert finished == [1]
    assert running() is None  # and nothing holds work that is done


_PRIVATE = ("12.3456", "65.4321", _CURVE.hex(), "06:00", "18:00", _SERIAL, _IN_INFO)
# Asked for everything, the curve is shown as the times it holds, not as bytes.
_SHOWN = tuple(private for private in _PRIVATE if private != _CURVE.hex())


def test_the_report_leaves_out_what_places_or_identifies_the_lamp(
    bench: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """A report can be shown to somebody else as it is.

    Where the lamp is, the sunrise and sunset times it works out from that,
    and its serial number are not in it. That they are there is: whoever
    reads the report can see the lamp has them.
    """
    bench._report(_a_lamp())

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
    monkeypatch.setattr(bench._PRIVACY, "show", True)

    bench._report(_a_lamp())

    printed = capsys.readouterr().out
    for private in _SHOWN:
        assert private in printed


def test_an_address_is_printed_by_its_end(
    bench: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enough to tell two lamps apart in a scan, and not the address."""
    assert bench._address(_ADDRESS) == "…EE:FF"
    monkeypatch.setattr(bench._PRIVACY, "show", True)
    assert bench._address(_ADDRESS) == _ADDRESS


@pytest.mark.parametrize(
    ("info", "said"),
    [
        (
            f"pkey:Glowrium-C051,devid:{_SERIAL};version:4,mac:{_IN_INFO};;",
            "model not as expected, firmware not as expected",
        ),
        # The lamp said nothing of its firmware: that is not the same thing.
        ("pkey:Glowrium-C051;;", "model Glowrium-C051, firmware unknown"),
    ],
)
def test_a_model_and_a_firmware_are_printed_as_the_log_says_them(
    bench: ModuleType, capsys: pytest.CaptureFixture[str], info: str, said: str
) -> None:
    """The device-info string is the lamp's, and so is where its fields end."""
    coordinator = _a_lamp()
    coordinator.device_info = _parse_device_info(info.encode())

    bench._report(coordinator)

    printed = capsys.readouterr().out
    assert _SERIAL not in printed
    assert _IN_INFO not in printed
    assert said in printed


def test_nothing_that_is_printed_carries_the_lamps_whole_address(
    bench: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not the bench's own lines, and not the ones it did not word.

    The integration names the lamp by its address in every line of its log -
    "is out of reach" comes at the end of every run - and the Bluetooth stack
    puts it into its errors. Both go through the streams the bench prints to,
    so that is where the address is taken out.
    """
    monkeypatch.setattr(bench._PRIVACY, "address", _ADDRESS)
    printed = io.StringIO()
    out = bench._Masked(printed)
    handler = logging.StreamHandler(out)
    logger = logging.getLogger("custom_components.glowrium.test_bench")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("bench (%s) is out of reach", _ADDRESS)
        out.write(f"connect failed: BleakError('{_ADDRESS} was not found')\n")
        # BlueZ names the device by a path, and not every line shouts.
        out.write("/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF is gone\n")
        out.write(f"no route to {_ADDRESS.lower()}\n")
    finally:
        logger.removeHandler(handler)

    said = printed.getvalue()
    assert _ADDRESS.lower() not in said.lower()
    assert "AA_BB_CC" not in said
    assert said.count("…EE:FF") == 3
    assert "dev_…EE_FF is gone" in said

    # Asked for everything, a line goes out exactly as it came.
    monkeypatch.setattr(bench._PRIVACY, "show", True)
    out.write(f"connecting to bench ({_ADDRESS})\n")
    out.write(f"no route to {_ADDRESS.lower()}\n")
    assert _ADDRESS in printed.getvalue()
    assert _ADDRESS.lower() in printed.getvalue()


def test_before_the_lamp_is_found_there_is_no_address_to_take_out(
    bench: ModuleType,
) -> None:
    """An empty address would match everywhere."""
    assert bench._PRIVACY.address == ""
    printed = io.StringIO()
    bench._Masked(printed).write("scanning 10s…\n")
    assert printed.getvalue() == "scanning 10s…\n"


def test_the_report_lists_a_property_nobody_named(
    bench: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """A property the integration has no name for is listed all the same.

    That is how a new model shows what else it reports. The mirror keeps a
    bounded number of such ids, and the bench prints what the mirror holds.
    """
    coordinator = _a_lamp()
    coordinator._ingest(cbor.encode({0x60: 5}))

    bench._report(coordinator)

    assert "0x60 ?" in capsys.readouterr().out


def test_the_bench_reaches_the_coordinator_by_two_private_names_only() -> None:
    """The bench speaks to the lamp as Home Assistant does: by the interface.

    Two things it reaches for that are not on it: the flag that keeps the
    bring-up from ever running on a bench, and the link the coordinator
    holds, through which it makes the connect Home Assistant would make at
    start-up - its one door to the link, as ``link_of`` is the tests'. The
    probes that reached for the client, the lock and the rest were retired
    with #21: mypy does not read this file, so a reach that outlived its
    name would only have shown next to a lamp.
    """
    tree = ast.parse((ROOT / "tools" / "bench.py").read_text(encoding="utf-8"))
    reached = sorted(
        {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr.startswith("_")
            and isinstance(node.value, ast.Name)
            and node.value.id == "coordinator"
        }
    )
    assert reached == ["_activation_checked", "_link"]
