# A release is one archive, and HACS installs nothing else

- **Status:** accepted
- **Date:** 2026-10-13 (#28: decided by the maintainer on 2026-10-07; that it
  is switched on with the next tag and no file is put on the releases that
  exist, on 2026-10-12, on #43 and #53)

## Context

Nobody counted installs. HACS downloaded the source archive of a tag, for
which GitHub publishes no counter; the releases carried no file, so their
download counters were zero by construction; and Home Assistant's opt-in
analytics did not list the integration (looked at on 2026-10-06). GitHub
does count the fetches of a file attached to a release, and HACS can be told
to install such a file (`zip_release` and `filename` in `hacs.json`).

What HACS does with those keys was read in its source, at 2.0.5 - its latest
release on 2026-10-13, and the version of 96 % of the HACS installations in
Home Assistant's analytics on 2026-10-07 - and on its main branch. **None of
it was run against a live HACS**, and the workflow below had made no release
when this was written. As read:

- It reads `hacs.json` at the version it is about to install. Each release
  goes by its own settings, and the releases that exist are untouched.
- Told to take a file, it looks for it on whatever it is asked to install and
  does not fall back to the tag's source. A release that promises a file and
  has none cannot be installed; neither can the default branch.
- Whether the default branch is offered goes by the `hacs.json` HACS holds
  for the repository, which is the latest release's: the branch goes on
  being offered until a release carries `hide_default_branch`. A download of
  the branch over an installation that is already there goes by the branch's
  own `hacs.json`: from the day the keys are on the branch it looks for a
  file there and fails.
- It unpacks the file as it is into the integration's directory.
- It reports the download count of the first file a release has.
- On 2.0.5, a first install that picks a version older than the latest goes
  by the latest release's settings. That is fixed on HACS' main branch and
  not released.
- Its validation action checks that the keys are well-formed and has no
  check that looks at a release's files.

`git archive` was measured (2026-10-13, git 2.55): it leaves out a tracked
file that a `.gitattributes` in the directory being packed marks
`export-ignore`. And `gh` was read at 2.101.0: it finds a draft by its tag,
says "release not found" of a tag that has none, and with `--draft` makes
the draft first and uploads to it second, leaving the draft if the upload
fails.

## Decision

- A release is one file, `glowrium.zip`: exactly the files tracked under the
  integration's directory at the tagged commit, at the archive's root, each
  as git holds it. It is built from git's objects, never from the working
  directory and not through `git archive`; the manifest is not rewritten;
  every entry carries the commit's time, so two builds of one commit on one
  toolchain are the same bytes.
- `hacs.json` carries `zip_release`, `filename` and `hide_default_branch`
  together. The third is required: without it HACS offers a default branch
  it can no longer install.
- The archive is built on the push of a version tag and put on a **draft**
  of the release. The maintainer writes the notes and publishes. The
  workflow publishes nothing, edits no release's title or notes, and makes,
  moves and deletes no tag.
- A tag is `v` and the version the manifest has at it; any other tag fails
  the run before anything is made.
- A release that exists without the archive is given it; a draft's archive
  is replaced; the archive of a published release is left alone, since
  GitHub's count belongs to the file and its bytes are what users have.
- One file per release and no second. The workflow says so when it finds
  one, and takes nothing off a release.
- Every day, and read-only, each published release - pre-releases too - is
  held to the `hacs.json` at its own tag: one that promises a file carries
  exactly that file, not empty. The file itself is never fetched. A
  `hacs.json` that cannot be read fails the check.
- The workflows read; the one job that makes a draft or attaches a file may
  write. Of that job's own scripts the token is handed to one, the one that
  talks to GitHub; the two actions the job uses get the job's token from
  GitHub, as in any job.

## Consequences

- An unreleased commit cannot be installed through HACS. What remains is a
  copy by hand, or a pre-release with a file of its own.
- For a release the keys take effect with the first tag that carries them;
  for the default branch, with the commit. Between the two, as read, HACS
  still offers the branch and an installation that picks it gets a failed
  download and keeps the files it had.
- The count is fetches of a release's file - installs, updates, re-downloads
  and anything else that fetched it - per release. It is not a number of
  installations, and the releases up to 0.4.0 have none.
- No file is attached to the releases that exist. On HACS 2.0.5 a first
  install that picks one of them is expected to fail once the latest release
  promises a file; the README says how to get round it.
- A release is never public without its file only if the order is kept: tag,
  wait for the draft, notes, publish. A release created by hand before the
  draft exists is public without its file until the run ends.
- A run that fails between making the draft and uploading to it leaves a
  draft without its file. A draft is not public, and a second run puts the
  file on it.
- Whoever can push a tag starts a job that can write to the repository's
  contents. That was already who can push.
- What guards a published release is this repository's own daily check: the
  validation action does not.
- GitHub's immutable releases are off for this repository (asked on
  2026-10-13). With them on, a file could not be put on a release already
  published; draft first is the order GitHub documents for them.

## Evidence

- `tools/release.py`; `.github/workflows/release.yml`, `release-check.yml`,
  `test.yml` (the build on every push); `hacs.json`.
- README, "Installation"; CONTRIBUTING, "Cutting a release"; CHANGELOG,
  Unreleased (#28).
- `tests/test_release.py::test_the_archive_holds_the_integration_at_its_root_as_it_is_at_the_commit`,
  `::test_the_archive_is_made_of_the_commit_and_not_of_the_working_directory`,
  `::test_a_tracked_file_is_shipped_whatever_git_is_told_of_exporting_it`,
  `::test_every_entry_carries_the_time_of_the_commit_and_the_mode_git_holds`,
  `::test_building_one_commit_twice_gives_the_same_bytes`,
  `::test_a_commit_may_be_named_by_a_tag_that_has_words_of_its_own`,
  `::test_a_tag_that_is_not_the_manifests_version_is_refused_and_both_are_named`,
  `::test_a_tag_with_no_release_gets_a_draft_that_carries_the_archive`,
  `::test_a_release_without_the_archive_is_given_it`,
  `::test_a_file_under_another_name_is_not_the_archive_and_is_spoken_of`,
  `::test_a_draft_that_has_the_archive_has_it_replaced`,
  `::test_a_published_release_that_has_the_archive_is_left_as_it_is`,
  `::test_a_release_is_held_to_the_hacs_json_at_its_own_tag`,
  `::test_a_published_release_that_does_not_carry_what_it_promises_fails_the_check`,
  `::test_a_pre_release_is_held_to_its_promise_as_a_release_is`,
  `::test_the_check_reads_and_never_asks_for_the_archive_itself`,
  `::test_one_job_may_write_and_no_script_but_the_attaching_one_has_the_token`,
  `::test_hacs_is_told_to_take_the_archive_and_not_to_offer_the_default_branch`.
- What was read in HACS and measured of GitHub: the triage notes on #28.

## Revisit when

A rehearsal against a live HACS shows that it does not do what its source
reads as; or HACS releases the fix for a first install of an older version,
and the sentence about it can leave the README.
