"""Tests for copying a card into a private directory.

Sources are built at runtime in a temporary directory. Nothing here touches a
real card, and the "card" fixtures hold arbitrary bytes.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from prisma_vent.copy_card import (
    MANIFEST_NAME,
    CopyError,
    CopySafetyError,
    execute,
    plan_copy,
)


def _fingerprint_of(items):
    """The module's canonical inventory fingerprint, for building fixtures."""
    from prisma_vent.copy_card import _inventory_fingerprint

    return _inventory_fingerprint(items)


def inventory_for(plan, card: Path):
    """The ``(path, size, sha256)`` triples a manifest should record.

    Computed here with plain hashlib rather than by calling into the module, so
    a test that checks the manifest is not checking the code against itself.
    """
    import hashlib

    return [
        (
            entry.relative_path,
            entry.size,
            hashlib.sha256((card / entry.relative_path).read_bytes()).hexdigest(),
        )
        for entry in plan.entries
    ]


def make_card(root: Path, *, extra: dict[str, bytes] | None = None) -> Path:
    """A directory shaped like the card, with arbitrary contents."""
    card = root / "card"
    files = {
        "WM30640/plugin/100000000/dcm.zip": b"PK-not-really",
        "WM30640/SN00000000/0123_2020-01-01.zip": b"archive bytes",
        "WM30640/SN00000000/trendcurve/0123_2020-01-01.tc": b"trend bytes",
        "WM30640/SN00000000/battery/0123_0001_2020-01-01.wmedf": b"battery",
        "WM30640/SN00000000/logs/kern.log": b"log line\n",
        "WM30640/SN00000000/device.xml": b"<xml/>",
    }
    files.update(extra or {})
    for relative, payload in files.items():
        target = card / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return card


# --------------------------------------------------------------------------
# Nothing is lost for being unfamiliar
# --------------------------------------------------------------------------


def test_every_regular_file_is_copied(tmp_path):
    card = make_card(tmp_path)
    plan = plan_copy(card, tmp_path / "out")
    result = execute(plan)
    assert len(result.copied) == 6
    for entry in plan.entries:
        assert (tmp_path / "out" / entry.relative_path).exists()


def test_an_unfamiliar_file_is_copied_and_marked(tmp_path):
    """A later firmware adding a file must not vanish from the copy.

    Recognition decides what the manifest says, never what gets copied.
    """
    card = make_card(tmp_path, extra={"WM30640/SN00000000/newthing.dat": b"?"})
    plan = plan_copy(card, tmp_path / "out")
    assert [e.relative_path for e in plan.unrecognised] == [
        "WM30640/SN00000000/newthing.dat"
    ]
    execute(plan)
    assert (tmp_path / "out" / "WM30640/SN00000000/newthing.dat").read_bytes() == b"?"

    manifest = json.loads((tmp_path / "out" / MANIFEST_NAME).read_text())
    marked = {f["path"]: f["recognised"] for f in manifest["files"]}
    assert marked["WM30640/SN00000000/newthing.dat"] is False
    assert marked["WM30640/SN00000000/device.xml"] is True


def test_symlinks_are_refused_not_followed(tmp_path):
    card = make_card(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"not part of the card")
    (card / "WM30640" / "link.txt").symlink_to(outside)

    plan = plan_copy(card, tmp_path / "out")
    assert any("symbolic link" in s for s in plan.skipped_non_regular)
    assert not any(e.relative_path.endswith("link.txt") for e in plan.entries)
    execute(plan)
    assert not (tmp_path / "out" / "WM30640" / "link.txt").exists()


def test_a_named_pipe_is_refused(tmp_path):
    card = make_card(tmp_path)
    os.mkfifo(card / "WM30640" / "pipe")
    plan = plan_copy(card, tmp_path / "out")
    assert any("named pipe" in s for s in plan.skipped_non_regular)


def test_operating_system_clutter_is_ignored(tmp_path):
    card = make_card(tmp_path, extra={"WM30640/.DS_Store": b"junk"})
    plan = plan_copy(card, tmp_path / "out")
    assert "WM30640/.DS_Store" in plan.ignored
    assert not any(e.relative_path.endswith(".DS_Store") for e in plan.entries)


# --------------------------------------------------------------------------
# Nothing is overwritten, nothing half-written counts
# --------------------------------------------------------------------------


def test_the_source_is_not_modified(tmp_path):
    card = make_card(tmp_path)
    before = {
        p.relative_to(card).as_posix(): (p.stat().st_size, p.read_bytes())
        for p in card.rglob("*")
        if p.is_file()
    }
    execute(plan_copy(card, tmp_path / "out"))
    after = {
        p.relative_to(card).as_posix(): (p.stat().st_size, p.read_bytes())
        for p in card.rglob("*")
        if p.is_file()
    }
    assert before == after


def test_rerunning_verifies_rather_than_recopying(tmp_path):
    """A resumed copy: identical files are checked, not written again."""
    card = make_card(tmp_path)
    execute(plan_copy(card, tmp_path / "out"))
    second = execute(plan_copy(card, tmp_path / "out"))
    assert second.copied == []
    assert len(second.already_present) == 6


def test_a_differing_destination_file_is_refused(tmp_path):
    card = make_card(tmp_path)
    execute(plan_copy(card, tmp_path / "out"))
    target = tmp_path / "out" / "WM30640/SN00000000/device.xml"
    target.chmod(0o600)
    target.write_bytes(b"<different/>")

    with pytest.raises(CopySafetyError, match="different content"):
        execute(plan_copy(card, tmp_path / "out"))
    # Still there, untouched.
    assert target.read_bytes() == b"<different/>"


def test_a_leftover_partial_is_discarded(tmp_path):
    """A half-written file is re-copied, never resumed from.

    Its contents cannot be trusted, and copying again is cheap next to
    guessing how much of it is good.
    """
    card = make_card(tmp_path)
    out = tmp_path / "out"
    execute(plan_copy(card, out))

    # Stand in for a run interrupted on this file: the finished copy is gone
    # and a partial is in its place.
    target = out / "WM30640/SN00000000/device.xml"
    target.chmod(0o600)
    target.unlink()
    stale = target.with_name("device.xml.partial")
    stale.write_bytes(b"half a fi")

    execute(plan_copy(card, out))
    assert not stale.exists()
    assert target.read_bytes() == b"<xml/>"


def test_an_interrupted_copy_can_be_resumed(tmp_path):
    """The manifest is written before copying, precisely so this works.

    Written only at the end, an interrupted run would leave a non-empty
    directory with no manifest — which the safety check refuses as somebody
    else's folder, making the copy impossible to finish.
    """
    import json
    import os

    from prisma_vent.copy_card import MANIFEST_NAME, CopyResult, _write_manifest

    card = make_card(tmp_path)
    out = tmp_path / "out"
    plan = plan_copy(card, out)

    # Simulate an interruption: destination prepared, manifest marked
    # incomplete, one file copied. The manifest is written through a directory
    # handle, exactly as the real run writes it.
    out.mkdir(parents=True)
    root_fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _write_manifest(
            plan, CopyResult(), root_fd, inventory_for(plan, card), complete=False
        )
    finally:
        os.close(root_fd)
    first = out / plan.entries[0].relative_path
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes((card / plan.entries[0].relative_path).read_bytes())

    assert json.loads((out / MANIFEST_NAME).read_text())["complete"] is False

    result = execute(plan_copy(card, out))
    assert len(result.already_present) == 1
    assert len(result.copied) == 5
    assert json.loads(result.manifest_path.read_text())["complete"] is True


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------


def test_identical_source_and_destination_are_refused(tmp_path):
    card = make_card(tmp_path)
    with pytest.raises(CopySafetyError, match="same directory"):
        plan_copy(card, card)


def test_destination_inside_source_is_refused(tmp_path):
    card = make_card(tmp_path)
    with pytest.raises(CopySafetyError, match="inside the source"):
        plan_copy(card, card / "WM30640" / "copy")


def test_source_inside_destination_is_refused(tmp_path):
    card = make_card(tmp_path)
    with pytest.raises(CopySafetyError, match="inside the destination"):
        plan_copy(card, tmp_path)


def test_a_non_empty_foreign_destination_is_refused(tmp_path):
    card = make_card(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "someones-notes.txt").write_text("mine")
    with pytest.raises(CopySafetyError, match="not empty"):
        plan_copy(card, out)


def test_a_destination_this_tool_wrote_is_accepted(tmp_path):
    card = make_card(tmp_path)
    execute(plan_copy(card, tmp_path / "out"))
    plan_copy(card, tmp_path / "out")  # the manifest identifies it as ours


def test_a_missing_source_is_an_error(tmp_path):
    with pytest.raises(CopyError, match="does not exist"):
        plan_copy(tmp_path / "nope", tmp_path / "out")


def test_an_empty_source_is_an_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(CopyError, match="no regular files"):
        plan_copy(empty, tmp_path / "out")


# --------------------------------------------------------------------------
# The destination is private, and describes itself
# --------------------------------------------------------------------------


def test_destination_is_owner_only(tmp_path):
    card = make_card(tmp_path)
    out = tmp_path / "out"
    execute(plan_copy(card, out))
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert stat.S_IMODE((out / "WM30640").stat().st_mode) == 0o700


def test_copies_are_read_only(tmp_path):
    card = make_card(tmp_path)
    out = tmp_path / "out"
    execute(plan_copy(card, out))
    mode = stat.S_IMODE((out / "WM30640/SN00000000/device.xml").stat().st_mode)
    assert not mode & stat.S_IWUSR


def test_manifest_records_hashes_and_counts(tmp_path):
    import hashlib

    card = make_card(tmp_path)
    out = tmp_path / "out"
    result = execute(plan_copy(card, out))
    manifest = json.loads(result.manifest_path.read_text())

    assert manifest["file_count"] == 6
    by_path = {f["path"]: f for f in manifest["files"]}
    entry = by_path["WM30640/SN00000000/device.xml"]
    assert entry["sha256"] == hashlib.sha256(b"<xml/>").hexdigest()
    assert entry["size"] == 6
    assert "modified" in entry
    assert manifest["adapter"]["package_version"]


def test_manifest_names_what_was_not_copied(tmp_path):
    card = make_card(tmp_path, extra={"WM30640/.DS_Store": b"junk"})
    (card / "WM30640" / "link").symlink_to(tmp_path / "elsewhere")
    out = tmp_path / "out"
    manifest = json.loads(execute(plan_copy(card, out)).manifest_path.read_text())
    assert any("link" in s for s in manifest["skipped_non_regular"])
    assert any(".DS_Store" in s for s in manifest["ignored"])


# --------------------------------------------------------------------------
# The operating system's own directories
# --------------------------------------------------------------------------


def _card_with_os_directories(tmp_path):
    source = tmp_path / "card"
    (source / "WM30640" / "SN00000000").mkdir(parents=True)
    (source / "WM30640" / "SN00000000" / "0123_2020-03-04.zip").write_bytes(b"real")

    trash = source / ".Trashes" / "501"
    trash.mkdir(parents=True)
    (trash / "deleted-recording.zip").write_bytes(b"deleted therapy data")
    (trash / "another.wmedf").write_bytes(b"also deleted")
    (source / ".Trashes" / "top-level.txt").write_bytes(b"deleted too")

    (source / ".Spotlight-V100" / "Store-V2").mkdir(parents=True)
    (source / ".Spotlight-V100" / "Store-V2" / "index.db").write_bytes(b"index")
    (source / ".fseventsd").mkdir()
    (source / ".fseventsd" / "0000000000000001").write_bytes(b"events")
    return source


def test_nothing_inside_the_trash_is_planned(tmp_path):
    """Copying deleted files is precisely what this tool promises not to do."""
    plan = plan_copy(_card_with_os_directories(tmp_path), tmp_path / "out")
    planned = {entry.relative_path for entry in plan.entries}
    assert planned == {"WM30640/SN00000000/0123_2020-03-04.zip"}
    assert not any(".Trashes" in name for name in planned)


def test_the_index_directories_are_not_entered_either(tmp_path):
    plan = plan_copy(_card_with_os_directories(tmp_path), tmp_path / "out")
    planned = {entry.relative_path for entry in plan.entries}
    assert not any(".Spotlight-V100" in name or ".fseventsd" in name
                   for name in planned)


def test_the_skipped_directories_are_named_in_the_plan(tmp_path):
    """Silently dropping them would be its own kind of quiet omission."""
    plan = plan_copy(_card_with_os_directories(tmp_path), tmp_path / "out")
    ignored = " ".join(plan.ignored)
    assert ".Trashes/ (directory, not descended into)" in plan.ignored
    assert ".Spotlight-V100" in ignored and ".fseventsd" in ignored


def test_nothing_from_them_reaches_the_destination(tmp_path):
    source = _card_with_os_directories(tmp_path)
    destination = tmp_path / "out"
    execute(plan_copy(source, destination))
    copied = {p.relative_to(destination).as_posix()
              for p in destination.rglob("*") if p.is_file()}
    assert not any(".Trashes" in name for name in copied)
    assert "WM30640/SN00000000/0123_2020-03-04.zip" in copied


def test_they_are_recorded_as_ignored_in_the_manifest(tmp_path):
    import json

    source = _card_with_os_directories(tmp_path)
    destination = tmp_path / "out"
    result = execute(plan_copy(source, destination))
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    ignored = " ".join(manifest["ignored"])
    assert ".Trashes" in ignored


def test_a_symlinked_directory_is_reported_and_not_followed(tmp_path):
    source = tmp_path / "card"
    (source / "WM30640").mkdir(parents=True)
    (source / "WM30640" / "device.xml").write_bytes(b"<x/>")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"not on the card")
    (source / "link").symlink_to(outside, target_is_directory=True)

    plan = plan_copy(source, tmp_path / "out")
    planned = {entry.relative_path for entry in plan.entries}
    assert planned == {"WM30640/device.xml"}
    assert any("link/" in name and "symbolic link" in name
               for name in plan.skipped_non_regular)


def test_a_symlink_named_like_an_ignored_directory_is_still_not_followed(tmp_path):
    source = tmp_path / "card"
    (source / "WM30640").mkdir(parents=True)
    (source / "WM30640" / "device.xml").write_bytes(b"<x/>")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"not on the card")
    (source / ".Trashes").symlink_to(outside, target_is_directory=True)

    plan = plan_copy(source, tmp_path / "out")
    planned = {entry.relative_path for entry in plan.entries}
    assert planned == {"WM30640/device.xml"}


# --------------------------------------------------------------------------
# The destination escape
#
# A reproduced exploit, and each of its parts closed separately. The whole was:
# leave any file named copy-manifest.json in the destination, make a directory
# the copy will need a symlink to somewhere else, run the copy — and health
# data lands outside the destination while the external directory is chmodded
# to 0700. Every test below asserts on where bytes went, not only on the
# exception, because an exception raised after a write is not a fix.
# --------------------------------------------------------------------------


def _resumable_copy(tmp_path, name="out"):
    """A destination this tool really did write, so resuming it is legitimate."""
    card = make_card(tmp_path)
    destination = tmp_path / name
    execute(plan_copy(card, destination))
    return card, destination


def _other_card(root: Path) -> Path:
    """A card holding entirely different files from :func:`make_card`.

    Disjoint on purpose: nothing collides, so nothing in the per-file checks
    would have noticed the two being merged.
    """
    card = root / "card"
    files = {
        "WM30640/SN00000000/0900_2021-09-09.zip": b"second-card archive",
        "WM30640/SN00000000/trendcurve/0900_2021-09-09.tc": b"second-card trend",
        "WM30640/SN00000000/second-card-only.log": b"second-card log\n",
    }
    for relative, payload in files.items():
        target = card / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return card


def _external(tmp_path, name="elsewhere"):
    outside = tmp_path / name
    outside.mkdir()
    (outside / "pre-existing.txt").write_bytes(b"someone else's file")
    os.chmod(outside, 0o755)
    return outside


def _external_is_untouched(outside: Path) -> bool:
    """Nothing written into it, and its permissions not changed."""
    entries = {p.name for p in outside.iterdir()}
    mode = stat.S_IMODE(os.stat(outside).st_mode)
    return entries == {"pre-existing.txt"} and mode == 0o755


def test_the_reproduced_exploit_writes_nothing_outside_the_destination(tmp_path):
    """The whole chain, exactly as it was reproduced, now refused."""
    card = make_card(tmp_path)
    destination = tmp_path / "out"
    destination.mkdir()
    outside = _external(tmp_path)

    (destination / MANIFEST_NAME).write_bytes(b"x")     # any file by that name
    (destination / "WM30640").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CopySafetyError):
        execute(plan_copy(card, destination))

    assert _external_is_untouched(outside)


def test_a_foreign_file_named_like_a_manifest_does_not_make_a_destination_ours(
    tmp_path,
):
    """Presence used to be the whole test. It is now the least of them."""
    card = make_card(tmp_path)
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / MANIFEST_NAME).write_bytes(b"not our manifest")
    (destination / "someones-notes.txt").write_bytes(b"keep me")

    with pytest.raises(CopySafetyError, match="not valid JSON"):
        plan_copy(card, destination)
    assert (destination / "someones-notes.txt").read_bytes() == b"keep me"


def test_a_malformed_manifest_is_refused_rather_than_repaired(tmp_path):
    card = make_card(tmp_path)
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / MANIFEST_NAME).write_text(json.dumps({"manifest_version": 1}))
    (destination / "other.txt").write_bytes(b"x")

    with pytest.raises(CopySafetyError, match="older build"):
        plan_copy(card, destination)


def test_a_manifest_that_is_a_json_array_is_refused(tmp_path):
    card = make_card(tmp_path)
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / MANIFEST_NAME).write_text("[]")
    (destination / "other.txt").write_bytes(b"x")

    with pytest.raises(CopySafetyError, match="not a JSON object"):
        plan_copy(card, destination)


def test_a_manifest_missing_its_required_fields_is_refused(tmp_path):
    from prisma_vent.copy_card import MANIFEST_VERSION

    card = make_card(tmp_path)
    for missing, pattern in (
        ("complete", "whether its copy finished"),
        ("adapter", "names no adapter version"),
        ("files", "carries no file list"),
        ("source_inventory", "records no source inventory"),
        ("source_inventory_sha256", "carries no source inventory fingerprint"),
    ):
        destination = tmp_path / f"out-{missing}"
        destination.mkdir()
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "complete": False,
            "adapter": {"package_version": "0.0.0", "decoder_schema_version": 1},
            "files": [],
            "source_inventory": [],
            # The fingerprint of an empty inventory, so every case above fails
            # for the field it is about rather than for a missing fingerprint.
            "source_inventory_sha256": _fingerprint_of([]),
        }
        del manifest[missing]
        (destination / MANIFEST_NAME).write_text(json.dumps(manifest))
        (destination / "other.txt").write_bytes(b"x")
        with pytest.raises(CopySafetyError, match=pattern):
            plan_copy(card, destination)


def test_a_manifest_from_a_different_card_is_refused(tmp_path):
    """Archive filenames repeat across cards, so mixing two is not recoverable."""
    _, destination = _resumable_copy(tmp_path)
    other = _other_card(tmp_path / "second")

    with pytest.raises(CopySafetyError, match="different source"):
        plan_copy(other, destination)


def test_a_second_card_at_the_same_mount_path_is_refused(tmp_path):
    """The reproduction: a mount path is not a card.

    Copy card A from /Volumes/CARD, eject it, insert card B, run again. The
    resolved source path is identical both times, so identifying the source by
    that path made the destination look resumable — and the two cards' files
    are disjoint, so nothing collided and both ended up in one backup.
    """
    import shutil

    mount = tmp_path / "mount"
    destination = tmp_path / "backup"
    card_a = make_card(tmp_path / "a")
    card_b = _other_card(tmp_path / "b")

    def insert(card):
        if mount.exists():
            shutil.rmtree(mount)
        shutil.copytree(card, mount)

    insert(card_a)
    execute(plan_copy(mount, destination))
    a_files = {p.name for p in destination.rglob("*") if p.is_file()}

    insert(card_b)
    with pytest.raises(CopySafetyError, match="mount path is not a card"):
        execute(plan_copy(mount, destination))

    # The decisive assertion is not the exception. It is that nothing from the
    # second card reached the backup.
    after = {p.name for p in destination.rglob("*") if p.is_file()}
    assert after == a_files
    assert not any("second-card" in name for name in after)


def test_the_refusal_names_the_mount_path_confusion(tmp_path):
    """A message that only says "different source" leaves the user guessing."""
    import shutil

    mount = tmp_path / "mount"
    destination = tmp_path / "backup"
    shutil.copytree(make_card(tmp_path / "a"), mount)
    execute(plan_copy(mount, destination))
    shutil.rmtree(mount)
    shutil.copytree(_other_card(tmp_path / "b"), mount)

    with pytest.raises(CopySafetyError) as raised:
        plan_copy(mount, destination)
    message = str(raised.value)
    assert "mount path is not a card" in message
    assert "empty directory" in message


def test_a_card_that_has_grown_since_the_copy_still_resumes(tmp_path):
    """A device writes to its card between an interruption and its resumption.

    Refusing that would make the resume path useless for the one situation it
    exists for, so growth is allowed and only disappearance or change is not.
    """
    card, destination = _resumable_copy(tmp_path)
    (card / "WM30640" / "SN00000000" / "0124_2020-01-02.zip").write_bytes(b"new day")

    plan = plan_copy(card, destination)
    assert plan.destination_state.resumable
    result = execute(plan)
    assert result.copied == ["WM30640/SN00000000/0124_2020-01-02.zip"]


def test_a_source_whose_file_changed_size_is_refused(tmp_path):
    """Same path, different size: not the source this backup was started from."""
    card, destination = _resumable_copy(tmp_path)
    (card / "WM30640" / "SN00000000" / "device.xml").write_bytes(b"<xml/><more/>")

    with pytest.raises(CopySafetyError, match="has since changed"):
        plan_copy(card, destination)


def test_a_source_missing_one_file_is_refused(tmp_path):
    """Partial disappearance is not the wholesale case, and says so differently."""
    card, destination = _resumable_copy(tmp_path)
    (card / "WM30640" / "SN00000000" / "device.xml").unlink()

    with pytest.raises(CopySafetyError, match="has since changed"):
        plan_copy(card, destination)


def test_the_manifest_records_the_inventory_before_copying_starts(tmp_path):
    """An interrupted copy has no complete file list, so the inventory is what
    a resume has to compare against. It is therefore written up front."""
    import os

    from prisma_vent.copy_card import CopyResult, _write_manifest

    card = make_card(tmp_path)
    out = tmp_path / "out"
    plan = plan_copy(card, out)
    out.mkdir(parents=True)
    root_fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _write_manifest(
            plan, CopyResult(), root_fd, inventory_for(plan, card), complete=False
        )
    finally:
        os.close(root_fd)

    manifest = json.loads((out / MANIFEST_NAME).read_text())
    assert manifest["complete"] is False
    assert manifest["files"] == []
    assert {e["path"] for e in manifest["source_inventory"]} == {
        e.relative_path for e in plan.entries
    }
    # Content, not only path and size: a same-size rewrite has to be visible.
    assert all(
        len(e["sha256"]) == 64 for e in manifest["source_inventory"]
    )


def test_the_resolved_source_path_is_recorded_but_decides_nothing(tmp_path):
    """It is kept for a person reading the folder, not for the tool."""
    card, destination = _resumable_copy(tmp_path)
    manifest = json.loads((destination / MANIFEST_NAME).read_text())
    assert manifest["source_resolved"] == str(card.resolve())

    # Same card, reached by a different spelling of the same path: still fine,
    # because the path is not what is compared.
    plan = plan_copy(card.parent / "." / card.name, destination)
    assert plan.destination_state.resumable


def test_the_same_card_named_differently_still_resumes(tmp_path):
    """Identity is the resolved path, so two spellings of one card are one card."""
    card, destination = _resumable_copy(tmp_path)
    spelled_differently = card.parent / "." / card.name

    plan = plan_copy(spelled_differently, destination)
    assert plan.destination_state.resumable
    result = execute(plan)
    assert result.copied == []
    assert len(result.already_present) == 6


def test_a_symlinked_manifest_is_never_followed_or_overwritten(tmp_path):
    """The old code opened it by path and would have written through it."""
    card = make_card(tmp_path)
    destination = tmp_path / "out"
    destination.mkdir()
    outside = _external(tmp_path)
    target = outside / "pre-existing.txt"
    (destination / MANIFEST_NAME).symlink_to(target)

    with pytest.raises(CopySafetyError, match="symbolic link"):
        plan_copy(card, destination)
    assert target.read_bytes() == b"someone else's file"


def test_a_manifest_that_is_a_directory_is_refused(tmp_path):
    card = make_card(tmp_path)
    destination = tmp_path / "out"
    (destination / MANIFEST_NAME).mkdir(parents=True)

    with pytest.raises(CopySafetyError, match="not a regular file"):
        plan_copy(card, destination)


def test_a_symlinked_nested_directory_is_refused_and_not_written_through(tmp_path):
    """The escape that actually moved health data out of the destination."""
    card, destination = _resumable_copy(tmp_path)
    outside = _external(tmp_path)

    # Remove the real subtree and put a link to somewhere else in its place.
    import shutil

    shutil.rmtree(destination / "WM30640")
    (destination / "WM30640").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CopySafetyError, match="symbolic link or is not a directory"):
        execute(plan_copy(card, destination))

    assert _external_is_untouched(outside)


def test_a_regular_file_where_a_directory_must_go_is_refused(tmp_path):
    card, destination = _resumable_copy(tmp_path)
    import shutil

    shutil.rmtree(destination / "WM30640")
    (destination / "WM30640").write_bytes(b"not a directory")

    with pytest.raises(CopySafetyError, match="symbolic link or is not a directory"):
        execute(plan_copy(card, destination))
    assert (destination / "WM30640").read_bytes() == b"not a directory"


def test_a_symlinked_target_file_is_refused_rather_than_written_through(tmp_path):
    card, destination = _resumable_copy(tmp_path)
    outside = _external(tmp_path)
    target = outside / "pre-existing.txt"

    victim = destination / "WM30640" / "SN00000000" / "device.xml"
    victim.unlink()
    victim.symlink_to(target)

    with pytest.raises(CopySafetyError, match="as a symbolic link"):
        execute(plan_copy(card, destination))
    assert target.read_bytes() == b"someone else's file"


def test_a_manifest_swapped_between_planning_and_execution_is_caught(tmp_path):
    """The plan is not a licence to write; the manifest is re-read first.

    The directory itself is untouched here, so only the manifest's contents
    differ — the substitution that a directory-identity check alone would miss.
    """
    card, destination = _resumable_copy(tmp_path)
    plan = plan_copy(card, destination)

    # A *valid* manifest of ours, differing only in a field that does not
    # affect any other check — so the digest comparison is the only thing that
    # can catch it, which is the point.
    manifest = json.loads((destination / MANIFEST_NAME).read_text())
    manifest["created"] = "2000-01-01T00:00:00+00:00"
    (destination / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")

    with pytest.raises(CopySafetyError, match="changed between planning"):
        execute(plan)


def test_a_manifest_replaced_by_a_foreign_one_after_planning_is_caught(tmp_path):
    """Invalid JSON is caught by the schema check rather than the digest."""
    card, destination = _resumable_copy(tmp_path)
    plan = plan_copy(card, destination)

    (destination / MANIFEST_NAME).write_bytes(b"swapped after planning")

    with pytest.raises(CopySafetyError, match="not valid JSON"):
        execute(plan)


def test_a_manifest_replaced_by_a_symlink_after_planning_is_caught(tmp_path):
    """And the swap is refused before anything is written through the link."""
    card, destination = _resumable_copy(tmp_path)
    plan = plan_copy(card, destination)
    outside = _external(tmp_path)
    target = outside / "pre-existing.txt"

    (destination / MANIFEST_NAME).unlink()
    (destination / MANIFEST_NAME).symlink_to(target)

    with pytest.raises(CopySafetyError, match="symbolic link"):
        execute(plan)
    assert target.read_bytes() == b"someone else's file"


def test_the_destination_directory_being_swapped_is_caught(tmp_path):
    """A different inode at the same path is a different destination."""
    card, destination = _resumable_copy(tmp_path)
    plan = plan_copy(card, destination)

    replacement = tmp_path / "replacement"
    replacement.mkdir()
    # Carry the manifest across, so only the directory's identity differs.
    (replacement / MANIFEST_NAME).write_bytes((destination / MANIFEST_NAME).read_bytes())
    import shutil

    shutil.rmtree(destination)
    replacement.rename(destination)

    with pytest.raises(CopySafetyError, match="no longer the directory"):
        execute(plan)


def test_the_manifest_is_written_atomically(tmp_path):
    """A torn manifest would strand the copy the manifest exists to rescue."""
    card, destination = _resumable_copy(tmp_path)

    leftovers = [p.name for p in destination.iterdir() if p.name.endswith(".partial")]
    assert leftovers == []
    manifest = json.loads((destination / MANIFEST_NAME).read_text())
    assert manifest["complete"] is True
    assert manifest["source_resolved"] == str(card.resolve())


def test_every_copied_file_stays_below_the_resolved_destination(tmp_path):
    """The property the whole rewrite exists to hold."""
    card, destination = _resumable_copy(tmp_path)
    root = destination.resolve()

    written = [p for p in destination.rglob("*") if p.is_file()]
    assert written
    for path in written:
        assert not path.is_symlink()
        assert root in path.resolve().parents


# --------------------------------------------------------------------------
# The no-overwrite race
#
# The destination is checked for an existing entry before a file is copied,
# and the finished temporary was then installed with `os.replace`, which
# overwrites. A file appearing under that name during the copy — another
# process, another run of this tool, a sync client — was silently destroyed,
# contradicting the property this module states in its own docstring.
# --------------------------------------------------------------------------


def test_installing_over_an_existing_file_is_refused(tmp_path):
    """The primitive, tested directly: link fails rather than replaces."""
    import os

    from prisma_vent.copy_card import _install_without_replacing

    root = tmp_path / "d"
    root.mkdir()
    (root / "target").write_bytes(b"somebody else's file")
    (root / "target.partial").write_bytes(b"ours")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(CopySafetyError, match="appeared in the destination"):
            _install_without_replacing(fd, "target.partial", "target")
    finally:
        os.close(fd)

    assert (root / "target").read_bytes() == b"somebody else's file"
    # Ours is cleaned up; theirs is not touched.
    assert not (root / "target.partial").exists()


def test_installing_over_a_symlink_is_refused(tmp_path):
    """A symlink under the name must not be followed and written through."""
    import os

    from prisma_vent.copy_card import _install_without_replacing

    root = tmp_path / "d"
    root.mkdir()
    outside = _external(tmp_path)
    (root / "target").symlink_to(outside / "pre-existing.txt")
    (root / "target.partial").write_bytes(b"ours")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(CopySafetyError, match="appeared in the destination"):
            _install_without_replacing(fd, "target.partial", "target")
    finally:
        os.close(fd)

    assert (outside / "pre-existing.txt").read_bytes() == b"someone else's file"


def test_installing_over_a_directory_is_refused(tmp_path):
    import os

    from prisma_vent.copy_card import _install_without_replacing

    root = tmp_path / "d"
    root.mkdir()
    (root / "target").mkdir()
    (root / "target" / "keep").write_bytes(b"keep me")
    (root / "target.partial").write_bytes(b"ours")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(CopySafetyError):
            _install_without_replacing(fd, "target.partial", "target")
    finally:
        os.close(fd)

    assert (root / "target" / "keep").read_bytes() == b"keep me"


def test_installing_onto_a_free_name_succeeds_and_removes_the_partial(tmp_path):
    """The ordinary path still works, and leaves no temporary behind."""
    import os

    from prisma_vent.copy_card import _install_without_replacing

    root = tmp_path / "d"
    root.mkdir()
    (root / "target.partial").write_bytes(b"ours")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _install_without_replacing(fd, "target.partial", "target")
    finally:
        os.close(fd)

    assert (root / "target").read_bytes() == b"ours"
    assert not (root / "target.partial").exists()


def test_a_file_appearing_mid_copy_is_not_overwritten(tmp_path, monkeypatch):
    """The race itself, forced open at the point where it exists.

    The source is hashed just before it is copied, so hashing is where a
    competing writer is made to appear. With `os.replace` the run reported
    success and the other file was gone.
    """
    import prisma_vent.copy_card as copy_card

    card = make_card(tmp_path)
    destination = tmp_path / "out"
    plan = plan_copy(card, destination)
    victim_relative = plan.entries[0].relative_path

    original = copy_card._digest_fd
    intruded = []

    def hash_and_intrude(source_fd, shown):
        digest = original(source_fd, shown)
        if not intruded:
            intruded.append(True)
            target = destination / victim_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"written by somebody else mid-copy")
        return digest

    monkeypatch.setattr(copy_card, "_digest_fd", hash_and_intrude)

    with pytest.raises(CopySafetyError):
        execute(plan)

    assert (destination / victim_relative).read_bytes() == (
        b"written by somebody else mid-copy"
    )


def test_no_partial_files_survive_a_refused_install(tmp_path, monkeypatch):
    """A discarded copy must not leave a half-file that looks finished."""
    import prisma_vent.copy_card as copy_card

    card = make_card(tmp_path)
    destination = tmp_path / "out"
    plan = plan_copy(card, destination)
    victim_relative = plan.entries[0].relative_path

    original = copy_card._digest_fd
    intruded = []

    def hash_and_intrude(source_fd, shown):
        digest = original(source_fd, shown)
        if not intruded:
            intruded.append(True)
            target = destination / victim_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"intruder")
        return digest

    monkeypatch.setattr(copy_card, "_digest_fd", hash_and_intrude)
    with pytest.raises(CopySafetyError):
        execute(plan)

    leftovers = [p for p in destination.rglob("*.partial")]
    assert leftovers == []


# --------------------------------------------------------------------------
# The source-side escape
#
# The mirror image of the destination escape above. Planning checked each file
# with lstat, so it refused symlinks — and execution then re-opened the file by
# path, twice, once to hash and once to copy. Replacing a planned file with a
# symlink in between made the copier follow it and read content from outside
# the source, successfully and silently. SECURITY.md calls reading outside the
# named directory a vulnerability.
#
# Every test below asserts on what ended up in the destination, not only that
# an exception was raised: an exception after the bytes have been copied is not
# a fix.
# --------------------------------------------------------------------------


def _outside_secret(tmp_path, payload=b"<xml/>", name="elsewhere"):
    """A file outside the source, the same size as a planned source file.

    Same size on purpose: a size check alone must not be what saves us, or the
    test would pass for the wrong reason.
    """
    outside = tmp_path / name
    outside.mkdir(exist_ok=True)
    secret = outside / "secret.dat"
    secret.write_bytes(payload)
    return secret


def _planned(plan, relative):
    return next(e for e in plan.entries if e.relative_path == relative)


def test_a_source_file_replaced_by_a_symlink_after_planning_is_refused(tmp_path):
    """The reproduced escape: nothing outside the source may be read."""
    card = make_card(tmp_path)
    destination = tmp_path / "out"
    plan = plan_copy(card, destination)

    victim = "WM30640/SN00000000/device.xml"
    assert _planned(plan, victim).size == 6          # b"<xml/>"
    secret = _outside_secret(tmp_path)               # also 6 bytes
    (card / victim).unlink()
    (card / victim).symlink_to(secret)

    with pytest.raises(CopySafetyError, match="symbolic link"):
        execute(plan)

    # The escape's payoff was a copy of the external file under the card's name.
    assert not (destination / victim).exists()
    assert not any(
        p.read_bytes() == b"<xml/>" and "secret" not in p.name
        for p in destination.rglob("*.dat")
    )


def test_no_byte_from_outside_the_source_reaches_the_destination(tmp_path):
    """Stated as a property over the whole destination, not per file."""
    card = make_card(tmp_path)
    destination = tmp_path / "out"
    plan = plan_copy(card, destination)

    marker = b"THIS BYTE PATTERN IS OUTSIDE THE SOURCE"
    secret = _outside_secret(tmp_path, payload=marker)
    victim = "WM30640/SN00000000/logs/kern.log"
    (card / victim).unlink()
    (card / victim).symlink_to(secret)

    with pytest.raises(CopySafetyError):
        execute(plan)

    for path in destination.rglob("*"):
        if path.is_file():
            assert marker not in path.read_bytes()


def test_a_nested_source_directory_replaced_by_a_symlink_is_refused(tmp_path):
    """A whole subtree swapped, not a single file."""
    import shutil

    card = make_card(tmp_path)
    destination = tmp_path / "out"
    plan = plan_copy(card, destination)

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "kern.log") .write_bytes(b"log line\n")
    shutil.rmtree(card / "WM30640" / "SN00000000" / "logs")
    (card / "WM30640" / "SN00000000" / "logs").symlink_to(
        outside, target_is_directory=True
    )

    with pytest.raises(CopySafetyError, match="symbolic link or is not a directory"):
        execute(plan)
    assert not (destination / "WM30640/SN00000000/logs/kern.log").exists()


def test_a_source_file_swapped_after_hashing_still_copies_the_planned_bytes(
    tmp_path, monkeypatch
):
    """The two passes read one descriptor, so a swap cannot get between them.

    The name is repointed at a same-size file outside the source after the hash
    is taken. Nothing is refused here, and that is the **correct** outcome
    rather than a gap: the descriptor was opened and verified once, so both
    passes read the file that was planned, and the file the name now points at
    is never opened at all.

    An earlier version of this test expected a refusal. That expectation was
    wrong. Refusing would mean a card the device is actively writing to makes
    copies fail, and it would buy nothing — the security property is that
    nothing outside the source is ever read, and holding the descriptor is what
    delivers it.
    """
    import prisma_vent.copy_card as copy_card

    card = make_card(tmp_path)
    destination = tmp_path / "out"
    plan = plan_copy(card, destination)
    victim = "WM30640/SN00000000/device.xml"
    planned_bytes = (card / victim).read_bytes()
    secret = _outside_secret(tmp_path, payload=b"SWAPT!")   # 6 bytes, as planned

    original = copy_card._digest_fd
    seen = []
    swapped = []

    def hash_then_swap(source_fd, shown):
        digest = original(source_fd, shown)
        if shown == victim:
            seen.append(True)
            # The inventory pass hashes every file first, so the *second* call
            # for this file is the one inside the copy. Intruding on the first
            # would only prove that the copy re-opens by name, which it does
            # not do any more.
            if len(seen) == 2 and not swapped:
                swapped.append(True)
                (card / victim).unlink()
                (card / victim).symlink_to(secret)
        return digest

    monkeypatch.setattr(copy_card, "_digest_fd", hash_then_swap)
    execute(plan)
    monkeypatch.undo()

    assert swapped, "the swap never happened, so this proved nothing"
    # The planned content, not what the name points at now.
    assert (destination / victim).read_bytes() == planned_bytes
    for path in destination.rglob("*"):
        if path.is_file():
            assert b"SWAPT!" not in path.read_bytes()


def test_a_source_file_rewritten_in_place_while_read_is_refused(
    tmp_path, monkeypatch
):
    """Same inode, same size, new content — the case identity cannot see.

    Neither the inode nor the size changes here, so only the before/after
    modification time and the content digest can catch it. Both are checked.
    """
    import os as _os
    import prisma_vent.copy_card as copy_card

    card = make_card(tmp_path)
    destination = tmp_path / "out"
    plan = plan_copy(card, destination)
    victim = "WM30640/SN00000000/device.xml"

    original = copy_card._digest_fd
    seen = []
    rewritten = []

    def hash_then_rewrite(source_fd, shown):
        digest = original(source_fd, shown)
        if shown == victim:
            seen.append(True)
        if len(seen) == 2 and not rewritten and shown == victim:
            rewritten.append(True)
            target = card / victim
            stale = _os.stat(target)
            with open(target, "r+b") as handle:
                handle.write(b"ZZZZZZ")          # same length as b"<xml/>" + pad
            _os.utime(target, ns=(stale.st_atime_ns, stale.st_mtime_ns + 10**9))
        return digest

    monkeypatch.setattr(copy_card, "_digest_fd", hash_then_rewrite)

    with pytest.raises(CopySafetyError, match="changed while it was being copied"):
        execute(plan)
    assert rewritten
    assert not (destination / victim).exists()


def test_a_refused_source_run_leaves_no_copy_that_looks_complete(tmp_path):
    """A refusal must not leave a directory a later run treats as finished."""

    card = make_card(tmp_path)
    destination = tmp_path / "out"
    plan = plan_copy(card, destination)

    secret = _outside_secret(tmp_path)
    victim = "WM30640/SN00000000/device.xml"
    (card / victim).unlink()
    (card / victim).symlink_to(secret)

    with pytest.raises(CopySafetyError):
        execute(plan)

    # Stronger than it used to be: the source is hashed before the manifest is
    # written, so a source this run will not read leaves the destination
    # completely untouched rather than holding an incomplete manifest.
    assert not (destination / MANIFEST_NAME).exists()
    assert [p for p in destination.rglob("*") if p.is_file()] == []


def test_a_source_root_swapped_between_planning_and_copying_is_refused(tmp_path):
    """A mount path is not a card, on the read side as well as the write side."""
    card = make_card(tmp_path)
    destination = tmp_path / "out"
    plan = plan_copy(card, destination)

    # A different directory moved into the same path: same name, new inode.
    replacement = tmp_path / "second"
    make_card(replacement)
    import shutil

    shutil.rmtree(card)
    (replacement / "card").rename(card)

    with pytest.raises(CopySafetyError, match="no longer the directory"):
        execute(plan)


def test_a_planned_source_file_that_vanished_is_refused_not_skipped(tmp_path):
    """Silently copying less than planned would be the worse failure."""
    card = make_card(tmp_path)
    destination = tmp_path / "out"
    plan = plan_copy(card, destination)
    (card / "WM30640" / "SN00000000" / "device.xml").unlink()

    with pytest.raises(CopySafetyError, match="is gone now"):
        execute(plan)


def test_an_unchanged_regular_source_still_copies(tmp_path):
    """The property every one of the refusals above must not have cost us."""
    card = make_card(tmp_path)
    destination = tmp_path / "out"
    result = execute(plan_copy(card, destination))

    assert len(result.copied) == 6
    assert result.already_present == []
    for relative in result.copied:
        assert (destination / relative).read_bytes() == (card / relative).read_bytes()


def test_the_source_is_never_opened_by_path_during_execute(tmp_path, monkeypatch):
    """A structural guard against the hole being reintroduced.

    The defect was not a wrong comparison, it was a second lookup by name. So
    this fails if `execute` opens *any* path under the source, however
    carefully it then checks what it got.
    """
    import builtins

    card = make_card(tmp_path)
    destination = tmp_path / "out"
    plan = plan_copy(card, destination)

    resolved_source = str(card.resolve())
    offenders = []

    real_open = builtins.open

    def watching_open(file, *args, **kwargs):
        if isinstance(file, (str, bytes, Path)) and str(file).startswith(
            resolved_source
        ):
            offenders.append(str(file))
        return real_open(file, *args, **kwargs)

    real_os_open = os.open

    def watching_os_open(path, *args, **kwargs):
        if kwargs.get("dir_fd") is None and isinstance(path, (str, bytes, Path)):
            if str(path).startswith(resolved_source) and str(path) != resolved_source:
                offenders.append(str(path))
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", watching_open)
    monkeypatch.setattr(os, "open", watching_os_open)
    execute(plan)
    monkeypatch.undo()

    assert offenders == [], f"execute opened source paths by name: {offenders}"


def test_there_is_no_path_based_source_hash_left(tmp_path):
    """The helper that made the escape possible must stay gone."""
    import prisma_vent.copy_card as copy_card

    assert not hasattr(copy_card, "_sha256_path")


# --------------------------------------------------------------------------
# The inventory recorded only path and size
#
# Interrupt a copy, change a file to different content of the *same length*,
# resume: the inventory still matched, so the resume was accepted and the
# changed bytes were copied in beside the ones already there. On a card a
# device writes to, a same-size rewrite is not an exotic case.
#
# The inventory now carries a SHA-256 per file, and the fingerprint over the
# whole inventory covers it.
# --------------------------------------------------------------------------


def _interruptible_card(root: Path) -> Path:
    """Two files of equal length, so size can never be what distinguishes them."""
    card = root / "card"
    for relative, payload in (
        ("WM30640/SN00000000/a.bin", b"same"),
        ("WM30640/SN00000000/b.bin", b"AAAA"),
    ):
        target = card / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return card


def _interrupt_after_first_file(card: Path, destination: Path) -> str:
    """Run a copy that stops after one file, leaving a resumable destination.

    The interruption is forced by making the *second* file unreadable at the
    moment it would be copied, which is a failure the copier reports rather
    than a state it invents.
    """
    import prisma_vent.copy_card as copy_card

    plan = plan_copy(card, destination)
    copied_first = plan.entries[0].relative_path
    real_copy_into = copy_card._copy_into
    calls = []

    def stop_after_one(*args, **kwargs):
        if calls:
            raise copy_card.CopyError("interrupted for the test")
        calls.append(True)
        return real_copy_into(*args, **kwargs)

    copy_card._copy_into = stop_after_one
    try:
        with pytest.raises(CopyError):
            execute(plan)
    finally:
        copy_card._copy_into = real_copy_into
    return copied_first


def test_a_resume_after_a_same_size_rewrite_is_refused(tmp_path):
    """The reproduction, exactly as reported.

    a.bin is copied, the run is interrupted, b.bin is rewritten from AAAA to
    BBBB — same path, same length — and the resume is attempted. Path and size
    alone accepted it and copied BBBB in.
    """
    card = _interruptible_card(tmp_path)
    destination = tmp_path / "out"
    _interrupt_after_first_file(card, destination)

    # The interruption left something to resume from.
    manifest = json.loads((destination / MANIFEST_NAME).read_text())
    assert manifest["complete"] is False

    changed = card / "WM30640" / "SN00000000" / "b.bin"
    assert changed.read_bytes() == b"AAAA"
    changed.write_bytes(b"BBBB")
    assert changed.stat().st_size == 4

    with pytest.raises(CopySafetyError, match="different content"):
        execute(plan_copy(card, destination))

    # The decisive assertion is not the exception: no changed byte got in.
    for path in destination.rglob("*"):
        if path.is_file() and path.name != MANIFEST_NAME:
            assert path.read_bytes() != b"BBBB"


def test_the_refusal_says_it_is_a_same_size_content_change(tmp_path):
    """"Has since changed" would leave the user hunting for a size difference."""
    card = _interruptible_card(tmp_path)
    destination = tmp_path / "out"
    _interrupt_after_first_file(card, destination)
    (card / "WM30640" / "SN00000000" / "b.bin").write_bytes(b"BBBB")

    with pytest.raises(CopySafetyError) as raised:
        execute(plan_copy(card, destination))
    message = str(raised.value)
    assert "same size as before but hold different content" in message
    assert "empty directory" in message


def test_an_already_copied_file_rewritten_at_the_same_size_is_refused(tmp_path):
    """The other half: the changed file is one the earlier run already copied."""
    card = _interruptible_card(tmp_path)
    destination = tmp_path / "out"
    copied = _interrupt_after_first_file(card, destination)

    (card / copied).write_bytes(b"XXXX")

    with pytest.raises(CopySafetyError, match="different content"):
        execute(plan_copy(card, destination))


def test_an_unchanged_source_still_resumes(tmp_path):
    """The refusals must not have cost the resume path its reason to exist."""
    card = _interruptible_card(tmp_path)
    destination = tmp_path / "out"
    copied = _interrupt_after_first_file(card, destination)

    result = execute(plan_copy(card, destination))
    assert result.already_present == [copied]
    assert result.copied == ["WM30640/SN00000000/b.bin"]
    assert (destination / "WM30640/SN00000000/b.bin").read_bytes() == b"AAAA"


def test_growth_still_resumes_when_nothing_recorded_changed(tmp_path):
    """A device writes to its card between an interruption and its resumption."""
    card = _interruptible_card(tmp_path)
    destination = tmp_path / "out"
    _interrupt_after_first_file(card, destination)

    (card / "WM30640" / "SN00000000" / "c.bin").write_bytes(b"brand new")

    plan = plan_copy(card, destination)
    assert plan.destination_state.resumable
    result = execute(plan)
    assert set(result.copied) == {
        "WM30640/SN00000000/b.bin",
        "WM30640/SN00000000/c.bin",
    }


def test_the_manifest_inventory_carries_a_content_hash_per_file(tmp_path):
    """Checked against hashes computed independently of the module."""
    import hashlib

    card = make_card(tmp_path)
    destination = tmp_path / "out"
    execute(plan_copy(card, destination))

    manifest = json.loads((destination / MANIFEST_NAME).read_text())
    recorded = {e["path"]: e["sha256"] for e in manifest["source_inventory"]}
    assert recorded
    for relative, digest in recorded.items():
        expected = hashlib.sha256((card / relative).read_bytes()).hexdigest()
        assert digest == expected


def test_the_inventory_fingerprint_covers_the_content_hash(tmp_path):
    """Two inventories differing only in content must fingerprint differently."""
    from prisma_vent.copy_card import _inventory_fingerprint

    a = [("x", 4, "a" * 64)]
    b = [("x", 4, "b" * 64)]
    assert _inventory_fingerprint(a) != _inventory_fingerprint(b)
    # And it is order-independent, since it is a set of files.
    two = [("x", 4, "a" * 64), ("y", 8, "c" * 64)]
    assert _inventory_fingerprint(two) == _inventory_fingerprint(list(reversed(two)))


def test_a_manifest_whose_inventory_was_edited_is_refused(tmp_path):
    """The fingerprint is recomputed, not trusted."""
    card, destination = _resumable_copy(tmp_path)
    manifest = json.loads((destination / MANIFEST_NAME).read_text())
    manifest["source_inventory"][0]["sha256"] = "0" * 64
    (destination / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")

    with pytest.raises(CopySafetyError, match="does not match its own fingerprint"):
        plan_copy(card, destination)


def test_a_manifest_with_a_malformed_content_hash_is_refused(tmp_path):
    """A hash-shaped field that is not a hash cannot establish anything."""
    card, destination = _resumable_copy(tmp_path)
    manifest = json.loads((destination / MANIFEST_NAME).read_text())
    manifest["source_inventory"][0]["sha256"] = "not-a-hash"
    (destination / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")

    with pytest.raises(CopySafetyError, match="malformed source inventory entry"):
        plan_copy(card, destination)


def test_a_version_three_manifest_is_refused(tmp_path):
    """Its inventory recorded no content, so it cannot show what it needs to."""
    card, destination = _resumable_copy(tmp_path)
    manifest = json.loads((destination / MANIFEST_NAME).read_text())
    manifest["manifest_version"] = 3
    (destination / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")

    with pytest.raises(CopySafetyError, match="manifest_version"):
        plan_copy(card, destination)


def test_the_copy_digest_never_comes_from_the_inventory_pass(tmp_path):
    """Finding 2's guarantee must survive Finding 1's fix.

    The inventory pass holds its own descriptors and closes them. Reusing its
    result as the digest a file is copied against would save a read and would
    put a second lookup back between hashing and copying — so each file is
    hashed again from the descriptor it is copied from, and that shows up as
    two hashing calls per file.
    """
    import prisma_vent.copy_card as copy_card

    card = _interruptible_card(tmp_path)
    destination = tmp_path / "out"
    plan = plan_copy(card, destination)

    original = copy_card._digest_fd
    calls = []

    def counting(source_fd, shown):
        calls.append(shown)
        return original(source_fd, shown)

    copy_card._digest_fd = counting
    try:
        execute(plan)
    finally:
        copy_card._digest_fd = original

    for entry in plan.entries:
        assert calls.count(entry.relative_path) == 2, calls


# --------------------------------------------------------------------------
# The two passes have to agree
#
# Hashing the source twice — once for the inventory, once from the descriptor
# the file is copied from — and then never comparing the two was worse than
# hashing once. The manifest recorded the inventory pass's digest while the
# copy verified itself against the other one, so a file changed between them
# produced a *finished* copy whose manifest described different bytes than the
# directory held. Reproduced: complete: true, the new content on disk, the old
# hash in the inventory.
#
# Two independent checks close it, because each alone has a blind spot:
# the recorded modification time (blind to a rewrite inside one filesystem
# tick) and the digest comparison (blind to nothing, but only inside execute).
# --------------------------------------------------------------------------


def _rewrite_keeping_mtime(path: Path, payload: bytes) -> None:
    """Change content in place and put the modification time back.

    This is what a filesystem with coarse timestamp granularity does by
    itself — and SD cards are formatted with such filesystems — so it is the
    case the digest comparison has to catch on its own.
    """
    import os as _os

    stale = _os.stat(path)
    path.write_bytes(payload)
    _os.utime(path, ns=(stale.st_atime_ns, stale.st_mtime_ns))


def test_a_change_between_inventorying_and_copying_is_refused(tmp_path, monkeypatch):
    """The reproduction: the manifest must never describe bytes that are not there."""
    import prisma_vent.copy_card as copy_card

    card = _interruptible_card(tmp_path)
    destination = tmp_path / "out"
    plan = plan_copy(card, destination)
    victim = card / "WM30640" / "SN00000000" / "b.bin"

    original = copy_card._hash_source

    def inventory_then_change(*args, **kwargs):
        digests = original(*args, **kwargs)
        _rewrite_keeping_mtime(victim, b"BBBB")
        return digests

    monkeypatch.setattr(copy_card, "_hash_source", inventory_then_change)

    with pytest.raises(CopySafetyError, match="between being inventoried"):
        execute(plan)


def test_no_finished_manifest_can_describe_bytes_that_are_not_there(
    tmp_path, monkeypatch
):
    """Stated as the property, not as the exception.

    Whatever the copy did, the manifest it left behind must not claim to be
    complete while its inventory disagrees with the files beside it.
    """
    import hashlib
    import prisma_vent.copy_card as copy_card

    card = _interruptible_card(tmp_path)
    destination = tmp_path / "out"
    plan = plan_copy(card, destination)
    victim = card / "WM30640" / "SN00000000" / "b.bin"

    original = copy_card._hash_source

    def inventory_then_change(*args, **kwargs):
        digests = original(*args, **kwargs)
        _rewrite_keeping_mtime(victim, b"BBBB")
        return digests

    monkeypatch.setattr(copy_card, "_hash_source", inventory_then_change)
    with pytest.raises(CopySafetyError):
        execute(plan)

    manifest = json.loads((destination / MANIFEST_NAME).read_text())
    assert manifest["complete"] is False
    recorded = {e["path"]: e["sha256"] for e in manifest["source_inventory"]}
    for relative, digest in recorded.items():
        copied = destination / relative
        if copied.exists():
            assert hashlib.sha256(copied.read_bytes()).hexdigest() == digest


def test_a_same_size_rewrite_after_planning_is_refused_by_the_mtime(tmp_path):
    """`PlannedEntry.mtime_ns` said it caught this, and nothing compared it.

    A comment describing a check that does not exist is worse than no comment:
    it is the reason a later reader stops looking.
    """
    card = _interruptible_card(tmp_path)
    destination = tmp_path / "out"
    plan = plan_copy(card, destination)

    import os as _os

    victim = card / "WM30640" / "SN00000000" / "b.bin"
    stale = _os.stat(victim)
    victim.write_bytes(b"BBBB")
    _os.utime(victim, ns=(stale.st_atime_ns, stale.st_mtime_ns + 10**9))

    with pytest.raises(CopySafetyError, match="was modified between planning"):
        execute(plan)
    assert not (destination / "WM30640/SN00000000/b.bin").exists()


def test_the_recorded_mtime_is_actually_compared(tmp_path):
    """The field exists because something reads it, checked directly."""
    card = _interruptible_card(tmp_path)
    plan = plan_copy(card, tmp_path / "out")
    entry = plan.entries[0]
    assert entry.mtime_ns == (card / entry.relative_path).stat().st_mtime_ns

    import os as _os

    import prisma_vent.copy_card as copy_card

    parent = _os.open(card / "WM30640" / "SN00000000", _os.O_RDONLY | _os.O_DIRECTORY)
    try:
        # Same file, but the entry claims a different modification time.
        lying = type(entry)(
            relative_path=entry.relative_path,
            size=entry.size,
            recognised=entry.recognised,
            dev=entry.dev,
            ino=entry.ino,
            mtime_ns=entry.mtime_ns + 1,
        )
        with pytest.raises(CopySafetyError, match="was modified between planning"):
            copy_card._open_source_file(parent, Path(entry.relative_path).name, lying)
    finally:
        _os.close(parent)


def test_an_unchanged_source_is_untroubled_by_either_check(tmp_path):
    """Both refusals must leave the ordinary path alone."""
    card = _interruptible_card(tmp_path)
    destination = tmp_path / "out"
    result = execute(plan_copy(card, destination))

    assert len(result.copied) == 2
    manifest = json.loads((destination / MANIFEST_NAME).read_text())
    assert manifest["complete"] is True

    import hashlib

    for entry in manifest["source_inventory"]:
        copied = (destination / entry["path"]).read_bytes()
        assert hashlib.sha256(copied).hexdigest() == entry["sha256"]
