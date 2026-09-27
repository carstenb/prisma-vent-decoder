"""Tests for reading a day archive.

Archives are built at runtime in a temporary directory. Nothing here is a real
recording, and no archive file is committed.
"""

import hashlib
import zipfile
from datetime import date, datetime, timedelta

import pytest

from prisma_vent.archive import ArchiveError, ArchiveLimits, open_day_archive
from prisma_vent.wmedf import iter_chunks
from synthetic import build_day_archive, session_event_xml, session_wmedf

ARCHIVE_DATE = date(2020, 3, 4)


@pytest.fixture
def one_session(tmp_path):
    return build_day_archive(
        tmp_path,
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 120)],
    )


# --------------------------------------------------------------------------
# Identity and pairing
# --------------------------------------------------------------------------


def test_archive_name_supplies_date_and_day_number(one_session):
    with open_day_archive(one_session) as archive:
        assert archive.archive_date == ARCHIVE_DATE
        assert archive.day_number == 123


def test_badly_named_archive_is_refused(tmp_path):
    bad = tmp_path / "recording.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("0001.wmedf", b"x")
    with pytest.raises(ArchiveError, match="NNNN_YYYY-MM-DD"):
        with open_day_archive(bad):
            pass


def test_sessions_are_paired(one_session):
    with open_day_archive(one_session) as archive:
        assert len(archive.sessions) == 1
        session = archive.sessions[0]
        assert session.number == 1
        assert session.signal_member == "0001.wmedf"
        assert session.event_member == "event_0001.xml"


def test_several_sessions_are_ordered(tmp_path):
    path = build_day_archive(
        tmp_path,
        archive_date=ARCHIVE_DATE,
        sessions=[
            (3, datetime(2020, 3, 5, 3, 0, 0), 60),
            (1, datetime(2020, 3, 4, 22, 0, 0), 60),
            (2, datetime(2020, 3, 5, 1, 0, 0), 60),
        ],
    )
    with open_day_archive(path) as archive:
        assert [s.number for s in archive.sessions] == [1, 2, 3]


def test_signal_without_its_event_file_is_refused(tmp_path):
    path = build_day_archive(
        tmp_path, archive_date=ARCHIVE_DATE, omit={"event_0001.xml"}
    )
    with pytest.raises(ArchiveError, match="do not pair up"):
        with open_day_archive(path):
            pass


def test_event_file_without_its_signal_is_refused(tmp_path):
    path = build_day_archive(tmp_path, archive_date=ARCHIVE_DATE, omit={"0001.wmedf"})
    with pytest.raises(ArchiveError, match="do not pair up"):
        with open_day_archive(path):
            pass


@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_duplicate_session_number_is_refused(tmp_path):
    """A ZIP may legally hold two members with the same name.

    Which one a reader gets is then implementation-defined, so an ambiguous
    session number is refused rather than silently resolved.
    """
    path = tmp_path / "0123_2020-03-04.zip"
    start = datetime(2020, 3, 4, 22, 0, 0)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", session_wmedf(start, 60))
        zf.writestr("0001.wmedf", session_wmedf(start, 90))
        zf.writestr("event_0001.xml", session_event_xml(start, 60))
    # The duplicate-member check fires first now, which is the stronger
    # guarantee: it rejects any repeated name, not only a session file.
    with pytest.raises(ArchiveError, match="more than one member named"):
        with open_day_archive(path):
            pass


# --------------------------------------------------------------------------
# Time resolution — the reason this module exists
# --------------------------------------------------------------------------


def test_session_before_midnight_resolves_to_the_archive_date(one_session):
    with open_day_archive(one_session) as archive:
        session = archive.sessions[0]
        assert session.start.value == datetime(2020, 3, 4, 22, 0, 0)
        assert session.stop.value == datetime(2020, 3, 4, 22, 2, 0)


def test_session_after_midnight_resolves_to_the_following_day(tmp_path):
    """The case a single time reference gets wrong by exactly one day.

    The session file counts from midnight of 5 March even though the archive
    is dated 4 March. Resolving against the archive's midnight would place
    this session a full day early — on a night that looks entirely ordinary.
    """
    path = build_day_archive(
        tmp_path,
        archive_date=ARCHIVE_DATE,
        sessions=[(2, datetime(2020, 3, 5, 0, 35, 0), 300)],
    )
    with open_day_archive(path) as archive:
        session = archive.sessions[0]
        assert session.start.value == datetime(2020, 3, 5, 0, 35, 0)
        assert session.start.value.date() != archive.archive_date


def test_a_night_stays_on_one_therapy_day(tmp_path):
    path = build_day_archive(
        tmp_path,
        archive_date=ARCHIVE_DATE,
        sessions=[
            (1, datetime(2020, 3, 4, 22, 30, 0), 60),
            (2, datetime(2020, 3, 5, 3, 15, 0), 60),
        ],
    )
    with open_day_archive(path) as archive:
        days = {s.therapy_day for s in archive.sessions}
        assert days == {ARCHIVE_DATE}


def test_duration_is_derived_from_the_resolved_instants(one_session):
    with open_day_archive(one_session) as archive:
        assert archive.sessions[0].duration == timedelta(seconds=120)


def test_a_start_disagreement_is_reported_not_refused(tmp_path):
    """An hour of disagreement is surfaced, and does not reject the session.

    It used to raise. Nothing in the format or in the manufacturer's figures
    bounds how far the two subsystems' timestamps may sit apart, so a bound
    here could only ever have been a number somebody chose — and a chosen
    number rejects correct data above it just as readily as it accepts
    mispaired data below it. The difference is measured and handed to the
    caller instead.
    """
    path = tmp_path / "0123_2020-03-04.zip"
    start = datetime(2020, 3, 4, 22, 0, 0)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", session_wmedf(start, 60))
        # Event file claims a start one hour later.
        zf.writestr(
            "event_0001.xml", session_event_xml(start + timedelta(hours=1), 60)
        )
    with open_day_archive(path) as archive:
        assert archive.sessions[0].start_skew == timedelta(hours=1)


def test_sub_second_difference_is_tolerated(tmp_path):
    # The XML carries milliseconds the header cannot express; that difference
    # is the format, not an error.
    path = tmp_path / "0123_2020-03-04.zip"
    start = datetime(2020, 3, 4, 22, 0, 0)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", session_wmedf(start, 60))
        zf.writestr(
            "event_0001.xml",
            session_event_xml(start + timedelta(milliseconds=945), 60),
        )
    with open_day_archive(path) as archive:
        assert archive.sessions[0].start.value.replace(microsecond=0) == start


# --------------------------------------------------------------------------
# Untrusted names and sizes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,message",
    [
        ("/etc/passwd", "absolute path"),
        ("../escape.txt", "traverses directories"),
        ("logs/../../escape.txt", "traverses directories"),
        ("windows\\path.txt", "backslash"),
    ],
)
def test_unsafe_member_names_are_refused(tmp_path, name, message):
    path = build_day_archive(
        tmp_path, archive_date=ARCHIVE_DATE, extra_members={name: b"x"}
    )
    with pytest.raises(ArchiveError, match=message):
        with open_day_archive(path):
            pass


def test_too_many_members_is_refused(tmp_path):
    extra = {f"logs/file{i}.log": b"x" for i in range(20)}
    path = build_day_archive(
        tmp_path, archive_date=ARCHIVE_DATE, extra_members=extra
    )
    with pytest.raises(ArchiveError, match="over the 10 allowed"):
        with open_day_archive(path, limits=ArchiveLimits(max_members=10)):
            pass


def test_oversized_member_is_refused(tmp_path):
    path = build_day_archive(
        tmp_path, archive_date=ARCHIVE_DATE, extra_members={"logs/big.log": b"x" * 5000}
    )
    with pytest.raises(ArchiveError, match="over the 1000 allowed"):
        with open_day_archive(path, limits=ArchiveLimits(max_member_bytes=1000)):
            pass


def test_oversized_total_is_refused(tmp_path):
    extra = {f"logs/f{i}.log": b"x" * 900 for i in range(6)}
    path = build_day_archive(
        tmp_path, archive_date=ARCHIVE_DATE, extra_members=extra
    )
    # Each member stays under its own limit; the sum is what trips.
    with pytest.raises(ArchiveError, match="exceeds 4000 bytes"):
        with open_day_archive(
            path, limits=ArchiveLimits(max_member_bytes=3000, max_total_bytes=4000)
        ):
            pass


def test_unknown_members_are_reported_not_rejected(tmp_path):
    """A later firmware adding a file should be visible, not silently dropped.

    Unknown is not the same as unsafe: the name is still checked, but an
    unrecognised safe member is surfaced rather than treated as fatal.
    """
    path = build_day_archive(
        tmp_path, archive_date=ARCHIVE_DATE, extra_members={"newthing.dat": b"x"}
    )
    with open_day_archive(path) as archive:
        assert archive.unknown_members == ("newthing.dat",)
        assert len(archive.sessions) == 1


def test_not_a_zip_is_refused(tmp_path):
    path = tmp_path / "0123_2020-03-04.zip"
    path.write_bytes(b"not a zip at all")
    with pytest.raises(ArchiveError, match="not a readable ZIP"):
        with open_day_archive(path):
            pass


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


def test_hash_is_of_the_original_archive_bytes(one_session):
    expected = hashlib.sha256(one_session.read_bytes()).hexdigest()
    with open_day_archive(one_session) as archive:
        assert archive.sha256 == expected
        assert archive.sessions[0].provenance.archive_sha256 == expected


def test_provenance_names_the_member_and_session(one_session):
    with open_day_archive(one_session) as archive:
        p = archive.sessions[0].provenance
        assert p.member_name == "0001.wmedf"
        assert p.session_number == 1
        assert p.archive_name == one_session.name


def test_adapter_version_is_recorded_and_may_lack_a_commit(one_session):
    with open_day_archive(one_session) as archive:
        adapter = archive.sessions[0].provenance.adapter
        assert adapter.package_version
        assert adapter.decoder_schema_version >= 1
        # Absent outside a built distribution, and consumers must cope.
        assert adapter.git_commit is None or isinstance(adapter.git_commit, str)


def test_identity_excludes_the_adapter_version(one_session):
    """Re-reading with a newer decoder must replace, not duplicate.

    Including the version in the key would turn a controlled reprocess into a
    second copy of every row.
    """
    with open_day_archive(one_session) as archive:
        p = archive.sessions[0].provenance
        key = p.identity("record-7")
        assert p.archive_sha256 in key
        assert p.member_name in key
        assert "record-7" in key
        assert str(p.adapter.package_version) not in key.split(":")[-1]


# --------------------------------------------------------------------------
# Reading through to the samples
# --------------------------------------------------------------------------


def test_signal_streams_straight_out_of_the_archive(one_session):
    with open_day_archive(one_session) as archive:
        session = archive.sessions[0]
        with archive.open_signal(session) as stream:
            from prisma_vent.wmedf import read_header

            header = read_header(stream)
            total = sum(len(c.digital) for c in iter_chunks(stream, header))
        assert header.n_records == 120
        assert total == 120 * 11  # 10 samples + 1 sample per record


def test_day_level_files_are_readable(one_session):
    with open_day_archive(one_session) as archive:
        assert archive.day_log().date == ARCHIVE_DATE
        assert archive.alarm_log().alarms[0].id == 794
        pmap = archive.parameter_map()
        assert pmap.names[69] == "IPAP"
        log = archive.parameter_log(pmap)
        assert log.snapshots[0].active_program_raw.value == "0"


def test_missing_day_member_is_refused(tmp_path):
    path = build_day_archive(tmp_path, archive_date=ARCHIVE_DATE, day_members=False)
    with open_day_archive(path) as archive:
        with pytest.raises(ArchiveError, match="no member 'event.xml'"):
            archive.day_log()


# --------------------------------------------------------------------------
# Time-base behaviour the synthetic fixtures did not originally cover
# --------------------------------------------------------------------------


def test_session_crossing_midnight_resolves_its_stop_to_the_next_day(tmp_path):
    """The stop offset counts from midnight of the day the session ended.

    Resolving it against the start's midnight puts the stop a full day early
    and yields a negative duration. No synthetic fixture showed this until one
    was built to cross midnight, which is why one is built here.
    """
    path = build_day_archive(
        tmp_path,
        archive_date=ARCHIVE_DATE,
        sessions=[(4, datetime(2020, 3, 4, 23, 38, 40), 27170)],
    )
    with open_day_archive(path) as archive:
        session = archive.sessions[0]
        assert session.start.value == datetime(2020, 3, 4, 23, 38, 40)
        assert session.stop.value == datetime(2020, 3, 5, 7, 11, 30)
        assert session.duration == timedelta(seconds=27170)


def test_duration_discrepancy_is_reported_not_bounded(tmp_path):
    """How far a span may drift from the record count is not established.

    Nothing in the format or in the manufacturer's figures bounds it, and a
    tolerance invented here would either reject correct sessions or accept
    wrong ones. The discrepancy is exposed for a caller to survey instead.
    """
    path = build_day_archive(
        tmp_path,
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 120)],
    )
    with open_day_archive(path) as archive:
        session = archive.sessions[0]
        assert session.accounted_duration == timedelta(seconds=120)
        assert session.duration_discrepancy == timedelta(0)


#: The signal header's start in every skew fixture below.
_SKEW_HEADER_START = datetime(2020, 3, 4, 22, 0, 0)


def _skewed_archive(tmp_path, skew: timedelta, *, name="0123_2020-03-04.zip"):
    """A session whose event file's *offset* sits `skew` from the header start.

    The offset is written explicitly rather than by handing a shifted datetime
    to :func:`session_event_xml`. That helper recomputes the offset from
    midnight of whatever day it is given, so shifting the datetime by a whole
    day produces the *same* offset and no skew at all — and shifting it by a
    few hours changes which therapy day the session lands in, which is a
    different failure from the one under test.
    """
    from synthetic import _offset_text

    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / name
    midnight = datetime(
        _SKEW_HEADER_START.year, _SKEW_HEADER_START.month, _SKEW_HEADER_START.day
    )
    begin = _offset_text(_SKEW_HEADER_START - midnight + skew)
    end = _offset_text(_SKEW_HEADER_START - midnight + skew + timedelta(seconds=60))
    event = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<desc><start time="{begin}"/>'
        '<RespEvent time="1" id="6" event="begin" Strength="1"/>'
        f'<stop time="{end}"/></desc>'
    ).encode("utf-8")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", session_wmedf(_SKEW_HEADER_START, 60))
        zf.writestr("event_0001.xml", event)
    return path


def test_start_skew_within_the_bound_is_accepted(tmp_path):
    """The header truncates to whole seconds; the XML keeps milliseconds.

    A session straddling a second boundary therefore has an XML start after
    its header start, and demanding equality after truncation would refuse it.
    """
    path = _skewed_archive(tmp_path, timedelta(milliseconds=1010))
    with open_day_archive(path) as archive:
        assert archive.sessions[0].start.value.date() == date(2020, 3, 4)


def test_no_start_skew_bound_exists_any_more(tmp_path):
    """Stated as its own test, because the absence is the decision.

    Two bounds have been withdrawn from this module. The first, 2 s, had no
    public derivation at all. The second, 60 s, was justified as "the coarsest
    quantum below a day" — but this format works in seconds and milliseconds
    as well, so a minute is not one of its invariants and the number was
    chosen rather than derived.

    Neither is described here in terms of the data it was set against. A
    withdrawn bound stated as "just above what was measured" still publishes
    that measurement, which is the thing being withdrawn.
    """
    import prisma_vent.archive as archive_module

    assert not hasattr(archive_module, "_MAX_START_SKEW")


@pytest.mark.parametrize(
    "skew",
    [
        timedelta(0),
        timedelta(milliseconds=1),
        timedelta(milliseconds=1010),
        timedelta(seconds=59, milliseconds=999),
        timedelta(seconds=60),
        timedelta(minutes=5),
        timedelta(hours=8),
        timedelta(seconds=-1),
        timedelta(minutes=-7),
    ],
    ids=["zero", "1ms", "just-over-1s", "just-under-60s", "exactly-60s",
         "5min", "8h", "negative-1s", "negative-7min"],
)
def test_any_start_skew_is_accepted_and_reported_verbatim(tmp_path, skew):
    """Including negative skew, which the old bound refused outright.

    Truncation cannot produce a negative difference, so it was argued that a
    negative one proves a mispairing. It does not: it proves only that the
    event file's subsystem stamped its start before the header's did, and
    nothing published says it cannot. The value is reported so a caller who
    has grounds to judge it can.
    """
    path = _skewed_archive(tmp_path, skew)
    with open_day_archive(path) as archive:
        session = archive.sessions[0]
    assert session.start_skew == skew
    assert session.start.value == _SKEW_HEADER_START + skew


def test_the_therapy_day_is_what_still_rejects_a_wrong_reference(tmp_path):
    """The structural invariant that survives, and needs no chosen number.

    A start resolved against the wrong reference midnight moves the session by
    a whole day, which puts it in a therapy day the archive is not named for.
    That is the failure the skew bound was really aimed at, and it is caught
    without bounding anything.
    """
    # +14 h from a 22:00 header start lands at 12:00 the next day, which is
    # the first instant of the *next* noon-to-noon therapy day.
    path = _skewed_archive(tmp_path, timedelta(hours=14))
    with pytest.raises(ArchiveError, match="falls in therapy day"):
        with open_day_archive(path):
            pass


def test_the_skew_is_zero_when_the_two_agree(tmp_path):
    """The ordinary case still reads as zero rather than as a small artefact."""
    path = _skewed_archive(tmp_path, timedelta(0))
    with open_day_archive(path) as archive:
        assert archive.sessions[0].start_skew == timedelta(0)


# --------------------------------------------------------------------------
# Review hardening
# --------------------------------------------------------------------------


def test_empty_session_with_equal_offsets_has_zero_duration(tmp_path):
    """A session that recorded nothing lasted no time.

    Treating equal offsets as a midnight crossing would turn that into a
    plausible twenty-four hours — and exactly twenty-four hours is what a
    wrongly applied day correction always produces.
    """
    path = tmp_path / "0123_2020-03-04.zip"
    start = datetime(2020, 3, 4, 22, 0, 0)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", session_wmedf(start, 0))
        zf.writestr("event_0001.xml", session_event_xml(start, 0))
    with open_day_archive(path) as archive:
        session = archive.sessions[0]
        assert session.duration == timedelta(0)
        assert session.start == session.stop


def test_equal_offsets_with_records_are_refused(tmp_path):
    # A session cannot both span time and not span it, and nothing in the
    # format says which reading is meant.
    path = tmp_path / "0123_2020-03-04.zip"
    start = datetime(2020, 3, 4, 22, 0, 0)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", session_wmedf(start, 60))
        zf.writestr("event_0001.xml", session_event_xml(start, 0))
    with pytest.raises(ArchiveError, match="identical"):
        with open_day_archive(path):
            pass


def test_a_twenty_four_hour_session_cannot_be_produced(tmp_path):
    """Exactly one day is unreachable, and that is the point.

    A stop exactly 24 h after a start writes the *same* offset, because the
    stop counts from its own midnight. So the only route to a 24-hour span is
    equal offsets — which is now handled explicitly rather than by adding a
    day. The sanity limit compares with ``>=`` as a second line, since exactly
    24 h is what a wrongly applied day correction produces.
    """
    path = tmp_path / "0123_2020-03-04.zip"
    start = datetime(2020, 3, 4, 13, 0, 0)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", session_wmedf(start, 86400))
        zf.writestr("event_0001.xml", session_event_xml(start, 86400))
    with pytest.raises(ArchiveError, match="identical"):
        with open_day_archive(path):
            pass


def test_session_from_another_therapy_day_is_refused(tmp_path):
    """The archive name is the weakest evidence in the archive.

    A rename changes it, so every member carrying a date is checked against
    it rather than trusted to match.
    """
    path = build_day_archive(
        tmp_path,
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 6, 22, 0, 0), 60)],
    )
    with pytest.raises(ArchiveError, match="falls in therapy day"):
        with open_day_archive(path):
            pass


@pytest.mark.parametrize("member", ["event.xml", "alarm.xml", "parameter.xml"])
def test_day_file_with_the_wrong_date_is_refused(tmp_path, member):
    """A file filed under the wrong day would attach data to the wrong date."""
    right = build_day_archive(tmp_path, archive_date=ARCHIVE_DATE)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    wrong = build_day_archive(
        other_dir,
        archive_date=date(2020, 3, 9),
        day_number=483,
        sessions=[(1, datetime(2020, 3, 9, 22, 0, 0), 60)],
    )

    spliced = tmp_path / "spliced.zip"
    with zipfile.ZipFile(right) as src, zipfile.ZipFile(wrong) as other:
        with zipfile.ZipFile(spliced, "w") as dst:
            for info in src.infolist():
                payload = (
                    other.read(member)
                    if info.filename == member
                    else src.read(info.filename)
                )
                dst.writestr(info.filename, payload)
    right.unlink()
    spliced.rename(right)

    reader = {
        "event.xml": lambda a: a.day_log(),
        "alarm.xml": lambda a: a.alarm_log(),
        "parameter.xml": lambda a: a.parameter_log(),
    }[member]
    with open_day_archive(right) as archive:
        with pytest.raises(ArchiveError, match="declares 2020-03-09"):
            reader(archive)


@pytest.mark.filterwarnings("ignore:Duplicate name")
@pytest.mark.parametrize("member", ["event.xml", "parameter.xml", "alarm.xml"])
def test_duplicate_day_member_is_refused(tmp_path, member):
    path = build_day_archive(tmp_path, archive_date=ARCHIVE_DATE)
    doubled = tmp_path / "doubled.zip"
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(doubled, "w") as dst:
        for info in src.infolist():
            dst.writestr(info.filename, src.read(info.filename))
        dst.writestr(member, src.read(member))
    final = tmp_path / "0479_2020-03-04.zip"
    doubled.rename(final)
    with pytest.raises(ArchiveError, match=f"more than one member named '{member}'"):
        with open_day_archive(final):
            pass


def test_open_signal_accepts_a_session_from_the_same_archive(one_session):
    with open_day_archive(one_session) as archive:
        with archive.open_signal(archive.sessions[0]) as stream:
            assert stream.read(8).startswith(b"1")


def test_open_signal_refuses_a_session_from_another_archive(tmp_path):
    """Every day has an 0001.wmedf, so the name alone proves nothing.

    Opening by name would hand back the wrong day's samples while the caller
    still holds this day's header, provenance and hash.
    """
    first = build_day_archive(
        tmp_path, archive_date=ARCHIVE_DATE, day_number=123,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 60)],
    )
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    second = build_day_archive(
        other_dir, archive_date=date(2020, 3, 5), day_number=479,
        sessions=[(1, datetime(2020, 3, 5, 22, 0, 0), 90)],
    )
    with open_day_archive(first) as a, open_day_archive(second) as b:
        assert a.sessions[0].signal_member == b.sessions[0].signal_member
        with pytest.raises(ArchiveError, match="carries provenance for"):
            with a.open_signal(b.sessions[0]):
                pass


def test_open_signal_refuses_a_tampered_archive_hash(one_session):
    from dataclasses import replace

    with open_day_archive(one_session) as archive:
        session = archive.sessions[0]
        forged = replace(
            session,
            provenance=replace(session.provenance, archive_sha256="0" * 64),
        )
        with pytest.raises(ArchiveError, match="carries provenance for"):
            with archive.open_signal(forged):
                pass


def test_open_signal_refuses_a_wrong_signal_member(one_session):
    from dataclasses import replace

    with open_day_archive(one_session) as archive:
        session = archive.sessions[0]
        forged = replace(session, signal_member="0009.wmedf")
        with pytest.raises(ArchiveError, match="not this archive's signal file"):
            with archive.open_signal(forged):
                pass


def test_open_signal_refuses_a_session_built_elsewhere(one_session):
    from dataclasses import replace

    with open_day_archive(one_session) as archive:
        session = archive.sessions[0]
        # Provenance and member all match; only the resolved stop was altered,
        # so this object is no longer the one the archive produced.
        forged = replace(session, stop=session.start)
        with pytest.raises(ArchiveError, match="not one of this archive's sessions"):
            with archive.open_signal(forged):
                pass


def _exported_session(tmp_path, skew: timedelta) -> dict:
    """Export an archive built with *skew* and return its one session row."""
    import json

    from prisma_vent.decode import export_archive

    path = _skewed_archive(tmp_path / "src", skew)
    destination = tmp_path / "export"
    export_archive(path, destination)
    exported = destination / path.stem
    line = (exported / "sessions.jsonl").read_text().splitlines()[0]
    return json.loads(line)


def test_the_skew_reaches_the_export(tmp_path):
    """A figure nobody can see is not really reported.

    The skew is the one piece of evidence a consumer would need in order to
    judge a suspected mispairing themselves, now that this decoder refuses to
    judge it for them. So it has to leave the process.
    """
    assert _exported_session(tmp_path, timedelta(seconds=90))["start_skew_seconds"] == 90.0


def test_a_negative_skew_survives_the_export_as_a_negative_number(tmp_path):
    """Clamping it at zero would hide exactly the case worth looking at."""
    assert _exported_session(tmp_path, timedelta(seconds=-30))["start_skew_seconds"] == -30.0
