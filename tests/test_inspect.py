"""Tests for the read-only inspection tool.

Archives are built at runtime. The tool is checked for what it prints and,
just as importantly, for what it refuses to print.
"""

import io
from datetime import date, datetime

import pytest

from prisma_vent.inspect import EXIT_OK, EXIT_STRUCTURAL, main
from synthetic import build_day_archive

ARCHIVE_DATE = date(2020, 3, 4)


@pytest.fixture
def archive(tmp_path):
    return build_day_archive(
        tmp_path,
        archive_date=ARCHIVE_DATE,
        sessions=[
            (1, datetime(2020, 3, 4, 22, 0, 0), 60),
            (2, datetime(2020, 3, 5, 1, 30, 0), 30),
        ],
    )


def run(*args) -> tuple[int, str]:
    out = io.StringIO()
    status = main([str(a) for a in args], out=out)
    return status, out.getvalue()


# --------------------------------------------------------------------------
# What it prints
# --------------------------------------------------------------------------


def test_reports_the_archive_and_its_sessions(archive):
    status, text = run(archive)
    assert status == EXIT_OK
    assert archive.name in text
    assert "therapy day     2020-03-04" in text
    assert "sessions        2" in text
    assert "session 0001" in text and "session 0002" in text


def test_reports_the_archive_hash(archive):
    import hashlib

    status, text = run(archive)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() in text


def test_times_are_labelled_as_the_device_clock(archive):
    """The zone is unresolved, so the output must not read as a normal time."""
    _, text = run(archive)
    assert "(device clock)" in text


def test_channel_table_lists_every_signal(archive):
    _, text = run(archive)
    assert "Pressure" in text and "Phase" in text
    assert "hPa" in text


def test_statistics_are_shown_in_physical_units(archive):
    _, text = run(archive)
    assert "observed (physical)" in text
    assert "0..65" in text  # the pressure channel spans its full declared range


def test_no_stats_skips_the_measurement_pass(archive):
    _, text = run("--no-stats", archive)
    assert "observed (physical)" not in text
    assert "Pressure" in text  # the table is still printed


def test_sample_preview_shows_raw_beside_physical(archive):
    """Both representations, because that is what makes a value attributable.

    A plausible physical number over an implausible raw one points at the
    scaling; the reverse points at the bytes.
    """
    _, text = run("--samples", "3", archive)
    assert "digital / physical" in text
    assert "0/0" in text


def test_day_level_files_are_summarised(archive):
    _, text = run(archive)
    assert "recording" in text
    assert "alarms" in text
    assert "parameter map" in text
    assert "snapshot" in text


def test_recording_windows_are_not_called_sessions(archive):
    # One window can hold several sessions, so counting windows undercounts.
    _, text = run(archive)
    assert "windows are device recordings, not sessions" in text


def test_active_program_is_shown_raw_and_flagged_as_unresolved(archive):
    _, text = run(archive)
    assert "ActiveProgram raw" in text
    assert "0- or 1-based is unknown" in text


def test_several_archives_are_reported_in_turn(tmp_path):
    first = build_day_archive(tmp_path, archive_date=ARCHIVE_DATE, day_number=123)
    # The session must fall in the therapy day its archive is named for; the
    # default fixture session sits on 4 March.
    second = build_day_archive(
        tmp_path,
        archive_date=date(2020, 3, 5),
        day_number=479,
        sessions=[(1, datetime(2020, 3, 5, 22, 0, 0), 60)],
    )
    status, text = run(first, second)
    assert status == EXIT_OK
    assert first.name in text and second.name in text


# --------------------------------------------------------------------------
# What it refuses to print
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "word",
    ["apnoea", "apnea", "hypopnoea", "hypopnea", "snore", "rera", "leakage event"],
)
def test_no_event_is_ever_named(archive, word):
    """Ids are unidentified, so a named column would be a guess in disguise."""
    _, text = run("--samples", "2", archive)
    assert word not in text.lower()


def test_event_ids_are_reported_as_ids(archive):
    _, text = run(archive)
    assert "by id:" in text
    assert "ids are unidentified; no event is named" in text


# --------------------------------------------------------------------------
# Failure behaviour
# --------------------------------------------------------------------------


def test_missing_file_reports_and_exits_non_zero(tmp_path, capsys):
    status, text = run(tmp_path / "0123_2020-03-04.zip")
    assert status == EXIT_STRUCTURAL
    assert text == ""
    assert "no such file" in capsys.readouterr().err


def test_unreadable_archive_reports_and_exits_non_zero(tmp_path, capsys):
    bad = tmp_path / "0123_2020-03-04.zip"
    bad.write_bytes(b"not a zip")
    status, _ = run(bad)
    assert status == EXIT_STRUCTURAL
    assert "not a readable ZIP" in capsys.readouterr().err


def test_one_bad_archive_does_not_stop_the_others(tmp_path, capsys):
    good = build_day_archive(tmp_path, archive_date=ARCHIVE_DATE, day_number=123)
    bad = tmp_path / "0479_2020-03-05.zip"
    bad.write_bytes(b"not a zip")
    status, text = run(bad, good)
    assert status == EXIT_STRUCTURAL  # the failure is still reported
    assert good.name in text  # but the readable one was still inspected


def test_negative_sample_count_is_a_usage_error(archive):
    with pytest.raises(SystemExit) as excinfo:
        run("--samples", "-1", archive)
    assert excinfo.value.code == 2


# --------------------------------------------------------------------------
# Trend curves
# --------------------------------------------------------------------------


def test_trend_curve_is_summarised(tmp_path):
    from synthetic import build_trend_curve

    path = build_trend_curve(tmp_path, day=date(2020, 1, 1), day_number=123,
                             populated=30, padding=5)
    status, text = run(path)
    assert status == EXIT_OK
    assert "therapy day     2020-01-01" in text
    assert "30 populated" in text
    assert "1:00:00" in text  # 30 records of two minutes


def test_trend_curve_estimate_is_labelled_as_one(tmp_path):
    """An estimate must not read like a count.

    A day archive counts therapy time exactly; this derives it. Merging the
    two would lose that distinction silently.
    """
    from synthetic import build_trend_curve

    path = build_trend_curve(tmp_path, day=date(2020, 1, 1), populated=5)
    _, text = run(path)
    assert "**estimated**" in text
    assert "counts this exactly" in text
    assert "separate readings" in text


def test_trend_curve_output_never_prints_the_serial(tmp_path):
    from prisma_vent.trendcurve import read_trend_curve
    from synthetic import build_trend_curve

    path = build_trend_curve(tmp_path, day=date(2020, 1, 1))
    serial = read_trend_curve(path).serial
    _, text = run(path)
    assert serial and serial not in text


def test_trend_curve_says_the_records_are_not_decoded(tmp_path):
    from synthetic import build_trend_curve

    path = build_trend_curve(tmp_path, day=date(2020, 1, 1))
    _, text = run(path)
    assert "not decoded" in text


def test_a_broken_trend_curve_reports_and_exits_non_zero(tmp_path, capsys):
    path = tmp_path / "0123_2020-01-01.tc"
    path.write_bytes(b"not a trend curve")
    status, _ = run(path)
    assert status == EXIT_STRUCTURAL
    assert "JSON header" in capsys.readouterr().err


# --------------------------------------------------------------------------
# The long-term record
# --------------------------------------------------------------------------


def _archive_with_long_term(tmp_path, **statistic_kwargs):
    from synthetic import build_statistic, build_usage_record
    from prisma_vent.statistic import EPOCH_OFFSET

    start = datetime(2020, 3, 4, 22, 0, 0)
    counter = int((start - datetime(1970, 1, 1) - EPOCH_OFFSET).total_seconds())
    day = 86_400
    kwargs = {
        "records": [
            build_usage_record(timestamp=counter - 10 * day, duration=300),
            build_usage_record(timestamp=counter, duration=60),
        ],
        "therapy_total": 500_000,
        "other_total": 900_000,
        "configuration": None,
    }
    kwargs.update(statistic_kwargs)
    return build_day_archive(
        tmp_path,
        archive_date=ARCHIVE_DATE,
        sessions=[(1, start, 3600)],
        extra_members={"statistic.proto": build_statistic(**kwargs)},
    )


def test_the_long_term_record_is_summarised(tmp_path):
    _, text = run(_archive_with_long_term(tmp_path))
    assert "long-term record" in text
    assert "2 spanning 10 days" in text


def test_the_lifetime_total_is_shown_in_hours_as_well(tmp_path):
    _, text = run(_archive_with_long_term(tmp_path))
    assert "500000 min = 8333.3 h" in text


def test_the_unidentified_counter_says_it_is_unidentified(tmp_path):
    """It looks like minutes and is not established as minutes of anything."""
    _, text = run(_archive_with_long_term(tmp_path))
    assert "900000" in text
    assert "unidentified" in text


def test_the_firmware_version_is_marked_inferred(tmp_path):
    """Nothing in the file states it, so the report must not promise more."""
    from synthetic import example_configuration

    _, text = run(
        _archive_with_long_term(tmp_path, configuration=example_configuration())
    )
    assert "6.3.0" in text
    assert "inferred from position, not stated anywhere in the file" in text


def test_times_from_the_long_term_record_are_labelled_device_local(tmp_path):
    _, text = run(_archive_with_long_term(tmp_path))
    assert text.count("(device local)") >= 2


def test_an_archive_without_the_member_says_so_and_still_reports_the_day(tmp_path):
    """A firmware need not write it; that must not abort the rest of the report."""
    archive = build_day_archive(
        tmp_path,
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 60)],
    )
    status, text = run(archive)
    assert status == EXIT_OK
    assert "long-term record unavailable" in text
    assert "sessions" in text


def test_an_unreadable_member_is_reported_rather_than_raised(tmp_path):
    archive = build_day_archive(
        tmp_path,
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 60)],
        extra_members={"statistic.proto": b"\xff\xff\xff\xff\xff\xff"},
    )
    status, text = run(archive)
    assert status == EXIT_OK
    assert "long-term record unavailable" in text
