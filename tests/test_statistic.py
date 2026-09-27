"""Tests for the ``statistic.proto`` reader and the protobuf walker.

Every fixture here is generated at runtime and entirely synthetic — made-up
timestamps, round durations, a configuration with two invented parameters.
Nothing is cut from a recording.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from prisma_vent.protobuf import (
    Field,
    ProtobufError,
    collect,
    walk,
    zigzag_decode,
)
from prisma_vent.statistic import (
    CATEGORY_COUNT,
    EPOCH_OFFSET,
    PROGRAM_SLOTS,
    StatisticError,
    read_statistic,
)
from synthetic import (
    build_statistic,
    build_usage_record,
    example_configuration,
    pb_bytes_field,
    pb_varint_field,
)


# ---------------------------------------------------------------------------
# The wire reader
# ---------------------------------------------------------------------------


def test_a_message_is_walkable_without_its_schema():
    """The premise of the whole module: structure needs no .proto file."""
    data = pb_varint_field(1, 150) + pb_bytes_field(2, b"hello")
    fields = list(walk(data))
    assert [f.number for f in fields] == [1, 2]
    assert fields[0].as_int == 150
    assert fields[1].as_bytes == b"hello"


def test_repeated_fields_stay_repeated():
    """Nothing is merged: the reader cannot know repeated from last-one-wins."""
    data = b"".join(pb_varint_field(3, n) for n in (7, 8, 9))
    assert [f.as_int for f in walk(data)] == [7, 8, 9]
    assert [f.as_int for f in collect(data)[3]] == [7, 8, 9]


def test_a_multi_byte_varint_round_trips():
    for value in (0, 1, 127, 128, 300, 2**31, 2**63 - 1):
        assert next(walk(pb_varint_field(1, value))).as_int == value


def test_a_truncated_length_is_refused_not_padded():
    data = pb_bytes_field(1, b"abcdef")[:-2]
    with pytest.raises(ProtobufError, match="only"):
        list(walk(data))


def test_a_runaway_varint_does_not_spin():
    """Without the length cap a run of continuation bytes never terminates."""
    with pytest.raises(ProtobufError, match="longer than 64 bits"):
        list(walk(b"\x08" + b"\x80" * 12 + b"\x01"))


def test_group_encoding_is_rejected_rather_than_skipped():
    """Skipping a field silently is the quiet data loss this package avoids."""
    with pytest.raises(ProtobufError, match="deprecated group"):
        list(walk(bytes([(1 << 3) | 3])))


def test_reserved_field_numbers_are_rejected():
    with pytest.raises(ProtobufError, match="reserved"):
        list(walk(pb_varint_field(19500, 1)))


def test_field_number_zero_is_rejected():
    with pytest.raises(ProtobufError, match="valid range"):
        list(walk(b"\x00\x01"))


def test_asking_a_varint_for_bytes_raises_rather_than_guessing():
    field = Field(1, 0, 5)
    with pytest.raises(ProtobufError):
        field.as_bytes  # noqa: B018 — the attribute access is the call
    with pytest.raises(ProtobufError):
        Field(2, 2, b"x").as_int  # noqa: B018 — likewise


def test_zigzag_is_offered_because_getting_it_wrong_is_invisible():
    assert zigzag_decode(0) == 0
    assert zigzag_decode(1) == -1
    assert zigzag_decode(2) == 1
    assert zigzag_decode(4294967294) == 2147483647


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def one(**kwargs):
    return build_statistic(records=[build_usage_record(**kwargs)])


def test_a_record_yields_a_start_and_a_duration():
    stat = read_statistic(one(timestamp=1_784_776_666, duration=401))
    record = stat.records[0]
    assert record.duration_minutes == 401
    assert record.raw_timestamp == 1_784_776_666
    expected = datetime(1970, 1, 1) + timedelta(seconds=1_784_776_666) + EPOCH_OFFSET
    assert record.start.value == expected


def test_the_raw_counter_is_kept_beside_the_converted_time():
    """So a wrong time can be blamed on the offset rather than the decoding."""
    stat = read_statistic(one(timestamp=12345, duration=1))
    record = stat.records[0]
    assert record.raw_timestamp == 12345
    assert (record.start.value - datetime(1970, 1, 1)).total_seconds() == (
        12345 + EPOCH_OFFSET.total_seconds()
    )


def test_the_time_carries_no_timezone_and_cannot_be_given_one():
    stat = read_statistic(one(timestamp=1000, duration=1))
    assert stat.records[0].start.value.tzinfo is None


def test_a_breakdown_that_does_not_sum_to_its_total_is_fatal():
    """The identity held without exception, which makes it a contract."""
    data = build_statistic(
        records=[build_usage_record(timestamp=1, duration=100, by_program=[50, 0, 0])]
    )
    with pytest.raises(StatisticError, match="parts sum to 50 but the total is 100"):
        read_statistic(data)


def test_every_one_of_the_four_sum_identities_is_enforced():
    cases = {
        "by_program": {"by_program": [1, 0, 0]},
        "by_category": {"by_category": [1] + [0] * 10},
        "second_by_program": {"second_by_program": [1, 0, 0]},
        "second_by_category": {"second_by_category": [1] + [0] * 10},
    }
    for _label, override in cases.items():
        data = build_statistic(
            records=[
                build_usage_record(timestamp=1, duration=10, second=10, **override)
            ]
        )
        with pytest.raises(StatisticError, match="parts sum to"):
            read_statistic(data)


def test_the_wrong_number_of_program_slots_is_fatal():
    data = build_statistic(
        records=[build_usage_record(timestamp=1, duration=0, by_program=[0, 0])]
    )
    with pytest.raises(StatisticError, match="expected 3 values"):
        read_statistic(data)


def test_the_wrong_number_of_categories_is_fatal():
    data = build_statistic(
        records=[build_usage_record(timestamp=1, duration=0, by_category=[0] * 10)]
    )
    with pytest.raises(StatisticError, match="expected 11 values"):
        read_statistic(data)


def test_a_second_quantity_above_the_duration_is_refused():
    """The format does not permit it, so it means a misread.

    The second quantity is a part of the duration, and a part does not exceed
    the whole it is a part of. The reader treats that as a contract.
    """
    data = build_statistic(
        records=[
            build_usage_record(
                timestamp=1,
                duration=10,
                second=20,
                second_by_program=[20, 0, 0],
                second_by_category=[0, 0, 20] + [0] * 8,
            )
        ]
    )
    with pytest.raises(StatisticError, match="exceeds the duration"):
        read_statistic(data)


def test_a_missing_field_is_named_rather_than_defaulted():
    record = pb_varint_field(1, 100) + pb_varint_field(2, 5)
    with pytest.raises(StatisticError, match=r"missing field\(s\)"):
        read_statistic(build_statistic(records=[record]))


def test_active_programs_and_categories_are_indices_not_names():
    """Naming a category would assert an ordering one coincidence cannot carry."""
    stat = read_statistic(
        one(
            timestamp=1,
            duration=30,
            by_program=[0, 30, 0],
            by_category=[0] * 8 + [30, 0, 0],
        )
    )
    record = stat.records[0]
    assert record.active_programs == (1,)
    assert record.active_categories == (8,)
    assert all(isinstance(i, int) for i in record.active_categories)


def test_histograms_are_handed_back_untouched():
    stat = read_statistic(one(timestamp=1, duration=0, histograms=3))
    record = stat.records[0]
    parsed = record.histograms()
    assert len(parsed) == PROGRAM_SLOTS
    assert parsed[0][1] == (0, 1, 2)


def test_histograms_are_not_parsed_until_asked_for(tmp_path):
    """Parsing them is most of the cost of reading the file, and few want them."""
    stat = read_statistic(one(timestamp=1, duration=0, histograms=3))
    record = stat.records[0]
    assert all(isinstance(b, bytes) for b in record.histogram_blocks)
    assert callable(record.histograms)


def test_a_record_with_no_histograms_is_not_an_error():
    """The count is not asserted: a firmware may record fewer."""
    stat = read_statistic(one(timestamp=1, duration=0, histograms=0))
    assert stat.records[0].histograms() == ()


# ---------------------------------------------------------------------------
# File level
# ---------------------------------------------------------------------------


def test_the_lifetime_counter_is_read():
    stat = read_statistic(
        build_statistic(
            records=[build_usage_record(timestamp=1, duration=10)],
            therapy_total=5000,
            other_total=9000,
        )
    )
    assert stat.cumulative_therapy_minutes == 5000
    assert stat.unidentified_total == 9000


def test_a_lifetime_counter_below_its_own_records_is_refused():
    """Catches the two counters being swapped or misread."""
    data = build_statistic(
        records=[build_usage_record(timestamp=1, duration=500)],
        therapy_total=100,
    )
    with pytest.raises(StatisticError, match="below the 500 min"):
        read_statistic(data)


def test_the_covered_span_is_reported():
    day = 86_400
    stat = read_statistic(
        build_statistic(
            records=[
                build_usage_record(timestamp=0, duration=1),
                build_usage_record(timestamp=10 * day, duration=1),
            ],
            therapy_total=None,
        )
    )
    assert stat.covered_span == timedelta(days=10)


def test_a_file_with_no_records_has_no_span():
    assert read_statistic(build_statistic(records=[])).covered_span is None


def test_a_file_that_is_not_protobuf_fails_loudly():
    with pytest.raises(StatisticError):
        read_statistic(b"\xff\xff\xff\xff\xff\xff")


def test_a_string_instead_of_bytes_is_refused():
    with pytest.raises(StatisticError, match="expected bytes"):
        read_statistic("not bytes")


# ---------------------------------------------------------------------------
# The embedded configuration
# ---------------------------------------------------------------------------


def test_the_named_configuration_is_read():
    stat = read_statistic(
        build_statistic(records=[], configuration=example_configuration())
    )
    config = stat.configuration
    assert config.device["ActiveProgram"] == 0
    assert config.therapy["IPAP"] == [180, 180, 180]


def test_the_firmware_version_is_labelled_probable():
    """Nothing in the file states it, so the name must not promise more."""
    stat = read_statistic(
        build_statistic(records=[], configuration=example_configuration())
    )
    assert stat.configuration.probable_firmware_version == "6.3.0"


def test_no_dotted_triple_means_no_version_guess():
    stat = read_statistic(
        build_statistic(
            records=[], configuration=example_configuration(), labels=("2_1_1",)
        )
    )
    assert stat.configuration.probable_firmware_version is None


def test_a_therapy_parameter_without_one_entry_per_program_is_fatal():
    """Its three entries are what makes it comparable with parameter.xml."""
    data = build_statistic(records=[], configuration=example_configuration(programs=2))
    with pytest.raises(StatisticError, match="expected one per program slot"):
        read_statistic(data)


def test_a_configuration_that_is_not_json_fails_loudly():
    block = pb_bytes_field(1, b"{not json")
    data = pb_bytes_field(1, b"1.0.12") + pb_bytes_field(3, block)
    with pytest.raises(StatisticError, match="not JSON"):
        read_statistic(data)


def test_a_configuration_missing_its_sections_fails_loudly():
    block = pb_bytes_field(1, json.dumps({"configuration": {"device": {}}}).encode())
    data = pb_bytes_field(1, b"1.0.12") + pb_bytes_field(3, block)
    with pytest.raises(StatisticError, match="lacks 'device' or 'therapy'"):
        read_statistic(data)


def test_the_whole_document_is_kept_so_nothing_unmodelled_is_lost():
    stat = read_statistic(
        build_statistic(records=[], configuration=example_configuration())
    )
    assert stat.configuration.raw["configuration"]["version"] == "0"


def test_two_configuration_blocks_are_refused():
    block = pb_bytes_field(1, json.dumps(example_configuration()).encode())
    data = (
        pb_bytes_field(1, b"1.0.12")
        + pb_bytes_field(3, block)
        + pb_bytes_field(3, block)
    )
    with pytest.raises(StatisticError, match="at most one configuration"):
        read_statistic(data)


def test_the_constants_match_what_the_device_writes():
    assert PROGRAM_SLOTS == 3
    assert CATEGORY_COUNT == 11
    assert EPOCH_OFFSET == timedelta(hours=12)
