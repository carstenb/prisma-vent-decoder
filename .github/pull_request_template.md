## What this changes, and why

<!-- The why matters more than the what. This repository's history is part of
     its documentation: a later reader needs to know what evidence supported
     the change, not only that it happened. -->

## Evidence

<!-- If this changes how something is decoded or interpreted, say what
     convinced you and what would have falsified it. "It looks right" is not
     evidence; "it disagrees with the device display by exactly one hour" is. -->

## Checklist

- [ ] No device file, recording, serial number or identifying path is in this
      change — including in tests, comments, commit messages and screenshots.
- [ ] New tests use the runtime fixture builder in `tests/synthetic.py`.
      Nothing is cut from a real recording, not even shortened or redacted.
- [ ] `.venv/bin/python -m pytest` passes.
- [ ] `scripts/pre-commit --all` and `scripts/pre-commit --history` pass.
- [ ] If the exported format changed, `docs/export-schema-v1.md` changed in the
      same commit. The schema is a contract, and an undocumented change to it
      is the documentation equivalent of a silent misparse.
- [ ] If a format finding changed, `docs/format.md` records it with the
      evidence, and anything inferred is marked as inferred.
- [ ] Nothing new is guessed. A value whose meaning is unestablished is
      exported raw and named as unidentified rather than given a plausible name.
- [ ] No new threshold comes from a recording. Any number that can make a
      check fail, refuse or reject is in `docs/thresholds.md` in this same
      commit, with its public source, derivation, applicable range, boundary
      behaviour and a synthetic boundary test. If there is no public
      derivation, the check reports rather than asserts, or does not exist.
- [ ] No comment, docstring or test records what a recording contained — a
      count, a duration, a ratio, a measured extreme, or "they all agreed".
- [ ] No clinical interpretation is added — no named event, no derived index,
      no threshold applied to a therapy.

## Anything you are unsure about

<!-- Uncertainty stated plainly is welcome and is much easier to review than
     uncertainty smoothed over. -->
