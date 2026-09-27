# Unofficial prisma VENT50 decoder

An independent, unofficial decoder for data a Löwenstein Medical prisma VENT50
home ventilator writes to its SD card. It reads the recordings, reports what is
in them, and refuses to guess.

> **Not medical software.** This decodes personal device data. It must not be
> used to diagnose any condition or to change ventilation settings. Verify
> anything important against the device display or a clinician's report.

> **Not affiliated with Löwenstein Medical.** No endorsement, no connection.
> Product names appear only to identify compatibility.

> **Your device files contain sensitive health data** and identifying
> information such as the device serial number. **Never attach real device
> files to a public issue.** See [Reporting problems](#reporting-problems).

**What it reads:** the card's day archives — signal files, respiratory events,
alarms and settings snapshots — along with `.tc` trend curves and the device's
own year-long usage record. **What it will not do:** say what any of it means
clinically, put a name to an event id, or apply a scale it cannot cite a source
for. Those are deliberate limits rather than gaps; [Status](#status) says what
is implemented and [Known limits](#known-limits) says where the format itself
runs out.

## Installation

Python 3.11, 3.12 or 3.13, on macOS or Linux. No runtime dependencies.

**Not on PyPI.** Download the wheel from the
[latest GitHub release](https://github.com/carstenb/prisma-vent-decoder/releases/latest)
and install the file you downloaded:

```bash
python -m pip install ./prisma_vent_decoder-*.whl
```

Every release lists the SHA-256 of both artefacts in its notes, so a downloaded
file can be checked before it is installed:

```bash
# macOS
shasum -a 256 prisma_vent_decoder-*.whl

# Linux
sha256sum prisma_vent_decoder-*.whl
```

Alternatively, install from a checkout:

```bash
python -m pip install .
```

The version is a major version zero, so the Python API may change in any
release. The stable contract is `export_schema_version` in an export's
manifest — see
[`docs/export-schema-v1.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/docs/export-schema-v1.md)
— and depending on that rather than on the package version is the intended
way to build against this.

## Quick start

Everything is read-only. Nothing this command does writes to the files it is
pointed at.

```bash
# Copy a card into a private directory first, and work from the copy.
prisma-vent copy-card --source /Volumes/YOUR_CARD \
                      --destination ~/private/card-copy --dry-run

# Look at one day archive, or one trend curve.
prisma-vent inspect ~/private/card-copy/.../0123_2020-01-01.zip

# Therapy time per day or month, from the device's own year-long record.
prisma-vent usage ~/private/card-copy/.../*.zip --by month

# Cross-check a decode against itself and against published device figures.
prisma-vent validate --report ~/private/card-copy/.../*.zip

# Export in a documented, versioned machine-readable format.
prisma-vent decode ~/private/card-copy/.../*.zip --output ~/private/export

```

Exit codes are a contract: `0` success, `1` structural decode or validation
failure, `2` usage error, `3` I/O failure, `4` a safety condition violated.
Findings go to stdout and diagnostics to stderr, so the two can be redirected
apart.

**The destination of `copy-card` holds personal health data.** Keep it outside
any repository, and outside anything that synchronises to a cloud service.

## Status

**Early, and deliberately narrow.** Everything below is implemented and covered
by tests against fixtures generated at runtime.

| Area | State |
|---|---|
| Time base | signed offsets, noon-to-noon therapy day, the several reference midnights |
| `.wmedf` headers | parsed, declarations validated rather than assumed |
| Sample values | raw digital, including the mixed 8-bit/16-bit layout; physical conversion separate |
| Sample times | derived from record and sample indices, never accumulated |
| Day archives | sessions paired with event files, events, alarms, settings snapshots split by program |
| `.tc` trend curves | structure only — header, 9-byte records, a therapy-time estimate. Contents not decoded |
| `statistic.proto` | full: per-session start and duration for a year, lifetime total, named settings |
| `copy-card` | verified by size and SHA-256, manifest recorded |
| Export | versioned and documented — [`docs/export-schema-v1.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/docs/export-schema-v1.md) |

**Not implemented: any interpretation of what the data means.** Not an omission
to be filled in later — see [Known limits](#known-limits).

Three levels of assurance, kept apart on purpose:

| | |
|---|---|
| **Exercised against real data** | run locally and read-only against one card and firmware |
| **Automatically tested** | synthetic fixtures only, generated at runtime |
| **Supported** | one device model, one firmware. A successful run over one card is not a claim about another |

Real device data stays outside this repository and is never committed as a
fixture. **No figure measured from a recording is published here** — not in the
documentation, not in a source comment, not in a test, not as a threshold.
Where a claim rests on comparing real files, it says so without quoting what
the comparison produced.

## What the format holds

Documented with evidence in
[`docs/format.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/docs/format.md).

- `.wmedf` signal files are a **variant of EDF**: version field `1` rather than
  `0`, and each signal declares its own storage width — 8-bit or 16-bit — in its
  reserved field. Standard EDF readers assume a uniform 16-bit layout and will
  misalign every channel after the first 8-bit one, **silently, with values that
  still look plausible**. Do not point one at these files.
- 16-bit values are little-endian; 8-bit signedness follows the declared digital
  minimum; scaling is affine and read per channel from the file.
- The **therapy day runs noon to noon**, not midnight to midnight.
- **Three different reference midnights are in play, not one.** The day-level
  log counts from midnight of the archive's own date throughout, reaching the
  following noon by counting past 24 hours. A session's *start* counts from
  midnight of the day that session began. A session's *end* may count from
  midnight of the day it **ended**, which for a session running across midnight
  is a different day again.

  So a session's start and stop must not be converted against the same
  reference midnight simply because they sit in the same file. Doing that
  displaces the value by exactly 24 hours whenever a session crosses midnight —
  and a night shifted by a whole day still reads as a perfectly ordinary night,
  just the wrong one.

### Settings history: two sources, neither sufficient

`parameter.xml` records a change in one of two ways, and **a history built
from either alone is wrong in a way that looks complete**:

- A change made **while the device is recording** is written as its own entry
  naming just that parameter.
- A change made **while the device was switched off** leaves no entry at all.
  It appears only as a difference between the last full snapshot before it and
  the first one after.

So a reader that scans for change entries silently misses every adjustment
made with the device off — and produces a plausible, gap-free-looking history.
One that only diffs snapshots loses the exact time of the changes it does
find, and misses a value that was set and corrected between two snapshots.

There is a mechanical trap in the second source as well. A snapshot on the
firmware examined holds a fixed number of entries; anything smaller is a
change record, not a partial snapshot. Diffing a full snapshot against one of
those reports every absent parameter as a change to nothing — hundreds of
entries of pure artefact.

This library gives you the snapshots and the change entries as the device
wrote them, with their times. It does **not** assemble the history for you,
because deciding how to merge two sources of evidence is a decision the caller
should be making deliberately.

### Parameter scales

`parametersmap.xml` maps parameter ids to names, but nothing in the archive
states the scale of a value: a pressure appears as an integer with the decimal
point implied. **One** of them — inspiratory pressure — has a factor confirmed,
against two distinct display readings that give the same ratio.

Every parameter is reported **raw**, confirmed or not. The factor is usually
guessable from the plausible range, and guessing is exactly what this decoder
does not do: a pressure off by a factor of ten still looks like a pressure.

**What is known, and how well, is in the export rather than only in prose:**

> `scales` contains confirmed conversions. `scale_candidates` contains
> experimentally observed conversions that may still change. `value_domains`
> lists parameters that are named states or steps rather than quantities, with
> the labels seen for the values seen. Parameters absent from all three have no
> known conversion.

Published in the export rather than only here, because a parameter with a
known factor and one without would otherwise look identical in it — which makes
inventing a factor the path of least resistance. Saying nothing does not
prevent a guess; it invites one.

Expiratory pressure is a candidate rather than confirmed, and the reason
generalises: the card carries the same raw value in every programme block of
every archive, so however many readings corroborate it, they corroborate **one
point**. A factor needs two.

**Two things that look like evidence for a factor and are not.** A signal
channel and a therapy parameter may share a name — `Frequency` is both — and a
channel's declared scale says nothing about how a setting of that name is
encoded. A settable range from the manual describes the front panel, not the
encoding behind it; it can rule candidates out, which is worth having, but it
cannot single one in.
[`docs/format.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/docs/format.md)
and
[`docs/device-reference.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/docs/device-reference.md)
work both through.

### Cross-check on the settings decoding

`statistic.proto` embeds a plain JSON configuration in which **every parameter
is named**, where `parameter.xml` is keyed by numeric id. Two files, two
formats, the same settings — so they can be compared, and a disagreement would
point at the block detection, the id-to-name map or the program ordering. The
decoder exposes both, so anyone with a card can run it on their own data.

It would not confirm the *scales* in any case: both state the same integers,
and neither says where the decimal point goes. **What the comparison produced
here is not published** — those are somebody's therapy settings, and the method
is the part that transfers.

### Long-term record

Day archives and trend curves each cover a few weeks. `statistic.proto` carries
a **rolling year** — a start time and a duration for every session in it. The
manufacturer documents the device as holding at most fourteen days internally,
so that is the difference between a few weeks of history and a year of it.

The reader checks what the format guarantees rather than assuming it: one
record per session with its timestamp matching a session start to the second,
durations in whole minutes, and every total summing to both of its breakdowns.
Two quantities, the eleven-way category axis and the per-program histograms are
**left unnamed and handed back raw**, because nothing establishes what they
are. [`docs/format.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/docs/format.md)
describes the layout and says what each of those is and is not.

Read it with `prisma-vent inspect`, or `DayArchive.statistic()` from the API.

`prisma-vent usage` reports it directly:

```bash
prisma-vent usage /path/to/*.zip --by month
```

Output, with **invented figures** — like every example in this repository, the
numbers below are made up and describe nobody:

```
2020-01   6.00 h/day  31d  ####################
2020-02   7.50 h/day  29d  #########################
…
2020-04   7.00 h/day  30d  #######################  partial

mean 6.90 h/day, median 7.00 h/day, over 120 day(s)
2 cut-off day(s) excluded — the record is a rolling window, so its first
and last days are incomplete. The periods holding them are still shown in
full, marked partial. --include-partial counts them too.
```

Four things that command is careful about, because each would otherwise
produce a confident wrong number. **The day boundary**: a therapy day runs noon
to noon, so summing by calendar date would split most nights in two and halve
them both; `--by calendar-day` exists for comparing against a source that bins
differently, and says so in its output. **Days with no stored therapy**: a covered day
carrying no record is reported as a zero, because leaving it out turns therapy
per covered day into therapy per day the device happened to run — a different
and always larger figure. A day is covered when it lies between the first and
last day carrying a session; outside that range nothing is invented. **A zero
means no whole therapy minutes and no session were stored, not that the device
stayed off** — the long-term record holds whole minutes, so a session under a
minute leaves no entry at all.
The `sessions` column counts stored records for the same reason.
**Partial edges**: the window rolls, so its first and last days are cut off by
definition. Those two **days** are left out of the mean and median unless
`--include-partial` asks for them — not the periods holding them, which are
still shown in full and marked partial. Dropping a whole month because one of
its days is a boundary day would discard up to thirty complete measurements to
avoid one incomplete one. Both summary figures are computed from the individual
days, never from the buckets, so with `--by month` a month covering one day
does not weigh as much as one covering thirty-one, and the median is a median
of days rather than of monthly averages.
**Duplicates**: every archive carries the same year, so rows are merged on their
raw timestamp; two copies of one session that *disagree* are a structural error
with exit code `1`, not something to settle by which archive was named first.

**Pointing it at a whole card recovers more than any single archive.** The
copies are rolling windows offset by a day each, so their union spans a year
plus the archives' own range, which is more than any single file holds.

`decode` writes the same rows to `usage.jsonl`, unaggregated, for a consumer
that wants to bin them its own way.

## Known limits

| Area | Status |
|---|---|
| Tested device | prisma VENT50 only |
| Signals | experimental |
| Events and alarms | time and numeric id only — the **names are known, the mapping is not** |
| Parameters | three program slots, two normally enabled; values cross-checkable against a second file, **scales mostly not** |
| Time zone | device clock runs local time; DST transitions are **not** handled |
| Sensor channels | an absent sensor records **zero**, not a distinct marker |
| Units | BTPS and STPD **coexist in one file**; no conversion is done |
| History depth | archives and trend curves cover weeks; `statistic.proto` a **rolling year** |
| Medical interpretation | **not supported** |

Three of these carry consequences worth spelling out.

**No event id has been identified.** Events can be counted and placed in time
but not named, so this tool derives **no apnoea index, no leak index, and no
summary that would require knowing what an event is**. Guessing that an id
means "apnoea" because the count looks plausible is the failure this project is
built to avoid.

The alarm names are known and the mapping is not: the manual lists the display
strings and `statistic.proto` carries eighteen alarm keys with thresholds, but
those keys address alarm *settings* by name while `alarm.xml` addresses
*occurrences* by number, and nothing joins the two id spaces. Two things would
make a wrong join hard to catch — alarms lag their own cause by up to twenty
breaths, and one display string can cover several distinct faults. Alarms are
therefore exported by number; the keys and the reasoning are in
[`docs/format.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/docs/format.md).

**An all-zero sensor channel is ambiguous, and stays that way.** With no
oximeter attached, the oximetry channels carry a plain zero rather than a
distinct marker, so nothing in the data separates "not measured" from "measured
as nought". The zeros are reported as read. Turning them into missing values
would be a conclusion dressed as a conversion; a consumer may reasonably *flag*
such a series as probably unavailable, but calling it a missing sensor needs
context from outside the file. **No clinical figure may be derived from a
series whose meaning is unresolved** — the plainest single reason this decoder
computes none.

**Daylight saving is not handled.** The clock runs local time, and the therapy
day runs noon to noon, so a transition falls mid-night: one therapy day an hour
short, another an hour long, one local hour occurring twice. None of it is
implemented and none of it is tested.

## Device constraints

Properties of the ventilator, not of this program. No amount of careful
decoding removes them, and **a consumer that ignores them will produce wrong
numbers from a correct decode.** Each is cited with edition, section and page
in
[`docs/device-reference.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/docs/device-reference.md);
what this decoder does about each is here.

- **Two unit systems coexist in one file** — some flows and volumes in BTPS,
  the rest in STPD. Comparing across them gives a consistent few-percent error
  that reads as noise. Units are reported as the file declares them and
  **nothing is converted between the systems**.
- **The traces may already be filtered.** Displayed pressure, flow and leakage
  are low-pass filtered; whether the card holds the displayed values or raw
  sensor output is not stated anywhere. Treat them as possibly smoothed.
- **The clock has no time source** and drifts. Timestamps are reported as
  device-local and **never converted**; a correction belongs alongside them,
  not applied to them.
- **Only fourteen days are held internally**, so a fresh card arrives already
  carrying history and older therapy days are not on the device at all.
  Missing early days are usually not a decoding problem.
- **The circuit type changes what the numbers mean.** It is recorded in the
  data, and volumes should not be interpreted without it.
- **Sessions may end with a low-pressure tail** after the soft-stop ramp.
  Those minutes are not a therapy setting.

## Design

- **Fail loudly.** A malformed or unexpected file raises. It does not produce a
  number.
- **Raw and physical values are both kept**, so a wrong result can be traced
  to byte decoding or to scaling rather than being ambiguous. The two are
  separate operations, and an unscaled chunk is distinguishable from a scaled
  one rather than merely undocumented.
- **Nothing is hardcoded** that the file declares — channel count, order, widths,
  scaling and parameter structure are all read per file, because they are what
  one firmware version writes rather than facts about the device.
- **Raw inputs are never written to.**
- **No threshold comes from a recording.** Every number that can fail, refuse
  or reject something is listed in
  [`docs/thresholds.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/docs/thresholds.md)
  with its public source, its derivation, the range it applies over, what
  happens at the boundary, and the synthetic test that pins it. Where no public
  derivation exists, the check **reports** instead of asserting, or it does not
  exist.
- No runtime dependencies; the standard library is sufficient.

### The five checks, and what each claims

Five checks, and the difference between them is the point of the exercise.
Each carries its own status, because they are independent and one can be
impossible while another succeeds.

| Check | Status it can reach | Why |
|---|---|---|
| plausibility | `passed`, `failed`, `not comparable` | compares against figures the manufacturer publishes, which the file cannot influence |
| long-term record | `passed`, `failed`, `not comparable` | compares two of the device's own outputs about the same session |
| breath rate | `reported`, `not comparable` | no published figure bounds within-session rate variation |
| volume | `reported`, `not comparable` | the expected relationship does not hold, and the explanations cannot be separated |
| target volume | `reported`, `not comparable` | what separates a delivered volume from its target is what the device is regulating, so bounding it would be a clinical claim |

**`reported` is not `passed`.** It means the check ran and produced a figure
this decoder asserts nothing about. Reading it as success would credit the
decode with surviving a test that was never applied to it.

**A check that could not run is not a check that passed** either. A session
with nothing comparable is counted and named rather than folded into a clean
summary line.

### Session duration against the device's own record

`validate` compares each session's duration against the entry the device
banked for it in its long-term record. Sessions lasting under a minute leave no
entry at all, since the device stores whole minutes, and those are reported as
`not comparable` — which is not `passed`.

A tolerance of one minute is allowed on matching a record to its session. It
is derived from the format rather than from measurement: the long-term record
stores whole minutes, so its idea of when a session began can differ from the
archive's by up to one storage quantum. Where more than one record falls inside
the window — including several sharing one instant — the session is reported as
not comparable. Ambiguity is refused, never resolved, and in particular never
resolved by whichever record happened to be written last.

**Be clear about what this proves, and what it does not.** A session has two
durations — the span between the start and stop times in its XML, and the span
its signal file's records account for. They differ, and the device banks the
*XML* span, truncated.

So the banked figure and the session XML come from the same side of the device,
and their agreement does **not** prove the signal decoding. What it proves is
that session boundaries are read correctly — a shifted or misattributed time
base puts the two out of step. **That is weaker than two unrelated subsystems
agreeing**, and it is worth knowing which of the two you have.

### Plausibility: the one external check

Most of what `validate` reports is self-referential. Comparing a sample against
the range the same header declares catches a misaligned record, but not a
misread header: if the scale factors or the channel order were wrong, the file
would agree with itself perfectly and every value would look reasonable. That
is precisely the silent misparse this project is most afraid of.

So one check compares decoded values against figures the manufacturer
publishes, which the file cannot influence. It reports `passed`, `failed`, or
`not comparable` when no channel in the file has a published bound — and `not
comparable` is not `passed`.

**Most channels have no bound**, and that is the honest position rather than a
gap. The manual gives maximum air flow as *above* 220 l/min — a guaranteed
minimum capability, which cannot be turned into a ceiling, and multiplying it
by a time gives another lower bound rather than an upper one. A ceiling for
breath rate or pulse rate would need a cited source and there is none. Bounds
that rested on either are absent rather than reworded, and
[`docs/thresholds.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/docs/thresholds.md)
lists each of them with its reason.

What is applied is short and one-sided: a published maximum pressure under fault,
the published settable maxima for the three channels that report settings, and
0–100 % on channels the file itself declares as percentages. **The bounds are
deliberately loose.** They are not there to judge a therapy. They are there to
catch a decode that is wrong by a factor of hundreds — a swapped byte order
turns an ordinary breath into something two orders of magnitude larger — and a
bound tightened until it hugs observed data would start reporting the patient
instead of the parser.

Two safeguards are built in against it. Each bound records whether it came
from a settable range, a hardware capability or the arithmetic of its own
unit, and **only channels that report settings may be bounded by a settable
range** — a limit on what may be dialled in says nothing about what may be
measured. And when a bound fires while being narrower than the range the
file's own header declares, the report says so: that combination points at the
bound before it points at the decoder.

## Development

Python 3.11, 3.12 and 3.13 are supported, and CI runs all three on Ubuntu and
macOS — six combinations, all of which must pass. The development target is
3.13.

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

`ruff` runs on defect rules only — pycodestyle errors, pyflakes and bugbear.
Which rules are selected, and why several obvious ones are not, is recorded in
`pyproject.toml` next to the selection itself rather than repeated here.

On macOS the system Python is a separate thing and must not be replaced or
upgraded — install alongside it. No Homebrew prefix is written down here: it
differs between Apple Silicon (`/opt/homebrew`) and Intel (`/usr/local`), and
`brew install python@3.13` puts `python3.13` on `PATH` in both cases.

Install the privacy guard as a hook once per clone:

```bash
ln -s ../../scripts/pre-commit .git/hooks/pre-commit
```

It inspects **content**, not filenames, in three modes — the staged changes,
`--all` for every tracked file, and `--history` for every blob in the object
store. All three run in CI. It is an additional barrier, **not** a guarantee.

Tests use **synthetic fixtures generated at runtime**. No file cut from a real
recording exists in this repository.

### Cutting a release

Each release freezes a contract describing the export it publishes — every
field path, its permitted types, whether it is always present. Every test run
checks the current export against **all** of them, so a field published once
cannot quietly disappear later. Contracts are never edited afterwards.

That means a release needs its contract before the tag exists, not after:

```bash
# 1. set the new version in pyproject.toml, then
PYTHONPATH=src:tests python3.13 tests/schema_contract.py \
    tests/contracts/export-schema-v1-v0.1.0.json
# 2. check the tree is releasable before touching the remote
python3.13 scripts/check-release-tag.py v0.1.0
```

The second command answers the same three questions CI does — tag against
version, contract present, contract exact — so a mismatch is caught here
rather than after a tag has been pushed. Pushing the tag then runs the usual
workflow and, if everything passes, attaches the artefacts it checked to a
**draft** release whose notes carry their SHA-256. Reviewing those notes and
publishing stay manual.

### `decode`: replacing an existing export

`decode` writes each export into a temporary directory and moves it into place
only once the manifest is written, so a run that fails leaves nothing behind
and never mixes two exports. **An existing export is never overwritten
silently**, and `--overwrite` replaces exactly one thing: a complete export of
*this same archive*, proved by a matching `archive_sha256`.

| destination | default | `--overwrite` |
|---|---|---|
| complete export of this archive, matching sha256 | refused | **replaced** |
| complete export of a different archive | refused | refused |
| incomplete, damaged, or not an export at all | refused | refused |

"Complete" is checked, not assumed: the manifest must be a regular file of
valid JSON declaring a schema version this build writes, naming this archive,
carrying a syntactically valid digest that matches the source bytes, and every
required file must be present as a regular file — a symlink does not count. **A
manifest carrying a matching hash is not on its own a licence to delete a
directory.**

Nor is the presence of the right files. The manifest lists every path the
export wrote, and the check is an **exact correspondence**: every declared path
must exist as a regular file, every regular file present must be declared, and
every directory must be one the declared paths imply. A subset check would not
do — a manifest could declare a path it never wrote, and a file later dropped
there would then pass as the exporter's own.

Declared paths must be safe and canonical, and `manifest.json` must list
itself; the exact rules are in
[`docs/export-schema-v1.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/docs/export-schema-v1.md).

So **anything found beside an export stops the replacement** — a note, a
spreadsheet, an unfamiliar subdirectory, even an empty one. Replacing it would
delete those, so the directory is left exactly as it is and the message names
the paths, never their contents.

A manifest **lacking** the list falls back to a strict allowlist of the
required files, `usage.jsonl` and `session_NNNN/signal_NN_*.csv`. That fallback
cannot tell a hand-made file matching those names from one this tool wrote,
which is why the declared list is the better route. **Only a missing `files`
field takes the fallback**; a malformed one is refused outright.

The permissive reading was rejected deliberately. Archive filenames repeat
across cards — the day counter runs with the device, not the card — so
`0123_2020-01-01.zip` from two cards is two different recordings under one
name. A flag meaning "redo this export" would otherwise destroy an export of
something else. Moving the old directory aside is one command and cannot go
wrong by accident.

If replacing an export fails **and** the previous one cannot be put back, the
run stops with the surviving copy's path in the message and deletes nothing.
If the swap succeeds but the old export cannot then be removed, that is
reported too, with exit code `3`: the new export is complete and in place, and
a second copy of health data is still on disk at the named path. Nothing tries
to delete it again.

`--signals` **fails on a channel it cannot resolve** rather than skipping it,
with exit code `2`: skipping would produce a successful export missing exactly
the channel that was asked for. Naming a channel twice, or once by label and
once by index, writes one file.

### `copy-card` guarantees

`copy-card` never picks a volume for you — the source is always named
explicitly. It does not descend into the operating system's own directories —
`.Trashes`, `.Spotlight-V100`, `.fseventsd` — which are listed as ignored in
the manifest; copying a card must not carry off files somebody deleted. It
copies every other regular file, familiar or not, refuses symlinks and other
non-regular objects, verifies each file by size and SHA-256 before counting it
as copied, and never overwrites: a file already present with different content
stops the run.

**Nothing is written outside the destination you name.** That is not a matter
of computing careful paths and trusting them. Once the destination is resolved
it is opened as a directory *handle*, and every subsequent directory and file
is created and opened relative to that handle with no-follow semantics. A
symlink anywhere below the destination is refused rather than traversed, and a
path swapped between planning and writing cannot redirect anything, because no
later operation goes through a path string at all.

A destination is treated as resumable **only** if it carries a manifest this
tool wrote: a regular, non-symlink file of valid JSON, declaring the expected
schema version, naming the same source, and stating whether its copy finished.
Every one of those is checked. Accepting a directory because it contains
something *named* `copy-manifest.json` would, combined with path-based writes,
be enough to place health data outside the chosen destination and to change
permissions on a directory elsewhere.

The destination is created owner-only and **holds personal health data** — keep
it outside any repository.

The source is opened read-only and is never written to. That is not the same
as the volume being mounted read-only, which no program can guarantee, and
this one does not claim it.

## Reporting problems

**The only thing to send is a synthetic reproduction.** The fixture builder
in `tests/synthetic.py` writes byte-exact `.wmedf`, ZIP, XML and protobuf files
from invented data, so a failing case built with it carries no health data at
all. If you can express the problem that way, that is the whole report.

Otherwise, send only:

- the package version, your Python version and operating system;
- the device model and, if you have it, the firmware version;
- the error message, with any file paths redacted;
- a description of what you expected and what you got, in words rather than
  figures — "the airway pressure channel decodes about ten times too small
  against the device display" says as much as a pair of numbers and publishes
  nothing.

Please do **not** attach `.zip`, `.wmedf`, `.tc`, `.proto` or device XML files,
serial numbers, full local paths, or screenshots containing personal data.

**No output of this program is offered as safe to paste.** `prisma-vent
inspect` in particular names the archive, reports session counts and durations,
and can print statistics measured from your recording. Read what you are about
to send; there is no command that will do it for you.

If a number is wrong but looks plausible, report it privately —
[`SECURITY.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/SECURITY.md)
explains why that is the most serious defect this project can have.

## Licence

MIT — see
[LICENSE](https://github.com/carstenb/prisma-vent-decoder/blob/main/LICENSE).
