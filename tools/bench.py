"""Run the real coordinator against a real lamp, from this machine.

A close-range counterpart to the Home Assistant instance on the server, which
sits at the edge of the lamp's range and is therefore only good for testing how
things degrade. Bring the laptop near the lamp and this exercises the same code
on a link that actually works - which is the only way to answer questions like
"does the connect-time read carry every key" without guessing.

    .venv/bin/python tools/bench.py            # connect, prime, report
    .venv/bin/python tools/bench.py --watch 5  # ...then follow notifications

It never writes a setting. The bring-up sequence is disabled outright rather
than relied upon not to trigger: a bench should not be able to reprovision
somebody's lamp because a flag read back wrong.

Docker on macOS cannot reach the host's Bluetooth controller, so this runs
directly on the machine. The address here is a CoreBluetooth UUID, not a MAC.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakScanner

from custom_components.glowrium.const import (
    NAME_PREFIX,
    STATE_KEYS,
)
from custom_components.glowrium.coordinator import GlowriumCoordinator

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
    0x35: "dst",
}


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
        print(f"  {address}  RSSI={rssi}")
    return best


def _report(coordinator: GlowriumCoordinator, from_read: set[int] | None) -> None:
    """Print what the lamp has told us, and what it has not."""
    state = coordinator.state
    print(f"\ndevice-info: {coordinator.device_info or '(none)'}")
    print(f"keys reported: {len(state)}")
    for key in sorted(state):
        raw = state[key]
        shown = raw.hex() if isinstance(raw, (bytes, bytearray)) else raw
        print(f"  0x{key:02x} {_KEY_NAMES.get(key, '?'):<14} {shown}")
    if from_read is not None:
        gap = [k for k in STATE_KEYS if k not in from_read]
        print(f"\nthe connect-time read alone carried {len(from_read)} keys")
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


async def main() -> int:
    """Connect once, prime, report, and optionally follow notifications."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", type=float, default=10.0, help="scan seconds")
    parser.add_argument("--watch", type=float, default=0.0, help="follow N seconds")
    parser.add_argument("--debug", action="store_true", help="integration debug log")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    device = await _find(args.scan)
    if device is None:
        print("no Glowrium advertising - move closer, or check the vendor app is off")
        return 1

    coordinator = GlowriumCoordinator(None, device.address, device.name)  # type: ignore[arg-type]
    coordinator._ble_device = lambda: device  # noqa: SLF001
    # The bench never provisions anything. Saying so here beats trusting that
    # 0x14 reads back the way we expect.
    coordinator._activation_checked = True  # noqa: SLF001

    # Record what the READ alone returns, before the batched request fills in
    # the rest. Reporting only the total is how this bench misled its author:
    # 24 keys after priming looks like "the read carries everything", when the
    # read had returned 20 and the request supplied the other four.
    from_read: set[int] = set()
    original_ingest = coordinator._ingest  # noqa: SLF001
    first = True

    def _watch_first(data: bytes) -> None:
        nonlocal first
        original_ingest(data)
        if first:
            first = False
            from_read.update(coordinator.state)

    coordinator._ingest = _watch_first  # noqa: SLF001

    print(f"\nconnecting to {device.name} ({device.address})…")
    try:
        async with coordinator._lock:  # noqa: SLF001
            await coordinator._connect_locked()  # noqa: SLF001
    except Exception as err:
        print(f"connect failed: {err!r}")
        return 1

    _report(coordinator, from_read)

    if args.watch:
        print(f"\nfollowing notifications for {args.watch:.0f}s…")
        before = dict(coordinator.state)
        await asyncio.sleep(args.watch)
        changed = {k: v for k, v in coordinator.state.items() if before.get(k) != v}
        print(f"changed while watching: {len(changed)}")
        for key, raw in sorted(changed.items()):
            shown = raw.hex() if isinstance(raw, (bytes, bytearray)) else raw
            print(f"  0x{key:02x} {_KEY_NAMES.get(key, '?'):<14} {shown}")

    client = coordinator._client  # noqa: SLF001
    if client is not None:
        await client.disconnect()
        print("\ndisconnected")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
