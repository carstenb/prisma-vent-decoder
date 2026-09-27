# prisma VENT50 export format, version 1

What `prisma-vent decode` writes, and what a program reading it may rely on.

Every example below is **synthetic**: made-up ids, round numbers, a date with
no meaning. Nothing here comes from a recording.

---

## Layout

One directory per archive, named after the archive:

```
0000_2020-01-01/
├── manifest.json            what this export is
├── sessions.jsonl           one row per session
├── events.jsonl             one row per event, of three kinds
├── parameters.json          settings snapshots and change records
├── usage.jsonl              the device's long-term record, one row per session
├── validation-report.json   cross-channel checks, machine-readable
└── session_0001/            only when --signals was given
    └── signal_00_airway-pressure.csv
```

Encoding is UTF-8 throughout. `.jsonl` files hold **exactly one JSON object
per line**, newline-terminated, no wrapping array. CSV files use `,`, quote
minimally, and carry a header row.

---

## Rules that hold everywhere

**Missing is `null`, never `0`.** The device itself writes a plain zero for a
sensor that is not attached, so zero already means two things in the source
data. Emitting zero for something this exporter does not have would add a
third, and no reader could separate them afterwards.

**No `NaN`, no `Infinity`.** Neither is valid JSON. A non-finite value is an
error during export, not something written out.

**Event and alarm ids are numbers with no name.** No id has been identified —
not from the card, not from the device logs, not from the manufacturer's
plugin. There is deliberately no `name` field to fill in. **Do not map these
ids to clinical names**, and do not compute an apnoea index, a leak index, or
any other figure that requires knowing what an event *is*.

**Raw and converted values appear together, separately.** A wrong number can
then be attributed to the byte decoding or to the unit conversion instead of
being ambiguous.

**Times carry no timezone, and must not be given one.** They are the device's
own clock. Its relationship to UTC is not established — see the project notes
— so a field named `*_device_local` is exactly that and converting it asserts
something unknown. Sample times are computed from record and sample indices,
not by accumulating an interval.

---

## `manifest.json`

```json
{
  "export_schema_version": 1,
  "archive_name": "0000_2020-01-01.zip",
  "archive_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "therapy_day": "2020-01-01",
  "day_number": 0,
  "session_count": 2,
  "event_count": 17,
  "signal_files": 0,
  "usage_records": 120,
  "files": ["events.jsonl", "manifest.json", "parameters.json",
            "sessions.jsonl", "usage.jsonl", "validation-report.json"],
  "unrecognised_members": [],
  "adapter": {
    "package_version": "0.1.0",
    "decoder_schema_version": 1,
    "git_commit": null,
    "dirty": null
  },
  "time_base": "…",
  "events": "…",
  "usage": "…"
}
```

`export_schema_version` is the field to check. `git_commit` and `dirty` are
`null` in an installed distribution, which has no repository to read them
from; a consumer must handle their absence rather than assume a checkout.

`files` lists every path this export wrote, relative to the export directory
and sorted, including `manifest.json` itself. It exists so a later run can tell
this export's own files from anything put beside them afterwards — a note, a
spreadsheet, a subdirectory — and refuse to replace a directory holding such
things rather than delete them. **A reader may ignore it**, and must tolerate a
manifest that lacks it: the field is optional in version 1.

Every entry is a safe canonical relative POSIX path — non-empty, no leading
slash, no `.` or `..` segment, no backslash, no trailing slash — and no path
appears twice. The list is written sorted, but order carries no meaning: it is
compared as a set.

**`therapy_day` is a noon-to-noon day**, not a calendar date: it runs from noon
of the named date to noon of the next. A session beginning after midnight
belongs to the previous therapy day, and that is not an error.

---

## `sessions.jsonl`

One object per session, in ascending session number.

| Field | Type | Notes |
|---|---|---|
| `session` | integer | the number in the archive, from 1 |
| `therapy_day` | date | noon-to-noon, as above |
| `start_device_local` | timestamp | no zone, see the rules |
| `stop_device_local` | timestamp | may fall on the next calendar day |
| `duration_seconds` | number | `stop − start` |
| `accounted_seconds` | number | what the signal file's records account for |
| `duration_discrepancy_seconds` | number | the difference; **reported, not bounded** |
| `start_skew_seconds` | number | the event file's start minus the signal header's; **reported, not bounded**, and may be negative |
| `record_count` | integer | |
| `record_duration_seconds` | number | one second on the firmware seen so far, but read it rather than assuming |
| `channels` | array | see below |
| `provenance` | object | see below |

`duration_discrepancy_seconds` is not explained by anything in the format, and
no published figure bounds it. It is exported so a consumer can see it, not
because a threshold is known — and none is applied.

`start_skew_seconds` is the same kind of figure. Up to one second of it is the
signal header's truncation to whole seconds; the rest is skew between the two
subsystems that stamp these files, which nothing published bounds. **Nothing
is rejected on it**: a bound here would have no derivation, and
[`thresholds.md`](thresholds.md) says why one is not invented. What does reject
a start resolved against the wrong reference midnight is the therapy-day
check, which needs no number.

Each channel:

```json
{
  "index": 0, "label": "Example Channel", "unit": "hPa",
  "samples_per_record": 10, "sampling_rate_hz": 10.0, "bits": 16,
  "digital_min": 0, "digital_max": 650,
  "physical_min": 0.0, "physical_max": 65.0
}
```

`index` is the channel's identity. `label` may repeat or differ only by
padding, so **do not key on it**. `unit` is `null` when the file declares
none.

---

## `events.jsonl`

Three kinds in one file, told apart by `kind`. They are in one file because
their difference is in *what they are timed against*, and separate files would
hide that behind a filename.

| `kind` | timed against | fields |
|---|---|---|
| `respiratory` | its session's start | `session`, `id`, `phase`, `strength`, `seconds_from_session_start`, `device_local` |
| `device_state` | the archive's own midnight | `session` is `null`, `id`, `status`, `offset_from_archive_midnight` |
| `alarm` | the archive's own midnight | `session` is `null`, `id`, `phase`, `offset_from_archive_midnight` |

`phase` is `begin` or `end` for respiratory events; alarms add `reset` and
`ack`. On alarms, `begin` and `end` are frequently written at the **same
instant** — their difference is not a duration.

`status` on a device-state event is `true`, `false`, or `null`. **`null` is not
`false`**: it means the id marks a moment rather than a state change.

`offset_from_archive_midnight` is a signed `±HHHH:MM:SS.mmm` string, not a
number. Hours routinely exceed 24 and may be negative. It is left as written
because resolving it needs a reference midnight, and the day-level files, a
session's start and a session's end can each use a different one.

Ordering: respiratory events come first, sorted by time within each session;
device states and alarms follow, each sorted by time. Do not rely on a global
chronological order across kinds.

---

## `parameters.json`

```json
{
  "available": true,
  "map_version": "0.0.0",
  "config_version": "0.0.0",
  "snapshots": [
    {
      "offset_from_archive_midnight": "+0012:00:00.000",
      "device_parameters": [
        {"id": 1, "name": "ExampleSetting", "position": 0,
         "program": null, "value_raw": "0"}
      ],
      "therapy_programs": [
        {"program": 0, "parameters": [
          {"id": 10, "name": "ExampleTherapySetting", "position": 1,
           "program": 0, "value_raw": "100"}
        ]}
      ],
      "active_program_raw": {"id": 1, "name": "ActiveProgram", "position": 0,
                             "program": null, "value_raw": "0"},
      "notes": []
    }
  ]
}
```

`available` is `false` with a `reason` when the archive has no parameter file.
`scales`, `scale_candidates` and `value_domains` are present in both shapes,
empty where there is nothing to say, so a reader never has to tell an absent
field from an empty one.

### `scales` and `scale_candidates`

Two objects keyed by the parameter's canonical name, saying **what is known
about converting a raw value, and how well**:

```json
"scales": {
  "IPAP": {
    "parameter_id": 69,
    "name": "IPAP",
    "unit": "hPa",
    "raw_delta": 100,
    "physical_delta": 1,
    "status": "confirmed",
    "evidence": ["device_display_reference", "multi_point_reference"]
  }
},
"scale_candidates": {
  "EPAP": {
    "parameter_id": 37,
    "name": "EPAP",
    "unit": "hPa",
    "raw_delta": 100,
    "physical_delta": 1,
    "status": "single_point_corroborated",
    "evidence": ["device_display_reference"]
  }
}
```

```text
physical_value = value_raw × physical_delta / raw_delta
```

A ratio of two integers rather than one number, so a factor that later turns
out to be non-integral is not a type change. **There is no offset term**: these
describe multiplication only, and an additive component would be a different
statement needing a field of its own.

What the two registries hold today, in full:

| Parameter | `raw_delta` | `physical_delta` | Unit | Registry | Evidence |
|---|---:|---:|---|---|---|
| `IPAP` | 100 | 1 | hPa | `scales` | display reference, multi-point |
| `EPAP` | 100 | 1 | hPa | `scale_candidates` | display reference |
| `VolumeTargetDeltaPressure` | 100 | 1 | hPa | `scale_candidates` | display reference |
| `Frequency` | 10 | 1 | /min | `scale_candidates` | display reference |
| `Ti` | 1000 | 1 | s | `scale_candidates` | display reference |
| `Ti_min` | 1000 | 1 | s | `scale_candidates` | display reference |
| `Ti_max` | 1000 | 1 | s | `scale_candidates` | display reference |
| `Ti_timed` | 1000 | 1 | s | `scale_candidates` | display reference |
| `VolumeTarget` | 1 | 1 | ml | `scale_candidates` | display reference, **signal cross-check** |
| `TriggerSensitivityExspiration` | 1 | 1 | % | `scale_candidates` | display reference |

The three pressures share a ratio and only one of them is confirmed; the four
inspiratory times share one and none is. In both cases that is a result of
separate observations, not a factor inherited from a family of names.

`VolumeTarget` alone carries a second method: a measured channel agrees with
the setting. That corroborates **meaning and magnitude** and is not a second
reference point, which is why it remains a candidate — see `validate`'s
target-volume check, which exists to gather exactly this kind of evidence.

| where a parameter appears | what that says |
|---|---|
| `scales` | the conversion is **confirmed** — several distinct raw/display pairs giving one ratio, consistent with proportional scaling through the origin |
| `scale_candidates` | a conversion has been **observed**, resting on a single reference point, and may still change |
| neither | **no multiplicative conversion is known** — which covers categorical values, ordinal steps, displays derived from other parameters, and parameters nobody has examined |

**Nothing is applied.** `value_raw` is written exactly as the device stored it,
whether or not a conversion is published for that parameter.

**`name` repeats the key** so an entry lifted out on its own is still complete,
and `parameter_id` is the id that name carries in this archive's
`parametersmap.xml`. Two runs over one archive produce the same bytes, because
the whole file is serialised with sorted keys — not because these entries are
built in name order, which they also are but which decides nothing here.

**All three are empty unless the archive's `map_version` and
`config_version` are the pair the evidence was gathered against, every expected
id carries its expected name, and each of those names appears once in the map.**
Any inconsistency empties all of them rather than publishing part of a table: a
partly trustworthy registry under a version claimed to be known is not
something a consumer could act on.

**A scale describes only the numerical conversion of a raw parameter. It does
not establish that the parameter is active or applicable in a particular
program or ventilation mode.** A stored zero may mean "off" rather than zero,
and a device hides parameters that do not belong to the running mode while
still storing a value for them.

### `value_domains`

Parameters whose values are **not quantities**: named states and steps. A scale
would be the wrong question for them.

```json
"value_domains": {
  "TherapyMode": {
    "parameter_id": 92,
    "name": "TherapyMode",
    "kind": "categorical",
    "observed_labels": {"2": "ST", "8": "MPVp"},
    "evidence": ["device_display_reference"]
  },
  "InspirationRamp": {
    "parameter_id": 74,
    "name": "InspirationRamp",
    "kind": "ordinal",
    "observed_labels": {"1": "1", "4": "4"},
    "evidence": ["device_display_reference"]
  }
}
```

`kind` is `categorical` for a named state and `ordinal` for a step or rank.
Neither has a physical scaling, and **no parameter appears both here and in a
scale registry** — the two answer different questions.

**`observed_labels` is partial by construction and always will be.**
Observation can show what a value displays as; it can never show that no
further values exist. A device writing a value absent from this map is not an
error, and the way to meet that case is to show the number. Reading this map as
exhaustive is the mistake it most invites.

Keys are the raw value **as written**, matching `value_raw`, which is a string.
Where an ordinal's label repeats its key, that is the finding: the raw value is
displayed as-is rather than translated through a code mapping.

Labels are the device's own display text, quoted as written. They were read
from a device configured in German — a mode designation such as `ST` is the
manufacturer's across languages, a word such as `Manuell` is not.

**A domain says nothing about applicability either.** A parameter belonging to
a ventilation mode the programme does not run still carries a stored value.

#### What may change without a new schema version

Candidates may be **added**, **corrected** as evidence arrives, **removed**, or
**moved into `scales`**. A value domain may gain an observed label, or gain an
entry, the same way. None of that needs export schema version 2; the **shape of
an entry and its field types** are what stay fixed.

Entries in `scales` carry the stronger expectation, though not infallibility:

> Confirmed entries are expected to remain stable. If new evidence reveals an
> error, an entry may be corrected or withdrawn with an explicit changelog
> entry, release note, tests, and an appropriate package-version change. Schema
> version 2 is required only when the promised structure or field types change.

#### Where the evidence comes from

`evidence` names the **methods** a conversion rests on, never the values it was
derived from: `device_display_reference`, `known_setting_change`,
`multi_point_reference`, `arithmetic_cross_check`, `signal_cross_check`.

A `signal_cross_check` corroborates **meaning and magnitude** and is not a
second reference point. A measured channel declares its own scale in its file
header, and that says nothing about how a setting of the same name is stored —
`format.md` records where that confusion has already cost this project a
published claim.

> The candidate factors originate from private local device-display
> observations. The underlying personal settings and photographs are not
> published, so these observations are not independently reproducible from
> repository contents alone.

Each of the four inspiratory-time parameters carries its own single
device-display reference. Their common candidate factor results from separate
observations and was not inherited from the parameter names.

**`value_raw` is raw**, for every parameter, whether or not a conversion is
published for it in `scales` or `scale_candidates`. Nothing in the archive
states a scale — a pressure is an integer with the decimal point implied — and
this field carries what the device wrote. A factor is usually guessable from
the plausible range, and a pressure wrong by ten still looks like a pressure,
so the guess is left to nobody: the registries above say what is known and how
well, and this field stays unchanged either way.

**`program` is `null` for device-level parameters** and an integer for therapy
ones. `null` means "not a therapy parameter", which is a different statement
from program 0.

**`active_program_raw` is not resolved to an entry in `therapy_programs`.**
The programme attribute is zero-based and the device's own display counts from
one, so the mapping is `file program N = device program N+1`. That is recorded
in the notes, not applied here.

**The block-to-programme mapping has since been confirmed directly.** Two
programmes were photographed on the device and each differs from the other in
exactly the ways the corresponding block does — mode, trigger type, pressure
ramp and the upper pressure. That settles which block is which programme, and
it is a different claim from what `active_program_raw` means: a programme
selected on a settings page need not be the one in therapy. **That second
question stays open**, which is why this field is still not resolved.

The convention can also be checked without any display: hold the pressure a
session delivered against the settings in each program block and see which
block it belongs to. Anyone with a card can run that comparison, and the method
is the part that transfers.

**What that comparison produced here is not recorded**, for the reason
[`format.md`](format.md) gives: which program a session ran on describes a
person's therapy rather than the format. The mapping above is documented rather
than applied in any case.

**A snapshot may be a change record.** Most hold the device's full parameter
set; some hold a single entry, which is one setting being changed while the
device was recording. A change made while the device was *off* leaves no
record at all and appears only as a difference between the full snapshots
either side. **Reconstructing a settings history needs both**, and diffing a
full snapshot against a change record reports every absent parameter as a
change to nothing.

`notes` carries structural observations — an unusual programme count, an
unexpected numbering. They are not errors.

---

## `usage.jsonl`

The device's own long-term record. **Unlike every other file here, it does not
describe this archive's day** — it covers the device's whole past year, so a
card yields a year of rows, far more than its day archives hold.

```json
{"start_device_local": "2020-01-01T22:00:00", "raw_timestamp": 1000000,
 "therapy_day": "2020-01-01", "duration_minutes": 400, "second_quantity": 0,
 "duration_by_program": [400, 0, 0],
 "duration_by_category": [0, 0, 400, 0, 0, 0, 0, 0, 0, 0, 0],
 "second_by_program": [0, 0, 0],
 "second_by_category": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]}
```

| Field | Notes |
|---|---|
| `start_device_local` | the session's start, device clock, no zone |
| `raw_timestamp` | the stored counter before the 12-hour offset is applied |
| `therapy_day` | noon-to-noon, given so the binning need not be re-derived |
| `duration_minutes` | whole minutes, **truncated by the device** |
| `second_quantity` | never exceeds the duration; **nothing else about it is known** |
| `duration_by_program` | three values, summing to `duration_minutes` |
| `duration_by_category` | eleven values, summing to `duration_minutes` |
| `second_by_program` / `second_by_category` | the same two breakdowns of `second_quantity` |

**The eleven categories are positions, not modes.** No ordering has been
established and no source gives one, so there is deliberately no name field.
Do not label them — and note that finding a list of eleven modes elsewhere
would not establish an ordering either. A matching count is not an ordering.

**The same rows appear in every export taken from one card.** Each archive
carries its own copy of the source file, so exporting a directory of archives
produces one overlapping copy of roughly the same year per archive.
**Deduplicate on `start_device_local`.** The copies are not identical: a later
archive's copy holds more recent sessions and has dropped the oldest, because
the window rolls.

**Rows are deliberately not aggregated.** Therapy time per day is one line of
consumer code, but the day boundary is a decision — this device's therapy day
runs noon to noon, while a comparison against another source may want midnight.
`therapy_day` is supplied for the former; the choice stays with the consumer.

`usage_records` in the manifest counts the rows, and is `null` — not `0` — when
the archive has no such member. A firmware that omits the file is a different
thing from one that recorded nothing.

**Absent from this file:** any waveform, event, alarm or per-breath figure. The
long-term record carries when a session ran and for how long, and no more. For
the days a day archive also covers, that archive is the fuller source.

## `validation-report.json`

```json
{"sessions": [
  {"session": 1,
   "checks": {
     "breath rate": {"status": "reported",
                     "detail": "counted 15.00/min from the phase channel …",
                     "ratio": 1.004},
     "volume": {"status": "not comparable",
                "detail": "channel(s) Tidal Volume absent from this file",
                "ratio": null},
     "target volume": {"status": "reported",
                       "detail": "median delivered volume over the volume …",
                       "ratio": 1.0},
     "plausibility": {"status": "passed", "detail": null, "ratio": null},
     "long-term record": {"status": "passed", "detail": null, "ratio": 1.0}},
   "range_violations": 0,
   "duration_discrepancy_seconds": 1.5}
]}
```

`status` is `passed`, `failed`, `reported`, or `not comparable`. **Neither
`reported` nor `not comparable` is `passed`**, and reading either as success is
the misreading this file is shaped to prevent:

| `status` | means |
|---|---|
| `passed` | the check ran and the decode satisfied it |
| `failed` | the check ran and the decode did not satisfy it |
| `reported` | the check ran and produced a figure **this decoder asserts nothing about**, because no publicly derivable bound exists for it |
| `not comparable` | the check could not run at all |

The checks are independent: one can be impossible while another succeeds.

`plausibility` and `long-term record` assert something. Three checks only
report, and for three different reasons:

- **`breath rate`** — nothing published bounds how much a rate varies *within*
  one session, as opposed to how accurately the device measures it.
- **`volume`** — the relationship that was expected between integrated flow
  and reported tidal volume does not reliably hold, and the explanations
  cannot be told apart from the data.
- **`target volume`** — bounding it would be a clinical claim, for the reason
  given where the check itself is described below.

**A reader must handle status values it does not recognise**, which is what
the compatibility rules below already require: a later version may add one
without changing the schema version.

`target volume` is the one check holding a **setting** against a
**measurement**: the delivered volume, over the samples where the `Target
Volume` state channel reads one, against the target the device was given. It is
always `reported` — what separates a delivered volume from its target is what
the device is regulating, and a bound on it would be a clinical claim.

It runs only where the enabled programmes agree on **exactly one distinct
target**. Which programme a session ran under is not established, so the answer
has to be one that does not depend on knowing: two enabled programmes both set
to 500 ml give it, and two set differently are `not comparable` rather than
resolved by picking the closer. A stored zero is not a target of zero
millilitres: the
manual's settable range begins at 100 ml, so a zero is not a set value at all.
The comparison reads the target through the **candidate** factor for
`VolumeTarget`, which is the point — a ratio near one is evidence for that
candidate, and nothing is asserted either way.

`long-term record` compares a session's duration against the entry the device
banked for it in `statistic.proto`. The duration compared is the span between
the session's start and stop times, truncated — which is the quantity the
device banks, and not the span the signal file's records account for. Both
come from the same side of the device, so agreement does not prove the signal
decoding; it proves that session boundaries are read correctly. A session
shorter than a minute is `not comparable`: the device stores whole minutes, so
it records no entry.

`range_violations` counts samples outside the range their own header declares.
Any number above zero deserves attention: it is the signature of a misaligned
decode.

### Why `plausibility` is the check to read first

Every other figure here is derived from the file, so a header read wrongly but
consistently would satisfy all of them. `plausibility` compares decoded values
against limits published by the manufacturer, which the file cannot influence,
and is therefore the only entry that can contradict a self-consistent misparse.

Its bounds are loose on purpose, and **few**: most channels have none, because
most channels have no published upper bound to cite. Absence means *not
checked*, never *unbounded* — [`thresholds.md`](thresholds.md) lists which
bounds exist, which were withdrawn and why. Those that remain catch a decode
wrong by a factor of hundreds — the byte-order and channel-width failures that
actually happen — and say nothing about whether a therapy is going well. A
`failed` here means the numbers in this export should not be trusted. It never
means anything about a patient.

A `failed` detail names the channel, the values reached, the bound, and whether
that bound came from a settable range, a published hardware capability, or the
arithmetic of the channel's own unit. A bound may be open on one side, shown as
`unbounded`, where the manufacturer publishes a maximum and says nothing below
it. The detail also says when the bound is narrower than the range the device's
own header declares — in which case suspect the bound before the decoder.

---

## Signal CSV

Written only when `--signals` names channels, one file per channel per session,
under `session_NNNN/`.

```csv
sample_index,device_local_time,digital_value,physical_value,unit
0,2020-01-01T22:00:00,0,0.0,hPa
1,2020-01-01T22:00:00.100000,5,0.5,hPa
```

`sample_index` counts from zero within the session. `device_local_time` is
derived from that index, so it is exact rather than accumulated.
`digital_value` is the stored integer and `physical_value` the converted
figure; both are present so a wrong value can be attributed.

At 10 Hz across a night one channel is several million rows. That is why
signals are opt-in.

---

## Compatibility

Within version 1:

- **New fields may be added** to any object. A reader must ignore fields it
  does not know rather than fail on them.
- **Existing fields will not be removed or change meaning**, and a field's
  type will not change.
- **New `kind` values may appear** in `events.jsonl`, and new `status` values
  in the validation report. A reader should skip rows whose kind it does not
  recognise rather than assume it has seen them all.
- **Row order within a file may change** except where guaranteed above.

Anything else — a removed field, a changed meaning, a changed type — raises
`export_schema_version`. Check it; do not sniff for fields.

---

## What this format will not give you

No clinical figure of any kind. No named events. No timezone. No merged view of
trend curves and archives — those are separate sources with different
provenance, and deciding which to prefer is the consumer's decision, made
deliberately rather than inherited.

The device's long-term record **is** included, as `usage.jsonl` — but only what
it holds: session start times and durations for a year, with no waveform, event
or alarm behind them. It is not a substitute for a day archive, it is a longer
and thinner view.

No threshold in this export was chosen to fit a recording. Every number that
can make a check fail is listed in [`thresholds.md`](thresholds.md) with its
public source and derivation.

This is data from a decoder that refuses to guess. It is not medical software,
and nothing derived from it should be treated as clinical fact without checking
against the device or a clinician.
