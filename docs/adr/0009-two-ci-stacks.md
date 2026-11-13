# Two CI stacks, with library versions taken from Home Assistant and written nowhere

- **Status:** accepted
- **Date:** 2026-10-07 (#24); 2026-10-13 (which minimum lets the ignore go);
  2026-11-13 (the newest stack moves to Home Assistant's own test package)

## Context

The suite was green on Home Assistant 2026.7 and failed on 2026.10 on
2026-10-05: the device-registry lookup it used had been deprecated and raises
in the newer test harness. On 2026-10-06 CI failed on the release branch
because 2026.10 types a config flow's `data_schema` as `probatio.Schema`, and
mypy had only ever been run locally against 2026.7. Both were found late
because one environment was being tested. The first version of the install
script then listed six Bluetooth libraries by name and installed them
unconstrained; both reviews caught `bluetooth-data-tools`, `aioesphomeapi`
and `protobuf` drifting ahead of what Home Assistant ships, so the tests ran
on libraries no user has.

The test plugin, `pytest-homeassistant-custom-component`, says it follows
Home Assistant daily, betas included. Its last release, 0.13.375 on
2026-10-25, carries 2026.10.4, and none followed for 2026.11 - so on
2026-11-13 the newest leg had not tested the Home Assistant users were
running. Home Assistant now publishes a test package of its own,
`pytest-homeassistant`, under its own version numbers from 2026.11.0b4
(2026-11-03). Run on it (2026.11.1), the suite needed three things the
plugin had brought along and the package does not: hassio's library
(`aiohasupervisor`), which usb imports (2026.10 does, 2026.7 did not);
`pytest-cov`, without which CI's `--cov` is refused; and `pytest-timeout`,
without which the timeout in `pyproject.toml` is an option pytest only warns
it does not know.
Nor does the package hold the Bluetooth libraries to the versions their
manifests name: it brought bleak 3.1.0, and bluetooth requires 3.0.2.
Everything else passed as on 2026.10.4, and Home Assistant logged nothing of
the integration it had not logged there.

## Decision

- CI runs the suite (ruff, mypy, pytest at 95 % coverage) on two stacks as a
  matrix with `fail-fast: false`: **oldest**, the Home Assistant this project
  names as its minimum (`hacs.json`), held to one release of the test plugin
  by the single line in `constraints-oldest.txt`; and **newest**, Home
  Assistant's own test package at its newest release, betas included. Its
  floor, `>=2026.11.0b0`, names a pre-release: under PEP 440 that lets pip
  take a beta of this package alone, where `--pre` would let in a beta of
  anything under it, and it keeps pip off the unrelated releases of 2020
  under the same name.
- The oldest stays on the plugin while the minimum is older than the first
  release the package exists for. The two give the same fixtures and are not
  installed together: the script names which package each stack runs on and
  refuses to install one stack into an environment that has the other's,
  and the tests take the helpers from whichever is there
  (`tests/ha_common.py`).
- `tools/stack.py oldest|newest` installs either, locally as in CI, and the
  libraries the way Home Assistant does: the requirements of its own
  `bluetooth`, `usb` and `hassio` integrations (and `aioesphomeapi` from
  `esphome`) at the exact versions their manifests name, under Home
  Assistant's `package_constraints.txt` - both read from the Home Assistant
  under test. `pytest-cov` and `pytest-timeout` are named, unpinned, in
  `requirements-test.txt`, so that on the oldest stack the plugin's own pins
  hold. The rest of the pytest configuration the package's description
  suggests - `-p no:respx` among it, so that Home Assistant puts its httpx
  alias in place before respx is imported - is not taken: these tests mock
  no HTTP, and the suite passes on 2026.11.1 without it.
- Those versions are written nowhere in this repository. Dependabot ignores
  only the test plugin; the libraries need no entry because nothing names
  them.
- `GLOWRIUM_STACK` tells the tests which leg they are on: on `oldest` the
  pinned plugin must be the declared minimum, and the check cannot be skipped.
- CI also runs once a week with nothing pushed: the newest leg moves by
  itself, and `_close_bus` reaches into bleak's private attributes (ADR 0003).
- A pull request from a branch of this repository is not run twice; the push
  run is kept (the merge commit with the base then goes untested).

## Consequences

- Raising the minimum is one change in two places together, `hacs.json` and
  `constraints-oldest.txt`; a test compares them.
- The day the minimum reaches 2026.11, the oldest stack moves to Home
  Assistant's package as well, pinned at the minimum itself - the package's
  number is Home Assistant's - and the plugin, the second import in
  `tests/ha_common.py` and the fixture that enables custom integrations go.
- Each stack needs a virtualenv of its own; one built for the newest stack
  before this has the plugin in it, and is refused until it is made anew.
- Before a push, the tests and mypy are run on both stacks locally.
- The `# type: ignore[arg-type, unused-ignore]` in `config_flow.py` goes with
  the move to probatio once the minimum is 2026.10 - not the first version
  that has probatio, 2026.9, where type checking refuses a probatio schema
  the other way round. The minimum is not raised for that alone; the brief
  for the day it moves is kept on #27, and the comment in
  `constraints-oldest.txt` points there and here.
- An environment whose libraries are not at the version the script would have
  left fails the suite; the remedy is to run the script again.

## Evidence

- ARCHITECTURE.md, "Testing" (last paragraph); `.github/workflows/test.yml`,
  `.github/dependabot.yml`, `constraints-oldest.txt`.
- `tests/test_stack.py::test_no_library_home_assistant_requires_is_named_in_the_requirements`,
  `::test_the_oldest_stack_is_the_minimum_this_project_declares`,
  `::test_this_environment_is_one_the_script_would_have_left`,
  `::test_the_libraries_are_installed_the_way_home_assistant_installs_them`,
  `::test_ci_runs_every_stack_the_script_installs`,
  `::test_dependabot_leaves_the_test_plugin_alone`,
  `::test_this_environment_is_one_stack_and_not_both`,
  `::test_a_hang_is_cut_short_and_coverage_is_counted_on_either_stack`,
  `::test_each_stack_is_the_same_requirements_and_what_carries_its_home_assistant`,
  `::test_a_stack_is_not_installed_over_the_other`.

## Revisit when

Home Assistant's test package comes to install the libraries of the
integrations it carries at the versions their manifests name - it ships
Home Assistant at a named release now, which was the first half of this
condition - or the minimum and the newest release stop differing in the
APIs this integration touches.
