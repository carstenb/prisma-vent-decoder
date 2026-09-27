"""Copy an SD card's contents into a private local directory.

Deliberately **file-based, not a disk image**. "Cloning" a card captures
partitions and free space, which means capturing deleted data nobody asked
for; this copies the files that are there and verifies each one.

Four principles shape the whole module.

**Nothing is lost for being unfamiliar.** Every regular file is copied, known
or not. A later firmware adding a file must show up in the copy and be visible
in the manifest as unrecognised — not be silently dropped because its
extension was not in a list. Only *non-regular* objects are refused: symlinks,
sockets, named pipes, device nodes.

**Nothing is overwritten or destroyed.** The source is opened for reading and
never written to. An existing destination file is verified rather than
replaced: identical content is a resumed copy, different content is refused.
Files are written to a temporary name, checked by size and SHA-256, and only
then renamed into place, so an interrupted run leaves no half-file that looks
finished.

**The destination holds health data.** It is created with restrictive
permissions, it carries a manifest recording what was copied and its hashes,
and it must never be placed inside a repository.

**Nothing is written outside the destination the caller named.** This is not a
matter of computing careful paths and trusting them. Once the destination is
resolved it is opened as a directory *handle*, and every subsequent directory
and file is created and opened relative to that handle with no-follow
semantics. A symlink anywhere below the destination is refused rather than
traversed, and a path swapped between planning and writing cannot redirect
anything, because no later operation goes through a path string at all.

Why that last principle needed its own machinery
------------------------------------------------

An earlier version treated *any* non-empty directory containing an entry named
``copy-manifest.json`` as a destination this tool had created and could resume
into, without looking at that entry at all. Combined with path-based writes,
that was enough to place health data outside the chosen destination:

1. leave any file — or a symlink — named ``copy-manifest.json`` in the
   destination;
2. make a directory the copy is going to need, such as ``WM30640/``, a symlink
   to somewhere else entirely;
3. run the copy.

``mkdir(parents=True, exist_ok=True)`` follows the symlink and succeeds, the
copied file lands in the external directory, and ``chmod`` walks back up the
*path* and sets that external directory to owner-only. The manifest symlink is
followed and overwritten too.

Both halves are now closed. A destination is resumable only if it carries a
manifest this tool wrote — a regular, non-symlink file, valid JSON, declaring
the expected schema and naming the same source — and every write goes through
a directory handle rather than a path.

One honest limit: this opens the source read-only and performs no write
against it, but it **cannot guarantee the operating system mounted the volume
read-only**. That is outside any program's control and is not claimed.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Iterator, TextIO

from . import DECODER_SCHEMA_VERSION, __version__

__all__ = [
    "add_arguments",
    "run",
    "CopyError",
    "CopySafetyError",
    "PlannedEntry",
    "CopyPlan",
    "CopyResult",
    "DestinationState",
    "plan_copy",
    "execute",
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
]


class CopyError(Exception):
    """The copy could not be completed. Maps to the I/O exit code."""


class CopySafetyError(CopyError):
    """A condition that would risk the source or the data. Its own exit code.

    Separate from :class:`CopyError` because the two want different responses:
    an I/O failure invites a retry, a safety violation invites reading the
    message.
    """


#: Written into the destination. Named plainly so a person finding the folder
#: later can tell what it is.
MANIFEST_NAME = "copy-manifest.json"

#: Version 2 added ``source_resolved`` and made the manifest the thing that
#: decides whether a destination may be resumed. Version 3 replaced that
#: identity: a **mount path is not a card**.
#:
#: ``/Volumes/CARD`` is whichever card is in the reader. Copy card A, eject it,
#: insert card B, run the copy again — the resolved source path is identical,
#: so a version-2 manifest recognised the destination as resumable and merged
#: two people's — or at least two recordings' — files into one backup. That was
#: reproduced.
#:
#: Version 3 records the **source inventory** instead, and a resume requires
#: every file the earlier run planned to still be present, at the same path and
#: the same size. Growth is allowed, because a device writes to a card between
#: an interrupted copy and its resumption. Disappearance and change are not.
#:
#: An older manifest is refused rather than reinterpreted: it does not carry
#: what the check now needs, and guessing would be the same mistake again.
MANIFEST_VERSION = 4

#: A manifest larger than this is not one of ours and is not going to be
#: parsed. Even a card with tens of thousands of files does not approach it.
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024

#: Permissions for the destination tree: owner only. This is health data.
_PRIVATE_DIR_MODE = 0o700

#: Copied files are written read-only to the owner. The manifest stays
#: writable, because a resumed run rewrites it.
_COPIED_FILE_MODE = 0o400
_MANIFEST_MODE = 0o600

_PARTIAL_SUFFIX = ".partial"

#: A recorded inventory hash has to look like one before it is compared, so a
#: manifest carrying something else is refused as malformed rather than simply
#: failing to match.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Shapes the card is known to hold. Membership is **only** used to mark an
#: entry recognised in the manifest — never to decide whether to copy it.
_KNOWN_PATTERNS = (
    re.compile(r"^WM\d+/plugin/\d+/dcm\.zip$"),
    re.compile(r"^WM\d+/SN\d+/\d{4}_\d{4}-\d{2}-\d{2}\.zip$"),
    re.compile(r"^WM\d+/SN\d+/trendcurve/\d{4}_\d{4}-\d{2}-\d{2}\.tc$"),
    re.compile(r"^WM\d+/SN\d+/battery/\d{4}_\d{4}_\d{4}-\d{2}-\d{2}\.wmedf$"),
    re.compile(r"^WM\d+/SN\d+/logs/.+$"),
    re.compile(r"^WM\d+/SN\d+/(configuration\.json|device\.xml|prismaVENT\.sdpvdat)$"),
)

#: Files an operating system leaves behind that are not card content.
_IGNORED_NAMES = {".DS_Store", "Thumbs.db", ".Spotlight-V100", ".Trashes", ".fseventsd"}

#: Without directory-relative operations there is no way to write below a
#: destination without going through a path a symlink can redirect. Rather than
#: fall back to the unsafe form silently, this module refuses to run.
_HAS_DIR_FD = os.open in os.supports_dir_fd and os.rename in os.supports_dir_fd


@dataclass(frozen=True)
class PlannedEntry:
    """One file the copy will produce.

    ``dev``, ``ino`` and ``mtime_ns`` are the identity **planning** saw, taken
    from an ``lstat`` that did not follow a link. :func:`execute` re-checks them
    against an ``fstat`` on the descriptor it actually reads from, so a source
    file replaced between the two — by a symlink, or by different content at
    the same size — is refused instead of copied.
    """

    relative_path: str
    size: int
    recognised: bool
    #: ``(st_dev, st_ino)`` at plan time. A different inode under the same name
    #: is a different file, whatever it is called.
    dev: int = 0
    ino: int = 0
    #: Modification time in nanoseconds at plan time. Compared before and after
    #: the read, so a file rewritten *in place* — same inode, same size — is
    #: caught as well.
    mtime_ns: int = 0

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.relative_path.split("/"))

    @property
    def identity(self) -> tuple[int, int]:
        return (self.dev, self.ino)


@dataclass(frozen=True)
class DestinationState:
    """What the destination looked like when the plan was made.

    Carried on the plan and re-checked by :func:`execute`, so a destination
    swapped between the two is refused instead of silently accepted. ``exists``
    with no ``manifest_sha256`` means an empty directory; a directory holding
    anything else without a manifest this tool wrote never reaches here.
    """

    exists: bool
    resumable: bool
    manifest_sha256: str | None = None
    #: The parsed manifest, kept so the source check can run once the plan's
    #: inventory exists. Whether a destination is *ours* can be decided from
    #: the manifest alone; whether it belongs to *this source* cannot, because
    #: that comparison needs the source walked first.
    manifest: dict | None = None
    #: ``(st_dev, st_ino)`` of the resolved destination, or ``None`` when it
    #: does not exist yet. Compared against the directory actually opened.
    identity: tuple[int, int] | None = None


@dataclass(frozen=True)
class CopyPlan:
    """What a copy would do, computed before anything is written."""

    source: Path
    destination: Path
    entries: tuple[PlannedEntry, ...]
    skipped_non_regular: tuple[str, ...]
    ignored: tuple[str, ...]
    resolved_source: Path
    resolved_destination: Path
    destination_state: DestinationState
    #: ``(st_dev, st_ino)`` of the resolved source root at plan time. The root
    #: is opened once in :func:`execute` and checked against this, so a card
    #: swapped at the same mount path between planning and copying is refused
    #: before a single byte is read from it.
    source_identity: tuple[int, int] | None = None

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries)

    @property
    def unrecognised(self) -> tuple[PlannedEntry, ...]:
        return tuple(e for e in self.entries if not e.recognised)


@dataclass
class CopyResult:
    """What a copy actually did."""

    copied: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    bytes_written: int = 0
    manifest_path: Path | None = None


def plan_copy(source: Path, destination: Path) -> CopyPlan:
    """Work out what would be copied, and refuse anything unsafe.

    Nothing is written here, so a caller can show the plan and stop.
    """
    if not _HAS_DIR_FD:  # pragma: no cover - not reachable on macOS or Linux
        raise CopySafetyError(
            "this platform has no directory-relative file operations, so a copy "
            "cannot be kept below its destination without trusting paths a "
            "symlink can redirect. Refusing rather than copying unsafely"
        )

    source = Path(source)
    destination = Path(destination)
    resolved_source, resolved_destination = _check_safety(source, destination)
    state = _inspect_destination(resolved_destination, resolved_source)

    entries: list[PlannedEntry] = []
    non_regular: list[str] = []
    ignored: list[str] = []

    for kind, path in sorted(_walk(source), key=lambda item: item[1]):
        relative = path.relative_to(source).as_posix()
        if ".." in relative.split("/"):
            raise CopySafetyError(f"{relative!r} traverses directories")

        if kind == "ignored":
            ignored.append(f"{relative}/ (directory, not descended into)")
            continue
        if kind == "symlink":
            non_regular.append(f"{relative}/ (symbolic link to a directory)")
            continue
        if path.name in _IGNORED_NAMES:
            ignored.append(relative)
            continue

        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            non_regular.append(f"{relative} (symbolic link)")
            continue
        if not stat.S_ISREG(info.st_mode):
            non_regular.append(f"{relative} ({_describe_type(info.st_mode)})")
            continue

        entries.append(
            PlannedEntry(
                relative_path=relative,
                size=info.st_size,
                # Recognition affects the manifest only. An unfamiliar file is
                # still copied: losing it would be the worse failure by far.
                recognised=any(p.match(relative) for p in _KNOWN_PATTERNS),
                dev=info.st_dev,
                ino=info.st_ino,
                mtime_ns=info.st_mtime_ns,
            )
        )

    if not entries:
        raise CopyError(f"{source} holds no regular files to copy")

    # Only now can the destination be checked against *this* source: the
    # comparison is against what the source holds, which is what has just been
    # walked. Doing it earlier would have to fall back on the path, which is
    # what made two cards under one mount point indistinguishable.
    if state.resumable and state.manifest is not None:
        # Paths and sizes only. Reading every file in full belongs in execute,
        # where the verified descriptors are; this catches an obviously wrong
        # card before anyone waits for that.
        _require_same_source(state.manifest, entries, None, resolved_destination)

    try:
        source_info = os.stat(resolved_source)
    except OSError as exc:  # pragma: no cover - it was just walked
        raise CopyError(f"{source} cannot be examined: {exc}") from exc

    return CopyPlan(
        source=source,
        destination=destination,
        entries=tuple(entries),
        skipped_non_regular=tuple(non_regular),
        ignored=tuple(ignored),
        resolved_source=resolved_source,
        resolved_destination=resolved_destination,
        destination_state=state,
        source_identity=(source_info.st_dev, source_info.st_ino),
    )


def execute(plan: CopyPlan, *, out: TextIO | None = None) -> CopyResult:
    """Carry out a plan, verifying every file before it counts as copied.

    **Both sides go through directory handles, never through a path string.**
    The destination side has done so for a while: a symlink introduced below it
    between planning and execution cannot redirect a write.

    The source side used to be different, and that was a hole of the same
    shape pointing the other way. Planning checked each file with ``lstat``, so
    it refused symlinks — and then execution re-opened the file *by path*,
    twice: once to hash it and once to copy it. Replacing a planned file with a
    symlink to something outside the source in between made the copier follow
    it and copy that content in, successfully and silently. ``SECURITY.md``
    calls reading outside the named directory a vulnerability, and it was one.

    So the source root is opened once and checked against the identity planning
    recorded, every subdirectory below it is opened relative to its parent with
    ``O_DIRECTORY | O_NOFOLLOW``, and every file is opened once with
    ``O_NOFOLLOW`` and verified with ``fstat`` before a byte is read.
    **Hashing and copying then both read that same descriptor**, so there is no
    second lookup for a swap to win, and the source is checked again afterwards
    so a file rewritten in place while it was being read is caught too.
    """
    result = CopyResult()
    root_fd = _open_destination_root(plan)
    open_directories: dict[str, int] = {"": root_fd}
    source_root_fd = _open_source_root(plan)
    source_directories: dict[str, int] = {"": source_root_fd}
    try:
        # Re-checked through the handle rather than the path, so a destination
        # swapped since planning is caught here rather than written into.
        _confirm_destination_unchanged(plan, root_fd)

        # Hashed before anything is written, so a source that disagrees with
        # what an earlier run recorded is refused with the destination
        # untouched.
        digests = _hash_source(plan, source_root_fd, source_directories)
        state = plan.destination_state
        if state.resumable and state.manifest is not None:
            # The authoritative check. The plan-time one compared paths and
            # sizes; this one compares content, which is what a same-size
            # rewrite defeats.
            _require_same_source(
                state.manifest, plan.entries, digests, plan.resolved_destination
            )
        inventory = _inventory_items(plan.entries, digests)

        # Written *before* anything is copied, not after. A run interrupted
        # halfway otherwise leaves a non-empty directory with no manifest,
        # which the safety check would then refuse as somebody else's folder -
        # making an interrupted copy impossible to resume, which is the
        # opposite of the intent.
        _write_manifest(plan, result, root_fd, inventory, complete=False)

        for entry in plan.entries:
            parts = entry.parts
            parent_fd = _directory_fd_for(open_directories, root_fd, parts[:-1])
            name = parts[-1]

            source_parent_fd = _source_directory_fd_for(
                source_directories, source_root_fd, parts[:-1]
            )
            source_fd, before = _open_source_file(source_parent_fd, name, entry)
            try:
                # Hashed again, from *this* descriptor. Reusing the inventory
                # pass's digest would be cheaper by a read and would quietly
                # undo what the descriptor discipline is for: that pass used a
                # different descriptor, so trusting its result here would put a
                # second lookup back between hashing and copying — which is the
                # exact shape of the hole this module just closed.
                digest = _digest_fd(source_fd, entry.relative_path)

                # **And the two passes have to agree.** Hashing twice and then
                # not comparing the results was worse than hashing once: the
                # manifest recorded the inventory pass's digest while the copy
                # verified itself against this one, so a file changed between
                # the two produced a finished copy whose manifest described
                # different bytes than the directory held. That was reproduced
                # — complete: true, the new content on disk, the old hash in
                # the inventory — and a manifest that disagrees with the data
                # beside it is worse than none, because a later resume trusts
                # it.
                if digest != digests[entry.relative_path]:
                    raise CopySafetyError(
                        f"{entry.relative_path} changed between being "
                        "inventoried and being copied, so the manifest and the "
                        "copy would describe different bytes. Nothing further "
                        "has been written; run the copy again"
                    )
                result_of_entry = _copy_one(
                    plan,
                    entry,
                    parent_fd,
                    name,
                    source_fd,
                    before,
                    digest,
                    result,
                    out,
                )
            finally:
                os.close(source_fd)
            if result_of_entry is _ALREADY_PRESENT:
                continue

        _write_manifest(plan, result, root_fd, inventory, complete=True)
        result.manifest_path = plan.resolved_destination / MANIFEST_NAME
        return result
    finally:
        for fd in open_directories.values():
            os.close(fd)
        for fd in source_directories.values():
            os.close(fd)


#: Sentinel telling :func:`execute` that a planned file was already there and
#: verified, so nothing was copied for it.
_ALREADY_PRESENT: object = object()


def _copy_one(
    plan: CopyPlan,
    entry: PlannedEntry,
    parent_fd: int,
    name: str,
    source_fd: int,
    before: os.stat_result,
    digest: str,
    result: CopyResult,
    out: TextIO | None,
) -> object:
    """Install one planned file, or confirm it is already there.

    Split out of :func:`execute` so the source descriptor's lifetime is one
    ``try``/``finally`` around a single call rather than wrapped around a long
    loop body. *digest* was computed from ``source_fd`` and nothing else, and
    *before* is the ``fstat`` taken when that descriptor was opened.
    """
    existing = _lstat_at(parent_fd, name)
    if existing is not None:
        # A resumed copy, or a collision. Verified rather than assumed, and
        # never overwritten: a differing file is somebody's data.
        if stat.S_ISLNK(existing.st_mode):
            raise CopySafetyError(
                f"{entry.relative_path} exists in the destination as a "
                "symbolic link. Nothing has been written through it"
            )
        if not stat.S_ISREG(existing.st_mode):
            raise CopySafetyError(
                f"{entry.relative_path} exists in the destination as "
                f"{_describe_type(existing.st_mode)}, which this tool "
                "will not replace"
            )
        if existing.st_size == entry.size and _sha256_at(parent_fd, name) == digest:
            result.already_present.append(entry.relative_path)
            return _ALREADY_PRESENT
        raise CopySafetyError(
            f"{entry.relative_path} already exists in the destination with "
            "different content. Nothing has been overwritten; move or remove "
            "it deliberately if it should be replaced."
        )

    written = _copy_into(parent_fd, name, source_fd, entry, before, digest)
    result.copied.append(entry.relative_path)
    result.bytes_written += written
    if out is not None:
        print(f"  {entry.relative_path}", file=out)
    return None


# --------------------------------------------------------------------------
# directory handles
# --------------------------------------------------------------------------


def _open_destination_root(plan: CopyPlan) -> int:
    """Create the destination if needed and return a handle to it.

    The path the caller named is resolved once, here and in :func:`plan_copy`,
    because the caller chose it deliberately and a symlink they wrote into
    their own path is theirs to have. Everything *below* it is a different
    matter and is never followed.
    """
    destination = plan.resolved_destination
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CopyError(f"{plan.destination} cannot be created: {exc}") from exc

    try:
        fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise CopySafetyError(
            f"{plan.destination} could not be opened as a directory: {exc}"
        ) from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):  # pragma: no cover - O_DIRECTORY
            raise CopySafetyError(f"{plan.destination} is not a directory")
        os.fchmod(fd, _PRIVATE_DIR_MODE)
    except Exception:
        os.close(fd)
        raise
    return fd


def _confirm_destination_unchanged(plan: CopyPlan, root_fd: int) -> None:
    """Refuse a destination that is not the one the plan was made against.

    Two things can have changed since planning: the directory itself may have
    been replaced, and the manifest inside it may have been swapped. Both are
    checked against what the plan recorded, and both are checked through the
    open handle rather than through the path.
    """
    state = plan.destination_state
    info = os.fstat(root_fd)
    identity = (info.st_dev, info.st_ino)
    if state.identity is not None and state.identity != identity:
        raise CopySafetyError(
            f"{plan.destination} is no longer the directory this copy was "
            "planned against — it has been replaced since. Nothing has been "
            "written; run the copy again"
        )

    current = _read_manifest(root_fd, plan.destination)
    current_digest = None if current is None else current[1]
    if current_digest != state.manifest_sha256:
        raise CopySafetyError(
            f"the {MANIFEST_NAME} in {plan.destination} changed between "
            "planning this copy and starting it. Nothing has been written"
        )
    if current is not None:
        _require_our_manifest(current[0], plan.resolved_source, plan.destination)
        # Re-run against the plan's inventory as well, so a manifest swapped
        # for another *valid* one naming a different source is caught here
        # rather than written into.
        _require_same_source(current[0], plan.entries, None, plan.destination)


def _directory_fd_for(
    cache: dict[str, int], root_fd: int, parts: tuple[str, ...]
) -> int:
    """A handle to the directory *parts* names below the destination root.

    Each level is created and opened relative to its parent with
    :data:`os.O_NOFOLLOW`, so a symlink at any level is refused rather than
    followed. Handles are cached for the run: a card holds a handful of
    directories and thousands of files.
    """
    prefix = ""
    parent_fd = root_fd
    for name in parts:
        prefix = f"{prefix}/{name}" if prefix else name
        cached = cache.get(prefix)
        if cached is not None:
            parent_fd = cached
            continue
        parent_fd = cache.setdefault(prefix, _child_directory_fd(parent_fd, name, prefix))
    return parent_fd


def _child_directory_fd(parent_fd: int, name: str, shown: str) -> int:
    """Create *name* under *parent_fd* if absent, and open it without following."""
    if name in ("", ".", ".."):  # pragma: no cover - refused during planning
        raise CopySafetyError(f"{shown!r} is not a usable directory name")
    try:
        os.mkdir(name, _PRIVATE_DIR_MODE, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise CopyError(f"cannot create {shown} in the destination: {exc}") from exc

    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
    except OSError as exc:
        # ELOOP is the no-follow refusal; ENOTDIR means something that is not a
        # directory is sitting where one has to go. Both mean the same thing to
        # a person reading this message.
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise CopySafetyError(
                f"{shown} in the destination is a symbolic link or is not a "
                "directory. Nothing has been written through it — a copy must "
                "stay below the destination you named"
            ) from exc
        raise CopyError(f"cannot open {shown} in the destination: {exc}") from exc

    try:
        os.fchmod(fd, _PRIVATE_DIR_MODE)
    except OSError as exc:  # pragma: no cover - the handle is already ours
        os.close(fd)
        raise CopyError(f"cannot set permissions on {shown}: {exc}") from exc
    return fd


# --------------------------------------------------------------------------
# source-side handles
#
# The mirror image of the destination machinery above, with one difference that
# matters: **nothing here creates anything.** A missing source directory or
# file is not made, it is refused — the plan said it was there, and if it is
# not, the plan is describing something other than what is now on the card.
# --------------------------------------------------------------------------


def _open_source_root(plan: CopyPlan) -> int:
    """Open the source root once, and confirm it is the one that was planned.

    The path the caller named is resolved, as on the destination side, because
    a symlink somebody wrote into their own argument is theirs to have. What is
    checked here is that the directory behind it is still the same directory:
    a card ejected and another inserted at the same mount path is a different
    source, and copying the second into a plan made for the first is exactly
    the confusion this refuses.
    """
    try:
        fd = os.open(
            plan.resolved_source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
    except OSError as exc:
        raise CopySafetyError(
            f"{plan.source} could not be opened as a directory: {exc}"
        ) from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):  # pragma: no cover - O_DIRECTORY
            raise CopySafetyError(f"{plan.source} is not a directory")
        if (
            plan.source_identity is not None
            and (info.st_dev, info.st_ino) != plan.source_identity
        ):
            raise CopySafetyError(
                f"{plan.source} is no longer the directory this copy was "
                "planned against — it has been replaced since, which at a "
                "mount path means a different card. Nothing has been read from "
                "it; run the copy again"
            )
    except Exception:
        os.close(fd)
        raise
    return fd


def _source_directory_fd_for(
    cache: dict[str, int], root_fd: int, parts: tuple[str, ...]
) -> int:
    """A handle to the source directory *parts* names, opened without following."""
    prefix = ""
    parent_fd = root_fd
    for name in parts:
        prefix = f"{prefix}/{name}" if prefix else name
        cached = cache.get(prefix)
        if cached is not None:
            parent_fd = cached
            continue
        parent_fd = cache.setdefault(
            prefix, _child_source_directory_fd(parent_fd, name, prefix)
        )
    return parent_fd


def _child_source_directory_fd(parent_fd: int, name: str, shown: str) -> int:
    """Open *name* under *parent_fd* as a directory, refusing a link."""
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise CopySafetyError(
                f"{shown} in the source is a symbolic link or is not a "
                "directory. A copy reads only what is below the source you "
                "named, so nothing has been read through it"
            ) from exc
        if exc.errno == errno.ENOENT:
            raise CopySafetyError(
                f"{shown} was in the source when this copy was planned and is "
                "gone now. Nothing has been read; run the copy again"
            ) from exc
        raise CopyError(f"cannot open {shown} in the source: {exc}") from exc
    return fd


def _open_source_file(
    parent_fd: int, name: str, entry: PlannedEntry
) -> tuple[int, os.stat_result]:
    """Open one planned source file and confirm it is the file that was planned.

    Returns the descriptor and the ``fstat`` taken from it. Everything after
    this point reads that descriptor and never the name again, which is what
    makes the checks below worth doing: they cannot be invalidated by a later
    lookup, because there is no later lookup.
    """
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise CopySafetyError(
                f"{entry.relative_path} in the source is a symbolic link now, "
                "and was a regular file when this copy was planned. A copy "
                "reads only what is below the source you named, so nothing has "
                "been read through it"
            ) from exc
        if exc.errno == errno.ENOENT:
            raise CopySafetyError(
                f"{entry.relative_path} was in the source when this copy was "
                "planned and is gone now. Nothing has been read; run the copy "
                "again"
            ) from exc
        raise CopyError(
            f"cannot read {entry.relative_path} from the source: {exc}"
        ) from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CopySafetyError(
                f"{entry.relative_path} in the source is "
                f"{_describe_type(info.st_mode)} now, and was a regular file "
                "when this copy was planned. Nothing has been read from it"
            )
        if (info.st_dev, info.st_ino) != entry.identity:
            raise CopySafetyError(
                f"{entry.relative_path} in the source is a different file than "
                "the one this copy was planned against — same name, different "
                "object. Nothing has been read from it; run the copy again"
            )
        if info.st_size != entry.size:
            raise CopySafetyError(
                f"{entry.relative_path} in the source changed size between "
                "planning this copy and reading it. Nothing has been read from "
                "it; run the copy again"
            )
        # PlannedEntry documented this as the check that catches a same-size
        # rewrite between planning and execution, and then nothing compared it.
        # A comment describing a check that does not exist is worse than no
        # comment: it is the reason a later reader stops looking.
        #
        # It is **not** sufficient on its own. A filesystem with coarse
        # timestamp granularity — and SD cards are formatted with such — leaves
        # the modification time unchanged for a rewrite inside the same tick,
        # which is why the digest comparison in :func:`execute` exists as well.
        # Two independent checks, because each one alone has a blind spot the
        # other does not.
        if info.st_mtime_ns != entry.mtime_ns:
            raise CopySafetyError(
                f"{entry.relative_path} in the source was modified between "
                "planning this copy and reading it. Nothing has been read from "
                "it; run the copy again"
            )
    except Exception:
        os.close(fd)
        raise
    return fd, info


def _require_source_unchanged(
    source_fd: int, entry: PlannedEntry, before: os.stat_result
) -> None:
    """Confirm the file did not change while it was being read.

    The identity checks in :func:`_open_source_file` catch a *replaced* file.
    They cannot catch one rewritten **in place** — same inode, same size, new
    content — because that changes nothing they look at. The modification time
    does change, so it is compared before and after; and the content digest is
    compared independently, since it was taken from this descriptor in a
    separate pass and would disagree with the bytes just written.

    **A file unlinked or renamed while it was held is not a change here**, and
    that is deliberate. The descriptor still refers to the object that was
    opened and verified, so both passes read the planned bytes and the copy is
    faithful to what the plan described. Refusing that case would make copying
    a card the device is actively writing to fail, and would buy nothing: the
    property being defended is that nothing outside the source is ever read,
    and holding the descriptor is precisely what delivers it.
    """
    after = os.fstat(source_fd)
    if (
        after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise CopySafetyError(
            f"{entry.relative_path} in the source changed while it was being "
            "copied, so no copy of it can be trusted. The partial copy has "
            "been discarded; run the copy again"
        )


def _digest_fd(source_fd: int, shown: str) -> str:
    """SHA-256 of a descriptor's whole content, from the beginning.

    Seeks rather than re-opening. The seek is the entire point: re-opening by
    name is what let a symlink introduced after planning decide what got
    hashed and copied.
    """
    digest = hashlib.sha256()
    os.lseek(source_fd, 0, os.SEEK_SET)
    for block in _read_blocks(source_fd, shown):
        digest.update(block)
    return digest.hexdigest()


def _read_blocks(source_fd: int, shown: str):
    """Yield the descriptor's content in blocks, from wherever it is positioned."""
    while True:
        try:
            block = os.read(source_fd, 1024 * 1024)
        except OSError as exc:
            raise CopyError(f"reading {shown} failed: {exc}") from exc
        if not block:
            return
        yield block


def _lstat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CopyError(f"cannot examine {name} in the destination: {exc}") from exc


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def _copy_into(
    parent_fd: int,
    name: str,
    source_fd: int,
    entry: PlannedEntry,
    before: os.stat_result,
    expected_digest: str,
) -> int:
    """Copy from *source_fd* to *name* under *parent_fd*, verified before it counts.

    Written to a temporary name in the same directory and installed with
    no-replace semantics, so an interrupted run never leaves a half-file that
    looks finished and a concurrent write is never destroyed.

    **The source is the descriptor, not a path.** *expected_digest* came from
    this same descriptor, so the two passes cannot disagree about which file
    they read — and after the copy the source is checked once more for a change
    made in place while it was being read.
    """
    expected_size = entry.size
    partial = name + _PARTIAL_SUFFIX
    stale = _lstat_at(parent_fd, partial)
    if stale is not None:
        # A leftover partial is discarded rather than resumed: its contents
        # cannot be trusted, and re-copying is cheap next to guessing. A
        # non-regular object under that name is not ours to remove.
        if not stat.S_ISREG(stale.st_mode) or stat.S_ISLNK(stale.st_mode):
            raise CopySafetyError(
                f"{name}{_PARTIAL_SUFFIX} in the destination is not a regular "
                "file, so it is not a leftover from an interrupted copy"
            )
        os.unlink(partial, dir_fd=parent_fd)

    written = 0
    try:
        fd = os.open(
            partial,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise CopyError(f"cannot create {name} in the destination: {exc}") from exc

    try:
        # Rewound explicitly: the hashing pass left the descriptor at the end,
        # and the whole point of reusing it is that nothing re-opens the name.
        os.lseek(source_fd, 0, os.SEEK_SET)
        with os.fdopen(fd, "wb") as dst:
            for block in _read_blocks(source_fd, entry.relative_path):
                dst.write(block)
                written += len(block)
    except (OSError, CopyError) as exc:
        os.unlink(partial, dir_fd=parent_fd)
        raise CopyError(
            f"copying {entry.relative_path} failed: {exc}"
        ) from exc

    try:
        _require_source_unchanged(source_fd, entry, before)
    except Exception:
        # Discarded before the exception leaves: a partial that survives a
        # refusal is a half-file somebody may later mistake for a copy.
        os.unlink(partial, dir_fd=parent_fd)
        raise

    if written != expected_size or _sha256_at(parent_fd, partial) != expected_digest:
        os.unlink(partial, dir_fd=parent_fd)
        raise CopyError(
            f"{entry.relative_path} did not survive the copy intact and has "
            "been discarded"
        )

    os.chmod(partial, _COPIED_FILE_MODE, dir_fd=parent_fd)
    _install_without_replacing(parent_fd, partial, name)
    return written


def _install_without_replacing(parent_fd: int, partial: str, name: str) -> None:
    """Move *partial* onto *name*, refusing if anything is already there.

    ``os.replace`` was used here, and it **overwrites**. That contradicted this
    module's stated no-overwrite property through a race: the destination is
    checked for an existing entry before the copy starts, and a file appearing
    under that name during the copy — another process, another run of this
    tool, a sync client — was then silently destroyed by the rename.

    ``link`` + ``unlink`` is the portable POSIX way to install a file
    atomically *without* replace semantics: ``link`` fails with ``EEXIST`` if
    the name is taken, and it fails whether what is taking it is a regular
    file, a directory or a symlink. There is no window between the check and
    the install, because the check *is* the install.
    """
    try:
        os.link(partial, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except FileExistsError as exc:
        # Ours to remove — we created it with O_EXCL a moment ago. Whatever is
        # sitting under *name* is not ours and is left exactly as it is.
        os.unlink(partial, dir_fd=parent_fd)
        raise CopySafetyError(
            f"{name} appeared in the destination while it was being copied. "
            "Nothing has been overwritten; the partial copy has been discarded"
        ) from exc
    except OSError as exc:
        os.unlink(partial, dir_fd=parent_fd)
        raise CopyError(f"cannot install {name} in the destination: {exc}") from exc
    os.unlink(partial, dir_fd=parent_fd)


def _write_manifest(
    plan: CopyPlan,
    result: CopyResult,
    root_fd: int,
    inventory: list,
    *,
    complete: bool,
) -> None:
    """Record what was copied, so the copy can be checked later.

    Lives beside the data and inherits its privacy: it names full paths and
    hashes. That is what makes it useful and why it belongs nowhere else.

    Written twice — once before copying with ``complete=False`` so an
    interrupted run is recognisable and resumable, once after with the full
    record. A manifest that still says ``complete: false`` is itself the
    signal that a copy did not finish.

    **Written atomically**, to a temporary name in the destination and renamed
    over the old one. A manifest half-written by an interrupted run would be
    invalid JSON, and this tool now refuses a destination whose manifest it
    cannot parse — so a torn write would strand the copy it exists to rescue.
    """
    entries = []
    if complete:
        for entry in plan.entries:
            parts = entry.parts
            info = _stat_through(root_fd, parts)
            entries.append(
                {
                    "path": entry.relative_path,
                    "size": info.st_size,
                    "modified": datetime.fromtimestamp(
                        info.st_mtime, timezone.utc
                    ).isoformat(),
                    "sha256": _sha256_through(root_fd, parts),
                    "recognised": entry.recognised,
                }
            )

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "complete": complete,
        "created": datetime.now(timezone.utc).isoformat(),
        "adapter": {
            "package_version": __version__,
            "decoder_schema_version": DECODER_SCHEMA_VERSION,
        },
        "source": str(plan.source),
        # Kept because it is what the person will recognise in the folder they
        # find later. **It is not the card's identity** and nothing decides a
        # resume on it: two cards behind one mount path are two cards, and that
        # is exactly the confusion this used to permit.
        "source_resolved": str(plan.resolved_source),
        # The identity a later run compares against: what was on the card when
        # this copy was planned. A resume requires every one of these to still
        # be there, at the same path and the same size.
        "source_inventory": [
            {"path": path, "size": size, "sha256": content}
            for path, size, content in sorted(inventory)
        ],
        "source_inventory_sha256": _inventory_fingerprint(inventory),
        "file_count": len(entries) if complete else len(plan.entries),
        "total_bytes": sum(e["size"] for e in entries) if complete else plan.total_bytes,
        "skipped_non_regular": list(plan.skipped_non_regular),
        "ignored": list(plan.ignored),
        "files": entries,
    }
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")

    temporary = MANIFEST_NAME + _PARTIAL_SUFFIX
    stale = _lstat_at(root_fd, temporary)
    if stale is not None and stat.S_ISREG(stale.st_mode):
        os.unlink(temporary, dir_fd=root_fd)
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            _MANIFEST_MODE,
            dir_fd=root_fd,
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, _MANIFEST_MODE, dir_fd=root_fd)
        os.replace(temporary, MANIFEST_NAME, src_dir_fd=root_fd, dst_dir_fd=root_fd)
    except OSError as exc:
        raise CopyError(f"the manifest could not be written: {exc}") from exc


def _inventory_fingerprint(items) -> str:
    """A digest of what the source holds, as a whole.

    Not an identity in the sense of "which physical card is this" — nothing on
    a card reliably says that, and a serial identifies the *device*, so two
    cards written by one ventilator share it. It is an identity in the sense
    that matters here: two sources with different contents are different
    sources, and must not be copied into one directory.

    **The content hash is part of it, and used not to be.** Path and size alone
    left a hole with a plausible way in: interrupt a copy, change a file to
    different content of the same length, resume — and the inventory still
    matched, so the resume was accepted and the changed bytes were copied in
    beside the ones already there. Same path and same size is not the same
    file, and on a card written by a device it is not even unlikely.

    *items* is a sequence of ``(path, size, sha256)``. It is taken as triples
    rather than as :class:`PlannedEntry` objects so the same canonical
    serialisation covers both sides of the comparison: the inventory this run
    computed, and the one a manifest recorded.
    """
    digest = hashlib.sha256()
    for path, size, content in sorted(items, key=lambda item: item[0]):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(content.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _inventory_items(entries, digests: dict[str, str]):
    """The canonical ``(path, size, sha256)`` triples for a set of entries."""
    return [
        (entry.relative_path, entry.size, digests[entry.relative_path])
        for entry in entries
    ]


def _recorded_items(recorded: list, shown: Path):
    """The same triples, read back out of a manifest and validated."""
    items = []
    for item in recorded:
        if not isinstance(item, dict):
            raise CopySafetyError(
                f"the {MANIFEST_NAME} in {shown} has a malformed source "
                "inventory, so it cannot show which card this destination "
                "belongs to"
            )
        path, size, content = item.get("path"), item.get("size"), item.get("sha256")
        if (
            not isinstance(path, str)
            or not isinstance(size, int)
            or not isinstance(content, str)
            or not _SHA256_RE.match(content)
        ):
            raise CopySafetyError(
                f"the {MANIFEST_NAME} in {shown} has a malformed source "
                "inventory entry, so it cannot show which card this "
                "destination belongs to"
            )
        items.append((path, size, content))
    return items


def _stat_through(root_fd: int, parts: tuple[str, ...]) -> os.stat_result:
    """``lstat`` a copied file by walking handles rather than a path."""
    fd, name = _resolve_through(root_fd, parts)
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    finally:
        _close_if_borrowed(fd, root_fd)


def _sha256_through(root_fd: int, parts: tuple[str, ...]) -> str:
    fd, name = _resolve_through(root_fd, parts)
    try:
        return _sha256_at(fd, name)
    finally:
        _close_if_borrowed(fd, root_fd)


def _resolve_through(root_fd: int, parts: tuple[str, ...]) -> tuple[int, str]:
    """Open the parent directory of *parts*, without following any link."""
    parent_fd = root_fd
    opened: list[int] = []
    try:
        for name in parts[:-1]:
            parent_fd = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
            opened.append(parent_fd)
    except OSError as exc:
        for fd in opened:
            os.close(fd)
        raise CopySafetyError(
            f"{'/'.join(parts)} could not be reached in the destination "
            f"without following a symbolic link: {exc}"
        ) from exc
    # Only the innermost handle is handed back; the rest are no longer needed.
    for fd in opened[:-1]:
        os.close(fd)
    return parent_fd, parts[-1]


def _close_if_borrowed(fd: int, root_fd: int) -> None:
    if fd != root_fd:
        os.close(fd)


# --------------------------------------------------------------------------
# safety
# --------------------------------------------------------------------------


def _check_safety(source: Path, destination: Path) -> tuple[Path, Path]:
    if not source.exists():
        raise CopyError(f"{source} does not exist")
    if not source.is_dir():
        raise CopyError(f"{source} is not a directory")

    try:
        resolved_source = source.resolve(strict=True)
    except OSError as exc:  # pragma: no cover - unreachable after exists()
        raise CopyError(f"{source} cannot be resolved: {exc}") from exc
    resolved_destination = destination.resolve()

    if resolved_source == resolved_destination:
        raise CopySafetyError("source and destination are the same directory")
    if resolved_source in resolved_destination.parents:
        raise CopySafetyError(
            "the destination lies inside the source, which would copy the card "
            "into itself"
        )
    if resolved_destination in resolved_source.parents:
        raise CopySafetyError(
            "the source lies inside the destination; copying would mix the card "
            "into an existing import"
        )
    return resolved_source, resolved_destination


def _inspect_destination(
    resolved_destination: Path, resolved_source: Path
) -> DestinationState:
    """Decide whether the destination may be written into, and how.

    Three outcomes and no fourth: it does not exist, it is empty, or it holds a
    manifest this tool wrote for this same source. **A directory holding
    anything else is refused**, including one holding a file merely *named*
    like a manifest — which is what the old check accepted.
    """
    if not resolved_destination.exists():
        return DestinationState(exists=False, resumable=False)
    if not resolved_destination.is_dir():
        raise CopyError(f"{resolved_destination} exists and is not a directory")

    info = os.stat(resolved_destination)
    identity = (info.st_dev, info.st_ino)

    try:
        root_fd = os.open(
            resolved_destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
    except OSError as exc:
        raise CopySafetyError(
            f"{resolved_destination} could not be opened as a directory: {exc}"
        ) from exc

    try:
        present = {
            name for name in os.listdir(root_fd) if name not in _IGNORED_NAMES
        }
        if not present:
            return DestinationState(exists=True, resumable=False, identity=identity)

        if MANIFEST_NAME not in present:
            raise CopySafetyError(
                f"{resolved_destination} is not empty and holds no "
                f"{MANIFEST_NAME}, so it is not a directory this tool created. "
                "Choose an empty one."
            )

        read = _read_manifest(root_fd, resolved_destination)
        if read is None:  # pragma: no cover - listed a moment ago
            raise CopySafetyError(
                f"the {MANIFEST_NAME} in {resolved_destination} disappeared "
                "while it was being read"
            )
        manifest, digest = read
        _require_our_manifest(manifest, resolved_source, resolved_destination)
        return DestinationState(
            exists=True,
            resumable=True,
            manifest_sha256=digest,
            identity=identity,
            manifest=manifest,
        )
    finally:
        os.close(root_fd)


def _read_manifest(root_fd: int, shown: Path) -> tuple[dict, str] | None:
    """The destination's manifest and the digest of its bytes, or ``None``.

    Opened with :data:`os.O_NOFOLLOW` and checked with ``fstat`` on the open
    handle, so a symlink under this name is refused rather than followed —
    and refused *before* anything is written, since the old code would have
    followed it and overwritten whatever it pointed at.
    """
    try:
        fd = os.open(MANIFEST_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise CopySafetyError(
                f"the {MANIFEST_NAME} in {shown} is a symbolic link. A copy "
                "will not follow one, and nothing has been written through it"
            ) from exc
        raise CopySafetyError(
            f"the {MANIFEST_NAME} in {shown} could not be read: {exc}"
        ) from exc

    # Checked on the raw descriptor before it is wrapped: opening a directory
    # as a stream raises on its own, with a message about file objects rather
    # than about what is actually wrong with the destination.
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CopySafetyError(
                f"the {MANIFEST_NAME} in {shown} is "
                f"{_describe_type(info.st_mode)}, not a regular file"
            )
        if info.st_size > _MAX_MANIFEST_BYTES:
            raise CopySafetyError(
                f"the {MANIFEST_NAME} in {shown} is {info.st_size} bytes, far "
                "larger than one this tool writes"
            )
    except Exception:
        os.close(fd)
        raise

    with os.fdopen(fd, "rb") as handle:
        raw = handle.read()

    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CopySafetyError(
            f"the {MANIFEST_NAME} in {shown} is not valid JSON, so this is not "
            f"a directory this tool wrote: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise CopySafetyError(
            f"the {MANIFEST_NAME} in {shown} is not a JSON object, so this is "
            "not a directory this tool wrote"
        )
    return manifest, hashlib.sha256(raw).hexdigest()


def _require_our_manifest(
    manifest: dict, resolved_source: Path, shown: Path
) -> None:
    """Refuse a manifest that is not one this build wrote.

    Presence used to be the whole test. It is now the least of them: the
    schema, the state and the recorded inventory all have to be there, because
    "there is a file with this name" is a claim anybody can make about a
    directory they want a copy written into.

    Whether the manifest belongs to *this source* is a separate question, and
    it is not answered here — see :func:`_require_same_source`, which needs the
    plan's inventory and therefore cannot run this early.
    """
    version = manifest.get("manifest_version")
    if version != MANIFEST_VERSION:
        if version == 1:
            raise CopySafetyError(
                f"the {MANIFEST_NAME} in {shown} was written by an older build "
                "that did not record which card it copied, so it cannot show "
                "this destination belongs to this source. Copy into a fresh "
                "directory instead"
            )
        raise CopySafetyError(
            f"the {MANIFEST_NAME} in {shown} declares manifest_version "
            f"{version!r} rather than {MANIFEST_VERSION}, so it is not one this "
            "build wrote"
        )

    if not isinstance(manifest.get("complete"), bool):
        raise CopySafetyError(
            f"the {MANIFEST_NAME} in {shown} does not say whether its copy "
            "finished, so it is not one this tool wrote"
        )
    adapter = manifest.get("adapter")
    if not isinstance(adapter, dict) or not isinstance(
        adapter.get("package_version"), str
    ):
        raise CopySafetyError(
            f"the {MANIFEST_NAME} in {shown} names no adapter version, so it is "
            "not one this tool wrote"
        )
    if not isinstance(manifest.get("files"), list):
        raise CopySafetyError(
            f"the {MANIFEST_NAME} in {shown} carries no file list, so it is not "
            "one this tool wrote"
        )

    recorded_inventory = manifest.get("source_inventory")
    fingerprint = manifest.get("source_inventory_sha256")
    if not isinstance(fingerprint, str) or not _SHA256_RE.match(fingerprint):
        raise CopySafetyError(
            f"the {MANIFEST_NAME} in {shown} carries no source inventory "
            "fingerprint, so its inventory cannot be shown to be the one this "
            "tool wrote"
        )
    if isinstance(recorded_inventory, list):
        # Recomputed from the recorded entries rather than trusted. A manifest
        # whose inventory has been edited — by hand, by a merge, by anything —
        # must not be able to declare a card's identity.
        if _inventory_fingerprint(
            _recorded_items(recorded_inventory, shown)
        ) != fingerprint:
            raise CopySafetyError(
                f"the {MANIFEST_NAME} in {shown} has a source inventory that "
                "does not match its own fingerprint, so it has been altered "
                "since this tool wrote it"
            )
    if not isinstance(manifest.get("source_inventory"), list):
        raise CopySafetyError(
            f"the {MANIFEST_NAME} in {shown} records no source inventory, so "
            "it cannot show which card this destination belongs to"
        )


def _require_same_source(
    manifest: dict, entries, digests: dict[str, str] | None, shown: Path
) -> None:
    """Refuse a destination that was filled from a different source.

    **The mount path is not the card.** ``/Volumes/CARD`` is whichever card is
    in the reader, so identifying a source by its resolved path meant that
    copying card A, ejecting it and inserting card B produced a destination the
    tool considered resumable — and it merged the two, since their files are
    disjoint and nothing collided. That was reproduced.

    What is compared instead is the inventory the earlier run recorded: every
    file it planned must still be present, at the same path, the same size
    **and with the same content**.

    **Content, because path and size were not enough.** Interrupt a copy,
    change a file to different content of the same length, resume: the
    inventory still matched, so the resume was accepted and the changed bytes
    were copied in beside the ones already there. On a card a device writes to,
    a same-size rewrite is not an exotic case.

    *digests* maps a relative path to the SHA-256 of its content. It is
    ``None`` at plan time, where this runs as a **cheap pre-check** on paths
    and sizes so that an obviously wrong card fails before anything is read in
    full; :func:`execute` calls it again with the hashes, and that call is the
    authoritative one. Nothing is written between the two.

    **Growth is allowed on purpose.** A device writes to its card, so a
    resumption hours after an interrupted copy legitimately sees files that did
    not exist before. Only disappearance and change are refused — and a
    different card is nothing but wholesale disappearance.
    """
    recorded = _recorded_items(manifest.get("source_inventory") or [], shown)
    present = {entry.relative_path: entry.size for entry in entries}

    missing: list[str] = []
    changed: list[str] = []
    rewritten: list[str] = []
    for path, size, content in recorded:
        if path not in present:
            missing.append(path)
        elif present[path] != size:
            changed.append(path)
        elif digests is not None and digests.get(path) != content:
            rewritten.append(path)

    if not missing and not changed and not rewritten:
        return

    # A different card is the wholesale case, and worth naming as such: it is
    # the mistake somebody is most likely to be in the middle of making.
    wholly_different = recorded and len(missing) == len(recorded)
    if wholly_different:
        raise CopySafetyError(
            f"{shown} holds a copy of a different source: not one of the "
            f"{len(recorded)} file(s) that copy recorded is present in the "
            "source now. A mount path is not a card — ejecting one and "
            "inserting another leaves the path unchanged. Resuming would mix "
            "two recordings into one directory, and archive filenames repeat "
            "across cards. Choose an empty directory"
        )
    if rewritten and not missing and not changed:
        raise CopySafetyError(
            f"{shown} holds a copy of a source that has since changed: "
            f"{len(rewritten)} file(s) are the same size as before but hold "
            "different content. Resuming would leave a directory holding some "
            "files from before the change and some from after, with no way to "
            "tell which is which. Choose an empty directory"
        )
    raise CopySafetyError(
        f"{shown} holds a copy of a source that has since changed: "
        f"{len(missing)} file(s) are gone, {len(changed)} differ in size and "
        f"{len(rewritten)} hold different content at the same size. A copy may "
        "resume onto a card that has grown, but not onto one whose existing "
        "files have moved or changed. Choose an empty directory"
    )


def _hash_source(
    plan: CopyPlan, source_root_fd: int, source_directories: dict[str, int]
) -> dict[str, str]:
    """SHA-256 every planned source file, through verified descriptors.

    Runs before anything is written, so a source that disagrees with the
    manifest is refused with the destination untouched.

    **This is a whole extra read of the card, and that is the price.** The
    manifest has to carry content hashes before copying starts — an interrupted
    run's manifest is exactly the one a resume validates against, so it cannot
    wait until the end — and the hashes have to come from descriptors this run
    opened and checked itself.

    The result is deliberately **not** reused as the digest each file is copied
    against. That would save the read and would put a second lookup back
    between hashing and copying, since this pass holds its own descriptors and
    closes them. Copying a card is a once-per-card operation, and the read is
    worth less than the property.
    """
    digests: dict[str, str] = {}
    for entry in plan.entries:
        parts = entry.parts
        parent_fd = _source_directory_fd_for(
            source_directories, source_root_fd, parts[:-1]
        )
        source_fd, _ = _open_source_file(parent_fd, parts[-1], entry)
        try:
            digests[entry.relative_path] = _digest_fd(source_fd, entry.relative_path)
        finally:
            os.close(source_fd)
    return digests


def _walk(root: Path) -> Iterator[tuple[str, Path]]:
    """Yield ``(kind, path)`` for everything the plan needs to know about.

    ``kind`` is ``"file"``, ``"ignored"`` for a pruned directory, or
    ``"symlink"`` for a directory link that was not followed.

    **Pruning happens before the descent, not after.** Filtering on the name of
    each *file* is not enough: the operating system's own directories are
    matched by their own names, so `.Trashes/whatever.zip` passes a per-file
    check and gets copied. That directory holds deleted files, and copying
    those is precisely what this tool promises not to do — a promise that,
    before this was fixed, it did not keep.
    """
    for current, directories, files in os.walk(root):
        directories.sort()
        for name in sorted(files):
            yield "file", Path(current) / name

        keep = []
        for name in directories:
            child = Path(current) / name
            if name in _IGNORED_NAMES:
                # Reported, then not descended into. Nothing inside is
                # planned, hashed or copied.
                yield "ignored", child
            elif child.is_symlink():
                # A link out of the card is exactly what must not be followed.
                yield "symlink", child
            else:
                keep.append(name)
        directories[:] = keep


def _describe_type(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "a symbolic link"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISFIFO(mode):
        return "named pipe"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
        return "device node"
    return "not a regular file"


# There is deliberately no path-based source hash any more. One existed, was
# called between planning and copying, and opened the source by name — which
# is what let a symlink introduced in between decide what got hashed. Reading
# the source now goes through _open_source_file and _digest_fd, and nothing
# else. Do not add a convenience wrapper that takes a path.


def _sha256_at(parent_fd: int, name: str) -> str:
    """Hash a file by name relative to an open directory, without following."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise CopyError(f"reading {name} failed: {exc}") from exc
    with os.fdopen(fd, "rb") as handle:
        return _digest(handle, name)


def _digest(handle, shown: str) -> str:
    digest = hashlib.sha256()
    try:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    except OSError as exc:
        raise CopyError(f"reading {shown} failed: {exc}") from exc
    return digest.hexdigest()


# --------------------------------------------------------------------------
# command
# --------------------------------------------------------------------------


def add_arguments(parser) -> None:
    """Declare the ``copy-card`` options.

    ``--source`` is required and has no default. This tool will never pick a
    removable volume for you: choosing the wrong one is not a mistake worth
    automating.
    """
    parser.add_argument(
        "--source",
        required=True,
        type=Path,
        metavar="DIR",
        help="the mounted card, named explicitly; no volume is ever auto-selected",
    )
    parser.add_argument(
        "--destination",
        required=True,
        type=Path,
        metavar="DIR",
        help="an empty directory, or one a previous copy of this card created",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be copied and stop",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="list every file (long, and names identifying paths)",
    )


def run(args, out: TextIO) -> int:
    from .cli import EXIT_IO, EXIT_OK, EXIT_SAFETY

    try:
        plan = plan_copy(args.source, args.destination)
    except CopySafetyError as exc:
        print(f"refusing to copy: {exc}", file=sys.stderr)
        return EXIT_SAFETY
    except CopyError as exc:
        print(f"cannot copy: {exc}", file=sys.stderr)
        return EXIT_IO

    # Shown before anything is written, so the wrong card can be caught by the
    # person who knows which one is right.
    print(f"  source       {plan.source}", file=out)
    print(f"  destination  {plan.destination}", file=out)
    print(
        f"  files        {len(plan.entries)}  "
        f"({plan.total_bytes / 1e6:.1f} MB)",
        file=out,
    )
    if plan.destination_state.resumable:
        print(
            "  resuming     an unfinished copy of this same card, verified "
            "against its manifest",
            file=out,
        )
    if plan.unrecognised:
        print(
            f"  unrecognised {len(plan.unrecognised)} file(s) — copied anyway and "
            "marked in the manifest",
            file=out,
        )
        for entry in plan.unrecognised[:10]:
            print(f"                 {entry.relative_path}", file=out)
    if plan.skipped_non_regular:
        print(
            f"  not copied   {len(plan.skipped_non_regular)} non-regular object(s):",
            file=out,
        )
        for item in plan.skipped_non_regular[:10]:
            print(f"                 {item}", file=out)

    if args.dry_run:
        print("\n  Dry run: nothing was written.", file=out)
        return EXIT_OK

    print(file=out)
    try:
        result = execute(plan, out=out if args.verbose else None)
    except CopySafetyError as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return EXIT_SAFETY
    except CopyError as exc:
        print(f"copy failed: {exc}", file=sys.stderr)
        return EXIT_IO

    print(
        f"  copied {len(result.copied)} file(s), "
        f"{result.bytes_written / 1e6:.1f} MB",
        file=out,
    )
    if result.already_present:
        print(
            f"  {len(result.already_present)} file(s) were already present and "
            "verified identical",
            file=out,
        )
    print(f"  manifest     {result.manifest_path}", file=out)
    print(
        "\n  The source was opened read-only and not written to. This program\n"
        "  cannot guarantee the operating system mounted the volume read-only.\n"
        "\n  The destination now holds personal health data. Keep it out of any\n"
        "  repository, and do not paste this output publicly: it names local\n"
        "  paths that can identify the device.",
        file=out,
    )
    return EXIT_OK
