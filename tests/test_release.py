"""Tests for tools/release.py: the archive a release carries, and its keeping.

A release is one file, `glowrium.zip`, and HACS unpacks it as it is into the
integration's directory. What goes into it is built here from a repository
made for the test; what GitHub is asked is read from the commands the script
gives to `gh`, which here is a stand-in. Nothing reaches GitHub.
"""

import json
import os
from pathlib import Path
import re
from types import ModuleType
from typing import Any
import zipfile

import pytest
import yaml

from .conftest import ROOT, load_tool

_HERE = "custom_components/glowrium"
# What is tracked under the integration's directory at the commit.
_TRACKED = {
    "manifest.json": b'{"domain": "glowrium", "version": "1.2.3"}\n',
    "__init__.py": b'"""The integration."""\n',
    "translations/en.json": b'{"title": "Glowrium"}\r\n',
    "not_text.bin": bytes(range(256)),
    "made_to_run.sh": b"#!/bin/sh\n",
}
# The one of them that git holds as a file to be run.
_RUN = "made_to_run.sh"
# And beside it: tracked, and no part of the integration.
_ELSEWHERE = {
    "README.md": b"# Glowrium\n",
    "custom_components/other/__init__.py": b"",
    "hacs.json": b"{}\n",
}


@pytest.fixture(scope="module")
def release() -> ModuleType:
    """Return tools/release.py as a module."""
    return load_tool("release")


@pytest.fixture
def repository(
    release: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Return a repository of one commit that holds an integration.

    Made with nothing of this machine's git in it: no signing, no hooks, and
    a commit time that is the test's own.
    """
    for name, value in {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "A test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "A test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_AUTHOR_DATE": "2026-01-02T03:04:06+09:00",
        "GIT_COMMITTER_DATE": "2026-01-02T03:04:06+09:00",
    }.items():
        monkeypatch.setenv(name, value)
    repo = tmp_path / "repo"
    repo.mkdir()
    release.git("init", "--quiet", repo=repo)
    for name, content in {
        **{f"{_HERE}/{name}": content for name, content in _TRACKED.items()},
        **_ELSEWHERE,
    }.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    release.git("add", "--all", repo=repo)
    release.git("update-index", "--chmod=+x", f"{_HERE}/{_RUN}", repo=repo)
    release.git("commit", "--quiet", "--message", "One", repo=repo)
    return repo


def _built(release: ModuleType, repo: Path, name: str = "glowrium.zip") -> Path:
    out = repo.parent / name
    release.build("HEAD", out, repo=repo)
    return out


def _held(archive: Path) -> dict[str, bytes]:
    """Return what an archive holds: each entry's name and its bytes."""
    with zipfile.ZipFile(archive) as opened:
        return {name: opened.read(name) for name in opened.namelist()}


def test_the_archive_holds_the_integration_at_its_root_as_it_is_at_the_commit(
    release: ModuleType, repository: Path
) -> None:
    """HACS unpacks the file as it is into the integration's own directory.

    So the manifest lies at the top, no entry carries the directory's path,
    and nothing from beside the integration is in it. Each file is the file
    at the commit byte for byte, a line ending and a byte that is no text
    among them.
    """
    assert _held(_built(release, repository)) == _TRACKED


def test_the_archive_is_made_of_the_commit_and_not_of_the_working_directory(
    release: ModuleType, repository: Path
) -> None:
    """What lies in the directory and is not in the commit is not shipped.

    A cache, a file nobody added, an edit nobody committed: none of it is in
    the archive, and a file deleted from the directory still is.
    """
    here = repository / _HERE
    (here / "__pycache__").mkdir()
    (here / "__pycache__" / "stale.pyc").write_bytes(b"\0")
    (here / "nobody_added.py").write_text("")
    (here / "__init__.py").write_text('"""Edited, and not committed."""\n')
    (here / "not_text.bin").unlink()

    assert _held(_built(release, repository)) == _TRACKED


def test_a_tracked_file_is_shipped_whatever_git_is_told_of_exporting_it(
    release: ModuleType, repository: Path
) -> None:
    """The archive is every tracked file, and an attribute takes none out.

    This is why it is not made with `git archive`: that was measured
    (2026-10-13, git 2.55) to leave out a file marked `export-ignore` by a
    `.gitattributes` in the very directory it packs. The test holds the
    build to the file being there; it does not run `git archive`.
    """
    (repository / _HERE / ".gitattributes").write_text("__init__.py export-ignore\n")
    release.git("add", "--all", repo=repository)
    release.git("commit", "--quiet", "--message", "Two", repo=repository)

    assert set(_held(_built(release, repository))) == {*_TRACKED, ".gitattributes"}


def test_every_entry_carries_the_time_of_the_commit_and_the_mode_git_holds(
    release: ModuleType, repository: Path
) -> None:
    """Nothing of the day or the machine it was built on is written in.

    The time is the commit's, in UTC whatever zone the commit was made in.
    The mode is the one git has for the file - to be read, or to be run - so
    that an archive unpacked by hand is made of files that can be read.
    """
    with zipfile.ZipFile(_built(release, repository)) as opened:
        entries = opened.infolist()

    assert {entry.date_time for entry in entries} == {(2026, 1, 1, 18, 4, 6)}
    assert {entry.filename: oct(entry.external_attr >> 16) for entry in entries} == {
        name: "0o100755" if name == _RUN else "0o100644" for name in _TRACKED
    }


def test_building_one_commit_twice_gives_the_same_bytes(
    release: ModuleType, repository: Path
) -> None:
    """On one machine with one toolchain: the same archive, to the byte."""
    first = _built(release, repository, "first.zip").read_bytes()
    second = _built(release, repository, "second.zip").read_bytes()

    assert first == second


def test_a_commit_may_be_named_by_a_tag_that_has_words_of_its_own(
    release: ModuleType, repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An annotated tag is an object beside the commit, as this project's are.

    The archive of it is the archive of the commit it points at, and carries
    that commit's time and not the tag's.
    """
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-02-03T04:05:06+00:00")
    release.git("tag", "--annotate", "--message", "Words", "v1.2.3", repo=repository)
    out = repository.parent / "glowrium.zip"

    release.build("v1.2.3", out, repo=repository)

    of_the_commit = _built(release, repository, "of-the-commit.zip")
    assert out.read_bytes() == of_the_commit.read_bytes()
    assert _held(out) == _TRACKED


def test_a_commit_that_is_not_there_ends_the_run_in_gits_own_words(
    release: ModuleType, repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """What git said is shown, and nothing is written."""
    out = repository.parent / "glowrium.zip"

    assert release.main(["archive", "no-such-commit", str(out)], repo=repository) == 1
    assert "fatal: " in capsys.readouterr().err
    assert not out.exists()


# --- The tag and the version the manifest has ---


@pytest.mark.parametrize("tag", ["v1.2.4", "1.2.3", "v1.2.3-rc1", "V1.2.3"])
def test_a_tag_that_is_not_the_manifests_version_is_refused_and_both_are_named(
    release: ModuleType,
    repository: Path,
    capsys: pytest.CaptureFixture[str],
    tag: str,
) -> None:
    """A release's tag is `v` and the manifest's version, or there is none.

    HACS shows the tag and Home Assistant the manifest. Nothing is built for
    a tag that says something else, and whoever pushed it is told both.
    """
    out = repository.parent / "glowrium.zip"
    asked = ["archive", "HEAD", str(out), "--tag", tag]

    assert release.main(asked, repo=repository) == 1

    said = capsys.readouterr().err
    assert f"The tag {tag} is not" in said
    assert "has 1.2.3, and" in said
    assert not out.exists()


def test_the_version_is_the_commits_and_not_the_working_directorys(
    release: ModuleType, repository: Path
) -> None:
    """A manifest edited and not committed is not the release's manifest."""
    (repository / _HERE / "manifest.json").write_text('{"version": "9.9.9"}')
    out = repository.parent / "glowrium.zip"
    asked = ["archive", "HEAD", str(out), "--tag"]

    assert release.main([*asked, "v9.9.9"], repo=repository) == 1
    assert not out.exists()
    assert release.main([*asked, "v1.2.3"], repo=repository) == 0
    assert _held(out) == _TRACKED


def test_with_no_tag_the_archive_is_built_and_no_version_is_asked_after(
    release: ModuleType, repository: Path
) -> None:
    """As CI builds it on every push, long before there is a tag."""
    out = repository.parent / "glowrium.zip"

    assert release.main(["archive", "HEAD", str(out)], repo=repository) == 0
    assert _held(out) == _TRACKED


# --- What GitHub is told when a version tag is pushed ---

_TAG = "v1.2.3"


class _GitHub:
    """Stands in for `gh`: says what release a tag has, and keeps what it is told."""

    def __init__(self, release: ModuleType, held: dict[str, Any] | str | None) -> None:
        """Hold the release the tag has, none, or the words GitHub fails with."""
        self._failing = release.NotDoneError
        self._held = held
        self.told: list[tuple[str, ...]] = []

    def __call__(self, *arguments: str) -> str:
        """Answer one command as `gh` would."""
        if arguments[:2] != ("release", "view"):
            self.told.append(arguments)
            return ""
        assert arguments[2:] == (_TAG, "--json", "isDraft,assets")
        if isinstance(self._held, dict):
            return json.dumps(self._held)
        raise self._failing(self._held or "release not found")


def _a_release(*files: str, draft: bool) -> dict[str, Any]:
    return {"isDraft": draft, "assets": [{"name": name} for name in files]}


def _attached(
    release: ModuleType, tmp_path: Path, held: dict[str, Any] | str | None
) -> tuple[int, _GitHub, str]:
    """Return how attaching ended, what GitHub was told, and the archive's path."""
    archive = tmp_path / "glowrium.zip"
    archive.write_bytes(b"an archive")
    github = _GitHub(release, held)
    return release.main(["attach", _TAG, str(archive)], gh=github), github, str(archive)


def test_a_tag_with_no_release_gets_a_draft_that_carries_the_archive(
    release: ModuleType, tmp_path: Path
) -> None:
    """A draft, for a tag that is there already, under the tag's own name.

    The maintainer writes the notes and publishes: nothing here does. The
    draft and its file are asked for in one command, which is refused where
    the tag is not there: so no tag is made.
    """
    ended, github, archive = _attached(release, tmp_path, None)

    assert ended == 0
    (told,) = github.told
    assert told[:8] == (
        "release",
        "create",
        _TAG,
        archive,
        "--draft",
        "--verify-tag",
        "--title",
        _TAG,
    )
    assert told[8] == "--notes"
    assert len(told) == 10


@pytest.mark.parametrize("draft", [True, False])
def test_a_release_without_the_archive_is_given_it(
    release: ModuleType, tmp_path: Path, draft: bool
) -> None:
    """A draft, or a release somebody published by hand right after the tag."""
    ended, github, archive = _attached(release, tmp_path, _a_release(draft=draft))

    assert ended == 0
    assert github.told == [("release", "upload", _TAG, archive)]


def test_a_file_under_another_name_is_not_the_archive_and_is_spoken_of(
    release: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The archive is put beside it, and the run says a release has one file.

    HACS reports the count of the first file a release has. Taking the other
    one off is the maintainer's: nothing here removes a file.
    """
    held = _a_release("notes.pdf", draft=True)

    ended, github, archive = _attached(release, tmp_path, held)

    assert ended == 0
    assert github.told == [("release", "upload", _TAG, archive)]
    assert "also carries notes.pdf" in capsys.readouterr().out


def test_a_draft_that_has_the_archive_has_it_replaced(
    release: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nobody has fetched a draft's file: the tag was pushed again, or the run."""
    held = _a_release("glowrium.zip", draft=True)

    ended, github, archive = _attached(release, tmp_path, held)

    assert ended == 0
    assert github.told == [("release", "upload", _TAG, archive, "--clobber")]
    assert "also carries" not in capsys.readouterr().out


def test_a_published_release_that_has_the_archive_is_left_as_it_is(
    release: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Replacing it would start its count again and change bytes under users."""
    held = _a_release("glowrium.zip", draft=False)

    ended, github, _ = _attached(release, tmp_path, held)

    assert ended == 0
    assert github.told == []
    assert f"{_TAG} is published and has its glowrium.zip" in capsys.readouterr().out


def test_an_answer_that_is_not_about_the_release_ends_the_run_and_is_shown(
    release: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only "there is no such release" makes a draft; a refusal makes nothing."""
    ended, github, _ = _attached(release, tmp_path, "HTTP 401: Bad credentials")

    assert ended == 1
    assert github.told == []
    assert "HTTP 401: Bad credentials" in capsys.readouterr().err


def test_a_file_under_another_name_is_not_attached(
    release: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """GitHub names a release's file after the file it was given."""
    other = tmp_path / "integration.zip"
    other.write_bytes(b"an archive")
    github = _GitHub(release, None)

    assert release.main(["attach", _TAG, str(other)], gh=github) == 1
    assert github.told == []
    assert "glowrium.zip" in capsys.readouterr().err


# --- What a published release promises, looked at every day ---

_PLAIN = {"name": "Glowrium", "homeassistant": "2026.7.0"}
_PROMISING = {**_PLAIN, "zip_release": True, "filename": "glowrium.zip"}
_NAMING_NO_FILE = {**_PLAIN, "zip_release": True}
_THE_ARCHIVE = (("glowrium.zip", 61_000),)


def _published(
    tag: str, files: tuple[tuple[str, int], ...] = (), **said: bool
) -> dict[str, Any]:
    """Return a release as GitHub's API lists it, as far as it is read here."""
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": [{"name": name, "size": size} for name, size in files],
        **said,
    }


class _Releases:
    """Stands in for `gh api`: the releases, and each tag's own hacs.json."""

    def __init__(
        self,
        release: ModuleType,
        releases: list[dict[str, Any]],
        hacs_json: dict[str, dict[str, Any]],
    ) -> None:
        """Hold the releases, a page apiece, and what hacs.json is at each tag."""
        self._failing = release.NotDoneError
        self._pages = [[listed] for listed in releases]
        self._hacs_json = hacs_json
        self.asked: list[tuple[str, ...]] = []

    def __call__(self, *arguments: str) -> str:
        """Answer one command as `gh` would."""
        self.asked.append(arguments)
        path = arguments[-1]
        if path == "repos/{owner}/{repo}/releases":
            return json.dumps(self._pages)
        start = "repos/{owner}/{repo}/contents/hacs.json?ref="
        assert path.startswith(start)
        tag = path.removeprefix(start)
        if tag not in self._hacs_json:
            raise self._failing("gh: Not Found (HTTP 404)")
        return json.dumps(self._hacs_json[tag])


def test_releases_that_promise_no_archive_pass_whatever_they_carry(
    release: ModuleType,
) -> None:
    """Every release made before the archive: HACS takes the tag's source."""
    releases = [_published("v0.4.0"), _published("v0.3.1", (("notes.pdf", 9),))]
    github = _Releases(release, releases, {"v0.4.0": _PLAIN, "v0.3.1": _PLAIN})

    assert release.main(["check"], gh=github) == 0


def test_a_release_is_held_to_the_hacs_json_at_its_own_tag(
    release: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """HACS reads the file at the version it installs, so each goes by its own.

    A release that promises the archive and carries it passes beside an
    older one that promises none and carries none.
    """
    releases = [_published("v0.5.0", _THE_ARCHIVE), _published("v0.4.0")]
    github = _Releases(release, releases, {"v0.5.0": _PROMISING, "v0.4.0": _PLAIN})

    assert release.main(["check"], gh=github) == 0
    assert "2 published, 1 of them promising" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("promised", "files", "carries"),
    [
        (_PROMISING, (), "carries no file"),
        (_PROMISING, (("integration.zip", 61_000),), "carries integration.zip"),
        (
            _PROMISING,
            (("glowrium.zip", 61_000), ("notes.pdf", 9)),
            "carries glowrium.zip (61000 bytes), notes.pdf (9 bytes)",
        ),
        (_PROMISING, (("glowrium.zip", 0),), "carries glowrium.zip (0 bytes)"),
        (_NAMING_NO_FILE, _THE_ARCHIVE, "promises a file it does not name"),
    ],
)
def test_a_published_release_that_does_not_carry_what_it_promises_fails_the_check(
    release: ModuleType,
    capsys: pytest.CaptureFixture[str],
    promised: dict[str, Any],
    files: tuple[tuple[str, int], ...],
    carries: str,
) -> None:
    """Exactly one file, under the promised name, and not empty.

    HACS fails to install a release whose file is not there, and reports the
    count of the first file a release has: so one file, and no second.
    """
    github = _Releases(release, [_published("v0.5.0", files)], {"v0.5.0": promised})

    assert release.main(["check"], gh=github) == 1
    said = capsys.readouterr().err
    assert said.startswith("v0.5.0 promises ")
    assert carries in said


def test_a_pre_release_is_held_to_its_promise_as_a_release_is(
    release: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """HACS offers a pre-release to whoever asks for them."""
    releases = [_published("v0.6.0b1", prerelease=True)]
    github = _Releases(release, releases, {"v0.6.0b1": _PROMISING})

    assert release.main(["check"], gh=github) == 1
    assert "v0.6.0b1 promises glowrium.zip" in capsys.readouterr().err


def test_every_release_that_fails_is_named(
    release: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not the first one alone, and on whichever page of the list it is."""
    releases = [_published("v0.6.0"), _published("v0.5.1", _THE_ARCHIVE)]
    releases.append(_published("v0.5.0"))
    hacs_json = dict.fromkeys(["v0.6.0", "v0.5.1", "v0.5.0"], _PROMISING)

    assert release.main(["check"], gh=_Releases(release, releases, hacs_json)) == 1
    said = capsys.readouterr().err
    assert "v0.6.0" in said
    assert "v0.5.0" in said
    assert "v0.5.1" not in said


def test_a_draft_is_not_looked_at(release: ModuleType) -> None:
    """HACS does not see a draft, and a draft is where the archive is put."""
    releases = [_published("v0.5.0", draft=True)]
    github = _Releases(release, releases, {"v0.5.0": _PROMISING})

    assert release.main(["check"], gh=github) == 0


def test_a_hacs_json_that_cannot_be_read_fails_the_check(
    release: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """What a release promises is not known, which is not the same as nothing."""
    github = _Releases(release, [_published("v0.5.0", _THE_ARCHIVE)], {})

    assert release.main(["check"], gh=github) == 1
    assert "Not Found (HTTP 404)" in capsys.readouterr().err


def test_the_check_reads_and_never_asks_for_the_archive_itself(
    release: ModuleType,
) -> None:
    """The list of releases, and each tag's hacs.json: nothing else is asked.

    A fetch of the archive would be counted as a download of it.
    """
    releases = [_published("v0.5.0", _THE_ARCHIVE), _published("v0.4.0")]
    github = _Releases(release, releases, {"v0.5.0": _PROMISING, "v0.4.0": _PLAIN})

    release.main(["check"], gh=github)

    raw = ("-H", "Accept: application/vnd.github.raw+json")
    assert github.asked == [
        ("api", "--paginate", "--slurp", "repos/{owner}/{repo}/releases"),
        ("api", *raw, "repos/{owner}/{repo}/contents/hacs.json?ref=v0.5.0"),
        ("api", *raw, "repos/{owner}/{repo}/contents/hacs.json?ref=v0.4.0"),
    ]


# --- The workflows that run the script ---

_WORKFLOWS = ROOT / ".github" / "workflows"
# PyYAML reads the key `on` as the boolean it also spells.
_ON = True
_BUILD = 'python tools/release.py archive HEAD "$RUNNER_TEMP/glowrium.zip"'


def _workflow(name: str) -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load((_WORKFLOWS / name).read_text())
    return loaded


def _runs(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def test_the_archive_is_put_on_a_release_for_a_version_tag_and_nothing_else() -> None:
    """Not for a branch, a pull request or the clock: a tag that starts with v."""
    assert _workflow("release.yml")[_ON] == {"push": {"tags": ["v*"]}}


def test_one_job_may_write_and_no_script_but_the_attaching_one_has_the_token() -> None:
    """The workflow reads; the one job that makes a draft or attaches may write.

    Of the job's own scripts, the token is given to the one that talks to
    GitHub, and the one before it - which builds the archive and holds the
    tag to the manifest's version - runs first and without it. The two
    actions the job uses are given the job's token by GitHub, as in any job.
    """
    loaded = _workflow("release.yml")
    (job,) = loaded["jobs"].values()
    building, attaching = (step for step in job["steps"] if "run" in step)

    assert loaded["permissions"] == {"contents": "read"}
    assert job["permissions"] == {"contents": "write"}
    assert building == {"run": f'{_BUILD} --tag "$TAG"'}
    assert attaching == {
        "run": 'python tools/release.py attach "$TAG" "$RUNNER_TEMP/glowrium.zip"',
        "env": {
            "GH_TOKEN": "${{ github.token }}",
            "GH_REPO": "${{ github.repository }}",
        },
    }
    assert job["env"] == {"TAG": "${{ github.ref_name }}"}
    assert "token" not in json.dumps([job["env"], job["steps"][:-1]])
    assert "secrets" not in (_WORKFLOWS / "release.yml").read_text()


def test_the_releases_are_looked_at_every_day_by_a_job_that_can_only_read() -> None:
    """On the clock and by hand, with the token every workflow here has."""
    loaded = _workflow("release-check.yml")
    (job,) = loaded["jobs"].values()
    (daily,) = loaded[_ON]["schedule"]

    assert set(loaded[_ON]) == {"schedule", "workflow_dispatch"}
    assert daily["cron"].split()[2:] == ["*", "*", "*"]
    assert loaded["permissions"] == {"contents": "read"}
    assert "permissions" not in job
    assert _runs(job) == ["python tools/release.py check"]


@pytest.mark.parametrize("workflow", ["release.yml", "release-check.yml"])
def test_a_workflow_of_the_release_is_made_as_the_others_are(workflow: str) -> None:
    """No action the tests' own workflow does not use, each at the same commit.

    A checkout leaves no credentials behind, and nothing GitHub fills in is
    written into a script's text: a tag's name is data, handed over in the
    environment.
    """
    tests_job = _workflow("test.yml")["jobs"]["test"]
    used_by_the_tests = {step["uses"] for step in tests_job["steps"] if "uses" in step}
    (job,) = _workflow(workflow)["jobs"].values()
    uses = [step for step in job["steps"] if "uses" in step]
    (checkout,) = [step for step in uses if "/checkout@" in step["uses"]]

    assert {step["uses"] for step in uses} == used_by_the_tests
    assert all(re.fullmatch(r"\S+@[0-9a-f]{40}", step["uses"]) for step in uses)
    assert checkout["with"] == {"persist-credentials": False}
    assert not [run for run in _runs(job) if "${{" in run]


def test_ci_builds_the_archive_on_every_push_long_before_there_is_a_tag() -> None:
    """With no tag, no release and a token that can only read."""
    loaded = _workflow("test.yml")

    assert _BUILD in _runs(loaded["jobs"]["test"])
    assert loaded["permissions"] == {"contents": "read"}


# --- What HACS is told, and what the documents call the file ---


def test_hacs_is_told_to_take_the_archive_and_not_to_offer_the_default_branch(
    release: ModuleType,
) -> None:
    """Three keys that come together, beside the two there were.

    Told to take a file, HACS looks for it on whatever it is asked to
    install, and the default branch has none: so the branch is not offered.
    """
    told = json.loads((ROOT / "hacs.json").read_text())

    assert told["zip_release"] is True
    assert told["filename"] == release.ARCHIVE
    assert told["hide_default_branch"] is True
    assert set(told) - {"zip_release", "filename", "hide_default_branch"} == {
        "name",
        "homeassistant",
    }


@pytest.mark.parametrize("document", ["README.md", "CONTRIBUTING.md"])
def test_the_documents_call_the_file_what_hacs_is_told_to_take(document: str) -> None:
    """Whoever unpacks it by hand, or cuts a release, looks for it by name."""
    filename = json.loads((ROOT / "hacs.json").read_text())["filename"]

    assert f"`{filename}`" in (ROOT / document).read_text()
