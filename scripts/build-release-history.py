#!/usr/bin/env python3
"""Build a fresh, publishable git history from the current working tree.

The history this repository has now is not publishable. Its commits contain
statements derived from one person's recordings — measured extremes, counts of
sessions, and observations about particular nights — that have since been
removed from the working tree. They are described here by category rather than
quoted, because quoting them in a file destined for the public repository
would publish them again, which is the mistake this whole exercise is about.

**A later commit does not remove them from the history**, and neither does
`commit --amend`: amending leaves the previous versions in the object store as
unreachable blobs, which is exactly what an audit is supposed to be able to
rule out.

The only thing that works is a new root commit in a **new object store**. That
is what this script builds, in a temporary directory, out of the tracked files
and nothing else.

What it does
------------

1. refuses to run unless the working tree is clean;
2. copies every tracked file — and only those — into an empty directory;
3. runs ``git init`` there, so the object store is genuinely new;
4. makes a single root commit, with an address it has checked is a GitHub
   noreply address;
5. runs the privacy guard over the tracked files and over every blob;
6. proves the object store holds **zero unreachable blobs**;
7. prints the remote steps and stops.

What it will not do
-------------------

**It contains no code that can reach the network or the existing history.**
There is no ``push``, no ``remote``, no ``gh``, and it never writes inside the
repository it is run from. That is a property you can check by reading it,
which is the point of it being a script rather than a list of instructions.

Replacing the private remote, making the repository public and enabling
private vulnerability reporting stay manual, and stay yours.

Usage
-----

    python3 scripts/build-release-history.py [--into DIR] [--message-file F]

With no ``--into`` it creates a temporary directory and prints where. Run it
as often as you like: it only ever writes to a directory it has confirmed is
empty.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

#: The address a public root commit must carry. Anything else — a personal
#: address, a work address — is published permanently in every clone, and is
#: the second thing this rebuild exists to fix.
_NOREPLY = re.compile(r"^[0-9]+\+[A-Za-z0-9-]+@users\.noreply\.github\.com$")

#: Paths that are synchronised to a cloud service on this machine. Building a
#: release history inside one uploads it, and — worse for a git directory —
#: lets a sync agent write conflict copies into `.git` while it is being built.
_SYNCED = ("/Documents/", "/Desktop/")

_DEFAULT_MESSAGE = """An independent decoder for prisma VENT50 SD-card data

Reads what a Löwenstein Medical prisma VENT50 home ventilator writes to its SD
card, reports what is in it, and refuses to guess.

No figure measured from a recording appears anywhere in this history: not in
the documentation, not in a source comment, not in a test, and not as a
threshold. Every number that can fail, refuse or reject something is listed in
docs/thresholds.md with its public source, derivation, applicable range,
boundary behaviour and the test that pins it. Where no public derivation
exists, the check reports instead of asserting, or does not exist.

Tests use synthetic fixtures generated at runtime. No file cut from a real
recording exists here.
"""


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if check and result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise SystemExit(f"error: git {' '.join(args)} failed: {message}")
    return result.stdout


def _fail(message: str) -> "SystemExit":
    return SystemExit(f"error: {message}")


def _require_clean_tree(repository: Path) -> None:
    """A release built from a dirty tree is a release nobody can reproduce."""
    status = _git("status", "--porcelain", cwd=repository).decode("utf-8", "replace")
    if status.strip():
        raise _fail(
            "the working tree has uncommitted changes, so what this would "
            "publish is not what anything else has verified:\n"
            + "\n".join(f"    {line}" for line in status.splitlines())
        )


def _resolve_identity(repository: Path, given: str | None) -> tuple[str, str]:
    """The name and address the root commit will carry, checked before use."""
    name = (
        _git("config", "user.name", cwd=repository, check=False)
        .decode("utf-8", "replace")
        .strip()
    )
    # `given if given is not None` rather than `given or`: an explicitly empty
    # --email is a mistake to report, not a reason to quietly fall back on
    # whatever the repository happens to be configured with. Falling back there
    # is how a release ends up authored by an address nobody chose.
    email = (
        given
        if given is not None
        else _git("config", "user.email", cwd=repository, check=False)
        .decode("utf-8", "replace")
        .strip()
    )
    if not name:
        raise _fail("this repository has no user.name configured")
    if not email:
        raise _fail("no address given and this repository has no user.email")
    if not _NOREPLY.match(email):
        raise _fail(
            f"{email!r} is not a GitHub noreply address. A root commit's author "
            "is in every clone forever, so this refuses anything that is not of "
            "the form <id>+<login>@users.noreply.github.com. Pass --email to "
            "give one explicitly"
        )
    return name, email


def _prepare_directory(into: Path | None) -> Path:
    if into is None:
        # Not under the repository, and not under a synced directory: mkdtemp
        # uses TMPDIR, which on macOS and Linux is neither.
        return Path(tempfile.mkdtemp(prefix="prisma-vent-release-"))
    into = into.expanduser().resolve()
    if into.exists() and any(into.iterdir()):
        raise _fail(f"{into} is not empty; this only ever writes into an empty one")
    into.mkdir(parents=True, exist_ok=True)
    return into


def _warn_if_synced(target: Path) -> None:
    if any(part in f"{target}/" for part in _SYNCED):
        print(
            f"warning: {target} looks like a cloud-synchronised directory.\n"
            "         A sync agent can write conflict copies into .git while "
            "this runs,\n         and it uploads the result. Prefer somewhere "
            "outside Documents and Desktop.",
            file=sys.stderr,
        )


#: Git modes this will copy: a regular file, and an executable one. Anything
#: else is refused by name below rather than handled, because a release build
#: is the wrong place to be clever about an entry nobody expected.
_REGULAR_MODES = {b"100644", b"100755"}

_MODE_NAMES = {
    b"120000": "a symbolic link",
    b"160000": "a submodule",
    b"040000": "a directory entry",
}


def _copy_tracked_files(repository: Path, target: Path) -> int:
    """Copy exactly what git tracks, and nothing else.

    Read from ``git ls-files -sz``: ``-z`` so paths split on NUL and never on a
    newline — a git path may legally contain one, and a release build is the
    last place to lose a file to that — and ``-s`` so each entry's **mode** is
    known before anything is copied.

    **The mode is why this is not three lines.** ``shutil.copy2`` follows
    symbolic links, so a tracked link (mode 120000) pointing outside the
    repository would be dereferenced into the release tree as a regular file
    holding whatever it pointed at. This repository has no tracked symlink
    today, which makes that a latent hole rather than an open one — and a
    release build is precisely where a latent hole should be closed, because
    it runs once, unattended, and produces the thing that gets published.

    So only regular and executable files are copied, links are copied as links
    are *not* — they are refused, by name — and ``follow_symlinks=False`` makes
    the copy itself incapable of dereferencing anything even if the mode check
    were somehow wrong.
    """
    listing = _git("ls-files", "-s", "-z", cwd=repository)
    refused: list[str] = []
    copied = 0

    for record in listing.split(b"\0"):
        if not record:
            continue
        # "<mode> <object> <stage>\t<path>"
        head, _, raw_path = record.partition(b"\t")
        mode = head.split(b" ")[0]
        relative = os.fsdecode(raw_path)

        if mode not in _REGULAR_MODES:
            described = _MODE_NAMES.get(mode, f"git mode {os.fsdecode(mode)}")
            refused.append(f"{relative} is {described}")
            continue

        source = repository / relative
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode):
            # The index says regular and the filesystem disagrees. Refuse
            # rather than resolve: one of the two is wrong and this script is
            # not the place to decide which.
            refused.append(
                f"{relative} is tracked as a regular file but is not one on disk"
            )
            continue

        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)
        copied += 1

    if refused:
        raise _fail(
            "the working tree tracks entries this cannot copy safely:\n"
            + "\n".join(f"    {item}" for item in refused)
            + "\n\nA symbolic link would be dereferenced into the release tree, "
            "carrying\n  whatever it points at — possibly from outside the "
            "repository. Remove or\n  replace these before building a release "
            "history."
        )
    return copied


def _unreachable_blobs(target: Path) -> int:
    """Blobs in the store that no ref reaches. For a fresh build this is zero.

    Computed here rather than read out of the guard's output, so the number
    this script asserts on is one it derived itself.
    """
    reachable = set()
    for line in _git("rev-list", "--objects", "--all", cwd=target).splitlines():
        oid = line.split(b" ")[0]
        if len(oid) >= 40:
            reachable.add(oid)

    everything = _git(
        "cat-file",
        "--batch-all-objects",
        "--batch-check=%(objectname) %(objecttype)",
        cwd=target,
    )
    unreachable = 0
    for line in everything.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == b"blob" and parts[0] not in reachable:
            unreachable += 1
    return unreachable


def _run_guard(target: Path, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, str(target / "scripts" / "pre-commit"), *args], cwd=target
    )
    if result.returncode != 0:
        raise _fail(
            f"the privacy guard refused the rebuilt history "
            f"({' '.join(args) or 'staged'}). Nothing was published; fix the "
            "working tree and run this again"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/build-release-history.py",
        description=(
            "Build a fresh publishable git history from the tracked files. "
            "Touches neither this repository's history nor any remote."
        ),
    )
    parser.add_argument(
        "--into",
        type=Path,
        metavar="DIR",
        help="an empty directory to build in; a temporary one by default",
    )
    parser.add_argument(
        "--message-file",
        type=Path,
        metavar="FILE",
        help="the root commit's message; a built-in one by default",
    )
    parser.add_argument(
        "--email",
        metavar="ADDRESS",
        help="the GitHub noreply address for the root commit",
    )
    args = parser.parse_args()

    repository = Path(
        os.fsdecode(_git("rev-parse", "--show-toplevel").strip())
    ).resolve()
    _require_clean_tree(repository)
    name, email = _resolve_identity(repository, args.email)

    message = _DEFAULT_MESSAGE
    if args.message_file is not None:
        message = args.message_file.read_text(encoding="utf-8")

    target = _prepare_directory(args.into)
    _warn_if_synced(target)

    copied = _copy_tracked_files(repository, target)

    # A new store, not a copy of the old one. Nothing under the source's .git
    # is read, and nothing is moved: this is the whole reason the script
    # exists rather than an amend.
    _git("init", "-q", ".", cwd=target)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=target)
    _git("config", "user.name", name, cwd=target)
    _git("config", "user.email", email, cwd=target)
    _git("config", "commit.gpgsign", "false", cwd=target)

    # The guard's machine-specific patterns live inside .git and are never
    # committed, so they have to be carried across deliberately.
    patterns = repository / ".git" / "decoder-secret-patterns"
    if patterns.is_file():
        shutil.copy2(patterns, target / ".git" / "decoder-secret-patterns")
        os.chmod(target / ".git" / "decoder-secret-patterns", 0o600)

    for script in ("pre-commit", "check-links.py"):
        path = target / "scripts" / script
        if path.exists():
            path.chmod(0o755)

    _git("add", "-A", cwd=target)
    _run_guard(target)                       # staged, as the hook would
    _git("commit", "-q", "-m", message, cwd=target)

    _run_guard(target, "--all")
    _run_guard(target, "--history")

    unreachable = _unreachable_blobs(target)
    if unreachable:
        raise _fail(
            f"the rebuilt store holds {unreachable} unreachable blob(s). A "
            "release history must hold none: they are not pushed, but they are "
            "in the directory, and their presence means something was written "
            "and then rewritten during the build"
        )

    head = os.fsdecode(_git("rev-parse", "HEAD", cwd=target).strip())
    print()
    print("Built a fresh history. Nothing has been published.")
    print()
    print(f"  directory     {target}")
    print(f"  root commit   {head}")
    print(f"  author        {name} <{email}>")
    print(f"  files         {copied} tracked file(s), one commit")
    print("  unreachable   0 blob(s)")
    print()
    print("Check it yourself before anything leaves this machine:")
    print()
    print(f"  git -C {target} log --format='%H %an <%ae>'")
    print(f"  git -C {target} log --stat")
    print(f"  cd {target} && python3 -m pytest -q")
    print()
    print("Then, and only when you have decided to:")
    print()
    print("  1. replace or recreate the PRIVATE remote and push this history;")
    print("  2. keep the repository private until you are satisfied with it;")
    print("  3. after making it public, enable private vulnerability reporting")
    print("     (it cannot be enabled while a repository is private) and check")
    print("     that the API returns 204 rather than 404.")
    print()
    print("This script does none of those, and contains no code that could.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
