"""Constants for the Glowrium integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "glowrium"

# BLE devices advertise a local name like "Glowrium-G7_6DCB8A".
NAME_PREFIX: Final = "Glowrium"

# GATT characteristics of the "rabbit iot ble" (facebd0x-...) control service.
WRITE_UUID: Final = "facebd01-7261-6262-6974-696f74626c65"  # commands (write)
NOTIFY_UUID: Final = "facebd02-7261-6262-6974-696f74626c65"  # state (notify/read/write)
# Readable device-info string: "brand:...;pkey:...;devid:...;mac:...;version:...".
INFO_UUID: Final = "facebd80-7261-6262-6974-696f74626c65"

# CBOR property keys (verified against btsnoop captures + the live device).
KEY_TIME: Final = 0x05  # bytes: year_be, month, day, hour, min, sec
KEY_POWER: Final = 0x06  # bool
KEY_BRIGHTNESS: Final = 0x08  # int 0..100
KEY_CIRCADIAN: Final = 0x09  # bool - sunrise/sunset synced mode
KEY_LATITUDE: Final = (
    0x0A  # float64 degrees - device computes the circadian curve from this
)
KEY_LONGITUDE: Final = 0x0B  # float64 degrees
KEY_SCHEDULE: Final = 0x0D  # bool - schedule/timer mode (exclusive w/ circadian)
KEY_TIMER: Final = 0x11  # Pro schedule slot (see TIMER_* offsets below)
KEY_ACTIVATED: Final = 0x14  # bool - device enabled/paired; False on a factory reset
KEY_INDICATOR: Final = 0x17  # bool - status indicator LED
KEY_LIGHTING_MODE: Final = 0x2B  # int - circadian lighting-mode index
KEY_RAMP: Final = 0x2F  # bytes(2) big-endian seconds; 0 = Sun Sync auto ramp
KEY_DST: Final = 0x35  # bytes: [enabled, offset_be_4] (offset 0x0e10 = 3600s = 1h)

# Bring-up ("pairing") the vendor app performs on a factory-reset device so that
# its light output is enabled - decoded from a fresh-pairing btsnoop, and purely
# local (no cloud, no BLE bond). The coordinator replays it when 0x14 reads False:
#   {0x53: 300} -> {0x05: <local time>, 0x31: 1} -> {0x14: True}
KEY_ACTIVATE_MISC: Final = 0x53  # unconfirmed param; the app sends 300
ACTIVATE_MISC_VALUE: Final = 300
KEY_TIME_SYNCED: Final = 0x31  # set to 1 together with the clock (0x05)

# Properties requested from the device on connect (raw id bytes to NOTIFY_UUID).
# Only ids the app itself requests are safe - others make the device disconnect.
STATE_KEYS: Final = (
    KEY_POWER,
    KEY_BRIGHTNESS,
    KEY_CIRCADIAN,
    KEY_LATITUDE,
    KEY_LONGITUDE,
    KEY_SCHEDULE,
    KEY_TIMER,
    KEY_ACTIVATED,
    KEY_INDICATOR,
    KEY_LIGHTING_MODE,
    KEY_RAMP,
    KEY_DST,
)

# 0x11 timer slot: [enabled, 00,00,00, start_h, start_m, end_h, end_m,
#                   brightness, gradual_seconds_be(2)] - 11 bytes.
TIMER_START_H: Final = 4
TIMER_START_M: Final = 5
TIMER_END_H: Final = 6
TIMER_END_M: Final = 7
TIMER_BRIGHTNESS: Final = 8
TIMER_GRADUAL: Final = 9  # 2-byte big-endian seconds at [9:11]
TIMER_DEFAULT: Final = bytes.fromhex(
    "0100000006001200640000"
)  # 06:00-18:00, 100%, no fade

# A lighting-mode command is {mode, 0x2c, ramp(0x2f), 0x32} in this key order.
# 0x2c/0x32 are constant; 0x2f is the ramp time and is preserved from state.
MODE_PARAM_2C: Final = bytes.fromhex("02d0")
MODE_PARAM_32: Final = bytes.fromhex("001e")
RAMP_DEFAULT: Final = bytes.fromhex("0e10")  # 3600s = 60 min

# Circadian lighting-mode presets are model-specific - see models.py.

# DST is a 5-byte struct: byte0 = enabled, bytes1-4 = offset seconds (1 hour).
DST_ON: Final = bytes.fromhex("0100000e10")
DST_OFF: Final = bytes.fromhex("0000000e10")

# Operating mode is the mutually-exclusive Circadian/Schedule pair.
MODE_MANUAL: Final = "manual"
MODE_CIRCADIAN: Final = "circadian"
MODE_SCHEDULE: Final = "schedule"
OPERATING_MODES: Final = (MODE_MANUAL, MODE_CIRCADIAN, MODE_SCHEDULE)
