"""Tests for the trend curve reader.

Fixtures are generated at runtime. The record bytes are arbitrary: the layout
is not decoded, so a fixture that pretended to encode values would be claiming
something this decoder does not know.
"""

from datetime import date, timedelta

import pytest

from prisma_vent.trendcurve import (
    RECORD_BYTES,
    RECORD_INTERVAL,
    TrendCurveError,
    read_trend_curve,
)
from synthetic import build_trend_curve

DAY = date(2020, 1, 1)


def test_reads_the_header(tmp_path):
    curve = read_trend_curve(build_trend_curve(tmp_path, day=DAY, day_number=123))
    assert curve.day == DAY
    assert curve.day_number == 123
    assert curve.device_type == "P32"
    assert curve.device_id == "002"


def test_offset_field_is_carried_not_interpreted(tmp_path):
    """Its meaning is unknown, so it is kept verbatim rather than parsed."""
    curve = read_trend_curve(build_trend_curve(tmp_path, day=DAY))
    assert curve.offset_field == "0105"


def test_records_are_split_and_handed_back_untouched(tmp_path):
    path = build_trend_curve(tmp_path, day=DAY, populated=4, padding=2)
    curve = read_trend_curve(path)
    assert len(curve.records) == 6
    assert all(len(r.raw) == RECORD_BYTES for r in curve.records)
    assert [r.index for r in curve.records] == [0, 1, 2, 3, 4, 5]


def test_padding_records_are_recognised(tmp_path):
    curve = read_trend_curve(build_trend_curve(tmp_path, day=DAY, populated=7, padding=3))
    assert curve.populated_records == 7
    assert sum(1 for r in curve.records if not r.populated) == 3


def test_therapy_time_is_two_minutes_per_populated_record(tmp_path):
    curve = read_trend_curve(build_trend_curve(tmp_path, day=DAY, populated=30, padding=5))
    assert curve.estimated_therapy_time == 30 * RECORD_INTERVAL
    assert curve.estimated_therapy_time == timedelta(hours=1)




def test_records_carry_no_decoded_fields(tmp_path):
    # The layout is unknown. There is deliberately nothing here to mistake for
    # a measurement.
    record = read_trend_curve(build_trend_curve(tmp_path, day=DAY)).records[0]
    assert not hasattr(record, "pressure")
    assert not hasattr(record, "value")
    assert isinstance(record.raw, bytes)


def test_provenance_names_the_file_and_hashes_it(tmp_path):
    import hashlib

    path = build_trend_curve(tmp_path, day=DAY)
    curve = read_trend_curve(path)
    assert curve.provenance.archive_name == path.name
    assert curve.provenance.archive_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert curve.provenance.session_number is None


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_header_day_disagreeing_with_the_filename_is_refused(tmp_path):
    # A header date that deliberately differs from the filename's.
    path = build_trend_curve(tmp_path, day=DAY, header_day="02.02.2021")
    with pytest.raises(TrendCurveError, match="declares 2021-02-02"):
        read_trend_curve(path)


def test_unexpected_device_type_is_refused(tmp_path):
    path = build_trend_curve(tmp_path, day=DAY, device_type="P99")
    with pytest.raises(TrendCurveError, match="device type"):
        read_trend_curve(path)


def test_body_that_is_not_whole_records_is_refused(tmp_path):
    path = build_trend_curve(tmp_path, day=DAY, body_extra=4)
    with pytest.raises(TrendCurveError, match="not a whole number"):
        read_trend_curve(path)


def test_missing_json_header_is_refused(tmp_path):
    path = tmp_path / "0123_2020-01-01.tc"
    path.write_bytes(b"\x00\x01\x02not json at all")
    with pytest.raises(TrendCurveError, match="does not begin with a JSON header"):
        read_trend_curve(path)


def test_malformed_json_header_is_refused(tmp_path):
    path = build_trend_curve(tmp_path, day=DAY, header_text='{"Type":"P32",}')
    with pytest.raises(TrendCurveError, match="unreadable JSON header"):
        read_trend_curve(path)


@pytest.mark.parametrize("bad", ["1.1.2020", "2020-01-01", "01.01.20", ""])
def test_malformed_header_day_is_refused(tmp_path, bad):
    path = build_trend_curve(tmp_path, day=DAY, header_day=bad)
    with pytest.raises(TrendCurveError, match="not exactly dd.mm.yyyy"):
        read_trend_curve(path)


def test_badly_named_file_is_refused(tmp_path):
    src = build_trend_curve(tmp_path, day=DAY)
    renamed = tmp_path / "curve.tc"
    src.rename(renamed)
    with pytest.raises(TrendCurveError, match="NNNN_YYYY-MM-DD"):
        read_trend_curve(renamed)


def test_oversized_file_is_refused(tmp_path):
    from prisma_vent import trendcurve

    path = build_trend_curve(tmp_path, day=DAY)
    original = trendcurve.MAX_FILE_BYTES
    trendcurve.MAX_FILE_BYTES = 10
    try:
        with pytest.raises(TrendCurveError, match="over the"):
            read_trend_curve(path)
    finally:
        trendcurve.MAX_FILE_BYTES = original
