"""Reader for ``.wmedf`` signal files.

``.wmedf`` is EDF with modifications, and the modifications are the reason a
standard EDF library must not be pointed at these files:

*   **Channel width is declared per signal**, in that signal's own 32-byte
    reserved field: ``#1`` means one byte per sample, ``#2`` means two. A
    record therefore interleaves 8-bit and 16-bit channels. A reader that
    assumes a uniform 16-bit layout misaligns every channel after the first
    8-bit one — silently, and with values that still look plausible.

*   **8-bit signedness follows the declared digital minimum.** A single
    convention across all 8-bit channels is wrong: one channel may span
    −100…100 while another spans 0…255.

*   16-bit samples are **little-endian**, established empirically against this
    device rather than assumed.

Decoding and scaling are separate operations here, and stay visible as two
steps: :func:`iter_digital_chunks` yields raw integers with ``physical`` left
unset, :func:`to_physical` fills it in, and :func:`iter_chunks` composes the
two. Keeping them apart means a wrong result can be attributed to the byte
decoding or to the conversion instead of being ambiguous, and an unscaled
chunk is distinguishable from a scaled one rather than merely undocumented.

Nothing the file declares is hardcoded here — channel count, order, widths and
ranges are read per file. They describe what one firmware version writes, not
a fact about the device.
"""

from __future__ import annotations

import enum
import math
import re
import sys
from array import array
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction
from typing import BinaryIO, Iterator, Sequence

from .timebase import DeviceLocalTime

__all__ = [
    "WmedfError",
    "DiagnosticPolicy",
    "RangeViolation",
    "ViolationCollector",
    "DEFAULT_VIOLATION_CAP",
    "verify_against_size",
    "Signal",
    "WmedfHeader",
    "SignalChunk",
    "read_header",
    "iter_digital_chunks",
    "to_physical",
    "iter_chunks",
    "read_all",
    "sample_time",
    "DEFAULT_RECORDS_PER_CHUNK",
]


class WmedfError(ValueError):
    """The file is not the format this decoder was written for.

    Raised rather than worked around. Every check that produces this is a
    check whose failure would otherwise yield numbers rather than an error.
    """


# ``array('h')`` is a C short, which the language only guarantees to be *at
# least* two bytes. Every platform this runs on makes it exactly two, but a
# silent mismatch would shift every 16-bit sample, so it is checked once here
# rather than assumed everywhere below.
if array("h").itemsize != 2:  # pragma: no cover - not reachable on supported platforms
    raise WmedfError(
        f"this platform's array('h') is {array('h').itemsize} bytes, not 2; "
        "the 16-bit decoding path would be wrong here"
    )


#: Records read per chunk. A session is a few MB, but the design must not
#: assume the whole card fits in memory.
DEFAULT_RECORDS_PER_CHUNK = 600

_FIXED_HEADER_BYTES = 256
_SIGNAL_HEADER_BYTES = 256

_WIDTH_BY_MARKER = {"#1": 1, "#2": 2}

#: What marks a file as this WM variant rather than standard EDF. Both are
#: checked and neither is corrected: a file that fails them is not the format
#: this decoder was written against, and reading it anyway is how a different
#: layout gets decoded as if it were this one.
EXPECTED_VERSION = "1"
EXPECTED_RESERVED_SUFFIX = "#s"

# The device writes fixed-width dd.mm.yy and hh.mm.ss. Accepting a shortened
# form such as "1.1.20" would mean accepting a writer we have never seen, and
# the field widths are what make the header self-describing.
_TWO_DIGIT_TRIPLE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{2})$")

_INT8_RANGE = (-128, 127)
_UINT8_RANGE = (0, 255)
_INT16_RANGE = (-32768, 32767)


class DiagnosticPolicy(enum.Enum):
    """What to do when a digital sample falls outside its declared range.

    An out-of-range value is the signature of a misaligned decode, so the
    default raises. But a device may also emit sentinel or invalid markers —
    the channels that are only populated when an oximeter is attached are the
    obvious candidates, and their encoding is not yet established — so the
    exploration pass needs to collect violations rather than stop at the first.

    The collecting member is verbosely named on purpose: it must not slip into
    a production path unnoticed.
    """

    STRICT = "strict"
    COLLECT_VIOLATIONS = "collect_violations"


@dataclass(frozen=True)
class RangeViolation:
    """One digital sample outside the range its own header declares."""

    signal_index: int
    label: str
    record_index: int
    sample_index: int
    value: int
    digital_min: int
    digital_max: int

    def __str__(self) -> str:
        return (
            f"signal {self.signal_index} ({self.label!r}) record {self.record_index} "
            f"sample {self.sample_index}: {self.value} outside "
            f"[{self.digital_min}, {self.digital_max}]"
        )


DEFAULT_VIOLATION_CAP = 1000


class ViolationCollector:
    """Gathers range violations during an exploration pass, with a cap.

    A cap is necessary rather than tidy: a misaligned channel puts *every*
    sample out of range, so an uncapped collector would build one object per
    sample — millions of them across a card — and turn a diagnostic into an
    out-of-memory failure.

    The cap must never make the survey look cleaner than it is, so the total
    is counted regardless of whether the detail was kept, and
    :attr:`truncated` says plainly that detail was dropped.
    """

    def __init__(self, cap: int = DEFAULT_VIOLATION_CAP) -> None:
        if cap <= 0:
            raise WmedfError("violation cap must be positive")
        self.cap = cap
        self.violations: list[RangeViolation] = []
        self.total_seen = 0

    def record(self, violation: RangeViolation) -> None:
        self.total_seen += 1
        if len(self.violations) < self.cap:
            self.violations.append(violation)

    @property
    def truncated(self) -> bool:
        return self.total_seen > len(self.violations)

    def __len__(self) -> int:
        return self.total_seen

    def summary(self) -> str:
        if self.total_seen == 0:
            return "no digital samples outside their declared range"
        text = f"{self.total_seen} sample(s) outside their declared range"
        if self.truncated:
            text += f"; showing the first {len(self.violations)}"
        return text


@dataclass(frozen=True)
class Signal:
    """One channel, as the file's own header describes it."""

    index: int
    label: str
    transducer: str
    unit: str
    physical_min: float
    physical_max: float
    digital_min: int
    digital_max: int
    prefiltering: str
    samples_per_record: int
    width_bytes: int
    reserved: str

    @property
    def signed(self) -> bool:
        """Whether samples are signed, which follows the declared minimum."""
        return self.digital_min < 0

    @property
    def scale(self) -> float:
        """Gain of the affine transform to physical units.

        Applied only in the scaling step; kept here because it is a property
        of the signal header. **This is the gain alone** — it is not the whole
        conversion. See :func:`to_physical`.
        """
        return (self.physical_max - self.physical_min) / (
            self.digital_max - self.digital_min
        )

    def to_physical_value(self, digital: int) -> float:
        """Convert one digital sample to physical units.

        The transform is **affine, not a bare multiplication**::

            physical = physical_min + (digital − digital_min) × scale

        On this device every channel seen so far has ``physical_min`` and
        ``digital_min`` either both zero or in the same proportion, so the
        offset term happens to vanish and ``digital × scale`` would give the
        same answer. That is a coincidence of one firmware's channel set, not
        a property of the format, and a channel with an offset range would be
        silently wrong under the shorter formula.
        """
        return self.physical_min + (digital - self.digital_min) * self.scale

    def sampling_rate_hz(self, record_duration_s: float) -> float:
        return self.samples_per_record / record_duration_s


@dataclass(frozen=True)
class WmedfHeader:
    """The complete header, validated."""

    version: str
    patient_id: str
    recording_id: str
    start: DeviceLocalTime
    header_bytes: int
    reserved: str
    n_records: int
    record_duration_s: float
    signals: tuple[Signal, ...]
    record_size_bytes: int
    signal_offsets: tuple[int, ...] = field(repr=False)

    @property
    def n_signals(self) -> int:
        return len(self.signals)

    def signal_by_label(self, label: str) -> Signal:
        """Look up one signal by label, raising if the label is not unique.

        Labels are a convenience. Identity is the index: a file may repeat a
        label, or carry two that differ only by padding, and silently picking
        one of them is how the wrong channel ends up in a chart.
        """
        matches = [s for s in self.signals if s.label == label]
        if not matches:
            raise WmedfError(f"no signal labelled {label!r}")
        if len(matches) > 1:
            raise WmedfError(
                f"label {label!r} is not unique (signals "
                f"{[s.index for s in matches]}) — address it by index"
            )
        return matches[0]


@dataclass(frozen=True)
class SignalChunk:
    """A run of consecutive samples for one signal.

    ``physical`` stays ``None`` until the scaling step fills it in, so an
    unscaled chunk cannot be mistaken for a scaled one.
    """

    signal: Signal
    start_record: int
    start_sample: int
    digital: array
    physical: array | None = None

    def __len__(self) -> int:
        return len(self.digital)


def read_header(stream: BinaryIO) -> WmedfHeader:
    """Read and validate the header from the current position of *stream*.

    The stream is left positioned at the first data record.
    """
    fixed = _read_exactly(stream, _FIXED_HEADER_BYTES, "fixed header")

    version = _text(fixed, 0, 8, "version")
    patient_id = _text(fixed, 8, 80, "patient id")
    recording_id = _text(fixed, 88, 80, "recording id")
    start_date_text = _text(fixed, 168, 8, "start date")
    start_time_text = _text(fixed, 176, 8, "start time")
    header_bytes = _integer(_text(fixed, 184, 8, "header byte count"), "header byte count")
    reserved = _text(fixed, 192, 44, "reserved")
    n_records = _integer(_text(fixed, 236, 8, "data record count"), "data record count")
    record_duration_s = _number(
        _text(fixed, 244, 8, "record duration"), "record duration"
    )
    n_signals = _integer(_text(fixed, 252, 4, "signal count"), "signal count")

    if version != EXPECTED_VERSION:
        raise WmedfError(
            f"version field is {version!r}, expected {EXPECTED_VERSION!r}. Standard "
            "EDF writes '0' and stores every channel as 16-bit; this decoder reads "
            "the WM variant and will not guess which it has been handed."
        )
    if not reserved.endswith(EXPECTED_RESERVED_SUFFIX):
        raise WmedfError(
            f"reserved field {reserved!r} does not end with "
            f"{EXPECTED_RESERVED_SUFFIX!r}, which marks this WM variant"
        )

    if n_signals <= 0:
        raise WmedfError(f"signal count must be positive, got {n_signals}")
    if record_duration_s <= 0:
        raise WmedfError(f"record duration must be positive, got {record_duration_s}")
    if n_records < 0:
        # Some EDF writers use -1 for "unknown, still recording". This decoder
        # reads finished archives, so an unknown count means the file is not
        # what is expected rather than something to work around.
        #
        # Zero **is** accepted, deliberately: a session that was started and
        # stopped without a full second of data is a real thing a device can
        # write, and it is not malformed. It simply yields no chunks, and
        # verify_against_size confirms the file carries no data region. The
        # distinction matters because rejecting it would turn an unremarkable
        # short session into a decoding failure.
        raise WmedfError(
            f"data record count is {n_records}; this decoder reads completed "
            "recordings, which declare a real count"
        )

    expected_header = _FIXED_HEADER_BYTES + n_signals * _SIGNAL_HEADER_BYTES
    if header_bytes != expected_header:
        raise WmedfError(
            f"header declares {header_bytes} bytes but {n_signals} signals imply "
            f"{expected_header} — the two disagree, so one of them is misread"
        )

    signal_block = _read_exactly(
        stream, n_signals * _SIGNAL_HEADER_BYTES, "signal header"
    )
    signals = _read_signals(signal_block, n_signals)

    record_size_bytes = sum(s.samples_per_record * s.width_bytes for s in signals)
    if record_size_bytes <= 0:
        raise WmedfError("computed record size is zero")

    offsets = []
    running = 0
    for s in signals:
        offsets.append(running)
        running += s.samples_per_record * s.width_bytes

    start = _parse_start(start_date_text, start_time_text)

    return WmedfHeader(
        version=version,
        patient_id=patient_id,
        recording_id=recording_id,
        start=start,
        header_bytes=header_bytes,
        reserved=reserved,
        n_records=n_records,
        record_duration_s=record_duration_s,
        signals=signals,
        record_size_bytes=record_size_bytes,
        signal_offsets=tuple(offsets),
    )


def verify_against_size(header: WmedfHeader, file_size: int) -> None:
    """Check the header's arithmetic against the actual file size.

    Separate from :func:`read_header` because a stream does not always know
    its own length — inside a ZIP the size comes from the archive entry.
    """
    data_bytes = file_size - header.header_bytes
    if data_bytes < 0:
        raise WmedfError(
            f"file is {file_size} bytes, shorter than its own {header.header_bytes}-byte header"
        )
    if data_bytes % header.record_size_bytes != 0:
        raise WmedfError(
            f"data region of {data_bytes} bytes is not a whole number of "
            f"{header.record_size_bytes}-byte records — the record layout is misread"
        )
    actual_records = data_bytes // header.record_size_bytes
    if actual_records != header.n_records:
        raise WmedfError(
            f"header declares {header.n_records} records but the file holds "
            f"{actual_records}"
        )


def iter_digital_chunks(
    stream: BinaryIO,
    header: WmedfHeader,
    *,
    records_per_chunk: int = DEFAULT_RECORDS_PER_CHUNK,
    policy: DiagnosticPolicy = DiagnosticPolicy.STRICT,
    violations: ViolationCollector | None = None,
) -> Iterator[SignalChunk]:
    """Yield raw digital samples, one chunk per signal per block of records.

    *stream* must be positioned at the first data record, as
    :func:`read_header` leaves it. Reading is strictly forward.

    ``physical`` is ``None`` on every chunk yielded here; scaling is a separate
    step.
    """
    if records_per_chunk <= 0:
        raise WmedfError("records_per_chunk must be positive")
    if policy is DiagnosticPolicy.COLLECT_VIOLATIONS and violations is None:
        raise WmedfError(
            "COLLECT_VIOLATIONS requires a ViolationCollector — otherwise the "
            "violations would be found and discarded"
        )

    record_index = 0
    while record_index < header.n_records:
        block_records = min(records_per_chunk, header.n_records - record_index)
        wanted = block_records * header.record_size_bytes
        block = _read_exactly(stream, wanted, "data record", allow_short=True)
        if len(block) != wanted:
            raise WmedfError(
                f"file ended after {record_index} records, short of the "
                f"{header.n_records} its header declares"
            )

        for signal, offset in zip(header.signals, header.signal_offsets, strict=True):
            samples = _extract_signal(
                block, block_records, header.record_size_bytes, signal, offset
            )
            _check_range(samples, signal, record_index, policy, violations)
            yield SignalChunk(
                signal=signal,
                start_record=record_index,
                start_sample=record_index * signal.samples_per_record,
                digital=samples,
                physical=None,
            )

        record_index += block_records


def to_physical(chunk: SignalChunk) -> SignalChunk:
    """Return *chunk* with its physical values filled in.

    Always computed from ``digital``, so calling this twice is harmless rather
    than compounding. Kept separate from decoding on purpose: when a value
    looks wrong, having both representations says whether the bytes or the
    conversion is at fault.
    """
    signal = chunk.signal
    offset = signal.physical_min
    gain = signal.scale
    origin = signal.digital_min
    physical = array("d", (offset + (v - origin) * gain for v in chunk.digital))
    return SignalChunk(
        signal=signal,
        start_record=chunk.start_record,
        start_sample=chunk.start_sample,
        digital=chunk.digital,
        physical=physical,
    )


def iter_chunks(
    stream: BinaryIO,
    header: WmedfHeader,
    *,
    records_per_chunk: int = DEFAULT_RECORDS_PER_CHUNK,
    policy: DiagnosticPolicy = DiagnosticPolicy.STRICT,
    violations: ViolationCollector | None = None,
) -> Iterator[SignalChunk]:
    """Decode and scale in one pass — the two steps composed, nothing more."""
    for chunk in iter_digital_chunks(
        stream,
        header,
        records_per_chunk=records_per_chunk,
        policy=policy,
        violations=violations,
    ):
        yield to_physical(chunk)


def read_all(
    stream: BinaryIO,
    header: WmedfHeader,
    *,
    policy: DiagnosticPolicy = DiagnosticPolicy.STRICT,
    violations: ViolationCollector | None = None,
) -> dict[int, SignalChunk]:
    """Read a whole session into memory, one chunk per signal, keyed by index.

    **This materialises everything**: for a full session that is on the order
    of half a million samples held twice, as integers and as floats. That is
    fine for one session and wrong for a whole card, which is why the chunked
    iterators are the primary interface and this is the convenience.
    """
    digital: dict[int, array] = {}
    for chunk in iter_digital_chunks(
        stream,
        header,
        records_per_chunk=DEFAULT_RECORDS_PER_CHUNK,
        policy=policy,
        violations=violations,
    ):
        existing = digital.get(chunk.signal.index)
        if existing is None:
            digital[chunk.signal.index] = chunk.digital
        else:
            existing.extend(chunk.digital)

    return {
        signal.index: to_physical(
            SignalChunk(
                signal=signal,
                start_record=0,
                start_sample=0,
                digital=digital.get(signal.index, array("b")),
                physical=None,
            )
        )
        for signal in header.signals
    }


def _sample_offset(
    record_duration_s: float, samples_per_record: int, absolute_sample_index: int
) -> timedelta:
    """Offset of one sample from the start of the recording.

    Derived **directly from the indices**, never by accumulating an interval.
    Adding a sample period repeatedly is what drifts: at 10 Hz over nine hours
    that is over three hundred thousand additions, and the resulting error is
    invisible in the output because every individual step looks right.

    ``Fraction`` is used so the division inside a record introduces no rounding
    of its own. It does **not** recover the decimal the header spelled —
    ``record_duration_s`` has already been through ``float``, so what is exact
    here is the arithmetic on that value, not the value itself. On this device
    the duration is one second, which a float represents exactly; the reason
    this matters is drift, not representation.
    """
    record_index, within = divmod(absolute_sample_index, samples_per_record)
    duration = Fraction(record_duration_s)
    offset = record_index * duration + Fraction(within) * duration / samples_per_record
    return timedelta(microseconds=round(offset * 1_000_000))


def sample_time(
    header: WmedfHeader, signal: Signal, absolute_sample_index: int
) -> DeviceLocalTime:
    """The instant of one sample that the session actually contains.

    Bounds are enforced rather than assumed. Returning a time for an index
    past the end would hide an off-by-one behind a perfectly plausible
    timestamp, and the caller would have no way to tell.

    The signal must belong to *header*. Passing one from another session is
    checked by comparing metadata, not merely the index: a different session's
    signal sitting at the same index may carry a different sampling rate, and
    the time computed from it would be wrong while looking ordinary.

    The time axis is never stored as an array; it is derived on demand.
    """
    if signal.index >= len(header.signals) or header.signals[signal.index] != signal:
        raise WmedfError(
            f"signal {signal.index} ({signal.label!r}) does not belong to this "
            "header. A signal from another session may declare a different "
            "sampling rate, and the resulting time would look ordinary."
        )

    # Type before value. bool is a subclass of int, so True would otherwise
    # sail through as index 1; a float would silently name half a sample; a
    # string or None would raise something other than this module's error and
    # so escape a caller catching WmedfError.
    if isinstance(absolute_sample_index, bool) or not isinstance(
        absolute_sample_index, int
    ):
        raise WmedfError(
            f"sample index must be an int, got "
            f"{type(absolute_sample_index).__name__} ({absolute_sample_index!r})"
        )

    if absolute_sample_index < 0:
        raise WmedfError(f"sample index must not be negative, got {absolute_sample_index}")

    total_samples = header.n_records * signal.samples_per_record
    if absolute_sample_index >= total_samples:
        raise WmedfError(
            f"sample index {absolute_sample_index} is past the end of this session, "
            f"which holds {total_samples} sample(s) of signal {signal.index} "
            f"({signal.label!r})"
        )

    return header.start.shift(
        _sample_offset(
            header.record_duration_s, signal.samples_per_record, absolute_sample_index
        )
    )


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _read_signals(block: bytes, n: int) -> tuple[Signal, ...]:
    def column(start: int, width: int) -> list[str]:
        base = start * n
        return [
            _text(block, base + i * width, width, f"signal field at {start}")
            for i in range(n)
        ]

    labels = column(0, 16)
    transducers = column(16, 80)
    units = column(96, 8)
    phys_min = column(104, 8)
    phys_max = column(112, 8)
    dig_min = column(120, 8)
    dig_max = column(128, 8)
    prefilter = column(136, 80)
    samples = column(216, 8)
    reserved = column(224, 32)

    signals: list[Signal] = []
    for i in range(n):
        marker = reserved[i].strip()
        width = _WIDTH_BY_MARKER.get(marker)
        if width is None:
            hint = (
                " An empty marker is what a standard EDF file has: those store "
                "every channel as 16-bit and are not this format."
                if marker == ""
                else ""
            )
            raise WmedfError(
                f"signal {i} ({labels[i]!r}) declares reserved marker {marker!r}; "
                f"only {sorted(_WIDTH_BY_MARKER)} are known. Guessing a width here "
                f"would misalign every later channel in the record.{hint}"
            )

        d_min = _integer(dig_min[i], f"signal {i} digital minimum")
        d_max = _integer(dig_max[i], f"signal {i} digital maximum")
        p_min = _number(phys_min[i], f"signal {i} physical minimum")
        p_max = _number(phys_max[i], f"signal {i} physical maximum")
        n_samples = _integer(samples[i], f"signal {i} samples per record")

        if d_max <= d_min:
            raise WmedfError(
                f"signal {i} ({labels[i]!r}) has digital range [{d_min}, {d_max}]"
            )
        if p_max <= p_min:
            raise WmedfError(
                f"signal {i} ({labels[i]!r}) has physical range [{p_min}, {p_max}]"
            )
        if n_samples <= 0:
            raise WmedfError(
                f"signal {i} ({labels[i]!r}) declares {n_samples} samples per record"
            )

        _check_marker_against_range(i, labels[i], marker, width, d_min, d_max)

        signals.append(
            Signal(
                index=i,
                label=labels[i],
                transducer=transducers[i],
                unit=units[i],
                physical_min=p_min,
                physical_max=p_max,
                digital_min=d_min,
                digital_max=d_max,
                prefiltering=prefilter[i],
                samples_per_record=n_samples,
                width_bytes=width,
                reserved=marker,
            )
        )
    return tuple(signals)


def _check_marker_against_range(
    index: int, label: str, marker: str, width: int, d_min: int, d_max: int
) -> None:
    """Cross-check the declared width against the declared range.

    The two are independent statements in the header. If they disagree, one is
    wrong, and continuing would decode the wrong number of bytes per sample.
    """
    if width == 1:
        lo, hi = (_INT8_RANGE if d_min < 0 else _UINT8_RANGE)
        kind = "int8" if d_min < 0 else "uint8"
    else:
        lo, hi = _INT16_RANGE
        kind = "int16"
    if d_min < lo or d_max > hi:
        raise WmedfError(
            f"signal {index} ({label!r}) declares width {marker} but a digital "
            f"range of [{d_min}, {d_max}], which does not fit {kind}"
        )


def _extract_signal(
    block: bytes, n_records: int, record_size: int, signal: Signal, offset: int
) -> array:
    """Gather one signal's samples out of a block of interleaved records."""
    span = signal.samples_per_record * signal.width_bytes

    if signal.width_bytes == 1:
        out = array("b" if signal.signed else "B")
    else:
        out = array("h")

    for r in range(n_records):
        start = r * record_size + offset
        out.frombytes(block[start : start + span])

    # array() uses the machine's byte order; the file is little-endian.
    if signal.width_bytes == 2 and sys.byteorder == "big":
        out.byteswap()

    return out


def _check_range(
    samples: Sequence[int],
    signal: Signal,
    first_record: int,
    policy: DiagnosticPolicy,
    violations: ViolationCollector | None,
) -> None:
    lo, hi = signal.digital_min, signal.digital_max
    if not samples:
        return
    # min/max run in C over the whole array. The per-sample loop below only
    # runs when something is actually wrong, which keeps the common case out
    # of the interpreter.
    if lo <= min(samples) and max(samples) <= hi:
        return
    for i, value in enumerate(samples):
        if lo <= value <= hi:
            continue
        violation = RangeViolation(
            signal_index=signal.index,
            label=signal.label,
            record_index=first_record + i // signal.samples_per_record,
            sample_index=i % signal.samples_per_record,
            value=value,
            digital_min=lo,
            digital_max=hi,
        )
        if policy is DiagnosticPolicy.STRICT:
            raise WmedfError(
                f"digital sample outside its declared range: {violation}. This is "
                "usually a misaligned decode. If this device emits sentinel values, "
                "establish that first — do not widen the range to make it pass."
            )
        violations.record(violation)  # type: ignore[union-attr]


def _read_exactly(
    stream: BinaryIO, count: int, what: str, *, allow_short: bool = False
) -> bytes:
    """Read exactly *count* bytes, looping until satisfied or the stream ends.

    A single ``read(n)`` is not enough: the io contract permits a stream to
    return fewer bytes than asked for without being at its end, and the
    archive members this decoder reads are streams. Treating a short read as
    end-of-file would truncate a session silently.

    With *allow_short*, a genuine end of stream returns what was available and
    lets the caller phrase the error in its own terms.
    """
    parts: list[bytes] = []
    remaining = count
    while remaining > 0:
        piece = stream.read(remaining)
        if not piece:
            break
        parts.append(piece)
        remaining -= len(piece)
    data = b"".join(parts)
    if len(data) != count and not allow_short:
        raise WmedfError(
            f"file ended inside the {what}: wanted {count} bytes, got {len(data)}"
        )
    return data


def _text(block: bytes, start: int, width: int, what: str) -> str:
    raw = block[start : start + width]
    try:
        decoded = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise WmedfError(f"{what} is not ASCII, so this is not an EDF header") from exc
    # EDF pads its fixed-width fields with spaces. Trimming that padding is
    # reading the format; anything further would be normalising the content.
    return decoded.strip()


def _integer(text: str, what: str) -> int:
    try:
        return int(text)
    except ValueError as exc:
        raise WmedfError(f"{what} is not an integer: {text!r}") from exc


def _number(text: str, what: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise WmedfError(f"{what} is not a number: {text!r}") from exc
    if not math.isfinite(value):
        # float() happily accepts 'nan', 'inf' and '-inf'. A NaN then passes
        # every range comparison silently, because comparisons against NaN are
        # all false — so an inverted or absent range would look valid and the
        # scale computed from it would be NaN for every sample.
        raise WmedfError(f"{what} is not a finite number: {text!r}")
    return value


def _parse_start(date_text: str, time_text: str) -> DeviceLocalTime:
    """Build the recording start from the EDF date and time fields.

    EDF writes ``dd.mm.yy``. The two-digit year is resolved by the EDF
    convention: 85–99 mean 1985–1999, 00–84 mean 2000–2084.
    """
    date_match = _TWO_DIGIT_TRIPLE_RE.match(date_text)
    time_match = _TWO_DIGIT_TRIPLE_RE.match(time_text)
    if date_match is None or time_match is None:
        raise WmedfError(
            f"start date/time {date_text!r} {time_text!r} is not exactly "
            "dd.mm.yy hh.mm.ss"
        )

    day, month, year = (int(p) for p in date_match.groups())
    hour, minute, second = (int(p) for p in time_match.groups())

    full_year = 1900 + year if year >= 85 else 2000 + year
    try:
        return DeviceLocalTime(datetime(full_year, month, day, hour, minute, second))
    except ValueError as exc:
        raise WmedfError(
            f"start date/time {date_text!r} {time_text!r} is not a real instant"
        ) from exc
