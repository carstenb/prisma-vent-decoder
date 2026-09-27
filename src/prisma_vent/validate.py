"""Check a decoded archive against itself.

The decoder can only tell you that a file matched the format it expected. That
is not the same as the numbers being right, and the failure this project fears
most — a plausible misparse — passes every structural check by definition.

So this module asks a different question: do quantities the device recorded
**independently of one another** agree? It records respiration as a 10 Hz
state channel *and* as a 1 Hz rate. It records flow at 10 Hz *and* the volume
derived from it at 1 Hz. Nothing forces those to agree unless the decode is
right, and no amount of self-consistency in the format can fake it.

**Five checks: two assert, three only report**, and that distinction is the
whole point of the exercise. They are the five in :data:`_CHECK_NAMES`; the
last item below is a figure the export carries rather than one of them:

*   **Breath rate — reported, not asserted.** Breaths counted from the phase
    channel are compared against the rate channel, and the difference is
    printed against a tolerance built from the manufacturer's stated rate
    accuracy plus the arithmetic error of counting over a finite span — see
    :func:`rate_tolerance_per_min`. Nothing is failed on it. That tolerance
    covers instrument accuracy and arithmetic; it does not cover how much a
    rate genuinely varies *within* one session, and no published figure bounds
    that. An earlier version failed the check outside a factor of two, a number
    justified by the ratios some misparses had happened to produce during
    development — which is a bound fitted to unpublishable evidence, and it has
    been withdrawn rather than reworded.

*   **Volume — reported, not asserted.** Integrating inspiratory flow was
    expected to land at or above the reported tidal volume, since the report is
    leak-compensated and the integral is not. It does not reliably do so.
    Several explanations remain open — the report may be BTPS corrected while
    the flow channel is not, the device may measure expiratory volume, or this
    module's breath segmentation may be too crude — and they cannot be told
    apart from the data alone. So the ratio is measured and printed, and
    nothing is concluded from it in either direction.

*   **Target volume — reported, not asserted.** The one check here holding a
    **setting** against a **measurement**; every other compares two of the
    device's own outputs, or an output against a published range. The delivered
    volume over the samples where the ``Target Volume`` state channel reads one
    is compared against the target the device was given, read through the
    *candidate* scale for ``VolumeTarget``. That is the point rather than a
    compromise: a ratio near one is evidence for the candidate and a ratio
    nowhere near it is evidence against, and neither is asserted on. What
    separates a delivered volume from its target is what the device is
    regulating, so bounding it would be a clinical claim.

*   **Physical plausibility — asserted.** Every check above compares the file
    against itself. This one compares it against ranges the manufacturer
    publishes, which is the only check here that could catch a header read
    wrongly but *consistently* — wrong scale factors or a wrong channel order
    leave a file in perfect agreement with itself. The bounds are deliberately
    generous and deliberately few: see :mod:`prisma_vent.device_limits` for
    which channels carry one, and for the longer list of channels whose bounds
    were withdrawn because no published figure supported them.

*   **Session duration against the device's own long-term record — asserted.**
    The device records each session's duration twice: in the session XML, and
    in the statistics it banks for the year. Comparing them establishes that
    session boundaries are read correctly. A session under a minute leaves no
    long-term entry at all, since the record stores whole minutes, and is
    reported as not comparable rather than as a failure.

*   **Session duration against the record count — not a check at all.** The
    wall-clock span and the span the records account for differ, and no
    mechanism explains the difference. It carries no status and reaches no
    verdict: it is exported unbounded, as ``duration_discrepancy_seconds``, so
    a consumer can see it.

The temptation in every unasserted case is to pick a bound that happens to fit
the recordings in front of you. That is how a check stops testing anything, and
it is also how a threshold smuggles somebody's therapy into a public
repository.

**A check that could not run is not a check that passed, and a check that only
reports is not a check that passed either.** Each check carries its own status
— ``passed``, ``failed``, ``reported`` or ``not comparable`` — since they are
independent and one can be impossible while another succeeds.

**Sampling rates come from the header, not from a constant.** A record may last
one second, but the header states its duration and this module reads it: every
rate and every integration below is expressed per second rather than per
record.
"""

from __future__ import annotations

import argparse
import enum
import statistics
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Sequence, TextIO

from .archive import ArchiveError, DayArchive, Session, open_day_archive
from .device_limits import bound_for
from .events import XmlError
from .parameter_registry import parameter_registries
from .statistic import StatisticError
from .timebase import TimebaseError
from .wmedf import (
    DiagnosticPolicy,
    ViolationCollector,
    WmedfError,
    iter_digital_chunks,
    read_header,
)

EXIT_OK = 0
EXIT_STRUCTURAL = 1
EXIT_USAGE = 2

#: The device's own stated accuracy for respiratory rate, from the
#: manufacturer's instructions for use — see ``docs/device-reference.md``,
#: "Respiratory rate accuracy", for the edition, section and page:
#: **±0.5 breaths per minute**.
#:
#: Note that this is an **absolute** figure, not a percentage. An earlier
#: version of this module gated on a fixed 5 % ratio, which is not the same
#: thing at any rate but ten per minute — it is 10 % at 5 bpm and 2.5 % at 20,
#: so it was simultaneously too loose at the bottom of the device's range and
#: too tight at the top.
DEVICE_RATE_ACCURACY_PER_MIN = 0.5

#: Counting breaths from the phase channel has its own error, independent of
#: the device's. Over a span of *T* minutes the count can be out by one breath
#: at either end, which is 1/T breaths per minute.
#:
#: The two combine into the bound this check applies:
#:
#:     tolerance = DEVICE_RATE_ACCURACY_PER_MIN + 1 / span_minutes
#:
#: Both terms are public: one is the manufacturer's figure, the other is
#: arithmetic. Neither comes from any recording.
def rate_tolerance_per_min(span_minutes: float) -> float:
    """Absolute tolerance in breaths per minute for a span of *span_minutes*."""
    if span_minutes <= 0:
        raise ValueError("span must be positive")
    return DEVICE_RATE_ACCURACY_PER_MIN + 1.0 / span_minutes


#: The shortest span over which a counted rate says anything. Derived, not
#: observed: require the counting term above to be no larger than the device's
#: own published accuracy, so that neither error source dominates —
#:
#:     1 / T <= 0.5   =>   T >= 2 minutes
#:
#: Applicable range: any session. Boundary behaviour: a session of exactly
#: 120 s is compared; anything shorter is ``not comparable``.
#:
#: Expressed in **seconds of recording**, not in records: the header states a
#: record's duration rather than this module assuming it.
MIN_SECONDS_FOR_RATE = 120.0

#: The phase channel must actually alternate for a rate to exist at all.
#:
#: Derived from the manufacturer's published ranges rather than from
#: recordings, and **rounded down**, because being too high here refuses
#: sessions the device can legitimately produce:
#:
#:     shortest settable inspiratory time            0.2 s
#:     longest period two edges can span in a
#:     session of MIN_SECONDS_FOR_RATE               120 s
#:     smallest producible duty cycle          0.2/120 = 0.00167
#:     rounded down to the next power of ten          0.001
#:
#: The 0.2 s comes from the specification table — see
#: ``docs/device-reference.md``, "Inspiratory time, shortest settable",
#: section 11.1.1, page 49. The 120 s is :data:`MIN_SECONDS_FOR_RATE`, itself
#: derived below. Nothing here is measured from a recording.
#:
#: This corrects an earlier value of 0.01, which was derived from a shortest
#: inspiratory time of 0.5 s and a 60 s period. The manual gives 0.5 s for
#: ``Ti/Ti max`` but **0.2 s** for ``Ti min``, so the old floor sat above what
#: the device can produce, and would have refused a session the device could
#: legitimately record.
#:
#: Applicable range: any session at least :data:`MIN_SECONDS_FOR_RATE` long.
#: Boundary behaviour: a duty cycle of exactly 0.001 is accepted and counted;
#: below it the check reports ``not comparable``.
#:
#: This is a precondition, not a tolerance. Below it there is no periodic
#: signal to count, and comparing a count of nothing against the device's *set*
#: rate would say something about an idle machine rather than about a decode.
MIN_PHASE_DUTY_CYCLE = 0.001


class CheckStatus(enum.Enum):
    """Outcome of one cross-channel check.

    Four outcomes, because they are four different claims:

    ``PASSED``
        The check ran and the decode satisfied it.
    ``FAILED``
        The check ran and the decode did not satisfy it.
    ``REPORTED``
        The check ran and produced a figure, but this module asserts nothing
        about that figure because no publicly derivable bound exists for it.
        **This is not ``PASSED``.** Reading it as success would credit the
        decode with having survived a test that was never applied.
    ``NOT_COMPARABLE``
        The check could not run at all.
    """

    PASSED = "passed"
    FAILED = "failed"
    REPORTED = "reported"
    NOT_COMPARABLE = "not comparable"


@dataclass(frozen=True)
class CheckResult:
    """One check's outcome, why, and the ratio it measured."""

    status: CheckStatus
    detail: str | None = None
    ratio: float | None = None

    @classmethod
    def not_comparable(cls, reason: str) -> "CheckResult":
        return cls(CheckStatus.NOT_COMPARABLE, detail=reason)

    @classmethod
    def reported(cls, detail: str, ratio: float | None = None) -> "CheckResult":
        """A figure with no bound behind it. Deliberately not ``PASSED``."""
        return cls(CheckStatus.REPORTED, detail=detail, ratio=ratio)


#: Order the summary lists them in.
_CHECK_NAMES = (
    "breath rate",
    "volume",
    "target volume",
    "plausibility",
    "long-term record",
)

#: Distinguishes "no long-term record" from "caller did not supply one".
_UNSET: object = object()

_CHANNELS = (
    "Breath Phase",
    "Frequency",
    "Patient Flow",
    "Target Volume",
    "Tidal Volume",
    "TotalLeakage",
)


@dataclass
class SessionReport:
    number: int
    records: int
    seconds: float
    duration_discrepancy_s: float
    violations: ViolationCollector
    breath: CheckResult = field(
        default_factory=lambda: CheckResult.not_comparable("not attempted")
    )
    volume: CheckResult = field(
        default_factory=lambda: CheckResult.not_comparable("not attempted")
    )
    target_volume: CheckResult = field(
        default_factory=lambda: CheckResult.not_comparable("not attempted")
    )
    plausibility: CheckResult = field(
        default_factory=lambda: CheckResult.not_comparable("not attempted")
    )
    long_term: CheckResult = field(
        default_factory=lambda: CheckResult.not_comparable("not attempted")
    )
    leak: float | None = None

    @property
    def checks(self) -> dict[str, CheckResult]:
        # Independent checks, each with its own status: one can be impossible
        # while another succeeds.
        return {
            "breath rate": self.breath,
            "volume": self.volume,
            "target volume": self.target_volume,
            "plausibility": self.plausibility,
            "long-term record": self.long_term,
        }

    @property
    def failures(self) -> list[tuple[str, str]]:
        return [
            (name, r.detail or "")
            for name, r in self.checks.items()
            if r.status is CheckStatus.FAILED
        ]

    @property
    def any_check_ran(self) -> bool:
        return any(
            r.status is not CheckStatus.NOT_COMPARABLE for r in self.checks.values()
        )


@dataclass
class ArchiveReport:
    name: str
    sessions: list[SessionReport] = field(default_factory=list)
    error: str | None = None

    @property
    def range_violations(self) -> int:
        return sum(s.violations.total_seen for s in self.sessions)

    @property
    def failed(self) -> bool:
        """Whether normal validation should treat this archive as a failure.

        Range violations count. A sample outside the range its own header
        declares is the signature of a misaligned decode; surveying one and
        then exiting zero would report the problem and pass anyway.
        """
        return (
            self.error is not None
            or any(s.failures for s in self.sessions)
            or self.range_violations > 0
        )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare this command's options; see :func:`prisma_vent.inspect.add_arguments`."""
    parser.add_argument("archive", nargs="+", type=Path)
    parser.add_argument(
        "--report",
        action="store_true",
        help=(
            "survey only: report findings without treating them as failure. "
            "Cross-channel disagreements and range violations are printed and "
            "the exit code stays zero; an archive that cannot be read at all "
            "still fails, because there was nothing to survey."
        ),
    )


def run(args: argparse.Namespace, out: TextIO) -> int:
    reports = [_check(path) for path in args.archive]
    _print(reports, out, report_only=args.report)

    if args.report:
        # Survey mode judges nothing it surveyed - neither disagreements nor
        # range violations. Only an unreadable archive still fails, since there
        # was nothing to survey in the first place.
        return EXIT_STRUCTURAL if any(r.error for r in reports) else EXIT_OK
    return EXIT_STRUCTURAL if any(r.failed for r in reports) else EXIT_OK


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m prisma_vent.validate",
        description="Cross-check decoded archives against themselves. Read-only.",
    )
    add_arguments(parser)
    args = parser.parse_args(argv)
    return run(args, out if out is not None else sys.stdout)


def _check(path: Path) -> ArchiveReport:
    report = ArchiveReport(name=path.name)
    try:
        with open_day_archive(path) as archive:
            # Both of these answer a question about the *archive*, so they
            # are asked once. Left to their defaults they would be asked again
            # for every session, re-parsing the same members each time.
            long_term = _long_term_index(archive)
            target = target_volume_setting(archive)
            for session in archive.sessions:
                report.sessions.append(
                    _check_session(
                        archive, session, long_term=long_term, target=target
                    )
                )
    except (ArchiveError, WmedfError, XmlError, TimebaseError) as exc:
        report.error = str(exc)
    except FileNotFoundError:
        report.error = "no such file"
    return report


def _check_session(
    archive: DayArchive,
    session: Session,
    *,
    long_term: dict | None = _UNSET,
    target: tuple[float | None, str] | object = _UNSET,
) -> SessionReport:
    """Run every check on one session.

    *long_term* is the archive's long-term index, built once per archive by
    :func:`_long_term_index` because reading it per session would re-parse a
    multi-megabyte member for every one. Left unset it is built here, so a caller that
    forgets cannot silently skip the check — which is how the exported
    validation report first came to report it as never attempted.
    """
    collector = ViolationCollector()
    header = session.header
    wanted = {}
    for label in _CHANNELS:
        try:
            wanted[header.signal_by_label(label).index] = label
        except WmedfError:
            pass  # a firmware need not write every channel

    samples: dict[int, list[int]] = {index: [] for index in wanted}
    # Extremes for every channel, not only the ones the cross-channel checks
    # need: the plausibility bounds apply to channels nothing else looks at.
    extremes: dict[int, tuple[int, int]] = {}
    with archive.open_signal(session) as stream:
        live = read_header(stream)
        for chunk in iter_digital_chunks(
            stream,
            live,
            policy=DiagnosticPolicy.COLLECT_VIOLATIONS,
            violations=collector,
        ):
            if chunk.signal.index in samples:
                samples[chunk.signal.index].extend(chunk.digital)
            if chunk.digital:
                low, high = min(chunk.digital), max(chunk.digital)
                previous = extremes.get(chunk.signal.index)
                extremes[chunk.signal.index] = (
                    (low, high)
                    if previous is None
                    else (min(previous[0], low), max(previous[1], high))
                )

    seconds = header.n_records * header.record_duration_s
    result = SessionReport(
        number=session.number,
        records=header.n_records,
        seconds=seconds,
        duration_discrepancy_s=session.duration_discrepancy.total_seconds(),
        violations=collector,
    )

    named = {label: (header.signals[i], samples[i]) for i, label in wanted.items()}
    result.breath = _breath_rate(named, header.record_duration_s, seconds)
    result.volume, result.leak = _volume(named, header.record_duration_s)
    setting, reason = (
        target_volume_setting(archive) if target is _UNSET else target
    )
    result.target_volume = _target_volume(named, setting, reason)
    result.plausibility = plausibility(header, extremes)
    index = _long_term_index(archive) if long_term is _UNSET else long_term
    result.long_term = against_long_term(session, index)
    return result


# The device writes a session's duration twice, through two unrelated
# subsystems: the signal file's own record count, and its long-term statistics.
# Comparing them is the only check here that sets one of the device's own
# outputs against another rather than against this decoder's expectations.

#: How far a long-term record's timestamp may sit from the session start it
#: belongs to. Derived from the format rather than from observation: the
#: long-term record stores whole minutes, so its idea of when a session began
#: can differ from the archive's by up to one storage quantum.
#:
#: The window's width cannot produce a wrong match. Where more than one record
#: falls inside it, :func:`against_long_term` reports the session as not
#: comparable instead of choosing — ambiguity is refused, never resolved.
_LONG_TERM_SKEW = timedelta(seconds=60)

#: A session shorter than this leaves no long-term record at all, because the
#: device stores whole minutes and this one truncates to zero. Its absence is
#: therefore expected rather than a finding.
_LONG_TERM_FLOOR = timedelta(minutes=1)


def _long_term_index(archive: DayArchive) -> dict | None:
    """The archive's long-term records grouped by start time, or None.

    None means the member is absent or unreadable, which makes the check not
    comparable rather than failed: a firmware need not write it.

    **Every record is kept, including several sharing one timestamp.** This
    used to be a ``{start: record}`` comprehension, which silently discarded
    all but the last record of any duplicated timestamp — so the same file
    could pass or fail depending only on the order its records happened to be
    written in. Two records at one instant declaring different durations are
    ambiguous, and ambiguity is refused here rather than resolved by whichever
    one a dict comprehension happened to keep.

    Discarding them at parse time was the alternative. It was rejected because
    nothing in the format forbids two records at one instant, so refusing the
    whole archive would turn an ambiguity about one session into a failure to
    read anything at all.
    """
    try:
        statistic = archive.statistic()
    except (ArchiveError, StatisticError):
        return None
    grouped: dict = {}
    for record in statistic.records:
        grouped.setdefault(record.start.value, []).append(record)
    return grouped


def against_long_term(session: Session, index: dict | None) -> CheckResult:
    """Compare a session's duration against the device's own statistics entry.

    **Which duration, and why it is the weaker of the two available.**

    A session has two spans: :attr:`Session.duration`, from the start and stop
    times in the session XML, and :attr:`Session.accounted_duration`, from the
    signal file's record count. They differ, by enough to land on either side
    of a whole minute, so which one is compared matters.

    **It is the XML span.** The device banks the interval between the times it
    recorded as start and stop, truncated to whole minutes; comparing against
    the record count instead fails sessions whose decode is perfectly correct.
    An earlier version of this docstring claimed the opposite and described the
    check as two unrelated subsystems agreeing.

    Be clear about what this therefore establishes. The banked figure and the
    session XML come from the same side of the device, so their agreement does
    **not** prove the signal decoding. What it does prove is that session
    boundaries are read correctly: a shifted or misattributed time base puts
    the two out of step. That is a weaker check than the one first described
    here, and saying so is better than leaving the stronger claim standing.

    The XML-versus-records difference is exported separately as
    ``duration_discrepancy_seconds``, unbounded, so a consumer can see it.
    """
    if index is None:
        return CheckResult.not_comparable(
            "the archive carries no readable long-term record"
        )

    start = session.start.value
    # The XML span, which is what the device banks. See the docstring: the
    # record-derived span is a different quantity and does not match it.
    banked_span = session.duration
    # Flattened across timestamps *and* within them: several records sharing
    # one instant are several candidates, not one.
    candidates = [
        record
        for moment, records in index.items()
        if abs(moment - start) <= _LONG_TERM_SKEW
        for record in records
    ]
    if not candidates:
        if banked_span < _LONG_TERM_FLOOR:
            return CheckResult.not_comparable(
                f"the session spans {banked_span.total_seconds():.0f} s, "
                f"under the minute the device stores as a whole number, so it "
                f"leaves no long-term record"
            )
        return CheckResult(
            CheckStatus.FAILED,
            detail=(
                f"the session spans "
                f"{banked_span.total_seconds() / 60:.2f} min but the "
                f"device's own long-term record holds no entry within "
                f"{_LONG_TERM_SKEW.total_seconds():.0f} s of its start. One of "
                f"the two time bases is being read wrongly"
            ),
        )
    if len(candidates) > 1:
        # Includes the case of several records at the *same* instant. Which of
        # them the device meant cannot be recovered from the file, so no answer
        # is given rather than an answer that depends on record order.
        return CheckResult.not_comparable(
            f"{len(candidates)} long-term records fall within "
            f"{_LONG_TERM_SKEW.total_seconds():.0f} s of this session's start, "
            f"so none can be attributed to it"
        )

    record = candidates[0]
    # The device truncates to whole minutes, so the floor is the value to
    # compare — rounding would disagree with the device half the time.
    expected = int(banked_span.total_seconds() // 60)
    if record.duration_minutes != expected:
        return CheckResult(
            CheckStatus.FAILED,
            detail=(
                f"the session spans "
                f"{banked_span.total_seconds() / 60:.2f} min, which "
                f"truncates to {expected}, but the device's own long-term "
                f"record says {record.duration_minutes}. Two of the device's "
                f"outputs disagree about the same session"
            ),
            ratio=(
                record.duration_minutes / expected if expected else None
            ),
        )
    return CheckResult(CheckStatus.PASSED, ratio=1.0)


def plausibility(header, extremes: dict[int, tuple[int, int]]) -> CheckResult:
    """Compare decoded extremes against ranges the manufacturer publishes.

    Takes the digital minimum and maximum already seen per channel and
    converts them, rather than re-reading: the extremes are what the bounds
    are about.

    A bound may be one-sided — most of the surviving ones are, because the
    manufacturer publishes a maximum and says nothing below it. An open side
    is not checked, and :meth:`PhysicalBound.exceeded_by` is what decides,
    rather than a comparison against a sentinel.
    """
    checked = 0
    failures: list[str] = []
    for signal in header.signals:
        bound = bound_for(signal.label)
        if bound is None or signal.index not in extremes:
            continue
        checked += 1
        low_digital, high_digital = extremes[signal.index]
        low = signal.to_physical_value(low_digital)
        high = signal.to_physical_value(high_digital)
        if bound.exceeded_by(low, high):
            failure = (
                f"{signal.label} reaches {low:g}..{high:g} {bound.unit}, outside the "
                f"published {bound.describe()} "
                f"[{bound.basis}] ({bound.source})"
            )
            # The mistake this check made on its first run was reading a
            # settable range as a measurement limit. A bound narrower than what
            # the device's own header declares possible is the signature of
            # exactly that, so say so where it is actionable rather than
            # leaving the reader to rediscover it.
            narrower = (
                bound.minimum is not None and bound.minimum > signal.physical_min
            ) or (bound.maximum is not None and bound.maximum < signal.physical_max)
            if narrower:
                failure += (
                    f". Note that this bound is narrower than the range the "
                    f"file's own header declares for the channel "
                    f"({signal.physical_min:g}..{signal.physical_max:g}), so the "
                    f"bound may be describing a setting rather than a "
                    f"measurement — check it before suspecting the decode"
                )
            failures.append(failure)

    if not checked:
        return CheckResult.not_comparable(
            "no channel in this file has a published physical bound. Most "
            "channels do not: see prisma_vent.device_limits for which bounds "
            "exist and which were withdrawn for lacking a published basis"
        )
    if failures:
        return CheckResult(
            CheckStatus.FAILED,
            detail=(
                "; ".join(failures)
                + ". These bounds come from the manufacturer rather than from the "
                "file, so this is the one check that survives a header read "
                "wrongly but consistently."
            ),
        )
    return CheckResult(CheckStatus.PASSED)


def _sampling_rate_hz(signal, record_duration_s: float) -> float:
    """Samples per second, which is not the same as samples per record.

    A record lasts one second on the firmware examined, but the header states
    its duration and this module reads it rather than assuming it.
    """
    return signal.samples_per_record / record_duration_s


def _breath_rate(named: dict, record_duration_s: float, seconds: float) -> CheckResult:
    missing = [c for c in ("Breath Phase", "Frequency") if c not in named]
    if missing:
        return CheckResult.not_comparable(
            f"channel(s) {', '.join(missing)} absent from this file"
        )

    phase_signal, phase = named["Breath Phase"]
    freq_signal, freq = named["Frequency"]
    if not phase or not freq:
        return CheckResult.not_comparable("no samples in the required channels")

    if seconds < MIN_SECONDS_FOR_RATE:
        return CheckResult.not_comparable(
            f"session is {seconds:.0f} s long, under the {MIN_SECONDS_FOR_RATE:.0f} s "
            "a rate needs in order to rise above sampling error"
        )

    duty = _duty_cycle(phase)
    if duty < MIN_PHASE_DUTY_CYCLE:
        return CheckResult.not_comparable(
            f"phase channel alternates for only {duty:.1%} of the session, so there "
            "is no regular breathing rate to count. Mouthpiece ventilation looks "
            "like this — breaths are taken on demand — and so does a device left "
            "running with nothing connected. Which it was cannot be told from "
            "this channel alone"
        )

    edges = _rising_edges(phase)
    if len(edges) < 2:
        return CheckResult.not_comparable("fewer than two breaths detected")

    # Rate is measured between the first and last edge, not over the whole
    # session. Dividing edge count by total length silently omits the breath
    # before the first edge — negligible over an hour, three percent over two
    # minutes, and a bias either way.
    rate_hz = _sampling_rate_hz(phase_signal, record_duration_s)
    span_minutes = (edges[-1] - edges[0]) / rate_hz / 60
    if span_minutes <= 0:
        return CheckResult.not_comparable("the detected breaths span no time")
    counted = (len(edges) - 1) / span_minutes

    reported_values = [freq_signal.to_physical_value(v) for v in freq if v > 0]
    if not reported_values:
        return CheckResult.not_comparable("rate channel reports nothing above zero")
    reported = statistics.median(reported_values)
    if reported <= 0:
        return CheckResult.not_comparable("rate channel median is not positive")

    ratio = counted / reported
    # Absolute, not relative: the device's published rate accuracy is ±0.5 per
    # minute regardless of the rate, and the counting error is one breath over
    # the span. A percentage of the rate is neither of those things.
    tolerance = rate_tolerance_per_min(span_minutes)
    difference = abs(counted - reported)
    within = difference <= tolerance

    # **Nothing is asserted here.** The tolerance is publicly derived, but it
    # accounts only for instrument accuracy and counting arithmetic. It does
    # not account for how much a rate varies within one session, and no
    # published figure does. A gross-disagreement factor used to sit here and
    # fail the check; the number behind it came from what some misparses had
    # happened to produce during development, which is a bound fitted to
    # evidence that cannot be published. It has been withdrawn.
    #
    # On-demand ventilation, where breaths are not delivered at a set rate,
    # produces a large difference from a perfectly correct decode — the
    # channels alone cannot tell that apart from a misparse, which is the
    # second reason this reports rather than judges.
    return CheckResult.reported(
        detail=(
            f"counted {counted:.2f}/min from the phase channel against a "
            f"reported {reported:.2f}/min — a difference of {difference:.2f}/min "
            f"{'within' if within else 'above'} the {tolerance:.2f}/min the "
            f"device's published accuracy and the counting arithmetic account "
            f"for. Reported, not asserted: nothing published bounds how much a "
            f"rate varies within a session, and on-demand ventilation produces "
            f"a large difference from a correct decode"
        ),
        ratio=ratio,
    )


def _volume(named: dict, record_duration_s: float) -> tuple[CheckResult, float | None]:
    required = ("Patient Flow", "Tidal Volume", "Breath Phase")
    missing = [c for c in required if c not in named]
    if missing:
        return (
            CheckResult.not_comparable(
                f"channel(s) {', '.join(missing)} absent from this file"
            ),
            None,
        )

    phase_signal, phase = named["Breath Phase"]
    flow_signal, flow = named["Patient Flow"]
    tidal_signal, tidal = named["Tidal Volume"]
    if not phase or not flow or not tidal:
        return CheckResult.not_comparable("no samples in the required channels"), None

    edges = _rising_edges(phase)
    if len(edges) < 3:
        return CheckResult.not_comparable("too few breaths to integrate over"), None

    # Minutes one flow sample stands for, taken from the header's own record
    # duration rather than an assumed second.
    per_sample_minutes = 1.0 / (_sampling_rate_hz(flow_signal, record_duration_s) * 60)

    volumes = []
    for start_i, end_i in zip(edges, edges[1:], strict=False):
        total = 0.0
        for index in range(start_i, min(end_i, len(flow))):
            value = flow_signal.to_physical_value(flow[index])
            if value > 0:
                total += value
        volumes.append(total * per_sample_minutes * 1000)
    reported = [tidal_signal.to_physical_value(v) for v in tidal if v > 0]
    if not volumes or not reported:
        return CheckResult.not_comparable("nothing to compare"), None

    integrated = statistics.median(volumes)
    declared = statistics.median(reported)
    if declared <= 0:
        return CheckResult.not_comparable("reported tidal volume is not positive"), None

    leak = None
    if "TotalLeakage" in named:
        leak_signal, leak_samples = named["TotalLeakage"]
        if leak_samples:
            leak = statistics.median(
                leak_signal.to_physical_value(v) for v in leak_samples
            )

    # Deliberately no assertion here. See the module docstring: the ratio was
    # expected to be one-sided and is not, and the competing explanations
    # cannot be told apart from the data. Reported, never passed — calling it
    # passed would credit the decode with surviving a test never applied.
    ratio = integrated / declared
    return (
        CheckResult.reported(
            detail=(
                f"integrated inspiratory flow over the reported tidal volume is "
                f"{ratio:.3f}. No bound is asserted in either direction"
            ),
            ratio=ratio,
        ),
        leak,
    )


# The device stores a volume target per therapy programme and measures the
# volume it actually delivered. Holding one against the other is a comparison
# between a setting and a measurement, which no other check here makes.

#: The manual's settable range for target volume, section 11.1.1, page 49. Used
#: for one thing only: deciding whether a **stored** value is a setting at all.
#: A programme with a target outside this range has not had one dialled in —
#: the device stores zero there — and zero is not a target of zero millilitres.
#: See ``docs/device-reference.md`` on why a settable range may bound a setting
#: and never a measurement.
_TARGET_VOLUME_SETTABLE_ML = (100.0, 2000.0)


#: Parameters whose change would move the answer. A record altering any of them
#: mid-archive means the target in force is not one value for the whole file.
#:
#: ``ProgramEnabled`` is a prefix because the device numbers one per programme.
#: ``VolumeTarget`` is matched **whole**: ``VolumeTargetControl`` and
#: ``VolumeTargetDeltaPressure`` are separate parameters that share its opening
#: letters and move nothing here, and matching them by prefix would refuse the
#: check while saying the target had changed — a refusal is only useful if its
#: stated reason is true. ``test_a_neighbouring_volume_target_parameter_does_not
#: _refuse`` holds this.
_TARGET_VOLUME_ENABLE_PREFIX = "ProgramEnabled"
_TARGET_VOLUME_PARAMETER = "VolumeTarget"


def _target_from_snapshot(snapshot, names, scale) -> tuple[float | None, str]:
    """The target one full settings snapshot puts in force, or why not.

    Refuses on anything it does not recognise rather than skipping it. An
    unreadable or out-of-range value used to be passed over, which left the
    *other* programme's target standing as if it were unambiguous — a clean
    ratio produced by discarding the evidence against it.
    """
    device = {names.get(entry.id): entry.value for entry in snapshot.device_parameters}

    # Every way of not knowing whether a programme is on is refused here, and
    # none of them is read as "off". Which programme delivered therapy is not
    # established, so a block wrongly treated as disabled takes its target out
    # of the comparison silently — and what is left then looks unambiguous
    # precisely because the disagreeing evidence was dropped.
    #
    # `ProgramEnabledN` is numbered from one over the blocks in order, so the
    # blocks have to be numbered the way that assumes. A block positioned
    # outside the run would otherwise match no flag and be skipped without
    # anything saying so.
    positions = [block.position for block in snapshot.therapy_programs]
    if sorted(positions) != list(range(len(positions))):
        return None, (
            "this archive's therapy programme blocks are not numbered from the "
            "first without gaps, so which enable flag belongs to which block "
            "is not established"
        )

    # Nothing requires these names to be present: `parameter_registries` gates
    # on the ids it publishes scales for, and `ProgramEnabled*` is not among
    # them. A map that never names them is not a device with everything
    # switched off, and a partial set is the same assertion made once per
    # block.
    flags = [f"ProgramEnabled{position + 1}" for position in positions]
    if any(flag not in device for flag in flags):
        return None, (
            "this archive's parameter map does not name which programmes are "
            "enabled"
        )

    # Only the two documented values. An unrecognised one is not a third state
    # to guess at: reading it as "off" hides that block's target, which is how
    # a competing target disappeared and left a ratio of exactly one behind.
    unknown = sorted({device[flag] for flag in flags} - {"0", "1"})
    if unknown:
        return None, (
            f"a programme enable flag reads {unknown[0]!r}, which is neither "
            "enabled nor disabled"
        )

    enabled = {
        position for position, flag in zip(positions, flags, strict=True)
        if device[flag] == "1"
    }
    if not enabled:
        return None, "no therapy programme is enabled"

    low, high = _TARGET_VOLUME_SETTABLE_ML
    targets = set()
    for block in snapshot.therapy_programs:
        if block.position not in enabled:
            continue
        for entry in block.parameters:
            if names.get(entry.id) != "VolumeTarget":
                continue
            try:
                raw = int(entry.value)
            except ValueError:
                return None, (
                    f"VolumeTarget reads {entry.value!r}, which is not a number"
                )
            # Zero is the one value documented as meaning no target. Anything
            # else outside the settable range is unexplained, and treating it
            # as absent would hide it behind another programme's answer.
            if raw == 0:
                continue
            millilitres = raw * scale["physical_delta"] / scale["raw_delta"]
            if not low <= millilitres <= high:
                return None, (
                    f"VolumeTarget reads {entry.value!r}, which is neither zero "
                    f"nor inside the settable {low:.0f}-{high:.0f} ml range"
                )
            targets.add(millilitres)

    if not targets:
        return None, "no enabled programme has a volume target set"
    if len(targets) > 1:
        return None, "enabled programmes have different target volumes"
    return targets.pop(), ""


def target_volume_setting(archive: DayArchive) -> tuple[float | None, str]:
    """The one volume target in force across this archive, or why there is none.

    Which programme a session ran under is **not** established — a programme
    selected on a settings page need not be the one delivering therapy — so
    this does not pick a block. It asks a narrower question: do the enabled
    programmes agree on **exactly one distinct non-zero target**? Where they
    do, the answer does not depend on which programme ran.

    **Nor does it depend on which snapshot is read.** ``parameter.xml`` holds
    full snapshots *and* single-entry change records, so a target can move
    within one archive. Reading the last snapshot and applying it to every
    session would date a setting wrongly for every session before the change —
    a 200 ml night compared against a 500 ml target set the following noon
    reports a ratio of 0.4 and looks like a decoding fault. Reconstructing the
    settings history per session is the eventual answer; until then a target
    that is not constant across the archive is refused.

    The value is read through the scale candidate for ``VolumeTarget``, which
    rests on a single reference point. **That is the point of the comparison**:
    a ratio near one is evidence for the candidate, and a ratio nowhere near it
    is evidence against. Neither is asserted on, and no bound is applied.
    """
    try:
        parameter_map = archive.parameter_map()
        log = archive.parameter_log(parameter_map)
    except (ArchiveError, XmlError) as exc:
        return None, f"settings unreadable: {exc}"

    _, candidates, _ = parameter_registries(parameter_map, log.config_version)
    scale = candidates.get("VolumeTarget")
    if scale is None:
        return None, "no scale is published for VolumeTarget on this map version"

    names = parameter_map.names
    full = [s for s in log.snapshots if len(s.therapy_programs) > 1]
    if not full:
        return None, "no full settings snapshot in this archive"

    for snapshot in log.snapshots:
        if len(snapshot.therapy_programs) > 1:
            continue
        touched = {names.get(entry.id) for entry in snapshot.device_parameters}
        for block in snapshot.therapy_programs:
            touched |= {names.get(entry.id) for entry in block.parameters}
        if any(
            name == _TARGET_VOLUME_PARAMETER
            or (name or "").startswith(_TARGET_VOLUME_ENABLE_PREFIX)
            for name in touched
        ):
            return None, (
                "a settings change within this archive altered the volume "
                "target or which programmes are enabled"
            )

    answers = {_target_from_snapshot(s, names, scale) for s in full}
    if len(answers) > 1:
        # Not necessarily two different targets: one snapshot may answer and
        # another refuse. Either way no single target holds for the archive,
        # and naming a cause the data does not show would be worse than saying
        # only what disagreeing snapshots establish.
        return None, "this archive's settings snapshots do not agree on one target"
    return answers.pop()


def _target_volume(named: dict, setting: float | None, reason: str) -> CheckResult:
    """The delivered volume against the target, while targeting was running.

    ``Target Volume`` is a **state channel**, not a setpoint: it reads zero or
    one, and its name says otherwise. The comparison is therefore restricted to
    the samples where it reads one, because a breath delivered with targeting
    off has nothing to do with the target.

    Always :data:`CheckStatus.REPORTED`. Nothing published bounds how far a
    delivered volume may sit from its target — that difference is what the
    device is regulating, and a bound on it would be a clinical claim.
    """
    if setting is None:
        return CheckResult.not_comparable(reason)
    required = ("Target Volume", "Tidal Volume")
    missing = [c for c in required if c not in named]
    if missing:
        return CheckResult.not_comparable(
            f"channel(s) {', '.join(missing)} absent from this file"
        )

    state_signal, state = named["Target Volume"]
    tidal_signal, tidal = named["Tidal Volume"]
    if not state or not tidal:
        return CheckResult.not_comparable("no samples in the required channels")

    # **Which digital value means "off" is read from the header, not assumed.**
    # This device declares `Breath Phase` as digital 1..2 and `Target Volume`
    # as 0..1, both mapping to physical 0..1 — two state channels, two
    # encodings, one file. Testing the digital sample against zero happens to
    # work for one of them and silently counts every sample as active for the
    # other. Converted through the channel's own affine mapping, "off" is its
    # physical minimum in either case.
    off = state_signal.physical_min

    # The two channels can be sampled at different rates, so a sample index in
    # one is not a sample index in the other. Both cover the same span, so the
    # position within that span is what lines them up.
    # Counted apart, because the two exclusions mean different things and the
    # report names one of them. Folding them into a single list made the
    # sentence below say that samples where targeting was on were samples where
    # it was off.
    targeting = 0
    delivered = []
    for index, value in enumerate(tidal):
        position = index / len(tidal)
        sample = state[min(int(position * len(state)), len(state) - 1)]
        if state_signal.to_physical_value(sample) <= off:
            continue
        targeting += 1
        physical = tidal_signal.to_physical_value(value)
        if physical > 0:
            delivered.append(physical)

    if not targeting:
        return CheckResult.not_comparable("targeting was not active in this session")
    if not delivered:
        # Targeting ran and every delivered volume was zero or below. Saying
        # targeting was not active would describe a different session.
        return CheckResult.not_comparable(
            "no delivered volume above zero while targeting was active"
        )

    median = statistics.median(delivered)
    ratio = median / setting
    return CheckResult.reported(
        f"median delivered volume over the volume target is {ratio:.3f}, "
        f"measured over the {len(delivered)} samples above zero among the "
        f"{targeting} of {len(tidal)} where targeting was active. No bound is "
        "asserted in either direction",
        ratio=ratio,
    )


def _duty_cycle(values: Sequence[int]) -> float:
    """Fraction of samples in the *less* common of a two-state channel's states.

    Zero means the channel never changed state at all.
    """
    if not values:
        return 0.0
    high = max(values)
    in_high = sum(1 for v in values if v == high)
    return min(in_high, len(values) - in_high) / len(values)


def _rising_edges(values: Sequence[int]) -> list[int]:
    """Indices where a two-state channel enters its upper state."""
    if not values:
        return []
    high = max(values)
    return [i for i in range(1, len(values)) if values[i] == high and values[i - 1] != high]


def _print(reports: list[ArchiveReport], out: TextIO, *, report_only: bool) -> None:
    discrepancies: list[float] = []
    ratios: list[tuple[float, float]] = []
    failures = 0
    violations = 0
    sessions = 0
    uncheckable = 0
    tallies = {name: {status: 0 for status in CheckStatus} for name in _CHECK_NAMES}

    for report in reports:
        if report.error:
            print(f"{report.name}: {report.error}", file=sys.stderr)
            continue
        for session in report.sessions:
            sessions += 1
            discrepancies.append(session.duration_discrepancy_s)
            if session.volume.ratio is not None and session.leak is not None:
                ratios.append((session.volume.ratio, session.leak))
            if not session.any_check_ran:
                uncheckable += 1

            for name, result in session.checks.items():
                tallies[name][result.status] += 1
                if result.status is CheckStatus.FAILED:
                    failures += 1
                    print(f"{report.name} s{session.number:04d}: {result.detail}", file=out)
                elif result.status is CheckStatus.NOT_COMPARABLE:
                    print(
                        f"{report.name} s{session.number:04d}: {name} not comparable — "
                        f"{result.detail}",
                        file=out,
                    )
                elif result.status is CheckStatus.REPORTED:
                    # Printed on its own wording, never as "passed". These are
                    # the checks that ran and produced a figure this module
                    # refuses to judge.
                    print(
                        f"{report.name} s{session.number:04d}: {name} reported "
                        f"(not asserted) — {result.detail}",
                        file=out,
                    )
                elif result.detail:
                    print(
                        f"{report.name} s{session.number:04d}: {name} noted — "
                        f"{result.detail}",
                        file=out,
                    )

            if session.violations.total_seen:
                violations += session.violations.total_seen
                print(
                    f"{report.name} s{session.number:04d}: RANGE VIOLATION — "
                    f"{session.violations.summary()}",
                    file=out,
                )

    print(file=out)
    print(f"sessions read: {sessions}", file=out)
    width = max(len(name) for name in _CHECK_NAMES)
    for name in _CHECK_NAMES:
        counts = tallies[name]
        print(
            f"  {name:{width}s} passed {counts[CheckStatus.PASSED]}   "
            f"failed {counts[CheckStatus.FAILED]}   "
            f"reported {counts[CheckStatus.REPORTED]}   "
            f"not comparable {counts[CheckStatus.NOT_COMPARABLE]}",
            file=out,
        )
    print(
        "  'reported' is not 'passed': the check ran and produced a figure "
        "this\n  decoder asserts nothing about, because no publicly derivable "
        "bound exists\n  for it.",
        file=out,
    )
    if uncheckable:
        print(
            f"  {uncheckable} session(s) had no cross-channel check run at all",
            file=out,
        )

    if discrepancies:
        print(
            "\nsession span minus the seconds its records account for "
            "(reported, not bounded):",
            file=out,
        )
        print(
            f"  min {min(discrepancies):+.3f}s  median "
            f"{statistics.median(discrepancies):+.3f}s  max {max(discrepancies):+.3f}s",
            file=out,
        )
        print(
            "  No limit is enforced. Neither tail has an explanation, and a bound\n"
            "  fitted to this spread would be a number chosen to pass.",
            file=out,
        )

    if len(ratios) >= 3:
        volume = [r for r, _ in ratios]
        leak = [value for _, value in ratios]
        print("\nintegrated flow over reported tidal volume:", file=out)
        print(
            f"  median {statistics.median(volume):.3f}  "
            f"min {min(volume):.3f}  max {max(volume):.3f}",
            file=out,
        )
        try:
            correlation = statistics.correlation(volume, leak)
        except statistics.StatisticsError:
            correlation = float("nan")
        print(
            f"  correlation with total leak: {correlation:+.3f}\n"
            "  Leak explains much of the spread above one but not the sessions\n"
            "  below it, so no bound is asserted on this ratio.",
            file=out,
        )

    print(file=out)
    if violations:
        # Named separately because it is a different kind of fault: a sample
        # outside its own declared range points at the decode, not at two
        # channels disagreeing.
        print(
            f"{violations} sample(s) outside their declared digital range.",
            file=out,
        )
    if failures:
        print(f"{failures} cross-channel disagreement(s).", file=out)
    elif any(
        tallies[n][CheckStatus.PASSED] or tallies[n][CheckStatus.REPORTED]
        for n in tallies
    ):
        print(
            "No cross-channel disagreement among the checks that assert.",
            file=out,
        )
    else:
        # Saying "no disagreement" here would claim a clean result from checks
        # that never happened.
        print("No cross-channel check could be run.", file=out)

    if report_only and (failures or violations):
        print("Survey mode: reported, not treated as failure.", file=out)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
