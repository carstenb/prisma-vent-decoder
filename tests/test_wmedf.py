"""Tests for the ``.wmedf`` header and digital decoder.

Every fixture is generated at runtime by :mod:`synthetic`. No file in this
repository comes from a real recording.
"""

import io
from datetime import datetime

import pytest

from prisma_vent.timebase import DeviceLocalTime
from prisma_vent.wmedf import (
    DiagnosticPolicy,
    ViolationCollector,
    WmedfError,
    iter_digital_chunks,
    read_header,
    verify_against_size,
)
from synthetic import SyntheticSignal, build_wmedf, ramp, three_channel_file

# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------


def test_reads_the_fixed_header_fields():
    raw, _ = three_channel_file()
    header = read_header(io.BytesIO(raw))

    assert header.version == "1"
    assert header.n_signals == 3
    assert header.record_duration_s == 1.0
    assert header.n_records == 4
    assert header.start == DeviceLocalTime(datetime(2020, 3, 4, 12, 0, 0))
    assert header.header_bytes == 256 + 3 * 256


def test_reads_per_signal_fields():
    raw, specs = three_channel_file()
    header = read_header(io.BytesIO(raw))

    for signal, spec in zip(header.signals, specs, strict=True):
        assert signal.label == spec.label
        assert signal.unit == spec.unit
        assert signal.digital_min == spec.digital_min
        assert signal.digital_max == spec.digital_max
        assert signal.samples_per_record == spec.samples_per_record
        assert signal.width_bytes == spec.width_bytes


def test_record_size_mixes_widths():
    # 10 samples x 2 bytes + 1 x 1 + 2 x 1 = 23. A uniform-16-bit reader would
    # compute 26 and misalign from the second record onwards.
    raw, _ = three_channel_file()
    header = read_header(io.BytesIO(raw))
    assert header.record_size_bytes == 23
    assert header.signal_offsets == (0, 20, 21)


def test_signedness_follows_the_declared_minimum():
    raw, _ = three_channel_file()
    header = read_header(io.BytesIO(raw))
    assert header.signals[1].signed is False  # 0…255
    assert header.signals[2].signed is True  # −100…100


def test_edf_two_digit_year_convention():
    raw = build_wmedf([_simple_signal()], 1, start=datetime(1999, 12, 31, 23, 59, 58))
    assert read_header(io.BytesIO(raw)).start.value.year == 1999

    raw = build_wmedf([_simple_signal()], 1, start=datetime(2026, 7, 23, 15, 17, 46))
    assert read_header(io.BytesIO(raw)).start.value.year == 2026


# --------------------------------------------------------------------------
# Digital decoding — the part that must be exactly right
# --------------------------------------------------------------------------


def test_digital_values_survive_the_round_trip():
    """The crown jewel: interleaved mixed-width records decode exactly.

    If sample offsets were computed as ``index * 2``, the 8-bit channels would
    return shifted bytes — still numbers, still in range, entirely wrong.
    """
    n_records = 4
    raw, specs = three_channel_file(n_records)
    stream = io.BytesIO(raw)
    header = read_header(stream)

    recovered: dict[int, list[int]] = {i: [] for i in range(3)}
    for chunk in iter_digital_chunks(stream, header, records_per_chunk=n_records):
        recovered[chunk.signal.index].extend(chunk.digital)

    for index, spec in enumerate(specs):
        assert recovered[index] == spec.data, f"signal {index} ({spec.label}) mismatched"


def test_chunking_does_not_change_the_result():
    n_records = 7
    raw, specs = three_channel_file(n_records)

    def read_with(chunk_size):
        stream = io.BytesIO(raw)
        header = read_header(stream)
        out: dict[int, list[int]] = {i: [] for i in range(len(specs))}
        for chunk in iter_digital_chunks(stream, header, records_per_chunk=chunk_size):
            out[chunk.signal.index].extend(chunk.digital)
        return out

    assert read_with(1) == read_with(3) == read_with(1000)


def test_chunk_positions_are_reported():
    n_records = 6
    raw, _ = three_channel_file(n_records)
    stream = io.BytesIO(raw)
    header = read_header(stream)

    starts = [
        (c.signal.index, c.start_record, c.start_sample)
        for c in iter_digital_chunks(stream, header, records_per_chunk=2)
    ]
    # Signal 0 has 10 samples per record, so its second chunk starts at 20.
    assert (0, 0, 0) in starts
    assert (0, 2, 20) in starts
    assert (0, 4, 40) in starts


def test_physical_is_none_before_scaling():
    raw, _ = three_channel_file()
    stream = io.BytesIO(raw)
    header = read_header(stream)
    for chunk in iter_digital_chunks(stream, header):
        assert chunk.physical is None


def test_eight_bit_extremes_are_not_clipped():
    signal = SyntheticSignal(
        label="Full Byte",
        unit="",
        physical_min=0.0,
        physical_max=255.0,
        digital_min=0,
        digital_max=255,
        samples_per_record=4,
        width_bytes=1,
        data=[0, 1, 254, 255],
    )
    raw = build_wmedf([signal], 1)
    stream = io.BytesIO(raw)
    header = read_header(stream)
    chunk = next(iter_digital_chunks(stream, header))
    assert list(chunk.digital) == [0, 1, 254, 255]


def test_signed_eight_bit_negatives_survive():
    signal = SyntheticSignal(
        label="Signed Byte",
        unit="ms",
        physical_min=-100.0,
        physical_max=100.0,
        digital_min=-100,
        digital_max=100,
        samples_per_record=4,
        width_bytes=1,
        data=[-100, -1, 0, 100],
    )
    raw = build_wmedf([signal], 1)
    stream = io.BytesIO(raw)
    header = read_header(stream)
    chunk = next(iter_digital_chunks(stream, header))
    assert list(chunk.digital) == [-100, -1, 0, 100]


def test_sixteen_bit_is_little_endian():
    # 0x0102 little-endian is bytes 02 01. If read big-endian this comes back
    # as 0x0201 = 513, a plausible-looking wrong number.
    signal = SyntheticSignal(
        label="Wide",
        unit="hPa",
        physical_min=0.0,
        physical_max=65.0,
        digital_min=-32000,
        digital_max=32000,
        samples_per_record=2,
        width_bytes=2,
        data=[258, -2],
    )
    raw = build_wmedf([signal], 1)
    stream = io.BytesIO(raw)
    header = read_header(stream)
    chunk = next(iter_digital_chunks(stream, header))
    assert list(chunk.digital) == [258, -2]
    assert raw[-4:] == bytes([0x02, 0x01, 0xFE, 0xFF])


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_unknown_width_marker_raises():
    signal = _simple_signal()
    signal.marker_override = "#4"
    raw = build_wmedf([signal], 1)
    with pytest.raises(WmedfError, match="reserved marker"):
        read_header(io.BytesIO(raw))


def test_empty_width_marker_raises():
    signal = _simple_signal()
    signal.marker_override = ""
    raw = build_wmedf([signal], 1)
    with pytest.raises(WmedfError, match="reserved marker"):
        read_header(io.BytesIO(raw))


def test_marker_disagreeing_with_digital_range_raises():
    # Declares one byte per sample but a range needing two.
    signal = SyntheticSignal(
        label="Impossible",
        unit="",
        physical_min=0.0,
        physical_max=1.0,
        digital_min=0,
        digital_max=300,
        samples_per_record=1,
        width_bytes=1,
        data=[0],
    )
    raw = build_wmedf([signal], 1)
    with pytest.raises(WmedfError, match="does not fit"):
        read_header(io.BytesIO(raw))


def test_header_length_disagreement_raises():
    raw = build_wmedf([_simple_signal()], 1, declared_header_bytes=999)
    with pytest.raises(WmedfError, match="disagree"):
        read_header(io.BytesIO(raw))


def test_zero_signals_raises():
    raw = build_wmedf([_simple_signal()], 1, declared_n_signals=0)
    with pytest.raises(WmedfError, match="signal count"):
        read_header(io.BytesIO(raw))


def test_zero_samples_per_record_raises():
    signal = _simple_signal()
    signal.samples_per_record = 0
    signal.data = []
    raw = build_wmedf([signal], 1)
    with pytest.raises(WmedfError, match="samples per record"):
        read_header(io.BytesIO(raw))


def test_zero_record_duration_raises():
    raw = build_wmedf([_simple_signal()], 1, record_duration="0")
    with pytest.raises(WmedfError, match="record duration"):
        read_header(io.BytesIO(raw))


def test_inverted_digital_range_raises():
    signal = _simple_signal()
    signal.digital_min, signal.digital_max = 100, 100
    raw = build_wmedf([signal], 1)
    with pytest.raises(WmedfError, match="digital range"):
        read_header(io.BytesIO(raw))


def test_inverted_physical_range_raises():
    signal = _simple_signal()
    signal.physical_min, signal.physical_max = 10.0, 1.0
    raw = build_wmedf([signal], 1)
    with pytest.raises(WmedfError, match="physical range"):
        read_header(io.BytesIO(raw))


def test_non_ascii_header_raises():
    raw = bytearray(build_wmedf([_simple_signal()], 1))
    raw[10] = 0xFF
    with pytest.raises(WmedfError, match="not ASCII"):
        read_header(io.BytesIO(bytes(raw)))


def test_truncated_header_raises():
    raw, _ = three_channel_file()
    with pytest.raises(WmedfError, match="ended inside"):
        read_header(io.BytesIO(raw[:100]))


def test_truncated_data_raises():
    raw, _ = three_channel_file(4)
    stream = io.BytesIO(raw[:-5])
    header = read_header(stream)
    with pytest.raises(WmedfError, match="file ended after"):
        list(iter_digital_chunks(stream, header))


# --------------------------------------------------------------------------
# Size verification
# --------------------------------------------------------------------------


def test_verify_against_size_accepts_a_consistent_file():
    raw, _ = three_channel_file(4)
    header = read_header(io.BytesIO(raw))
    verify_against_size(header, len(raw))


def test_verify_against_size_rejects_a_wrong_record_count():
    raw = build_wmedf([_simple_signal()], 4, declared_n_records=9)
    header = read_header(io.BytesIO(raw))
    with pytest.raises(WmedfError, match="records but the file holds"):
        verify_against_size(header, len(raw))


def test_verify_against_size_rejects_a_ragged_data_region():
    raw, _ = three_channel_file(4)
    header = read_header(io.BytesIO(raw))
    with pytest.raises(WmedfError, match="not a whole number"):
        verify_against_size(header, len(raw) - 1)


# --------------------------------------------------------------------------
# Range diagnostics
# --------------------------------------------------------------------------


def _out_of_range_file() -> bytes:
    signal = SyntheticSignal(
        label="Sensor",
        unit="%",
        physical_min=0.0,
        physical_max=100.0,
        digital_min=0,
        digital_max=100,
        samples_per_record=2,
        width_bytes=1,
        data=[50, 200],  # 200 is outside the declared maximum
    )
    return build_wmedf([signal], 1)


def test_out_of_range_sample_raises_by_default():
    stream = io.BytesIO(_out_of_range_file())
    header = read_header(stream)
    with pytest.raises(WmedfError, match="outside its declared range"):
        list(iter_digital_chunks(stream, header))


def test_out_of_range_sample_can_be_collected_instead():
    stream = io.BytesIO(_out_of_range_file())
    header = read_header(stream)
    collector = ViolationCollector()
    list(
        iter_digital_chunks(
            stream,
            header,
            policy=DiagnosticPolicy.COLLECT_VIOLATIONS,
            violations=collector,
        )
    )
    assert collector.total_seen == 1
    assert collector.truncated is False
    assert collector.violations[0].value == 200
    assert collector.violations[0].record_index == 0
    assert collector.violations[0].sample_index == 1


def test_collecting_without_a_destination_raises():
    # Otherwise the violations would be found and silently discarded, which is
    # worse than either alternative.
    stream = io.BytesIO(_out_of_range_file())
    header = read_header(stream)
    with pytest.raises(WmedfError, match="requires a ViolationCollector"):
        list(
            iter_digital_chunks(
                stream, header, policy=DiagnosticPolicy.COLLECT_VIOLATIONS
            )
        )


def test_violation_collection_is_capped_but_still_counted():
    """A misaligned channel puts every sample out of range.

    Collecting one object per sample would exhaust memory over a whole card,
    so the detail is capped — but the count must stay honest, and the
    collector must say that detail was dropped.
    """
    signal = SyntheticSignal(
        label="Broken",
        unit="%",
        physical_min=0.0,
        physical_max=100.0,
        digital_min=0,
        digital_max=10,
        samples_per_record=50,
        width_bytes=1,
        data=[200] * 50,
    )
    stream = io.BytesIO(build_wmedf([signal], 1))
    header = read_header(stream)
    collector = ViolationCollector(cap=7)
    list(
        iter_digital_chunks(
            stream,
            header,
            policy=DiagnosticPolicy.COLLECT_VIOLATIONS,
            violations=collector,
        )
    )
    assert collector.total_seen == 50
    assert len(collector.violations) == 7
    assert collector.truncated is True
    assert "50" in collector.summary() and "7" in collector.summary()


def test_a_clean_file_reports_no_violations():
    raw, _ = three_channel_file()
    stream = io.BytesIO(raw)
    header = read_header(stream)
    collector = ViolationCollector()
    list(
        iter_digital_chunks(
            stream,
            header,
            policy=DiagnosticPolicy.COLLECT_VIOLATIONS,
            violations=collector,
        )
    )
    assert collector.total_seen == 0
    assert collector.truncated is False


def test_violation_cap_must_be_positive():
    with pytest.raises(WmedfError, match="cap must be positive"):
        ViolationCollector(cap=0)


# --------------------------------------------------------------------------
# Stream behaviour
# --------------------------------------------------------------------------


class _DribblingStream(io.RawIOBase):
    """A stream that returns fewer bytes than asked for, which io permits.

    Archive members are streams, and treating a short read as end-of-file
    would truncate a session silently rather than loudly.
    """

    def __init__(self, data: bytes, most: int = 7):
        self._data = data
        self._pos = 0
        self._most = most

    def readable(self) -> bool:
        return True

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._data) - self._pos
        take = min(size, self._most, len(self._data) - self._pos)
        chunk = self._data[self._pos : self._pos + take]
        self._pos += take
        return chunk


def test_short_reads_do_not_truncate_the_session():
    n_records = 5
    raw, specs = three_channel_file(n_records)

    stream = _DribblingStream(raw)
    header = read_header(stream)
    recovered: dict[int, list[int]] = {i: [] for i in range(3)}
    for chunk in iter_digital_chunks(stream, header, records_per_chunk=2):
        recovered[chunk.signal.index].extend(chunk.digital)

    for index, spec in enumerate(specs):
        assert recovered[index] == spec.data


# --------------------------------------------------------------------------
# Degenerate but legal files
# --------------------------------------------------------------------------


def test_a_file_with_no_records_yields_nothing():
    signal = _simple_signal()
    signal.data = []
    raw = build_wmedf([signal], 0)
    stream = io.BytesIO(raw)
    header = read_header(stream)
    assert header.n_records == 0
    assert list(iter_digital_chunks(stream, header)) == []
    verify_against_size(header, len(raw))


def test_negative_record_count_raises():
    raw = build_wmedf([_simple_signal()], 1, declared_n_records=-1)
    with pytest.raises(WmedfError, match="completed recordings"):
        read_header(io.BytesIO(raw))


def test_zero_records_is_accepted_deliberately():
    """Zero is legal; negative is not. The distinction is intentional.

    A session started and stopped inside one second writes no records. That is
    an unremarkable short session, not a malformed file, and rejecting it
    would turn it into a decoding failure.
    """
    signal = _simple_signal()
    signal.data = []
    raw = build_wmedf([signal], 0)
    header = read_header(io.BytesIO(raw))
    assert header.n_records == 0
    verify_against_size(header, len(raw))


# --------------------------------------------------------------------------
# Non-finite numbers
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["nan", "inf", "-inf", "NaN", "Infinity"])
def test_non_finite_record_duration_raises(text):
    """float() accepts these, and then every comparison against them is false.

    A NaN duration would slip past the ``<= 0`` check silently rather than
    loudly, and every derived sampling rate would be NaN.
    """
    raw = build_wmedf([_simple_signal()], 1, record_duration=text)
    with pytest.raises(WmedfError, match="not a finite number|not a number"):
        read_header(io.BytesIO(raw))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_physical_minimum_raises(value):
    signal = _simple_signal()
    signal.physical_min = value
    raw = build_wmedf([signal], 1)
    with pytest.raises(WmedfError, match="not a finite number"):
        read_header(io.BytesIO(raw))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_physical_maximum_raises(value):
    signal = _simple_signal()
    signal.physical_max = value
    raw = build_wmedf([signal], 1)
    with pytest.raises(WmedfError, match="not a finite number"):
        read_header(io.BytesIO(raw))


def test_nan_physical_range_would_otherwise_pass_the_ordering_check():
    # Guards the reasoning, not just the outcome: with NaN, ``max <= min`` is
    # false, so the inverted-range check alone would have let this through.
    assert not (float("nan") <= 0.0)


# --------------------------------------------------------------------------
# Format discriminator
# --------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["0", "2", "", "x"])
def test_wrong_version_raises(version):
    raw = build_wmedf([_simple_signal()], 1, version=version)
    with pytest.raises(WmedfError, match="version field"):
        read_header(io.BytesIO(raw))


@pytest.mark.parametrize("reserved", ["", " " * 44, "#x", "s#", "#s trailing"])
def test_missing_reserved_marker_raises(reserved):
    raw = build_wmedf([_simple_signal()], 1, reserved=reserved)
    with pytest.raises(WmedfError, match="reserved field"):
        read_header(io.BytesIO(raw))


def test_reserved_marker_may_be_padded():
    # The real device writes spaces then the marker; padding is not content.
    raw = build_wmedf([_simple_signal()], 1, reserved=" " * 42 + "#s")
    assert read_header(io.BytesIO(raw)).reserved == "#s"


# --------------------------------------------------------------------------
# Start date and time must be exactly dd.mm.yy hh.mm.ss
# --------------------------------------------------------------------------


# The EDF date and time fields are eight bytes wide, so anything longer cannot
# occur: a writer emitting "2020.01.01" would have its value truncated by the
# field boundary and the remainder read as the next field. Only forms that fit
# are worth testing.
@pytest.mark.parametrize(
    "date_text",
    [
        "1.1.20",  # single-digit fields
        "01.1.20",
        "1.01.20",
        "01-01-20",  # wrong separators
        "01/01/20",
        "010120",  # no separators
        "01.01.2",  # short year
        "",
        "aa.bb.cc",
    ],
)
def test_malformed_start_date_raises(date_text):
    raw = build_wmedf([_simple_signal()], 1, start_date_text=date_text)
    with pytest.raises(WmedfError, match="not exactly"):
        read_header(io.BytesIO(raw))


@pytest.mark.parametrize(
    "time_text",
    ["1.2.3", "12.3.45", "12:30:45", "123045", "12.30", "12.30.4", ""],
)
def test_malformed_start_time_raises(time_text):
    raw = build_wmedf([_simple_signal()], 1, start_time_text=time_text)
    with pytest.raises(WmedfError, match="not exactly"):
        read_header(io.BytesIO(raw))


@pytest.mark.parametrize(
    "date_text,time_text",
    [
        ("32.01.20", "12.00.00"),  # no such day
        ("01.13.20", "12.00.00"),  # no such month
        ("30.02.20", "12.00.00"),  # not in February
        ("01.01.20", "25.00.00"),  # no such hour
        ("01.01.20", "12.60.00"),  # no such minute
        ("01.01.20", "12.00.60"),  # no such second
    ],
)
def test_well_formed_but_impossible_start_raises(date_text, time_text):
    # Shape is right, value is not. Both must be refused, and for different
    # reasons, so the message says which.
    raw = build_wmedf(
        [_simple_signal()], 1, start_date_text=date_text, start_time_text=time_text
    )
    with pytest.raises(WmedfError, match="not a real instant"):
        read_header(io.BytesIO(raw))


def test_standard_edf_gets_a_pointed_error():
    # A plain EDF has a blank per-signal reserved field. The message should say
    # so rather than leaving the reader to wonder what marker was expected.
    signal = _simple_signal()
    signal.marker_override = ""
    raw = build_wmedf([signal], 1)
    with pytest.raises(WmedfError, match="standard EDF"):
        read_header(io.BytesIO(raw))


# --------------------------------------------------------------------------
# Signal identity
# --------------------------------------------------------------------------


def test_label_lookup_rejects_an_ambiguous_label():
    a, b = _simple_signal(), _simple_signal()
    raw = build_wmedf([a, b], 1)
    header = read_header(io.BytesIO(raw))
    with pytest.raises(WmedfError, match="not unique"):
        header.signal_by_label(a.label)


def test_label_lookup_rejects_an_unknown_label():
    raw, _ = three_channel_file()
    header = read_header(io.BytesIO(raw))
    with pytest.raises(WmedfError, match="no signal labelled"):
        header.signal_by_label("Nonexistent")


def test_index_remains_the_stable_identity():
    raw, specs = three_channel_file()
    header = read_header(io.BytesIO(raw))
    assert [s.index for s in header.signals] == [0, 1, 2]
    assert [s.label for s in header.signals] == [s.label for s in specs]


def _simple_signal() -> SyntheticSignal:
    signal = SyntheticSignal(
        label="Plain",
        unit="hPa",
        physical_min=0.0,
        physical_max=65.0,
        digital_min=0,
        digital_max=650,
        samples_per_record=2,
        width_bytes=2,
    )
    signal.data = ramp(8, signal.digital_min, signal.digital_max)
    return signal
