"""Structural contract for the version 1 export.

``docs/export-schema-v1.md`` promises that existing fields will not be removed
and that a field's type will not change. Nothing compared the export against
that promise, so this module builds a machine-readable *contract* — every
field path an export writes, with its permitted JSON types and whether it is
always present — and compares a later export against it.

**This is a structural check, not a semantic one.** It cannot see that a field
kept its name and changed what it means. A guard that appeared to cover more
than it does would be worse than none, so the name says `structural`
everywhere and the limits are stated in
:func:`compare_backward_compatible`.

The module doubles as the generator. Run it against any checkout to dump that
checkout's contract:

    PYTHONPATH=<tree>/src:<tree>/tests python3 tests/schema_contract.py OUT.json

which is how the frozen contract for a published tag is produced.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import prisma_vent

#: Every output form the export can write. Deliberately a constant rather than
#: something derived from ``docs/export-schema-v1.md``: the document's headings
#: are not uniform — ``## Signal CSV`` carries no backticks and names no fixed
#: file — so a parser would silently skip it and then report full coverage.
#: Reporting coverage for something never examined is the failure this list
#: exists to prevent. Add a form here when the export learns to write one.
COVERED_FORMS = (
    "manifest.json",
    "sessions.jsonl",
    "events.jsonl[kind=respiratory]",
    "events.jsonl[kind=device_state]",
    "events.jsonl[kind=alarm]",
    "parameters.json[available=true]",
    "parameters.json[available=false]",
    "usage.jsonl",
    "validation-report.json",
    "signal-csv-header",
)

#: Objects whose **keys are data**, not schema. Their members come and go as
#: what the decoder knows changes, so a contract must freeze the shape of an
#: entry and never the current membership.
#:
#: Without this, the contract of the next release would carry
#: ``/scale_candidates/EPAP/unit``. Confirming that candidate later — moving it
#: to ``scales`` — would delete those paths, and the guard would report the
#: success as a removed field needing schema version 2. The one thing this
#: project most wants to happen would have been the one thing the guard
#: forbade.
#:
#: See :func:`compare_backward_compatible` for the other half: an emptied map
#: is "no entries right now", not "the entry shape is gone".
#:
#: **Two levels of this exist.** A value domain is keyed by parameter name, and
#: the labels inside it are keyed by the *raw value observed* — which is the
#: most volatile data in the whole export, since observing one more value adds
#: a key. Missing the nested one would freeze "these are the values that had
#: been seen by the time of that release" into a contract that may never be
#: edited.
DYNAMIC_MAP_PATHS = frozenset(
    {
        "/scales",
        "/scale_candidates",
        "/value_domains",
        "/value_domains/*/observed_labels",
    }
)

ARCHIVE_DATE = date(2020, 3, 4)


# --------------------------------------------------------------------------
# Typing and flattening
# --------------------------------------------------------------------------


def json_type(value: Any) -> str:
    """Name the JSON type of *value*.

    ``bool`` is tested **before** ``int`` on purpose: in Python ``bool`` is a
    subclass of ``int``, so a plain ``isinstance(value, int)`` reports ``true``
    as an integer. A field that changed from boolean to 0/1 would then pass
    unnoticed — exactly the regression this module exists to catch.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise TypeError(f"value of unexpected type {type(value)!r} in an export")


def flatten(value: Any, prefix: str = "") -> dict[str, str]:
    """Map every path in *value* to its JSON type.

    Array indices collapse to ``*`` — ``/snapshots/*/device_parameters/*/id``,
    never ``/snapshots/0/device_parameters/3/id``. Without that the contract
    would encode the size of the fixture that produced it, and adding a second
    session to a fixture would read as a schema change.

    **The keys of a :data:`DYNAMIC_MAP_PATHS` object stay whole here** and
    collapse in :func:`_merge` instead. Collapsing them at this level lost the
    only information that says whether an entry shape holds: one dict cannot
    hold ``/scale_candidates/*/raw_delta`` both present and absent, so the
    entry that had the field overwrote the entry that did not, and a registry
    where one candidate had lost a required field read as intact. Merging is
    where presence is decided, so it is where the collapse belongs.
    """
    paths = {prefix or "/": json_type(value)}
    if isinstance(value, dict):
        for key, item in value.items():
            paths.update(flatten(item, f"{prefix}/{key}"))
    elif isinstance(value, list):
        for item in value:
            paths.update(flatten(item, f"{prefix}/*"))
    return paths


def _split_dynamic(path: str) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """Split one concrete path into (entry, shape, collapsed path).

    *entry* names the dynamic-map entries the path sits inside, innermost last
    — ``("/scale_candidates/EPAP",)`` — and is what presence is counted over.
    *shape* is the same with the data keys dropped, so that entries of one map
    are only ever compared against entries of that map. *collapsed* is the
    path as the contract records it, with every data key replaced by ``*``.

    A path outside every dynamic map has an empty entry and shape, which makes
    the record itself the unit and reproduces the ordinary per-record count.
    """
    entry: list[str] = []
    shape: list[str] = []
    collapsed = ""
    for segment in path.split("/")[1:]:
        if collapsed in DYNAMIC_MAP_PATHS:
            entry.append(f"{collapsed}/{segment}")
            shape.append(collapsed)
            collapsed = f"{collapsed}/*"
        else:
            collapsed = f"{collapsed}/{segment}"
    return tuple(entry), tuple(shape), collapsed or "/"


def _merge(records: list[dict[str, str]]) -> dict[str, dict]:
    """Fold per-record path maps into one form description.

    A path seen in every record is ``required``; one seen in some is
    ``optional``. Types accumulate, so a field that is a string in one record
    and ``null`` in another carries both.

    **Each entry of a dynamic map counts separately.** Its keys collapse to
    ``*`` here, and the unit presence is measured over is the entry rather
    than the record — otherwise ``/scale_candidates/*/raw_delta`` would read as
    ``required`` because *some* candidate still had it, and a candidate that
    had lost the field would pass. The denominator is scoped to the map the
    entry belongs to, so an entry of one registry is never counted against
    another's.
    """
    if not records:
        return {}
    seen: dict[str, set[tuple[int, tuple[str, ...]]]] = {}
    universe: dict[tuple[str, ...], set[tuple[int, tuple[str, ...]]]] = {}
    shapes: dict[str, tuple[str, ...]] = {}
    types: dict[str, set[str]] = {}
    for index, record in enumerate(records):
        universe.setdefault((), set()).add((index, ()))
        for path, kind in record.items():
            entry, shape, collapsed = _split_dynamic(path)
            unit = (index, entry)
            seen.setdefault(collapsed, set()).add(unit)
            universe.setdefault(shape, set()).add(unit)
            shapes[collapsed] = shape
            types.setdefault(collapsed, set()).add(kind)
    return {
        path: {
            "presence": (
                "required"
                if len(seen[path]) == len(universe[shapes[path]])
                else "optional"
            ),
            "types": sorted(types[path]),
        }
        for path in sorted(seen)
    }


# --------------------------------------------------------------------------
# Reading one export directory into forms
# --------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def forms_from_export(directory: Path) -> dict[str, dict]:
    """Describe every output form present in one export directory."""
    forms: dict[str, list[dict[str, str]]] = {}

    def add(name: str, obj: Any) -> None:
        forms.setdefault(name, []).append(flatten(obj))

    manifest = directory / "manifest.json"
    if manifest.exists():
        add("manifest.json", json.loads(manifest.read_text("utf-8")))

    sessions = directory / "sessions.jsonl"
    if sessions.exists():
        for row in _read_jsonl(sessions):
            add("sessions.jsonl", row)

    events = directory / "events.jsonl"
    if events.exists():
        for row in _read_jsonl(events):
            # Three record kinds share one file because what they are timed
            # against differs. Folding them together would hide a field that
            # disappeared from one kind but survives in another.
            add(f"events.jsonl[kind={row['kind']}]", row)

    usage = directory / "usage.jsonl"
    if usage.exists():
        for row in _read_jsonl(usage):
            add("usage.jsonl", row)

    report = directory / "validation-report.json"
    if report.exists():
        add("validation-report.json", json.loads(report.read_text("utf-8")))

    parameters = directory / "parameters.json"
    if parameters.exists():
        loaded = json.loads(parameters.read_text("utf-8"))
        # `available: false` carries a `reason` and none of the rest, so the
        # two are separate forms rather than one form with optional halves.
        add(f"parameters.json[available={str(loaded['available']).lower()}]", loaded)

    for path in sorted(directory.glob("session_*/signal_*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle))
        # A CSV has no per-field types; the promise here is the column set and
        # its order, so it is recorded as an ordered list.
        forms.setdefault("signal-csv-header", []).append(
            {f"/{index}": name for index, name in enumerate(header)}
        )

    return {name: _merge(records) for name, records in forms.items()}


# --------------------------------------------------------------------------
# The fixtures the contract is measured from
# --------------------------------------------------------------------------


def build_all_forms(tmp_path: Path) -> dict[str, dict]:
    """Produce every covered form and describe it.

    Three exports, because no single archive yields all of them: one full
    archive, one carrying the long-term record, and one without a parameter
    file so the unavailable shape is exercised too.
    """
    from prisma_vent.decode import export_archive
    from synthetic import (
        build_day_archive,
        build_statistic,
        build_usage_record,
    )

    merged: dict[str, list[dict]] = {}

    def collect(directory: Path) -> None:
        for name, paths in forms_from_export(directory).items():
            merged.setdefault(name, []).append(paths)

    sessions = [
        (1, datetime(2020, 3, 4, 22, 0, 0), 120),
        (2, datetime(2020, 3, 5, 1, 30, 0), 60),
    ]

    full = build_day_archive(
        tmp_path / "full", archive_date=ARCHIVE_DATE, sessions=sessions
    )
    collect(
        export_archive(
            full, tmp_path / "out-full", signals=["Pressure"], max_samples=5
        ).directory
    )

    with_usage = build_day_archive(
        tmp_path / "usage",
        archive_date=ARCHIVE_DATE,
        sessions=sessions[:1],
        extra_members={
            "statistic.proto": build_statistic(
                records=[build_usage_record(timestamp=1_000_000, duration=400)],
                therapy_total=100_000,
            )
        },
    )
    collect(export_archive(with_usage, tmp_path / "out-usage").directory)

    # `day_members=False` drops parameter.xml, which is what makes the export
    # write `available: false`.
    bare = build_day_archive(
        tmp_path / "bare",
        archive_date=ARCHIVE_DATE,
        sessions=sessions[:1],
        day_members=False,
    )
    collect(export_archive(bare, tmp_path / "out-bare").directory)

    return {name: _combine(parts) for name, parts in merged.items()}


def _combine(parts: list[dict[str, dict]]) -> dict[str, dict]:
    """Fold the same form seen in several exports into one description."""
    combined: dict[str, dict] = {}
    for part in parts:
        for path, entry in part.items():
            existing = combined.get(path)
            if existing is None:
                combined[path] = {
                    "presence": entry["presence"],
                    "types": list(entry["types"]),
                }
                continue
            existing["types"] = sorted(set(existing["types"]) | set(entry["types"]))
            if entry["presence"] == "optional":
                existing["presence"] = "optional"
    # A path absent from one export but present in another is conditional.
    seen = [set(part) for part in parts]
    everywhere = set.intersection(*seen) if seen else set()
    for path, entry in combined.items():
        if path not in everywhere:
            entry["presence"] = "optional"
    return {path: combined[path] for path in sorted(combined)}


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


def _emptied_dynamic_maps(form: dict[str, dict]) -> set[str]:
    """Dynamic maps this form writes but currently has no entries in.

    Present-and-empty is the case that needs the exemption. A map the export
    stopped writing *altogether* is absent here, so its disappearance is still
    reported.
    """
    return {
        path
        for path in DYNAMIC_MAP_PATHS
        if path in form and not any(p.startswith(f"{path}/") for p in form)
    }


def compare_backward_compatible(
    contract: dict[str, dict], current: dict[str, dict]
) -> list[str]:
    """Report where *current* breaks what *contract* promised.

    Additive change is allowed: a field or a whole form that the contract does
    not mention is not a breach, because a reader written against the contract
    ignores it. Breaking change is not: a path that vanished, a type that
    appeared where the contract did not allow it, or a field that used to be
    guaranteed and is now conditional.

    **Three limits, stated rather than implied.** A contract file can itself be
    edited, so this makes a breach expensive and visible rather than
    impossible. The types recorded are only those the fixtures actually
    exercised, which makes the guard's reach a property of the fixtures rather
    than of the format. And a path the fixtures only ever saw as ``null``
    carries a presence promise but no type promise — see the comment below,
    and ``test_a_field_the_fixtures_only_saw_as_null_promises_no_type``.

    Under a :data:`DYNAMIC_MAP_PATHS` object the promise is about the shape of
    an entry, not about which entries exist. Members may appear, disappear or
    move between two such maps; a field within an entry may not.
    """
    problems: list[str] = []
    for form, paths in sorted(contract.items()):
        if form not in current:
            problems.append(f"{form}: the export no longer writes this form")
            continue
        empty_maps = _emptied_dynamic_maps(current[form])
        for path, entry in sorted(paths.items()):
            # An emptied dynamic map has no `/…/*/…` paths to compare against.
            # That is "no entries right now", not "the entry shape is gone" —
            # and it is what happens when every candidate has been confirmed,
            # which is the success case again. The map itself is still checked
            # by its own path, so the field cannot vanish unnoticed.
            if any(path.startswith(f"{m}/*") for m in empty_maps):
                continue
            now = current[form].get(path)
            if now is None:
                problems.append(
                    f"{form} {path}: promised by the contract, absent from the "
                    "export — removing a field needs schema version 2"
                )
                continue
            promised = set(entry["types"])
            # A path the fixtures only ever saw as `null` carries no type
            # promise, because none was ever observed. `manifest.json`'s
            # `git_commit` is the example: an export from a temporary
            # directory has no repository to read, so the fixture writes
            # `null`, while an export from a checkout writes a string.
            # Treating `null` as *the* type would freeze a false alarm into a
            # file that must never be edited — the guard would reject correct
            # exports and there would be no legitimate way to fix it.
            # Presence is still checked; only the type claim is withheld.
            if promised == {"null"}:
                continue
            extra = set(now["types"]) - promised
            if extra:
                problems.append(
                    f"{form} {path}: type {sorted(extra)} is not one the "
                    f"contract allows ({entry['types']}) — changing or widening "
                    "a field's type needs schema version 2"
                )
            if entry["presence"] == "required" and now["presence"] == "optional":
                problems.append(
                    f"{form} {path}: the contract guarantees this field, but the "
                    "export now omits it from some records"
                )
    return problems


def compare_exact(contract: dict[str, dict], current: dict[str, dict]) -> list[str]:
    """Report every difference, in either direction.

    Used only when cutting a release, where the new version's contract must
    describe the export it is about to publish exactly.
    """
    problems = compare_backward_compatible(contract, current)
    for form, paths in sorted(current.items()):
        if form not in contract:
            problems.append(f"{form}: written by the export, missing from the contract")
            continue
        for path, entry in sorted(paths.items()):
            promised = contract[form].get(path)
            if promised is None:
                problems.append(f"{form} {path}: written by the export, not in the contract")
            elif set(promised["types"]) != set(entry["types"]):
                problems.append(
                    f"{form} {path}: contract says {promised['types']}, "
                    f"export writes {entry['types']}"
                )
            elif promised["presence"] != entry["presence"]:
                problems.append(
                    f"{form} {path}: contract says {promised['presence']}, "
                    f"export writes {entry['presence']}"
                )
    return problems


# --------------------------------------------------------------------------
# Generator
# --------------------------------------------------------------------------


def _describe_source() -> dict[str, str]:
    """Where this contract came from, in terms that survive a rebuilt history.

    **The release, not a commit.** A contract cannot name the commit that
    contains it — the commit does not exist until the file is in it — so an
    earlier version recorded whatever ``HEAD`` happened to be, which was the
    *previous* commit. Worse, a commit id stops resolving the moment a history
    is re-created, and this project's history has been re-created more than
    once: the same mistake killed a downstream commit pin twice, which is why
    that pin now names a tag.

    A tag is the granularity this actually has. Check it out, run the command
    below, and you get this file back. `check-release-tag.py` refuses a release
    whose contract names a different one, so the field is load-bearing rather
    than decorative.
    """
    return {
        "release": f"v{prisma_vent.__version__}",
        "command": (
            "PYTHONPATH=<tree>/src:<tree>/tests "
            "python3 tests/schema_contract.py OUT.json"
        ),
    }


def main(argv: list[str]) -> int:
    import tempfile

    if len(argv) != 2:
        print(__doc__)
        return 2
    with tempfile.TemporaryDirectory() as scratch:
        forms = build_all_forms(Path(scratch))
    document = {"_generated_from": _describe_source(), "forms": forms}
    Path(argv[1]).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {argv[1]}: {len(forms)} forms")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
