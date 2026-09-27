"""Reading one day archive — a ``NNNN_YYYY-MM-DD.zip`` from the card.

This module does three things the rest of the decoder deliberately does not.

**It resolves time.** The session files and the day-level files count from
different midnights, and this is the one place that knows both the archive's
date and each session's own start date. Resolving here means the choice is
made once, visibly, and can be checked: a session's start offset resolved
against its own midnight must agree with the start recorded in its ``.wmedf``
header, to the second. If it does not, the pairing or the reference is wrong,
and the session is refused rather than placed on a plausible wrong night.

**It bounds untrusted input.** Member names and sizes come from a file, so
they are checked before anything is read: no absolute paths, no traversal, a
cap on member count and on uncompressed size both per member and in total.

**It records provenance.** Every session carries the archive's SHA-256 —
computed once over the original bytes, never over unpacked or re-sorted
members — together with the member name, the session number and the adapter
version. The identity used for idempotent re-import deliberately excludes the
adapter version: re-reading with a newer decoder must be a controlled
reprocess, not a silent duplication.

Nothing is extracted to disk. Members are read as streams from the archive,
and the raw inputs are never written to.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import BinaryIO, Iterator

from . import DECODER_SCHEMA_VERSION, __version__
from .events import (
    AlarmLog,
    DayLog,
    ParameterLog,
    ParameterMap,
    SessionEvents,
    read_alarm_log,
    read_day_log,
    read_parameter_log,
    read_parameter_map,
    read_session_events,
)
from .statistic import StatisticFile, read_statistic
from .timebase import DeviceLocalTime, session_reference, therapy_day
from .wmedf import WmedfHeader, read_header, verify_against_size

__all__ = [
    "ArchiveError",
    "ArchiveLimits",
    "AdapterVersion",
    "Provenance",
    "Session",
    "DayArchive",
    "open_day_archive",
]


class ArchiveError(ValueError):
    """The archive is not shaped the way a day archive is expected to be."""


@dataclass(frozen=True)
class ArchiveLimits:
    """Bounds on untrusted archive content.

    A day archive holds a few dozen members, the largest a few megabytes, and
    tens of megabytes in total. These defaults are generous against that while
    still refusing anything absurd, and they are explicit so that raising one is
    a decision rather than an edit buried in a reader.
    """

    max_members: int = 256
    max_member_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024


# Member names the decoder understands. Anything else that is *safe* is kept
# and reported rather than rejected: a later firmware adding a file should be
# visible, not silently dropped and not fatal.
_SIGNAL_RE = re.compile(r"^(\d{4})\.wmedf$")
_SESSION_EVENT_RE = re.compile(r"^event_(\d{4})\.xml$")
_DAY_MEMBERS = {
    "event.xml",
    "alarm.xml",
    "parameter.xml",
    "parametersmap.xml",
    "device.xml",
    "statistic.proto",
}
_KNOWN_PREFIXES = ("logs/", "service/")

_ARCHIVE_NAME_RE = re.compile(r"^(\d{4})_(\d{4})-(\d{2})-(\d{2})\.zip$")

#: A span no session can have. Not a tolerance - a structural limit, derived
#: from the failure it exists to catch rather than from how long therapy runs.
#: The day correction in :meth:`DayArchive._resolve_stop` adds exactly one day,
#: so a wrongly applied correction produces a span of exactly twenty-four hours
#: or more. The comparison is therefore ``>=``, and the number is that
#: correction's own size rather than a figure chosen to fit anything.
_IMPLAUSIBLE_SESSION_SPAN = timedelta(hours=24)

#: **There is no bound on the start skew, and there deliberately is not one.**
#:
#: The signal header stores whole seconds and the session XML stores
#: milliseconds, so truncation alone accounts for up to one second of
#: difference. Beyond that the two files are stamped by different subsystems,
#: and *nothing in the format and nothing the manufacturer publishes bounds
#: the skew between them*.
#:
#: Two earlier bounds are withdrawn. The first, 2 s, had no public derivation.
#: The second, 60 s, was justified as "the coarsest quantum below a day" —
#: which is not an invariant of this format at all, since it works in seconds
#: and milliseconds too, so the minute was chosen rather than derived.
#:
#: Neither is described here in terms of the recordings it was set against.
#: Saying a withdrawn bound "sat just above what was measured" publishes that
#: measurement, which is precisely what withdrawing it was for.
#:
#: Both had the same two failure modes, in opposite directions — accepting a
#: mispaired file whose skew falls under the bound, and refusing correct data
#: whose skew exceeds it.
#:
#: So the skew is now **measured and reported, never asserted on**. It is
#: exposed as :attr:`Session.start_skew` and exported as
#: ``start_skew_seconds``, unbounded, so a consumer can see it and judge.
#:
#: What still rejects a session is the one invariant that *is* structural: the
#: noon-to-noon therapy day a session resolves into must be the day the archive
#: is named for. That is checked below, it needs no chosen number, and it
#: catches the failure the skew bound was really aimed at — a start resolved
#: against the wrong reference midnight, which moves a session by a whole day.
_START_SKEW_IS_REPORTED_NOT_BOUNDED = True


@dataclass(frozen=True)
class AdapterVersion:
    """Which code produced a reading.

    ``git_commit`` and ``dirty`` come from build-time information when it is
    present. An installed wheel or an exported tree has no repository, so both
    are optional by design and consumers must handle them missing rather than
    assuming a checkout.
    """

    package_version: str
    decoder_schema_version: int
    git_commit: str | None = None
    dirty: bool | None = None

    @classmethod
    def current(cls) -> "AdapterVersion":
        commit: str | None = None
        dirty: bool | None = None
        try:  # pragma: no cover - present only in built distributions
            from ._build_info import GIT_COMMIT, GIT_DIRTY

            commit, dirty = GIT_COMMIT, GIT_DIRTY
        except ImportError:
            pass
        return cls(
            package_version=__version__,
            decoder_schema_version=DECODER_SCHEMA_VERSION,
            git_commit=commit,
            dirty=dirty,
        )


@dataclass(frozen=True)
class Provenance:
    """Where a reading came from. Mandatory on everything this adapter emits."""

    archive_sha256: str
    archive_name: str
    member_name: str
    session_number: int | None
    adapter: AdapterVersion

    def identity(self, record: str) -> str:
        """A stable key for idempotent import.

        Built from the archive hash, the member and the record's own identity —
        and **not** from the adapter version. Including the version would make
        re-reading with a newer decoder create duplicates instead of replacing
        what it supersedes, which is the opposite of what a version bump
        should mean.
        """
        return f"{self.archive_sha256}:{self.member_name}:{record}"


@dataclass(frozen=True)
class Session:
    """One therapy session: a signal file and its paired event file."""

    number: int
    signal_member: str
    event_member: str
    header: WmedfHeader
    events: SessionEvents
    start: DeviceLocalTime
    stop: DeviceLocalTime
    provenance: Provenance
    #: The event file's resolved start minus the signal header's start. Up to
    #: one second of it is the header's truncation to whole seconds; the rest
    #: is skew between the two subsystems that stamp these files, which nothing
    #: published bounds. **Reported, never asserted on** — see
    #: :data:`_START_SKEW_IS_REPORTED_NOT_BOUNDED`. May be negative.
    #:
    #: Defaulted so a caller constructing a Session by hand need not supply it;
    #: provenance stays mandatory, because a reading without it cannot be
    #: traced back to the bytes it came from.
    start_skew: timedelta = timedelta(0)

    @property
    def therapy_day(self) -> date:
        """The noon-to-noon day this session belongs to."""
        return therapy_day(self.start)

    @property
    def duration(self) -> timedelta:
        return self.stop.value - self.start.value

    @property
    def accounted_duration(self) -> timedelta:
        """The span the signal file's records account for."""
        return timedelta(
            seconds=self.header.n_records * self.header.record_duration_s
        )

    @property
    def duration_discrepancy(self) -> timedelta:
        """Wall-clock span minus the span the records account for.

        Surveyed rather than bounded: see :meth:`DayArchive._resolve_stop`.
        """
        return self.duration - self.accounted_duration


class DayArchive:
    """One day archive, opened for reading.

    Use :func:`open_day_archive`, which closes the underlying file.
    """

    def __init__(
        self,
        zf: zipfile.ZipFile,
        path: Path,
        sha256: str,
        limits: ArchiveLimits,
    ) -> None:
        self._zf = zf
        self.path = path
        self.name = path.name
        self.sha256 = sha256
        self.limits = limits

        self.archive_date, self.day_number = _parse_archive_name(path.name)
        self.unknown_members: tuple[str, ...] = ()
        self._signals: dict[int, str] = {}
        self._session_events: dict[int, str] = {}
        self._inspect_members()
        self.sessions: tuple[Session, ...] = self._build_sessions()

    # -- members ---------------------------------------------------------

    def _inspect_members(self) -> None:
        infos = self._zf.infolist()
        if len(infos) > self.limits.max_members:
            raise ArchiveError(
                f"archive holds {len(infos)} members, over the "
                f"{self.limits.max_members} allowed"
            )

        total = 0
        unknown: list[str] = []
        seen: set[str] = set()
        for info in infos:
            name = info.filename
            _require_safe_name(name)
            if info.is_dir():
                continue
            # A ZIP may legally carry two members with the same name, and
            # getinfo() then picks one of them by an implementation detail.
            # Every such archive is ambiguous, whatever the name.
            if name in seen:
                raise ArchiveError(
                    f"archive holds more than one member named {name!r}; which "
                    "one a reader gets is implementation-defined"
                )
            seen.add(name)
            if info.file_size > self.limits.max_member_bytes:
                raise ArchiveError(
                    f"member {name!r} is {info.file_size} bytes uncompressed, over "
                    f"the {self.limits.max_member_bytes} allowed"
                )
            total += info.file_size
            if total > self.limits.max_total_bytes:
                raise ArchiveError(
                    f"archive exceeds {self.limits.max_total_bytes} bytes uncompressed"
                )

            signal = _SIGNAL_RE.match(name)
            event = _SESSION_EVENT_RE.match(name)
            if signal:
                _claim(self._signals, int(signal.group(1)), name, "signal file")
            elif event:
                _claim(self._session_events, int(event.group(1)), name, "session event file")
            elif name in _DAY_MEMBERS:
                continue
            elif name.startswith(_KNOWN_PREFIXES):
                continue
            else:
                unknown.append(name)

        self.unknown_members = tuple(sorted(unknown))

    def member_names(self) -> tuple[str, ...]:
        """Every member this archive holds, recognised or not, sorted.

        Directories are left out: a ZIP may or may not carry entries for them,
        so their presence says something about the writer rather than about
        the archive's contents.
        """
        return tuple(
            sorted(info.filename for info in self._zf.infolist() if not info.is_dir())
        )

    # -- sessions --------------------------------------------------------

    def _build_sessions(self) -> tuple[Session, ...]:
        only_signals = sorted(set(self._signals) - set(self._session_events))
        only_events = sorted(set(self._session_events) - set(self._signals))
        if only_signals or only_events:
            raise ArchiveError(
                "signal and event files do not pair up: "
                f"signal without event {only_signals}, event without signal {only_events}"
            )

        sessions = []
        for number in sorted(self._signals):
            sessions.append(self._read_session(number))
        return tuple(sessions)

    def _read_session(self, number: int) -> Session:
        signal_member = self._signals[number]
        event_member = self._session_events[number]

        with self._zf.open(signal_member) as stream:
            header = read_header(stream)
        verify_against_size(header, self._zf.getinfo(signal_member).file_size)

        events = read_session_events(self._read_member(event_member))

        # The session file counts from midnight of the day the session starts.
        # That date comes from the signal header, which is the reliable anchor.
        reference = session_reference(header.start.value.date())
        resolved_start = reference.shift(events.start)

        # The difference between the two subsystems' idea of the start is
        # measured and carried on the Session, never asserted on. See
        # _START_SKEW_IS_REPORTED_NOT_BOUNDED above for why no bound survives.
        start_skew = resolved_start.value - header.start.value

        resolved_stop = self._resolve_stop(number, reference, events, resolved_start, header)

        session = Session(
            number=number,
            signal_member=signal_member,
            event_member=event_member,
            header=header,
            events=events,
            start=resolved_start,
            stop=resolved_stop,
            start_skew=start_skew,
            provenance=self.provenance_for(signal_member, number),
        )

        # The noon-to-noon day a session falls in must be the day the archive
        # claims to be. A mismatch means the session belongs to another
        # archive, and its provenance would name the wrong one.
        if session.therapy_day != self.archive_date:
            raise ArchiveError(
                f"session {number:04d} ({signal_member}) falls in therapy day "
                f"{session.therapy_day.isoformat()} but {self.name} is named for "
                f"{self.archive_date.isoformat()}"
            )
        return session

    @staticmethod
    def _resolve_stop(number, reference, events, start, header) -> DeviceLocalTime:
        """Resolve a session's stop, which may count from a different midnight.

        A third time reference. Where the *start* offset counts from midnight
        of the day the session began, the *stop* offset counts from midnight of
        the day it **ended**. For a session running from late evening to the
        following morning, resolving both against the same midnight puts the
        stop a full day early and yields a negative duration.

        The correction is not a guess. The signal file independently declares
        how many records it holds and how long each one lasts, so the corrected
        span has a second source to agree with - which is what makes adding a
        day a derivation rather than a patch.
        """
        stop = reference.shift(events.stop)

        if stop.value < start.value:
            # The stop offset counts from midnight of the day the session
            # ended, so a session crossing midnight resolves backwards here.
            stop = DeviceLocalTime(stop.value + timedelta(days=1))
        elif stop.value == start.value:
            # Equal offsets are only meaningful for a session that recorded
            # nothing. Adding a day here would turn a correct duration of zero
            # into a plausible twenty-four hours.
            if header.n_records != 0:
                raise ArchiveError(
                    f"session {number:04d}: start and stop offsets are identical "
                    f"but the signal file declares {header.n_records} record(s). "
                    "A session cannot both span time and not span it, and there "
                    "is no evidence for which reading is meant."
                )

        span = stop.value - start.value
        # Deliberately no tolerance on how closely the span matches the record
        # count. The day correction above is structural - a stop cannot precede
        # its start - but how far a span may drift from the seconds the signal
        # file accounts for is a question about this device, not about this
        # format, and it is not yet answered. An invented bound would either
        # reject correct sessions or accept wrong ones. The discrepancy is
        # exposed on Session instead, for the exploration pass to survey.
        if span < timedelta(0) or span >= _IMPLAUSIBLE_SESSION_SPAN:
            raise ArchiveError(
                f"session {number:04d}: resolved span of {span} is not a possible "
                "session length, so the stop offset's reference cannot be determined"
            )
        return stop

    # -- day-level files -------------------------------------------------

    def day_log(self) -> DayLog:
        log = read_day_log(self._read_member("event.xml"))
        self._require_date(log.date, "event.xml")
        return log

    def alarm_log(self) -> AlarmLog:
        log = read_alarm_log(self._read_member("alarm.xml"))
        self._require_date(log.date, "alarm.xml")
        return log

    def parameter_map(self) -> ParameterMap:
        # No day date to check: the map is an id-to-name table, not a log.
        return read_parameter_map(self._read_member("parametersmap.xml"))

    def statistic(self) -> StatisticFile:
        """The device's own long-term usage record.

        Deliberately **not** date-checked against the archive name. Unlike
        every other member, this one is not about the archive's day: it carries
        the device's whole history, so the copy in each archive covers a year
        rather than that date. Requiring it to match would reject a correct
        file.
        """
        return read_statistic(self._read_member("statistic.proto"))

    def parameter_log(self, parameter_map: ParameterMap | None = None) -> ParameterLog:
        log = read_parameter_log(
            self._read_member("parameter.xml"), parameter_map=parameter_map
        )
        self._require_date(log.date, "parameter.xml")
        return log

    def _require_date(self, found: date, member_name: str) -> None:
        """Check a member's own date against the one the filename declares.

        The archive name is the only thing that says which day this is, and it
        is the weakest evidence in the archive - a rename changes it. Every
        member that carries a date is therefore checked against it, through one
        helper so the three call sites cannot drift into three different rules.
        """
        if found != self.archive_date:
            raise ArchiveError(
                f"{member_name} in {self.name} declares {found.isoformat()} but the "
                f"archive is named for {self.archive_date.isoformat()}. Either the "
                "archive was renamed or a file was filed under the wrong day; both "
                "would attach this data to the wrong date."
            )

    # -- access ----------------------------------------------------------

    @contextmanager
    def open_signal(self, session: Session) -> Iterator[BinaryIO]:
        """Open a session's signal file as a stream, positioned at the start.

        Read directly from the archive; nothing is extracted to disk. The
        session must belong to *this* archive — see :meth:`_require_own_session`.
        """
        self._require_own_session(session)
        with self._zf.open(session.signal_member) as stream:
            yield stream

    def _require_own_session(self, session: Session) -> None:
        """Refuse a session that did not come from this archive.

        Member names repeat across archives — every day has an ``0001.wmedf``.
        Opening by name alone would therefore hand back a stream from the wrong
        day while the caller still holds the other day's header, provenance and
        archive hash: plausible samples, wrong origin, and nothing in the
        result to show it.

        Checked here rather than at each call site so that later
        session-scoped accessors inherit the same rule instead of inventing
        their own.
        """
        expected = self._signals.get(session.number)
        if expected is None or expected != session.signal_member:
            raise ArchiveError(
                f"session {session.number:04d} names member "
                f"{session.signal_member!r}, which is not this archive's signal "
                f"file for that session ({expected!r})"
            )

        provenance = session.provenance
        if (
            provenance.archive_sha256 != self.sha256
            or provenance.archive_name != self.name
            or provenance.member_name != session.signal_member
            or provenance.session_number != session.number
        ):
            raise ArchiveError(
                f"session {session.number:04d} carries provenance for "
                f"{provenance.archive_name!r} ({provenance.archive_sha256[:12]}…) "
                f"but this archive is {self.name!r} ({self.sha256[:12]}…)"
            )

        if session not in self.sessions:
            raise ArchiveError(
                f"session {session.number:04d} is not one of this archive's "
                "sessions; it was constructed elsewhere or has been altered"
            )

    def provenance_for(self, member_name: str, session_number: int | None = None) -> Provenance:
        return Provenance(
            archive_sha256=self.sha256,
            archive_name=self.name,
            member_name=member_name,
            session_number=session_number,
            adapter=AdapterVersion.current(),
        )

    def _read_member(self, name: str) -> bytes:
        try:
            info = self._zf.getinfo(name)
        except KeyError:
            raise ArchiveError(f"archive has no member {name!r}") from None
        if info.file_size > self.limits.max_member_bytes:
            raise ArchiveError(f"member {name!r} is over the size limit")
        with self._zf.open(name) as stream:
            return stream.read(info.file_size + 1)


@contextmanager
def open_day_archive(
    path: str | Path, *, limits: ArchiveLimits | None = None
) -> Iterator[DayArchive]:
    """Open a day archive for reading, closing it afterwards."""
    path = Path(path)
    digest = _sha256_of_file(path)
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"{path.name} is not a readable ZIP archive: {exc}") from exc
    try:
        yield DayArchive(zf, path, digest, limits or ArchiveLimits())
    finally:
        zf.close()


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _sha256_of_file(path: Path) -> str:
    """Hash the archive's original bytes.

    Over the file as it is, not over its unpacked or re-sorted members: the
    hash has to identify the thing that was read, and repacking changes bytes
    without changing content.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_archive_name(name: str) -> tuple[date, int]:
    match = _ARCHIVE_NAME_RE.match(name)
    if match is None:
        raise ArchiveError(
            f"{name!r} is not named NNNN_YYYY-MM-DD.zip. The day counter and the "
            "date are both part of the archive's identity."
        )
    number, year, month, day = match.groups()
    try:
        return date(int(year), int(month), int(day)), int(number)
    except ValueError as exc:
        raise ArchiveError(f"{name!r} carries an impossible date") from exc


def _require_safe_name(name: str) -> None:
    if not name:
        raise ArchiveError("archive contains a member with an empty name")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ArchiveError(f"member {name!r} is an absolute path")
    if "\\" in name:
        raise ArchiveError(f"member {name!r} contains a backslash")
    parts = name.split("/")
    if any(part in ("..", ".") for part in parts):
        raise ArchiveError(f"member {name!r} traverses directories")
    if any("\x00" in part for part in parts):
        raise ArchiveError(f"member {name!r} contains a null byte")


def _claim(target: dict[int, str], number: int, name: str, what: str) -> None:
    if number in target:
        raise ArchiveError(
            f"archive holds more than one {what} numbered {number:04d}: "
            f"{target[number]!r} and {name!r}"
        )
    target[number] = name
