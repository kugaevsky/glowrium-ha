"""Home Assistant's test helpers, from whichever package this stack has.

The newest stack runs on Home Assistant's own test package, the oldest on the
plugin that carried Home Assistant before that package existed (2026.11).
Both give the same helpers in a module named ``common``. The day the oldest
stack moves to the package too, the second import goes (ADR 0009).
"""

try:
    from pytest_homeassistant.common import (
        MockConfigEntry,
        async_fire_time_changed,
        mock_restore_cache,
        mock_restore_cache_with_extra_data,
    )
except ModuleNotFoundError:  # the oldest stack
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        async_fire_time_changed,
        mock_restore_cache,
        mock_restore_cache_with_extra_data,
    )

__all__ = [
    "MockConfigEntry",
    "async_fire_time_changed",
    "mock_restore_cache",
    "mock_restore_cache_with_extra_data",
]
