"""Tests for the machine-readable export.

The format is a promise to a program, so these check the promise rather than
the prose: one JSON object per line, no invented names, nulls where a value is
absent, and nothing that would parse as valid JSON only by luck.
"""

import csv
import io
import json
import math
import os
from datetime import date, datetime
from pathlib import Path

import pytest

from prisma_vent.decode import (
    EXPORT_SCHEMA_VERSION,
    export_archive,
)
from synthetic import build_day_archive

ARCHIVE_DATE = date(2020, 3, 4)


@pytest.fixture
def exported(tmp_path):
    archive = build_day_archive(
        tmp_path / "card",
        archive_date=ARCHIVE_DATE,
        sessions=[
            (1, datetime(2020, 3, 4, 22, 0, 0), 120),
            (2, datetime(2020, 3, 5, 1, 30, 0), 60),
        ],
    )
    result = export_archive(archive, tmp_path / "out")
    return result.directory


def read_jsonl(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_the_expected_files_are_written(exported):
    names = {p.name for p in exported.iterdir()}
    assert names == {
        "manifest.json",
        "sessions.jsonl",
        "events.jsonl",
        "parameters.json",
        "validation-report.json",
    }


def test_manifest_declares_the_schema_version(exported):
    manifest = json.loads((exported / "manifest.json").read_text())
    assert manifest["export_schema_version"] == EXPORT_SCHEMA_VERSION
    assert manifest["session_count"] == 2
    assert manifest["therapy_day"] == "2020-03-04"


def test_jsonl_is_one_object_per_line(exported):
    """Not a wrapped array, and not pretty-printed across lines."""
    for name in ("sessions.jsonl", "events.jsonl"):
        text = (exported / name).read_text(encoding="utf-8")
        assert text.endswith("\n")
        for line in text.splitlines():
            assert isinstance(json.loads(line), dict)


def test_sessions_carry_provenance_without_the_adapter_version(exported):
    """Re-importing with a newer decoder must replace, not duplicate."""
    rows = read_jsonl(exported / "sessions.jsonl")
    identity = rows[0]["provenance"]["identity"]
    assert rows[0]["provenance"]["archive_sha256"] in identity
    assert "0.1.0" not in identity


def test_channels_are_identified_by_index(exported):
    rows = read_jsonl(exported / "sessions.jsonl")
    channels = rows[0]["channels"]
    assert [c["index"] for c in channels] == list(range(len(channels)))
    assert all("label" in c and "sampling_rate_hz" in c for c in channels)


# --------------------------------------------------------------------------
# The rules the format promises
# --------------------------------------------------------------------------


def test_no_event_carries_a_name(exported):
    """No id has been identified; there is deliberately nothing to fill in."""
    for row in read_jsonl(exported / "events.jsonl"):
        assert "name" not in row
        assert isinstance(row["id"], int)


def test_events_are_told_apart_by_kind(exported):
    kinds = {row["kind"] for row in read_jsonl(exported / "events.jsonl")}
    assert kinds <= {"respiratory", "device_state", "alarm"}
    for row in read_jsonl(exported / "events.jsonl"):
        if row["kind"] == "respiratory":
            assert row["session"] is not None
        else:
            assert row["session"] is None


def test_absent_status_is_null_not_false(exported):
    """Null means "not a state change", which is a different claim."""
    rows = [r for r in read_jsonl(exported / "events.jsonl")
            if r["kind"] == "device_state"]
    assert rows
    assert all(r["status"] in (True, False, None) for r in rows)


def test_parameters_are_raw_and_programs_are_not_resolved(exported):
    data = json.loads((exported / "parameters.json").read_text())
    assert data["available"] is True
    snapshot = data["snapshots"][0]
    for entry in snapshot["device_parameters"]:
        assert entry["program"] is None      # not "program 0"
        assert isinstance(entry["value_raw"], str)
    if snapshot["active_program_raw"] is not None:
        # Reported as written, never resolved to an entry in therapy_programs.
        assert "resolved_program" not in snapshot["active_program_raw"]


# --------------------------------------------------------------------------
# The scale registries
#
# Two objects that say what is known about converting a raw parameter, and how
# well. `scales` holds conversions confirmed by several distinct raw/display
# pairs giving one ratio; `scale_candidates` holds ones resting on a single
# reference point. A parameter in neither has no known conversion.
#
# They are separate objects rather than one with a status column because the
# separation is the safety boundary: reading `scales` cannot hand you a
# candidate. `status` only makes an entry self-describing once lifted out.
# --------------------------------------------------------------------------


EVIDENCE_METHODS = {
    "device_display_reference",
    "known_setting_change",
    "multi_point_reference",
    "arithmetic_cross_check",
    "signal_cross_check",
}


def _registries(exported):
    data = json.loads((exported / "parameters.json").read_text())
    return data["scales"], data["scale_candidates"]


def test_a_confirmed_scale_is_published_and_a_candidate_is_kept_apart(exported):
    scales, candidates = _registries(exported)
    assert set(scales) == {"IPAP"}
    assert "IPAP" not in candidates
    assert len(candidates) == 9


def test_no_parameter_appears_in_both_registries(exported):
    """The separation is what makes reading `scales` safe."""
    scales, candidates = _registries(exported)
    assert not set(scales) & set(candidates)


def test_status_matches_the_registry_an_entry_is_in(exported):
    """A confirmed entry in the candidate registry would defeat the split."""
    scales, candidates = _registries(exported)
    assert all(e["status"] == "confirmed" for e in scales.values())
    assert all(e["status"] != "confirmed" for e in candidates.values())


def test_every_candidate_is_written_out_with_its_own_factor(exported):
    """Named one by one, because each rests on its own observation.

    The four inspiratory-time parameters share a ratio, and that is a result,
    not an inheritance: each has its own display reading. Asserting them as a
    group — or generating them from one constant — would hide four
    observations behind a resemblance between names, which is the reasoning
    that put an unfounded third confirmed factor into this project's
    documentation.
    """
    _, candidates = _registries(exported)
    expected = {
        "EPAP": (100, 1, "hPa"),
        "Frequency": (10, 1, "/min"),
        "Ti": (1000, 1, "s"),
        "Ti_max": (1000, 1, "s"),
        "Ti_min": (1000, 1, "s"),
        "Ti_timed": (1000, 1, "s"),
        "TriggerSensitivityExspiration": (1, 1, "%"),
        "VolumeTarget": (1, 1, "ml"),
        "VolumeTargetDeltaPressure": (100, 1, "hPa"),
    }
    assert set(candidates) == set(expected)
    for name, (raw_delta, physical_delta, unit) in expected.items():
        entry = candidates[name]
        assert entry["raw_delta"] == raw_delta, name
        assert entry["physical_delta"] == physical_delta, name
        assert entry["unit"] == unit, name


def test_every_entry_is_self_describing_and_well_formed(exported):
    scales, candidates = _registries(exported)
    seen_ids, seen_names = set(), set()
    for registry in (scales, candidates):
        for key, entry in registry.items():
            assert entry["name"] == key
            assert isinstance(entry["parameter_id"], int) and entry["parameter_id"] > 0
            assert entry["unit"]
            assert isinstance(entry["raw_delta"], int) and entry["raw_delta"] > 0
            assert (
                isinstance(entry["physical_delta"], int) and entry["physical_delta"] > 0
            )
            assert math.gcd(entry["raw_delta"], entry["physical_delta"]) == 1
            assert entry["evidence"]
            assert set(entry["evidence"]) <= EVIDENCE_METHODS
            assert entry["parameter_id"] not in seen_ids
            assert entry["name"] not in seen_names
            seen_ids.add(entry["parameter_id"])
            seen_names.add(entry["name"])


def test_two_exports_of_one_archive_are_byte_identical(tmp_path):
    """Reproducibility, asserted where it can actually fail.

    An earlier version of this test compared each registry's key order against
    its own sorted order. That can never fail: `_dump` serialises with
    `sort_keys=True`, so every object in the export comes out ordered whatever
    order it was built in. It was asserting a property of `json.dumps`.

    What is worth asserting is the thing that property exists for.
    """
    archive = build_day_archive(
        tmp_path / "card",
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 60)],
    )
    first = export_archive(archive, tmp_path / "a").directory
    second = export_archive(archive, tmp_path / "b").directory
    for name in ("parameters.json", "events.jsonl", "sessions.jsonl"):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_value_domains_name_the_parameters_that_are_not_quantities(exported):
    """A step is not a measurement, and a mode is not a number to scale."""
    data = json.loads((exported / "parameters.json").read_text())
    domains = data["value_domains"]
    assert set(domains) == {
        "ExpirationRamp",
        "InspirationRamp",
        "TherapyMode",
        "TriggerSensitivityInspiration",
        "TriggerType",
        "VolumeTargetControl",
    }
    assert {e["kind"] for e in domains.values()} == {"ordinal", "categorical"}


def test_a_parameter_never_has_both_a_scale_and_a_value_domain(exported):
    """The two answer different questions, and no parameter takes both.

    `TriggerSensitivityExspiration` is a percentage and carries a scale
    candidate; `TriggerSensitivityInspiration`, one id away, is a step and
    carries a domain. The device's screens for the two look alike, so this is
    the pairing most likely to be confused.
    """
    data = json.loads((exported / "parameters.json").read_text())
    scaled = set(data["scales"]) | set(data["scale_candidates"])
    assert not scaled & set(data["value_domains"])
    assert "TriggerSensitivityExspiration" in data["scale_candidates"]
    assert "TriggerSensitivityInspiration" in data["value_domains"]


def test_observed_labels_are_keyed_by_the_raw_value_as_written(exported):
    """`value_raw` is a string, so these keys are too — they must match it."""
    data = json.loads((exported / "parameters.json").read_text())
    for entry in data["value_domains"].values():
        assert entry["observed_labels"]
        assert all(isinstance(k, str) for k in entry["observed_labels"])
        assert all(v for v in entry["observed_labels"].values())


def test_every_value_domain_entry_is_well_formed(exported):
    """The same invariants the scale registries get.

    Written because the first version of these tests checked the two scale
    registries and left this one out, so an entry here could have carried no
    evidence at all and nothing would have said so.
    """
    data = json.loads((exported / "parameters.json").read_text())
    for key, entry in data["value_domains"].items():
        assert entry["name"] == key
        assert isinstance(entry["parameter_id"], int) and entry["parameter_id"] > 0
        assert entry["kind"] in {"ordinal", "categorical"}
        assert entry["observed_labels"]
        assert entry["evidence"]
        assert set(entry["evidence"]) <= EVIDENCE_METHODS


def test_no_name_appears_in_more_than_one_registry(exported):
    """Across all three, not just the two scale ones."""
    data = json.loads((exported / "parameters.json").read_text())
    names = [
        name
        for field in ("scales", "scale_candidates", "value_domains")
        for name in data[field]
    ]
    assert len(names) == len(set(names))


def test_the_registries_share_no_parameter_id_either(exported):
    """A name collision is the obvious mistake; an id collision is the quiet one."""
    data = json.loads((exported / "parameters.json").read_text())
    ids = [
        entry["parameter_id"]
        for field in ("scales", "scale_candidates", "value_domains")
        for entry in data[field].values()
    ]
    assert len(ids) == len(set(ids))


def test_a_value_domain_is_partial_and_says_nothing_about_other_values(exported):
    """Observation shows what a value displays as, never that no others exist.

    The device writes eight for one therapy mode and two for another; nothing
    seen so far says what a five would mean, and the map must not imply it
    does. This is asserted rather than only documented because a consumer
    treating the map as exhaustive is the failure it invites.
    """
    data = json.loads((exported / "parameters.json").read_text())
    modes = data["value_domains"]["TherapyMode"]["observed_labels"]
    assert set(modes) == {"2", "8"}
    assert "5" not in modes


def _decode_with_map(tmp_path, **map_kwargs):
    """Export an archive whose parameter map is built to order."""
    from synthetic import _parameter_log_xml, _parameter_map_xml

    archive = build_day_archive(
        tmp_path / "card",
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 60)],
        day_members=False,
        extra_members={
            "parameter.xml": _parameter_log_xml(ARCHIVE_DATE),
            "parametersmap.xml": _parameter_map_xml(**map_kwargs),
        },
    )
    directory = export_archive(archive, tmp_path / "out").directory
    return json.loads((directory / "parameters.json").read_text())


def _assert_all_registries_empty(data):
    for field in ("scales", "scale_candidates", "value_domains"):
        assert data[field] == {}, field


def test_a_map_version_the_evidence_does_not_cover_publishes_nothing(tmp_path):
    """A factor was established against one device state.

    Emitting it for a map that merely looks right would turn "confirmed for
    the version examined" into "confirmed wherever this name appears", across
    firmware nobody has seen.
    """
    _assert_all_registries_empty(_decode_with_map(tmp_path, version="9.9.9"))


def test_a_config_version_that_differs_publishes_nothing(tmp_path):
    """Both versions are checked, not only the map's.

    The readings were taken from one device state, so binding to either alone
    would claim more than was observed. Tested with the map version left
    **correct**, so a check that looked only at the map would go green here.
    """
    from synthetic import _parameter_map_xml

    log = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<log version="1" date="2020-03-04">'
        '<config version="9.9.9"/>'
        '<parameter time="+0012:00:00.000" id="69" value="1600"/>'
        "</log>"
    ).encode("utf-8")
    archive = build_day_archive(
        tmp_path / "card",
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 60)],
        day_members=False,
        extra_members={
            "parameter.xml": log,
            "parametersmap.xml": _parameter_map_xml(),
        },
    )
    directory = export_archive(archive, tmp_path / "out").directory
    _assert_all_registries_empty(
        json.loads((directory / "parameters.json").read_text())
    )


def test_an_expected_id_carrying_another_name_publishes_nothing(tmp_path):
    """Otherwise a scale would be applied to the wrong parameter.

    The expected name is **still in the map**, just on a different id. That is
    what isolates this check: with the name simply gone, the uniqueness rule
    would reject the map anyway and this test would pass with the id-to-name
    comparison deleted. Two earlier versions did exactly that.
    """
    from synthetic import REGISTERED_PARAMETER_IDS

    entries = (
        (1, "ActiveProgram"),
        *((pid, "SomethingElse" if pid == 69 else name)
          for pid, name in REGISTERED_PARAMETER_IDS),
        (200, "IPAP"),
    )
    _assert_all_registries_empty(_decode_with_map(tmp_path, entries=entries))


def test_an_expected_id_missing_from_the_map_publishes_nothing(tmp_path):
    """With every other id present, so nothing else explains the refusal.

    Both the id-to-name comparison and the uniqueness rule reject this one —
    the name is not on its id, and it is nowhere else either. It is written
    down separately because a map that has simply lost a parameter is the case
    a reader will expect to find covered.
    """
    from synthetic import REGISTERED_PARAMETER_IDS

    entries = tuple(
        (pid, name)
        for pid, name in ((1, "ActiveProgram"), *REGISTERED_PARAMETER_IDS)
        if pid != 69
    )
    _assert_all_registries_empty(_decode_with_map(tmp_path, entries=entries))


def test_a_name_appearing_twice_publishes_nothing(tmp_path):
    """`read_parameter_map` rejects a repeated id but not a repeated name.

    Two ids could therefore carry one name, and nothing would say which the
    evidence was gathered against.
    """
    from synthetic import REGISTERED_PARAMETER_IDS

    entries = (*REGISTERED_PARAMETER_IDS, (200, "IPAP"))
    _assert_all_registries_empty(_decode_with_map(tmp_path, entries=entries))


def test_an_archive_without_parameters_still_carries_both_registries(tmp_path):
    """Every shape of this file has the same top-level fields.

    A consumer never has to tell "absent" from "empty".
    """
    archive = build_day_archive(
        tmp_path / "card",
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 60)],
        day_members=False,
    )
    directory = export_archive(archive, tmp_path / "out").directory
    data = json.loads((directory / "parameters.json").read_text())
    assert data["available"] is False
    assert data["scales"] == {}
    assert data["scale_candidates"] == {}
    assert data["value_domains"] == {}


def test_a_scale_does_not_change_value_raw(exported):
    """The decoder says what a conversion would be; it never performs one."""
    data = json.loads((exported / "parameters.json").read_text())
    for program in data["snapshots"][0]["therapy_programs"]:
        for entry in program["parameters"]:
            assert isinstance(entry["value_raw"], str)
            assert "value" not in entry
            assert "physical_value" not in entry


def test_validation_distinguishes_not_comparable_from_passed(exported):
    data = json.loads((exported / "validation-report.json").read_text())
    for session in data["sessions"]:
        for check in session["checks"].values():
            assert check["status"] in ("passed", "failed", "not comparable")


def test_nan_would_be_refused_rather_than_written(tmp_path):
    """Python's encoder emits NaN by default; JSON has no such value.

    Files carrying it are rejected by some parsers and silently misread by
    others, so the exporter must not produce one.
    """
    from prisma_vent.decode import _dump

    with pytest.raises(ValueError):
        _dump({"x": float("nan")})
    with pytest.raises(ValueError):
        _dump({"x": float("inf")})


def test_everything_written_is_valid_utf8_json(exported):
    for path in exported.rglob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------


def test_signals_are_not_written_unless_asked(exported):
    assert not list(exported.glob("session_*"))


def test_signal_csv_carries_raw_and_converted(tmp_path):
    archive = build_day_archive(
        tmp_path / "card",
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 60)],
    )
    result = export_archive(
        archive, tmp_path / "out", signals=["Pressure"], max_samples=25
    )
    assert result.signal_files == 1

    path = next(result.directory.glob("session_0001/signal_*.csv"))
    rows = list(csv.DictReader(path.open()))
    assert len(rows) == 25
    assert set(rows[0]) == {
        "sample_index",
        "device_local_time",
        "digital_value",
        "physical_value",
        "unit",
    }
    assert rows[0]["unit"] == "hPa"


def test_signal_times_are_derived_not_accumulated(tmp_path):
    """Ten hertz means exactly a tenth of a second, every time."""
    archive = build_day_archive(
        tmp_path / "card",
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 60)],
    )
    result = export_archive(
        archive, tmp_path / "out", signals=["Pressure"], max_samples=40
    )
    path = next(result.directory.glob("session_0001/signal_*.csv"))
    rows = list(csv.DictReader(path.open()))
    times = [datetime.fromisoformat(r["device_local_time"]) for r in rows]
    gaps = {round((b - a).total_seconds(), 6) for a, b in zip(times, times[1:], strict=False)}
    assert gaps == {0.1}
    # And the last one is exactly its index away from the first.
    assert (times[-1] - times[0]).total_seconds() == pytest.approx(
        int(rows[-1]["sample_index"]) * 0.1
    )


def test_a_channel_can_be_named_by_index(tmp_path):
    archive = build_day_archive(
        tmp_path / "card",
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 60)],
    )
    result = export_archive(
        archive, tmp_path / "out", signals=["1"], max_samples=5
    )
    assert result.signal_files == 1
    assert next(result.directory.glob("session_0001/signal_01_*.csv")).exists()


def test_an_unknown_channel_name_is_refused_rather_than_skipped(tmp_path):
    """This test asserted the opposite until a review pointed out why.

    Silently writing nothing meant a typo produced a successful export missing
    exactly the channel that was asked for — a quiet omission dressed as a
    result.
    """
    from prisma_vent.decode import SignalSelectionError

    archive = build_day_archive(
        tmp_path / "card",
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 60)],
    )
    with pytest.raises(SignalSelectionError, match="No Such Channel"):
        export_archive(archive, tmp_path / "out", signals=["No Such Channel"])


# --------------------------------------------------------------------------
# The long-term record
# --------------------------------------------------------------------------


def _archive_with_usage(tmp_path, records):
    from synthetic import build_statistic, build_usage_record

    return build_day_archive(
        tmp_path / "card",
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 120)],
        extra_members={
            "statistic.proto": build_statistic(
                records=[build_usage_record(**r) for r in records],
                therapy_total=100_000,
            )
        },
    )


def test_the_long_term_record_is_exported(tmp_path):
    archive = _archive_with_usage(
        tmp_path, [{"timestamp": 1_000_000, "duration": 400}]
    )
    directory = export_archive(archive, tmp_path / "out").directory
    rows = read_jsonl(directory / "usage.jsonl")
    assert len(rows) == 1
    assert rows[0]["duration_minutes"] == 400


def test_a_usage_row_carries_its_therapy_day_so_binning_is_not_re_derived(tmp_path):
    """Noon-to-noon is a decision; the consumer should not have to rediscover it."""
    from prisma_vent.statistic import EPOCH_OFFSET

    # Choose a moment that lands before noon, where noon-to-noon and midnight
    # binning disagree — otherwise the test would pass on either rule.
    stamp = int(
        (datetime(2021, 6, 15, 3, 0, 0) - datetime(1970, 1, 1) - EPOCH_OFFSET
         ).total_seconds()
    )
    archive = _archive_with_usage(tmp_path, [{"timestamp": stamp, "duration": 300}])
    directory = export_archive(archive, tmp_path / "out").directory
    row = read_jsonl(directory / "usage.jsonl")[0]
    assert row["start_device_local"] == "2021-06-15T03:00:00"
    assert row["therapy_day"] == "2021-06-14"        # the night belongs to the 14th


def test_usage_rows_are_not_aggregated(tmp_path):
    """Two sessions on one day stay two rows: aggregating is the consumer's call."""
    archive = _archive_with_usage(
        tmp_path,
        [
            {"timestamp": 1_000_000, "duration": 100},
            {"timestamp": 1_020_000, "duration": 200},
        ],
    )
    directory = export_archive(archive, tmp_path / "out").directory
    rows = read_jsonl(directory / "usage.jsonl")
    assert [r["duration_minutes"] for r in rows] == [100, 200]


def test_the_manifest_counts_the_usage_rows(tmp_path):
    archive = _archive_with_usage(
        tmp_path, [{"timestamp": 1, "duration": 1}, {"timestamp": 2, "duration": 1}]
    )
    directory = export_archive(archive, tmp_path / "out").directory
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["usage_records"] == 2
    assert "deduplicate on start_device_local" in manifest["usage"]


def test_an_archive_without_the_member_reports_null_not_zero(tmp_path):
    """Null distinguishes a firmware that omits it from one recording nothing."""
    archive = build_day_archive(
        tmp_path / "card",
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 60)],
    )
    directory = export_archive(archive, tmp_path / "out").directory
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["usage_records"] is None
    assert not (directory / "usage.jsonl").exists()


def test_categories_are_exported_as_indices_without_names(tmp_path):
    archive = _archive_with_usage(
        tmp_path, [{"timestamp": 1_000_000, "duration": 60}]
    )
    directory = export_archive(archive, tmp_path / "out").directory
    row = read_jsonl(directory / "usage.jsonl")[0]
    assert len(row["duration_by_category"]) == 11
    assert sum(row["duration_by_category"]) == 60
    assert not any("mode" in key or "name" in key for key in row)


# --------------------------------------------------------------------------
# Signal selectors
# --------------------------------------------------------------------------


def _one_session(tmp_path, **kwargs):
    return build_day_archive(
        tmp_path / "card",
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 60)],
        **kwargs,
    )


def test_a_negative_index_is_refused(tmp_path):
    from prisma_vent.decode import SignalSelectionError

    archive = _one_session(tmp_path)
    with pytest.raises(SignalSelectionError, match="index -1"):
        export_archive(archive, tmp_path / "out", signals=["-1"])


def test_an_index_past_the_last_channel_is_refused(tmp_path):
    from prisma_vent.decode import SignalSelectionError

    archive = _one_session(tmp_path)
    with pytest.raises(SignalSelectionError, match="index 999"):
        export_archive(archive, tmp_path / "out", signals=["999"])


def test_the_same_label_twice_writes_one_file(tmp_path):
    archive = _one_session(tmp_path)
    label = _first_label(archive)
    result = export_archive(
        archive, tmp_path / "out", signals=[label, label]
    )
    assert result.signal_files == 1


def test_a_label_and_its_index_are_the_same_channel(tmp_path):
    archive = _one_session(tmp_path)
    label = _first_label(archive)
    result = export_archive(archive, tmp_path / "out", signals=[label, "0"])
    assert result.signal_files == 1
    written = list((result.directory / "session_0001").glob("*.csv"))
    assert len(written) == 1


def _first_label(archive) -> str:
    from prisma_vent.archive import open_day_archive

    with open_day_archive(archive) as opened:
        return opened.sessions[0].header.signals[0].label


def test_a_channel_missing_from_one_session_names_that_session(tmp_path):
    """Layout is read per session, so this cannot be checked once."""
    import zipfile
    from prisma_vent.decode import SignalSelectionError
    from synthetic import SyntheticSignal, build_wmedf, session_event_xml

    start_one = datetime(2020, 3, 4, 22, 0, 0)
    start_two = datetime(2020, 3, 5, 2, 0, 0)
    (tmp_path / "card").mkdir(parents=True, exist_ok=True)
    path = tmp_path / "card" / "0123_2020-03-04.zip"
    common = SyntheticSignal("Shared", "hPa", 0.0, 10.0, 0, 100, 1, 2, data=[1] * 60)
    extra = SyntheticSignal("Only In One", "hPa", 0.0, 10.0, 0, 100, 1, 2, data=[1] * 60)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", build_wmedf([common, extra], 60, start=start_one))
        zf.writestr("event_0001.xml", session_event_xml(start_one, 60))
        zf.writestr("0002.wmedf", build_wmedf([common], 60, start=start_two))
        zf.writestr("event_0002.xml", session_event_xml(start_two, 60))

    with pytest.raises(SignalSelectionError, match="session 0002"):
        export_archive(path, tmp_path / "out", signals=["Only In One"])


def test_a_failed_selector_leaves_no_partial_signal_files(tmp_path):
    """Resolution happens for every session before any CSV is written."""
    import zipfile
    from prisma_vent.decode import SignalSelectionError
    from synthetic import SyntheticSignal, build_wmedf, session_event_xml

    start_one = datetime(2020, 3, 4, 22, 0, 0)
    start_two = datetime(2020, 3, 5, 2, 0, 0)
    (tmp_path / "card").mkdir(parents=True, exist_ok=True)
    path = tmp_path / "card" / "0123_2020-03-04.zip"
    common = SyntheticSignal("Shared", "hPa", 0.0, 10.0, 0, 100, 1, 2, data=[1] * 60)
    extra = SyntheticSignal("Only In One", "hPa", 0.0, 10.0, 0, 100, 1, 2, data=[1] * 60)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("0001.wmedf", build_wmedf([common, extra], 60, start=start_one))
        zf.writestr("event_0001.xml", session_event_xml(start_one, 60))
        zf.writestr("0002.wmedf", build_wmedf([common], 60, start=start_two))
        zf.writestr("event_0002.xml", session_event_xml(start_two, 60))

    out = tmp_path / "out"
    with pytest.raises(SignalSelectionError):
        export_archive(path, out, signals=["Only In One"])
    assert not list(out.rglob("*.csv"))


def test_a_bad_selector_maps_to_the_usage_exit_code(tmp_path):
    """Nothing failed to decode; the command line asked for the impossible."""
    from prisma_vent.cli import EXIT_USAGE, main

    archive = _one_session(tmp_path)
    status = main(
        [
            "decode",
            str(archive),
            "--output",
            str(tmp_path / "out"),
            "--signals",
            "Nope",
        ],
        out=io.StringIO(),
    )
    assert status == EXIT_USAGE


# --------------------------------------------------------------------------
# An export never half-lands on top of another
# --------------------------------------------------------------------------


def test_an_existing_export_of_the_same_archive_is_refused(tmp_path):
    from prisma_vent.decode import ExportExistsError

    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    export_archive(archive, out)
    with pytest.raises(ExportExistsError, match="same archive"):
        export_archive(archive, out)


def test_overwrite_replaces_it(tmp_path):
    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    first = export_archive(archive, out)
    second = export_archive(archive, out, overwrite=True)
    assert first.directory == second.directory
    assert (second.directory / "manifest.json").is_file()


def test_a_different_archive_with_the_same_name_is_refused(tmp_path):
    """Two cards can produce the same filename; their contents cannot match."""
    from prisma_vent.decode import ExportExistsError

    a = build_day_archive(
        tmp_path / "one",
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 60)],
    )
    b = build_day_archive(
        tmp_path / "two",
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 120)],
    )
    assert a.name == b.name
    out = tmp_path / "out"
    export_archive(a, out)
    with pytest.raises(ExportExistsError, match="different"):
        export_archive(b, out)


def test_a_destination_without_a_manifest_is_never_touched(tmp_path):
    """A mistyped destination must not eat somebody else's directory."""
    from prisma_vent.decode import ExportExistsError

    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    intruder = out / archive.stem
    intruder.mkdir(parents=True)
    (intruder / "important.txt").write_text("not mine to delete")

    with pytest.raises(ExportExistsError, match="not recognisably a complete export"):
        export_archive(archive, out)
    with pytest.raises(ExportExistsError, match="not recognisably a complete export"):
        export_archive(archive, out, overwrite=True)
    assert (intruder / "important.txt").read_text() == "not mine to delete"


def test_an_incomplete_export_is_refused(tmp_path):
    """The manifest is written last, so its absence means the run did not finish."""
    from prisma_vent.decode import ExportExistsError

    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    result = export_archive(archive, out)
    (result.directory / "manifest.json").unlink()
    with pytest.raises(ExportExistsError, match="not recognisably a complete export"):
        export_archive(archive, out)


def test_stale_signal_files_are_not_carried_over(tmp_path):
    """The second run's export must contain only the second run's files."""
    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    label = _first_label(archive)
    first = export_archive(archive, out, signals=[label])
    assert list((first.directory / "session_0001").glob("*.csv"))

    second = export_archive(archive, out, overwrite=True)
    assert second.signal_files == 0
    assert not list(second.directory.rglob("*.csv"))


def test_a_failure_midway_leaves_the_destination_untouched(tmp_path, monkeypatch):
    archive = _one_session(tmp_path)
    out = tmp_path / "out"

    import prisma_vent.decode as decode_module

    def explode(*args, **kwargs):
        raise RuntimeError("disk gave up")

    monkeypatch.setattr(decode_module, "_write_manifest", explode)
    with pytest.raises(RuntimeError, match="disk gave up"):
        export_archive(archive, out)

    assert not (out / archive.stem).exists()
    assert list(out.iterdir()) == []          # no staging directory left behind


def test_a_failure_does_not_destroy_an_earlier_export(tmp_path, monkeypatch):
    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    good = export_archive(archive, out)
    manifest_before = (good.directory / "manifest.json").read_text()

    import prisma_vent.decode as decode_module

    def explode(*args, **kwargs):
        raise RuntimeError("disk gave up")

    monkeypatch.setattr(decode_module, "_write_manifest", explode)
    with pytest.raises(RuntimeError):
        export_archive(archive, out, overwrite=True)

    assert (good.directory / "manifest.json").read_text() == manifest_before


def test_a_successful_export_holds_only_this_run(tmp_path):
    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    result = export_archive(archive, out)
    names = {p.name for p in result.directory.iterdir()}
    assert names == {
        "manifest.json",
        "sessions.jsonl",
        "events.jsonl",
        "parameters.json",
        "validation-report.json",
    }


# --------------------------------------------------------------------------
# The --overwrite contract, and the failure paths around it
# --------------------------------------------------------------------------


def test_overwrite_redoes_an_export_of_the_same_archive(tmp_path):
    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    export_archive(archive, out)
    result = export_archive(archive, out, overwrite=True)
    assert (result.directory / "manifest.json").is_file()


def test_overwrite_does_not_replace_an_export_of_another_archive(tmp_path):
    """Archive names repeat across cards, so this would be somebody's night."""
    from prisma_vent.decode import ExportExistsError

    a = build_day_archive(
        tmp_path / "one",
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 60)],
    )
    b = build_day_archive(
        tmp_path / "two",
        archive_date=ARCHIVE_DATE,
        sessions=[(1, datetime(2020, 3, 4, 22, 0, 0), 120)],
    )
    assert a.name == b.name
    out = tmp_path / "out"
    first = export_archive(a, out)
    before = (first.directory / "manifest.json").read_text()

    with pytest.raises(ExportExistsError, match="different"):
        export_archive(b, out, overwrite=True)
    assert (first.directory / "manifest.json").read_text() == before


def test_overwrite_never_replaces_a_directory_without_a_manifest(tmp_path):
    from prisma_vent.decode import ExportExistsError

    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    intruder = out / archive.stem
    intruder.mkdir(parents=True)
    (intruder / "notes.txt").write_text("someone else's")

    with pytest.raises(ExportExistsError, match="not recognisably a complete export"):
        export_archive(archive, out, overwrite=True)
    assert (intruder / "notes.txt").read_text() == "someone else's"


def _failing_rename(monkeypatch, fail_on):
    """Make the *fail_on*-th os.rename inside decode raise."""
    import prisma_vent.decode as decode_module

    real = decode_module.os.rename
    calls = {"n": 0}

    def fake(src, dst):
        calls["n"] += 1
        if calls["n"] in fail_on:
            raise OSError(f"rename {calls['n']} refused")
        return real(src, dst)

    monkeypatch.setattr(decode_module.os, "rename", fake)
    return calls


def test_a_failed_swap_puts_the_previous_export_back(tmp_path, monkeypatch):
    """First rename moves the old aside; the second fails; restore succeeds."""
    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    good = export_archive(archive, out)
    before = (good.directory / "manifest.json").read_text()

    _failing_rename(monkeypatch, fail_on={2})
    with pytest.raises(OSError, match="rename 2 refused"):
        export_archive(archive, out, overwrite=True)

    assert (good.directory / "manifest.json").read_text() == before
    assert not list(out.glob(".*partial*"))


def test_a_failed_restore_keeps_the_backup_and_names_it(tmp_path, monkeypatch):
    """Both moves fail. The previous export must survive somewhere findable."""
    from prisma_vent.decode import ExportReplaceError

    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    good = export_archive(archive, out)
    before = (good.directory / "manifest.json").read_text()

    _failing_rename(monkeypatch, fail_on={2, 3})
    with pytest.raises(ExportReplaceError) as caught:
        export_archive(archive, out, overwrite=True)

    message = str(caught.value)
    assert "NOT been deleted" in message
    backups = list(out.glob(".*replaced*"))
    assert len(backups) == 1
    assert str(backups[0]) in message
    assert (backups[0] / "manifest.json").read_text() == before


def test_the_backup_is_removed_only_after_a_successful_swap(tmp_path):
    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    export_archive(archive, out)
    result = export_archive(archive, out, overwrite=True)
    assert (result.directory / "manifest.json").is_file()
    assert not list(out.glob(".*replaced*"))
    assert [p.name for p in out.iterdir()] == [archive.stem]


def test_a_failure_before_the_swap_leaves_the_old_export_whole(tmp_path, monkeypatch):
    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    good = export_archive(archive, out)
    names_before = sorted(p.name for p in good.directory.iterdir())
    before = (good.directory / "manifest.json").read_text()

    import prisma_vent.decode as decode_module

    monkeypatch.setattr(
        decode_module,
        "_write_manifest",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stopped early")),
    )
    with pytest.raises(RuntimeError, match="stopped early"):
        export_archive(archive, out, overwrite=True)

    assert sorted(p.name for p in good.directory.iterdir()) == names_before
    assert (good.directory / "manifest.json").read_text() == before
    assert not list(out.glob(".*replaced*"))
    assert not list(out.glob(".*partial*"))


# --------------------------------------------------------------------------
# A hash field alone is not a licence to delete
# --------------------------------------------------------------------------


def _decoy(tmp_path, manifest, *, members=None, keep=None):
    """A destination directory holding whatever *manifest* says, plus files."""

    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    target = out / archive.stem
    target.mkdir(parents=True)
    if manifest is not None:
        (target / "manifest.json").write_text(json.dumps(manifest))
    present = (
        keep
        if keep is not None
        else ["sessions.jsonl", "events.jsonl", "parameters.json",
              "validation-report.json"]
    )
    for name in present:
        (target / name).write_text("")
    for name in (members or []):
        (target / name).write_text("decoy")
    return archive, out, target


def _good_manifest(archive):
    from prisma_vent.decode import EXPORT_SCHEMA_VERSION, _sha256_of

    return {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "archive_name": archive.name,
        "archive_sha256": _sha256_of(archive),
    }


def test_a_manifest_holding_only_a_matching_hash_is_not_enough(tmp_path):
    """The hole this finding closed: a hash field licensed a recursive delete."""
    from prisma_vent.decode import ExportExistsError, _sha256_of

    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    target = out / archive.stem
    target.mkdir(parents=True)
    (target / "manifest.json").write_text(
        json.dumps({"archive_sha256": _sha256_of(archive)})
    )
    (target / "precious.txt").write_text("keep me")

    with pytest.raises(ExportExistsError, match="export schema version"):
        export_archive(archive, out, overwrite=True)
    assert (target / "precious.txt").read_text() == "keep me"


def test_an_unsupported_schema_version_is_never_replaced(tmp_path):
    from prisma_vent.decode import ExportExistsError

    archive = _one_session(tmp_path)
    manifest = _good_manifest(archive) | {"export_schema_version": 99}
    _, out, target = _decoy(tmp_path, manifest, members=["precious.txt"])
    with pytest.raises(ExportExistsError, match="export schema version 99"):
        export_archive(archive, out, overwrite=True)
    assert (target / "precious.txt").read_text() == "decoy"


def test_a_missing_required_file_is_never_replaced(tmp_path):
    from prisma_vent.decode import ExportExistsError

    archive = _one_session(tmp_path)
    _, out, target = _decoy(
        tmp_path,
        _good_manifest(archive),
        keep=["sessions.jsonl", "events.jsonl", "parameters.json"],
        members=["precious.txt"],
    )
    with pytest.raises(ExportExistsError, match="validation-report.json"):
        export_archive(archive, out, overwrite=True)
    assert (target / "precious.txt").read_text() == "decoy"


def test_a_required_file_that_is_a_symlink_does_not_count(tmp_path):
    """Following one would take the delete somewhere else entirely."""
    from prisma_vent.decode import ExportExistsError

    archive = _one_session(tmp_path)
    _, out, target = _decoy(
        tmp_path,
        _good_manifest(archive),
        keep=["sessions.jsonl", "events.jsonl", "parameters.json"],
    )
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}")
    (target / "validation-report.json").symlink_to(elsewhere)

    with pytest.raises(ExportExistsError, match="validation-report.json"):
        export_archive(archive, out, overwrite=True)
    assert elsewhere.read_text() == "{}"


def test_a_manifest_naming_another_archive_is_never_replaced(tmp_path):
    """No stray file here: identity must refuse on its own merits."""
    from prisma_vent.decode import ExportExistsError

    archive = _one_session(tmp_path)
    manifest = _good_manifest(archive) | {"archive_name": "9999_2020-01-01.zip"}
    _, out, target = _decoy(tmp_path, manifest)
    before = sorted(p.name for p in target.iterdir())
    with pytest.raises(ExportExistsError, match="different archive"):
        export_archive(archive, out, overwrite=True)
    assert sorted(p.name for p in target.iterdir()) == before


def test_a_syntactically_invalid_hash_is_never_replaced(tmp_path):
    from prisma_vent.decode import ExportExistsError

    archive = _one_session(tmp_path)
    manifest = _good_manifest(archive) | {"archive_sha256": "not-a-digest"}
    _, out, target = _decoy(tmp_path, manifest, members=["precious.txt"])
    with pytest.raises(ExportExistsError, match="no valid archive_sha256"):
        export_archive(archive, out, overwrite=True)
    assert (target / "precious.txt").read_text() == "decoy"


def test_a_valid_hash_of_something_else_is_never_replaced(tmp_path):
    from prisma_vent.decode import ExportExistsError

    archive = _one_session(tmp_path)
    manifest = _good_manifest(archive) | {"archive_sha256": "0" * 64}
    _, out, target = _decoy(tmp_path, manifest)
    before = sorted(p.name for p in target.iterdir())
    with pytest.raises(ExportExistsError, match="sha256 differs"):
        export_archive(archive, out, overwrite=True)
    assert sorted(p.name for p in target.iterdir()) == before


def test_a_genuine_complete_export_is_still_replaceable(tmp_path):
    """The permission must survive being narrowed."""
    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    export_archive(archive, out)
    result = export_archive(archive, out, overwrite=True)
    assert (result.directory / "manifest.json").is_file()


# --------------------------------------------------------------------------
# A backup that could not be removed
# --------------------------------------------------------------------------


def test_a_failed_cleanup_is_reported_and_keeps_both_copies(tmp_path, monkeypatch):
    from prisma_vent.decode import ExportCleanupError

    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    first = export_archive(archive, out)
    before = (first.directory / "manifest.json").read_text()

    import prisma_vent.decode as decode_module

    def refuse(path, *args, **kwargs):
        raise OSError("directory is busy")

    monkeypatch.setattr(decode_module.shutil, "rmtree", refuse)
    with pytest.raises(ExportCleanupError) as caught:
        export_archive(archive, out, overwrite=True)

    message = str(caught.value)
    # The new export is in place and complete.
    assert (out / archive.stem / "manifest.json").is_file()
    assert "written successfully" in message

    # The old one survives, whole, at a path the message names.
    backups = list(out.glob(".*replaced*"))
    assert len(backups) == 1
    assert str(backups[0]) in message
    assert (backups[0] / "manifest.json").read_text() == before
    assert "delete it yourself" in message


def test_a_failed_cleanup_is_not_reported_as_a_failed_rollback(tmp_path, monkeypatch):
    """One says the data is missing, the other that it is in place."""
    from prisma_vent.decode import ExportCleanupError, ExportReplaceError

    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    export_archive(archive, out)

    import prisma_vent.decode as decode_module

    monkeypatch.setattr(
        decode_module.shutil,
        "rmtree",
        lambda *a, **k: (_ for _ in ()).throw(OSError("busy")),
    )
    with pytest.raises(ExportCleanupError) as caught:
        export_archive(archive, out, overwrite=True)
    assert not isinstance(caught.value, ExportReplaceError)
    assert "could not restore" not in str(caught.value)


def test_a_failed_cleanup_maps_to_the_io_exit_code(tmp_path, monkeypatch):
    from prisma_vent.cli import EXIT_IO, main

    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    export_archive(archive, out)

    import prisma_vent.decode as decode_module

    monkeypatch.setattr(
        decode_module.shutil,
        "rmtree",
        lambda *a, **k: (_ for _ in ()).throw(OSError("busy")),
    )
    status = main(
        ["decode", str(archive), "--output", str(out), "--overwrite"],
        out=io.StringIO(),
    )
    assert status == EXIT_IO


def test_a_clean_swap_still_exits_zero(tmp_path):
    from prisma_vent.cli import EXIT_OK, main

    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    export_archive(archive, out)
    status = main(
        ["decode", str(archive), "--output", str(out), "--overwrite"],
        out=io.StringIO(),
    )
    assert status == EXIT_OK
    assert not list(out.glob(".*replaced*"))


# --------------------------------------------------------------------------
# Anything added beside an export is not ours to delete
# --------------------------------------------------------------------------


def _fingerprint(directory: Path) -> dict:
    """Every entry under *directory*, with its bytes, so loss is detectable."""
    out = {}
    for item in sorted(directory.rglob("*")):
        key = item.relative_to(directory).as_posix()
        if item.is_symlink():
            out[key] = ("symlink", os.readlink(item))
        elif item.is_dir():
            out[key] = ("dir", None)
        else:
            out[key] = ("file", item.read_bytes())
    return out


def _export_then(tmp_path, add):
    """A genuine export, then *add* puts something beside it."""
    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    result = export_archive(archive, out)
    add(result.directory)
    return archive, out, result.directory


def _both_ways_refused(archive, out, directory, pattern):
    from prisma_vent.decode import ExportExistsError

    before = _fingerprint(directory)
    for overwrite in (False, True):
        with pytest.raises(ExportExistsError, match=pattern):
            export_archive(archive, out, overwrite=overwrite)
    assert _fingerprint(directory) == before


def test_a_stray_file_beside_an_export_stops_the_replacement(tmp_path):
    archive, out, directory = _export_then(
        tmp_path, lambda d: (d / "notes.txt").write_text("my own analysis")
    )
    _both_ways_refused(archive, out, directory, "did not write")


def test_a_stray_top_level_directory_stops_it(tmp_path):
    def add(d):
        (d / "workings").mkdir()
        (d / "workings" / "sums.csv").write_text("a,b\n1,2\n")

    archive, out, directory = _export_then(tmp_path, add)
    _both_ways_refused(archive, out, directory, "did not write")


def test_a_stray_file_inside_a_session_directory_stops_it(tmp_path):
    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    label = _first_label(archive)
    result = export_archive(archive, out, signals=[label])
    (result.directory / "session_0001" / "notes.txt").write_text("mine")
    _both_ways_refused(archive, out, result.directory, "did not write")


def test_an_extra_directory_level_stops_it(tmp_path):
    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    label = _first_label(archive)
    result = export_archive(archive, out, signals=[label])
    deeper = result.directory / "session_0001" / "raw"
    deeper.mkdir()
    (deeper / "extra.csv").write_text("x\n")
    _both_ways_refused(archive, out, result.directory, "did not write")


def test_a_stray_symlink_stops_it_and_is_not_followed(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("not in the export")

    archive, out, directory = _export_then(
        tmp_path, lambda d: (d / "shortcut.txt").symlink_to(outside)
    )
    _both_ways_refused(archive, out, directory, "symbolic link")
    assert outside.read_text() == "not in the export"


def test_the_message_names_the_paths_but_not_their_contents(tmp_path):
    from prisma_vent.decode import ExportExistsError

    archive, out, _ = _export_then(
        tmp_path, lambda d: (d / "notes.txt").write_text("secret analysis")
    )
    with pytest.raises(ExportExistsError) as caught:
        export_archive(archive, out, overwrite=True)
    assert "notes.txt" in str(caught.value)
    assert "secret analysis" not in str(caught.value)


def test_a_clean_export_is_still_replaceable(tmp_path):
    """The permission must survive being narrowed twice over."""
    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    export_archive(archive, out)
    result = export_archive(archive, out, overwrite=True)
    assert (result.directory / "manifest.json").is_file()


def test_legitimate_optional_artefacts_are_still_allowed(tmp_path):
    """usage.jsonl and signal CSVs are ours; they must not read as strays."""
    archive = _archive_with_usage(tmp_path, [{"timestamp": 1_000_000, "duration": 5}])
    out = tmp_path / "out"
    label = _first_label(archive)
    first = export_archive(archive, out, signals=[label])
    assert (first.directory / "usage.jsonl").is_file()
    assert list((first.directory / "session_0001").glob("*.csv"))

    result = export_archive(archive, out, signals=[label], overwrite=True)
    assert (result.directory / "usage.jsonl").is_file()


def test_the_manifest_declares_every_file_it_produced(tmp_path):
    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    label = _first_label(archive)
    result = export_archive(archive, out, signals=[label])
    manifest = json.loads((result.directory / "manifest.json").read_text())
    on_disk = sorted(
        p.relative_to(result.directory).as_posix()
        for p in result.directory.rglob("*")
        if p.is_file()
    )
    assert manifest["files"] == on_disk
    assert "manifest.json" in manifest["files"]


def test_an_export_without_a_declared_file_list_falls_back_to_the_allowlist(tmp_path):
    """Written by an earlier build of the same schema version."""
    from prisma_vent.decode import ExportExistsError

    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    result = export_archive(archive, out)
    manifest_path = result.directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["files"]
    manifest_path.write_text(json.dumps(manifest))

    # Still replaceable: everything present matches the allowlist.
    export_archive(archive, out, overwrite=True)

    # But a stray file is still caught.
    result = export_archive(archive, out, overwrite=True)
    manifest = json.loads((result.directory / "manifest.json").read_text())
    del manifest["files"]
    (result.directory / "manifest.json").write_text(json.dumps(manifest))
    (result.directory / "notes.txt").write_text("mine")
    with pytest.raises(ExportExistsError, match="did not write"):
        export_archive(archive, out, overwrite=True)


# --------------------------------------------------------------------------
# The declared file list must correspond exactly to what is on disk
# --------------------------------------------------------------------------


def _export_with_manifest(tmp_path, mutate):
    """A genuine export whose manifest is then altered by *mutate*."""
    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    result = export_archive(archive, out)
    path = result.directory / "manifest.json"
    manifest = json.loads(path.read_text())
    mutate(manifest, result.directory)
    path.write_text(json.dumps(manifest))
    return archive, out, result.directory


def _refused(archive, out, directory, pattern):
    from prisma_vent.decode import ExportExistsError

    before = _fingerprint(directory)
    for overwrite in (False, True):
        with pytest.raises(ExportExistsError, match=pattern):
            export_archive(archive, out, overwrite=overwrite)
    assert _fingerprint(directory) == before


def test_a_declared_path_that_is_not_on_disk_is_refused(tmp_path):
    """Subset-checking would have let a phantom declaration legitimise a file."""
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: m["files"].append("notes.txt")
    )
    _refused(archive, out, directory, "declared but missing")


def test_a_file_on_disk_that_is_not_declared_is_refused(tmp_path):
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: (d / "notes.txt").write_text("mine")
    )
    _refused(archive, out, directory, "notes.txt")


def test_a_duplicate_entry_is_refused(tmp_path):
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: m["files"].append("events.jsonl")
    )
    _refused(archive, out, directory, "more than once")


def test_an_absolute_path_is_refused(tmp_path):
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: m["files"].append("/etc/passwd")
    )
    _refused(archive, out, directory, "absolute path")


def test_a_parent_traversal_is_refused(tmp_path):
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: m["files"].append("../notes.txt")
    )
    _refused(archive, out, directory, "not canonical")


def test_an_embedded_traversal_is_refused(tmp_path):
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: m["files"].append("a/../notes.txt")
    )
    _refused(archive, out, directory, "not canonical")


def test_a_leading_dot_segment_is_refused(tmp_path):
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: m["files"].append("./notes.txt")
    )
    _refused(archive, out, directory, "not canonical")


def test_an_empty_path_is_refused(tmp_path):
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: m["files"].append("")
    )
    _refused(archive, out, directory, "is empty")


def test_a_backslash_path_is_refused(tmp_path):
    """Platform-dependent separators would mean different things to different readers."""
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: m["files"].append("session_0001\\\\signal.csv")
    )
    _refused(archive, out, directory, "backslash")


def test_a_trailing_slash_is_refused(tmp_path):
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: m["files"].append("session_0001/")
    )
    _refused(archive, out, directory, "ends with a slash")


def test_a_manifest_not_listing_itself_is_refused(tmp_path):
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: m["files"].remove("manifest.json")
    )
    _refused(archive, out, directory, "does not list manifest.json")


def test_an_unimplied_empty_directory_is_refused(tmp_path):
    """Nothing declares it, and an empty directory holds no file to notice."""
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: (d / "workings").mkdir()
    )
    _refused(archive, out, directory, r"workings/ \(directory\)")


def test_a_files_field_that_is_not_a_list_is_refused(tmp_path):
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: m.__setitem__("files", "events.jsonl")
    )
    _refused(archive, out, directory, "not a list")


def test_a_files_entry_that_is_not_a_string_is_refused(tmp_path):
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: m["files"].append(42)
    )
    _refused(archive, out, directory, "not a string")


def test_a_null_files_field_does_not_fall_back_to_the_allowlist(tmp_path):
    """Only a *missing* field is legacy. A broken one is a reason to refuse."""
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: m.__setitem__("files", None)
    )
    _refused(archive, out, directory, "not a list")


def test_an_untouched_export_is_still_accepted(tmp_path):
    archive = _one_session(tmp_path)
    out = tmp_path / "out"
    export_archive(archive, out)
    result = export_archive(archive, out, overwrite=True)
    assert (result.directory / "manifest.json").is_file()


def test_declaration_order_is_not_part_of_the_contract(tmp_path):
    """The exporter writes it sorted; validation compares sets."""
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: m["files"].reverse()
    )
    result = export_archive(archive, out, overwrite=True)
    assert (result.directory / "manifest.json").is_file()


def test_a_legacy_export_without_the_field_uses_only_the_allowlist(tmp_path):
    archive, out, directory = _export_with_manifest(
        tmp_path, lambda m, d: m.pop("files")
    )
    # Allowlist-clean, so replaceable.
    export_archive(archive, out, overwrite=True)
