"""Builders for synthetic test fixtures.

Fixtures are constructed here at test runtime and never committed. Nothing in
this file is derived from a real recording: the waveforms are arithmetic, the
dates are arbitrary, and no device identifier appears anywhere.

The builder writes byte-exact ``.wmedf`` files so expected values are known
precisely rather than approximately, and it can be told to write malformed
headers, because the decoder's refusal to accept them is the behaviour most
worth testing.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

FIXED_HEADER_BYTES = 256
SIGNAL_HEADER_BYTES = 256


@dataclass
class SyntheticSignal:
    """One channel to write, with its digital sample values."""

    label: str
    unit: str
    physical_min: float
    physical_max: float
    digital_min: int
    digital_max: int
    samples_per_record: int
    width_bytes: int
    #: Digital values, length must be n_records * samples_per_record.
    data: list[int] = field(default_factory=list)
    #: Overrides the reserved marker, for testing rejection of unknown widths.
    marker_override: str | None = None
    transducer: str = ""
    prefiltering: str = ""

    @property
    def marker(self) -> str:
        if self.marker_override is not None:
            return self.marker_override
        return {1: "#1", 2: "#2"}[self.width_bytes]


def ramp(n: int, low: int, high: int) -> list[int]:
    """A deterministic saw-tooth across the whole digital range.

    Uses the extremes so an off-by-one in width or signedness shows up as a
    wrong value rather than as a plausible one.
    """
    if n <= 0:
        return []
    if n == 1:
        return [low]
    span = high - low
    return [low + (i * span) // (n - 1) for i in range(n)]


def build_wmedf(
    signals: list[SyntheticSignal],
    n_records: int,
    *,
    start: datetime = datetime(2020, 3, 4, 12, 0, 0),
    version: str = "1",
    record_duration: str = "1",
    reserved: str = " " * 42 + "#s",
    declared_header_bytes: int | None = None,
    declared_n_records: int | None = None,
    declared_n_signals: int | None = None,
    patient_id: str = "Unknown patient",
    recording_id: str = "Unknown record",
    start_date_text: str | None = None,
    start_time_text: str | None = None,
) -> bytes:
    """Assemble a ``.wmedf`` file.

    The ``declared_*`` overrides write a header field that disagrees with the
    actual content, which is how the validation paths are exercised.
    """
    n = len(signals)
    header_bytes = (
        declared_header_bytes
        if declared_header_bytes is not None
        else FIXED_HEADER_BYTES + n * SIGNAL_HEADER_BYTES
    )

    out = bytearray()
    out += _pad(version, 8)
    out += _pad(patient_id, 80)
    out += _pad(recording_id, 80)
    out += _pad(
        start_date_text if start_date_text is not None else start.strftime("%d.%m.%y"), 8
    )
    out += _pad(
        start_time_text if start_time_text is not None else start.strftime("%H.%M.%S"), 8
    )
    out += _pad(str(header_bytes), 8)
    out += _pad(reserved, 44)
    out += _pad(str(declared_n_records if declared_n_records is not None else n_records), 8)
    out += _pad(record_duration, 8)
    out += _pad(str(declared_n_signals if declared_n_signals is not None else n), 4)

    out += b"".join(_pad(s.label, 16) for s in signals)
    out += b"".join(_pad(s.transducer, 80) for s in signals)
    out += b"".join(_pad(s.unit, 8) for s in signals)
    out += b"".join(_pad(_num(s.physical_min), 8) for s in signals)
    out += b"".join(_pad(_num(s.physical_max), 8) for s in signals)
    out += b"".join(_pad(str(s.digital_min), 8) for s in signals)
    out += b"".join(_pad(str(s.digital_max), 8) for s in signals)
    out += b"".join(_pad(s.prefiltering, 80) for s in signals)
    out += b"".join(_pad(str(s.samples_per_record), 8) for s in signals)
    out += b"".join(_pad(s.marker, 32) for s in signals)

    for record in range(n_records):
        for signal in signals:
            lo = record * signal.samples_per_record
            hi = lo + signal.samples_per_record
            values = signal.data[lo:hi]
            if len(values) != signal.samples_per_record:
                raise ValueError(
                    f"signal {signal.label!r} has {len(signal.data)} samples, "
                    f"needs {n_records * signal.samples_per_record}"
                )
            out += _pack(values, signal)

    return bytes(out)


def three_channel_file(n_records: int = 4) -> tuple[bytes, list[SyntheticSignal]]:
    """A file mixing widths, rates and signedness.

    This is the shape that catches a decoder computing sample offsets as
    ``index * 2``: the 8-bit channel in the middle shifts every byte after it.
    A non-zero ``physical_min`` on one channel means the affine offset cannot
    be dropped without a test failing.
    """
    signals = [
        SyntheticSignal(
            label="Wide Fast",
            unit="hPa",
            physical_min=0.0,
            physical_max=65.0,
            digital_min=0,
            digital_max=650,
            samples_per_record=10,
            width_bytes=2,
        ),
        SyntheticSignal(
            label="Narrow Unsigned",
            unit="%",
            physical_min=0.0,
            physical_max=100.0,
            digital_min=0,
            digital_max=255,
            samples_per_record=1,
            width_bytes=1,
        ),
        SyntheticSignal(
            label="Narrow Signed",
            unit="ms",
            # Non-zero physical minimum: exercises the offset term.
            physical_min=-100.0,
            physical_max=100.0,
            digital_min=-100,
            digital_max=100,
            samples_per_record=2,
            width_bytes=1,
        ),
    ]
    for signal in signals:
        signal.data = ramp(
            n_records * signal.samples_per_record, signal.digital_min, signal.digital_max
        )
    return build_wmedf(signals, n_records), signals


def session_wmedf(start: datetime, n_records: int) -> bytes:
    """A small but valid signal file starting at *start*."""
    signals = [
        SyntheticSignal(
            label="Pressure",
            unit="hPa",
            physical_min=0.0,
            physical_max=65.0,
            digital_min=0,
            digital_max=650,
            samples_per_record=10,
            width_bytes=2,
        ),
        SyntheticSignal(
            label="Phase",
            unit="",
            physical_min=0.0,
            physical_max=1.0,
            digital_min=0,
            digital_max=1,
            samples_per_record=1,
            width_bytes=1,
        ),
    ]
    for signal in signals:
        signal.data = ramp(
            n_records * signal.samples_per_record, signal.digital_min, signal.digital_max
        )
    return build_wmedf(signals, n_records, start=start)


def _offset_text(delta: timedelta) -> str:
    total_ms = round(abs(delta).total_seconds() * 1000)
    sign = "-" if delta < timedelta(0) else "+"
    ms = total_ms % 1000
    s = total_ms // 1000
    return f"{sign}{s // 3600:04d}:{(s // 60) % 60:02d}:{s % 60:02d}.{ms:03d}"


def session_event_xml(start: datetime, seconds: int) -> bytes:
    """An event file written the way the device writes them.

    The start offset counts from midnight of the day the session *began*; the
    stop offset counts from midnight of the day it **ended**. For a session
    that does not cross midnight those are the same reference, so a fixture
    has to be built deliberately across midnight for the difference to show
    at all.
    """
    stop_at = start + timedelta(seconds=seconds)
    begin = _offset_text(start - datetime(start.year, start.month, start.day))
    end = _offset_text(stop_at - datetime(stop_at.year, stop_at.month, stop_at.day))
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<desc><start time="{begin}"/>'
        f'<RespEvent time="1" id="6" event="begin" Strength="1"/>'
        f'<RespEvent time="0" id="6" event="begin" Strength="0"/>'
        f'<stop time="{end}"/></desc>'
    ).encode("utf-8")


def build_day_archive(
    path,
    *,
    archive_date: date,
    day_number: int = 123,
    sessions: list[tuple[int, datetime, int]] | None = None,
    day_members: bool = True,
    extra_members: dict[str, bytes] | None = None,
    omit: set[str] | None = None,
):
    """Write a synthetic ``NNNN_YYYY-MM-DD.zip`` and return its path.

    *sessions* is a list of ``(number, start, duration_seconds)``, or
    ``(number, start, record_seconds, xml_seconds)`` when the two must differ.

    Signal and event files are generated consistently from one duration by
    default, so a test that wants them to disagree has to say so explicitly —
    which the four-element form is for. The signal file's records and the
    session XML's start-to-stop span are genuinely separate quantities on the
    device, and a check that claims to use one must not silently use the other.
    """
    import zipfile

    sessions = sessions if sessions is not None else [(1, datetime(2020, 3, 4, 22, 0, 0), 60)]
    omit = omit or set()
    path.mkdir(parents=True, exist_ok=True)
    target = path / f"{day_number:04d}_{archive_date.isoformat()}.zip"

    with zipfile.ZipFile(target, "w") as zf:
        for entry in sessions:
            number, start, record_seconds = entry[0], entry[1], entry[2]
            xml_seconds = entry[3] if len(entry) > 3 else record_seconds
            signal_name = f"{number:04d}.wmedf"
            event_name = f"event_{number:04d}.xml"
            if signal_name not in omit:
                zf.writestr(signal_name, session_wmedf(start, record_seconds))
            if event_name not in omit:
                zf.writestr(event_name, session_event_xml(start, xml_seconds))

        if day_members:
            zf.writestr("event.xml", _day_log_xml(archive_date))
            zf.writestr("alarm.xml", _alarm_log_xml(archive_date))
            zf.writestr("parameter.xml", _parameter_log_xml(archive_date))
            zf.writestr("parametersmap.xml", _parameter_map_xml())

        for name, payload in (extra_members or {}).items():
            zf.writestr(name, payload)

    return target


def _day_log_xml(archive_date: date) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<log version="1" date="{archive_date.isoformat()}">'
        '<start time="+0012:00:00.000"/>'
        '<event time="+0013:00:00.000" id="55" status="true"/>'
        '<stop time="+0036:00:00.000"/></log>'
    ).encode("utf-8")


def _alarm_log_xml(archive_date: date) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<log version="1" date="{archive_date.isoformat()}">'
        '<start time="+0012:00:00.000"/>'
        '<alarm time="+0014:00:00.000" id="794" event="begin"/>'
        '<stop time="+0036:00:00.000"/></log>'
    ).encode("utf-8")


def _parameter_log_xml(archive_date: date) -> bytes:
    rows = "".join(
        f'<parameter time="+0012:00:00.000" id="{i}" value="{v}"/>'
        for i, v in [(1, "0"), (2, "1"), (69, "a1"), (37, "b1"), (69, "a2"), (37, "b2")]
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<log version="1" date="{archive_date.isoformat()}">'
        f'<config version="6.3.0"/>{rows}</log>'
    ).encode("utf-8")


#: The ids and names the device's own map uses for every parameter the decoder
#: publishes something about — a scale, a scale candidate, or a value domain.
#:
#: **These have to be the real ones.** The registries are emitted only when the
#: map's ids carry the expected names, so a fixture with invented ids would
#: leave all of them empty in every test — and, worse, in the export the next
#: release's contract is built from. The entry shape would then be absent from
#: that contract and unprotected, which is exactly what the contract exists to
#: prevent. The *values* below stay synthetic; only the identifiers are real,
#: and an id-to-name mapping is not device data.
#:
#: Note 105 beside 106: the expiratory trigger is a percentage and carries a
#: scale candidate, the inspiratory one is a step and carries a value domain.
#: The device's screens for them look alike, so the ids are what keeps them
#: apart.
REGISTERED_PARAMETER_IDS = (
    (37, "EPAP"),
    (39, "ExpirationRamp"),
    (41, "Frequency"),
    (69, "IPAP"),
    (74, "InspirationRamp"),
    (92, "TherapyMode"),
    (93, "Ti"),
    (94, "Ti_max"),
    (95, "Ti_min"),
    (96, "Ti_timed"),
    (105, "TriggerSensitivityExspiration"),
    (106, "TriggerSensitivityInspiration"),
    (107, "TriggerType"),
    (111, "VolumeTarget"),
    (112, "VolumeTargetControl"),
    (113, "VolumeTargetDeltaPressure"),
)


def _parameter_map_xml(
    version: str = "6.3.0", entries: tuple[tuple[int, str], ...] | None = None
) -> bytes:
    if entries is None:
        entries = ((1, "ActiveProgram"), (2, "AutoLock"), *REGISTERED_PARAMETER_IDS)
    rows = "".join(f'<parameter name="{n}" id="{i}"/>' for i, n in entries)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<parameters version="{version}" count="{len(entries)}">'
        f"{rows}</parameters>"
    ).encode("utf-8")


def _pack(values: list[int], signal: SyntheticSignal) -> bytes:
    if signal.width_bytes == 1:
        fmt = "b" if signal.digital_min < 0 else "B"
        return struct.pack(f"<{len(values)}{fmt}", *values)
    return struct.pack(f"<{len(values)}h", *values)


def _num(value: float) -> str:
    # EDF numbers are plain decimal text; keep it short enough for the field.
    text = f"{value:g}"
    return text


def _pad(text: str, width: int) -> bytes:
    raw = text.encode("ascii")
    if len(raw) > width:
        raise ValueError(f"{text!r} does not fit in {width} bytes")
    return raw.ljust(width, b" ")


def build_trend_curve(
    path,
    *,
    day: date,
    day_number: int = 123,
    populated: int = 10,
    padding: int = 5,
    device_type: str = "P32",
    header_day: str | None = None,
    body_extra: int = 0,
    header_text: str | None = None,
):
    """Write a synthetic ``.tc`` file and return its path.

    Record contents are arbitrary non-zero bytes: the layout is not decoded,
    so the fixtures do not pretend to encode anything.
    """
    name = f"{day_number:04d}_{day.isoformat()}.tc"
    path.mkdir(parents=True, exist_ok=True)
    target = path / name
    header = header_text
    if header is None:
        header = (
            '{"Type":"%s","SN":"0000000000","devid":"002","Day":"%s","Offset":"0105"}'
            % (
                device_type,
                # `or` would swallow a deliberately empty value, which is one
                # of the malformed cases worth testing.
                day.strftime("%d.%m.%Y") if header_day is None else header_day,
            )
        )
    body = bytearray()
    for i in range(populated):
        body += bytes(((i % 250) + 1,) * 9)
    body += bytes(9 * padding)
    body += bytes(body_extra)
    target.write_bytes(header.encode("ascii") + bytes(body))
    return target


# ---------------------------------------------------------------------------
# statistic.proto
# ---------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    """Encode a non-negative integer the way protobuf does."""
    if value < 0:
        raise ValueError("varint values are non-negative")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def pb_varint_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def pb_bytes_field(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def build_usage_record(
    *,
    timestamp: int,
    duration: int,
    second: int = 0,
    by_program: list[int] | None = None,
    by_category: list[int] | None = None,
    second_by_program: list[int] | None = None,
    second_by_category: list[int] | None = None,
    histograms: int = 0,
) -> bytes:
    """One statistics record, consistent by default.

    Defaults put the whole duration into one program slot and one category, so
    the sum identities the reader enforces hold without the caller arranging
    it. A test that wants an inconsistent breakdown constructs it deliberately.
    """
    if by_program is None:
        by_program = [duration, 0, 0]
    if by_category is None:
        by_category = [0, 0, duration] + [0] * 8
    if second_by_program is None:
        second_by_program = [second, 0, 0]
    if second_by_category is None:
        second_by_category = [0, 0, second] + [0] * 8

    out = pb_varint_field(1, timestamp)
    out += pb_varint_field(2, duration)
    out += pb_varint_field(3, second)
    for v in by_program:
        out += pb_varint_field(4, v)
    for v in by_category:
        out += pb_varint_field(5, v)
    for v in second_by_program:
        out += pb_varint_field(6, v)
    for v in second_by_category:
        out += pb_varint_field(7, v)
    for i in range(histograms):
        block = b"".join(pb_varint_field(1, n) for n in range(i, i + 3))
        out += pb_bytes_field(8, block)
    return out


def build_statistic(
    *,
    format_version: str = "1.0.12",
    records: list[bytes] | None = None,
    configuration: dict | None = None,
    labels: tuple[str, ...] = ("2_1_1", "6.3.0"),
    therapy_total: int | None = 1000,
    other_total: int | None = 2000,
) -> bytes:
    """A synthetic ``statistic.proto``. Entirely made up; no device data."""
    import json

    out = pb_bytes_field(1, format_version.encode("ascii"))
    for record in records or []:
        out += pb_bytes_field(2, record)
    if configuration is not None:
        block = pb_bytes_field(1, json.dumps(configuration).encode("utf-8"))
        for i, label in enumerate(labels, start=2):
            block += pb_bytes_field(i, label.encode("ascii"))
        out += pb_bytes_field(3, block)
    if therapy_total is not None:
        out += pb_varint_field(4, therapy_total)
    if other_total is not None:
        out += pb_varint_field(5, other_total)
    return out


def example_configuration(programs: int = 3) -> dict:
    """A minimal named configuration in the shape the device writes."""
    return {
        "configuration": {
            "version": "0",
            "device": {"ActiveProgram": 0, "ProgramEnabled1": 1},
            "therapy": {"IPAP": [180] * programs, "EPAP": [40] * programs},
        }
    }
