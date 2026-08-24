"""Tests for the minimal CBOR codec (validated against real device bytes)."""

import pytest

from custom_components.glowrium import cbor
from custom_components.glowrium.const import (
    KEY_BRIGHTNESS,
    KEY_LIGHTING_MODE,
    KEY_POWER,
)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({KEY_POWER: True}, "a106f5"),
        ({KEY_POWER: False}, "a106f4"),
        ({KEY_BRIGHTNESS: 100}, "a1081864"),
        ({KEY_BRIGHTNESS: 50}, "a1081832"),
        ({KEY_BRIGHTNESS: 25}, "a1081819"),
    ],
)
def test_encode_commands(payload: dict[int, object], expected: str) -> None:
    """Commands encode to the exact bytes observed on the wire."""
    assert cbor.encode(payload).hex() == expected


def test_encode_lighting_mode_matches_capture() -> None:
    """A lighting-mode command encodes to the captured frame."""
    payload = {
        KEY_LIGHTING_MODE: 5,
        0x2C: bytes.fromhex("02d0"),
        0x2F: bytes.fromhex("0e10"),
        0x32: bytes.fromhex("001e"),
    }
    assert cbor.encode(payload).hex() == "a4182b05182c4202d0182f420e10183242001e"


def test_decode_bool_map() -> None:
    """A notification decodes to a {key: value} map."""
    assert cbor.decode(bytes.fromhex("a106f5")) == {KEY_POWER: True}


def test_decode_byte_string() -> None:
    """Byte-string values (e.g. the DST struct) decode to raw bytes."""
    assert cbor.decode(bytes.fromhex("a11835450100000e10")) == {
        0x35: bytes.fromhex("0100000e10")
    }


def test_decode_float64() -> None:
    """float64 values decode to floats."""
    decoded = cbor.decode(bytes.fromhex("a10afb4043747ae147ae14"))
    assert round(decoded[0x0A], 2) == 38.91


def test_encode_float64() -> None:
    """float64 coordinates encode with the 0xfb prefix and round-trip."""
    coords = {0x0A: 41.3166, 0x0B: 69.2906}
    encoded = cbor.encode(coords)
    assert encoded[0] == 0xA2  # map with 2 pairs
    assert encoded[2:3] == b"\xfb"  # first value is a float64
    assert cbor.decode(encoded) == coords


@pytest.mark.parametrize(
    "payload",
    [
        {KEY_POWER: True},
        {KEY_BRIGHTNESS: 73},
        {KEY_LIGHTING_MODE: 9},
        {
            KEY_LIGHTING_MODE: 9,
            0x2C: bytes.fromhex("02d0"),
            0x2F: bytes.fromhex("0708"),
            0x32: bytes.fromhex("001e"),
        },
    ],
)
def test_round_trip(payload: dict[int, object]) -> None:
    """Round-trip through encode/decode returns the original mapping."""
    assert cbor.decode(cbor.encode(payload)) == payload


def test_map_split_across_frames_keeps_what_arrived() -> None:
    """A map header promising more pairs than the frame carries is not fatal.

    Real capture from a Glowrium G8: the device split its property map, so the
    first notification declared 12 pairs and contained 11. Discarding the frame
    threw away every property that did arrive.
    """
    frame = bytes.fromhex(
        "ac06f508184609f40afb4043747ae147ae140bfb40534147ae147ae10df411"
        "4b010200ff0a12121264000014f517f5182b01182f420000"
    )
    value, short = cbor.decode_frame(frame)
    assert short is True
    assert value[0x06] is True  # power
    assert value[0x08] == 70  # brightness
    assert value[0x14] is True  # activated
    assert value[0x2B] == 1  # lighting mode
    assert len(value) == 11  # 11 of the promised 12


def test_complete_map_is_not_flagged_short() -> None:
    """A frame that carries every promised pair decodes with short=False."""
    value, short = cbor.decode_frame(bytes.fromhex("a206f5081819"))
    assert short is False
    assert value == {0x06: True, 0x08: 25}


def test_plain_decode_stays_strict_on_a_short_map() -> None:
    """decode() is not tolerant: only device frames opt into that.

    A truncated map in something we encoded ourselves is a real bug and must not
    be silently downgraded to partial data.
    """
    with pytest.raises((ValueError, IndexError)):
        cbor.decode(bytes.fromhex("a306f508"))


def test_trailing_bytes_are_rejected() -> None:
    """A frame carrying more than its declared item is malformed.

    Accepting the remainder would let a corrupt frame decode to a short,
    plausible map - including {0x14: False}, the one value that triggers the
    device bring-up sequence.
    """
    with pytest.raises(ValueError, match="trailing bytes"):
        cbor.decode(bytes.fromhex("a114f4081832"))
    with pytest.raises(ValueError, match="trailing bytes"):
        cbor.decode_frame(bytes.fromhex("a106f5deadbeef"))


def test_trailing_bytes_raise_their_own_type() -> None:
    """Trailing bytes are distinguishable from a merely malformed frame.

    The coordinator reports them separately, because on a model that never
    produced them before they mean something changed. Still a ValueError, so
    callers that only care that decoding failed keep working.
    """
    with pytest.raises(cbor.TrailingBytesError) as err:
        cbor.decode_frame(bytes.fromhex("a106f5deadbeef"))
    assert err.value.count == 4
    assert isinstance(err.value, ValueError)

    # A truncated frame is NOT this error - nothing was left over.
    with pytest.raises((ValueError, IndexError)) as other:
        cbor.decode(bytes.fromhex("a306f508"))
    assert not isinstance(other.value, cbor.TrailingBytesError)
