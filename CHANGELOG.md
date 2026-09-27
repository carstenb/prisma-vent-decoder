# Changelog

Notable changes to this decoder. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Two contracts are versioned separately from the package**, because consumers
depend on them rather than on the version number:

- `export_schema_version` in an export's `manifest.json` — currently **1**;
- `decoder_schema_version` in its provenance — currently **1**.

A change to either is called out in its own right.

## [0.1.0] - 2026-09-27

First public release. Read-only throughout: nothing it does writes to the files
it is pointed at.

**What 0.1.0 does and does not promise.** This is a major version zero, so the
Python API may change in any release — module paths, function signatures and
the shape of what they return are not a contract yet. What *is* a contract is
the pair of schema versions named above: an export declaring
`export_schema_version` 1 will keep meaning what `docs/export-schema-v1.md`
says it means, and a change to either number is called out in its own right.

Consumers should depend on the schema version, not on the package version.

### Added

- **`.wmedf` signal files** — an EDF variant with a per-signal 8-bit/16-bit
  width marker and little-endian 16-bit values. Header declarations are
  validated rather than assumed.
- **Raw and physical values as separate operations**, so a wrong number can be
  attributed to byte decoding or to scaling instead of being ambiguous.
- **The device time base** — signed offsets, the noon-to-noon therapy day, and
  the several reference midnights the day-level and session-level files count
  from.
- **Day archives** read as streams from the ZIP, sessions paired with their
  event files, member names and sizes bounded, provenance recorded.
- **Events, alarms and settings snapshots**, the latter split into therapy
  programs by detecting the block structure rather than hardcoding it.
- **`.tc` trend curves** as far as their structure is known.
- **`statistic.proto`** — the device's long-term record, a start time and a
  duration for every session of a rolling year, plus a generic protobuf wire
  reader that needs no schema.
- **`prisma-vent`** with `copy-card`, `decode`, `inspect`, `usage` and
  `validate`; documented exit codes; findings on stdout, diagnostics on
  stderr.
- **A versioned export format** — see `docs/export-schema-v1.md`.
- **Five cross-checks** in `validate`, with four statuses — `passed`, `failed`,
  `reported` and `not comparable` — because those are four different claims and
  only one of them says the decode survived a test.
- **A content-based privacy guard** at `scripts/pre-commit`, in three modes:
  the staged changes for the git hook, `--all` for every tracked file, and
  `--history` for every blob in the object store. All three run in CI. It is
  written in Python because the shell version split a NUL-separated path list
  on newlines, and a git path may legally contain one — a staged file under
  such a name was scanned as two paths that did not exist, and passed. An
  additional barrier, not a scanner.
- **`docs/thresholds.md`**, listing every number that can make this decoder
  fail, refuse or reject something, with its public source, derivation,
  applicable range, boundary behaviour and the test that pins it.
- **`docs/device-reference.md` now cites a downloadable edition** with section
  and page numbers for every figure, so a reader can check the reasoning in a
  minute rather than trust it.
- **`validate` holds the delivered volume against the target that was set.**
  The one check here comparing a **setting** with a **measurement**; every
  other compares two of the device's own outputs, or an output against a
  published range. Always `reported`: what separates a delivered volume from
  its target is what the device is regulating, and a bound on it would be a
  clinical claim. It cannot change an exit code, and a test says so.

  Two things had to be settled before it could run honestly. `Target Volume`
  is a **state channel** reading zero or one, not a setpoint — the name says
  otherwise — so the comparison is restricted to the samples where targeting
  was actually running. And which programme a session ran under is not
  established, so the check does not pick a block: it asks whether the enabled
  programmes agree on exactly one *distinct* target, which is an answer that
  does not depend on knowing which ran. Two enabled programmes set to the same
  volume are therefore compared; two set differently report `not comparable`
  rather than choosing the closer.

  **Nor does it depend on which snapshot is read.** `parameter.xml` holds full
  snapshots *and* single-entry change records, so a target can move within one
  archive. Taking the last snapshot would date the setting wrongly for every
  session before the change — a 200 ml night held against a 500 ml target set
  the following noon reports a ratio of 0.4 and reads as a decoding fault.
  Reconstructing the settings history per session is the eventual answer; until
  then a target that is not constant across the archive is refused, and so is
  an archive whose change records touch the target or which programmes are
  enabled.

  A stored zero is not a target of zero millilitres. The manual's settable
  range begins at 100 ml, so a zero is not a set value — the sentinel case the
  format notes warn about, and here it is what makes the check tractable at
  all.

  The target is read through the **candidate** factor for `VolumeTarget`,
  which is the point rather than a compromise: a ratio near one is evidence for
  that candidate, a ratio nowhere near it is evidence against, and neither is
  asserted on.
- **The block-to-programme mapping is confirmed rather than only conventional.**
  Two programmes were photographed and each differs from the other in exactly
  the ways the corresponding block does. `active_program_raw` stays unresolved
  all the same, because which block is which programme and what that field
  means are two questions: a programme selected on a settings page need not be
  the one running therapy. Resolving the field would answer the second by
  assuming the first.
- **`value_domains` names the parameters that are not quantities.** Modes,
  trigger types and the pressure ramps are named states or steps; asking for
  their scale is the wrong question. Each entry says whether it is `categorical`
  or `ordinal` and carries the labels observed for the values observed.

  **The map is partial by construction and always will be.** Observation can
  show what a value displays as; it can never show that no further values
  exist. A device writing a value absent from the map is not an error, and the
  way to meet it is to show the number. Where an ordinal's label repeats its
  key, that is the finding: the raw value is displayed as-is rather than
  translated through a code mapping.

  No parameter appears both here and in a scale registry. The pairing most
  likely to be confused is the inspiratory and expiratory trigger sensitivity,
  one id apart, on device screens that look alike: the expiratory one is a
  percentage with a scale candidate, the inspiratory one a step with a domain.

  Labels are the device's own display text, quoted as written, read from a
  device configured in German — a mode designation such as `ST` is the
  manufacturer's across languages, a word such as `Manuell` is not.
- **The export says what is known about converting a raw parameter, and how
  well.** `parameters.json` gains `scales` and `scale_candidates`, keyed by
  parameter name. The first holds conversions confirmed by several distinct
  raw/display pairs giving one ratio; the second holds ones resting on a single
  reference point. A parameter in neither has no known conversion.

  **Nothing is applied.** `value_raw` is written exactly as before, for every
  parameter, confirmed or not. Neither schema version moves — these are
  additive fields, and the frozen contracts are untouched.

  A parameter with a known factor and one without would otherwise look
  identical in the file, which makes inventing a factor the path of least
  resistance. **Saying nothing does not prevent a guess; it invites one.**

  Two objects rather than one with a status column, because the separation is
  what makes the safety hold: reading `scales` cannot hand you a candidate.
  Factors are a ratio of two integers with no offset term, so a factor that
  later proves non-integral is not a type change. Both objects are emitted only
  when the archive's map and configuration versions are the pair the evidence
  was gathered against, each expected id carries its expected name, and each
  name appears once — any inconsistency empties both rather than publishing
  part of a table.

  Each candidate is written out in full. The four inspiratory-time parameters
  share a ratio because each has its own reference, not because their names
  look alike; generating them from one constant would hide four observations
  behind a resemblance, which is the reasoning this project has already had to
  retract once.
- **A structural compatibility guard for export schema version 1.**
  `docs/export-schema-v1.md` promises that existing fields will not be removed
  and that a field's type will not change; nothing compared the export against
  that promise. Around twenty tests checked individual behaviours, but none
  knew the complete set of fields, so a field could vanish and the suite stayed
  green.

  Each published release now contributes a frozen contract under
  `tests/contracts/`, listing every field path the export writes with its
  permitted types and whether it is always present. Every test run checks the
  current export against **all** of them, so a field added after one release
  and published in the next is protected too, rather than only the fields that
  existed at the first. Old contracts are never edited: a contract states what
  a release actually shipped.

  Records that share a file are described separately — the three event kinds in
  `events.jsonl`, and `parameters.json` with and without a parameter file —
  because a union over all rows would keep reporting a field after it vanished
  from one kind alone.

  **The guard is structural.** It cannot see a field that kept its name and
  changed meaning, the types it knows are only those the fixtures exercise, and
  a path the fixtures only ever saw as `null` promises presence but no type.
  All three limits are recorded where the comparison happens, because a guard
  that appears to cover more than it does is worse than none.
- **`ruff` runs in CI, on defect rules rather than style rules.** `E`, `F` and
  `B` — pycodestyle errors, pyflakes, bugbear — and nothing else. `DTZ` is
  deliberately absent: it reports every naive `datetime` here, and this decoder
  handles device local time that `docs/export-schema-v1.md` says must not be
  given a zone. Import sorting and modernisation rules are absent for the
  smaller reason that they rewrite working code to a house style this project
  has not adopted. The reasoning sits in `pyproject.toml` beside the selection
  it explains.

  It found two things worth having. A loop in the test suite did nothing at
  all — both its variables unused, its body `pass` — left behind by an earlier
  edit. And a test that walked a parsed header alongside the specification it
  was built from would have checked only as many signals as the parser
  returned, so a parser losing one would have left the test green; that `zip`
  is now `strict=True`, as are the three in `src/` where a silent truncation
  would have meant dropped sample values. The two places that pair a list with
  itself offset by one say `strict=False`, because there the lengths differ on
  purpose.

  The line limit is 100, not the default 88, which would have flagged 86 lines
  of prose in comments and error messages. Four lines were over 100 and were
  wrapped.
- **Releases are drafted by CI from the artefacts that passed the checks.**
  Building and attaching them by hand is how a wheel comes to be named
  something the program it contains does not report. A tag runs the same
  workflow as any other push: the existing packaging job builds and checks the
  artefacts once,
  and a new job attaches those same files. There is no second packaging path
  that could drift from the checked one.

  Three gates run before anything is created: the tag must equal `v` plus the
  version in `pyproject.toml`, the tagged commit must be reachable from `main`,
  and a frozen export contract for that version must exist and describe the
  export exactly. `scripts/check-release-tag.py` answers all three and can be
  run locally.

  What CI produces is a **draft**, never a published release, and its notes
  contain only the SHA-256 of both artefacts — the figures `README.md` has
  always promised and nothing produced. The rest is written by hand. Generated
  release notes were considered and rejected: they copy commit subjects
  verbatim into something published, which removes the one human reading that
  this project has already needed.

  If a run is interrupted after the draft exists but before its assets finish
  uploading, the next run refuses rather than completing or overwriting it.
  Only an unambiguous "not found" allows a draft to be created; an
  authentication or network failure stops the job, so a check that could not
  run never reads as a check that passed.
- **A second, display-independent way to check the numbering convention** is
  described in `docs/export-schema-v1.md`: hold the pressure a session
  delivered against the settings in each programme block and see which block it
  belongs to. Anyone with a card can run it, which is what makes it worth
  describing.

  **What that comparison produces on any particular card is not recorded**, in
  the documentation or anywhere else. `docs/format.md` gives the rule: a claim
  resting on comparing real files says so without quoting what the comparison
  produced, because which programme a session ran on describes a person's
  therapy rather than the format.
- **The README says how to install without PyPI.** The wheel and sdist are
  attached to each GitHub release, and the release notes carry the SHA-256 of
  both, so a download can be checked before it is installed. The install
  command deliberately does **not** name a version: a pinned filename in a
  README goes stale at the next release and then tells people to install an old
  one. Naming files one by one is the same mistake that `MANIFEST.in` makes
  every time a list there falls behind the directory it describes, so the
  install command enumerates nothing.
- **`docs/format.md` now says what `battery/*.wmedf` is.** It carries the same
  extension as the signal files and is a different format: standard EDF,
  version field `0`, every channel 16 bits, no per-signal width markers. This
  package refuses them by the version field and does not read them; a standard
  EDF reader handles them correctly. Two properties are noted for anyone
  pointing one at them — the record count is routinely `-1`, so the file's size
  is the authority, and some channels are flags packed into 16 bits under an
  8-bit declared range they then exceed.

### Changed before first release

Nothing here is a released-behaviour change — there is no earlier release — but
each was a defect worth recording, because the reasoning is the documentation.

- **Bounds derived from a lower bound have been withdrawn.** The manual gives
  maximum air flow as *above* 220 l/min, a guaranteed minimum capability. It
  had been used as a ceiling for flow, and multiplied by an inspiratory time to
  bound a single breath at 15 litres. Both are gone, along with rate and pulse
  ceilings that rested on uncited assertions about the human body, and every
  negative floor, each of which described a recording rather than a published
  figure. See `WITHDRAWN_BOUNDS` in `src/prisma_vent/device_limits.py`.
- **`validate` gained a `reported` status**, and the breath-rate and volume
  checks now use it instead of `passed`. `reported` means the check ran and
  produced a figure the decoder asserts nothing about. The gross-disagreement
  factor the breath-rate check used to fail on was withdrawn: the number came
  from what some misparses happened to produce during development, which is a
  threshold fitted to evidence that cannot be published.
- **`copy-card` no longer treats any file named `copy-manifest.json` as proof
  that a destination is its own.** Combined with path-based writes, that was
  enough to place copied health data outside the chosen destination and to
  change permissions on a directory elsewhere, by making a needed subdirectory
  a symlink. Every write now goes through a directory handle opened with
  no-follow semantics, the manifest is validated for schema, source identity
  and state before a destination is resumed, and it is written atomically. The
  manifest version is now 2; a version-1 manifest is refused rather than
  trusted, since it was written before any of this was checked.
- **`MIN_PHASE_DUTY_CYCLE` corrected from 0.01 to 0.001.** The old value was
  derived from a shortest inspiratory time of 0.5 s. The manual gives 0.5 s for
  `Ti/Ti max` but 0.2 s for `Ti min`, so the old floor sat above what the
  device can produce.
- **The start-skew bound is gone entirely.** It was 2 s, then 60 s; neither
  had a derivation from the format or from anything the manufacturer
  publishes, so both could refuse correct data and accept mispaired data. The
  skew is now measured, carried on `Session.start_skew` and exported as
  `start_skew_seconds`, and nothing rejects on it. What still rejects a wrong
  reference midnight is the therapy-day check, which needs no chosen number.
- **`share-safe` was withdrawn rather than fixed again.** It promised a report
  a bug reporter could paste without reading it. Three successive audits found
  three different leaks: masking digits left every alphabetic part of a member
  name intact, so `patient-<name>-private-notes.txt` passed through untouched;
  channel labels and units had the same hole; and the report disclosed whether
  an archive held any session at all, which is a count. Each fix grew the code
  that then had to be trusted, and the promise — *safe without reading it* —
  is the hardest kind to keep, because it has to hold for every input the
  device can produce and not merely for the ones tested.

  The replacement is a rule that fits in a sentence and needs no audit: **send
  a synthetic reproduction, and nothing else.** `tests/synthetic.py` writes
  byte-exact files from invented data, which carries no health data at all.
  README, `SECURITY.md`, `CONTRIBUTING.md` and the issue template now say that
  no output of this program is safe to paste — including `inspect`, about
  which no such claim was ever audited.
- **`copy-card` no longer treats a mount path as a card's identity.** Copying
  card A from `/Volumes/CARD`, ejecting it and inserting card B left the
  resolved source path unchanged, so the destination looked resumable and the
  two cards' disjoint files were merged into one backup. The manifest now
  records the source inventory, and a resume requires every file the earlier
  run planned to still be present, at the same path, the same size **and with
  the same content**. Growth is allowed; disappearance and change are not.

  Path and size alone were not enough, and left a plausible way in: interrupt a
  copy, change a file to different content of the same length, resume — the
  inventory still matched, the resume was accepted, and the changed bytes were
  copied in beside the ones already there. On a card a device writes to, a
  same-size rewrite is not exotic. The inventory therefore carries a SHA-256
  per file, and `source_inventory_sha256` is mandatory and recomputed from the
  recorded entries rather than trusted, so an edited inventory cannot declare a
  card's identity.

  This costs a whole extra read of the card: the hashes have to be in the
  manifest *before* copying starts, because an interrupted run's manifest is
  exactly what a resume validates against. The inventory pass's digests are
  deliberately **not** reused as the digest each file is copied against — that
  pass holds its own descriptors, so reusing them would put a second lookup
  back between hashing and copying, which is the hole the source-side rewrite
  had just closed. Manifest version 4; versions 1 to 3 are refused rather than
  trusted, since each recorded less than the check now needs.
- **`copy-card` installs a finished file without replace semantics.**
  `os.replace` overwrote anything that appeared under the target name between
  the existence check and the rename, contradicting the no-overwrite property
  this package states. It now uses `link` + `unlink`, which fails on `EEXIST`
  against a file, a directory or a symlink alike.
- **Duplicate long-term timestamps are no longer collapsed.** The index was a
  `{start: record}` comprehension, so several records at one instant became
  whichever came last — and the same file passed or failed depending only on
  the order its records were written in. All records are kept and a session
  matching more than one is reported `not comparable`.
- **The source distribution ships the complete tests directory.** It used to
  ship `tests/test_*.py` without `tests/synthetic.py`, which every one of them
  imports, so an unpacked sdist collected ten `ModuleNotFoundError`. CI now
  unpacks the sdist and runs the tests it ships.

### Known limits

Stated here as well as in the README, because a changelog is what gets read
when something looks wrong:

- **No event or alarm id has been identified.** The names are known; the
  mapping to the numeric ids is not. Nothing is named.
- **One parameter has a confirmed scale**, and nine carry a candidate factor
  resting on a single reference point. No parameter in either group is
  converted: every value is exported raw.
- **The device clock's relationship to UTC is unresolved**, and daylight saving
  is not handled.
- **An absent sensor records a plain zero**, indistinguishable in the data from
  a genuine reading of zero.
- **Most channels have no published bound**, so the one external check covers
  few of them. Absence means *not checked*, never *unbounded*.
- **BTPS and STPD coexist in one file** and no conversion is done.
- One device model on one firmware. A successful run against one card is not a
  claim about any other.
