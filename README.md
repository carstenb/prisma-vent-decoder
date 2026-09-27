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

**Early, and deliberately narrow.** What it will not do is tell you what any
of it means clinically, and that is a deliberate limit rather than a gap.

What is implemented, and covered by tests against fixtures generated at
runtime:

- the device time base — signed offsets, the noon-to-noon therapy day, and the
  several reference midnights the day-level and session-level files count
  from;
- `.wmedf` header parsing, with the format's own declarations validated rather
  than assumed;
- decoding of **raw digital sample values**, including the mixed 8-bit and
  16-bit channel layout;
- conversion to physical units, and a sample time axis derived exactly from
  record and sample indices rather than accumulated;
- reading a day archive — sessions paired with their event files, respiratory
  and device events, alarms, and settings snapshots split into their therapy
  programs — with member names and sizes bounded, and provenance recorded;
- reading `.tc` trend curves as far as their structure is known: a header, a
  sequence of 9-byte records, and an **estimate** of the day's therapy time
  from how many of them are populated. The record contents are not decoded and
  the reader claims nothing about them;
- reading `statistic.proto`, the device's own long-term record — a start time
  and a duration for every session of the past year, its lifetime therapy
  total, and a settings snapshot with parameters **named**. Includes a generic
  protobuf wire reader that needs no schema;
- copying a mounted card into a private directory, verifying every file by
  size and SHA-256 and recording a manifest;
- exporting an archive in a versioned, documented machine-readable format —
  see [`docs/export-schema-v1.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/docs/export-schema-v1.md).

What is not implemented:

- any interpretation of what the data *means*. That is not an omission to be
  filled in later: see the limits below.

Three levels of assurance, kept apart on purpose:

- **Exercised against real data.** The decoder has been run locally and
  read-only against one prisma VENT50 card and firmware combination. Real
  device data stays outside this repository and is never committed as a test
  fixture, and **no figure measured from a recording is published here** — not
  in the documentation, not in a source comment, not in a test, and not as a
  threshold. Where a claim below rests on comparing real files, it says so
  without quoting what the comparison produced.
- **Automatically tested.** The suite uses synthetic fixtures only, generated
  at runtime.
- **Supported.** One device model on one firmware. A successful run over one
  card is not a claim about other firmware versions, and several format
  details are known to be firmware-specific.

## What has been established about the format

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

### Reconstructing a settings history takes two sources, not one

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

### One parameter has a confirmed scale, and the export says so

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

**A measured channel and a therapy parameter can share a name, and their
scales have nothing to do with each other.** `Frequency` is both: a signal
channel, whose scale the `.wmedf` header states outright, and a setting in
`parameter.xml`, whose scale nothing states. A channel scale the file declares
about itself says nothing about how a setting of that name is encoded.

**A settable range is not proof of a factor either.** The manual gives ranges
and step sizes for what a clinician may dial in — see
[`docs/device-reference.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/docs/device-reference.md).
Those can rule some candidates out, which is worth having, but they describe
the device's front panel and not the encoding in the file behind it.

### A second file lets the settings decoding be checked independently

`statistic.proto` is a Protocol Buffers payload, and reading it needs no
schema: it embeds a plain JSON configuration in which **every parameter is
named**, with therapy parameters given as a three-element array, one per
program.

That makes it a genuine cross-check on what this decoder reads out of
`parameter.xml` and `parametersmap.xml`: two files in different formats, one
keyed by numeric id and one by name, describing the same settings. A
disagreement would point at the block detection, the id-to-name map or the
program ordering. The decoder exposes both, so anyone with a card can run the
comparison on their own data.

**What that comparison produced here is not published**, for the same reason
nothing else measured from a recording is: those are somebody's therapy
settings. The method is the part that transfers.

It would not confirm the *scales* in any case. Both files state the same
integers, and neither states where the decimal point goes.

### The same file holds a year of history, and it is now readable

Day archives and trend curves each cover a few weeks. This file carries a
**rolling year** — a start time and a duration for every session in it. The
manufacturer documents the device as holding at most fourteen days internally
([`docs/device-reference.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/docs/device-reference.md)),
so that is the difference between a few weeks of history and a year of it.

Read it with `prisma-vent inspect`, or `DayArchive.statistic()` from the API.

What the reader enforces rather than assumes:

- **One record per session**, its timestamp matching an archive session's start
  exactly, to the second.
- **Session duration in whole minutes, truncated.** Because the device
  truncates, a session's own span and the figure banked for it differ by
  somewhere in `[0, 1)` minutes — which is arithmetic about the storage, not a
  measurement of anything.
- **Every total is broken down by program slot and by an eleven-way category,
  and both breakdowns must sum to it.** A partition sums to what it partitions;
  the reader raises where it does not.
- **Lifetime therapy time.** Every archive carries its own copy of this file,
  so a card gives one reading per day, and consecutive readings can be checked
  against that day's session durations. The reader exposes both figures.

What is deliberately left alone:

- **The eleven categories are not named.** No ordering has been established
  and no source gives one. A single correspondence would not establish an
  ordering for eleven positions, and a count that happens to match eleven is
  not evidence of one either.
- **Two quantities stay unnamed** — a per-record figure that never exceeds the
  duration, and a second lifetime counter. Nothing else in the data measures
  either independently, so neither is given a meaning.
- **The per-program histogram blocks are handed back raw.** 553 numbers per
  program per session, plainly distributions of something unidentified.

The timestamps looked like they would settle whether the device clock is UTC or
local, being absolute. They do not: they run exactly twelve hours behind Unix
time and track the device's own clock, so they are a local-time counter in
epoch clothing and carry no zone information at all.

Also in the file: probably the firmware version, and the circuit type
(`HoseType`, `ExhalationSystem`) that the manual says must be known before
volumes mean anything.

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

Three of these deserve emphasis.

**No event id has been identified.** Events can be counted and placed in time
but not named — so this tool derives no apnoea index, no leak index, and no
summary statistic that would require knowing what an event *is*. Guessing that
an id means "apnoea" because the count looks plausible is precisely the failure
this project is built to avoid.

This is worth stating carefully, because it is now easy to believe the opposite.
**The alarm names are known. The mapping is not.** Two sources give a catalogue:
the manufacturer's manual lists the display strings, and `statistic.proto`
carries eighteen alarm keys as configuration —

> `AlarmApnoe`, `AlarmArpLimit`, `AlarmFrequencyHigh`, `AlarmFrequencyLow`,
> `AlarmLeakageHigh`, `AlarmMinuteVolumeHigh`, `AlarmMinuteVolumeLow`,
> `AlarmPressureHigh`, `AlarmPressureLow`, `AlarmPulseHigh`, `AlarmPulseLow`,
> `AlarmSpO2High`, `AlarmSpO2Low`, `AlarmSystemInactivation`,
> `AlarmVolumeHigh`, `AlarmVolumeLow`, `AlarmVolumeLowMPVv`, and
> `AlarmVentilationSwitchedOff`

— each with a configured threshold per program. So the *vocabulary* is settled
and the *thresholds* are readable.

What no source gives is the join. Those keys are alarm **settings**, addressed
by name. The ids in `alarm.xml` are alarm **occurrences**, addressed by number,
and nothing found so far connects the two id spaces. Having eighteen names and
a list of numeric ids invites lining them up in order; that is a guess, and a
wrong one labels an apnoea as a leak while looking entirely reasonable.

Two further reasons an id-to-name guess would be hard to catch even if
tested against the recordings: **alarms lag their own cause** — physiological
alarms fire three breaths after the limit is reached, ten for rebreathing, at
most twenty for the ARP limit, and three seconds for the pulse and SpO₂ alarms
(cited with its page in
[`docs/device-reference.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/docs/device-reference.md))
— so an alarm appears *after* whatever triggered it and correlation against the
traces must allow for that. And a single display string can cover several
distinct faults, so even a correct name would not identify the cause.

Until the join is established, alarms are exported by number.

**An all-zero sensor channel is ambiguous, and stays that way.** When no
oximeter is attached, the oximetry channels — oxygen saturation, pulse rate and
signal quality — carry a plain zero rather than a distinct marker, so nothing
in the data separates "not measured" from "measured as nought".

The decoder therefore reports the zeros as it read them. It does not silently
turn them into missing values: that would be a conclusion dressed as a
conversion, and the raw figures are what make a later disagreement traceable.
A consumer may reasonably *flag* an all-zero series as probably unavailable,
but calling it a missing sensor is a claim the data cannot support on its own —
it needs context from outside the file, such as what was connected at the
time.

Either way, no clinical figure may be derived from a series whose meaning is
unresolved. This is the plainest single reason why the decoder computes none.

**Daylight saving is not handled.** The device clock runs local time, which was
confirmed against the device display. Because the therapy day runs noon to
noon, a transition falls in the middle of a night rather than at a tidy
boundary, producing one therapy day an hour short and another an hour long,
and one local hour that occurs twice. None of this is implemented and none of
it is tested.

## Constraints that come from the device, not from this code

These are properties of the ventilator, taken from the manufacturer's patient
instructions for use and cited with edition, section and page in
[`docs/device-reference.md`](https://github.com/carstenb/prisma-vent-decoder/blob/main/docs/device-reference.md).
No amount of careful decoding removes them, and a consumer that ignores them
will produce wrong numbers from a correct decode.

**Two unit systems coexist in one file.** Patient flow, target volume, breath
volume and minute volume are stated in **BTPS**; every other flow and volume is
**STPD**. Comparing one against the other — a leak flow against a tidal volume,
say — produces a consistent few-percent error that looks like noise rather than
like a mistake. This decoder reports units as the file declares them and does
not convert between the systems.

**The traces may already be filtered.** The manual states that *displayed*
pressure, flow and leakage are low-pass filtered. Whether the values written to
the card are the displayed ones or raw sensor output is not stated anywhere, so
calling them raw sensor data is an assumption. Treat them as possibly smoothed.

**The clock has no time source.** The device carries a "clock not set" alarm
whose stated remedy is having the clock set by a dealer so that the course of
therapy is recorded correctly. Nothing synchronises it, so it drifts.
Timestamps are therefore reported as device-local and never converted; a
correction belongs alongside them, not applied to them.

**Only fourteen days are held internally.** When a fresh card is inserted the
device writes out its buffer, which is why a new card arrives already carrying
history. But therapy days older than that window are not on the device at all —
they exist only on the previous card or in the manufacturer's cloud. Missing
early days are usually not a decoding problem.

**The circuit type changes what the numbers mean.** Leakage circuit and a
single circuit with a valve differ in their pressure floor and in how
leak-derived values behave. The type is recorded in the data, and volumes
should not be interpreted without it.

**Sessions may end with a low-pressure tail.** After the soft-stop ramp expires
the device keeps running at a low pressure until it is put into standby. Those
minutes are not a therapy setting and should not be read as one.

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

### What `validate` asserts, and what it only reports

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

### A check that sets the device against itself

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

### One check does not trust the file

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

### What `decode` guarantees about an existing export

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

Declared paths must be safe and canonical: relative, POSIX, non-empty, no
`.` or `..` segment, no backslash, no trailing slash, no duplicates, and
`manifest.json` must list itself. The exporter writes the list sorted, but
order is not part of the contract — validation compares sets.

So **anything found beside an export stops the replacement** — a note, a
spreadsheet, an unfamiliar subdirectory, even an empty one. Replacing it would
delete those, so the directory is left exactly as it is and the message names
the paths, never their contents.

An export written before that list existed falls back to a strict allowlist of
the required files, `usage.jsonl` and `session_NNNN/signal_NN_*.csv`. That
fallback cannot tell a hand-made file matching those names from one this tool
wrote, which is why the declared list is the better route. **Only a missing
`files` field takes the fallback**; a malformed one is refused outright.

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

### What `copy-card` guarantees

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
