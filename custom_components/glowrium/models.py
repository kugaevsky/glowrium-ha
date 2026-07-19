"""Per-model profiles for Glowrium devices.

The BLE control protocol (CBOR over the ``facebd0x`` service) is shared across
the Glowrium family; only a few bits differ per model - the marketing name, the
light-entity icon, and the set of circadian lighting presets. Those live here,
keyed by the ``pkey``
field of the device-info string (``facebd80``). Add a new model by adding one
``GlowriumModel`` entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class GlowriumModel:
    """Model-specific configuration resolved from the device-info ``pkey``."""

    pkey: str  # device-info identifier, e.g. "Glowrium-C051"
    name: str  # marketing model name shown on the device page
    lighting_modes: dict[str, int]  # circadian preset label -> command index
    icon: str | None = None  # light-entity icon; None -> HA's default light icon


# Glowrium G7 - 48W puck grow light (app model "Glowrium-C051"). Fully verified
# on hardware; indices confirmed against btsnoop captures.
G7: Final = GlowriumModel(
    pkey="Glowrium-C051",
    name="Glowrium G7",
    lighting_modes={
        "Sun SYNC": 1,
        "Before Sunrise": 2,
        "Sunrise Sync": 5,
        "Sunset Sync": 9,
        "After Sunset": 16,
        "Two-Phase": 18,
        "Balance": 19,
        "Enhanced Two-Phase": 32,
    },
    icon="mdi:lightbulb-group",
)

# Registry keyed by device-info pkey. Extend with other Glowrium models here.
MODELS: Final[dict[str, GlowriumModel]] = {G7.pkey: G7}

# Reference presets for a device whose pkey we do not have a profile for yet.
DEFAULT_MODEL: Final = G7


def resolve_model(pkey: str | None) -> GlowriumModel:
    """Return the model profile for a device-info ``pkey``.

    A known pkey gets its full profile. An unknown (or not-yet-read) device gets
    a generic profile - a generic ``Glowrium`` name with the reference presets -
    rather than masquerading as a specific, tested model.
    """
    if pkey and pkey in MODELS:
        return MODELS[pkey]
    return GlowriumModel(
        pkey=pkey or "",
        name="Glowrium",
        lighting_modes=DEFAULT_MODEL.lighting_modes,
    )
