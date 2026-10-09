# Two CI stacks, with library versions taken from Home Assistant and written nowhere

- **Status:** accepted
- **Date:** 2026-10-07 (#24)

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

## Decision

- CI runs the suite (ruff, mypy, pytest at 95 % coverage) on two stacks as a
  matrix with `fail-fast: false`: **oldest**, the Home Assistant this project
  names as its minimum (`hacs.json`), held to one release of the test plugin
  by the single line in `constraints-oldest.txt`; and **newest**, the plugin
  unpinned, betas included - whatever it tracks that day.
- `tools/stack.py oldest|newest` installs either, locally as in CI, and the
  libraries the way Home Assistant does: the requirements of its own
  `bluetooth` and `usb` integrations (and `aioesphomeapi` from `esphome`) at
  the exact versions their manifests name, under Home Assistant's
  `package_constraints.txt` - both read from the Home Assistant under test.
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
- Before a push, the tests and mypy are run on both stacks locally.
- The `# type: ignore[arg-type, unused-ignore]` in `config_flow.py` goes with
  the move to probatio once the minimum has it.
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
  `::test_dependabot_leaves_the_test_plugin_alone`.

## Revisit when

Home Assistant ships a supported way to install an integration's test
environment at a named release, or the minimum and the newest release stop
differing in the APIs this integration touches.
