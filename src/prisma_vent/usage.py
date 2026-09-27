"""Therapy time over the long run, from the device's own record.

    prisma-vent usage ARCHIVE...

Every other command here works on one archive's day. This one works on the year
that `statistic.proto` carries inside each archive: a start time and a duration
for every session, reaching a rolling year back rather than the few weeks the
day archives cover.

**What it reports is duration, and only duration.** How long the device ran, per
session, per day, per month. Not how well it ran — the long-term record holds no
waveform, no event and no alarm, so nothing here says anything about the quality
of a night's ventilation. For the days a day archive also covers, that archive
is the fuller source.

Four things this command is careful about, because each would otherwise
produce a confident wrong number:

**The day boundary.** A therapy day runs noon to noon on this device, so a
session starting after midnight belongs to the day before. Summing by calendar
date instead would split most nights in two and halve them both. ``--by
calendar-day`` exists for comparing against a source that bins differently, and
says so in its output rather than quietly changing the meaning of a column.

**Days with no stored therapy.** A covered day carrying no long-term record is
reported as a zero rather than left out. Omitting it would silently turn
"therapy time per covered day" into "therapy time per day the device happened
to run", which is a different and always larger number.

**A zero says less than it looks like it says.** The long-term record stores
whole minutes, and a session shorter than a minute leaves no entry at all — six
such sessions do occur. So a zero day means *no whole stored
therapy minutes and no stored session*, and it is **not** proof that the device
was never switched on. Anything that needs to distinguish "off all day" from
"used briefly" has to come from a day archive, which records sessions of any
length.

**A day is covered** when it lies between the first and the last day carrying a
session, inclusive. Days outside that range are not reported at all: the record
does not reach them, and a zero there would claim knowledge the file does not
have. That distinction is the reason zeros are materialised inside the range
and never outside it.

**Partial days at the edges.** The record is a rolling window, so its first and
last days are cut off mid-day by definition — the newest is usually the day the
card was pulled. Including them in an average drags it down for a reason that
has nothing to do with therapy. They are marked, and excluded from the summary
statistics unless ``--include-partial`` is given.

**Averages are per day, not per bucket.** With ``--by month`` each bucket
covers a different number of days — the first and last months of a rolling
window usually cover only a few. Averaging the monthly figures would weight a
month covering one day the same as one covering thirty-one, and a median of
monthly averages is not a median of days whatever it is labelled. Both summary
figures are therefore computed from the individual days a bucket aggregates, so
"h/day" means the same thing in every mode.

**Duplicate rows across archives.** Each archive carries its own copy of the
whole year, so pointing this command at a directory of archives would
otherwise count every session once per archive. Rows are deduplicated on their
raw timestamp, and what was merged is reported. Two rows sharing a timestamp but disagreeing
about the session are a **structural error**, not something to resolve by
argument order — see :func:`_load`.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Iterable, Sequence, TextIO

from . import __version__
from .archive import ArchiveError, open_day_archive
from .events import XmlError
from .statistic import StatisticError, UsageRecord
from .timebase import TimebaseError, therapy_day
from .wmedf import WmedfError

__all__ = [
    "add_arguments",
    "run",
    "main",
    "Bucket",
    "Day",
    "UsageError",
    "DateRangeError",
    "summarise",
]

EXIT_OK = 0
EXIT_STRUCTURAL = 1
EXIT_USAGE = 2

#: A rolling window of a year is what this device keeps. Fifty years is far
#: beyond anything legitimate and still small enough to materialise, so it
#: catches a misread timestamp before it becomes an allocation.
_MAX_COVERED_DAYS = 50 * 366


class UsageError(ValueError):
    """The records cannot be summarised as they stand."""


@dataclass(frozen=True)
class Day:
    """One covered day, and whether the window cuts it off."""

    date: date
    #: Whole therapy minutes stored for this day.
    minutes: int
    #: How many long-term records fall on it.
    sessions: int
    #: True only for the first and last day the record covers, which the
    #: rolling window clips mid-day. **A day is the unit that can be partial**;
    #: a month is merely one that contains such a day.
    partial: bool = False

    @property
    def hours(self) -> float:
        return self.minutes / 60


@dataclass(frozen=True)
class Bucket:
    """Therapy time over one period, and the days it is made of."""

    label: str

    #: The individual days this period covers, in order. Everything else here
    #: is derived from them.
    #:
    #: The summary is computed from days rather than from buckets for two
    #: reasons. Averaging bucket figures would weight a month covering one day
    #: like one covering thirty-one, and a median of monthly averages is not a
    #: median of days however it is labelled. And a month is excluded from the
    #: summary only for the *day* that is clipped, not wholesale — dropping a
    #: full month because one of its days is a boundary day would discard up to
    #: thirty complete measurements to avoid one incomplete one.
    days_covered: tuple[Day, ...] = ()

    @property
    def minutes(self) -> int:
        """Whole therapy minutes stored in the long-term record for this period."""
        return sum(day.minutes for day in self.days_covered)

    @property
    def sessions(self) -> int:
        """How many long-term **records** fall in this period.

        Not how many times the device was used: the record holds whole
        minutes, so a session under a minute is absent from it entirely.
        """
        return sum(day.sessions for day in self.days_covered)

    @property
    def days(self) -> int:
        return len(self.days_covered)

    @property
    def partial(self) -> bool:
        """True when any day in this period is clipped by the window."""
        return any(day.partial for day in self.days_covered)

    @property
    def daily_hours(self) -> tuple[float, ...]:
        return tuple(day.hours for day in self.days_covered)

    @property
    def hours(self) -> float:
        return self.minutes / 60

    @property
    def hours_per_day(self) -> float:
        return self.minutes / 60 / self.days if self.days else 0.0


def _bucket_key(record: UsageRecord, by: str) -> date:
    if by == "calendar-day":
        return record.start.value.date()
    return therapy_day(record.start)


def summarise(
    records: Iterable[UsageRecord],
    *,
    by: str = "day",
    since: date | None = None,
    until: date | None = None,
) -> list[Bucket]:
    """Group records into periods over every covered day.

    A **covered day** lies between the first and the last day carrying a
    session, inclusive. Every one of them gets a bucket, including days with no
    stored record: dropping those would turn therapy per covered day into
    therapy per day the device happened to run — a different and always larger
    figure. Days outside that range get no bucket at all, because the record
    does not reach them and a zero there would claim knowledge the file does
    not have.

    A zero bucket means **no whole therapy minutes and no session stored in the
    long-term record**. Since that record holds whole minutes, a session under
    a minute leaves no entry, so a zero is not proof the device stayed off.

    Boundary periods are marked rather than dropped: a caller may legitimately
    want them, and silently discarding data is worse than labelling it.
    """
    per_day: dict[date, list[UsageRecord]] = defaultdict(list)
    for record in records:
        per_day[_bucket_key(record, by)].append(record)
    if not per_day:
        return []

    active = sorted(per_day)
    first, last = active[0], active[-1]

    # Every covered day, including those with no stored record. Omitting them
    # would divide by active days and report therapy per *used* day while
    # calling it per day. A zero here means nothing was stored, which is not
    # quite the same as nothing happening — see the docstring.
    span_days = (last - first).days + 1
    if span_days > _MAX_COVERED_DAYS:
        raise UsageError(
            f"the records span {span_days} days, over the "
            f"{_MAX_COVERED_DAYS}-day cap. That is far longer than any "
            f"rolling window this device keeps, so a timestamp is being read "
            f"wrongly rather than the device having run for that long"
        )
    days = [first + timedelta(days=offset) for offset in range(span_days)]

    # Built once, so the partial flag lives on the day it belongs to rather
    # than being re-derived from a position in a list.
    covered = [
        Day(
            date=day,
            minutes=sum(r.duration_minutes for r in per_day.get(day, ())),
            sessions=len(per_day.get(day, ())),
            partial=day in (first, last),
        )
        for day in days
    ]

    # Filtered only now: the flags above describe the record's own window, and
    # re-deriving them from a filtered list would mark the filter's edges as
    # though the device had stopped there.
    if since is not None:
        covered = [entry for entry in covered if entry.date >= since]
    if until is not None:
        covered = [entry for entry in covered if entry.date <= until]
    if not covered:
        return []

    if by == "month":
        per_month: dict[tuple[int, int], list[Day]] = defaultdict(list)
        for entry in covered:
            per_month[(entry.date.year, entry.date.month)].append(entry)
        return [
            Bucket(label=f"{year}-{month:02d}", days_covered=tuple(members))
            for (year, month), members in sorted(per_month.items())
        ]

    return [
        Bucket(label=entry.date.isoformat(), days_covered=(entry,))
        for entry in covered
    ]


#: The fields two records sharing a timestamp must agree on to be the same
#: session. Every one has an established meaning — a difference in any of them
#: means the two snapshots disagree about what happened, which no merge rule
#: can resolve.
#:
#: **Histogram blocks are deliberately absent.** They hold unidentified
#: distributions, so a difference between two copies cannot be judged: it might
#: mean the snapshots disagree, or it might mean the blocks are cumulative,
#: or resampled, or scoped to something other than the session. Treating an
#: unexplained difference as a conflict would reject good data on the strength
#: of a guess about semantics this project does not have. They are therefore
#: not compared, and this is a known limit rather than an oversight.
_IDENTITY_FIELDS = (
    "duration_minutes",
    "second_quantity",
    "duration_by_program",
    "duration_by_category",
    "second_by_program",
    "second_by_category",
)


def _disagreements(a: UsageRecord, b: UsageRecord) -> list[str]:
    """Which established fields two same-timestamped records differ on."""
    return [
        name
        for name in _IDENTITY_FIELDS
        if getattr(a, name) != getattr(b, name)
    ]


def _load(
    paths: Sequence[Path], out: TextIO
) -> tuple[list[UsageRecord], int, int, list[str]]:
    """Read every archive named, merging the year each one repeats.

    Records are keyed by their raw timestamp, since each stands for one
    session. Two archives holding the same session must describe it the same
    way; where they do not, that is reported as a conflict rather than settled
    by which archive was named first. **A first-one-wins rule would make the
    result depend on argument order**, which for a check meant to catch a
    misread is worse than useless.

    **Conflict messages carry no timestamp and no values.** An earlier version
    printed the record's raw timestamp to identify it, on the reasoning that a
    diagnostic needs something to point at. That was wrong: the timestamp
    converts directly into the exact minute a therapy session began, which is
    health data in its own right — the very thing a person might paste into a
    bug report. Conflicts are numbered instead, and identified by the archives
    and the field names involved, all of which are safe to share.
    """
    seen: dict[int, UsageRecord] = {}
    origin: dict[int, str] = {}
    duplicates = 0
    failures = 0
    conflicts: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
    for path in paths:
        try:
            with open_day_archive(path) as archive:
                statistic = archive.statistic()
        except (
            ArchiveError,
            WmedfError,
            XmlError,
            TimebaseError,
            StatisticError,
        ) as exc:
            print(f"{path.name}: {exc}", file=sys.stderr)
            failures += 1
            continue
        except FileNotFoundError:
            print(f"{path}: no such file", file=sys.stderr)
            failures += 1
            continue
        for record in statistic.records:
            key = record.raw_timestamp
            previous = seen.get(key)
            if previous is None:
                seen[key] = record
                origin[key] = path.name
                continue
            differing = _disagreements(previous, record)
            if differing:
                # Keyed on nothing that identifies the session in time. The
                # archives and the field names are enough to find it, and both
                # are safe to quote.
                conflicts.add(
                    (
                        tuple(sorted({origin[key], path.name})),
                        tuple(differing),
                    )
                )
            else:
                duplicates += 1
    # Sorted before numbering, so the same set of archives produces the same
    # report whatever order they were named in.
    described = [
        f"conflict {number}: {' and '.join(archives)} disagree about a session "
        f"they both hold — {', '.join(fields)} differ"
        for number, (archives, fields) in enumerate(sorted(conflicts), start=1)
    ]
    return (
        sorted(seen.values(), key=lambda r: r.raw_timestamp),
        duplicates,
        failures,
        described,
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "archive",
        nargs="+",
        type=Path,
        help="one or more NNNN_YYYY-MM-DD.zip; each carries the whole year",
    )
    parser.add_argument(
        "--by",
        choices=("day", "calendar-day", "month"),
        default="day",
        help=(
            "day (therapy day, noon to noon, the device's own boundary), "
            "calendar-day (midnight, for comparing against another source), "
            "or month"
        ),
    )
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="omit days before this one; the day itself is included",
    )
    parser.add_argument(
        "--until",
        metavar="YYYY-MM-DD",
        help="omit days after this one; the day itself is included",
    )
    parser.add_argument(
        "--include-partial",
        action="store_true",
        help=(
            "count the rolling window's cut-off first and last days in the "
            "summary as well"
        ),
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="write machine-readable rows instead of a table",
    )


class DateRangeError(ValueError):
    """A ``--since``/``--until`` value the command cannot act on."""


def _parse_date(text: str, flag: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise DateRangeError(
            f"{flag}: {text!r} is not a date in YYYY-MM-DD form"
        ) from None


def run(args: argparse.Namespace, out: TextIO) -> int:
    records, duplicates, failures, conflicts = _load(args.archive, out)
    if conflicts:
        # Structural, not a warning: two of the device's own snapshots
        # disagree, so any figure derived from them would be a coin toss.
        print(
            f"{len(conflicts)} conflicting record(s) across the archives given:",
            file=sys.stderr,
        )
        for conflict in conflicts:
            print(f"  {conflict}", file=sys.stderr)
        return EXIT_STRUCTURAL
    if not records:
        print("no long-term records found in the archives given", file=sys.stderr)
        return EXIT_STRUCTURAL

    try:
        since = _parse_date(args.since, "--since") if args.since else None
        until = _parse_date(args.until, "--until") if args.until else None
    except DateRangeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    if since is not None and until is not None and since > until:
        print(
            f"--since {since.isoformat()} is after --until {until.isoformat()}, "
            f"so no day could satisfy both. Both bounds are inclusive",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        buckets = summarise(records, by=args.by, since=since, until=until)
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_STRUCTURAL

    if not buckets:
        print("no days left after --since/--until", file=sys.stderr)
        return EXIT_STRUCTURAL

    if args.csv:
        _write_csv(buckets, out)
        return EXIT_OK if not failures else EXIT_STRUCTURAL

    _write_table(buckets, args, records, duplicates, out)
    return EXIT_OK if not failures else EXIT_STRUCTURAL


def _write_csv(buckets: Sequence[Bucket], out: TextIO) -> None:
    writer = csv.writer(out)
    writer.writerow(["period", "therapy_minutes", "sessions", "days", "partial"])
    for bucket in buckets:
        writer.writerow(
            [
                bucket.label,
                bucket.minutes,
                bucket.sessions,
                bucket.days,
                "true" if bucket.partial else "false",
            ]
        )


def _write_table(
    buckets: Sequence[Bucket],
    args: argparse.Namespace,
    records: Sequence[UsageRecord],
    duplicates: int,
    out: TextIO,
) -> None:
    boundary = {
        "day": "therapy day, noon to noon",
        "calendar-day": "calendar day, midnight to midnight",
        "month": "calendar month, of therapy days",
    }[args.by]
    print(f"  {len(records)} sessions, grouped by {boundary}", file=out)
    if duplicates:
        print(
            f"  {duplicates} duplicate row(s) merged — every archive on a card "
            f"carries the same year",
            file=out,
        )
    print(file=out)

    width = max(len(b.label) for b in buckets)
    scale = max((b.hours_per_day for b in buckets), default=1.0) or 1.0
    for bucket in buckets:
        bar = "#" * round(bucket.hours_per_day / scale * 34)
        note = "  partial" if bucket.partial else ""
        if args.by == "month":
            print(
                f"  {bucket.label:<{width}}  {bucket.hours_per_day:5.2f} h/day  "
                f"{bucket.days:>2}d  {bar}{note}",
                file=out,
            )
        else:
            print(
                f"  {bucket.label:<{width}}  {bucket.hours:5.2f} h  "
                f"{bucket.sessions:>2} sess  {bar}{note}",
                file=out,
            )

    # Days, not buckets. Excluding a whole month because one of its days is a
    # boundary day would throw away up to thirty complete measurements to
    # avoid one incomplete one, so only the clipped days themselves are left
    # out. In day mode this is the same thing as excluding the bucket.
    all_days = [day for bucket in buckets for day in bucket.days_covered]
    counted = [d for d in all_days if args.include_partial or not d.partial]
    excluded = len(all_days) - len(counted)
    print(file=out)
    per_day = [day.hours for day in counted]
    if not per_day:
        print("  every day shown is cut off by the window; nothing to average",
              file=out)
    else:
        print(
            f"  mean {mean(per_day):.2f} h/day, median {median(per_day):.2f} "
            f"h/day, over {len(per_day)} day(s)",
            file=out,
        )
        if excluded:
            print(
                f"  {excluded} cut-off day(s) excluded — the record is a "
                f"rolling window, so its first and last days are incomplete. "
                f"The periods holding them are still shown in full, marked "
                f"partial. --include-partial counts them too.",
                file=out,
            )
    # Printed on every path, including the one where nothing could be
    # averaged: that is exactly when a reader is most likely to take the
    # single figure above for more than it is.
    print(
        "\n  Duration only, in whole minutes: the long-term record stores no "
        "waveform,\n  event or alarm, and no session shorter than a minute. A "
        "0.00 h day therefore\n  means nothing was stored for it, not "
        "necessarily that the device stayed off.",
        file=out,
    )


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m prisma_vent.usage",
        description="Therapy time over the device's long-term record.",
    )
    add_arguments(parser)
    parser.add_argument("--version", action="version", version=__version__)
    return run(parser.parse_args(argv), out if out is not None else sys.stdout)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
