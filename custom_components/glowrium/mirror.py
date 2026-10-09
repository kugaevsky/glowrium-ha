"""What the lamp has said, frame by frame; and what was written to it.

The mirror of the lamp's state: a read-only mapping of property id to value,
filled from the frames the lamp notifies and from the echo of what was
written to it, and never emptied. It knows how to read a frame (``cbor``)
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
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
import logging
from typing import Any

from . import cbor
from .const import KEY_LATITUDE, KEY_LONGITUDE, KEY_TIME

# The mirror's lines go out under the coordinator's name, as the link's do: the
# log filters, and the tests' caplog, were set up on that name before either
# module existed.
_LOGGER = logging.getLogger(f"{__package__}.coordinator")

# Said wherever the log asks for a frame to be posted (see _for_the_log).
_BLANKED = (
    "What reads as the coordinates stored in the lamp, or as the sunrise and "
    "sunset times it works out from them, is shown as xx; look the frame over "
    "all the same before posting it"
)
# The id under which the lamp keeps those times (nothing here reads them), as
# it stands in a frame: an id above 23 takes a second byte.
_CURVE_KEY = bytes((0x18, 0x34))
# How many bytes follow the head of a CBOR float: double, single, half.
_FLOAT_BYTES = {b"\xfb": 8, b"\xfa": 4, b"\xf9": 2}
# The head of a CBOR byte string. Up to 23 bytes the length is in the head
# itself; 0x58 and 0x59 are followed by one and by two bytes of length.
_BYTES_SHORT = range(0x40, 0x58)
_BYTES_LONGER = {b"\x58": 1, b"\x59": 2}


def _private_at(frame: bytes, at: int) -> tuple[int, int]:
    """Tell whether a value that says where the lamp is starts at ``at``.

    ``(head, value)``: how many bytes name it - the id and the head of the
    value - and how many the value itself takes. ``(1, 0)`` when nothing of
    the kind starts here.
    """
    if frame[at] in (KEY_LATITUDE, KEY_LONGITUDE):
        value = _FLOAT_BYTES.get(frame[at + 1 : at + 2])
        return (2, value) if value else (1, 0)
    if frame[at : at + 2] == _CURVE_KEY:
        head = frame[at + 2 : at + 3]
        if head and head[0] in _BYTES_SHORT:
            return 3, head[0] - _BYTES_SHORT.start
        more = _BYTES_LONGER.get(head, 0)
        length = frame[at + 3 : at + 3 + more]
        if more and len(length) == more:
            return 3 + more, int.from_bytes(length, "big")
    return 1, 0


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


class Mirror(Mapping[int, Any]):
    """What the lamp has said, by property id; and what was written to it.

    A mapping to read, never to write: a value gets in through ``take`` (a
    frame the lamp sent) or ``echo`` (a command it acknowledged), and nothing
    is ever taken out - the mirror is not emptied when a link drops, so what
    it holds can be hours old, which is why the clock in it is dated.

    A report is a frame that carried at least one property. They are
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
        described: Callable[[], str],
        now: Callable[[], datetime],
    ) -> None:
        """Build an empty mirror of the lamp at ``address``.

        ``described`` says what the lamp is - its model and firmware - for the
        two warnings that ask for a frame to be posted; it is asked when the
        warning is written, since the lamp describes itself only after its
        first frames. ``now`` is the host's clock, for dating the lamp's.
        """
        self._address = address
        self._described = described
        self._now = now
        self._values: dict[int, Any] = {}
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
        """Take a frame from the lamp; return the ids it carried.

        Empty when the frame was no report: nothing in it could be read, or
        it was not a map of properties, or a map with nothing in it. Whatever
        the bytes, nothing is raised: this runs inside the Bluetooth stack's
        own notify handler, and the decoder's promise that nothing but a
        ``ValueError`` leaves it is kept here.

        A frame with an item the decoder cannot read is kept as far as it was
        read: dropping it would cost more than its properties, since a state
        request answered only by such a frame would count as unanswered, the
        connect would fall back on reading, and on BlueZ a read ends the link.
        A frame of which nothing was read is still no report.
        """
        short = said = False
        try:
            decoded, short = cbor.decode_frame(frame)
        except cbor.UnreadableItemError as err:
            self._log_unreadable_item(frame, err)
            decoded, said = err.ahead, True
        except cbor.TrailingBytesError as err:
            # Reported apart from a merely malformed frame, and loudly the first
            # time: rejecting these is what changed in #5, and on a model whose
            # frames were always fully consumed before, this is the regression
            # that change risks. Buried in "Undecodable frame" at debug level it
            # would never be noticed.
            self._log_trailing_bytes(frame, err.count)
            return frozenset()
        except ValueError as err:  # the decoder raises nothing else
            _LOGGER.debug("Undecodable frame %s: %s", _for_the_log(frame), err)
            return frozenset()
        if not isinstance(decoded, dict) or not decoded:
            if not said:
                # Decoded without a fault, and still of no use: every frame
                # that is dropped is named, or nobody can ask what it was.
                _LOGGER.debug(
                    "%s: frame %s decodes to nothing that can be used: it is "
                    "not a map of properties, or is one with nothing in it",
                    self._address,
                    _for_the_log(frame),
                )
            return frozenset()
        if short:
            _LOGGER.debug(
                "%s: property map split across frames; kept %d of them",
                self._address,
                len(decoded),
            )
        self._merge(decoded)
        self.reports += 1
        self._reported_at.update(dict.fromkeys(decoded, self.reports))
        self._wake()
        return frozenset(decoded)

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

    # --- the two warnings that ask for a frame --------------------------------

    def _log_trailing_bytes(self, data: bytes, count: int) -> None:
        """Report a frame rejected for trailing bytes: once loudly, then quietly.

        Notifications arrive continuously, so an unconditional warning would
        flood the log; one per session is enough to surface the problem while
        the hex dump below gives whoever reports it everything needed to decode
        the frame by hand.
        """
        if self._trailing_warned:
            _LOGGER.debug(
                "%s: frame %s again carries %d trailing bytes",
                self._address,
                _for_the_log(data),
                count,
            )
            return
        self._trailing_warned = True
        _LOGGER.warning(
            "%s (%s) sent a frame with %d trailing bytes "
            "and it was dropped: %s. The frame declared less than it carried, "
            "so accepting the remainder could mean acting on a corrupt state. "
            "Please report this frame - it is exactly the hex dump needed. %s",
            self._address,
            self._described(),
            count,
            _for_the_log(data),
            _BLANKED,
        )

    def _log_unreadable_item(self, data: bytes, err: cbor.UnreadableItemError) -> None:
        """Report a frame that was read only in part: once loudly, then quietly.

        As with trailing bytes: a lamp that sends one such frame sends them
        all day, and the first is the one that has to be seen - with the bytes
        it takes to give the item a reading.
        """
        if self._unreadable_warned:
            _LOGGER.debug(
                "%s: frame %s again carries an item that cannot be read (%s); "
                "kept the %d properties ahead of it",
                self._address,
                _for_the_log(data),
                err,
                len(err.ahead),
            )
            return
        self._unreadable_warned = True
        _LOGGER.warning(
            "%s (%s) sent a frame with an item this "
            "integration cannot read (%s): %s. The %d properties ahead of it "
            "were kept; whatever follows it could not be found. Please report "
            "this frame - it is exactly the hex dump needed. %s",
            self._address,
            self._described(),
            err,
            _for_the_log(data),
            len(err.ahead),
            _BLANKED,
        )
