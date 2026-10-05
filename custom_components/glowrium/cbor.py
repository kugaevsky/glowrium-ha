"""Minimal CBOR codec for the Glowrium BLE property protocol.

Only the subset the device uses is implemented: maps keyed by unsigned ints,
unsigned/negative ints, byte strings, text strings, arrays, booleans, null and
IEEE-754 single/double floats. Validated against the live device.

What it decodes comes off a radio, so the decoder promises its caller one
thing: it returns a value or raises ``ValueError``, whatever the bytes. A key
that is not a property id, a container nested deeper than any real frame, an
item this subset has no reading for - each is a malformed frame, not an
exception of its own kind.
"""

from __future__ import annotations

import struct
from typing import Any

_LEN_BYTES = {24: 1, 25: 2, 26: 4, 27: 8}

# The lamp sends a flat map. The bound is there for a frame built to recurse:
# five hundred maps inside one another fit in a single attribute value.
_MAX_DEPTH = 4


class _RanOutError(ValueError):
    """The buffer ended before the item did.

    Told apart from every other way a frame can be malformed, because it is
    the one a split map is allowed (see ``_Decoder._map``).
    """


class TrailingBytesError(ValueError):
    """A frame carried more bytes than the item it declared.

    Its own type, rather than a plain ``ValueError``, so a caller can tell this
    apart from a frame that is simply malformed: on a device whose frames were
    always fully consumed before, trailing bytes mean something changed and are
    worth reporting as themselves. Subclasses ``ValueError`` so callers that
    only care that decoding failed need no change.
    """

    def __init__(self, count: int) -> None:
        """Record how many bytes were left over."""
        super().__init__(f"{count} trailing bytes")
        self.count = count


class _Decoder:
    def __init__(self, data: bytes, *, tolerant: bool = False) -> None:
        self._b = data
        self._i = 0
        self._depth = 0
        # Device frames may end part-way through a map (see _map). Only the
        # device path opts into tolerating that; everything else stays strict,
        # so a short map in our own encoded payloads is still an error.
        self._tolerant = tolerant
        self.short = False

    def read(self) -> Any:
        ib = self._byte()
        major, ai = ib >> 5, ib & 0x1F
        if major == 0:
            return self._uint(ai)
        if major == 1:
            return -1 - self._uint(ai)
        if major == 2:
            return self._take(self._uint(ai))
        if major == 3:
            return self._take(self._uint(ai)).decode("utf-8", "replace")
        if major in (4, 5):
            return self._container(major, self._uint(ai))
        if major == 7:
            return self._simple(ai)
        raise ValueError(f"unsupported CBOR major type {major}")

    def _container(self, major: int, count: int) -> Any:
        """Decode an array or a map, one level further in."""
        if self._depth == _MAX_DEPTH:
            raise ValueError(f"CBOR nested deeper than {_MAX_DEPTH} levels")
        self._depth += 1
        try:
            if major == 4:
                return [self.read() for _ in range(count)]
            return self._map(count)
        finally:
            self._depth -= 1

    def _map(self, pairs: int) -> dict[int, Any]:
        """Decode a map of ``pairs`` key/value pairs.

        When ``tolerant`` (device frames only), a buffer that ends part-way
        through the map yields the pairs that did arrive, flagged via ``short``.
        A device can split a long property map across notifications: the first
        frame carries a header promising N pairs but contains only some of them,
        and treating that as garbage discards every property that *did* arrive.

        That is all it tolerates. Only the buffer ending counts - a pair that is
        there and malformed is a malformed frame - and only for the outermost
        map: one nested inside a value is all or nothing, so the pair it sits in
        is dropped whole rather than kept with half a value.

        When not tolerant the shortfall raises, so a truncated map in anything
        we encoded ourselves is still surfaced as the bug it is.
        """
        out: dict[int, Any] = {}
        tolerant = self._tolerant and self._depth == 1
        for _ in range(pairs):
            if not tolerant:
                # Two statements, not out[self._key()] = self.read(): Python
                # evaluates an assignment's right-hand side before its target,
                # which would read the pair value-first.
                key = self._key()
                out[key] = self.read()
                continue
            start = self._i
            try:
                key = self._key()
                value = self.read()
            except _RanOutError:
                self._i = start
                self.short = True
                break
            out[key] = value
        return out

    def _key(self) -> int:
        """Read a map key, which in this protocol is a property id.

        An unsigned integer and nothing else. Whatever else CBOR allows there
        is not a frame from this lamp, and some of it - an array, another map -
        cannot be a dictionary key at all.
        """
        ib = self._byte()
        if ib >> 5 != 0:
            raise ValueError(f"unsupported CBOR map key of major type {ib >> 5}")
        return self._uint(ib & 0x1F)

    def expect_consumed(self) -> None:
        """Raise if the buffer holds more than the item just decoded."""
        if self._i != len(self._b):
            raise TrailingBytesError(len(self._b) - self._i)

    def _byte(self) -> int:
        if self._i >= len(self._b):
            raise _RanOutError("truncated CBOR item")
        v = self._b[self._i]
        self._i += 1
        return v

    def _take(self, n: int) -> bytes:
        v = self._b[self._i : self._i + n]
        if len(v) != n:
            raise _RanOutError("truncated CBOR value")
        self._i += n
        return v

    def _uint(self, ai: int) -> int:
        if ai < 24:
            return ai
        if ai not in _LEN_BYTES:
            raise ValueError(f"unsupported CBOR length {ai}")
        return int.from_bytes(self._take(_LEN_BYTES[ai]), "big")

    def _simple(self, ai: int) -> Any:
        if ai == 20:
            return False
        if ai == 21:
            return True
        if ai == 22:
            return None
        if ai == 26:
            return struct.unpack(">f", self._take(4))[0]
        if ai == 27:
            return struct.unpack(">d", self._take(8))[0]
        raise ValueError(f"unsupported CBOR simple value {ai}")


def decode(data: bytes) -> Any:
    """Decode a single CBOR item from ``data``, or raise ``ValueError``.

    Trailing bytes are an error: a frame that carries more than the item it
    declares is malformed, and silently ignoring the remainder lets a corrupt
    frame decode to a short, plausible-looking map.
    """
    dec = _Decoder(data)
    value = dec.read()
    dec.expect_consumed()
    return value


def decode_frame(data: bytes) -> tuple[Any, bool]:
    """Decode ``data``, also reporting whether a map ended part-way.

    ``(value, short)``. Use this for device frames, where a split map should
    yield the properties it carried rather than nothing at all. Anything it
    cannot decode raises ``ValueError``, and nothing else does.
    """
    dec = _Decoder(data, tolerant=True)
    value = dec.read()
    if not dec.short:
        # A short map legitimately leaves the incomplete pair unread; anything
        # else with bytes to spare is malformed.
        dec.expect_consumed()
    return value, dec.short


def _head(major: int, n: int) -> bytes:
    base = major << 5
    if n < 24:
        return bytes([base | n])
    if n < 0x100:
        return bytes([base | 24, n])
    if n < 0x10000:
        return bytes([base | 25]) + n.to_bytes(2, "big")
    if n < 0x1_0000_0000:
        return bytes([base | 26]) + n.to_bytes(4, "big")
    return bytes([base | 27]) + n.to_bytes(8, "big")


def encode(obj: Any) -> bytes:
    """Encode ``obj`` to CBOR (the subset used for device commands)."""
    if isinstance(obj, bool):  # must precede int - bool is a subclass of int
        return b"\xf5" if obj else b"\xf4"
    if isinstance(obj, int):
        return _head(0, obj) if obj >= 0 else _head(1, -1 - obj)
    if isinstance(obj, float):
        return b"\xfb" + struct.pack(">d", obj)
    if isinstance(obj, (bytes, bytearray)):
        return _head(2, len(obj)) + bytes(obj)
    if isinstance(obj, str):
        encoded = obj.encode("utf-8")
        return _head(3, len(encoded)) + encoded
    if isinstance(obj, dict):
        out = bytearray(_head(5, len(obj)))
        for key, value in obj.items():
            out += encode(key) + encode(value)
        return bytes(out)
    if isinstance(obj, (list, tuple)):
        out = bytearray(_head(4, len(obj)))
        for value in obj:
            out += encode(value)
        return bytes(out)
    raise TypeError(f"cannot CBOR-encode {type(obj).__name__}")
