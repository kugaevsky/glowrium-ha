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
