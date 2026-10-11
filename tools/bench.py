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

It speaks to the lamp as Home Assistant does. The coordinator is built with a
keeper of the bench's own and started as the setup of a config entry starts
it: the first connect and the first exchange on it are work the keeper is
handed, and the coordinator's commands do the writing. It reads nothing of
its own, and reaches for nothing the integration keeps to itself. The probes
it once carried - of the DST convention, of the clock resync, of what makes
the lamp report its curve - had their answers, which ARCHITECTURE.md records,
and were retired with #21; they are in this file's history and run from a
checkout of the code they were written against.

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
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
import re
import sys
from typing import Any, TextIO

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
from custom_components.glowrium.link import Dial, dial_by_bluetooth

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


# How often the bench's clock comes round: as Home Assistant's timer does.
_TICK = 30.0


class BenchKeeper:
    """What keeps the coordinator here: this run (``coordinator.Keeper``).

    Home Assistant keeps a coordinator for a config entry: it has an entry
    to put work on, scanners to hear the lamp through, a dashboard for a
    repair, a registry for what the lamp says it is, and a home. The bench
    has a lamp it found a moment ago and the terminal. It starts the work it
    is handed and holds it - the loop holds a task only weakly - and it
    ticks the clock; the rest it has nowhere to put, and the integration's
    log and the report say it.
    """

    def __init__(self) -> None:
        """Hold nothing yet, and watch nothing."""
        # Work that ends with the watching, and work that nothing ends.
        self._spawned: set[asyncio.Task[None]] = set()
        self._lasting: set[asyncio.Task[None]] = set()
        self._clock: asyncio.TimerHandle | None = None
        # What work ended in, where that was an error nobody caught. A link
        # that is lost is caught and logged by the integration; what is left
        # is a fault, and the bench is where it has to be seen.
        self.faults: list[BaseException] = []

    def spawn(self, work: Coroutine[Any, Any, None], name: str) -> None:
        """Start ``work`` and hold it until it is done, or the watching ends."""
        self._hold(work, name, self._spawned)

    def run_lasting(
        self, work: Coroutine[Any, Any, None], name: str
    ) -> asyncio.Task[None]:
        """Start ``work``, hold it until it is done, and return its task."""
        return self._hold(work, name, self._lasting)

    def _hold(
        self,
        work: Coroutine[Any, Any, None],
        name: str,
        among: set[asyncio.Task[None]],
    ) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(work, name=name)
        among.add(task)

        def _done(done: asyncio.Task[None]) -> None:
            among.discard(done)
            if not done.cancelled() and (fault := done.exception()) is not None:
                self.faults.append(fault)

        task.add_done_callback(_done)
        return task

    def watch(
        self,
        advertising: Callable[[bool], None],
        tick: Callable[[], None],
    ) -> bool:
        """Tick as Home Assistant's timer does, and say the lamp is there.

        The bench scanned for the lamp before it built anything, and does
        not listen to the air after that: it never says the lamp was heard
        or has gone, and what the link makes of a tick is all that keeps it.
        """
        loop = asyncio.get_running_loop()

        def _on_the_clock() -> None:
            self._clock = loop.call_later(_TICK, _on_the_clock)
            tick()

        self._clock = loop.call_later(_TICK, _on_the_clock)
        return True

    def unwatch(self) -> None:
        """Stop the clock, and end the work that was to end with the watching."""
        if self._clock is not None:
            self._clock.cancel()
            self._clock = None
        for task in list(self._spawned):
            task.cancel()

    def stack_fault(self, count: int | None) -> None:
        """Do nothing: the integration's log says it, and there is no dashboard."""

    def lamp_described(
        self, model_id: str | None, firmware: str | None, serial: str | None
    ) -> None:
        """Do nothing: the report prints what the coordinator has of it."""

    def coordinates(self) -> tuple[float, float]:
        """Answer zero and zero: the bench has no home."""
        return 0.0, 0.0

    async def idle(self) -> None:
        """Wait until none of the work this keeper started is still running."""
        while running := self._spawned | self._lasting:
            await asyncio.wait(running)


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
    keeper = BenchKeeper()
    coordinator = _coordinator(
        device.address,
        device.name,
        keeper,
        dial_by_bluetooth(lambda: device, device.address, device.name),
    )

    print(f"\nconnecting to {device.name} ({_address(device.address)})…")
    return await _drive(coordinator, keeper, args)


def _what_went_wrong(
    coordinator: GlowriumCoordinator, keeper: BenchKeeper
) -> str | None:
    """Say why the lamp was not read on the first connect, or None if it was.

    A link that was lost on the way is the integration's to log, and here it
    shows as what the link says of itself: none is held, or one is held that
    the first exchange was not made on - a connect that ran out of time with
    the lamp dialled and not answering. Anything else the work ended in is a
    fault, and is printed as it is.
    """
    if keeper.faults:
        return "; ".join(repr(fault) for fault in keeper.faults)
    link = coordinator.diagnostics()["link"]
    if link["primed"]:
        return None
    what = (
        "a link is held, and the first exchange was not made on it"
        if link["connected"]
        else "no link is held after the first connect"
    )
    return f"{what}. The integration's log says how it ended (--debug)"


def _coordinator(
    address: str, name: str, keeper: BenchKeeper, dial: Dial
) -> GlowriumCoordinator:
    """Build the coordinator the bench drives: the integration's own.

    Kept by the bench, dialling the lamp the bench found - and bringing no
    lamp up. The bench never provisions anything, and saying so here beats
    trusting that 0x14 reads back the way we expect.
    """
    return GlowriumCoordinator(address, name, keeper=keeper, dial=dial, bring_up=False)


async def _drive(
    coordinator: GlowriumCoordinator, keeper: BenchKeeper, args: argparse.Namespace
) -> int:
    """Start the coordinator as the setup of a config entry does, and report.

    The connect Home Assistant makes at start-up - the link, then the first
    exchange on it: the state, the clock, the device info - is work the
    keeper is handed by the start. The bench waits for it to be done. How it
    ended is not an error to catch here (see ``_what_went_wrong``).
    """
    await coordinator.async_start()
    await keeper.idle()
    if (wrong := _what_went_wrong(coordinator, keeper)) is not None:
        print(f"connect failed: {wrong}")
        await coordinator.async_stop()
        await keeper.idle()
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
    await keeper.idle()
    print("\ndisconnected")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
