"""Tests for the privacy guard at ``scripts/pre-commit``.

Every fixture here is invented. The "recordings" are a few bytes of ZIP magic
or a hand-built EDF-shaped header; nothing is cut from a device file, and the
only serial-shaped strings are the reserved all-zero one and obvious nonsense.

The guard is exercised as a program, in throwaway git repositories, because
that is how it runs: as a hook, in CI, and in an audit before publication. A
unit test of its predicates would not have caught the defect these tests exist
for.

**The defect.** The guard used to convert git's NUL-separated path list to
newline-separated text with ``tr '\\0' '\\n'``. A newline is a legal character
in a git path, so a file named ``evil<newline>name.bin`` became two paths,
neither of which existed, and a staged file beginning with ZIP magic passed
with exit code 0. Every hostile name below is parametrised over the whole
check, not only over the newline that was reproduced.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parent.parent / "scripts" / "pre-commit"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="the guard is a git hook and needs git"
)

#: Names git accepts and a line-oriented reader does not survive. The newline
#: is the one that was reproduced; the rest are the same class of mistake.
HOSTILE_NAMES = [
    pytest.param("evil\nname.bin", id="newline"),
    pytest.param("evil\tname.bin", id="tab"),
    pytest.param("-evil-name.bin", id="leading-dash"),
    pytest.param("evil name with spaces.bin", id="spaces"),
    pytest.param("bös-datei-Ω.bin", id="non-ascii"),
    pytest.param("quoted\"name.bin", id="double-quote"),
    pytest.param("back\\slash.bin", id="backslash"),
]

#: A ZIP local file header. Device day archives are ZIPs, which is why the
#: guard refuses anything starting with this.
ZIP_MAGIC = b"PK\x03\x04" + b"\x00" * 26

#: Assembled from two pieces rather than written out, so **this file does not
#: itself contain a serial-shaped literal**. The guard would otherwise flag its
#: own test suite, and the answer to that is not another entry in the
#: exemption list: an exemption list is a thing that grows until the check
#: covers nothing. The digits are invented.
SERIAL_SHAPED = b"SN" + b"12345678"


def _run_git(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repository, check=True, capture_output=True
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A throwaway repository with the guard available as a program."""
    root = tmp_path / "repo"
    root.mkdir()
    _run_git(root, "init", "-q", ".")
    _run_git(root, "config", "user.email", "nobody@example.invalid")
    _run_git(root, "config", "user.name", "Nobody")
    _run_git(root, "commit", "-q", "--allow-empty", "-m", "root")
    return root


def guard(repository: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GUARD), *args],
        cwd=repository,
        capture_output=True,
    )


def write(repository: Path, name: str, payload: bytes) -> None:
    """Write a file by *name*, which may be hostile, and stage it."""
    path = repository / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    # `--` so a leading-dash name is a path and not an option, and the literal
    # name rather than a glob, so nothing is quietly normalised.
    _run_git(repository, "add", "--", name)


# --------------------------------------------------------------------------
# The defect: hostile names must not disappear from the scan
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_recording_under_a_hostile_name_is_refused(repository, name):
    """The reproduction, generalised. Exit 0 here was the bug."""
    write(repository, name, ZIP_MAGIC)
    result = guard(repository)
    assert result.returncode == 1
    assert b"ZIP magic" in result.stderr


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_hostile_name_is_scanned_in_all_mode_too(repository, name):
    """`--all` is what CI runs, so it must not have its own blind spot."""
    write(repository, name, ZIP_MAGIC)
    _run_git(repository, "commit", "-q", "-m", "add")
    result = guard(repository, "--all")
    assert result.returncode == 1
    assert b"ZIP magic" in result.stderr


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_harmless_file_under_a_hostile_name_still_passes(repository, name):
    """The guard must not become a name filter. Only content decides."""
    write(repository, name, b"an ordinary note about nothing in particular\n")
    result = guard(repository)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_the_hostile_name_is_reported_readably(repository, name):
    """A finding nobody can act on is close to no finding at all."""
    write(repository, name, ZIP_MAGIC)
    result = guard(repository)
    # Escaped rather than raw, so a newline in a path cannot forge a second
    # line of the report.
    assert b"evil" in result.stderr or b"datei" in result.stderr or (
        b"quoted" in result.stderr or b"back" in result.stderr
    )
    assert result.stderr.count(b"  - ") == 1


def test_a_newline_name_counts_as_one_file_not_two(repository):
    """The split that caused the defect, observed directly in the count."""
    write(repository, "one\ntwo.txt", b"harmless\n")
    assert guard(repository).returncode == 0
    _run_git(repository, "commit", "-q", "-m", "add")
    counted = guard(repository, "--all")
    assert b"1 tracked file(s) scanned" in counted.stdout


# --------------------------------------------------------------------------
# The content checks themselves
# --------------------------------------------------------------------------


def test_anything_under_data_is_refused(repository):
    write(repository, "data/whatever.txt", b"harmless text\n")
    result = guard(repository)
    assert result.returncode == 1
    assert b"lives under data/" in result.stderr


def test_a_nested_data_directory_is_refused(repository):
    write(repository, "some/data/whatever.txt", b"harmless text\n")
    result = guard(repository)
    assert result.returncode == 1
    assert b"lives under data/" in result.stderr


def test_a_file_merely_named_data_is_not_refused(repository):
    """`data` as a *file* name is not `data/` as a directory."""
    write(repository, "notes/data", b"harmless text\n")
    assert guard(repository).returncode == 0


def _edf_header() -> bytes:
    """An EDF-shaped file: version field, and a dd.mm.yy date at offset 168.

    Padded past the fixed 256-byte header, as a real one is by its per-signal
    headers — the check only looks at files larger than that, since 256 bytes
    of anything is not a recording.
    """
    header = bytearray(b" " * 512)
    header[0:8] = b"1       "
    header[168:176] = b"01.01.20"
    return bytes(header)


def test_an_edf_shaped_file_is_refused_whatever_it_is_called(repository):
    write(repository, "innocent.txt", _edf_header())
    result = guard(repository)
    assert result.returncode == 1
    assert b"EDF/.wmedf" in result.stderr


def test_a_large_proto_payload_is_refused(repository):
    write(repository, "statistic.proto", b"\x08\x01" * 4096)
    result = guard(repository)
    assert result.returncode == 1
    assert b"large .proto" in result.stderr


def test_a_small_proto_schema_is_not_refused(repository):
    write(repository, "schema.proto", b'syntax = "proto3";\nmessage A {}\n')
    assert guard(repository).returncode == 0


def test_a_large_binary_is_refused(repository):
    write(repository, "blob.dat", b"\x01\x00\x02" * 400_000)
    result = guard(repository)
    assert result.returncode == 1
    assert b"large binary file" in result.stderr


def test_a_large_text_file_is_not_refused(repository):
    write(repository, "long.md", b"a line of ordinary prose\n" * 60_000)
    assert guard(repository).returncode == 0


def test_device_xml_structures_are_refused(repository):
    write(repository, "settings.xml", b'<?xml version="1.0"?><Device_sn>x</Device_sn>')
    result = guard(repository)
    assert result.returncode == 1
    assert b"device-specific XML" in result.stderr


def test_prose_mentioning_those_element_names_is_not_refused(repository):
    """Documentation about the format is exactly what this repository is for."""
    write(
        repository,
        "docs/format.md",
        b"The archive carries a <Device_sn> element, which is the serial.\n",
    )
    assert guard(repository).returncode == 0


def test_a_serial_shaped_identifier_is_refused(repository):
    write(repository, "notes.txt", b"the card was " + SERIAL_SHAPED + b"\n")
    result = guard(repository)
    assert result.returncode == 1
    assert b"serial-shaped identifier" in result.stderr


def test_this_test_file_carries_no_serial_shaped_literal():
    """The guard scans its own tests, so they must not trip it.

    Written as a test rather than left to the hook, because the hook is the
    thing that would be bypassed with --no-verify while somebody worked out
    what was wrong.
    """
    import re

    source = Path(__file__).read_bytes()
    for match in re.finditer(rb"\bSN[0-9]{6,}\b", source):
        assert re.match(rb"^SN0+$", match.group()), match.group()


def test_the_reserved_all_zero_serial_is_allowed(repository):
    """Synthetic fixtures need a serial-shaped path to exercise the layout."""
    write(repository, "notes.txt", b"the fixture path is WM30640/SN00000000/\n")
    assert guard(repository).returncode == 0


def test_the_wm_article_number_is_not_flagged(repository):
    """It identifies the model, is the same on every unit, and is documented."""
    write(repository, "notes.txt", b"the card layout starts at WM30640/\n")
    assert guard(repository).returncode == 0


# --------------------------------------------------------------------------
# The uncommitted local pattern list
# --------------------------------------------------------------------------


def test_a_locally_configured_pattern_is_matched(repository):
    (repository / ".git" / "decoder-secret-patterns").write_bytes(
        b"# a comment line\nnot-a-real-secret-[0-9]+\n"
    )
    write(repository, "notes.txt", b"mentions not-a-real-secret-42 in passing\n")
    result = guard(repository)
    assert result.returncode == 1
    assert b"locally configured sensitive pattern" in result.stderr


def test_the_local_pattern_itself_is_never_printed(repository):
    """Naming what matched would publish the thing the file exists to hide."""
    (repository / ".git" / "decoder-secret-patterns").write_bytes(
        b"not-a-real-secret-[0-9]+\n"
    )
    write(repository, "notes.txt", b"mentions not-a-real-secret-42 in passing\n")
    result = guard(repository)
    assert b"not-a-real-secret" not in result.stderr
    assert b"not-a-real-secret" not in result.stdout


def test_an_invalid_local_pattern_stops_the_guard(repository):
    """A pattern that never compiles is a check that silently never runs."""
    (repository / ".git" / "decoder-secret-patterns").write_bytes(b"[unclosed\n")
    write(repository, "notes.txt", b"harmless\n")
    result = guard(repository)
    assert result.returncode != 0
    assert b"not a valid regex" in result.stderr


# --------------------------------------------------------------------------
# History mode: contents, not only names
# --------------------------------------------------------------------------


def test_a_recording_removed_before_head_is_still_found(repository):
    """The gap the name-based CI checks left.

    Committed as `payload.bin` and deleted again, it is absent from the working
    tree and from every tracked name — and still in the history that
    publication publishes.
    """
    write(repository, "payload.bin", ZIP_MAGIC)
    _run_git(repository, "commit", "-q", "--no-verify", "-m", "add")
    _run_git(repository, "rm", "-q", "payload.bin")
    _run_git(repository, "commit", "-q", "--no-verify", "-m", "remove")

    assert guard(repository, "--all").returncode == 0      # nothing tracked now
    result = guard(repository, "--history")
    assert result.returncode == 1
    assert b"ZIP magic" in result.stderr


def test_a_recording_renamed_before_head_is_still_found(repository):
    """An extension check would have been satisfied by the rename alone."""
    write(repository, "recording.wmedf", _edf_header())
    _run_git(repository, "commit", "-q", "--no-verify", "-m", "add")
    _run_git(repository, "mv", "recording.wmedf", "notes.txt")
    _run_git(repository, "commit", "-q", "--no-verify", "-m", "rename")

    result = guard(repository, "--history")
    assert result.returncode == 1
    assert b"EDF/.wmedf" in result.stderr


def test_a_path_under_data_in_history_is_found(repository):
    write(repository, "data/recording.txt", b"harmless text\n")
    _run_git(repository, "commit", "-q", "--no-verify", "-m", "add")
    _run_git(repository, "rm", "-q", "-r", "data")
    _run_git(repository, "commit", "-q", "--no-verify", "-m", "remove")

    result = guard(repository, "--history")
    assert result.returncode == 1
    assert b"lives under data/" in result.stderr


def test_history_mode_reports_an_object_inventory(repository):
    """An audit has to be able to state what the store holds, not hope."""
    write(repository, "notes.txt", b"harmless\n")
    _run_git(repository, "commit", "-q", "-m", "add")

    result = guard(repository, "--history")
    assert result.returncode == 0
    assert b"object store:" in result.stdout
    assert b"reachable from a ref" in result.stdout


def test_history_mode_counts_unreachable_blobs(repository):
    """They are not pushed, but they are in the clone and in any copy of it."""
    write(repository, "orphan.txt", b"written, then abandoned\n")
    result = guard(repository, "--history")
    assert result.returncode == 0
    assert b"1 not" in result.stdout


def test_a_hostile_name_in_history_is_scanned(repository):
    write(repository, "evil\nname.bin", ZIP_MAGIC)
    _run_git(repository, "commit", "-q", "--no-verify", "-m", "add")
    _run_git(repository, "rm", "-q", "--", "evil\nname.bin")
    _run_git(repository, "commit", "-q", "--no-verify", "-m", "remove")

    result = guard(repository, "--history")
    assert result.returncode == 1
    assert b"ZIP magic" in result.stderr


# --------------------------------------------------------------------------
# Housekeeping
# --------------------------------------------------------------------------


def test_the_guard_is_executable():
    """It is installed as a hook by symlink, so the bit has to be on the file."""
    assert os.access(GUARD, os.X_OK)


def test_nothing_staged_is_not_a_failure(repository):
    assert guard(repository).returncode == 0


def test_an_unknown_option_is_a_usage_error(repository):
    assert guard(repository, "--nonsense").returncode == 2


def test_the_two_scanning_modes_are_mutually_exclusive(repository):
    assert guard(repository, "--all", "--history").returncode == 2


def test_the_guard_does_not_flag_this_repository():
    """The real one, not a fixture: a guard that fires on its own tree is noise.

    Skipped when the suite is being run from an unpacked source distribution,
    which is a directory of files rather than a checkout. That is a real way to
    run these tests — the sdist ships them deliberately — so it must not fail
    there for a reason that has nothing to do with the guard.
    """
    root = GUARD.parent.parent
    inside_a_checkout = subprocess.run(
        ["git", "rev-parse", "--git-dir"], cwd=root, capture_output=True
    )
    if inside_a_checkout.returncode != 0:
        pytest.skip("not a git checkout, so there is no tracked tree to scan")

    result = subprocess.run(
        [sys.executable, str(GUARD), "--all"], cwd=root, capture_output=True
    )
    assert result.returncode == 0, result.stderr
