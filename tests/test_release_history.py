"""Tests for the release-history builder at ``scripts/build-release-history.py``.

The script runs once, unattended, and produces the thing that gets published.
That makes it the worst place for a latent hole and the best place for tests
that are about refusals rather than about output.

Every fixture is a throwaway git repository built here. Nothing touches the
repository these tests live in, and nothing in the script can reach a network.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build-release-history.py"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="the script builds a git history"
)

NOREPLY = "1+someone@users.noreply.github.com"


def _git(repository: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A minimal committed repository with the script available to it."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "README.md").write_text("# a repository\n")
    shutil.copy2(SCRIPT, root / "scripts" / SCRIPT.name)
    # The builder runs the privacy guard out of the tree it is building, so the
    # tree has to carry one.
    guard = SCRIPT.parent / "pre-commit"
    shutil.copy2(guard, root / "scripts" / "pre-commit")

    _git(root, "init", "-q", ".")
    _git(root, "config", "user.name", "Someone")
    _git(root, "config", "user.email", NOREPLY)
    _git(root, "add", "-A")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "first")
    return root


def build(repository: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(repository / "scripts" / SCRIPT.name), *args],
        cwd=repository,
        capture_output=True,
    )


# --------------------------------------------------------------------------
# Tracked symlinks
#
# shutil.copy2 follows symbolic links. A tracked link pointing outside the
# repository would therefore be dereferenced into the release tree as a
# regular file holding whatever it pointed at — and the release tree is what
# gets published.
# --------------------------------------------------------------------------


def test_a_tracked_symlink_is_refused(repository, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("content from outside the repository\n")
    (repository / "link.txt").symlink_to(secret)
    _git(repository, "add", "-A")
    _git(repository, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "link")

    result = build(repository, "--into", str(tmp_path / "release"))
    assert result.returncode == 1
    assert b"symbolic link" in result.stderr


def test_the_refusal_names_the_offending_entry(repository, tmp_path):
    """A refusal nobody can act on is close to no refusal at all."""
    (repository / "link.txt").symlink_to(repository / "README.md")
    _git(repository, "add", "-A")
    _git(repository, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "link")

    result = build(repository, "--into", str(tmp_path / "release"))
    assert b"link.txt" in result.stderr


def test_no_content_from_outside_reaches_the_release_tree(repository, tmp_path):
    """The property, not the message: the payoff must not happen."""
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = "THIS IS OUTSIDE THE REPOSITORY"
    (outside / "secret.txt").write_text(marker + "\n")
    (repository / "link.txt").symlink_to(outside / "secret.txt")
    _git(repository, "add", "-A")
    _git(repository, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "link")

    release = tmp_path / "release"
    build(repository, "--into", str(release))

    for path in release.rglob("*"):
        if path.is_file():
            assert marker not in path.read_text(errors="replace")


def test_a_symlink_is_refused_even_when_it_points_inside(repository, tmp_path):
    """The mode is refused as a mode, not judged by where it happens to aim."""
    (repository / "link.txt").symlink_to("README.md")
    _git(repository, "add", "-A")
    _git(repository, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "link")

    result = build(repository, "--into", str(tmp_path / "release"))
    assert result.returncode == 1
    assert b"symbolic link" in result.stderr


# --------------------------------------------------------------------------
# The other refusals
# --------------------------------------------------------------------------


def test_a_dirty_working_tree_is_refused(repository, tmp_path):
    """What it would publish would not be what anything else verified."""
    (repository / "README.md").write_text("# changed\n")

    result = build(repository, "--into", str(tmp_path / "release"))
    assert result.returncode == 1
    assert b"uncommitted changes" in result.stderr


def test_an_address_that_is_not_a_noreply_one_is_refused(repository, tmp_path):
    """A root commit's author is in every clone forever."""
    result = build(
        repository, "--into", str(tmp_path / "release"), "--email", "someone@example.com"
    )
    assert result.returncode == 1
    assert b"noreply" in result.stderr


@pytest.mark.parametrize(
    "address",
    [
        "someone@users.noreply.github.example",
        "someone@users.noreply.github.com",     # no numeric id
        "1+someone@github.com",
        "",
    ],
)
def test_only_a_properly_shaped_noreply_address_is_accepted(
    repository, tmp_path, address
):
    result = build(repository, "--into", str(tmp_path / "release"), "--email", address)
    assert result.returncode == 1


def test_a_non_empty_target_directory_is_refused(repository, tmp_path):
    """It only ever writes into a directory it has confirmed is empty."""
    release = tmp_path / "release"
    release.mkdir()
    (release / "something").write_text("already here\n")

    result = build(repository, "--into", str(release))
    assert result.returncode == 1
    assert b"not empty" in result.stderr
    assert (release / "something").read_text() == "already here\n"


# --------------------------------------------------------------------------
# What it produces when it does run
# --------------------------------------------------------------------------


def test_it_builds_a_single_root_commit_in_a_new_store(repository, tmp_path):
    release = tmp_path / "release"
    result = build(repository, "--into", str(release))
    assert result.returncode == 0, result.stderr

    log = subprocess.run(
        ["git", "log", "--format=%H %P %ae"],
        cwd=release,
        capture_output=True,
        check=True,
    ).stdout.decode()
    lines = [line for line in log.splitlines() if line.strip()]
    assert len(lines) == 1
    commit, _, rest = lines[0].partition(" ")
    parents, _, email = rest.rpartition(" ")
    assert parents.strip() == "", "a release history must have a root commit"
    assert email == NOREPLY


def test_the_new_store_holds_no_unreachable_blob(repository, tmp_path):
    """An amend leaves the previous versions behind; a fresh build must not."""
    release = tmp_path / "release"
    assert build(repository, "--into", str(release)).returncode == 0

    reachable = subprocess.run(
        ["git", "rev-list", "--objects", "--all"],
        cwd=release,
        capture_output=True,
        check=True,
    ).stdout.split()
    everything = subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"],
        cwd=release,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    blobs = {line.split()[0] for line in everything if line.split()[1] == b"blob"}
    assert blobs - set(reachable) == set()


def test_it_never_writes_inside_the_repository_it_is_run_from(repository, tmp_path):
    """The source repository must come out of this untouched."""
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, check=True
    ).stdout
    listing_before = sorted(p.name for p in repository.iterdir())

    assert build(repository, "--into", str(tmp_path / "release")).returncode == 0

    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, check=True
    ).stdout
    assert before == after
    assert sorted(p.name for p in repository.iterdir()) == listing_before


def test_the_script_contains_no_way_to_reach_a_remote(repository):
    """Read as a property of the text, because that is how a reader checks it.

    The script's claim is that replacing the remote stays manual. That claim is
    only worth anything if nothing in it can push, and the cheapest way to keep
    it true is to fail here when somebody adds the convenience later.
    """
    source = SCRIPT.read_text()
    body = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    _, _, after_docstring = body.partition('"""')
    _, _, code = after_docstring.partition('"""')
    for forbidden in ('"push"', "'push'", '"remote"', "'remote'", '"gh"', "'gh'"):
        assert forbidden not in code, f"{forbidden} appears in the script's code"


def test_the_tracked_files_arrive_and_nothing_else_does(repository, tmp_path):
    release = tmp_path / "release"
    assert build(repository, "--into", str(release)).returncode == 0

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=repository, capture_output=True, check=True
    ).stdout.decode().split()
    arrived = sorted(
        str(p.relative_to(release))
        for p in release.rglob("*")
        if p.is_file() and ".git" not in p.parts
    )
    assert arrived == sorted(tracked)


def test_an_executable_file_keeps_its_mode(repository, tmp_path):
    """Mode 100755 is copied, not refused along with the modes that are."""
    release = tmp_path / "release"
    assert build(repository, "--into", str(release)).returncode == 0
    assert os.access(release / "scripts" / "pre-commit", os.X_OK)
