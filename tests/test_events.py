"""Tests for the archive XML readers.

Every document here is written inline. Nothing is taken from a real archive:
the dates are arbitrary, the offsets are round numbers, and the ids are small
integers chosen to make the structure legible.
"""

from datetime import date, timedelta

import pytest

from prisma_vent.events import (
    XmlError,
    read_alarm_log,
    read_day_log,
    read_parameter_log,
    read_parameter_map,
    read_session_events,
)

DECL = '<?xml version="1.0" encoding="utf-8"?>'


def doc(body: str, decl: str = DECL) -> bytes:
    return (decl + body).encode("utf-8")


# --------------------------------------------------------------------------
# The XML acceptance contract
# --------------------------------------------------------------------------


def test_document_without_a_declaration_is_refused():
    with pytest.raises(XmlError, match="no XML declaration"):
        read_session_events(b"<desc></desc>")


def test_declaration_without_an_encoding_is_refused():
    with pytest.raises(XmlError, match="does not state an encoding"):
        read_session_events(doc("<desc/>", decl='<?xml version="1.0"?>'))


@pytest.mark.parametrize("encoding", ["UTF-8", "utf-8", "ascii", "US-ASCII"])
def test_accepted_encodings_are_case_insensitive(encoding):
    body = '<desc><start time="+0000:00:00.000"/><stop time="+0000:00:01.000"/></desc>'
    read_session_events(doc(body, decl=f'<?xml version="1.0" encoding="{encoding}"?>'))


@pytest.mark.parametrize("encoding", ["utf-16", "latin-1", "iso-8859-1", "utf-32"])
def test_other_encodings_are_refused(encoding):
    with pytest.raises(XmlError, match="not accepted"):
        read_session_events(doc("<desc/>", decl=f'<?xml version="1.0" encoding="{encoding}"?>'))


def test_utf16_document_is_refused_before_any_scan():
    """A byte scan for DOCTYPE is defeated by UTF-16.

    The encoding is therefore settled first, so the forbidden-declaration
    check never runs against bytes it cannot read.
    """
    raw = ('<?xml version="1.0" encoding="utf-16"?><desc/>').encode("utf-16")
    with pytest.raises(XmlError, match="UTF-16"):
        read_session_events(raw)


def test_byte_order_mark_is_refused():
    with pytest.raises(XmlError, match="byte order mark"):
        read_session_events(b"\xef\xbb\xbf" + doc("<desc/>"))


@pytest.mark.parametrize(
    "prologue",
    [
        '<!DOCTYPE desc [<!ENTITY x "y">]>',
        "<!DOCTYPE desc SYSTEM 'external.dtd'>",
        '<!doctype desc [<!entity x "y">]>',
    ],
)
def test_doctype_and_entity_declarations_are_refused(prologue):
    with pytest.raises(XmlError, match="DOCTYPE or ENTITY"):
        read_session_events(doc(prologue + "<desc/>"))


def test_oversized_document_is_refused_before_parsing():
    body = "<desc>" + "<!-- pad -->" * 2000 + "</desc>"
    with pytest.raises(XmlError, match="over the"):
        read_session_events(doc(body), max_bytes=512)


def test_malformed_xml_is_refused():
    with pytest.raises(XmlError, match="not well-formed"):
        read_session_events(doc("<desc><start></desc>"))


def test_wrong_root_element_is_refused():
    with pytest.raises(XmlError, match="root element"):
        read_session_events(doc("<log/>"))


# --------------------------------------------------------------------------
# Session events
# --------------------------------------------------------------------------

SESSION = doc(
    """<desc>
    <start time="+0015:00:00.000"/>
    <RespEvent time="120" id="6" event="end" Strength="1"/>
    <RespEvent time="0" id="6" event="begin" Strength="1"/>
    <RespEvent time="60" id="9" event="begin" Strength="0"/>
    <stop time="+0016:00:00.000"/>
    </desc>"""
)


def test_session_start_and_stop_are_offsets():
    result = read_session_events(SESSION)
    assert result.start == timedelta(hours=15)
    assert result.stop == timedelta(hours=16)


def test_session_events_are_sorted_on_read():
    """The device writes these out of chronological order.

    Anything downstream that assumes monotonic time would be quietly wrong,
    so ordering is imposed here rather than hoped for.
    """
    result = read_session_events(SESSION)
    assert [e.seconds for e in result.events] == [0, 60, 120]


def test_session_events_carry_no_name():
    event = read_session_events(SESSION).events[0]
    assert not hasattr(event, "name")
    assert event.id == 6


def test_session_event_fields():
    first = read_session_events(SESSION).events[0]
    assert (first.seconds, first.id, first.phase, first.strength) == (0, 6, "begin", 1)


def test_session_without_stop_is_refused():
    with pytest.raises(XmlError, match="missing its start or stop"):
        read_session_events(doc('<desc><start time="+0000:00:00.000"/></desc>'))


def test_unexpected_element_in_session_is_refused():
    body = '<desc><start time="+0000:00:00.000"/><Mystery/><stop time="+0000:00:01.000"/></desc>'
    with pytest.raises(XmlError, match="unexpected element <Mystery>"):
        read_session_events(doc(body))


def test_unexpected_attribute_is_refused():
    body = (
        '<desc><start time="+0000:00:00.000"/>'
        '<RespEvent time="0" id="1" event="begin" Strength="0" extra="x"/>'
        '<stop time="+0000:00:01.000"/></desc>'
    )
    with pytest.raises(XmlError, match="unexpected attribute"):
        read_session_events(doc(body))


def test_unknown_event_phase_is_refused():
    body = (
        '<desc><start time="+0000:00:00.000"/>'
        '<RespEvent time="0" id="1" event="middle" Strength="0"/>'
        '<stop time="+0000:00:01.000"/></desc>'
    )
    with pytest.raises(XmlError, match="expected begin or end"):
        read_session_events(doc(body))


# --------------------------------------------------------------------------
# Day log
# --------------------------------------------------------------------------

DAY = doc(
    """<log version="1" date="2020-03-04">
    <start time="+0012:00:00.000"/>
    <event time="-0001:00:00.000" id="84" status="true"/>
    <event time="+0015:00:00.000" id="71"/>
    <stop time="+0024:00:00.000"/>
    <start time="+0024:00:30.000"/>
    <stop time="+0036:00:00.000"/>
    </log>"""
)


def test_day_log_header():
    log = read_day_log(DAY)
    assert log.date == date(2020, 3, 4)
    assert log.version == "1"


def test_day_log_windows_are_paired():
    log = read_day_log(DAY)
    assert len(log.windows) == 2
    assert log.windows[0].start == timedelta(hours=12)
    assert log.windows[0].stop == timedelta(hours=24)
    assert log.windows[1].stop == timedelta(hours=36)


def test_day_log_offsets_may_exceed_a_day_and_be_negative():
    log = read_day_log(DAY)
    assert log.events[0].time == -timedelta(hours=1)
    assert log.windows[1].stop == timedelta(hours=36)


def test_absent_status_is_none_not_false():
    """A missing status means "not a state change", which is not False."""
    log = read_day_log(DAY)
    by_id = {e.id: e for e in log.events}
    assert by_id[84].status is True
    assert by_id[71].status is None


def test_non_boolean_status_is_refused():
    body = (
        '<log version="1" date="2020-03-04">'
        '<event time="+0000:00:00.000" id="1" status="yes"/></log>'
    )
    with pytest.raises(XmlError, match="not true or false"):
        read_day_log(doc(body))


def test_overlapping_windows_are_refused():
    body = (
        '<log version="1" date="2020-03-04">'
        '<start time="+0000:00:00.000"/><start time="+0001:00:00.000"/></log>'
    )
    with pytest.raises(XmlError, match="starts before the previous one stops"):
        read_day_log(doc(body))


def test_stop_without_start_is_refused():
    body = '<log version="1" date="2020-03-04"><stop time="+0001:00:00.000"/></log>'
    with pytest.raises(XmlError, match="stops without having started"):
        read_day_log(doc(body))


def test_unterminated_final_window_is_allowed():
    # The card is pulled while the device runs, so the last day legitimately
    # ends without a stop.
    body = '<log version="1" date="2020-03-04"><start time="+0012:00:00.000"/></log>'
    log = read_day_log(doc(body))
    assert log.windows[0].stop is None


@pytest.mark.parametrize("bad", ["2020-3-4", "04.03.2020", "2020-13-01", "2020-02-30", ""])
def test_malformed_log_date_is_refused(bad):
    body = f'<log version="1" date="{bad}"></log>'
    with pytest.raises(XmlError, match="not YYYY-MM-DD|not a real date"):
        read_day_log(doc(body))


# --------------------------------------------------------------------------
# Alarm log
# --------------------------------------------------------------------------


def test_alarm_phases():
    body = """<log version="1" date="2020-03-04">
    <start time="+0012:00:00.000"/>
    <alarm time="+0013:00:00.000" id="581" event="begin"/>
    <alarm time="+0013:00:00.000" id="581" event="end"/>
    <alarm time="+0013:00:05.000" id="581" event="reset"/>
    <stop time="+0024:00:00.000"/>
    </log>"""
    log = read_alarm_log(doc(body))
    assert [a.phase for a in log.alarms] == ["begin", "end", "reset"]
    # begin and end share a timestamp, so a duration taken from their
    # difference would be zero. Recorded here so the shape is not mistaken for
    # an interval.
    assert log.alarms[0].time == log.alarms[1].time


def test_unknown_alarm_phase_is_refused():
    body = (
        '<log version="1" date="2020-03-04">'
        '<alarm time="+0000:00:00.000" id="1" event="raised"/></log>'
    )
    with pytest.raises(XmlError, match="alarm event attribute"):
        read_alarm_log(doc(body))


# --------------------------------------------------------------------------
# Parameter map
# --------------------------------------------------------------------------

MAP = doc(
    """<parameters version="6.3.0" count="4">
    <parameter name="ActiveProgram" id="1"/>
    <parameter name="AutoLock" id="2"/>
    <parameter name="IPAP" id="10"/>
    <parameter name="EPAP" id="11"/>
    </parameters>"""
)


def test_parameter_map_reads_names():
    m = read_parameter_map(MAP)
    assert m.version == "6.3.0"
    assert m.names[10] == "IPAP"


def test_parameter_map_count_must_match():
    body = '<parameters version="1" count="9"><parameter name="A" id="1"/></parameters>'
    with pytest.raises(XmlError, match="declares 9 entries but lists 1"):
        read_parameter_map(doc(body))


def test_duplicate_parameter_id_is_refused():
    body = (
        '<parameters version="1" count="2">'
        '<parameter name="A" id="1"/><parameter name="B" id="1"/></parameters>'
    )
    with pytest.raises(XmlError, match="listed twice"):
        read_parameter_map(doc(body))


def test_looking_up_a_foreign_id_is_refused():
    """Event and alarm ids share no namespace with parameters.

    They collide numerically, so a low event id would resolve to a real
    parameter name — plausible, and wrong.
    """
    m = read_parameter_map(MAP)
    with pytest.raises(XmlError, match="share no namespace"):
        m.name(581)


# --------------------------------------------------------------------------
# Parameter snapshots — the positional program structure
# --------------------------------------------------------------------------


def _snapshot_doc(entries: list[tuple[int, str]], when: str = "+0012:00:00.000") -> bytes:
    rows = "".join(
        f'<parameter time="{when}" id="{i}" value="{v}"/>' for i, v in entries
    )
    return doc(f'<log version="1" date="2020-03-04"><config version="6.3.0"/>{rows}</log>')


THREE_PROGRAMS = _snapshot_doc(
    [(1, "0"), (2, "1")]
    + [(10, "a1"), (11, "b1"), (12, "c1")]
    + [(10, "a2"), (11, "b2"), (12, "c2")]
    + [(10, "a3"), (11, "b3"), (12, "c3")]
)


def test_snapshot_splits_into_device_parameters_and_programs():
    log = read_parameter_log(THREE_PROGRAMS)
    snapshot = log.snapshots[0]
    assert [p.id for p in snapshot.device_parameters] == [1, 2]
    assert len(snapshot.therapy_programs) == 3
    assert [p.value for p in snapshot.therapy_programs[0].parameters] == ["a1", "b1", "c1"]
    assert [p.value for p in snapshot.therapy_programs[2].parameters] == ["a3", "b3", "c3"]


def test_programs_are_distinguished_only_by_position():
    """Every program lists the same ids; only order separates them.

    A dictionary keyed by id would keep the last program and discard the rest,
    including whichever one is actually in use.
    """
    log = read_parameter_log(THREE_PROGRAMS)
    programs = log.snapshots[0].therapy_programs
    assert all([p.id for p in prog.parameters] == [10, 11, 12] for prog in programs)
    assert programs[0].position == 0 and programs[2].position == 2
    naive = {p.id: p.value for prog in programs for p in prog.parameters}
    assert naive == {10: "a3", 11: "b3", 12: "c3"}  # what keying by id loses


def test_entries_keep_their_position():
    log = read_parameter_log(THREE_PROGRAMS)
    snapshot = log.snapshots[0]
    assert [p.position for p in snapshot.device_parameters] == [0, 1]
    assert [p.position for p in snapshot.therapy_programs[1].parameters] == [5, 6, 7]


def test_active_program_is_reported_raw_with_its_entry():
    log = read_parameter_log(THREE_PROGRAMS, parameter_map=read_parameter_map(MAP))
    active = log.snapshots[0].active_program_raw
    assert active is not None
    assert active.id == 1
    assert active.value == "0"
    assert active.position == 0
    # ActiveProgram is a device-level parameter, so it carries no program
    # attribute of its own, and it is deliberately not resolved to
    # therapy_programs[0]: whether its value indexes the same space as the
    # program attribute is evidence, not yet fact.
    assert active.program is None


def test_two_snapshots_are_kept_separately():
    rows = [(1, "0"), (10, "x"), (10, "y")]
    body = (
        '<log version="1" date="2020-03-04">'
        + "".join(f'<parameter time="+0012:00:00.000" id="{i}" value="{v}"/>' for i, v in rows)
        + "".join(f'<parameter time="+0024:00:00.000" id="{i}" value="{v}"/>' for i, v in rows)
        + "</log>"
    )
    log = read_parameter_log(doc(body))
    assert len(log.snapshots) == 2
    assert log.snapshots[0].time == timedelta(hours=12)
    assert log.snapshots[1].time == timedelta(hours=24)


def test_uneven_program_blocks_are_refused():
    entries = [(1, "0")] + [(10, "a"), (11, "b")] + [(10, "c"), (11, "d")] + [(10, "e")]
    with pytest.raises(XmlError, match="does not divide into"):
        read_parameter_log(_snapshot_doc(entries))


def test_blocks_with_different_ids_are_refused():
    entries = [(10, "a"), (11, "b"), (10, "c"), (12, "d")]
    with pytest.raises(XmlError, match="different parameter ids"):
        read_parameter_log(_snapshot_doc(entries))


def test_missing_device_block_is_permitted_but_noted():
    entries = [(10, "a"), (11, "b"), (10, "c"), (11, "d")]
    log = read_parameter_log(_snapshot_doc(entries))
    snapshot = log.snapshots[0]
    assert snapshot.device_parameters == ()
    assert len(snapshot.therapy_programs) == 2
    assert any("no leading block" in n for n in snapshot.notes)


def test_unexpected_program_count_is_noted_not_rejected():
    entries = [(1, "0")] + [(10, "a"), (10, "b")]
    log = read_parameter_log(_snapshot_doc(entries))
    assert len(log.snapshots[0].therapy_programs) == 2
    assert any("not 3" in n for n in log.snapshots[0].notes)


def test_config_version_is_kept():
    log = read_parameter_log(THREE_PROGRAMS)
    assert log.config_version == "6.3.0"


def test_unexpected_element_in_parameter_log_is_refused():
    body = '<log version="1" date="2020-03-04"><surprise/></log>'
    with pytest.raises(XmlError, match="unexpected element <surprise>"):
        read_parameter_log(doc(body))


# --------------------------------------------------------------------------
# The explicit program attribute — found only by running against a real file
# --------------------------------------------------------------------------


def _tagged_doc(rows: list[tuple[int, str, int | None]]) -> bytes:
    parts = []
    for pid, value, program in rows:
        program_attr = "" if program is None else f' program="{program}"'
        parts.append(
            f'<parameter time="+0012:00:00.000" id="{pid}" value="{value}"{program_attr}/>'
        )
    return doc(f'<log version="1" date="2020-03-04">{"".join(parts)}</log>')


TAGGED = _tagged_doc(
    [(1, "0", None), (2, "1", None)]
    + [(10, "a1", 0), (11, "b1", 0)]
    + [(10, "a2", 1), (11, "b2", 1)]
    + [(10, "a3", 2), (11, "b3", 2)]
)


def test_program_attribute_is_authoritative():
    """The device says which program an entry belongs to; believe it.

    An earlier reading of these files concluded the grouping was positional
    only. That was wrong — the attribute is there, and a regex that stopped
    after ``value`` simply never saw it.
    """
    snapshot = read_parameter_log(TAGGED).snapshots[0]
    assert [p.id for p in snapshot.device_parameters] == [1, 2]
    assert [prog.position for prog in snapshot.therapy_programs] == [0, 1, 2]
    assert [p.value for p in snapshot.therapy_programs[1].parameters] == ["a2", "b2"]


def test_device_parameters_carry_no_program():
    snapshot = read_parameter_log(TAGGED).snapshots[0]
    assert all(p.program is None for p in snapshot.device_parameters)
    assert all(
        p.program == prog.position
        for prog in snapshot.therapy_programs
        for p in prog.parameters
    )


def test_program_numbering_starts_at_zero():
    # Which is why ActiveProgram="0" is consistent with 0-based indexing —
    # evidence for that open question, recorded rather than acted on.
    snapshot = read_parameter_log(TAGGED).snapshots[0]
    assert min(prog.position for prog in snapshot.therapy_programs) == 0


def test_programs_with_different_ids_are_refused():
    bad = _tagged_doc([(10, "a", 0), (11, "b", 0), (10, "c", 1), (12, "d", 1)])
    with pytest.raises(XmlError, match="different parameter ids"):
        read_parameter_log(bad)


def test_interleaved_programs_are_refused():
    """The layout and the attributes must agree about the structure.

    Previously a note; a note is not enough, because a caller can use the
    result normally without ever seeing it.
    """
    mixed = _tagged_doc([(10, "a", 0), (10, "b", 1), (11, "c", 0), (11, "d", 1)])
    with pytest.raises(XmlError, match="interleaved"):
        read_parameter_log(mixed)


def test_unexpected_program_numbering_is_noted():
    odd = _tagged_doc([(10, "a", 1), (10, "b", 2)])
    snapshot = read_parameter_log(odd).snapshots[0]
    assert any("not a run from zero" in n for n in snapshot.notes)


def test_positional_fallback_still_works_without_the_attribute():
    # Kept as an independent cross-check and as the path for a writer that
    # omits the attribute; it is not dead code.
    snapshot = read_parameter_log(THREE_PROGRAMS).snapshots[0]
    assert len(snapshot.therapy_programs) == 3
    assert all(p.program is None for p in snapshot.therapy_programs[0].parameters)


def test_acknowledged_alarm_phase_is_accepted():
    """`ack` is a phase the format uses, so the reader must accept it.

    It belongs in the accepted set because the device writes it when an alarm
    is acknowledged — a property of the format, established by reading
    archives rather than by reasoning about what a device might write. The
    document below is invented.
    """
    body = (
        '<log version="1" date="2020-03-04">'
        '<alarm time="+0013:00:00.000" id="581" event="ack"/></log>'
    )
    log = read_alarm_log(doc(body))
    assert log.alarms[0].phase == "ack"


# --------------------------------------------------------------------------
# Review hardening
# --------------------------------------------------------------------------


def test_two_start_elements_are_refused():
    """A second start is not a correction of the first.

    Keeping either would be a choice this decoder has no grounds to make, and
    silently keeping the last one anchors the whole session on it.
    """
    body = (
        '<desc><start time="+0001:00:00.000"/><start time="+0002:00:00.000"/>'
        '<stop time="+0003:00:00.000"/></desc>'
    )
    with pytest.raises(XmlError, match="more than one <start>"):
        read_session_events(doc(body))


def test_two_stop_elements_are_refused():
    body = (
        '<desc><start time="+0001:00:00.000"/>'
        '<stop time="+0002:00:00.000"/><stop time="+0003:00:00.000"/></desc>'
    )
    with pytest.raises(XmlError, match="more than one <stop>"):
        read_session_events(doc(body))


def test_exactly_one_start_and_stop_remain_valid():
    body = '<desc><start time="+0001:00:00.000"/><stop time="+0002:00:00.000"/></desc>'
    result = read_session_events(doc(body))
    assert result.start == timedelta(hours=1)
    assert result.stop == timedelta(hours=2)


# -- declared encoding is the encoding actually used ------------------------


def test_ascii_declaration_with_pure_ascii_is_valid():
    body = '<desc><start time="+0001:00:00.000"/><stop time="+0002:00:00.000"/></desc>'
    decl = '<?xml version="1.0" encoding="ascii"?>'
    assert read_session_events((decl + body).encode("ascii")).start == timedelta(hours=1)


def test_ascii_declaration_with_non_ascii_bytes_is_refused():
    """A false declaration must not pass merely because UTF-8 would read it."""
    decl = '<?xml version="1.0" encoding="ascii"?>'
    body = '<desc><start time="+0001:00:00.000"/><stop time="+0002:00:00.000"/>ü</desc>'
    with pytest.raises(XmlError, match="not valid ascii"):
        read_session_events((decl + body).encode("utf-8"))


def test_utf8_declaration_with_non_ascii_is_accepted():
    body = (
        '<desc><start time="+0001:00:00.000"/>'
        '<RespEvent time="0" id="1" event="begin" Strength="0"/>'
        '<stop time="+0002:00:00.000"/></desc>'
    )
    # A non-ASCII character in a comment: legal UTF-8, legal XML.
    assert read_session_events(doc("<!-- Grün -->" + body)).stop == timedelta(hours=2)


def test_invalid_utf8_is_refused():
    decl = '<?xml version="1.0" encoding="utf-8"?>'
    raw = decl.encode("ascii") + b"<desc>\xff\xfe\x00</desc>"
    with pytest.raises(XmlError, match="not valid utf-8"):
        read_session_events(raw)


# -- leaf elements carry no children and no body ----------------------------


def test_nested_element_inside_a_resp_event_is_refused():
    body = (
        '<desc><start time="+0001:00:00.000"/>'
        '<RespEvent time="0" id="1" event="begin" Strength="0"><extra/></RespEvent>'
        '<stop time="+0002:00:00.000"/></desc>'
    )
    with pytest.raises(XmlError, match="nested <extra>"):
        read_session_events(doc(body))


def test_nested_element_inside_a_parameter_is_refused():
    body = (
        '<log version="1" date="2020-03-04">'
        '<parameter time="+0012:00:00.000" id="1" value="0"><nested/></parameter></log>'
    )
    with pytest.raises(XmlError, match="nested <nested>"):
        read_parameter_log(doc(body))


def test_text_body_on_a_leaf_element_is_refused():
    body = (
        '<desc><start time="+0001:00:00.000"/>'
        '<RespEvent time="0" id="1" event="begin" Strength="0">surprise</RespEvent>'
        '<stop time="+0002:00:00.000"/></desc>'
    )
    with pytest.raises(XmlError, match="contains text"):
        read_session_events(doc(body))


def test_indented_xml_remains_valid():
    """The device indents its day-level files; whitespace is not content."""
    body = """<log version="1" date="2020-03-04">
        <start time="+0012:00:00.000"/>
        <event time="+0013:00:00.000" id="55" status="true"/>
        <stop time="+0036:00:00.000"/>
    </log>"""
    log = read_day_log(doc(body))
    assert len(log.windows) == 1 and log.events[0].id == 55


def test_untagged_id_repeated_across_therapy_blocks_is_refused():
    """A therapy parameter that lost its attribute must not become a device one.

    This is the silent mis-decode the strict contract exists for: the entry
    would be reported as a device setting, the remaining programs would still
    look identical to each other, and nothing downstream could tell.
    """
    rows = (
        [(1, "0", None)]
        + [(10, "a1", 0), (11, "x", None)]
        + [(10, "a2", 1), (11, "y", None)]
        + [(10, "a3", 2), (11, "z", None)]
    )
    with pytest.raises(XmlError, match="appear more than once without a program"):
        read_parameter_log(_tagged_doc(rows))


def test_single_untagged_entry_inside_the_therapy_blocks_is_refused():
    rows = [(1, "0", None), (10, "a", 0), (99, "stray", None), (10, "b", 1)]
    with pytest.raises(XmlError, match="follows entries that do"):
        read_parameter_log(_tagged_doc(rows))


def test_device_parameter_after_the_first_tagged_entry_is_refused():
    rows = [(10, "a", 0), (10, "b", 1), (1, "late", None)]
    with pytest.raises(XmlError, match="follows entries that do"):
        read_parameter_log(_tagged_doc(rows))


def test_correct_leading_block_plus_three_programs_is_valid():
    rows = (
        [(1, "0", None), (2, "1", None)]
        + [(10, "a1", 0), (11, "b1", 0)]
        + [(10, "a2", 1), (11, "b2", 1)]
        + [(10, "a3", 2), (11, "b3", 2)]
    )
    snapshot = read_parameter_log(_tagged_doc(rows)).snapshots[0]
    assert [p.id for p in snapshot.device_parameters] == [1, 2]
    assert len(snapshot.therapy_programs) == 3
    assert snapshot.notes == ()


def test_tagged_programs_with_different_ids_still_refused():
    rows = [(10, "a", 0), (11, "b", 0), (10, "c", 1), (12, "d", 1)]
    with pytest.raises(XmlError, match="different parameter ids"):
        read_parameter_log(_tagged_doc(rows))


def test_snapshot_with_only_device_parameters_is_valid():
    rows = [(1, "0", None), (2, "1", None)]
    snapshot = read_parameter_log(_tagged_doc(rows)).snapshots[0]
    assert snapshot.therapy_programs == ()
    assert len(snapshot.device_parameters) == 2
