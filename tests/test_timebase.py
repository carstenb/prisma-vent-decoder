"""Tests for the device time base.

All dates here are synthetic. Nothing in this file is taken from a real
recording.
"""

from datetime import date, datetime, timedelta

import pytest

from prisma_vent.timebase import (
    DeviceLocalTime,
    TimebaseError,
    day_reference,
    format_offset,
    parse_offset,
    session_reference,
    therapy_day,
)

# --------------------------------------------------------------------------
# Offset parsing
# --------------------------------------------------------------------------


def test_parses_a_plain_positive_offset():
    assert parse_offset("+0001:02:03.004") == timedelta(
        hours=1, minutes=2, seconds=3, milliseconds=4
    )


def test_parses_a_negative_offset():
    assert parse_offset("-0001:02:03.004") == -timedelta(
        hours=1, minutes=2, seconds=3, milliseconds=4
    )


def test_hours_may_exceed_a_full_day():
    # A day-level log reaches the following noon at +0036:00:00. Bounding the
    # hour field to 24 would reject every second half of every archive.
    assert parse_offset("+0036:00:00.000") == timedelta(hours=36)


def test_hours_may_exceed_ninety_nine():
    # The field is four digits, so values a two-digit parser would truncate
    # are representable and must round-trip.
    assert parse_offset("+9999:00:00.000") == timedelta(hours=9999)
    assert format_offset(timedelta(hours=9999)) == "+9999:00:00.000"


def test_negative_offsets_may_span_more_than_a_day():
    assert parse_offset("-0030:15:00.000") == -timedelta(hours=30, minutes=15)


def test_negative_zero_is_canonicalised_to_zero():
    # A sign on zero carries no information. Accepting it and normalising is
    # right; rejecting it would fail on a value the device may legitimately
    # write.
    assert parse_offset("-0000:00:00.000") == timedelta(0)
    assert parse_offset("+0000:00:00.000") == timedelta(0)
    assert parse_offset("-0000:00:00.000") == parse_offset("+0000:00:00.000")


@pytest.mark.parametrize(
    "bad",
    [
        "0001:02:03.004",  # no sign: the device always writes one
        "+001:02:03.004",  # three-digit hours
        "+0001:02:03",  # no milliseconds
        "+0001:02:03.04",  # two-digit milliseconds
        "+0001:60:00.000",  # minute field out of range
        "+0001:00:60.000",  # second field out of range
        "+0001-02-03.004",  # wrong separators
        " +0001:02:03.004",  # leading space
        "+0001:02:03.004 ",  # trailing space
        "",
    ],
)
def test_malformed_offsets_raise(bad):
    with pytest.raises(TimebaseError):
        parse_offset(bad)


def test_non_string_offset_raises():
    with pytest.raises(TimebaseError):
        parse_offset(3600)


@pytest.mark.parametrize(
    "text",
    ["+0000:00:00.000", "+0012:00:00.670", "+0036:00:00.559", "-0008:44:31.078"],
)
def test_offset_round_trips(text):
    assert format_offset(parse_offset(text)) == text


# --------------------------------------------------------------------------
# DeviceLocalTime
# --------------------------------------------------------------------------


def test_device_local_time_rejects_an_aware_datetime():
    from datetime import timezone

    with pytest.raises(TimebaseError):
        DeviceLocalTime(datetime(2020, 3, 4, 12, 0, tzinfo=timezone.utc))


def test_device_local_time_rejects_a_non_datetime():
    with pytest.raises(TimebaseError):
        DeviceLocalTime(date(2020, 3, 4))


def test_device_local_time_shifts_and_stays_naive():
    shifted = DeviceLocalTime(datetime(2020, 3, 4, 12, 0)).shift(timedelta(hours=25))
    assert shifted.value == datetime(2020, 3, 5, 13, 0)
    assert shifted.value.tzinfo is None


# --------------------------------------------------------------------------
# The two references — the whole point of this module
# --------------------------------------------------------------------------


def test_day_and_session_references_are_not_interchangeable():
    """The same instant, written in both files, uses different offsets.

    A session beginning after midnight is written in the day-level log as an
    offset past 24 hours from the archive date, and in its own session file as
    a small offset from the *following* midnight. Both must resolve to one
    instant. Assuming a single reference puts the session a full day early.
    """
    archive_date = date(2020, 3, 4)
    session_start_date = date(2020, 3, 5)  # the session began after midnight

    from_day_log = day_reference(archive_date).shift(parse_offset("+0024:35:07.619"))
    from_session = session_reference(session_start_date).shift(
        parse_offset("+0000:35:07.619")
    )

    assert from_day_log == from_session
    assert from_day_log.value == datetime(2020, 3, 5, 0, 35, 7, 619000)


def test_using_the_wrong_reference_lands_exactly_one_day_early():
    # Documents the failure mode explicitly, so a regression is recognisable
    # as this bug rather than as a puzzling off-by-something.
    session_start_date = date(2020, 3, 5)
    correct = session_reference(session_start_date).shift(parse_offset("+0000:35:07.619"))
    wrong = day_reference(date(2020, 3, 4)).shift(parse_offset("+0000:35:07.619"))
    assert correct.value - wrong.value == timedelta(days=1)


def test_session_starting_before_the_archive_date():
    # Negative offsets occur: the day-level log carries events stamped before
    # its nominal day began.
    resolved = day_reference(date(2020, 3, 4)).shift(parse_offset("-0008:44:31.078"))
    assert resolved.value == datetime(2020, 3, 3, 15, 15, 28, 922000)


def test_references_reject_a_datetime():
    # datetime subclasses date; accepting one would silently discard its time.
    with pytest.raises(TimebaseError):
        day_reference(datetime(2020, 3, 4, 12, 0))
    with pytest.raises(TimebaseError):
        session_reference(datetime(2020, 3, 4, 12, 0))


# --------------------------------------------------------------------------
# The noon-to-noon therapy day
# --------------------------------------------------------------------------


def test_noon_exactly_belongs_to_its_own_date():
    assert therapy_day(DeviceLocalTime(datetime(2020, 3, 4, 12, 0, 0))) == date(2020, 3, 4)


def test_one_millisecond_before_noon_belongs_to_the_previous_day():
    instant = DeviceLocalTime(datetime(2020, 3, 4, 11, 59, 59, 999000))
    assert therapy_day(instant) == date(2020, 3, 3)


def test_a_night_is_not_split_across_two_therapy_days():
    """The reason the noon boundary exists at all."""
    evening = DeviceLocalTime(datetime(2020, 3, 4, 22, 30))
    small_hours = DeviceLocalTime(datetime(2020, 3, 5, 3, 15))
    assert therapy_day(evening) == therapy_day(small_hours) == date(2020, 3, 4)


def test_midnight_belongs_to_the_previous_therapy_day():
    assert therapy_day(DeviceLocalTime(datetime(2020, 3, 5, 0, 0))) == date(2020, 3, 4)


def test_month_boundary():
    assert therapy_day(DeviceLocalTime(datetime(2020, 4, 1, 6, 0))) == date(2020, 3, 31)


def test_year_boundary():
    assert therapy_day(DeviceLocalTime(datetime(2021, 1, 1, 6, 0))) == date(2020, 12, 31)
    end_of_day = day_reference(date(2020, 12, 31)).shift(parse_offset("+0036:00:00.000"))
    assert end_of_day.value == datetime(2021, 1, 1, 12, 0)
    assert therapy_day(end_of_day) == date(2021, 1, 1)


def test_leap_day():
    assert therapy_day(DeviceLocalTime(datetime(2020, 3, 1, 6, 0))) == date(2020, 2, 29)


def test_therapy_day_rejects_a_bare_datetime():
    # Accepting one would hide whether a zone had been assumed somewhere.
    with pytest.raises(TimebaseError):
        therapy_day(datetime(2020, 3, 4, 12, 0))
