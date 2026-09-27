"""Tests for scaling digital samples to physical units, and for the time axis.

Scaling is a separate step from decoding on purpose, and these tests keep it
that way: a chunk that has not been scaled must be distinguishable from one
that has, and the conversion must be affine rather than a bare multiplication.

All fixtures are generated at runtime. Nothing here comes from a recording.
"""

import io
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from prisma_vent.timebase import DeviceLocalTime
from prisma_vent.wmedf import (
    WmedfError,
    _sample_offset,
    iter_chunks,
    iter_digital_chunks,
    read_all,
    read_header,
    sample_time,
    to_physical,
)
from synthetic import SyntheticSignal, build_wmedf, ramp, three_channel_file

# --------------------------------------------------------------------------
# The conversion is affine
# --------------------------------------------------------------------------


def test_offset_term_is_applied():
    """A channel whose physical and digital minima are not proportional.

    ``digital * scale`` would give 0.0 at the digital minimum instead of the
    declared −50.0. Every channel on the real device happens to avoid this, so
    the shorter formula would pass unnoticed there — hence a fixture that does
    not.
    """
    signal = SyntheticSignal(
        label="Offset",
        unit="ms",
        physical_min=-50.0,
        physical_max=50.0,
        digital_min=0,
        digital_max=100,
        samples_per_record=3,
        width_bytes=1,
        data=[0, 50, 100],
    )
    stream = io.BytesIO(build_wmedf([signal], 1))
    header = read_header(stream)
    chunk = to_physical(next(iter_digital_chunks(stream, header)))

    assert list(chunk.physical) == [-50.0, 0.0, 50.0]


def test_bare_multiplication_would_differ():
    # Guards the reasoning, so the offset is not "simplified" away later.
    signal = SyntheticSignal(
        label="Offset",
        unit="ms",
        physical_min=-50.0,
        physical_max=50.0,
        digital_min=0,
        digital_max=100,
        samples_per_record=1,
        width_bytes=1,
        data=[0],
    )
    stream = io.BytesIO(build_wmedf([signal], 1))
    header = read_header(stream)
    s = header.signals[0]
    assert s.to_physical_value(0) == -50.0
    assert 0 * s.scale == 0.0  # what the shorter formula would produce


def test_scale_and_offset_for_a_proportional_channel():
    signal = SyntheticSignal(
        label="Pressure",
        unit="hPa",
        physical_min=0.0,
        physical_max=65.0,
        digital_min=0,
        digital_max=650,
        samples_per_record=3,
        width_bytes=2,
        data=[0, 65, 650],
    )
    stream = io.BytesIO(build_wmedf([signal], 1))
    header = read_header(stream)
    chunk = to_physical(next(iter_digital_chunks(stream, header)))
    assert [round(v, 10) for v in chunk.physical] == [0.0, 6.5, 65.0]


def test_negative_digital_minimum_scales_correctly():
    signal = SyntheticSignal(
        label="Flow",
        unit="l/min",
        physical_min=-120.0,
        physical_max=250.0,
        digital_min=-1200,
        digital_max=2500,
        samples_per_record=3,
        width_bytes=2,
        data=[-1200, 0, 2500],
    )
    stream = io.BytesIO(build_wmedf([signal], 1))
    header = read_header(stream)
    chunk = to_physical(next(iter_digital_chunks(stream, header)))
    assert [round(v, 9) for v in chunk.physical] == [-120.0, 0.0, 250.0]


def test_endpoints_map_to_the_declared_extremes():
    raw, specs = three_channel_file()
    stream = io.BytesIO(raw)
    header = read_header(stream)
    for chunk in iter_chunks(stream, header):
        s = chunk.signal
        assert s.to_physical_value(s.digital_min) == pytest.approx(s.physical_min)
        assert s.to_physical_value(s.digital_max) == pytest.approx(s.physical_max)


# --------------------------------------------------------------------------
# Decoding and scaling stay distinguishable
# --------------------------------------------------------------------------


def test_digital_chunks_are_unscaled_and_scaled_chunks_are_not():
    raw, _ = three_channel_file()

    stream = io.BytesIO(raw)
    header = read_header(stream)
    assert all(c.physical is None for c in iter_digital_chunks(stream, header))

    stream = io.BytesIO(raw)
    header = read_header(stream)
    assert all(c.physical is not None for c in iter_chunks(stream, header))


def test_scaling_preserves_the_digital_values():
    raw, _ = three_channel_file()
    stream = io.BytesIO(raw)
    header = read_header(stream)
    for chunk in iter_digital_chunks(stream, header):
        scaled = to_physical(chunk)
        assert list(scaled.digital) == list(chunk.digital)
        assert len(scaled.physical) == len(scaled.digital)


def test_scaling_twice_is_harmless():
    # Recomputed from digital each time rather than applied to the previous
    # result, so a double call cannot compound.
    raw, _ = three_channel_file()
    stream = io.BytesIO(raw)
    header = read_header(stream)
    once = to_physical(next(iter_digital_chunks(stream, header)))
    twice = to_physical(once)
    assert list(once.physical) == list(twice.physical)


def test_chunk_positions_survive_scaling():
    raw, _ = three_channel_file(6)
    stream = io.BytesIO(raw)
    header = read_header(stream)
    for chunk in iter_chunks(stream, header, records_per_chunk=2):
        assert chunk.start_sample == chunk.start_record * chunk.signal.samples_per_record


# --------------------------------------------------------------------------
# read_all
# --------------------------------------------------------------------------


def test_read_all_returns_every_signal_whole():
    n_records = 5
    raw, specs = three_channel_file(n_records)
    stream = io.BytesIO(raw)
    header = read_header(stream)
    result = read_all(stream, header)

    assert set(result) == {0, 1, 2}
    for index, spec in enumerate(specs):
        assert list(result[index].digital) == spec.data
        assert result[index].physical is not None
        assert len(result[index].physical) == len(spec.data)


def test_read_all_matches_the_chunked_path():
    raw, _ = three_channel_file(5)

    stream = io.BytesIO(raw)
    header = read_header(stream)
    whole = read_all(stream, header)

    stream = io.BytesIO(raw)
    header = read_header(stream)
    streamed: dict[int, list[float]] = {i: [] for i in range(3)}
    for chunk in iter_chunks(stream, header, records_per_chunk=2):
        streamed[chunk.signal.index].extend(chunk.physical)

    for index in streamed:
        assert list(whole[index].physical) == streamed[index]


def test_read_all_on_an_empty_session():
    signal = SyntheticSignal(
        label="Plain",
        unit="hPa",
        physical_min=0.0,
        physical_max=65.0,
        digital_min=0,
        digital_max=650,
        samples_per_record=2,
        width_bytes=2,
        data=[],
    )
    raw = build_wmedf([signal], 0)
    stream = io.BytesIO(raw)
    header = read_header(stream)
    result = read_all(stream, header)
    assert list(result[0].digital) == []
    assert list(result[0].physical) == []


# --------------------------------------------------------------------------
# The time axis is derived, not accumulated
# --------------------------------------------------------------------------


def _one_signal_header(samples_per_record: int, n_records: int = 3):
    signal = SyntheticSignal(
        label="Fast",
        unit="hPa",
        physical_min=0.0,
        physical_max=65.0,
        digital_min=0,
        digital_max=650,
        samples_per_record=samples_per_record,
        width_bytes=2,
    )
    signal.data = ramp(n_records * samples_per_record, 0, 650)
    raw = build_wmedf([signal], n_records, start=datetime(2020, 3, 4, 12, 0, 0))
    return read_header(io.BytesIO(raw))


def test_first_sample_is_the_recording_start():
    header = _one_signal_header(10)
    assert sample_time(header, header.signals[0], 0) == header.start


def test_ten_hertz_samples_are_a_tenth_of_a_second_apart():
    header = _one_signal_header(10)
    signal = header.signals[0]
    assert sample_time(header, signal, 1).value == datetime(2020, 3, 4, 12, 0, 0, 100000)
    assert sample_time(header, signal, 5).value == datetime(2020, 3, 4, 12, 0, 0, 500000)
    assert sample_time(header, signal, 10).value == datetime(2020, 3, 4, 12, 0, 1)


def test_one_hertz_samples_are_a_second_apart():
    # Five records, so index 3 exists. Written with three, this asked for a
    # sample the session did not contain — which the bound now refuses, and
    # which previously returned a plausible time for nothing.
    header = _one_signal_header(1, n_records=5)
    signal = header.signals[0]
    assert sample_time(header, signal, 3).value == datetime(2020, 3, 4, 12, 0, 3)


def test_no_drift_over_a_long_session():
    """Nine hours at 10 Hz is 324 000 samples.

    Tested against the offset helper rather than through ``sample_time``,
    which now refuses indices past the end of a session — materialising a
    nine-hour fixture just to reach one index would be waste, and the property
    under test is the arithmetic, not the bounds.
    """
    index = 324_000
    assert _sample_offset(1.0, 10, index) == timedelta(seconds=index / 10)

    naive = 0.0
    for _ in range(index):
        naive += 0.1
    assert naive != index / 10  # the approach deliberately not taken


def test_offset_helper_matches_sample_time_within_bounds():
    header = _one_signal_header(10, n_records=3)
    signal = header.signals[0]
    for index in (0, 1, 9, 10, 29):
        assert (
            sample_time(header, signal, index).value
            == header.start.value + _sample_offset(1.0, 10, index)
        )


def test_sample_time_rejects_a_negative_index():
    header = _one_signal_header(10)
    with pytest.raises(WmedfError, match="must not be negative"):
        sample_time(header, header.signals[0], -1)


def test_last_sample_of_the_session_is_accepted():
    header = _one_signal_header(10, n_records=3)  # 30 samples, indices 0..29
    signal = header.signals[0]
    assert sample_time(header, signal, 29).value == datetime(2020, 3, 4, 12, 0, 2, 900000)


def test_one_past_the_last_sample_is_refused():
    """The off-by-one this bound exists to catch.

    Without it the function returns a perfectly plausible timestamp for a
    sample the session does not contain, and nothing downstream can tell.
    """
    header = _one_signal_header(10, n_records=3)
    with pytest.raises(WmedfError, match="past the end"):
        sample_time(header, header.signals[0], 30)


def test_far_out_of_range_index_is_refused():
    header = _one_signal_header(10, n_records=3)
    with pytest.raises(WmedfError, match="past the end"):
        sample_time(header, header.signals[0], 1_000_000)


def test_every_index_is_refused_for_an_empty_session():
    signal = SyntheticSignal(
        label="Fast",
        unit="hPa",
        physical_min=0.0,
        physical_max=65.0,
        digital_min=0,
        digital_max=650,
        samples_per_record=10,
        width_bytes=2,
        data=[],
    )
    header = read_header(io.BytesIO(build_wmedf([signal], 0)))
    with pytest.raises(WmedfError, match="past the end"):
        sample_time(header, header.signals[0], 0)


def test_signal_from_another_header_is_refused():
    """Checked by metadata, not by index alone.

    A signal at the same index in another session may declare a different
    sampling rate, so the time computed from it would be wrong while looking
    entirely ordinary.
    """
    fast = _one_signal_header(10, n_records=3)
    slow = _one_signal_header(1, n_records=3)
    with pytest.raises(WmedfError, match="does not belong to this header"):
        sample_time(fast, slow.signals[0], 0)


def test_signal_with_an_index_beyond_the_header_is_refused():
    header = _one_signal_header(10, n_records=3)
    foreign = replace(header.signals[0], index=7)
    with pytest.raises(WmedfError, match="does not belong to this header"):
        sample_time(header, foreign, 0)


def test_identical_metadata_from_an_equivalent_header_is_accepted():
    # Equality, not identity: two headers describing the same signal are
    # indistinguishable, and refusing that would be pedantry rather than safety.
    a = _one_signal_header(10, n_records=3)
    b = _one_signal_header(10, n_records=3)
    assert sample_time(a, b.signals[0], 5) == sample_time(a, a.signals[0], 5)


def test_sample_time_returns_device_local_time():
    header = _one_signal_header(10)
    assert isinstance(sample_time(header, header.signals[0], 7), DeviceLocalTime)


@pytest.mark.parametrize(
    "bad", [0.5, 1.0, True, False, "3", None, b"3", [1], 3 + 0j]
)
def test_sample_index_must_be_a_real_int(bad):
    """Type before value.

    ``bool`` subclasses ``int``, so ``True`` would otherwise pass as index 1;
    a float would name half a sample; a string or None would raise something
    other than WmedfError and so escape a caller catching it.
    """
    header = _one_signal_header(10, n_records=3)
    with pytest.raises(WmedfError, match="must be an int"):
        sample_time(header, header.signals[0], bad)


def test_ordinary_integer_indices_still_work():
    header = _one_signal_header(10, n_records=3)
    assert sample_time(header, header.signals[0], 0).value == datetime(2020, 3, 4, 12, 0, 0)
    assert sample_time(header, header.signals[0], 29).value == datetime(
        2020, 3, 4, 12, 0, 2, 900000
    )
