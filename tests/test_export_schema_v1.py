"""The structural compatibility guard for schema version 1.

``docs/export-schema-v1.md`` promises that existing fields will not be removed
and that a field's type will not change. Twenty-odd tests in
``test_decode.py`` check individual behaviours; none knew the complete set of
fields, so a field could vanish and the suite stayed green.

Each published release contributes one frozen contract under
``tests/contracts/``. They are never edited: a contract states what a release
actually shipped, so making a breach green means writing something untrue
about a release that exists with attached artefacts, rather than tidying a
line. New releases add a file; they do not change the old ones.

The guard is **structural**. It cannot see a field that kept its name and
changed meaning; the types it knows are only those the fixtures exercise; and
a path the fixtures only ever saw as ``null`` promises presence but no type.
All three limits are stated in
``schema_contract.compare_backward_compatible``.
"""

import json
import tempfile
from pathlib import Path

import pytest

from schema_contract import (
    COVERED_FORMS,
    _merge,
    build_all_forms,
    compare_backward_compatible,
    flatten,
    json_type,
)

CONTRACT_DIR = Path(__file__).parent / "contracts"


@pytest.fixture(scope="module")
def current_forms():
    """Every output form the export writes today, described once."""
    with tempfile.TemporaryDirectory() as scratch:
        return build_all_forms(Path(scratch))


def published_contracts() -> list[Path]:
    return sorted(CONTRACT_DIR.glob("export-schema-v1-v*.json"))


# --------------------------------------------------------------------------
# The contracts themselves
# --------------------------------------------------------------------------


def test_at_least_one_published_contract_exists():
    """Without a contract the rest of this file asserts nothing at all."""
    assert published_contracts(), (
        f"no frozen contract in {CONTRACT_DIR}. One is generated per release; "
        "see tests/schema_contract.py for the command."
    )


@pytest.mark.parametrize("path", published_contracts(), ids=lambda p: p.stem)
def test_a_contract_records_where_it_came_from(path):
    """A contract without provenance is a number nobody can reproduce.

    The release rather than a commit: a commit id stops resolving when a
    history is re-created, and this one has been. The tag is what a reader can
    still check out in a year.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    source = document["_generated_from"]
    assert source["release"], f"{path.name} does not name the release it belongs to"
    assert source["command"], f"{path.name} does not record how to rebuild it"
    assert document["forms"], f"{path.name} describes no forms"


@pytest.mark.parametrize("path", published_contracts(), ids=lambda p: p.stem)
def test_a_contract_is_named_after_the_release_it_records(path):
    """The filename and the provenance must agree, or one of them is wrong.

    Cheap to get wrong by copying a contract and editing one of the two. The
    release gate compares both against the tag being pushed; this compares them
    against each other, so a mismatch is caught without cutting a release.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    assert path.stem.endswith(document["_generated_from"]["release"]), (
        f"{path.name} records release "
        f"{document['_generated_from']['release']!r}"
    )


# --------------------------------------------------------------------------
# Backward compatibility
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", published_contracts(), ids=lambda p: p.stem)
def test_the_export_still_honours_every_published_contract(path, current_forms):
    """Each released contract keeps holding, not only the most recent one.

    A single oldest contract would protect only the fields that existed then.
    Fields added later and published in a subsequent release would be free to
    disappear again, which is the gap this parametrisation closes.
    """
    contract = json.loads(path.read_text(encoding="utf-8"))["forms"]
    problems = compare_backward_compatible(contract, current_forms)
    assert not problems, (
        f"the export breaks what {path.name} published:\n  "
        + "\n  ".join(problems)
        + "\n\ndocs/export-schema-v1.md promises existing fields will not be "
        "removed and types will not change. Breaking that needs schema "
        "version 2, not an edit to a frozen contract."
    )


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------


def test_the_fixtures_exercise_every_form_the_guard_claims_to_cover(current_forms):
    """Both directions, because either gap makes the guard dishonest.

    A form written but not listed is unguarded. A form listed but never
    produced is worse: the guard reports coverage for something it has never
    examined.
    """
    produced = set(current_forms)
    claimed = set(COVERED_FORMS)
    assert produced == claimed, (
        f"written but not covered: {sorted(produced - claimed)}; "
        f"covered but never produced: {sorted(claimed - produced)}"
    )


def test_the_three_event_kinds_are_kept_apart(current_forms):
    """Folding them together would hide a field lost from one kind alone.

    ``phase`` exists on respiratory events and on alarms but not on device
    state; a union over all rows of ``events.jsonl`` would keep reporting it
    after it vanished from one of them.
    """
    respiratory = current_forms["events.jsonl[kind=respiratory]"]
    device_state = current_forms["events.jsonl[kind=device_state]"]
    assert "/phase" in respiratory
    assert "/phase" not in device_state
    assert "/status" in device_state
    assert "/status" not in respiratory


# --------------------------------------------------------------------------
# The machinery the guard rests on
# --------------------------------------------------------------------------


def test_booleans_are_not_reported_as_integers():
    """``bool`` subclasses ``int``, so the order of the checks is the check.

    Reversed, a field changing from ``true`` to ``1`` would pass as unchanged —
    the regression this module is for.
    """
    assert json_type(True) == "boolean"
    assert json_type(False) == "boolean"
    assert json_type(1) == "integer"
    assert json_type(0) == "integer"


def test_array_positions_collapse_so_the_contract_is_not_fixture_sized():
    one = flatten({"rows": [{"id": 1}]})
    two = flatten({"rows": [{"id": 1}, {"id": 2}]})
    assert one == two
    assert "/rows/*/id" in one


def test_null_is_a_type_of_its_own():
    """`null` is not absence: docs/export-schema-v1.md says so for `status`."""
    assert json_type(None) == "null"
    assert flatten({"a": None})["/a"] == "null"


def test_a_field_the_fixtures_only_saw_as_null_promises_no_type():
    """Observing only `null` is missing information, not a type promise.

    `manifest.json`'s `git_commit` is `null` in every fixture, because an
    export built in a temporary directory has no repository to read it from;
    an export from a checkout writes a string. If `null` counted as the type,
    the guard would reject that correct export — and the contract is frozen,
    so there would be no legitimate way to correct it.
    """
    contract = {"f": {"/a": {"presence": "required", "types": ["null"]}}}
    populated = {"f": {"/a": {"presence": "required", "types": ["string"]}}}
    assert compare_backward_compatible(contract, populated) == []

    # Presence is still promised, so losing the field is still a breach.
    absent = {"f": {}}
    assert compare_backward_compatible(contract, absent)


# --------------------------------------------------------------------------
# Registries whose keys are data
# --------------------------------------------------------------------------


def _registries(scales=None, candidates=None):
    """Both registries as the export always writes them — present, maybe empty.

    Modelling one of them as *absent* would not describe any export this
    decoder produces, and a registry that genuinely stopped being written is a
    breach; see the test that says so.
    """
    entry = {"unit": "hPa", "raw_delta": 100}
    return flatten(
        {
            "scales": {name: dict(entry) for name in (scales or ())},
            "scale_candidates": {name: dict(entry) for name in (candidates or ())},
        }
    )


def test_registry_membership_is_not_part_of_the_contract_shape():
    """Two different parameter names describe the same shape.

    The keys under these objects are what the decoder currently knows, which
    changes as evidence arrives. Freezing them would make every new finding a
    schema question.

    Asserted on the merged form, because that is what a contract file holds.
    The collapse used to happen in `flatten` and moved into `_merge`, where
    presence can be counted per entry; a version of this test that read
    `flatten` alone would now be checking a step that no longer decides
    anything.
    """
    one = _merge([_registries(scales=["IPAP"])])
    other = _merge([_registries(scales=["EPAP"])])
    assert set(one) == set(other)
    assert "/scales/*/unit" in one
    assert "/scales/IPAP/unit" not in one


def test_an_observed_label_is_data_too(current_forms):
    """The keys inside a value domain are raw values, not field names.

    They are the most volatile thing the export writes: seeing one more value
    of a parameter adds a key. Frozen as schema, a release would promise "these
    are the values that had been observed by then", and correcting or dropping
    one would read as a removed field.
    """
    paths = set(current_forms["parameters.json[available=true]"])
    assert "/value_domains/*/observed_labels/*" in paths
    # Every segment after the registry name is a wildcard or a field name of
    # the entry itself. A parameter name or a raw value appearing here would
    # mean the contract had frozen data.
    fields = {"evidence", "kind", "name", "observed_labels", "parameter_id", "*"}
    for path in paths:
        if not path.startswith("/value_domains/"):
            continue
        assert set(path.split("/")[2:]) <= fields, path


def test_confirming_a_candidate_does_not_break_the_contract():
    """The success case must not read as a schema break.

    A candidate that is confirmed moves from one registry to the other. Under
    a contract keyed by concrete names its old paths would vanish and the
    guard would demand schema version 2 for the one thing this project is
    working towards.
    """
    before = {"parameters.json": _merge([_registries(candidates=["EPAP"])])}
    after = {"parameters.json": _merge([_registries(scales=["EPAP"])])}
    assert compare_backward_compatible(before, after) == []


def test_an_emptied_registry_is_no_entries_not_a_removed_shape():
    """Every candidate confirmed empties that registry — also a success."""
    contract = {"parameters.json": _merge([_registries(candidates=["EPAP"])])}
    emptied = {"parameters.json": _merge([_registries(scales=["EPAP", "Ti"])])}
    assert compare_backward_compatible(contract, emptied) == []


def test_the_exemption_does_not_cover_a_registry_that_still_has_entries():
    """Otherwise the exemption above would be a hole.

    A field missing from an entry is a breach whether or not the registry has
    other members.
    """
    contract = {"parameters.json": _merge([_registries(candidates=["EPAP"])])}
    short = {
        "parameters.json": _merge(
            [flatten({"scales": {}, "scale_candidates": {"EPAP": {"unit": "hPa"}}})]
        )
    }
    problems = compare_backward_compatible(contract, short)
    assert problems and "raw_delta" in problems[0]


def test_a_registry_the_export_stopped_writing_is_still_a_breach():
    """Present-and-empty is exempt; gone altogether is not."""
    contract = {"parameters.json": _merge([_registries(candidates=["EPAP"])])}
    gone = {"parameters.json": _merge([flatten({"scales": {}})])}
    problems = compare_backward_compatible(contract, gone)
    assert problems and "/scale_candidates" in problems[0]


@pytest.mark.parametrize(
    "registry, kept, reduced",
    [
        (
            "scales",
            {"unit": "hPa", "raw_delta": 100},
            {"unit": "hPa"},
        ),
        (
            "scale_candidates",
            {"unit": "s", "raw_delta": 1000},
            {"unit": "s"},
        ),
        (
            "value_domains",
            {"kind": "ordinal", "observed_labels": {"1": "1"}},
            {"observed_labels": {"1": "1"}},
        ),
    ],
)
def test_a_field_lost_by_one_entry_is_a_breach_even_where_others_keep_it(
    registry, kept, reduced
):
    """The false negative that made the guard's promise conditional.

    ``flatten`` collapsed entry keys to ``*`` and folded them into one dict, so
    the entry that still had a field overwrote the entry that had lost it and
    the path stayed ``required``. A registry where exactly one member had been
    written short compared clean — while the documentation said the contract
    protects the shape of *each* entry.

    Two members, and only the second is reduced, so the surviving member is
    what would have hidden it. Reverting the collapse into ``flatten`` turns
    every case here green.
    """
    contract = {
        "parameters.json": _merge(
            [flatten({registry: {"First": dict(kept), "Second": dict(kept)}})]
        )
    }
    short = {
        "parameters.json": _merge(
            [flatten({registry: {"First": dict(kept), "Second": dict(reduced)}})]
        )
    }
    missing = sorted(set(kept) - set(reduced))
    problems = compare_backward_compatible(contract, short)
    assert problems and any(field in problems[0] for field in missing)


def test_a_label_inside_one_value_domain_may_not_change_type():
    """The nested dynamic level, where the keys are raw values.

    Its keys are data — observing one more value adds one — but what a label
    *is* stays part of the shape, and one entry disagreeing is enough.
    """
    contract = {
        "parameters.json": _merge(
            [
                flatten(
                    {
                        "value_domains": {
                            "A": {"observed_labels": {"1": "one"}},
                            "B": {"observed_labels": {"2": "two"}},
                        }
                    }
                )
            ]
        )
    }
    retyped = {
        "parameters.json": _merge(
            [
                flatten(
                    {
                        "value_domains": {
                            "A": {"observed_labels": {"1": "one"}},
                            "B": {"observed_labels": {"9": 9}},
                        }
                    }
                )
            ]
        )
    }
    problems = compare_backward_compatible(contract, retyped)
    assert problems and "observed_labels" in problems[0]


def test_a_registry_entry_may_not_change_a_field_type():
    contract = {"parameters.json": _merge([_registries(scales=["IPAP"])])}
    retyped = {
        "parameters.json": _merge(
            [
                flatten(
                    {
                        "scales": {"IPAP": {"unit": "hPa", "raw_delta": "100"}},
                        "scale_candidates": {},
                    }
                )
            ]
        )
    }
    problems = compare_backward_compatible(contract, retyped)
    assert problems and "raw_delta" in problems[0]


def test_a_nullable_field_with_a_known_type_still_promises_that_type():
    """`["integer", "null"]` is a real promise; `["null"]` alone is not."""
    contract = {"f": {"/a": {"presence": "required", "types": ["integer", "null"]}}}
    widened = {"f": {"/a": {"presence": "required", "types": ["string"]}}}
    assert compare_backward_compatible(contract, widened)
