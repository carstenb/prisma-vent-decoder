"""Tests for the ``usage`` command.

Fixtures are generated at runtime and entirely synthetic.
"""

from __future__ import annotations

import io
from datetime import date, datetime

import pytest

from prisma_vent.statistic import EPOCH_OFFSET
from prisma_vent.usage import Bucket, main, summarise
from synthetic import build_day_archive, build_statistic, build_usage_record

ARCHIVE_DATE = date(2020, 3, 4)


def stamp(moment: datetime) -> int:
    """The counter the device would store for a device-local instant."""
    return int((moment - datetime(1970, 1, 1) - EPOCH_OFFSET).total_seconds())


def make_archive(tmp_path, sessions, *, day_number=123, archive_date=ARCHIVE_DATE):
    """*sessions* is a list of (device-local start, duration minutes)."""
    return build_day_archive(
        tmp_path,
        archive_date=archive_date,
        day_number=day_number,
        sessions=[
            (1, datetime.combine(archive_date, datetime.min.time()).replace(hour=22), 60)
        ],
        extra_members={
            "statistic.proto": build_statistic(
                records=[
                    build_usage_record(timestamp=stamp(start), duration=minutes)
                    for start, minutes in sessions
                ],
                therapy_total=1_000_000,
            )
        },
    )


def run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    status = main([str(a) for a in argv], out=out)
    return status, out.getvalue()


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def records(*pairs):
    from prisma_vent.statistic import read_statistic

    data = build_statistic(
        records=[
            build_usage_record(timestamp=stamp(start), duration=minutes)
            for start, minutes in pairs
        ],
        therapy_total=1_000_000,
    )
    return read_statistic(data).records


def test_sessions_on_one_night_land_in_one_therapy_day():
    """The point of noon-to-noon: an evening and the small hours are one night."""
    buckets = summarise(
        records(
            (datetime(2021, 6, 14, 22, 0), 100),
            (datetime(2021, 6, 15, 2, 0), 200),
        )
    )
    assert [b.label for b in buckets] == ["2021-06-14"]
    assert buckets[0].minutes == 300


def test_calendar_day_splits_that_same_night_in_two():
    """Offered deliberately, and it must be visibly different from the default."""
    buckets = summarise(
        records(
            (datetime(2021, 6, 14, 22, 0), 100),
            (datetime(2021, 6, 15, 2, 0), 200),
        ),
        by="calendar-day",
    )
    assert [b.label for b in buckets] == ["2021-06-14", "2021-06-15"]
    assert [b.minutes for b in buckets] == [100, 200]


def test_the_first_and_last_periods_are_marked_partial():
    """A rolling window cuts them off; an average over them would be wrong."""
    buckets = summarise(
        records(
            (datetime(2021, 6, 14, 22, 0), 60),
            (datetime(2021, 6, 15, 22, 0), 600),
            (datetime(2021, 6, 16, 22, 0), 60),
        )
    )
    assert [b.partial for b in buckets] == [True, False, True]


def test_partial_periods_are_marked_not_dropped():
    """Silently discarding data is worse than labelling it."""
    buckets = summarise(records((datetime(2021, 6, 14, 22, 0), 60)))
    assert len(buckets) == 1
    assert buckets[0].partial and buckets[0].minutes == 60


def test_a_month_containing_a_clipped_day_is_itself_partial():
    buckets = summarise(
        records(
            (datetime(2021, 6, 30, 22, 0), 60),
            (datetime(2021, 7, 15, 22, 0), 600),
            (datetime(2021, 8, 2, 22, 0), 60),
        ),
        by="month",
    )
    assert [(b.label, b.partial) for b in buckets] == [
        ("2021-06", True),
        ("2021-07", False),
        ("2021-08", True),
    ]


def test_hours_per_day_divides_by_the_days_a_period_holds():
    from prisma_vent.usage import Day

    bucket = Bucket(
        "2021-06",
        days_covered=tuple(
            Day(date=date(2021, 6, n), minutes=60, sessions=1)
            for n in range(1, 11)
        ),
    )
    assert bucket.days == 10
    assert bucket.hours_per_day == 1.0
    assert bucket.hours == 10.0


def test_no_records_yields_no_buckets():
    assert summarise([]) == []


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def test_the_report_names_the_day_boundary_it_used(tmp_path):
    archive = make_archive(tmp_path, [(datetime(2021, 6, 14, 22, 0), 300)])
    _, text = run(archive)
    assert "therapy day, noon to noon" in text


def test_calendar_day_says_so_rather_than_quietly_changing_the_column(tmp_path):
    archive = make_archive(tmp_path, [(datetime(2021, 6, 14, 22, 0), 300)])
    _, text = run(archive, "--by", "calendar-day")
    assert "calendar day, midnight to midnight" in text


def test_partial_periods_are_excluded_from_the_average_by_default(tmp_path):
    archive = make_archive(
        tmp_path,
        [
            (datetime(2021, 6, 14, 22, 0), 60),    # clipped
            (datetime(2021, 6, 15, 22, 0), 600),
            (datetime(2021, 6, 16, 22, 0), 600),
            (datetime(2021, 6, 17, 22, 0), 60),    # clipped
        ],
    )
    _, text = run(archive)
    assert "mean 10.00 h/day" in text
    assert "2 cut-off day(s) excluded" in text


def test_include_partial_counts_them(tmp_path):
    archive = make_archive(
        tmp_path,
        [
            (datetime(2021, 6, 14, 22, 0), 60),
            (datetime(2021, 6, 15, 22, 0), 600),
            (datetime(2021, 6, 16, 22, 0), 60),
        ],
    )
    _, text = run(archive, "--include-partial")
    assert "over 3 day(s)" in text
    assert "excluded" not in text


def test_every_period_partial_is_said_rather_than_averaged(tmp_path):
    archive = make_archive(tmp_path, [(datetime(2021, 6, 14, 22, 0), 300)])
    _, text = run(archive)
    assert "every day shown is cut off" in text
    assert "mean " not in text


def test_the_duration_only_caveat_is_always_printed(tmp_path):
    """The figures invite a clinical reading they cannot support."""
    archive = make_archive(tmp_path, [(datetime(2021, 6, 14, 22, 0), 300)])
    _, text = run(archive)
    assert "Duration only" in text
    assert "no waveform" in text


def test_the_caveat_states_the_resolution_and_what_a_zero_day_is_not(tmp_path):
    """A zero is 'nothing stored', not 'the device stayed off'."""
    archive = make_archive(
        tmp_path,
        [
            (datetime(2021, 6, 14, 22, 0), 60),
            (datetime(2021, 6, 16, 22, 0), 60),
        ],
    )
    _, text = run(archive)
    assert "whole minutes" in text
    assert "no session shorter than a minute" in text
    assert "not necessarily that the device stayed off" in text


def test_no_output_claims_the_device_was_never_switched_on(tmp_path):
    archive = make_archive(
        tmp_path,
        [
            (datetime(2021, 6, 14, 22, 0), 60),
            (datetime(2021, 6, 16, 22, 0), 60),
        ],
    )
    _, text = run(archive)
    for phrase in ("never ran", "never switched on", "device was off"):
        assert phrase not in text


def test_a_zero_day_still_reports_zero_minutes():
    """The value is right; only the claim about what it proves was too strong."""
    buckets = summarise(
        records(
            (datetime(2021, 6, 14, 22, 0), 60),
            (datetime(2021, 6, 16, 22, 0), 60),
        )
    )
    middle = buckets[1]
    assert middle.label == "2021-06-15"
    assert middle.minutes == 0 and middle.sessions == 0
    assert middle.hours == 0.0


def test_duplicate_rows_across_archives_are_merged_and_reported(tmp_path):
    """Every archive on a card repeats the same year."""
    sessions = [
        (datetime(2021, 6, 14, 22, 0), 60),
        (datetime(2021, 6, 15, 22, 0), 600),
    ]
    a = make_archive(tmp_path / "a", sessions, day_number=123)
    b = make_archive(tmp_path / "b", sessions, day_number=479, archive_date=date(2020, 3, 5))
    _, text = run(a, b)
    assert "2 sessions" in text
    assert "2 duplicate row(s) merged" in text


def test_the_union_of_archives_reaches_further_than_either(tmp_path):
    """Each copy is a different window, so together they cover more."""
    a = make_archive(tmp_path / "a", [(datetime(2021, 6, 1, 22, 0), 60)], day_number=123)
    b = make_archive(
        tmp_path / "b",
        [(datetime(2021, 7, 1, 22, 0), 60)],
        day_number=479,
        archive_date=date(2020, 3, 5),
    )
    _, text = run(a, b)
    assert "2021-06-01" in text and "2021-07-01" in text


def test_since_and_until_filter(tmp_path):
    archive = make_archive(
        tmp_path,
        [
            (datetime(2021, 6, 14, 22, 0), 60),
            (datetime(2021, 6, 15, 22, 0), 60),
            (datetime(2021, 6, 16, 22, 0), 60),
        ],
    )
    _, text = run(archive, "--since", "2021-06-15")
    assert "2021-06-14" not in text and "2021-06-15" in text
    _, text = run(archive, "--until", "2021-06-14")
    assert "2021-06-15" not in text


def test_a_bad_date_is_refused_rather_than_ignored(tmp_path, capsys):
    """A usage error, so exit 2 rather than the structural code."""
    from prisma_vent.usage import EXIT_USAGE

    archive = make_archive(tmp_path, [(datetime(2021, 6, 14, 22, 0), 60)])
    status, _ = run(archive, "--since", "yesterday")
    assert status == EXIT_USAGE
    assert "not a date" in capsys.readouterr().err


def test_csv_carries_the_partial_flag(tmp_path):
    archive = make_archive(
        tmp_path,
        [
            (datetime(2021, 6, 14, 22, 0), 60),
            (datetime(2021, 6, 15, 22, 0), 600),
        ],
    )
    _, text = run(archive, "--csv")
    lines = text.strip().splitlines()
    assert lines[0] == "period,therapy_minutes,sessions,days,partial"
    assert lines[1].endswith(",true")
    assert lines[-1].endswith(",true")


def test_an_archive_without_the_member_fails_loudly(tmp_path):
    archive = build_day_archive(
        tmp_path,
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 60)],
    )
    status, _ = run(archive)
    assert status == 1


# ---------------------------------------------------------------------------
# Days the device did not run
# ---------------------------------------------------------------------------


def test_a_gap_between_active_days_appears_as_zero_days():
    """A covered day with no therapy is a measurement, not an absence."""
    buckets = summarise(
        records(
            (datetime(2021, 6, 14, 22, 0), 60),
            (datetime(2021, 6, 17, 22, 0), 600),
        )
    )
    assert [b.label for b in buckets] == [
        "2021-06-14",
        "2021-06-15",
        "2021-06-16",
        "2021-06-17",
    ]
    assert [b.minutes for b in buckets] == [60, 0, 0, 600]
    assert [b.sessions for b in buckets] == [1, 0, 0, 1]


def test_zero_days_are_not_marked_partial():
    """Partial means cut off by the window, which an inner zero day is not."""
    buckets = summarise(
        records(
            (datetime(2021, 6, 14, 22, 0), 60),
            (datetime(2021, 6, 17, 22, 0), 600),
        )
    )
    assert [b.partial for b in buckets] == [True, False, False, True]


def test_days_outside_the_window_are_not_invented():
    """A zero outside the covered range would claim knowledge the file lacks."""
    buckets = summarise(records((datetime(2021, 6, 15, 22, 0), 60)))
    assert [b.label for b in buckets] == ["2021-06-15"]


def test_a_zero_day_pulls_the_daily_mean_down(tmp_path):
    """The defect: dividing by active days reported therapy per *used* day."""
    archive = make_archive(
        tmp_path,
        [
            (datetime(2021, 6, 14, 22, 0), 60),     # clipped
            (datetime(2021, 6, 15, 22, 0), 600),
            # 16th: device never ran
            (datetime(2021, 6, 17, 22, 0), 600),
            (datetime(2021, 6, 18, 22, 0), 60),     # clipped
        ],
    )
    _, text = run(archive)
    # Three counted days: 10 h, 0 h, 10 h -> 6.67, not the 10.00 that
    # dividing by active days alone would give.
    assert "mean 6.67 h/day" in text
    assert "over 3 day(s)" in text


def test_a_month_divides_by_covered_days_not_active_ones():
    """June is fully covered here, so the divisor is 30 rather than 2."""
    buckets = summarise(
        records(
            (datetime(2021, 6, 1, 22, 0), 600),
            (datetime(2021, 6, 30, 22, 0), 600),
        ),
        by="month",
    )
    assert len(buckets) == 1
    assert buckets[0].days == 30
    assert buckets[0].minutes == 1200
    assert buckets[0].hours_per_day == pytest.approx(20 / 30)


def test_a_month_divides_only_by_the_days_the_window_covers():
    """A month the window merely clips counts its covered days, not all of it."""
    buckets = summarise(
        records(
            (datetime(2021, 6, 28, 22, 0), 60),
            (datetime(2021, 7, 2, 22, 0), 60),
        ),
        by="month",
    )
    assert [(b.label, b.days) for b in buckets] == [("2021-06", 3), ("2021-07", 2)]


def test_zero_days_reach_the_csv(tmp_path):
    archive = make_archive(
        tmp_path,
        [
            (datetime(2021, 6, 14, 22, 0), 60),
            (datetime(2021, 6, 16, 22, 0), 60),
        ],
    )
    _, text = run(archive, "--csv")
    rows = [line.split(",") for line in text.strip().splitlines()[1:]]
    assert [r[0] for r in rows] == ["2021-06-14", "2021-06-15", "2021-06-16"]
    assert rows[1][1] == "0" and rows[1][2] == "0"


def test_since_and_until_still_filter_zero_days(tmp_path):
    archive = make_archive(
        tmp_path,
        [
            (datetime(2021, 6, 14, 22, 0), 60),
            (datetime(2021, 6, 18, 22, 0), 60),
        ],
    )
    _, text = run(archive, "--since", "2021-06-16", "--until", "2021-06-17")
    assert "2021-06-16" in text and "2021-06-17" in text
    assert "2021-06-14" not in text and "2021-06-18" not in text


def test_calendar_day_grouping_also_materialises_zero_days():
    buckets = summarise(
        records(
            (datetime(2021, 6, 14, 22, 0), 60),
            (datetime(2021, 6, 16, 2, 0), 60),
        ),
        by="calendar-day",
    )
    assert [b.label for b in buckets] == ["2021-06-14", "2021-06-15", "2021-06-16"]


def test_an_implausible_span_is_refused_rather_than_materialised():
    """A misread timestamp must not become an allocation."""
    from prisma_vent.usage import UsageError

    with pytest.raises(UsageError, match="over the"):
        summarise(
            records(
                (datetime(1971, 1, 1, 22, 0), 60),
                (datetime(2100, 1, 1, 22, 0), 60),
            )
        )


# ---------------------------------------------------------------------------
# Conflicting copies of the same session
# ---------------------------------------------------------------------------


def _from_specs(tmp_path, specs, *, day_number, archive_date):
    """An archive whose long-term records are given explicitly."""
    from synthetic import build_statistic, build_usage_record

    start = datetime.combine(archive_date, datetime.min.time()).replace(hour=22)
    return build_day_archive(
        tmp_path,
        archive_date=archive_date,
        day_number=day_number,
        sessions=[(1, start, 60)],
        extra_members={
            "statistic.proto": build_statistic(
                records=[build_usage_record(**spec) for spec in specs],
                therapy_total=1_000_000,
            )
        },
    )


def _pair(tmp_path, first_records, second_records):
    """Two archives whose copies of the same session can be made to disagree."""
    return (
        _from_specs(
            tmp_path / "a", first_records,
            day_number=123, archive_date=date(2020, 3, 4),
        ),
        _from_specs(
            tmp_path / "b", second_records,
            day_number=479, archive_date=date(2020, 3, 5),
        ),
    )


def _spec(moment, duration, **overrides):
    spec = {"timestamp": stamp(moment), "duration": duration}
    spec.update(overrides)
    return spec


def test_identical_copies_of_a_session_merge(tmp_path):
    spec = _spec(datetime(2021, 6, 15, 22, 0), 600)
    a, b = _pair(tmp_path, [spec], [dict(spec)])
    status, text = run(a, b)
    assert status == 0
    assert "1 duplicate row(s) merged" in text


def test_the_same_session_with_a_different_duration_is_refused(tmp_path):
    when = datetime(2021, 6, 15, 22, 0)
    a, b = _pair(tmp_path, [_spec(when, 600)], [_spec(when, 601)])
    status, _ = run(a, b)
    assert status == 1


def test_a_differing_breakdown_is_refused_even_at_the_same_duration(tmp_path):
    """The totals agree; the device disagrees about which program ran."""
    when = datetime(2021, 6, 15, 22, 0)
    a, b = _pair(
        tmp_path,
        [_spec(when, 600)],
        [_spec(when, 600, by_program=[0, 600, 0])],
    )
    status, _ = run(a, b)
    assert status == 1


def test_the_conflict_is_the_same_whichever_archive_comes_first(tmp_path, capsys):
    """A first-one-wins rule would make the answer depend on argument order."""
    when = datetime(2021, 6, 15, 22, 0)
    a, b = _pair(tmp_path, [_spec(when, 600)], [_spec(when, 601)])

    status_ab, _ = run(a, b)
    forward = capsys.readouterr().err
    status_ba, _ = run(b, a)
    backward = capsys.readouterr().err

    assert status_ab == status_ba == 1
    assert forward == backward


def test_the_conflict_names_the_fields_but_not_the_values(tmp_path, capsys):
    """A diagnostic a user might paste must not carry therapy figures."""
    when = datetime(2021, 6, 15, 22, 0)
    a, b = _pair(tmp_path, [_spec(when, 777)], [_spec(when, 888)])
    run(a, b)
    err = capsys.readouterr().err
    assert "duration_minutes" in err
    assert "duration_by_program" in err          # derived fields differ too
    assert "777" not in err
    assert "888" not in err


def test_the_conflict_carries_no_session_timestamp(tmp_path, capsys):
    """The raw counter converts straight into the minute a session began.

    That is health data, and a conflict report is exactly the sort of output
    somebody pastes into a bug report. Conflicts are numbered instead.
    """
    when = datetime(2021, 6, 15, 22, 0)
    a, b = _pair(tmp_path, [_spec(when, 777)], [_spec(when, 888)])
    run(a, b)
    err = capsys.readouterr().err

    counter = stamp(when)
    assert str(counter) not in err
    assert "raw timestamp" not in err
    assert "2021-06-15" not in err and "22:00" not in err
    assert "conflict 1:" in err


def test_the_conflict_still_says_which_archives_disagree(tmp_path, capsys):
    when = datetime(2021, 6, 15, 22, 0)
    a, b = _pair(tmp_path, [_spec(when, 777)], [_spec(when, 888)])
    run(a, b)
    err = capsys.readouterr().err
    assert a.name in err and b.name in err


def test_a_conflict_stops_the_report_rather_than_averaging_around_it(tmp_path):
    when = datetime(2021, 6, 15, 22, 0)
    a, b = _pair(tmp_path, [_spec(when, 600)], [_spec(when, 601)])
    _, text = run(a, b)
    assert "mean" not in text


def test_histogram_differences_alone_are_not_a_conflict(tmp_path):
    """Documented decision: their semantics are unknown, so a difference
    between two copies cannot be judged as disagreement."""
    when = datetime(2021, 6, 15, 22, 0)
    a, b = _pair(
        tmp_path,
        [_spec(when, 600, histograms=0)],
        [_spec(when, 600, histograms=3)],
    )
    status, text = run(a, b)
    assert status == 0
    assert "1 duplicate row(s) merged" in text


# ---------------------------------------------------------------------------
# Averages across periods of unequal length
# ---------------------------------------------------------------------------


def _one_long_month_and_one_short(tmp_path):
    """January fully covered at 2 h/day; February covered for one day at 20 h."""
    sessions = [(datetime(2021, 1, day, 22, 0), 120) for day in range(1, 32)]
    sessions.append((datetime(2021, 2, 1, 22, 0), 1200))
    return make_archive(tmp_path, sessions)


def test_the_monthly_mean_is_weighted_by_covered_days(tmp_path):
    """A one-day month must not count as much as a thirty-one-day one."""
    _, text = run(_one_long_month_and_one_short(tmp_path), "--by", "month",
                  "--include-partial")
    # Weighted: (31 x 120 + 1 x 1200) / 60 / 32 = 2.56 h/day.
    # The old unweighted mean of bucket averages would have been
    # (2.00 + 20.00) / 2 = 11.00, which is what this test exists to exclude.
    assert "mean 2.56 h/day" in text
    assert "mean 11.00" not in text
    assert "over 32 day(s)" in text


def test_the_two_monthly_averages_really_do_differ(tmp_path):
    """Guards the test above: without a spread it would prove nothing."""
    buckets = summarise(
        records(*[(datetime(2021, 1, day, 22, 0), 120) for day in range(1, 32)],
                (datetime(2021, 2, 1, 22, 0), 1200)),
        by="month",
    )
    assert [b.days for b in buckets] == [31, 1]
    assert buckets[0].hours_per_day == pytest.approx(2.0)
    assert buckets[1].hours_per_day == pytest.approx(20.0)


def test_the_median_is_a_median_of_days_not_of_months(tmp_path):
    """32 days: thirty-one at 2 h and one at 20 h. The median day is 2 h."""
    _, text = run(_one_long_month_and_one_short(tmp_path), "--by", "month",
                  "--include-partial")
    assert "median 2.00 h/day" in text
    # A median of the two monthly averages would be 11.00.
    assert "median 11.00" not in text


def test_the_median_is_labelled_with_the_unit_it_carries(tmp_path):
    _, text = run(_one_long_month_and_one_short(tmp_path), "--by", "month",
                  "--include-partial")
    assert "median" in text and "h/day, over" in text


def test_day_mode_summary_is_unchanged_by_the_weighting(tmp_path):
    archive = make_archive(
        tmp_path,
        [
            (datetime(2021, 6, 14, 22, 0), 60),     # clipped
            (datetime(2021, 6, 15, 22, 0), 600),
            (datetime(2021, 6, 17, 22, 0), 600),
            (datetime(2021, 6, 18, 22, 0), 60),     # clipped
        ],
    )
    _, text = run(archive)
    assert "mean 6.67 h/day" in text
    assert "over 3 day(s)" in text


def test_only_the_clipped_days_leave_a_partial_month(tmp_path):
    """The defect: one boundary day used to discard its whole month.

    January is covered for 31 days, of which the 1st is the window's first day
    and therefore clipped. February is covered for one day, which is the
    window's last. Excluding the two boundary *days* leaves 30 complete days;
    excluding the two *months* would have left nothing at all.
    """
    archive = _one_long_month_and_one_short(tmp_path)
    _, without = run(archive, "--by", "month")
    assert "over 30 day(s)" in without
    assert "2 cut-off day(s) excluded" in without
    assert "mean 2.00 h/day" in without

    _, with_partial = run(archive, "--by", "month", "--include-partial")
    assert "over 32 day(s)" in with_partial


def test_a_fully_covered_month_between_two_clipped_ones_is_counted(tmp_path):
    sessions = [(datetime(2021, 1, 31, 22, 0), 60)]
    sessions += [(datetime(2021, 2, day, 22, 0), 120) for day in range(1, 29)]
    sessions.append((datetime(2021, 3, 1, 22, 0), 60))
    _, text = run(make_archive(tmp_path, sessions), "--by", "month")
    assert "over 28 day(s)" in text
    assert "mean 2.00 h/day" in text


# ---------------------------------------------------------------------------
# A clipped day must not cost its month
# ---------------------------------------------------------------------------


def _three_months(tmp_path):
    """Jan clipped at the start, Feb whole, Mar clipped at the end.

    January: the 20th is the window's first day (1 h), then 11 full days at
    2 h. February: 28 full days at 3 h. March: two full days at 4 h and the
    3rd is the window's last day (5 h).
    """
    sessions = [(datetime(2021, 1, 20, 22, 0), 60)]
    sessions += [(datetime(2021, 1, day, 22, 0), 120) for day in range(21, 32)]
    sessions += [(datetime(2021, 2, day, 22, 0), 180) for day in range(1, 29)]
    sessions += [(datetime(2021, 3, day, 22, 0), 240) for day in range(1, 3)]
    sessions.append((datetime(2021, 3, 3, 22, 0), 300))
    return make_archive(tmp_path, sessions)


def test_a_clipped_first_day_does_not_remove_its_month(tmp_path):
    _, text = run(_three_months(tmp_path), "--by", "month")
    # 11 January days + 28 February + 2 March = 41 complete days.
    assert "over 41 day(s)" in text
    assert "2 cut-off day(s) excluded" in text


def test_the_complete_days_of_a_partial_month_still_count(tmp_path):
    buckets = summarise(
        records(
            *[(datetime(2021, 1, day, 22, 0), 120) for day in range(20, 32)],
        ),
        by="month",
    )
    january = buckets[0]
    assert january.partial                      # it holds boundary days
    assert january.days == 12
    assert sum(not d.partial for d in january.days_covered) == 10


def test_exactly_the_two_boundary_days_are_dropped(tmp_path):
    from prisma_vent.usage import _load
    import sys as _sys

    archive = _three_months(tmp_path)
    recs, _, _, _ = _load([archive], _sys.stdout)
    buckets = summarise(recs, by="month")
    days = [d for b in buckets for d in b.days_covered]
    assert sum(d.partial for d in days) == 2
    assert [d.date.isoformat() for d in days if d.partial] == [
        "2021-01-20",
        "2021-03-03",
    ]


def test_a_fully_covered_middle_month_is_untouched(tmp_path):
    _, text = run(_three_months(tmp_path), "--by", "month")
    assert "2021-02   3.00 h/day  28d" in text


def test_the_month_row_still_shows_every_day_it_holds(tmp_path):
    """The row is the record; only the summary excludes the clipped days."""
    _, text = run(_three_months(tmp_path), "--by", "month")
    january = [line for line in text.splitlines() if "2021-01" in line][0]
    assert "12d" in january
    assert "partial" in january


def test_the_message_does_not_claim_whole_periods_were_excluded(tmp_path):
    _, text = run(_three_months(tmp_path), "--by", "month")
    assert "cut-off period(s) excluded" not in text
    assert "still shown in full, marked" in text


def test_the_weighted_mean_uses_only_the_counted_days(tmp_path):
    _, text = run(_three_months(tmp_path), "--by", "month")
    # (11 x 2 + 28 x 3 + 2 x 4) / 41 = 114/41 = 2.78 h/day.
    assert "mean 2.78 h/day" in text


def test_the_median_is_over_the_counted_days_too(tmp_path):
    _, text = run(_three_months(tmp_path), "--by", "month")
    # 41 days: eleven 2 h, twenty-eight 3 h, two 4 h. The 21st is 3 h.
    assert "median 3.00 h/day" in text


def test_include_partial_adds_back_exactly_those_two_days(tmp_path):
    _, text = run(_three_months(tmp_path), "--by", "month", "--include-partial")
    assert "over 43 day(s)" in text
    assert "excluded" not in text


def test_day_mode_keeps_its_behaviour(tmp_path):
    """Excluding clipped days is what day mode always did."""
    _, text = run(_three_months(tmp_path))
    assert "over 41 day(s)" in text
    assert "2 cut-off day(s) excluded" in text


def test_since_and_until_do_not_change_which_days_are_clipped(tmp_path):
    """Partial belongs to the window, not to what the user chose to display."""
    archive = _three_months(tmp_path)
    _, text = run(archive, "--by", "month", "--since", "2021-02-01")
    february = [line for line in text.splitlines() if "2021-02" in line][0]
    assert "partial" not in february
    march = [line for line in text.splitlines() if "2021-03" in line][0]
    assert "partial" in march


# ---------------------------------------------------------------------------
# Date bounds are day bounds, in every mode
# ---------------------------------------------------------------------------


def _february(tmp_path):
    """A window covering 2021-01-31 to 2021-03-01, February at 2 h a day."""
    sessions = [(datetime(2021, 1, 31, 22, 0), 60)]
    sessions += [(datetime(2021, 2, day, 22, 0), 120) for day in range(1, 29)]
    sessions.append((datetime(2021, 3, 1, 22, 0), 60))
    return make_archive(tmp_path, sessions)


def test_a_month_is_trimmed_to_the_days_asked_for(tmp_path):
    """The defect: --since mid-month returned the whole month's total."""
    _, text = run(_february(tmp_path), "--by", "month",
                  "--since", "2021-02-15", "--until", "2021-02-20")
    february = [line for line in text.splitlines() if "2021-02" in line][0]
    assert "6d" in february                       # the 15th to the 20th
    assert "over 6 day(s)" in text
    assert "2021-01" not in text and "2021-03" not in text


def test_the_trimmed_month_reports_only_the_remaining_minutes(tmp_path):
    buckets = summarise(
        records(*[(datetime(2021, 2, day, 22, 0), 120) for day in range(1, 29)]),
        by="month",
        since=date(2021, 2, 15),
        until=date(2021, 2, 20),
    )
    assert len(buckets) == 1
    assert buckets[0].days == 6
    assert buckets[0].minutes == 6 * 120
    assert buckets[0].sessions == 6


def test_both_bounds_are_inclusive(tmp_path):
    buckets = summarise(
        records(*[(datetime(2021, 2, day, 22, 0), 120) for day in range(1, 29)]),
        since=date(2021, 2, 10),
        until=date(2021, 2, 12),
    )
    assert [b.label for b in buckets] == ["2021-02-10", "2021-02-11", "2021-02-12"]


def test_bounds_spanning_several_months_trim_both_ends(tmp_path):
    sessions = [(datetime(2021, 1, day, 22, 0), 60) for day in range(1, 32)]
    sessions += [(datetime(2021, 2, day, 22, 0), 120) for day in range(1, 29)]
    sessions += [(datetime(2021, 3, day, 22, 0), 180) for day in range(1, 32)]
    _, text = run(make_archive(tmp_path, sessions), "--by", "month",
                  "--since", "2021-01-20", "--until", "2021-03-10")
    assert "over 50 day(s)" in text          # 12 + 28 + 10
    january = [line for line in text.splitlines() if "2021-01" in line][0]
    march = [line for line in text.splitlines() if "2021-03" in line][0]
    assert "12d" in january and "10d" in march


def test_a_month_with_no_remaining_days_disappears(tmp_path):
    _, text = run(_february(tmp_path), "--by", "month", "--since", "2021-02-01")
    assert "2021-01" not in text


def test_the_summary_uses_only_the_filtered_days(tmp_path):
    sessions = [(datetime(2021, 2, day, 22, 0), 60) for day in range(1, 15)]
    sessions += [(datetime(2021, 2, day, 22, 0), 600) for day in range(15, 29)]
    _, text = run(make_archive(tmp_path, sessions), "--by", "month",
                  "--since", "2021-02-15", "--until", "2021-02-27")
    # Thirteen days at 10 h; the 1 h days are outside the bounds.
    assert "mean 10.00 h/day" in text
    assert "median 10.00 h/day" in text
    assert "over 13 day(s)" in text


def test_the_csv_matches_the_table_after_filtering(tmp_path):
    archive = _february(tmp_path)
    _, table = run(archive, "--by", "month", "--since", "2021-02-15",
                   "--until", "2021-02-20")
    _, csv_text = run(archive, "--by", "month", "--since", "2021-02-15",
                      "--until", "2021-02-20", "--csv")
    row = csv_text.strip().splitlines()[1].split(",")
    assert row[0] == "2021-02"
    assert row[3] == "6"                    # days
    assert "6d" in table


def test_a_filter_edge_inside_the_window_is_not_partial(tmp_path):
    """Partial means the rolling window clipped the day, not that a reader
    chose to start there."""
    buckets = summarise(
        records(*[(datetime(2021, 2, day, 22, 0), 120) for day in range(1, 29)]),
        since=date(2021, 2, 10),
        until=date(2021, 2, 12),
    )
    assert not any(b.partial for b in buckets)


def test_a_genuinely_clipped_day_stays_partial_through_the_filter(tmp_path):
    buckets = summarise(
        records(*[(datetime(2021, 2, day, 22, 0), 120) for day in range(1, 29)]),
        since=date(2021, 2, 1),
        until=date(2021, 2, 3),
    )
    assert [b.partial for b in buckets] == [True, False, False]


def test_calendar_day_mode_filters_the_same_way(tmp_path):
    buckets = summarise(
        records(
            (datetime(2021, 6, 14, 22, 0), 60),
            (datetime(2021, 6, 15, 22, 0), 60),
            (datetime(2021, 6, 16, 2, 0), 60),
        ),
        by="calendar-day",
        since=date(2021, 6, 15),
    )
    assert [b.label for b in buckets] == ["2021-06-15", "2021-06-16"]


def test_since_after_until_is_a_usage_error(tmp_path, capsys):
    """An empty result would look like an answer; this is a bad question."""
    from prisma_vent.usage import EXIT_USAGE

    archive = _february(tmp_path)
    status, _ = run(archive, "--since", "2021-02-20", "--until", "2021-02-10")
    assert status == EXIT_USAGE
    err = capsys.readouterr().err
    assert "is after" in err and "inclusive" in err


def test_bounds_that_exclude_everything_are_reported(tmp_path):
    archive = _february(tmp_path)
    status, _ = run(archive, "--since", "2030-01-01")
    assert status == 1
