"""Tests for the release gate at ``scripts/check-release-tag.py``.

The script decides whether a tree may be published under a given tag. It runs
unattended, once, immediately before artefacts are attached to a release — so
like the release-history builder beside it, the tests here are about
**refusals**. A gate that fails open is worse than no gate, because the failure
looks like a pass.

Nothing here writes to the repository or reaches a network.
"""

from __future__ import annotations

import importlib.util
import tempfile
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check-release-tag.py"


def load_script():
    """Import the script despite the hyphen in its filename."""
    spec = importlib.util.spec_from_file_location("check_release_tag", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return load_script()


def test_a_tag_that_disagrees_with_the_version_is_refused(gate, capsys):
    """The failure this gate exists for: a wheel named other than the program.

    It happened here once — the wheel said 0.1.0 while `prisma-vent --version`
    said 0.1.0.dev0 — and was caught by hand rather than by anything.
    """
    assert gate.check("v99.0.0") == 1
    assert "does not match pyproject.toml" in capsys.readouterr().err


def test_a_version_without_a_frozen_contract_is_refused(gate, monkeypatch, capsys):
    """Publishing without a contract leaves that release unprotected forever.

    The next release's contract would not know these fields, so they could
    disappear later with nothing to notice.
    """
    monkeypatch.setattr(gate, "project_version", lambda: "99.0.0")
    assert gate.check("v99.0.0") == 1
    assert "no frozen export contract" in capsys.readouterr().err


def test_a_contract_that_misdescribes_the_export_is_refused(
    gate, monkeypatch, tmp_path, capsys
):
    """A release's own contract has to be exact, not merely compatible.

    Ordinary runs allow additions within a schema version. At the moment of
    release the contract is a record of what is being published, so a field it
    claims and the export does not write is a defect in the record.
    """
    import json

    contracts = tmp_path / "contracts"
    contracts.mkdir()
    published = ROOT / "tests" / "contracts" / "export-schema-v1-v0.1.0.json"
    document = json.loads(published.read_text(encoding="utf-8"))
    # Re-labelled for the version it is copied to, so the release check
    # passes and this test still isolates the one it is about.
    document["_generated_from"]["release"] = "v99.0.0"
    document["forms"]["manifest.json"]["/never_written"] = {
        "presence": "required",
        "types": ["string"],
    }
    (contracts / "export-schema-v1-v99.0.0.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    monkeypatch.setattr(gate, "project_version", lambda: "99.0.0")
    monkeypatch.setattr(gate, "CONTRACTS", contracts)

    assert gate.check("v99.0.0") == 1
    assert "does not describe this export exactly" in capsys.readouterr().err


def test_a_contract_that_omits_something_the_export_writes_is_refused(
    gate, monkeypatch, tmp_path, capsys
):
    """The other direction, which nothing held.

    Ordinary runs allow the export to gain fields, so a contract that merely
    omits one stays compatible. At the moment of release that is exactly the
    defect: the record would not mention a field being published, and no later
    contract could tell whether it had been there from the start.

    Built from the current export and then reduced by a single path, so the
    contract is exact in every other respect. Otherwise the refusal could come
    from any of the differences a stale contract already carries, and the
    forward direction would go on being untested — which is how it went
    untested until now. **Deleting `compare_exact`'s forward loop entirely
    leaves the suite green without this test.**
    """
    import json
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "tests"))
    from schema_contract import build_all_forms

    contracts = tmp_path / "contracts"
    contracts.mkdir()
    with tempfile.TemporaryDirectory() as scratch:
        forms = build_all_forms(Path(scratch))
    removed = forms["manifest.json"].pop("/export_schema_version")
    assert removed, "the path this test removes must exist to have been removed"
    (contracts / "export-schema-v1-v9.9.9.json").write_text(
        json.dumps(
            {
                "_generated_from": {"release": "v9.9.9", "command": "y"},
                "forms": forms,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "project_version", lambda: "9.9.9")
    monkeypatch.setattr(gate, "CONTRACTS", contracts)

    assert gate.check("v9.9.9") == 1
    assert "does not describe this export exactly" in capsys.readouterr().err


def test_a_tree_whose_contract_matches_its_export_is_released(
    gate, monkeypatch, tmp_path
):
    """The gate must also say yes, or it would only ever have been tested red.

    Built here rather than asked of the working tree. A tree is releasable
    under its own version only in the moment after a release: any later change
    to the export — including one that is perfectly compatible — makes it
    differ from that version's frozen contract, which is the gate working. An
    earlier version of this test asserted the tree was always releasable and
    went red the first time the export gained a field.
    """
    import json
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "tests"))
    from schema_contract import build_all_forms

    contracts = tmp_path / "contracts"
    contracts.mkdir()
    with tempfile.TemporaryDirectory() as scratch:
        forms = build_all_forms(Path(scratch))
    (contracts / "export-schema-v1-v9.9.9.json").write_text(
        json.dumps(
            {
                "_generated_from": {"release": "v9.9.9", "command": "y"},
                "forms": forms,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "project_version", lambda: "9.9.9")
    monkeypatch.setattr(gate, "CONTRACTS", contracts)

    assert gate.check("v9.9.9") == 0


def test_the_ref_prefix_ci_passes_is_stripped(gate, capsys):
    """CI hands over `refs/tags/vX.Y.Z`; a person types `vX.Y.Z`.

    Checked on a refusal, so the assertion does not depend on the tree being
    releasable: what matters is that the tag reaching the comparison is the
    bare one.
    """
    assert gate.main(["check-release-tag.py", "refs/tags/v99.0.0"]) == 1
    assert "'v99.0.0'" in capsys.readouterr().err


def test_no_argument_is_a_usage_error_not_a_pass():
    """Called wrongly, it must not look like approval."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 2
