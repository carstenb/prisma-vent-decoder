# Contributing

Thank you for looking. This is a personal project that was made public because
the format knowledge in it is worth sharing, not because it needs a team — so
please open an issue before writing anything substantial, and expect replies to
take days rather than hours.

## Read this first: never send device files

**Do not attach `.zip`, `.wmedf`, `.tc`, `.proto` or device XML to an issue, a
pull request or a comment.** They carry health data and a device serial number,
and a public repository is forever.

This is not a formality. The single failure this project cannot walk back is
somebody's recording ending up in a public place, and the most likely way for
that to happen is a well-meaning bug report with a helpful attachment.

Also keep out: screenshots showing recordings or serial numbers, full paths
containing your name, and pasted output you have not read through first.

**No output of this program is safe to paste, and none is offered as such.**
`prisma-vent inspect` names the archive and can print statistics measured from
a recording. Do not treat its output as safe, and please do not ask a reporter
to paste part of it.

A `share-safe` command promising a pasteable report existed briefly and was
withdrawn. Each audit found another leak in it — free-text channel labels,
unrecognised member names, whether the archive held any session at all — and
each fix grew the code that had to be trusted. The replacement is a rule:
**send a synthetic reproduction, and nothing else.**

**Test fixtures are generated at runtime and are never committed.**
`tests/synthetic.py` builds byte-exact `.wmedf`, ZIP,
XML and protobuf files from invented data. If you need a new shape to
reproduce something, add a builder there — do not cut a fixture from a real
recording, not even a short one, not even with the serial removed.

A privacy guard enforces part of this. Install it once:

```bash
ln -s ../../scripts/pre-commit .git/hooks/pre-commit
```

It inspects staged **content**, not filenames, so renaming a recording does not
get it past. `scripts/pre-commit --all` scans every tracked file, which is what
CI runs. **It is an additional barrier, not a guarantee** — it does not replace
looking at what you are about to commit.

## What this project will and will not do

The boundary matters more here than the coding style, so please take it as
fixed rather than as an opening position:

**It will not interpret data clinically.** No event is named, no apnoea or leak
index is computed, no threshold is applied to a therapy. Not because it would be
hard — because no event id has been identified, and a plausible guess wearing
the clothes of a measurement is exactly the failure this project exists to
avoid. A pull request adding clinical meaning will be declined however good the
code is.

**It fails loudly.** A file that is not what it claims to be raises. It does not
fall back, clamp, interpolate or return a best effort. If you find yourself
writing `except: pass` or a default value, that is the signal to stop.

**Nothing is guessed.** Where a value's meaning is unestablished it is exported
raw, named as unidentified, and documented as such. "It is probably the mode"
is a note in `docs/`, not a field name.

**Findings are recorded with their evidence.** `docs/format.md` says how
each conclusion was reached and how strongly. If you establish something new,
write down what would have falsified it.

**No runtime dependencies.** The standard library is enough and staying that way
is deliberate.

## Setting up

The interpreter is named explicitly at every step, because a bare `python` is
how a system interpreter gets used by accident:

```bash

python3.13 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

Python 3.11 to 3.13 are supported; CI runs all three.

## Changes that are welcome

- **Another device or firmware.** The most valuable thing anyone else can
  contribute is evidence that these formats do or do not hold elsewhere.
  Describe in words what a different model or firmware writes — which channels,
  which widths, which member names — rather than pasting a program's output.
- **Identifying an event or alarm id**, with the evidence. The route that would
  work: note the text the device displayed and the minute it appeared, then find
  the id logged at that minute. One matched id is worth more than a plausible
  table of twenty.
- **Falsifying something in `docs/format.md`.** Several conclusions there rest on one
  card. If your data contradicts one, that is a contribution, not a complaint.
- **Bugs, tests, documentation**, and anything that makes a wrong number easier
  to notice.

## Pull requests

- One change per pull request, with its own tests.
- Tests use synthetic fixtures only.
- Run `.venv/bin/python -m pytest` and `scripts/pre-commit --all` before
  pushing.
- If you change what the exporter writes, update `docs/export-schema-v1.md` in
  the same commit — the schema is a contract, and a silent change to it is the
  documentation equivalent of a silent misparse.
- Explain in the message *why*, and what evidence supports it. This repository's
  history is part of its documentation.

## Style

Match what is there. In particular: comments explain why a thing is the way it
is, especially where the obvious approach is wrong. Several of them exist
because the obvious approach *was* taken first and turned out not to survive.

**A comment must not record what a recording contained.** A bound justified by
what was measured on one card is a measurement of somebody's therapy written
into a public file, and it stays one when the figure is left out — the bound
then carries it, and so does a sentence saying the bound sits "just above" it.
It is also a threshold nobody else can check.

Say what the format guarantees, or what the manufacturer publishes, or say
that the question is open — and where a number has no public derivation,
report it rather than assert on it. `docs/thresholds.md` records every number
this decoder still asserts on and where each one comes from; a new one belongs
there in the same commit.
