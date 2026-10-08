"""Run the real coordinator against a real lamp, from this machine.

A close-range counterpart to the Home Assistant instance on the server. Bring
the laptop near the lamp and this exercises the same code over a strong link
and a different Bluetooth stack - which is how questions like "does a read of
the characteristic carry every key" get answered without guessing.

    .venv/bin/python tools/bench.py            # connect, prime, report
    .venv/bin/python tools/bench.py --watch 5  # ...then follow notifications
    .venv/bin/python tools/bench.py --clock    # verify the clock resync, both branches

What it prints can be shown to somebody else as it is: where the lamp is, the
sunrise and sunset times it works out from that and its serial number are
left out, and its address is printed by its last characters only - in the
bench's own lines, in the integration's log and in the errors of the
Bluetooth stack alike. The name the lamp advertises is printed as it is.
--show-private puts everything back.

It never writes a setting of its own accord. The bring-up sequence is disabled
outright rather than relied upon not to trigger: a bench should not be able to
reprovision somebody's lamp because a flag read back wrong. Priming, though,
now corrects a drifted clock - that is the integration's behaviour, not the
bench's, and --clock is the mode that puts it on trial deliberately.

Docker on macOS cannot reach the host's Bluetooth controller, so this runs
directly on the machine. The address here is a CoreBluetooth UUID, not a MAC.

What a Mac cannot show: through BlueZ a GATT read of this lamp ends the link
two seconds later, and through macOS it does not (ARCHITECTURE.md, "Priming
state on connect"). The bench reads facebd02 itself, to report what a read
carries, so on a Linux host expect the link to go right after the report.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from pathlib import Path
import re
import sys
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakScanner
from homeassistant.util import dt as dt_util

from custom_components.glowrium import cbor, identity, protocol
from custom_components.glowrium.const import (
    KEY_BRIGHTNESS,
    KEY_DST,
    KEY_LATITUDE,
    KEY_LONGITUDE,
    KEY_POWER,
    KEY_TIME,
    NAME_PREFIX,
    NOTIFY_UUID,
    STATE_KEYS,
)
from custom_components.glowrium.coordinator import (
    _CLOCK_TOLERANCE,
    GlowriumCoordinator,
    dial_by_bluetooth,
)

# Probe brightness: pick whichever end the lamp is not already near, so the
# change is visible and the restore is meaningful.
# 0x05 is year_BE(2), month, day, hour, minute, second.
_CLOCK_BYTES = 7

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

# The circadian curve the lamp computes for itself (see _curve).
_CURVE_KEY = 0x34
# What places the lamp: where it is, and the sunrise and sunset times it
# works out from that.
_PRIVATE_KEYS = frozenset({KEY_LATITUDE, KEY_LONGITUDE, _CURVE_KEY})
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


def _as_printed(key: int, raw: object) -> object:
    """Return the value under ``key`` as it is printed."""
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


def _report(coordinator: GlowriumCoordinator, from_read: set[int] | None) -> None:
    """Print what the lamp has told us, and what it has not."""
    state = coordinator.state
    print(f"\ndevice-info: {_device_info(coordinator)}")
    print(f"keys reported: {len(state)}")
    for key in sorted(state):
        print(f"  {_state_line(key, state[key])}")
    if from_read is not None:
        gap = [k for k in STATE_KEYS if k not in from_read]
        print(f"\na read of facebd02 alone carried {len(from_read)} keys")
        if gap:
            names = ", ".join(f"0x{k:02x} {_KEY_NAMES.get(k, '?')}" for k in gap)
            print(f"  it did NOT carry: {names}")
            print("  those come from the batched STATE_KEYS request")
        else:
            print("  which is every key in STATE_KEYS - no request needed")
    missing = [k for k in STATE_KEYS if k not in state]
    if missing:
        names = ", ".join(f"0x{k:02x} {_KEY_NAMES.get(k, '?')}" for k in missing)
        print(f"\nSTATE_KEYS still missing after priming: {names}")
    else:
        print("\nafter priming, every key in STATE_KEYS is present")


async def _read_directly(coordinator: GlowriumCoordinator) -> set[int] | None:
    """Read facebd02 once and return the keys it actually carried."""
    client = coordinator._client  # noqa: SLF001
    if client is None:
        return None
    try:
        raw = bytes(await client.read_gatt_char(NOTIFY_UUID))
    except Exception as err:
        print(f"\ndirect read failed: {err}")
        return None
    value, short = cbor.decode_frame(raw)
    keys = set(value) if isinstance(value, dict) else set()
    print(
        f"\ndirect read of facebd02: {len(raw)} bytes, header 0x{raw[0]:02x}, "
        f"{len(keys)} pairs, split-across-frames={short}"
    )
    return keys


async def _confirmed_by_device(
    coordinator: GlowriumCoordinator, key: int, want: object, seconds: float = 3.0
) -> str:
    """Wait for the lamp itself to report ``key`` as ``want``.

    The coordinator echoes a successful write into its own mirror, so comparing
    against the mirror proves nothing about the lamp. Watching the report count
    move is what shows the device agreed.
    """
    reports = coordinator._reports  # noqa: SLF001
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if coordinator._reports > reports and coordinator.state.get(key) == want:  # noqa: SLF001
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


def _clock(raw: object) -> str:
    """Render the device clock (0x05) as it reads on the wire."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < _CLOCK_BYTES:
        return f"(unreadable: {raw!r})"
    year = (raw[0] << 8) | raw[1]
    return (
        f"{year:04d}-{raw[2]:02d}-{raw[3]:02d} {raw[4]:02d}:{raw[5]:02d}:{raw[6]:02d}"
    )


async def _probe_dst(coordinator: GlowriumCoordinator) -> None:
    """Find out whether enabling DST moves the lamp's own clock.

    The integration writes local wall-clock time, which already carries any
    summer-time shift. If the lamp then applies the offset from 0x35 on top,
    every schedule runs an hour out - and nothing in the protocol says which
    convention the firmware expects. Toggling the flag and watching 0x05 is the
    cheapest way to find out, and it is reversible.
    """
    client = coordinator._client  # noqa: SLF001
    if client is None:
        print("\nno link for the DST probe")
        return
    was = coordinator.state.get(KEY_DST)
    print(
        f"\nDST probe - restoring 0x35={was.hex() if isinstance(was, bytes) else was}"
    )

    async def _snapshot(label: str) -> None:
        # The clock comes from the characteristic, which carries it. 0x35 does
        # not - the read stops at 0x15 - so that one is taken from the mirror,
        # where the lamp's own notification puts it.
        raw = bytes(await client.read_gatt_char(NOTIFY_UUID))
        value, _ = cbor.decode_frame(raw)
        clock = value.get(0x05) if isinstance(value, dict) else None
        dst = coordinator.state.get(KEY_DST)
        dst_hex = dst.hex() if isinstance(dst, (bytes, bytearray)) else dst
        print(f"  {label:<22} clock={_clock(clock)}  0x35={dst_hex}")

    before_all = dict(coordinator.state)
    await _snapshot("before")
    for enabled in (True, False):
        await coordinator.async_set_dst(enabled)
        await asyncio.sleep(2.5)
        await _snapshot(f"DST={enabled}")
    if isinstance(was, (bytes, bytearray)):
        await coordinator.async_set_dst(bool(was[0]))
        await asyncio.sleep(1.5)
        await _snapshot("restored")
    print(
        "  the lamp applies the offset itself if the clock jumps an hour with the flag"
    )
    moved = {
        k: v
        for k, v in coordinator.state.items()
        if k not in (KEY_DST, 0x05) and before_all.get(k) != v
    }
    if moved:
        print("  other keys the lamp recomputed while the flag moved:")
        for key, value in sorted(moved.items()):
            print(f"    {_state_line(key, value)}")
    else:
        print(
            "  nothing else the lamp reports changed - the flag is stored, not applied"
        )


# Ten times the coordinator's tolerance, and nothing like an hour: an offset of
# exactly an hour could be confused with a DST convention, and a smaller one
# with the lamp's own second-level jitter.
_INDUCED_DRIFT = timedelta(minutes=10)


async def _device_clock(
    coordinator: GlowriumCoordinator,
) -> tuple[datetime | None, float | None]:
    """Read 0x05 off the characteristic and return it with its drift, in seconds.

    Straight from the lamp rather than from the state mirror: a write echoes
    into the mirror, so the mirror would agree with us about a clock the lamp
    never took.
    """
    client = coordinator._client  # noqa: SLF001
    if client is None:
        return None, None
    # Retried: a read taken the instant a link is rebuilt has come back without
    # 0x05 in it, and reporting that as "the lamp has no clock" would condemn a
    # correction that did happen. Three tries cost a second and remove the
    # question.
    got: object = None
    for _ in range(3):
        try:
            raw = bytes(await client.read_gatt_char(NOTIFY_UUID))
        except Exception as err:
            print(f"  clock read failed: {err!r}")
            return None, None
        value, _short = cbor.decode_frame(raw)
        got = value.get(KEY_TIME) if isinstance(value, dict) else None
        if isinstance(got, (bytes, bytearray)) and len(got) >= _CLOCK_BYTES:
            break
        await asyncio.sleep(0.4)
    if not isinstance(got, (bytes, bytearray)) or len(got) < _CLOCK_BYTES:
        return None, None
    try:
        reported = datetime(
            (got[0] << 8) | got[1], got[2], got[3], got[4], got[5], got[6]
        )
    except ValueError:  # the lamp can report an impossible date
        return None, None
    now = dt_util.now().replace(tzinfo=None)
    return reported, (reported - now).total_seconds()


async def _reconnect(coordinator: GlowriumCoordinator, tries: int = 3) -> bool:
    """Drop the link and build a fresh one exactly as the integration does."""
    client = coordinator._client  # noqa: SLF001
    if client is not None:
        with contextlib.suppress(Exception):
            await client.disconnect()
    coordinator._client = None  # noqa: SLF001
    coordinator._primed_client = None  # noqa: SLF001
    for attempt in range(1, tries + 1):
        await asyncio.sleep(2.0)
        try:
            async with coordinator._lock:  # noqa: SLF001
                await coordinator._connect_locked()  # noqa: SLF001
        except Exception as err:
            print(f"    reconnect {attempt}/{tries} failed: {err!r}")
            continue
        if coordinator._client is not None:  # noqa: SLF001
            return True
        print(f"    reconnect {attempt}/{tries}: the link answered nothing")
    return False


async def _ensure_clock_right(coordinator: GlowriumCoordinator) -> bool:
    """Leave the lamp with a correct clock, whatever the test did to it.

    Unconditional, and not an afterthought: the Home Assistant instance that
    gets the lamp back runs a version with no resync at all, so a lamp handed
    over with a wrong clock would run its schedule and its circadian curve off
    it indefinitely, with nothing to show for it - which is precisely the
    failure this fix exists to prevent.
    """
    for attempt in range(1, 4):
        reported, drift = await _device_clock(coordinator)
        if drift is not None and abs(drift) < _CLOCK_TOLERANCE:
            print(f"  clock left correct: {reported} ({drift:+.0f}s)")
            return True
        print(f"  restoring the clock by hand ({attempt}/3)…")
        try:
            async with coordinator._lock:  # noqa: SLF001
                await coordinator._write_raw(  # noqa: SLF001
                    protocol.clock_command(dt_util.now())
                )
        except Exception as err:
            print(f"    write failed: {err!r}")
            await _reconnect(coordinator)
        await asyncio.sleep(1.5)
    reported, drift = await _device_clock(coordinator)
    print(f"  !! THE LAMP'S CLOCK IS STILL WRONG: {reported} ({drift})")
    print("  !! do not hand it back to Home Assistant until this is put right")
    return False


async def _probe_clock(coordinator: GlowriumCoordinator) -> bool:
    """Put the clock resync on trial against the lamp, both branches.

    Called on a link that is up but deliberately not primed, so the clock can
    be read as found before anything has had the chance to correct it.
    """
    print("\nclock resync - the lamp's own 0x05 against real time")
    reported, drift = await _device_clock(coordinator)
    if drift is None:
        print("  the lamp reports no readable clock - nothing to judge")
        return False
    print(f"  as found:        {reported}  {drift:+.0f}s   (up, not yet primed)")

    writes = coordinator._writes_sent  # noqa: SLF001
    await coordinator._async_prime()  # noqa: SLF001
    primed, primed_drift = await _device_clock(coordinator)
    wrote = coordinator._writes_sent - writes  # noqa: SLF001
    print(f"  after priming:   {primed}  {primed_drift:+.0f}s   writes={wrote}")

    quiet_ok = True
    if abs(drift) < _CLOCK_TOLERANCE:
        quiet_ok = wrote == 0
        print(
            "  quiet branch:    "
            + (
                "OK - a clock this close was left alone"
                if quiet_ok
                else f"FAILED - it wrote {wrote} time(s) anyway"
            )
        )
    else:
        print(
            f"  the lamp was already {drift:+.0f}s out, so priming should have "
            "corrected it"
        )
        quiet_ok = wrote >= 1 and abs(primed_drift) < _CLOCK_TOLERANCE
        print("  correcting branch (as found): " + ("OK" if quiet_ok else "FAILED"))

    seconds = int(_INDUCED_DRIFT.total_seconds())
    print(f"\n  inducing a {seconds}s drift - ten times the tolerance, and not")
    print("  an hour, so it cannot be read as a daylight-saving offset")
    wrong = protocol.clock_command(dt_util.now() - _INDUCED_DRIFT)
    try:
        async with coordinator._lock:  # noqa: SLF001
            await coordinator._write_raw(wrong)  # noqa: SLF001
    except Exception as err:
        print(f"  could not write the wrong clock: {err!r}")
        await _ensure_clock_right(coordinator)
        return False
    await asyncio.sleep(1.5)
    bad, bad_drift = await _device_clock(coordinator)
    shown = "unreadable" if bad_drift is None else f"{bad_drift:+.0f}s"
    print(f"  lamp now reads:  {bad}  {shown}")
    if bad_drift is None or abs(bad_drift) < _CLOCK_TOLERANCE:
        print("  the lamp did not take the wrong clock - the correction is untestable")
        await _ensure_clock_right(coordinator)
        return False

    print("  reconnecting - the same connect->prime path the integration runs")
    if not await _reconnect(coordinator):
        print("  !! lost the lamp while its clock is wrong")
        await _ensure_clock_right(coordinator)
        return False
    fixed, fixed_drift = await _device_clock(coordinator)
    corrected = fixed_drift is not None and abs(fixed_drift) < _CLOCK_TOLERANCE
    shown = "unreadable" if fixed_drift is None else f"{fixed_drift:+.0f}s"
    print(f"  after reconnect: {fixed}  {shown}")
    print(
        "  correcting branch: "
        + ("OK - the lamp was put right by the resync" if corrected else "FAILED")
    )

    restored = await _ensure_clock_right(coordinator)
    return quiet_ok and corrected and restored


# 0x34 is seven four-byte times, seconds from midnight: the circadian curve the
# lamp computes for itself. A date three months out moves sunrise by the best
# part of two hours here, which is far past any ambiguity.
_CURVE_DATE_SHIFT = timedelta(days=91)


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


class _Tap:
    """Record every notification the lamp pushes, and when.

    Installed before the connect, because ``start_notify`` is handed a bound
    method: replacing the attribute afterwards would leave bleak calling the
    original and the tap silent.
    """

    def __init__(self, coordinator: GlowriumCoordinator) -> None:
        """Wrap the coordinator's notify callback without replacing it."""
        self._original = coordinator._on_notify  # noqa: SLF001
        self.events: list[tuple[float, dict]] = []
        self._started = 0.0

        def _tapped(char: object, data: bytearray) -> None:
            try:
                value, _short = cbor.decode_frame(bytes(data))
            except Exception:
                value = {}
            if isinstance(value, dict) and value:
                now = asyncio.get_running_loop().time()
                self.events.append((now - self._started, value))
            self._original(char, data)

        coordinator._on_notify = _tapped  # type: ignore[method-assign]  # noqa: SLF001

    def start(self) -> None:
        """Zero the clock the events are timed against."""
        self._started = asyncio.get_running_loop().time()
        self.events.clear()

    def since(self, mark: int) -> list[tuple[float, dict]]:
        """Events recorded after ``mark``."""
        return self.events[mark:]

    def report(self, label: str, mark: int) -> bool:
        """Print what arrived since ``mark``; return whether 0x34 was in it."""
        events = self.since(mark)
        if not events:
            print(f"  {label:<26} nothing")
            return False
        carried = False
        for at, value in events:
            keys = " ".join(f"0x{k:02x}" for k in sorted(value))
            print(f"  {label:<26} +{at:5.1f}s  {len(value):2d} keys: {keys}")
            label = ""
            if _CURVE_KEY in value:
                carried = True
                print(f"  {'':<26}         0x34 = {_curve(value[_CURVE_KEY])}")
        return carried


def _settle_curve(answers: dict[str, bool], restored: object, moved: object) -> None:
    """Print what the phases add up to."""
    print("\n  what this settles:")
    for question, answer in answers.items():
        print(f"    0x34 {question}: {answer}")
    if isinstance(moved, (bytes, bytearray)):
        print(f"    curve on the moved date: {_curve(moved)}")
    if isinstance(restored, (bytes, bytearray)):
        print(f"    curve once restored:     {_curve(restored)}")
    if not (
        isinstance(moved, (bytes, bytearray))
        and isinstance(restored, (bytes, bytearray))
    ):
        return
    if moved != restored:
        print(
            "    the curve moved with the date and came back - the clock feeds "
            "the circadian engine"
        )
    else:
        print(
            "    the curve did NOT move with the date - whatever computes it, "
            "the date is not the input"
        )


async def _probe_curve(coordinator: GlowriumCoordinator, tap: _Tap) -> bool:
    """Find out whether writing the clock is what makes the lamp report 0x34.

    An earlier session saw 0x34 arrive in a run that also corrected the clock
    and concluded the clock feeds the circadian engine. That does not follow:
    both runs wrote a clock, neither recorded when 0x34 arrived, and a lamp
    that dumps state on subscribe or after any write at all would look
    identical. This isolates it - a quiet window, a write with nothing to do
    with time, then the clock - and then settles the question outright by
    moving the date, since the curve is computed from the date and the
    coordinates and cannot follow a clock moved within the same day.
    """
    print("\n0x34 - is the clock what makes the lamp recompute its curve?")

    mark = len(tap.events)
    print("\n  A. quiet window, 20s, nothing written")
    await asyncio.sleep(20.0)
    unprompted = tap.report("on subscribe / unprompted", mark)

    client = coordinator._client  # noqa: SLF001
    if client is None:
        print("  lost the link before anything could be written")
        return False
    raw = bytes(await client.read_gatt_char(NOTIFY_UUID))
    value, _short = cbor.decode_frame(raw)
    in_read = _CURVE_KEY in value if isinstance(value, dict) else False
    brightness = value.get(KEY_BRIGHTNESS) if isinstance(value, dict) else None
    print(f"  0x34 present in the facebd02 read: {in_read}")

    async def _write(what: str, payload: dict[int, object]) -> bool:
        mark = len(tap.events)
        try:
            async with coordinator._lock:  # noqa: SLF001
                await coordinator._write_raw(payload)  # noqa: SLF001
        except Exception as err:
            print(f"  {what} failed: {err!r}")
            return False
        await asyncio.sleep(6.0)
        return tap.report(what, mark)

    print("\n  B. control write: brightness set to what it already is")
    after_control = False
    if isinstance(brightness, int):
        after_control = await _write(
            "after 0x08 (no change)", {KEY_BRIGHTNESS: brightness}
        )
    else:
        print("  brightness unreadable - control write skipped")

    print("\n  C. clock write, same date and time it already believes")
    after_same = await _write(
        "after 0x05 (unchanged)",
        protocol.clock_command(dt_util.now()),
    )

    print(f"\n  D. clock write, date moved {_CURVE_DATE_SHIFT.days} days on")
    shifted = dt_util.now() + _CURVE_DATE_SHIFT
    print(f"     the lamp will believe it is {shifted:%Y-%m-%d %H:%M}")
    after_date = await _write(
        "after 0x05 (date moved)",
        protocol.clock_command(shifted),
    )
    moved_curve = coordinator.state.get(_CURVE_KEY)

    print("\n  E. clock restored")
    after_restore = await _write(
        "after 0x05 (restored)",
        protocol.clock_command(dt_util.now()),
    )

    _settle_curve(
        {
            "arrives unprompted, no write at all": unprompted,
            "follows a write that is not the clock": after_control,
            "follows a clock write that changes nothing": after_same,
            "follows a clock write that moves the date": after_date,
            "follows the restore": after_restore,
        },
        coordinator.state.get(_CURVE_KEY),
        moved_curve,
    )

    return await _ensure_clock_right(coordinator)


def _parse_args() -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", type=float, default=10.0, help="scan seconds")
    parser.add_argument("--watch", type=float, default=0.0, help="follow N seconds")
    parser.add_argument("--debug", action="store_true", help="integration debug log")
    parser.add_argument(
        "--dst",
        action="store_true",
        help="settle the DST convention: does the lamp shift its own clock?",
    )
    parser.add_argument(
        "--curve",
        action="store_true",
        help="isolate what makes the lamp report 0x34: a quiet window, a write "
        "unrelated to time, then the clock, then the date",
    )
    parser.add_argument(
        "--clock",
        action="store_true",
        help="put the clock resync on trial: leave a correct clock alone, "
        "correct a wrong one, and hand the lamp back right either way",
    )
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

    # Before the connect: start_notify is handed a bound method, so a tap
    # installed afterwards would never be called.
    tap = _Tap(coordinator) if args.curve else None

    print(f"\nconnecting to {device.name} ({_address(device.address)})…")
    try:
        async with coordinator._lock:  # noqa: SLF001
            # Neither probe primes: priming corrects a drift and asks for a
            # batch of properties, either of which would spend the measurement
            # before it was taken.
            prime = not (args.clock or args.curve)
            await coordinator._connect_locked(prime=prime)  # noqa: SLF001
    except Exception as err:
        print(f"connect failed: {err!r}")
        return 1
    if tap is not None:
        tap.start()

    clock_ok = True
    if args.clock:
        clock_ok = await _probe_clock(coordinator)

    if tap is not None:
        clock_ok = await _probe_curve(coordinator, tap) and clock_ok

    # Ask the characteristic directly rather than inferring from the mirror.
    # Two earlier attempts to infer it were both wrong: the total after priming
    # counts what the request supplied, and "the first frame ingested" can be a
    # notification the lamp pushed on subscribe, not the read at all.
    from_read = await _read_directly(coordinator)
    _report(coordinator, from_read)

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

    if args.dst:
        await _probe_dst(coordinator)

    client = coordinator._client  # noqa: SLF001
    if client is not None:
        await client.disconnect()
        print("\ndisconnected")
    return 0 if clock_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
