"""Look at a day archive without changing anything.

    python -m prisma_vent.inspect ARCHIVE.zip

This is a **development tool, not a public interface**. The stable
``prisma-vent`` command is a later deliverable; these module paths carry no
compatibility guarantee, and the output is meant to be read by a person rather
than parsed.

What it will not do:

*   **Name an event.** No event or alarm id has been identified, so they are
    reported as ids and counts. A column headed "apnoea" would be a guess
    wearing the clothes of a measurement.
*   **Convert a timestamp.** The device clock's zone is unresolved, so times
    are printed as the device recorded them and labelled as such.
*   **Write anything.** It opens the archive read-only and extracts nothing.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence, TextIO

from . import __version__
from .archive import ArchiveError, DayArchive, Session, open_day_archive
from .events import XmlError
from .statistic import PROGRAM_SLOTS, StatisticError
from .timebase import TimebaseError, format_offset
from .trendcurve import (
    RECORD_INTERVAL,
    TrendCurveError,
    read_trend_curve,
)
from .wmedf import DiagnosticPolicy, Signal, ViolationCollector, WmedfError, iter_digital_chunks

EXIT_OK = 0
EXIT_STRUCTURAL = 1
EXIT_USAGE = 2


@dataclass
class ChannelStats:
    """Extremes and mean for one channel, accumulated without materialising."""

    count: int = 0
    digital_min: int = 0
    digital_max: int = 0
    digital_sum: int = 0

    def update(self, samples: Sequence[int]) -> None:
        if not samples:
            return
        low, high = min(samples), max(samples)
        if self.count == 0:
            self.digital_min, self.digital_max = low, high
        else:
            self.digital_min = min(self.digital_min, low)
            self.digital_max = max(self.digital_max, high)
        self.count += len(samples)
        self.digital_sum += sum(samples)

    @property
    def digital_mean(self) -> float:
        return self.digital_sum / self.count if self.count else 0.0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare this command's options.

    Kept separate from :func:`main` so the installed ``prisma-vent`` command
    and the module entry point describe exactly the same interface rather than
    two that drift apart.
    """
    parser.add_argument(
        "archive",
        nargs="+",
        type=Path,
        help="NNNN_YYYY-MM-DD.zip, or NNNN_YYYY-MM-DD.tc for a trend curve",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=0,
        metavar="N",
        help="also print the first N samples of each channel, raw and converted",
    )
    parser.add_argument(
        "--no-stats",
        action="store_true",
        help="skip the per-channel statistics, which require reading every sample",
    )


def run(args: argparse.Namespace, out: TextIO) -> int:
    """Execute the command. Findings to *out*, diagnostics to stderr."""
    status = EXIT_OK
    for index, path in enumerate(args.archive):
        if index:
            print(file=out)
        try:
            _report(path, args, out)
        except (
            ArchiveError,
            WmedfError,
            XmlError,
            TimebaseError,
            TrendCurveError,
        ) as exc:
            print(f"{path.name}: {exc}", file=sys.stderr)
            status = EXIT_STRUCTURAL
        except FileNotFoundError:
            print(f"{path}: no such file", file=sys.stderr)
            status = EXIT_STRUCTURAL
    return status


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m prisma_vent.inspect",
        description="Print what is in a prisma VENT50 day archive. Read-only.",
    )
    add_arguments(parser)
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    if args.samples < 0:
        parser.error("--samples must not be negative")
    return run(args, out if out is not None else sys.stdout)


def _report(path: Path, args: argparse.Namespace, out: TextIO) -> None:
    if path.suffix == ".tc":
        _trend_report(path, out)
        return
    with open_day_archive(path) as archive:
        _archive_header(archive, out)
        _sessions(archive, args, out)
        _day_level(archive, out)
        _long_term(archive, out)


def _long_term(archive: DayArchive, out: TextIO) -> None:
    """Summarise the device's own long-term record.

    Kept short. This member covers the device's whole history rather than this
    archive's day, so a full listing would drown the report it appears in.
    """
    try:
        stat = archive.statistic()
    except (ArchiveError, StatisticError) as exc:
        print(f"\n  long-term record unavailable: {exc}", file=out)
        return

    print(f"\n  long-term record (statistic.proto, format {stat.format_version})",
          file=out)
    span = stat.covered_span
    print(
        f"    sessions        {len(stat.records)}"
        + (f" spanning {span.days} days" if span else ""),
        file=out,
    )
    if stat.records:
        first = min(r.start.value for r in stat.records)
        last = max(r.start.value for r in stat.records)
        print(f"    earliest        {first}  (device local)", file=out)
        print(f"    latest          {last}  (device local)", file=out)
    if stat.cumulative_therapy_minutes is not None:
        total = stat.cumulative_therapy_minutes
        print(
            f"    therapy total   {total} min = {total / 60:.1f} h  "
            f"(lifetime, confirmed against archive sessions)",
            file=out,
        )
    if stat.unidentified_total is not None:
        print(
            f"    second counter  {stat.unidentified_total}  "
            f"(unidentified; not minutes of anything established)",
            file=out,
        )
    if stat.configuration is not None:
        version = stat.configuration.probable_firmware_version
        print(
            f"    firmware        {version or 'not identifiable'}  "
            f"(inferred from position, not stated anywhere in the file)",
            file=out,
        )
        print(
            f"    named settings  {len(stat.configuration.device)} device, "
            f"{len(stat.configuration.therapy)} therapy x "
            f"{PROGRAM_SLOTS} programs",
            file=out,
        )


def _trend_report(path: Path, out: TextIO) -> None:
    """Summarise a trend curve.

    The device serial appears in these headers and is deliberately not printed:
    it identifies the device, and this output is meant to be pasteable.
    """
    curve = read_trend_curve(path)
    _rule(path.name, out)
    print(f"  therapy day     {curve.day}  (counter {curve.day_number:04d})", file=out)
    print(f"  sha256          {curve.provenance.archive_sha256}", file=out)
    print(f"  device type     {curve.device_type}  (device id {curve.device_id})", file=out)
    print(
        f"  offset field    {curve.offset_field}   "
        "(meaning unknown; carried as written)",
        file=out,
    )
    print(
        f"  records         {len(curve.records)}, of which "
        f"{curve.populated_records} populated",
        file=out,
    )
    print(
        f"  therapy time    {curve.estimated_therapy_time}  — **estimated** at "
        f"{int(RECORD_INTERVAL.total_seconds())}s per populated record",
        file=out,
    )
    print(
        "                  an estimate; a day archive counts this exactly, and\n"
        "                  the two are separate readings rather than one value",
        file=out,
    )
    print(
        "\n  The 9 bytes of a record are not decoded. Nothing here is derived\n"
        "  from their contents.",
        file=out,
    )


def _archive_header(archive: DayArchive, out: TextIO) -> None:
    _rule(archive.name, out)
    print(f"  therapy day     {archive.archive_date}  (counter {archive.day_number:04d})", file=out)
    print(f"  sha256          {archive.sha256}", file=out)
    print(f"  sessions        {len(archive.sessions)}", file=out)
    if archive.unknown_members:
        # Surfaced rather than ignored: a later firmware adding a file should
        # be visible here rather than discovered by its absence downstream.
        print(f"  unrecognised    {', '.join(archive.unknown_members)}", file=out)


def _sessions(archive: DayArchive, args: argparse.Namespace, out: TextIO) -> None:
    for session in archive.sessions:
        print(file=out)
        _rule(f"session {session.number:04d}", out, char="-")
        header = session.header
        print(f"  start           {session.start}", file=out)
        print(f"  stop            {session.stop}", file=out)
        print(f"  duration        {session.duration}", file=out)
        print(
            f"  records         {header.n_records} x {header.record_duration_s:g}s"
            f"  ({header.record_size_bytes} bytes each)",
            file=out,
        )
        print(f"  channels        {header.n_signals}", file=out)
        _respiratory_events(session, out)

        measured = None if args.no_stats else _channel_stats(archive, session)
        _channel_table(header.signals, header.record_duration_s, measured, out)
        if args.samples:
            _sample_preview(archive, session, args.samples, out)


def _respiratory_events(session: Session, out: TextIO) -> None:
    events = session.events.events
    if not events:
        print("  events          none", file=out)
        return
    counts: dict[int, int] = {}
    for event in events:
        if event.phase == "begin":
            counts[event.id] = counts.get(event.id, 0) + 1
    summary = "  ".join(f"id {i}: {n}" for i, n in sorted(counts.items()))
    print(f"  events          {len(events)} entries, by id: {summary}", file=out)
    print("                  (ids are unidentified; no event is named)", file=out)


@dataclass
class Measured:
    """Per-channel statistics plus whatever fell outside its declared range."""

    stats: dict[int, ChannelStats]
    violations: ViolationCollector


def _channel_stats(archive: DayArchive, session: Session) -> Measured:
    stats: dict[int, ChannelStats] = {s.index: ChannelStats() for s in session.header.signals}
    collector = ViolationCollector()
    with archive.open_signal(session) as stream:
        from .wmedf import read_header

        header = read_header(stream)
        for chunk in iter_digital_chunks(
            stream,
            header,
            policy=DiagnosticPolicy.COLLECT_VIOLATIONS,
            violations=collector,
        ):
            stats[chunk.signal.index].update(chunk.digital)
    return Measured(stats=stats, violations=collector)


def _channel_table(
    signals: Iterable[Signal],
    record_duration_s: float,
    measured: "Measured | None",
    out: TextIO,
) -> None:
    print(file=out)
    head = f"  {'#':>3} {'channel':<18}{'unit':<8}{'Hz':>4}{'bits':>5}{'digital range':>16}"
    if measured:
        head += f"{'observed (physical)':>28}{'mean':>10}"
    print(head, file=out)

    for signal in signals:
        # Derived, not assumed: samples_per_record equals the rate in hertz
        # only while a record lasts one second, which is this firmware's
        # choice rather than a property of the format.
        rate = signal.sampling_rate_hz(record_duration_s)
        row = (
            f"  {signal.index:>3} {signal.label:<18}{signal.unit:<8}{rate:>4g}"
            f"{signal.width_bytes * 8:>5}"
            f"{f'{signal.digital_min}..{signal.digital_max}':>16}"
        )
        if measured:
            entry = measured.stats.get(signal.index)
            if entry and entry.count:
                low = signal.to_physical_value(entry.digital_min)
                high = signal.to_physical_value(entry.digital_max)
                mean = signal.to_physical_value(entry.digital_mean)
                row += f"{f'{low:g}..{high:g}':>28}{mean:>10.3f}"
            else:
                row += f"{'-':>28}{'-':>10}"
        print(row, file=out)

    if measured and measured.violations.total_seen:
        print(f"\n  range violations: {measured.violations.summary()}", file=out)
        for violation in measured.violations.violations[:5]:
            print(f"    {violation}", file=out)


def _sample_preview(
    archive: DayArchive, session: Session, count: int, out: TextIO
) -> None:
    """First few samples of each channel, raw beside converted.

    Both representations, because that is what makes a wrong value
    attributable: a plausible physical number over an implausible raw one
    points at the scaling, and the reverse points at the bytes.
    """
    from .wmedf import read_header, to_physical

    print(file=out)
    print(f"  first {count} sample(s) per channel — digital / physical", file=out)
    seen: set[int] = set()
    with archive.open_signal(session) as stream:
        header = read_header(stream)
        # Enough records that even a 1 Hz channel can show the requested
        # number of samples; asking for three and being given one is a
        # confusing way to present a diagnostic.
        for chunk in iter_digital_chunks(stream, header, records_per_chunk=count):
            if chunk.signal.index in seen:
                continue
            seen.add(chunk.signal.index)
            scaled = to_physical(chunk)
            pairs = "  ".join(
                f"{d}/{p:g}"
                for d, p in list(zip(scaled.digital, scaled.physical, strict=True))[:count]
            )
            print(f"    {chunk.signal.index:>3} {chunk.signal.label:<18}{pairs}", file=out)
            if len(seen) == header.n_signals:
                break


def _day_level(archive: DayArchive, out: TextIO) -> None:
    print(file=out)
    _rule("day-level files", out, char="-")

    try:
        day_log = archive.day_log()
    except ArchiveError as exc:
        print(f"  event.xml       {exc}", file=out)
    else:
        windows = ", ".join(
            f"{format_offset(w.start)}..{format_offset(w.stop) if w.stop else 'open'}"
            for w in day_log.windows
        )
        print(f"  recording       {windows}", file=out)
        print(
            "                  (windows are device recordings, not sessions;"
            " one can hold several)",
            file=out,
        )
        print(f"  state events    {_by_id(e.id for e in day_log.events)}", file=out)

    try:
        alarm_log = archive.alarm_log()
    except ArchiveError as exc:
        print(f"  alarm.xml       {exc}", file=out)
    else:
        print(f"  alarms          {_by_id(a.id for a in alarm_log.alarms)}", file=out)

    try:
        pmap = archive.parameter_map()
        log = archive.parameter_log(pmap)
    except ArchiveError as exc:
        print(f"  parameter.xml   {exc}", file=out)
        return

    print(f"  parameter map   {len(pmap.names)} names, version {pmap.version}", file=out)
    for snapshot in log.snapshots:
        programs = len(snapshot.therapy_programs)
        size = len(snapshot.therapy_programs[0].parameters) if programs else 0
        active = (
            snapshot.active_program_raw.value
            if snapshot.active_program_raw is not None
            else "?"
        )
        print(
            f"  snapshot        {format_offset(snapshot.time)}: "
            f"{len(snapshot.device_parameters)} device + {programs} x {size} therapy; "
            f"ActiveProgram raw {active}",
            file=out,
        )
        for note in snapshot.notes:
            print(f"                  note: {note}", file=out)
    if log.snapshots:
        print(
            "                  (ActiveProgram is not resolved to a program:"
            " 0- or 1-based is unknown)",
            file=out,
        )


def _by_id(ids: Iterable[int]) -> str:
    counts: dict[int, int] = {}
    for value in ids:
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return "none"
    return "  ".join(f"id {i}: {n}" for i, n in sorted(counts.items()))


def _rule(title: str, out: TextIO, char: str = "=") -> None:
    print(f"{char * 2} {title} {char * max(3, 72 - len(title))}", file=out)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
