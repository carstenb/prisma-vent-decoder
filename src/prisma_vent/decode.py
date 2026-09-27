"""Export a decoded archive in a documented, versioned format.

``inspect`` prints for a person to read. This writes for a program to consume,
which is a different promise: the layout is specified in
``docs/export-schema-v1.md``, it carries its own version, and it will not
change under a reader without that version changing too.

Several rules run through the whole format, and each one exists because its
opposite would quietly corrupt something:

*   **Missing is ``null``, never ``0``.** The device writes a plain zero for a
    sensor that is not attached, so zero already carries two meanings in the
    source. Emitting zero for something this exporter does not have would add
    a third, and nothing downstream could separate them.

*   **No ``NaN`` or ``Infinity``.** Those are not valid JSON. Python's encoder
    emits them anyway unless told not to, producing files that many parsers
    reject and others silently misread, so serialisation runs with
    ``allow_nan=False`` and a non-finite value is an error rather than output.

*   **Event ids stay numeric and unnamed.** No id has been identified. A
    ``"name"`` field would have to be invented, and an invented name in a
    machine-readable file is far more dangerous than one in a printout.

*   **Raw and converted values are both present, separately.** A wrong number
    can then be attributed to the byte decoding or to the conversion instead
    of being ambiguous.

*   **Times are device-local with an unresolved zone**, and the field name says
    so. They are computed from record and sample indices rather than by
    accumulating an interval, which drifts.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Sequence, TextIO

from . import DECODER_SCHEMA_VERSION, __version__
from .archive import ArchiveError, DayArchive, Session, open_day_archive
from .events import XmlError
from .parameter_registry import parameter_registries
from .statistic import StatisticError
from .timebase import TimebaseError, format_offset, therapy_day
from .wmedf import (
    DiagnosticPolicy,
    ViolationCollector,
    WmedfError,
    iter_chunks,
    read_header,
    sample_time,
)

__all__ = [
    "add_arguments",
    "run",
    "export_archive",
    "SignalSelectionError",
    "ExportExistsError",
    "ExportCleanupError",
    "ExportReplaceError",
    "EXPORT_SCHEMA_VERSION",
]


class SignalSelectionError(ValueError):
    """``--signals`` named a channel this archive does not offer.

    Mapped to the **usage** exit code rather than the structural one. Nothing
    failed to decode: the archive read correctly and the request could not be
    satisfied, so the remedy is always to change the command line. One rule
    covers a typo, an out-of-range index and a channel that some sessions lack,
    which is more predictable for a script than a subtle split between them.
    """


class ExportExistsError(ValueError):
    """The destination already holds an export, and it was not overwritten."""


class ExportCleanupError(OSError):
    """The export succeeded, but the previous one could not be removed.

    Distinct from :class:`ExportReplaceError`, and it must stay distinct: there
    the new export never landed, here it did. Confusing the two would tell
    somebody their data is missing when it is in place, or the reverse.
    """


class ExportReplaceError(OSError):
    """Replacing an export failed *and* the previous one could not be restored.

    Raised only in the one case where both moves fail. It exists so the message
    can say where the previous export survives, because the alternative — a
    cleanup that runs regardless — deletes exactly the data the transaction was
    protecting.
    """

#: The version a consumer should check. Documented in
#: ``docs/export-schema-v1.md`` together with what may change without it
#: rising and what may not.
EXPORT_SCHEMA_VERSION = 1

MANIFEST = "manifest.json"
SESSIONS = "sessions.jsonl"
EVENTS = "events.jsonl"
USAGE = "usage.jsonl"
PARAMETERS = "parameters.json"
VALIDATION = "validation-report.json"

#: Rough bytes per exported signal sample, for the estimate shown before a
#: signal export starts. Derived from the width of a row this exporter writes;
#: approximate on purpose, since its job is to warn rather than to predict.
_BYTES_PER_SAMPLE = 60


@dataclass(frozen=True)
class ExportResult:
    directory: Path
    session_count: int
    event_count: int
    signal_files: int
    bytes_written: int


def export_archive(
    path: Path,
    destination: Path,
    *,
    signals: Sequence[str] = (),
    max_samples: int | None = None,
    out: TextIO | None = None,
    overwrite: bool = False,
) -> ExportResult:
    """Write one archive's contents into *destination*.

    Signals are exported only for the channels named in *signals*: at 10 Hz
    across a night they dwarf everything else here, and a default that wrote
    them would make the common case unusable.

    **The export is assembled elsewhere and moved into place at the end.** It
    is written into a temporary sibling directory, the manifest is written
    last, and only then is the directory renamed. Three problems follow from
    writing in place, and all three are silent:

    * a run that fails halfway leaves a directory holding some files from this
      export and some from the last one, with a manifest that claims it is
      whole;
    * a second run with a different ``--signals`` selection leaves the first
      run's CSVs behind, so the export contains channels its own manifest does
      not account for;
    * a different archive that happens to share a filename overwrites the JSON
      but not the signal directories.

    An existing export is **never overwritten silently**. Its manifest is read
    first, and ``overwrite`` replaces exactly one thing: a complete export of
    this same archive, proved by a matching ``archive_sha256``. A destination
    holding an export of a *different* archive, or no readable manifest, is
    refused with or without the flag — see :func:`_refuse_or_clear`.
    """
    final = destination / path.stem
    if final.exists():
        _refuse_or_clear(final, path, overwrite)

    destination.mkdir(parents=True, exist_ok=True)
    # A sibling of the destination, so the final step is a rename within one
    # filesystem rather than a copy that can half-succeed.
    staging = Path(tempfile.mkdtemp(dir=destination, prefix=f".{path.stem}.partial-"))

    try:
        with open_day_archive(path) as archive:
            events = _write_events(archive, staging)
            _write_sessions(archive, staging)
            _write_parameters(archive, staging)
            _write_validation(archive, staging)
            usage_records = _write_usage(archive, staging)

            signal_files = 0
            if signals:
                signal_files = _write_signals(
                    archive, staging, signals, max_samples, out
                )

            # Last, so its presence means the export beside it is complete.
            _write_manifest(
                archive, staging, path, events, signal_files, usage_records
            )
            session_count = len(archive.sessions)

        written = sum(p.stat().st_size for p in staging.rglob("*") if p.is_file())
    except BaseException:
        # Including KeyboardInterrupt: a half-written export left in the
        # destination is exactly what this is here to prevent.
        shutil.rmtree(staging, ignore_errors=True)
        raise

    backup = final.with_name(f".{path.stem}.replaced-{os.getpid()}")
    backup_made = False
    try:
        if final.exists():
            os.rename(final, backup)
            backup_made = True
        os.rename(staging, final)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if backup_made and not final.exists():
            try:
                os.rename(backup, final)
            except OSError as restore_failure:
                # Both moves failed. The previous export now exists only under
                # the backup path, so it is left exactly where it is and its
                # location is named. Deleting it here — which an unconditional
                # cleanup used to do — would destroy the one complete export
                # this whole transaction exists to protect.
                raise ExportReplaceError(
                    f"could not put the new export at {final} ({exc}), and "
                    f"could not restore the previous one either "
                    f"({restore_failure}). The previous export has NOT been "
                    f"deleted: it is intact at {backup}. Move it back by hand "
                    f"once the cause is fixed"
                ) from restore_failure
        raise
    # Only now, with the new export in place, is the backup expendable. But a
    # failure to remove it is not nothing: it leaves a second, hidden copy of
    # somebody's health data on disk while the command reports success. It is
    # reported instead — and never retried by another route, since a second
    # deletion strategy is how the wrong directory gets removed.
    if backup_made:
        try:
            shutil.rmtree(backup)
        except OSError as exc:
            raise ExportCleanupError(
                f"the export was written successfully to {final}, and is "
                f"complete. What failed is removing the previous export, which "
                f"is still on disk at {backup} ({exc}). That is a second copy "
                f"of personal health data. Check it, then delete it yourself — "
                f"nothing here will try again"
            ) from exc

    return ExportResult(
        directory=final,
        session_count=session_count,
        event_count=events,
        signal_files=signal_files,
        bytes_written=written,
    )


#: Every file a complete export of this schema version contains. ``usage.jsonl``
#: is absent: a firmware without the long-term member legitimately produces an
#: export without it. Signal CSVs are absent for the same reason — they are
#: written only when asked for.
_REQUIRED_MEMBERS = (
    MANIFEST,
    SESSIONS,
    EVENTS,
    PARAMETERS,
    VALIDATION,
)

_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")


class _Existing:
    """What was found at a destination that already exists."""

    def __init__(self, complete: bool, reason: str = "", same_archive: bool = False):
        self.complete = complete
        self.reason = reason
        self.same_archive = same_archive


def _inspect_existing(final: Path, source: Path) -> _Existing:
    """Decide whether *final* is a complete export of *source*.

    **A manifest carrying a hash is not enough**, and treating it as enough was
    a real hole: a truncated run, a hand-made directory or a file called
    ``manifest.json`` containing ``{"archive_sha256": "..."}`` would all have
    licensed a recursive delete. What is checked instead:

    * the manifest is a regular file of valid UTF-8 JSON, and an object;
    * its ``export_schema_version`` is one this build writes;
    * ``archive_sha256`` is syntactically a SHA-256 digest;
    * ``archive_name`` is this source archive's name;
    * every file in :data:`_REQUIRED_MEMBERS` exists **as a regular file** —
      a symlink does not count, since following one would take the delete
      somewhere else entirely;
    * and only then, that the digest matches this archive's bytes.

    Contents are not revalidated. A required file may legitimately be empty,
    and re-reading an export to prove it decodes is a different job from
    deciding whether it is safe to replace. This establishes identity and
    structural completeness, which is what a delete permission needs.
    """
    manifest_path = final / MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return _Existing(False, "it has no manifest.json of its own")

    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return _Existing(False, "its manifest.json is not readable JSON")
    if not isinstance(loaded, dict):
        return _Existing(False, "its manifest.json is not a JSON object")

    version = loaded.get("export_schema_version")
    if version != EXPORT_SCHEMA_VERSION:
        return _Existing(
            False,
            f"its manifest declares export schema version {version!r}, and this "
            f"build writes version {EXPORT_SCHEMA_VERSION}",
        )

    digest = loaded.get("archive_sha256")
    if not isinstance(digest, str) or not _SHA256_PATTERN.match(digest):
        return _Existing(False, "its manifest has no valid archive_sha256")

    missing = [
        name
        for name in _REQUIRED_MEMBERS
        if (final / name).is_symlink() or not (final / name).is_file()
    ]
    if missing:
        return _Existing(
            False,
            f"it is missing {', '.join(missing)}, or has them as something "
            f"other than regular files, so the export is incomplete",
        )

    unexpected = _unexpected_entries(final, loaded.get("files", _NO_FIELD))
    if unexpected:
        shown = ", ".join(unexpected[:8])
        more = f" and {len(unexpected) - 8} more" if len(unexpected) > 8 else ""
        return _Existing(
            False,
            f"it holds entries this exporter did not write: {shown}{more}. "
            f"Replacing it would delete them, so it is left alone",
        )

    if loaded.get("archive_name") != source.name:
        return _Existing(
            True,
            f"it is an export of {loaded.get('archive_name')!r}, not of "
            f"{source.name!r}",
        )
    if digest != _sha256_of(source):
        return _Existing(
            True,
            "it is an export of a different archive that happens to share this "
            "filename (sha256 differs)",
        )
    return _Existing(True, same_archive=True)


#: Sentinel telling "the manifest has no files field" apart from "it has one
#: that is null or malformed". Only the first falls back to the allowlist: a
#: broken declaration is a reason to refuse, not a reason to trust less.
_NO_FIELD = object()


def _unexpected_entries(final: Path, declared: object) -> list[str]:
    """Everything about *final* that stops it counting as this exporter's work.

    Two routes, and which one applies is decided by whether the manifest
    declares its files at all — never by whether the declaration looks
    convenient.

    **Declared route.** Since this build records every path it produced under
    ``files``, the check is an exact correspondence: every declared path exists
    as a regular file, every regular file on disk is declared, and every
    directory present is one the declared paths imply. Subset-checking would
    not be enough — a manifest could declare a path it never wrote, and a file
    later dropped there would then pass as the exporter's own.

    **Legacy route**, used only when ``files`` is absent, for exports written
    by an earlier build of this same schema version. It permits exactly the
    required members, ``usage.jsonl``, and ``session_NNNN/signal_NN_*.csv``.
    Its limit is worth stating: a file that happens to match those names cannot
    be told from one this tool wrote, so a hand-made
    ``session_0001/signal_00_notes.csv`` would be treated as ours. The declared
    list has no such gap.

    Symlinks are never accepted under either route: a symlink is not something
    this exporter writes, and following one during a delete leads somewhere it
    has no business going.
    """
    if declared is _NO_FIELD:
        return _legacy_problems(final)
    return _declared_problems(final, declared)


def _path_fault(entry: object) -> str | None:
    """Why *entry* is not usable as a declared relative path, or ``None``.

    Rejects rather than normalises. A path that needs normalising before it can
    be compared is a path whose meaning depends on who is comparing it, and
    this comparison decides whether a directory may be deleted.
    """
    if not isinstance(entry, str):
        return f"{entry!r} is not a string"
    if not entry:
        return "an entry is empty"
    if "\\" in entry:
        return f"{entry!r} contains a backslash, which is not a POSIX separator"
    if entry.startswith("/"):
        return f"{entry!r} is an absolute path"
    if entry.endswith("/"):
        return f"{entry!r} ends with a slash, so it names a directory"
    segments = entry.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return f"{entry!r} is not canonical: it has an empty, '.' or '..' segment"
    return None


def _declared_problems(final: Path, declared: object) -> list[str]:
    if not isinstance(declared, list):
        return ["its manifest's 'files' is not a list"]

    problems = [
        fault for entry in declared if (fault := _path_fault(entry)) is not None
    ]
    if problems:
        return sorted(set(problems))

    names = [str(entry) for entry in declared]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        return [f"its manifest lists {name!r} more than once" for name in duplicates]

    allowed = set(names)
    if MANIFEST not in allowed:
        return [f"its manifest does not list {MANIFEST} among its own files"]

    on_disk: set[str] = set()
    for item in sorted(final.rglob("*")):
        relative = item.relative_to(final).as_posix()
        if item.is_symlink():
            problems.append(f"{relative} (symbolic link)")
        elif item.is_dir():
            continue                      # judged below, against the implied set
        elif item.is_file():
            on_disk.add(relative)
        else:
            problems.append(f"{relative} (not a regular file)")

    problems += sorted(on_disk - allowed)
    problems += sorted(
        f"{name} (declared but missing)" for name in allowed - on_disk
    )

    implied = {parent for name in allowed for parent in _parents_of(name)}
    directories = {
        item.relative_to(final).as_posix()
        for item in final.rglob("*")
        if item.is_dir() and not item.is_symlink()
    }
    problems += sorted(f"{name}/ (directory)" for name in directories - implied)
    return sorted(set(problems))


def _legacy_problems(final: Path) -> list[str]:
    problems = []
    for item in sorted(final.rglob("*")):
        relative = item.relative_to(final).as_posix()
        if item.is_symlink():
            problems.append(f"{relative} (symbolic link)")
        elif item.is_dir():
            continue
        elif not item.is_file():
            problems.append(f"{relative} (not a regular file)")
        elif not _matches_allowlist(relative):
            problems.append(relative)
    return sorted(set(problems))


def _parents_of(relative: str) -> set[str]:
    parts = relative.split("/")[:-1]
    return {"/".join(parts[: n + 1]) for n in range(len(parts))}


_SIGNAL_CSV = re.compile(r"\Asession_\d{4}/signal_\d{2}_[A-Za-z0-9._-]+\.csv\Z")


def _matches_allowlist(relative: str) -> bool:
    if relative in _REQUIRED_MEMBERS or relative == USAGE:
        return True
    return bool(_SIGNAL_CSV.match(relative))


def _refuse_or_clear(final: Path, source: Path, overwrite: bool) -> None:
    """Decide what to do about a destination that already exists.

    **``overwrite`` replaces one thing only: a structurally complete export of
    this same archive.** Everything else is refused, with or without it:

    ==============================================  =========  ===============
    destination                                     default    ``overwrite``
    ==============================================  =========  ===============
    complete export of this archive, matching hash  refused    **replaced**
    complete export of a different archive          refused    refused
    incomplete, damaged, or not an export at all    refused    refused
    ==============================================  =========  ===============

    The permissive reading — let ``overwrite`` replace anything it recognises —
    was rejected deliberately. Archive filenames repeat across cards, since the
    counter restarts with the device rather than with the card, so
    ``0123_2020-01-01.zip`` from two cards is two different people's nights
    under one name. A flag meant to say "redo this export" would then quietly
    destroy an export of something else. Moving the old directory aside is one
    command and cannot go wrong by accident.
    """
    found = _inspect_existing(final, source)

    if not found.complete:
        raise ExportExistsError(
            f"{final} already exists but is not recognisably a complete export "
            f"from this tool: {found.reason}. It may be an unfinished run or "
            f"somebody else's directory. Refusing to write into it or replace "
            f"it, even with --overwrite. Move it aside yourself, or choose "
            f"another destination"
        )

    if not found.same_archive:
        raise ExportExistsError(
            f"{final} already holds an export of a different archive: "
            f"{found.reason}. Archive names repeat across cards, so this is "
            f"very likely another recording. Refusing to replace it even with "
            f"--overwrite. Choose another destination, or move the old export "
            f"aside yourself"
        )

    if not overwrite:
        raise ExportExistsError(
            f"{final} already holds a complete export of this same archive "
            f"(matching sha256). Pass --overwrite to redo it. It is not reused "
            f"as-is because it may have been written with different options"
        )


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# writers
# --------------------------------------------------------------------------


def _dump(obj: object) -> str:
    """Serialise one JSON value under this format's rules."""
    return json.dumps(obj, allow_nan=False, sort_keys=True, ensure_ascii=False)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_dump(row) + "\n")


def _write_sessions(archive: DayArchive, directory: Path) -> None:
    rows = []
    for session in archive.sessions:
        rows.append(
            {
                "session": session.number,
                "therapy_day": session.therapy_day.isoformat(),
                "start_device_local": session.start.value.isoformat(),
                "stop_device_local": session.stop.value.isoformat(),
                "duration_seconds": session.duration.total_seconds(),
                "accounted_seconds": session.accounted_duration.total_seconds(),
                # Reported, not bounded: neither tail of this is explained.
                "duration_discrepancy_seconds": (
                    session.duration_discrepancy.total_seconds()
                ),
                # Also reported, not bounded. Up to one second is the signal
                # header's truncation to whole seconds; nothing published
                # bounds the rest, so nothing here rejects on it. May be
                # negative. Added within schema version 1.
                "start_skew_seconds": session.start_skew.total_seconds(),
                "record_count": session.header.n_records,
                "record_duration_seconds": session.header.record_duration_s,
                "channels": [
                    {
                        "index": s.index,
                        "label": s.label,
                        "unit": s.unit or None,
                        "samples_per_record": s.samples_per_record,
                        "sampling_rate_hz": s.sampling_rate_hz(
                            session.header.record_duration_s
                        ),
                        "bits": s.width_bytes * 8,
                        "digital_min": s.digital_min,
                        "digital_max": s.digital_max,
                        "physical_min": s.physical_min,
                        "physical_max": s.physical_max,
                    }
                    for s in session.header.signals
                ],
                "provenance": _provenance(session),
            }
        )
    _write_jsonl(directory / SESSIONS, rows)


def _write_events(archive: DayArchive, directory: Path) -> int:
    """All three kinds of event in one file, told apart by ``kind``.

    Respiratory events belong to a session and count from its start; the
    day-level ones count from the archive's own midnight. Keeping them in one
    file with explicit kinds makes that difference visible rather than
    implicit in which file a row came from.
    """
    rows: list[dict] = []

    for session in archive.sessions:
        for event in session.events.events:
            rows.append(
                {
                    "kind": "respiratory",
                    "session": session.number,
                    "id": event.id,  # deliberately unnamed
                    "phase": event.phase,
                    "strength": event.strength,
                    "seconds_from_session_start": event.seconds,
                    "device_local": (
                        session.start.value + timedelta(seconds=event.seconds)
                    ).isoformat(),
                }
            )

    try:
        day_log = archive.day_log()
    except ArchiveError:
        day_log = None
    if day_log is not None:
        for event in day_log.events:
            rows.append(
                {
                    "kind": "device_state",
                    "session": None,
                    "id": event.id,
                    "status": event.status,  # None means "not a state change"
                    "offset_from_archive_midnight": format_offset(event.time),
                }
            )

    try:
        alarm_log = archive.alarm_log()
    except ArchiveError:
        alarm_log = None
    if alarm_log is not None:
        for alarm in alarm_log.alarms:
            rows.append(
                {
                    "kind": "alarm",
                    "session": None,
                    "id": alarm.id,
                    "phase": alarm.phase,
                    "offset_from_archive_midnight": format_offset(alarm.time),
                }
            )

    _write_jsonl(directory / EVENTS, rows)
    return len(rows)


def _write_parameters(archive: DayArchive, directory: Path) -> None:
    try:
        parameter_map = archive.parameter_map()
        log = archive.parameter_log(parameter_map)
    except ArchiveError as exc:
        # Both registries appear here too, empty. Every shape of this file then
        # carries the same top-level fields, so a consumer never has to tell
        # "absent" from "empty".
        (directory / PARAMETERS).write_text(
            _dump(
                {
                    "available": False,
                    "reason": str(exc),
                    "scales": {},
                    "scale_candidates": {},
                    "value_domains": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return

    snapshots = []
    for snapshot in log.snapshots:
        snapshots.append(
            {
                "offset_from_archive_midnight": format_offset(snapshot.time),
                "device_parameters": [
                    _parameter(p, parameter_map) for p in snapshot.device_parameters
                ],
                "therapy_programs": [
                    {
                        "program": program.position,
                        "parameters": [
                            _parameter(p, parameter_map) for p in program.parameters
                        ],
                    }
                    for program in snapshot.therapy_programs
                ],
                # Reported as written. Whether it indexes the same space as the
                # program attribute is evidence, not something this file asserts.
                "active_program_raw": (
                    _parameter(snapshot.active_program_raw, parameter_map)
                    if snapshot.active_program_raw is not None
                    else None
                ),
                "notes": list(snapshot.notes),
            }
        )

    scales, candidates, domains = parameter_registries(
        parameter_map, log.config_version
    )
    (directory / PARAMETERS).write_text(
        _dump(
            {
                "available": True,
                "map_version": parameter_map.version,
                "config_version": log.config_version,
                "scales": scales,
                "scale_candidates": candidates,
                "value_domains": domains,
                "snapshots": snapshots,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _parameter(value, parameter_map) -> dict:
    return {
        "id": value.id,
        "name": parameter_map.names.get(value.id),
        "position": value.position,
        "program": value.program,
        # Raw, always — for parameters with a published conversion as much as
        # for the rest. What is known about converting a value, and how well,
        # is in `scales` and `scale_candidates`; nothing is applied here.
        "value_raw": value.value,
    }


def _write_validation(archive: DayArchive, directory: Path) -> None:
    from .validate import _check_session, _long_term_index, target_volume_setting

    # Both answer a question about the archive rather than about a session, so
    # both are asked once. The first parses a member covering the device's
    # whole year; the second parses the settings and their name map. Left to
    # their defaults, each session would re-read them.
    long_term = _long_term_index(archive)
    target = target_volume_setting(archive)
    sessions = []
    for session in archive.sessions:
        report = _check_session(
            archive, session, long_term=long_term, target=target
        )
        sessions.append(
            {
                "session": session.number,
                "checks": {
                    name: {
                        "status": result.status.value,
                        "detail": result.detail,
                        "ratio": result.ratio,
                    }
                    for name, result in report.checks.items()
                },
                "range_violations": report.violations.total_seen,
                "duration_discrepancy_seconds": report.duration_discrepancy_s,
            }
        )
    (directory / VALIDATION).write_text(
        _dump({"sessions": sessions}) + "\n", encoding="utf-8"
    )


def _write_signals(
    archive: DayArchive,
    directory: Path,
    wanted: Sequence[str],
    max_samples: int | None,
    out: TextIO | None,
) -> int:
    # Resolve every session before writing any of them, so a selector that
    # fails on the third session does not leave two sessions' worth of CSV
    # behind. Channel layout is read per session, so this cannot be checked
    # once against the archive.
    resolved = {
        session.number: _resolve_channels(session, wanted)
        for session in archive.sessions
    }
    written = 0
    for session in archive.sessions:
        chosen = resolved[session.number]
        if not chosen:
            continue
        session_dir = directory / f"session_{session.number:04d}"
        session_dir.mkdir(parents=True, exist_ok=True)

        for signal in chosen:
            total = session.header.n_records * signal.samples_per_record
            limit = total if max_samples is None else min(total, max_samples)
            if out is not None:
                print(
                    f"  session {session.number:04d} {signal.label}: "
                    f"{limit} samples, roughly "
                    f"{limit * _BYTES_PER_SAMPLE / 1e6:.1f} MB",
                    file=out,
                )
            written += 1
            _write_signal_csv(archive, session, signal, session_dir, limit)
    return written


def _write_signal_csv(
    archive: DayArchive, session: Session, signal, directory: Path, limit: int
) -> None:
    slug = "".join(c.lower() if c.isalnum() else "-" for c in signal.label).strip("-")
    path = directory / f"signal_{signal.index:02d}_{slug}.csv"

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["sample_index", "device_local_time", "digital_value", "physical_value", "unit"]
        )
        unit = signal.unit or ""
        emitted = 0
        with archive.open_signal(session) as stream:
            header = read_header(stream)
            for chunk in iter_chunks(
                stream,
                header,
                policy=DiagnosticPolicy.COLLECT_VIOLATIONS,
                violations=ViolationCollector(),
            ):
                if chunk.signal.index != signal.index:
                    continue
                for offset, (digital, physical) in enumerate(
                    zip(chunk.digital, chunk.physical, strict=True)
                ):
                    index = chunk.start_sample + offset
                    if emitted >= limit:
                        return
                    writer.writerow(
                        [
                            index,
                            # Derived from the indices, never accumulated.
                            sample_time(header, chunk.signal, index).value.isoformat(),
                            digital,
                            repr(physical),
                            unit,
                        ]
                    )
                    emitted += 1


def _resolve_channels(session: Session, wanted: Sequence[str]) -> list:
    """Resolve ``--signals`` selectors against one session's channels.

    **Every selector must resolve.** An unknown label or an out-of-range index
    used to be skipped, which meant a typo produced a successful export missing
    exactly the channel that was asked for — the quiet omission this package
    exists to avoid. A caller who mistypes a channel deserves to be told, not
    handed a smaller export that looks complete.

    Selectors are **deduplicated by channel index**, so naming a channel twice —
    or once by label and once by index — writes one file and counts one file.

    Channel layout is read per session, so a channel present in one session and
    absent from another is possible. That is reported against the session it
    failed in, since which session is missing it is the useful part.
    """
    signals = session.header.signals
    chosen: dict[int, object] = {}
    for name in wanted:
        stripped = name.strip()
        if stripped.lstrip("+-").isdigit():
            index = int(stripped)
            if not 0 <= index < len(signals):
                raise SignalSelectionError(
                    f"session {session.number:04d} has channels 0 to "
                    f"{len(signals) - 1}; --signals asked for index {index}"
                )
            signal = signals[index]
        else:
            matches = [s for s in signals if s.label == stripped]
            if not matches:
                raise SignalSelectionError(
                    f"session {session.number:04d} has no channel labelled "
                    f"{stripped!r}. --signals takes a label exactly as the file "
                    f"spells it, or a channel index"
                )
            if len(matches) > 1:
                # Labels are not guaranteed unique; the index is the identity.
                raise SignalSelectionError(
                    f"session {session.number:04d} has {len(matches)} channels "
                    f"labelled {stripped!r}, so the name does not identify one. "
                    f"Use the channel index instead"
                )
            signal = matches[0]
        chosen[signal.index] = signal
    return [chosen[index] for index in sorted(chosen)]


def _write_usage(archive: DayArchive, directory: Path) -> int | None:
    """Export the device's own long-term record, one row per session.

    This member is unlike every other one in the archive: it describes the
    device's whole past year rather than this archive's day, so the same rows
    appear in every archive exported from one card. A consumer merging several
    exports must deduplicate, and ``start_device_local`` is the key to do it
    with.

    **Deliberately not aggregated.** Producing a therapy-time-per-day series
    from this is one line of consumer code, but the day boundary is a decision:
    this device's therapy day runs noon to noon, and a consumer comparing
    against another source may want midnight instead. ``therapy_day`` is given
    so the noon-to-noon binning need not be re-derived, and the rows are left
    unaggregated so the choice stays with whoever makes it.

    Returns the number of rows, or ``None`` when the archive has no such member
    — which is not an error, and is reported as ``null`` rather than ``0`` so a
    firmware that omits it is distinguishable from one that records nothing.
    """
    try:
        statistic = archive.statistic()
    except (ArchiveError, StatisticError):
        return None

    rows = []
    for record in statistic.records:
        rows.append(
            {
                "start_device_local": record.start.value.isoformat(),
                "raw_timestamp": record.raw_timestamp,
                "therapy_day": therapy_day(record.start).isoformat(),
                "duration_minutes": record.duration_minutes,
                # Never exceeds the duration; nothing else about it is known.
                "second_quantity": record.second_quantity,
                "duration_by_program": list(record.duration_by_program),
                # Indices, not names. The manual lists eleven ventilation modes
                # and one recording matches one index, which is a coincidence
                # rather than an ordering for eleven categories.
                "duration_by_category": list(record.duration_by_category),
                "second_by_program": list(record.second_by_program),
                "second_by_category": list(record.second_by_category),
            }
        )
    _write_jsonl(directory / USAGE, rows)
    return len(rows)


def _write_manifest(
    archive: DayArchive,
    directory: Path,
    source: Path,
    event_count: int,
    signal_files: int,
    usage_records: int | None,
) -> None:
    adapter = archive.sessions[0].provenance.adapter if archive.sessions else None
    # Everything written so far, plus the manifest itself, which is written
    # last and so is not on disk yet. Declaring the file list is what lets a
    # later run tell this export's own files from anything added beside them —
    # and refuse to delete a directory holding the latter.
    produced = sorted(
        [MANIFEST]
        + [
            item.relative_to(directory).as_posix()
            for item in directory.rglob("*")
            if item.is_file()
        ]
    )
    (directory / MANIFEST).write_text(
        _dump(
            {
                "export_schema_version": EXPORT_SCHEMA_VERSION,
                "archive_name": source.name,
                "archive_sha256": archive.sha256,
                "therapy_day": archive.archive_date.isoformat(),
                "day_number": archive.day_number,
                "session_count": len(archive.sessions),
                "event_count": event_count,
                "signal_files": signal_files,
                "usage_records": usage_records,
                "files": produced,
                "unrecognised_members": list(archive.unknown_members),
                "adapter": {
                    "package_version": __version__,
                    "decoder_schema_version": DECODER_SCHEMA_VERSION,
                    "git_commit": adapter.git_commit if adapter else None,
                    "dirty": adapter.dirty if adapter else None,
                },
                "time_base": (
                    "All times are the device's own clock. Its relationship to "
                    "UTC is not established, so they carry no zone and must not "
                    "be converted."
                ),
                "events": (
                    "Event and alarm ids are reported as numbers. None has been "
                    "identified; there is deliberately no name field."
                ),
                "usage": (
                    "usage.jsonl is the device's own long-term record and "
                    "covers its whole past year, not this archive's day. The "
                    "same rows appear in every export from one card; "
                    "deduplicate on start_device_local when merging."
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _provenance(session: Session) -> dict:
    provenance = session.provenance
    return {
        "archive_sha256": provenance.archive_sha256,
        "archive_name": provenance.archive_name,
        "member_name": provenance.member_name,
        "session_number": provenance.session_number,
        # Excludes the adapter version on purpose: re-importing with a newer
        # decoder must replace rather than duplicate.
        "identity": provenance.identity(f"session-{session.number:04d}"),
    }


# --------------------------------------------------------------------------
# command
# --------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("archive", nargs="+", type=Path)
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        metavar="DIR",
        help="directory to write into; one subdirectory per archive",
    )
    parser.add_argument(
        "--signals",
        default="",
        metavar="CHANNELS",
        help=(
            "comma-separated channel labels or indices to export as CSV. "
            "Omitted by default: at 10 Hz across a night these dwarf "
            "everything else"
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "redo an existing export of this same archive. Replaces nothing "
            "else: a destination holding an export of a different archive, or "
            "no readable manifest at all, is refused with or without this flag"
        ),
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=None,
        metavar="N",
        help="cap the samples written per channel",
    )


def run(args: argparse.Namespace, out: TextIO) -> int:
    from .cli import EXIT_IO, EXIT_OK, EXIT_STRUCTURAL, EXIT_USAGE

    wanted = [s.strip() for s in args.signals.split(",") if s.strip()]
    status = EXIT_OK
    for path in args.archive:
        try:
            result = export_archive(
                path,
                args.output,
                signals=wanted,
                max_samples=args.max_samples,
                out=out,
                overwrite=args.overwrite,
            )
        except SignalSelectionError as exc:
            # The command line asked for something the data does not offer.
            print(f"{path.name}: {exc}", file=sys.stderr)
            return EXIT_USAGE
        except ExportExistsError as exc:
            print(f"{path.name}: {exc}", file=sys.stderr)
            status = EXIT_STRUCTURAL
            continue
        except ExportReplaceError as exc:
            # Printed on its own rather than under "cannot write export",
            # because the important part is where the surviving copy is.
            print(f"{path.name}: {exc}", file=sys.stderr)
            return EXIT_IO
        except ExportCleanupError as exc:
            # The export itself is fine; what is left is a stray copy of health
            # data. Non-zero because the operation did not finish, but the
            # message must not read as though the export failed.
            print(f"{path.name}: {exc}", file=sys.stderr)
            return EXIT_IO
        except (ArchiveError, WmedfError, XmlError, TimebaseError) as exc:
            print(f"{path.name}: {exc}", file=sys.stderr)
            status = EXIT_STRUCTURAL
            continue
        except FileNotFoundError:
            print(f"{path}: no such file", file=sys.stderr)
            status = EXIT_STRUCTURAL
            continue
        except OSError as exc:
            print(f"{path.name}: cannot write export: {exc}", file=sys.stderr)
            return EXIT_IO

        print(
            f"  {path.name} -> {result.directory}  "
            f"({result.session_count} sessions, {result.event_count} events, "
            f"{result.signal_files} signal file(s), "
            f"{result.bytes_written / 1e6:.2f} MB)",
            file=out,
        )

    print(
        "\n  The export contains personal health data. Format version "
        f"{EXPORT_SCHEMA_VERSION}; see docs/export-schema-v1.md.",
        file=out,
    )
    return status
