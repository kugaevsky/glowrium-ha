"""Tests for what every test here runs with: the fixtures of ``conftest.py``."""

import asyncio
import logging

import pytest


async def test_a_slow_step_of_the_loop_is_not_a_warning_a_test_hears(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """What asyncio says of a slow step is kept from the tests; the rest is not.

    Why, and at what price, is said at the fixture (``slow_steps_go_unsaid``).
    Fifteen cases in the tests of the bus lifetime read every WARNING there
    is, whoever wrote it - to hold the integration to having warned of
    nothing, or of exactly so much - and each fails with that note among
    them. Here every step is made a slow one, so the note is certain to be
    written.
    """
    loop = asyncio.get_running_loop()
    assert loop.get_debug()  # the test plugin's doing, and where the note is from
    monkeypatch.setattr(loop, "slow_callback_duration", 0)

    await asyncio.sleep(0)  # a step ends here, and asyncio notes it
    logging.getLogger("asyncio").warning("anything else asyncio has to say")

    assert [r.getMessage() for r in caplog.records if r.name == "asyncio"] == [
        "anything else asyncio has to say"
    ]
