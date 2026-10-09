# Settings are `config` entities; the light and the operating mode are the only controls

- **Status:** accepted
- **Date:** 2026-10-07 (#36)

## Context

Home Assistant's device page has three fixed sections - Controls,
Configuration, Diagnostic - chosen by `entity_category`, and the category
decides more than the section. An entity without one is taken for one of the
device's main controls: an action aimed at an area, a device or a floor
reaches it, and voice assistants expose it by default. The indicator switch,
the DST switch and the *Sync location* button carried no category. "Turn on
every switch in this room" set the daylight-saving flag, which on the G7 it
was watched on put the lamp's own program an hour out, with nothing to say
why. The *Latitude* and *Longitude* sensors, enabled, kept the lamp's position
in states and in the recorder's history on every installation, unasked.

## Decision

- Controls are the light and the operating mode, and only those.
- Everything else that can be set - lighting mode, ramp, schedule start, end,
  gradual and brightness, DST, the indicator, *Sync location* - is a setting:
  `EntityCategory.CONFIG`. It sits under Configuration, an action aimed at a
  room or a device passes it over, and one that names the entity reaches it.
- *Latitude*, *Longitude* and *Activated* are `EntityCategory.DIAGNOSTIC`; the
  two coordinate sensors have `entity_registry_enabled_default = False`.
- A new setting is `config` from its first commit.

## Consequences

- `entity_registry_enabled_default` applies at first registration only: an
  installation that already had the coordinate sensors keeps them as they
  were, and the tests set that case up explicitly.
- Mode-dependent settings still gate on the operating mode (`mode_allows`) and
  stay available while it is unknown.
- Three entities moved from Controls to Configuration for existing users
  (CHANGELOG 0.3.1).

## Evidence

- CHANGELOG 0.3.1 (#36); README, "Controls and settings".
- `tests/test_init.py::test_an_action_aimed_at_the_lamps_room_reaches_the_light_and_no_setting`,
  `::test_a_setting_of_the_lamp_is_registered_as_a_setting`,
  `::test_coordinate_sensors_an_installation_has_already_are_left_alone`.

## Revisit when

Home Assistant adds a category or a flag that separates "not a main control"
from "configuration", or the lamp grows a control that is neither its light
nor its mode.
