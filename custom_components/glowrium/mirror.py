"""What the lamp has said, frame by frame; and what was written to it.

The mirror of the lamp's state: a read-only mapping of property id to value,
filled from the frames the lamp notifies and from the echo of what was
written to it, and never emptied. What it holds is bounded all the same: the
ids the integration knows, and a fixed number of the ids it does not - the
device chooses what it reports, and whatever answers at the lamp's address
is taken for the lamp. It knows how to read a frame (``cbor``)
and what in one gives the lamp's place away, and nothing of Home Assistant,
of the link, or of the lamp's protocol beyond that.

Two ways in and none out: ``take`` for a frame from the lamp, ``echo`` for a
command the lamp acknowledged. What a caller then does with the news - tell
the entities, note that the lamp answered - is the caller's: the mirror has
no initiative, so whoever calls it is on the stack and acts on the return
value.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Iterator, Mapping
from datetime import datetime
import logging
from typing import Any, Final

from . import cbor
from .const import KEY_CURVE, KEY_LATITUDE, KEY_LONGITUDE, KEY_TIME

# The mirror's lines go out under the coordinator's name, as the link's do: the
# log filters, and the tests' caplog, were set up on that name before either
# module existed.
_LOGGER = logging.getLogger(f"{__package__}.coordinator")

# How many ids the integration has no name for are kept. The device chooses
# the ids it reports, and whatever answers at the lamp's address is taken for
# the lamp - the protocol has no pairing - so without a limit a device that
# sends new ids in every frame grows the mirror for as long as it is let: by
# up to 32 KiB a frame, measured (2026-10-09). A G7 that answers the state
# request reports no such id; a read of the state, on a G7 or a G8, brings
# eleven.
_OTHERS_KEPT: Final = 64
# Said where the log tells of a frame it could not use in full: where the
# frame is, and what was done to it there (see _for_the_log). No line above
# debug prints a frame: its bytes are the lamp's to choose, and the default
# log is posted for reasons that have nothing to do with this integration.
_IN_THE_DEBUG_LOG = (
    "With debug logging enabled for this integration the frame is in the log, "
    "where what reads as the coordinates stored in the lamp, or as the sunrise "
    "and sunset times it works out from them, is shown as xx; look the frame "
    "over all the same before posting it"
)
# The ids of what places the lamp, each looked for by its last byte. An id
# above 23 is written in two bytes or more - 18 34, 19 00 34 and longer - and a
# decoder takes them all for the same id; the last byte is the one they share.
_PLACE = frozenset({KEY_LATITUDE, KEY_LONGITUDE, KEY_CURVE})
# How many bytes follow the head of a CBOR float: double, single, half.
_FLOAT_BYTES = {b"\xfb": 8, b"\xfa": 4, b"\xf9": 2}
# The head of a CBOR item (RFC 8949, 3). Its three high bits say what the item
# is. Its five low bits are its argument - a tag's number, a string's length -
# when that is up to 23; 24 to 27 put the argument in the next one, two, four
# or eight bytes. 31 in a string's head makes its length indefinite: pieces,
# each a string with a length of its own, up to a break.
_TAG = 6
_BYTE_STRING = 2
_ARGUMENT_IN_THE_HEAD = range(24)
_ARGUMENT_BYTES = {24: 1, 25: 2, 26: 4, 27: 8}
_PIECES = b"\x5f"
_BREAK = b"\xff"
# How many tags, and how many pieces of a string, are stepped over from one
# offset. Every offset of a frame is looked at on its own, so a walk that
# could run to the end of the frame from each of them made rendering a frame
# built for it quadratic: 4 ms for 512 bytes where an ordinary frame takes a
# quarter of one, measured (2026-10-09). No lamp has been seen to write a tag,
# or a string in pieces, at all. Past the limit what is left of the frame is
# blanked: a false match can only blank more.
_STEPS: Final = 4


def _head_of(kind: int, frame: bytes, at: int) -> tuple[int, int]:
    """Read the head of an item of ``kind`` at ``at``: its bytes, and its argument.

    ``(0, 0)`` when no such head stands there whole - an item of another
    kind, the end of the frame, a string of indefinite length.
    """
    if at >= len(frame) or frame[at] >> 5 != kind:
        return 0, 0
    argument = frame[at] & 0x1F
    if argument in _ARGUMENT_IN_THE_HEAD:
        return 1, argument
    more = _ARGUMENT_BYTES.get(argument, 0)
    following = frame[at + 1 : at + 1 + more]
    if not more or len(following) < more:
        return 0, 0
    return 1 + more, int.from_bytes(following, "big")


def _past_tags(frame: bytes, at: int) -> int | None:
    """Return where the item at ``at`` begins once the tags on it are stepped over.

    A tag wraps the item that follows it, and a decoder that does not read
    tags stops there: the value behind one is then in a frame that gets
    printed, with nothing between its id and its head but the tag. ``None``
    when more tags stand there than are stepped over.
    """
    for _ in range(_STEPS + 1):
        head, _number = _head_of(_TAG, frame, at)
        if not head:
            return at
        at += head
    return None


def _byte_string_at(frame: bytes, at: int) -> tuple[int, int]:
    """Size the byte string that begins at ``at``: its head, and its value.

    ``(0, 0)`` when none begins there. One of indefinite length runs up to
    its break; to the end of the frame when no break is found within
    ``_STEPS`` pieces, or what stands where a piece should is not one - a
    string in pieces among them, so that nothing here calls itself. The
    pieces are stepped over by their lengths: a byte of a piece that reads as
    the break is not taken for it.
    """
    if frame[at : at + 1] != _PIECES:
        return _head_of(_BYTE_STRING, frame, at)
    end = at + 1
    for _ in range(_STEPS + 1):
        if frame[end : end + 1] == _BREAK:
            return 1, end - at - 1
        head, length = _head_of(_BYTE_STRING, frame, end)
        if not head:
            break
        end += head + length
    return 1, len(frame) - at - 1


def _private_at(frame: bytes, at: int) -> tuple[int, int]:
    """Tell whether a value that says where the lamp is starts at ``at``.

    ``(head, value)``: how many bytes name it - the id, any tags and the head
    of the value - and how many the value itself takes: none, when nothing
    of the kind starts here.
    """
    if frame[at] not in _PLACE:
        return 1, 0
    value_at = _past_tags(frame, at + 1)
    if value_at is None:
        return 1, len(frame)
    if frame[at] == KEY_CURVE:
        head, value = _byte_string_at(frame, value_at)
    else:
        head, value = 1, _FLOAT_BYTES.get(frame[value_at : value_at + 1], 0)
    return value_at - at + head, value


def _for_the_log(frame: bytes) -> str:
    """Return ``frame`` as hex, with what says where the lamp is put down as xx.

    A frame goes into the log so that it can be posted, and a frame can hold
    the coordinates the lamp was given and the sunrise and sunset times it
    works out from them, which give the place away as well. Asking whoever
    posts it to blank hex by hand is asking for it to be forgotten.

    The frame being logged is one that could not be read to its end, so the
    values are not found by decoding it: they are looked for by their bytes -
    an id and the head of its value. A match that was something else costs a
    few bytes of the dump. Everything else stays as it is and where it is,
    which is what makes the dump worth having.

    Every offset is looked at, whatever was found before it. Skipping past a
    value once it is found would be the natural way to walk a frame, and a
    false match would then carry the search over the id of a real one - whose
    value would be printed whole. Looked at one by one, a false match can
    only blank more.
    """
    hidden = bytearray(len(frame))
    for at in range(len(frame)):
        head, value = _private_at(frame, at)
        start = at + head
        hidden[start : start + value] = b"\x01" * len(hidden[start : start + value])
    return "".join(
        "xx" if blank else f"{byte:02x}"
        for byte, blank in zip(frame, hidden, strict=True)
    )


class _Printed:
    """A frame as it is printed, worked out only if the line is written.

    Rendering a frame looks at every byte of it, and a line below the level
    the log is kept at is never written. The logger is handed this and not
    the string, so that a frame nobody will read is not rendered.
    """

    __slots__ = ("_frame",)

    def __init__(self, frame: bytes) -> None:
        """Remember the frame; nothing is rendered yet."""
        self._frame = frame

    def __str__(self) -> str:
        """Render the frame, blanked (``_for_the_log``)."""
        return _for_the_log(self._frame)


class Mirror(Mapping[int, Any]):
    """What the lamp has said, by property id; and what was written to it.

    A mapping to read, never to write: a value gets in through ``take`` (a
    frame the lamp sent) or ``echo`` (a command it acknowledged), and nothing
    is ever taken out - the mirror is not emptied when a link drops, so what
    it holds can be hours old, which is why the clock in it is dated.

    The ids the integration knows are always kept, and so is every echo. Of
    the ids it has no name for, the first ``_OTHERS_KEPT`` a session brings
    are kept and none of them is ever dropped to make room; a property
    beyond that is not stored, and counted each time it is reported
    (``not_kept``).

    A report is a frame of which at least one property was kept. They are
    numbered: ``reports`` is how many there have been, and
    ``reported_since(n)`` the ids the reports numbered above ``n`` carried -
    what priming compares with the keys it asked for, and what vouching for
    a failed write compares with what the write set. An echo is no report:
    it moves no number and wakes nobody.
    """

    def __init__(
        self,
        address: str,
        *,
        known: Collection[int],
        described: Callable[[], str],
        now: Callable[[], datetime],
    ) -> None:
        """Build an empty mirror of the lamp at ``address``.

        ``known`` are the ids the integration has a name for - what it asks
        the lamp for, reads or writes. They are always kept; of other ids
        only ``_OTHERS_KEPT``. The mirror is told them and knows nothing else
        of the protocol.

        ``described`` says what the lamp is - its model and firmware - for the
        warnings that ask for a report; it is asked when a warning is
        written, since the lamp describes itself only after its first
        frames. ``now`` is the host's clock, for dating the lamp's.
        """
        self._address = address
        self._known = frozenset(known)
        self._described = described
        self._now = now
        self._values: dict[int, Any] = {}
        # How many ids nobody has a name for were taken from frames: no more
        # than _OTHERS_KEPT, the first to come. And how many times a reported
        # property was not kept because there was no room left.
        self._others = 0
        self.not_kept = 0
        self._not_kept_warned = False
        # Monotonic counters, not values: vouching needs to know that a report
        # is NEWER than the write it is vouching for, and the mirror alone
        # cannot tell a fresh report from an hours-old one.
        self.reports = 0
        # What ``reports`` stood at when the lamp last reported each id. A
        # report vouches for what it carried, not for everything in the mirror.
        self._reported_at: dict[int, int] = {}
        # The host's clock at the moment the lamp's (0x05) last came in: a
        # clock is right or wrong only against the moment it was read at.
        self.clock_heard_at: datetime | None = None
        # Set once a frame has been rejected for trailing bytes, so the warning
        # is raised once per session instead of on every notification; and the
        # same for a frame that was read only in part.
        self._trailing_warned = False
        self._unreadable_warned = False
        # Whoever is waiting for the next report (next_report).
        self._waiters: list[asyncio.Future[None]] = []

    # --- the mapping ----------------------------------------------------------

    def __getitem__(self, key: int) -> Any:
        """Return the value last reported or echoed under ``key``."""
        return self._values[key]

    def __iter__(self) -> Iterator[int]:
        """Iterate over the ids the mirror holds a value for."""
        return iter(self._values)

    def __len__(self) -> int:
        """Return how many ids the mirror holds a value for."""
        return len(self._values)

    def __repr__(self) -> str:
        """Show the values, as a dict would."""
        return f"Mirror({self._values!r})"

    # --- the two ways in ------------------------------------------------------

    def take(self, frame: bytes) -> frozenset[int]:
        """Take a frame from the lamp; return the ids taken in from it.

        Empty when the frame was no report: nothing in it could be read, or
        it was not a map of properties, or a map with nothing in it, or there
        was room for none of what it carried (``_kept_of``). Whatever
        the bytes, nothing is raised: this runs inside the Bluetooth stack's
        own notify handler, and the decoder's promise that nothing but a
        ``ValueError`` leaves it is kept here.

        A frame with an item the decoder cannot read is kept as far as it was
        read: dropping it would cost more than its properties, since a state
        request answered only by such a frame would count as unanswered, the
        connect would fall back on reading, and on BlueZ a read ends the link.
        A frame of which nothing was read is still no report.
        """
        short = False
        unreadable: cbor.UnreadableItemError | None = None
        try:
            decoded, short = cbor.decode_frame(frame)
        except cbor.UnreadableItemError as err:
            decoded, unreadable = err.ahead, err
        except cbor.TrailingBytesError as err:
            # Reported apart from a merely malformed frame, and loudly the first
            # time: rejecting these is what changed in #5, and on a model whose
            # frames were always fully consumed before, this is the regression
            # that change risks. Buried in "Undecodable frame" at debug level it
            # would never be noticed.
            self._log_trailing_bytes(frame, err.count)
            return frozenset()
        except ValueError as err:  # the decoder raises nothing else
            _LOGGER.debug("Undecodable frame %s: %s", _Printed(frame), err)
            return frozenset()
        carried = decoded if isinstance(decoded, dict) else {}
        kept = self._kept_of(carried)
        if unreadable is not None:
            # Said now that what was kept is known: the line counts it.
            self._log_unreadable_item(frame, unreadable, len(kept))
        elif not carried:
            # Decoded without a fault, and still of no use: every frame that
            # is dropped is named, or nobody can ask what it was.
            _LOGGER.debug(
                "%s: frame %s decodes to nothing that can be used: it is "
                "not a map of properties, or is one with nothing in it",
                self._address,
                _Printed(frame),
            )
        elif not kept:
            # There was room for none of it. Dropped, then, and named too.
            _LOGGER.debug(
                "%s: frame %s carries %d properties and none of them is kept",
                self._address,
                _Printed(frame),
                len(carried),
            )
        if not kept:
            return frozenset()
        if short:
            _LOGGER.debug(
                "%s: property map split across frames; kept %d of them",
                self._address,
                len(kept),
            )
        self._merge(kept)
        self.reports += 1
        self._reported_at.update(dict.fromkeys(kept, self.reports))
        self._wake()
        return frozenset(kept)

    def _kept_of(self, carried: Mapping[int, Any]) -> dict[int, Any]:
        """Return what of a frame's properties is kept, and count what is not.

        An id the integration knows is kept, and so is one the mirror holds
        already. An id nobody named is given one of the places there are for
        such ids, while one is left. What is not kept is counted, and said
        the first time.
        """
        kept: dict[int, Any] = {}
        for key, value in carried.items():
            if key in self._known or key in self._values:
                kept[key] = value
            elif self._others < _OTHERS_KEPT:
                self._others += 1
                kept[key] = value
        if len(kept) < len(carried):
            self._note_not_kept(len(carried) - len(kept))
        return kept

    def _note_not_kept(self, count: int) -> None:
        """Count properties there was no room for, and say so the first time.

        Once a session, as with a frame that cannot be read in full: a device
        that reports more ids than are kept does so in every frame. Nothing
        of the frame goes into the line - the ids and the values in it are
        the device's to choose.
        """
        self.not_kept += count
        if self._not_kept_warned:
            return
        self._not_kept_warned = True
        _LOGGER.warning(
            "%s (%s) reports more properties than this integration keeps: it "
            "keeps the ones it knows and %d others, and counts each time "
            "another is reported. No lamp has been seen to do this. Please "
            "report this model",
            self._address,
            self._described(),
            _OTHERS_KEPT,
        )

    def echo(self, payload: Mapping[int, Any]) -> None:
        """Take what a command set, once the lamp acknowledged the write.

        Our word for it, not the lamp's: no report, no number, nobody woken.
        The lamp reports what changed of its own accord, and a report is what
        vouches for a write whose acknowledgement went missing.
        """
        self._merge(payload)

    # --- what has been reported, and waiting for more -------------------------

    def reported_since(self, report: int) -> frozenset[int]:
        """Return the ids some report numbered above ``report`` carried."""
        return frozenset(key for key, at in self._reported_at.items() if at > report)

    async def next_report(self) -> None:
        """Return once the first report after this call has been taken.

        Never on an echo, and never on a frame that was no report. A caller
        that is cancelled - its deadline is its own - is forgotten.
        """
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def _wake(self) -> None:
        """Wake everybody waiting for a report: one has just been taken."""
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    def _merge(self, values: Mapping[int, Any]) -> None:
        """Take ``values`` in, and date the lamp's clock if it is among them."""
        self._values.update(values)
        if KEY_TIME in values:
            self.clock_heard_at = self._now()

    # --- the warnings a frame can cause, and the frame at debug ----------------

    def _log_trailing_bytes(self, data: bytes, count: int) -> None:
        """Report a frame rejected for trailing bytes: once loudly, each time at debug.

        Notifications arrive continuously, so an unconditional warning would
        flood the log; one per session is enough to surface the problem. The
        frame itself goes into the debug log, the first one too, and into no
        line above it (see ``_IN_THE_DEBUG_LOG``).
        """
        _LOGGER.debug(
            "%s: frame %s carries %d trailing bytes and was dropped",
            self._address,
            _Printed(data),
            count,
        )
        if self._trailing_warned:
            return
        self._trailing_warned = True
        _LOGGER.warning(
            "%s (%s) sent a frame with %d trailing bytes and it was dropped. The "
            "frame declared less than it carried, so accepting the remainder "
            "could mean acting on a corrupt state. Please report this, with the "
            "frame. %s",
            self._address,
            self._described(),
            count,
            _IN_THE_DEBUG_LOG,
        )

    def _log_unreadable_item(
        self, data: bytes, err: cbor.UnreadableItemError, kept: int
    ) -> None:
        """Report a frame that was read only in part: once loudly, each time at debug.

        As with trailing bytes: a lamp that sends one such frame sends them
        all day, and the first is the one that has to be seen. The frame, with
        the bytes it takes to give the item a reading, is in the debug log.
        ``kept`` is how many of the properties ahead of the item the mirror
        took in, which is not always how many were read.
        """
        _LOGGER.debug(
            "%s: frame %s carries an item that cannot be read (%s); kept the %d "
            "properties ahead of it",
            self._address,
            _Printed(data),
            err,
            kept,
        )
        if self._unreadable_warned:
            return
        self._unreadable_warned = True
        _LOGGER.warning(
            "%s (%s) sent a frame with an item this integration cannot read (%s). "
            "The %d properties ahead of it were kept; whatever follows it could "
            "not be found. Please report this, with the frame. %s",
            self._address,
            self._described(),
            err,
            kept,
            _IN_THE_DEBUG_LOG,
        )
