"""Run the real coordinator against a real lamp, from this machine.

A close-range counterpart to the Home Assistant instance on the server. Bring
the laptop near the lamp and this exercises the same code over a strong link
and a different Bluetooth stack.

    .venv/bin/python tools/bench.py             # connect, prime, report
    .venv/bin/python tools/bench.py --watch 5   # ...then follow notifications
    .venv/bin/python tools/bench.py --commands  # ...and exercise the write path

What it prints can be shown to somebody else as it is: where the lamp is, the
sunrise and sunset times it works out from that and its serial number are
left out, and its address is printed by its last characters only - in the
bench's own lines, in the integration's log and in the errors of the
Bluetooth stack alike. The name the lamp advertises is printed as it is.
--show-private puts everything back.

It never writes a setting of its own accord. The bring-up sequence is disabled
outright rather than relied upon not to trigger: a bench should not be able to
reprovision somebody's lamp because a flag read back wrong. Priming, though,
corrects a drifted clock - that is the integration's behaviour, not the
bench's.

It speaks to the lamp as Home Assistant does: the link the coordinator holds
makes the connect and the first exchange on it, and the coordinator's commands
do the writing. It reads nothing of its own. The probes it once carried - of
the DST convention, of the clock resync, of what makes the lamp report its
curve - had their answers, which ARCHITECTURE.md records, and were retired
with #21; they are in this file's history and run from a checkout of the
code they were written against.

Docker on macOS cannot reach the host's Bluetooth controller, so this runs
directly on the machine. The address here is a CoreBluetooth UUID, not a MAC.

What a Mac cannot show: through BlueZ a GATT read of this lamp ends the link
two seconds later, and through macOS it does not (ARCHITECTURE.md, "Priming
state on connect"). The integration reads the device-info string once per
session, last of all, so on a Linux host expect the link to go right after
the report.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
import re
import sys
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakScanner
from homeassistant.util import dt as dt_util

from custom_components.glowrium import identity
from custom_components.glowrium.const import (
    KEY_BRIGHTNESS,
    KEY_CURVE,
    KEY_LATITUDE,
    KEY_LONGITUDE,
    KEY_POWER,
    NAME_PREFIX,
    STATE_KEYS,
)
from custom_components.glowrium.coordinator import GlowriumCoordinator
from custom_components.glowrium.link import Link, dial_by_bluetooth

# Probe brightness: pick whichever end the lamp is not already near, so the
# change is visible and the restore is meaningful.
_BRIGHT_MIDPOINT = 50
_BRIGHT_LOW = 30
_BRIGHT_HIGH = 80

_KEY_NAMES = {
    0x05: "time",
    0x06: "power",
    0x08: "brightness",
    0x09: "circadian",
    0x0A: "latitude",
    0x0B: "longitude",
    0x0D: "schedule",
    0x11: "timer slot",
    0x14: "activated",
    0x17: "indicator",
    0x2B: "lighting mode",
    0x2F: "ramp",
    0x34: "curve",
    0x35: "dst",
}

# What places the lamp: where it is, and the sunrise and sunset times it
# works out from that.
_PRIVATE_KEYS = frozenset({KEY_LATITUDE, KEY_LONGITUDE, KEY_CURVE})
_NOT_SHOWN = "not shown; --show-private prints it"


@dataclass
class _Privacy:
    """What of the lamp's is printed.

    Not what places or identifies it, by default: a report is what gets
    shown to somebody else when the lamp does something nobody expected.
    """

    show: bool = False  # --show-private
    address: str = ""  # the lamp's, once it is found


_PRIVACY = _Privacy()


def _curve(raw: object) -> str:
    """Render 0x34 as wall-clock times - or as how many, unless asked for them."""
    readable = isinstance(raw, (bytes, bytearray)) and raw and not len(raw) % 4
    if not _PRIVACY.show:
        if not readable:
            return "(unreadable)"
        return f"({len(raw) // 4} times, {_NOT_SHOWN})"
    if not readable:
        return f"(unreadable: {raw!r})"
    times = []
    for i in range(0, len(raw), 4):
        seconds = int.from_bytes(raw[i : i + 4], "big")
        times.append(f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}")
    return " ".join(times)


def _as_printed(key: int, raw: object) -> object:
    """Return the value under ``key`` as it is printed."""
    if key == KEY_CURVE:
        return _curve(raw)
    if key in _PRIVATE_KEYS and not _PRIVACY.show:
        return f"({_NOT_SHOWN})"
    return raw.hex() if isinstance(raw, (bytes, bytearray)) else raw


def _state_line(key: int, raw: object) -> str:
    """Return one line of a listing of the state: the id, its name, its value."""
    return f"0x{key:02x} {_KEY_NAMES.get(key, '?'):<14} {_as_printed(key, raw)}"


def _address(address: str) -> str:
    """Return ``address`` as it is printed: by its end, unless asked for whole.

    The end is enough to tell two lamps apart in a scan, and is no more
    than the name a lamp advertises usually ends with.
    """
    return address if _PRIVACY.show else f"…{address[-5:]}"


class _Masked:
    """A stream on which the lamp's address is printed by its end.

    Everything the bench prints goes through one - its own lines, the
    integration's log, which names the lamp by its address in every line,
    and the errors of the Bluetooth stack, which were worded elsewhere. The
    address is taken out here because no line can be trusted to leave it
    out: BlueZ also spells it with underscores, in the path of the device.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        whole = _PRIVACY.address
        if whole and not _PRIVACY.show:
            for spelt in (whole, whole.replace(":", "_")):
                text = re.sub(
                    re.escape(spelt), _address(spelt), text, flags=re.IGNORECASE
                )
        return self._stream.write(text)

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


def _device_info(coordinator: GlowriumCoordinator) -> str:
    """Return the device-info string as it is printed.

    The serial number and the address are in it, and where one field ends is
    what the parser made of the string: the model and the firmware are held
    to their shapes, as the integration's log holds them.
    """
    info = coordinator.device_info
    if not info:
        return "(none)"
    if _PRIVACY.show:
        return str(info)
    said = identity.described(coordinator.model_id, coordinator.sw_version)
    return f"{said}; {len(info)} fields ({_NOT_SHOWN})"


def _link_of(coordinator: GlowriumCoordinator) -> Link:
    """Return the link the coordinator holds.

    The bench's one door to it, as ``link_of`` is the tests': the connect is
    the link's to make, and nothing else of the link's is reached for.
    """
    return coordinator._link  # noqa: SLF001


def _reports(coordinator: GlowriumCoordinator) -> int:
    """Return how many reports the lamp has made, as the diagnostics count them."""
    return int(coordinator.diagnostics()["link"]["reports"])


async def _find(seconds: float) -> object | None:
    """Return the strongest advertising Glowrium, or None."""
    print(f"scanning {seconds:.0f}s…")
    best = None
    seen: dict[str, int] = {}

    def _seen(device: object, adv: object) -> None:
        nonlocal best
        name = getattr(device, "name", None) or ""
        if not name.startswith(NAME_PREFIX):
            return
        seen[device.address] = adv.rssi
        if best is None or adv.rssi > seen.get(best.address, -999):
            best = device

    scanner = BleakScanner(detection_callback=_seen)
    await scanner.start()
    await asyncio.sleep(seconds)
    await scanner.stop()
    for address, rssi in seen.items():
        print(f"  {_address(address)}  RSSI={rssi}")
    return best


def _report(coordinator: GlowriumCoordinator) -> None:
    """Print what the lamp has told us, and what it has not."""
    state = coordinator.state
    print(f"\ndevice-info: {_device_info(coordinator)}")
    print(f"keys reported: {len(state)}")
    for key in sorted(state):
        print(f"  {_state_line(key, state[key])}")
    missing = [k for k in STATE_KEYS if k not in state]
    if missing:
        names = ", ".join(f"0x{k:02x} {_KEY_NAMES.get(k, '?')}" for k in missing)
        print(f"\nSTATE_KEYS still missing after priming: {names}")
    else:
        print("\nafter priming, every key in STATE_KEYS is present")


async def _confirmed_by_device(
    coordinator: GlowriumCoordinator, key: int, want: object, seconds: float = 3.0
) -> str:
    """Wait for the lamp itself to report ``key`` as ``want``.

    The coordinator echoes a successful write into its own mirror, so comparing
    against the mirror proves nothing about the lamp. Watching the report count
    move is what shows the device agreed.
    """
    reports = _reports(coordinator)
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if _reports(coordinator) > reports and coordinator.state.get(key) == want:
            return "confirmed by the lamp"
        await asyncio.sleep(0.05)
    if coordinator.state.get(key) == want:
        return "our echo only - the lamp did not report it back"
    return f"NOT confirmed (mirror says {coordinator.state.get(key)!r})"


async def _exercise_commands(coordinator: GlowriumCoordinator) -> None:
    """Toggle power and brightness, checking the lamp agrees, then put it back."""
    was_on = coordinator.state.get(KEY_POWER)
    was_bright = coordinator.state.get(KEY_BRIGHTNESS)
    print(f"\nwrite path - restoring power={was_on} brightness={was_bright} at the end")

    async def _timed(what: str, coro: object) -> None:
        start = asyncio.get_running_loop().time()
        try:
            await coro
            took = asyncio.get_running_loop().time() - start
            print(f"  {what:<28} ok in {took:.2f}s")
        except Exception as err:
            took = asyncio.get_running_loop().time() - start
            print(f"  {what:<28} FAILED in {took:.2f}s: {err}")

    target = not bool(was_on)
    await _timed(f"power -> {target}", coordinator.async_set_power(target))
    print(f"    {await _confirmed_by_device(coordinator, KEY_POWER, target)}")

    if was_on is not None:
        await _timed(
            f"power -> {bool(was_on)}", coordinator.async_set_power(bool(was_on))
        )
        print(f"    {await _confirmed_by_device(coordinator, KEY_POWER, bool(was_on))}")

    if isinstance(was_bright, int):
        probe = _BRIGHT_LOW if was_bright > _BRIGHT_MIDPOINT else _BRIGHT_HIGH
        await _timed(f"brightness -> {probe}", coordinator.async_set_brightness(probe))
        print(f"    {await _confirmed_by_device(coordinator, KEY_BRIGHTNESS, probe)}")
        await _timed(
            f"brightness -> {was_bright}", coordinator.async_set_brightness(was_bright)
        )
        print(
            f"    {await _confirmed_by_device(coordinator, KEY_BRIGHTNESS, was_bright)}"
        )


def _parse_args() -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", type=float, default=10.0, help="scan seconds")
    parser.add_argument("--watch", type=float, default=0.0, help="follow N seconds")
    parser.add_argument("--debug", action="store_true", help="integration debug log")
    parser.add_argument(
        "--commands",
        action="store_true",
        help="exercise the write path (power, brightness) and restore afterwards",
    )
    parser.add_argument(
        "--show-private",
        action="store_true",
        help="print what is left out by default: the coordinates, the sunrise "
        "and sunset times, the serial number and the whole address",
    )
    return parser.parse_args()


async def main() -> int:
    """Connect once, prime, report, and optionally follow notifications."""
    args = _parse_args()
    _PRIVACY.show = args.show_private
    # Before logging is set up: its handler keeps the stream it finds.
    sys.stdout, sys.stderr = _Masked(sys.stdout), _Masked(sys.stderr)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Home Assistant sets this during setup; nothing does outside it, and the
    # default is UTC. The lamp's clock is local wall time, so a bench that
    # skipped this would hand a +05 lamp a clock five hours slow the moment
    # priming decided it had drifted - the bench corrupting the very thing it
    # is here to check.
    dt_util.set_default_time_zone(datetime.now().astimezone().tzinfo)
    print(f"local time: {dt_util.now():%Y-%m-%d %H:%M:%S %Z}")

    device = await _find(args.scan)
    if device is None:
        print("no Glowrium advertising - move closer, or check the vendor app is off")
        return 1

    _PRIVACY.address = device.address
    coordinator = GlowriumCoordinator(
        None,
        device.address,
        device.name,
        dial=dial_by_bluetooth(lambda: device, device.address, device.name),
    )
    # The bench never provisions anything. Saying so here beats trusting that
    # 0x14 reads back the way we expect.
    coordinator._activation_checked = True  # noqa: SLF001

    print(f"\nconnecting to {device.name} ({_address(device.address)})…")
    try:
        # The connect Home Assistant makes at start-up: the link, then the
        # first exchange on it - the state, the clock, the device info.
        await _link_of(coordinator).connect()
    except Exception as err:
        print(f"connect failed: {err!r}")
        return 1

    _report(coordinator)

    if args.watch:
        print(f"\nfollowing notifications for {args.watch:.0f}s…")
        before = dict(coordinator.state)
        await asyncio.sleep(args.watch)
        changed = {k: v for k, v in coordinator.state.items() if before.get(k) != v}
        print(f"changed while watching: {len(changed)}")
        for key, raw in sorted(changed.items()):
            print(f"  {_state_line(key, raw)}")

    if args.commands:
        await _exercise_commands(coordinator)

    # Let go the way the integration does: through the hang-up, which is what
    # closes the bus behind the client on a BlueZ host.
    await coordinator.async_stop()
    print("\ndisconnected")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
