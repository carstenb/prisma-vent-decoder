#!/usr/bin/env python3
"""Decide whether this tree may be released under a given tag.

Three questions, all of which have to be answered before anything is
published:

1. Does the tag match the version the package will call itself? A wheel named
   one thing and a program reporting another has already happened here once
   and was caught by hand.
2. Is there a frozen export contract for that version? A release without one
   is a published state that nothing will ever protect again — the next
   release's contract would not know its fields, so they could disappear
   unnoticed.
3. Does that contract describe the export *exactly*? Ordinary test runs only
   check backward compatibility, because additions are allowed within a schema
   version. At the moment of release the new contract has to be a faithful
   record, not merely a compatible one.

Run it locally the same way CI does::

    python3 scripts/check-release-tag.py v0.1.0

Exit status is 0 when the tree may be released under that tag, 1 otherwise.
This script never writes anything and never talks to the network.
"""

from __future__ import annotations

import json
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "tests" / "contracts"


def fail(message: str) -> int:
    print(f"refusing to release: {message}", file=sys.stderr)
    return 1


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def check(tag: str) -> int:
    version = project_version()
    expected = f"v{version}"
    if tag != expected:
        return fail(
            f"tag {tag!r} does not match pyproject.toml, which says version "
            f"{version!r} and so expects the tag {expected!r}. Move the tag or "
            "correct the version; releasing on a mismatch is how a wheel comes "
            "to be named differently from the program inside it."
        )

    contract_path = CONTRACTS / f"export-schema-v1-{expected}.json"
    if not contract_path.exists():
        return fail(
            f"no frozen export contract at {contract_path.relative_to(ROOT)}. "
            "Generate it from this tree before tagging:\n"
            "    PYTHONPATH=src:tests python3 tests/schema_contract.py "
            f"{contract_path.relative_to(ROOT)}\n"
            "Without it, the fields this release publishes are protected by "
            "nothing once the next release is cut."
        )

    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT / "tests"))
    from schema_contract import build_all_forms, compare_exact

    document = json.loads(contract_path.read_text(encoding="utf-8"))
    # The contract says which release it was built for. A file copied from the
    # previous release and renamed would otherwise pass every check here: the
    # name would match the tag, the shape would match the export, and only this
    # field would still name the release it actually came from.
    recorded = document.get("_generated_from", {}).get("release")
    if recorded != expected:
        return fail(
            f"{contract_path.name} records release {recorded!r}, but this is "
            f"{expected}. Regenerate it from this tree rather than renaming "
            "the previous one."
        )

    contract = document["forms"]
    with tempfile.TemporaryDirectory() as scratch:
        current = build_all_forms(Path(scratch))

    problems = compare_exact(contract, current)
    if problems:
        joined = "\n  ".join(problems)
        return fail(
            f"{contract_path.name} does not describe this export exactly:\n  "
            f"{joined}\n"
            "A release's own contract has to be a faithful record of what it "
            "publishes. Regenerate it with the command above."
        )

    print(f"{tag}: version, contract and export agree")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    tag = argv[1]
    # CI passes refs/tags/vX.Y.Z; a person passes vX.Y.Z.
    return check(tag.removeprefix("refs/tags/"))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
