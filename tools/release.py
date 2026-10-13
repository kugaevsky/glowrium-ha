"""Build the archive a release carries, and put it on the release.

    python tools/release.py archive REF OUT [--tag TAG]   # build it
    python tools/release.py attach TAG FILE                # put it on GitHub
    python tools/release.py check                          # is it on each?

A release of this integration is one file, `glowrium.zip`: `hacs.json` tells
HACS to take it, and GitHub counts how often it is fetched. The file is built
on the push of a version tag and put on a draft of the release; the
maintainer writes the notes and publishes.

Nothing here publishes a release, edits the notes of one, or makes, moves or
deletes a tag.

What is said below of HACS was read in its source and not seen to happen;
what was read, and where, is in ADR 0017.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
import zipfile

ROOT = Path(__file__).resolve().parent.parent
# The directory a release is made of, as git names it.
INTEGRATION = "custom_components/glowrium"
# The one file of a release. hacs.json names it to HACS, and GitHub names a
# release's file after the file it is given.
ARCHIVE = "glowrium.zip"

# What a draft says until the maintainer has written the release's notes.
_NOTES = (
    "A draft made for the tag by the Release workflow. It carries the "
    "archive HACS installs. These words stand where the release's notes go: "
    "write them before publishing."
)
# How `gh` says that a tag has no release, a draft counted (read in its
# source at 2.101.0: `ErrReleaseNotFound`).
_NO_RELEASE = "release not found"

# `gh`, or what stands in for it: given a command's words, returns what it
# printed, and raises NotDoneError with what it said when it failed.
Gh = Callable[..., str]


class NotDoneError(Exception):
    """What was asked is not done, and the message says why."""


def _run(command: list[str], cwd: Path | None = None) -> bytes:
    """Return what a command prints, or refuse with what it said in failing."""
    done = subprocess.run(  # noqa: S603 - git or gh, and arguments built here
        command, cwd=cwd, check=False, capture_output=True
    )
    if done.returncode:
        said = done.stderr.decode(errors="replace").strip()
        raise NotDoneError(said or f"{command[0]} failed and said nothing.")
    return done.stdout


def git(*arguments: str, repo: Path = ROOT) -> bytes:
    """Return what git prints when asked this of a repository."""
    return _run(["git", *arguments], repo)


def github(*arguments: str) -> str:
    """Return what `gh` prints when asked this of GitHub."""
    return _run(["gh", *arguments]).decode()


def build(ref: str, out: Path, repo: Path = ROOT) -> None:
    """Write the integration as it is at a commit into an archive.

    Out of git's objects, and never out of the working directory: what is in
    the archive is what is tracked under the integration's directory at that
    commit, each file as git holds it and at the archive's root. Not through
    `git archive`, which leaves out what its attributes tell it to.
    """
    # A tag with words of its own is an object beside the commit it names.
    commit = git("rev-parse", "--verify", f"{ref}^{{commit}}", repo=repo)
    commit = commit.decode().strip()
    committed = int(git("show", "--no-patch", "--format=%ct", commit, repo=repo))
    listing = git("ls-tree", "-r", "-z", f"{commit}:{INTEGRATION}", repo=repo)
    with zipfile.ZipFile(out, "w") as archive:
        for entry in listing.rstrip(b"\0").split(b"\0"):
            described, name = entry.split(b"\t", 1)
            mode, _, blob = described.decode().split()
            # The commit's time, in UTC, and the mode git has: nothing of
            # the day or the machine this runs on.
            file = zipfile.ZipInfo(name.decode(), time.gmtime(committed)[:6])
            file.compress_type = zipfile.ZIP_DEFLATED
            file.external_attr = int(mode, 8) << 16
            archive.writestr(file, git("cat-file", "blob", blob, repo=repo))


def check_tag(tag: str, ref: str, repo: Path = ROOT) -> None:
    """Refuse a tag that is not `v` and the version the manifest has at it.

    HACS shows the tag and Home Assistant shows the manifest; a release in
    which the two differ is one no reader can name.
    """
    manifest = git("show", f"{ref}:{INTEGRATION}/manifest.json", repo=repo)
    version = json.loads(manifest)["version"]
    if tag != f"v{version}":
        raise NotDoneError(
            f"The tag {tag} is not the manifest's version: the manifest at "
            f"{ref} has {version}, and a release of it is tagged v{version}."
        )


def _release_of(tag: str, gh: Gh) -> dict[str, Any] | None:
    """Return the release a tag has, a draft as well, or None if it has none."""
    try:
        found = gh("release", "view", tag, "--json", "isDraft,assets")
    except NotDoneError as failed:
        # Only this answer means there is none. Any other - no access, no
        # network - says nothing of the release, and nothing is made on it.
        if _NO_RELEASE in str(failed):
            return None
        raise
    release: dict[str, Any] = json.loads(found)
    return release


def attach(tag: str, archive: Path, gh: Gh = github) -> str:
    """Put the archive on the tag's release, and return what was done.

    A tag with no release gets a draft; a release without the archive gets
    it; a draft's archive is replaced. The archive of a published release is
    left: replacing it would start its count again and change the bytes
    under whoever has installed it.
    """
    if archive.name != ARCHIVE:
        raise NotDoneError(
            f"A release's file is named {ARCHIVE}, and this is {archive}."
        )
    release = _release_of(tag, gh)
    if release is None:
        gh(
            *("release", "create", tag, str(archive)),
            *("--draft", "--verify-tag", "--title", tag, "--notes", _NOTES),
        )
        return f"Made a draft of {tag} that carries {ARCHIVE}."
    carried = [file["name"] for file in release["assets"]]
    if ARCHIVE not in carried:
        gh("release", "upload", tag, str(archive))
        done = f"Put {ARCHIVE} on the release of {tag}."
    elif release["isDraft"]:
        gh("release", "upload", tag, str(archive), "--clobber")
        done = f"Replaced {ARCHIVE} on the draft of {tag}."
    else:
        done = (
            f"The release of {tag} is published and has its {ARCHIVE}: left as it is."
        )
    others = [name for name in carried if name != ARCHIVE]
    if others:
        # Nothing here takes a file off a release. HACS counts the first one.
        done += f" It also carries {', '.join(others)}: a release is to carry one file."
    return done


def check_releases(gh: Gh = github) -> str:
    """Hold every published release to what its own hacs.json promises.

    HACS reads hacs.json at the version it installs. A release whose
    hacs.json tells it to take a file has to carry that file - exactly one,
    under that name and not empty: HACS does not fall back to the tag's
    source, and counts the first file a release has. A release that promises
    none passes, and a draft is not looked at.

    Read through the API alone. The archive itself is never fetched: that
    would be counted as a download of it.
    """
    listed = gh("api", "--paginate", "--slurp", "repos/{owner}/{repo}/releases")
    published = [
        release
        for page in json.loads(listed)
        for release in page
        if not release["draft"]
    ]
    promising = 0
    broken = []
    for release in published:
        tag = release["tag_name"]
        told = json.loads(
            gh(
                *("api", "-H", "Accept: application/vnd.github.raw+json"),
                f"repos/{{owner}}/{{repo}}/contents/hacs.json?ref={tag}",
            )
        )
        if not told.get("zip_release"):
            continue
        promising += 1
        promised = told.get("filename")
        carried = {file["name"]: file["size"] for file in release["assets"]}
        if list(carried) == [promised] and carried[promised] > 0:
            continue
        files = ", ".join(f"{name} ({size} bytes)" for name, size in carried.items())
        broken.append(
            f"{tag} promises {promised or 'a file it does not name'} "
            f"and carries {files or 'no file'}."
        )
    if broken:
        raise NotDoneError("\n".join(broken))
    return (
        f"{len(published)} published, {promising} of them promising an "
        "archive: each carries what it promises."
    )


def main(argv: list[str], *, repo: Path = ROOT, gh: Gh = github) -> int:
    """Do the one thing asked for, and say what was done or why it was not."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    archive = commands.add_parser("archive", help="build the archive of a commit")
    archive.add_argument("ref", help="the commit, as git names it")
    archive.add_argument("out", type=Path, help="where to write the archive")
    archive.add_argument("--tag", help="the tag the commit is released under")
    attaching = commands.add_parser("attach", help="put the archive on a release")
    attaching.add_argument("tag", help="the tag of the release")
    attaching.add_argument("file", type=Path, help="the archive")
    commands.add_parser("check", help="hold each release to what it promises")
    asked = parser.parse_args(argv)

    try:
        if asked.command == "check":
            done = check_releases(gh)
        elif asked.command == "attach":
            done = attach(asked.tag, asked.file, gh)
        else:
            if asked.tag is not None:
                check_tag(asked.tag, asked.ref, repo=repo)
            build(asked.ref, asked.out, repo=repo)
            done = f"Built {asked.out} from {asked.ref}."
    except NotDoneError as refused:
        print(refused, file=sys.stderr)
        return 1
    print(done)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
