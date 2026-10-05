"""Tests for the minimal CBOR codec (validated against real device bytes)."""

import random

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
    coords = {0x0A: 12.3456, 0x0B: 65.4321}
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


@pytest.mark.parametrize(
    "frame",
    [
        "a18000",  # an empty array as the key
        "a1a000",  # an empty map as the key
        "ada200",  # a map that ran out, standing in as a key
        "a1f500",  # true
        "a1f600",  # null
        "a1616100",  # the text "a"
        "a1410000",  # one byte
        "a1fb3ff000000000000000",  # 1.0
        "a12000",  # -1: the protocol's ids are not negative either
    ],
)
def test_a_map_keyed_by_anything_but_a_property_id_is_malformed(frame: str) -> None:
    """A map key is a property id, and a property id is an unsigned integer.

    Nothing else is a frame from this lamp. It used to be decoded all the same,
    and a key that cannot be hashed - an array, another map - left the decoder
    as a TypeError, which is not what its caller catches: three bytes off the
    air ended the notification callback in a traceback.
    """
    with pytest.raises(ValueError, match="map key"):
        cbor.decode(bytes.fromhex(frame))
    with pytest.raises(ValueError, match="map key"):
        cbor.decode_frame(bytes.fromhex(frame))


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param("a100" * 250 + "00", id="250 maps, each the value of the last"),
        pytest.param("81" * 250 + "00", id="250 arrays"),
        pytest.param("81" * 499 + "00", id="499 arrays: all a frame has room for"),
    ],
)
def test_a_frame_nested_deeper_than_any_real_one_is_malformed(frame: str) -> None:
    """Nesting is bounded, so a frame cannot be built to exhaust the stack.

    The lamp sends a flat map. The decoder recurses for every level, and an
    attribute value has room for five hundred of them. As released, it was a
    map nested as a key that went too deep - one byte a level - and 498 of
    them ended in a RecursionError, again something the caller does not
    catch. A key is a property id now, which shuts that way in. The bound
    shuts the other: without it the last frame here is deeper than Python
    lets this decoder go, where the first two would merely decode.
    """
    deep = bytes.fromhex(frame)
    with pytest.raises(ValueError, match="nested"):
        cbor.decode(deep)
    with pytest.raises(ValueError, match="nested"):
        cbor.decode_frame(deep)


def test_modest_nesting_still_decodes() -> None:
    """The bound is on depth, not on containers as such."""
    nested = {1: [{2: [3, 4]}, 5]}  # four containers deep
    assert cbor.decode(cbor.encode(nested)) == nested
    with pytest.raises(ValueError, match="nested"):
        cbor.decode(cbor.encode({1: [{2: [[3], 4]}, 5]}))  # five


def test_containers_side_by_side_are_not_containers_inside_one_another() -> None:
    """Depth comes back down on the way out of a container.

    Five arrays in one flat map are two levels deep, however many of them
    there are.
    """
    flat = {1: [], 2: [], 3: [], 4: [], 5: []}
    assert cbor.decode(cbor.encode(flat)) == flat
    assert cbor.decode_frame(bytes.fromhex("a501800280038004800580")) == (flat, False)


@pytest.mark.parametrize(
    "frame",
    [
        "a206f508c0",  # the second value is a tag, which is not supported
        "a206f5c000",  # the second key is
        "a206f5081c",  # the second value has a length this codec does not read
    ],
)
def test_only_the_end_of_the_buffer_makes_a_map_short(frame: str) -> None:
    """A pair that is malformed is not a pair that did not arrive.

    Tolerating a split map means accepting a buffer that ends early. It used
    to mean accepting any failure inside a pair, so a frame with one good pair
    and then an item nothing here can read decoded to that one pair and was
    passed off as a map that had been split.
    """
    with pytest.raises(ValueError, match="unsupported"):
        cbor.decode_frame(bytes.fromhex(frame))


@pytest.mark.parametrize(
    ("frame", "ahead"),
    [
        ("a206f508c0", {0x06: True}),  # the second value is a tag
        ("a206f5c000", {0x06: True}),  # the second key is
        ("a206f5081c", {0x06: True}),  # a length this codec does not read
        ("a308c006f50919", {}),  # the first value already
        ("a306f508a101c00919", {0x06: True}),  # inside a nested map: that pair whole
        ("a206f508818181818100", {0x06: True}),  # nested deeper than any frame is
        ("a18000", {}),  # keyed by an array
    ],
)
def test_an_item_that_cannot_be_read_says_what_was_read_ahead_of_it(
    frame: str, ahead: dict[int, object]
) -> None:
    """The frame is malformed, and the error hands over the pairs before the item.

    An item with no reading here has no length either, so nothing behind it
    can be found. What came ahead of it was read exactly as it would have
    been from a whole frame, and whether that is worth keeping is for the
    caller to say - which it cannot, unless it is told what there was. The
    error carries it, the way a read that was cut short carries what it got.
    """
    with pytest.raises(cbor.UnreadableItemError) as err:
        cbor.decode_frame(bytes.fromhex(frame))
    assert err.value.ahead == ahead
    assert isinstance(err.value, ValueError)
    assert not isinstance(err.value, cbor.TrailingBytesError)


@pytest.mark.parametrize("frame", ["c000", "8206c0", "81a206f508c0"])
def test_only_the_properties_of_a_device_frame_are_handed_over(frame: str) -> None:
    """What is ahead of an item means something only in the outermost map.

    A frame that is not a map has no properties to keep, and a strict decode
    is of something this integration encoded itself.
    """
    with pytest.raises(ValueError, match="unsupported") as err:
        cbor.decode_frame(bytes.fromhex(frame))
    assert not isinstance(err.value, cbor.UnreadableItemError)

    with pytest.raises(ValueError, match="unsupported") as err:
        cbor.decode(bytes.fromhex("a206f508c0"))
    assert not isinstance(err.value, cbor.UnreadableItemError)


def test_a_map_cut_short_inside_a_value_drops_that_pair_whole() -> None:
    """Only the outermost map may end early; what is inside it is all or nothing.

    A nested map that ran out used to hand back what it had and rewind, and the
    map around it carried on from the rewound position, reading the same bytes
    a second time as something else.
    """
    # {1: {2: 3, <the buffer ends here>
    value, short = cbor.decode_frame(bytes.fromhex("a201a20203"))
    assert short is True
    assert value == {}  # the only pair that started never finished

    # {6: True, 1: {2: 3, <the buffer ends here>
    value, short = cbor.decode_frame(bytes.fromhex("a306f501a20203"))
    assert short is True
    assert value == {0x06: True}


def test_a_value_cut_off_by_the_end_of_the_buffer_is_a_pair_that_did_not_arrive() -> (
    None
):
    """Running out in the middle of a value is still running out.

    A frame can end inside a byte string as easily as between two pairs. The
    pairs before it arrived whole, and are kept.
    """
    # {6: True, 0x11: <eleven bytes promised, two delivered>
    value, short = cbor.decode_frame(bytes.fromhex("a206f5114b0102"))
    assert short is True
    assert value == {0x06: True}

    # The same inside a length that was itself cut off.
    value, short = cbor.decode_frame(bytes.fromhex("a206f51159"))
    assert short is True
    assert value == {0x06: True}


def test_nothing_but_a_valueerror_leaves_the_frame_decoder() -> None:
    """Whatever arrives, the decoder either decodes it or raises ValueError.

    That is the whole of what its caller has to know. The corpus is the kind
    of thing a radio can deliver: noise, noise leaning towards containers and
    long lengths, a real frame with bytes flipped, cut and inserted, and one
    container nested as deep as a frame has room for.
    """
    rng = random.Random(20261005)
    real = cbor.encode(
        {
            0x05: bytes.fromhex("07ea0a05080f1e"),
            0x06: True,
            0x08: 70,
            0x0A: 12.34,
            0x11: bytes.fromhex("0100000006001200640000"),
            0x2B: 1,
            0x35: bytes.fromhex("0100000e10"),
        }
    )
    heads = bytes(range(0x80, 0xC0)) + bytes([0x9B, 0xBB, 0x5B, 0x7B, 0xF9, 0xFB])

    def frame() -> bytes:
        kind = rng.randrange(4)
        if kind == 0:
            return rng.randbytes(rng.randrange(0, 301))
        if kind == 1:
            return bytes(
                rng.choice(heads) if rng.random() < 0.5 else rng.randrange(256)
                for _ in range(rng.randrange(0, 301))
            )
        if kind == 2:
            mutated = bytearray(real)
            for _ in range(rng.randrange(1, 6)):
                if not mutated:
                    break
                at = rng.randrange(len(mutated))
                op = rng.randrange(3)
                if op == 0:
                    mutated[at] = rng.randrange(256)
                elif op == 1:
                    del mutated[at:]
                else:
                    mutated[at:at] = rng.randbytes(rng.randrange(1, 9))
            return bytes(mutated)
        head = rng.choice([b"\x81", b"\xa1\x00", b"\xa2\x00", b"\x9f", b"\xbf"])
        return head * rng.randrange(1, 256) + rng.randbytes(rng.randrange(0, 4))

    decoded = 0
    for _ in range(20_000):
        data = frame()
        try:
            cbor.decode_frame(data)
        except ValueError:
            continue
        decoded += 1
    assert decoded > 1_000  # the corpus is not all rejects: some of it is valid
