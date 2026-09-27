# prisma VENT50 SD-card format

What this decoder has established about the files a prisma VENT50 writes, and
what it has not. Written as a specification: it says what the format *is*, not
what any particular recording contained.

**Scope.** Everything here was established against one device model on one
firmware version. A different firmware may differ, and several details below
are explicitly firmware-specific. Nothing is hardcoded that a file declares.

**No figure measured from a recording appears here**, and none appears
anywhere else in this repository — not in a source comment, not in a test, and
not as a threshold. Where a claim rests on comparing real files, it says so
without quoting what the comparison produced: those figures describe a person's
therapy rather than the format.

Manufacturer figures are a different matter and are cited with edition,
section and page in [`device-reference.md`](device-reference.md). Every number
this decoder can fail on is listed in [`thresholds.md`](thresholds.md) with its
derivation.

**Other people's work, and where it stops.** OSCAR's prisma loader — **GPLv3**
— was read while working out what these files are. **No code was taken, and
none may be:** this project is MIT licensed and cannot incorporate
GPL-licensed work. That reading gave orientation about what to look for; every
statement here was then confirmed against this device's own files, or it is
not here. It is also where the named respiratory event ids OSCAR carries for
the prisma *CPAP* models were seen, and they are deliberately not adopted —
see [Events, alarms and parameters](#events-alarms-and-parameters).

---

## Card layout

```
WM30640/
├── plugin/NNNNNNNNN/dcm.zip
└── SN########/                    device serial
    ├── NNNN_YYYY-MM-DD.zip        one archive per therapy day
    ├── battery/NNNN_SSSS_YYYY-MM-DD.wmedf
    ├── trendcurve/NNNN_YYYY-MM-DD.tc
    ├── logs/
    ├── configuration.json
    ├── device.xml
    └── prismaVENT.sdpvdat
```

`NNNN` is a therapy-day counter running since the device entered service, not
since the card was inserted, and it continues across card changes.

**The trees cover different spans.** Trend curves reach further back than the
daily archives; battery files further still. When a fresh card is inserted the
device writes out its internal buffer, which the manufacturer documents as
holding at most fourteen days — so a new card arrives already carrying history,
and the window differs per tree.

**A fourth source is nested rather than parallel.** `statistic.proto` sits
inside *every* daily archive, and each copy covers a rolling year rather than
that archive's day. The trees are therefore three resolutions of overlapping
spans, with the deepest nested inside the shallowest.

| Source | Reaches back | Per | Resolution |
|---|---|---|---|
| Day archives | weeks | session | 10 Hz waveforms, events, settings |
| `trendcurve/` | weeks more | 2 minutes | contents undecoded; therapy time estimable |
| `statistic.proto` | a rolling year | session | start time and duration only |

---

## `.wmedf` — EDF with modifications

**Do not point a standard EDF reader at these files.** They interleave 8-bit
and 16-bit channels in one record; a reader assuming uniform 16-bit misaligns
everything after the first 8-bit channel — silently, with values still in a
believable range.

- Version field is `1`, not `0`. The global reserved field ends `#s`.
- **Each signal declares its own storage width** in its 32-byte reserved field:
  `#1` for 8-bit, `#2` for 16-bit. Never infer it, and never assume
  `index × 2` offsets.
- **16-bit values are little-endian.** Decoded that way, an airway pressure
  channel stays inside the header's own declared physical range and produces a
  smooth respiratory waveform; big-endian yields pressures two orders of
  magnitude too high, well past the 60 hPa the device is specified never to
  exceed.
- **8-bit signedness follows the declared digital minimum.**
- Scaling is affine and must be written out in full:

  ```
  scale    = (phys_max − phys_min) / (dig_max − dig_min)
  physical = phys_min + (digital − dig_min) × scale
  ```

- Records last one second on the firmware examined, mixing 1 Hz and 10 Hz
  channels. The header states the duration; read it rather than assuming. This
  decoder does read it, so a firmware writing longer records is handled rather
  than silently misread by that factor.

### The battery files share the extension and not the format

`battery/NNNN_SSSS_YYYY-MM-DD.wmedf` is **standard EDF**: version field `0`,
every channel 16 bits, no per-signal width markers. It is the extension that
misleads, not the file.

**This package does not read them**, and refuses them by the version field
rather than guessing. That refusal is the point: the two formats differ in
exactly the way that makes a wrong guess silent, and a reader willing to try
both would be a reader that misaligns one of them without saying so. A standard
EDF reader handles these files correctly, which is the other half of the same
statement.

Two properties are worth knowing before pointing one at them. The record count
is routinely `-1` — the EDF value for "unknown", which a device writes while
recording and does not go back to correct — so the file's size is the
authority. And the declared physical range is nominal here too, more strongly
than in the signal files: some channels are flags packed into 16 bits and
declare an 8-bit range they then exceed, so a reader that validates against the
header would reject them.

### Declared ranges are nominal, not clamps

Samples outside the range a header declares do occur, and they are two distinct
phenomena rather than one:

- **Clipping** — a volume channel pins at 32767, the maximum of a signed 16-bit
  sample. The declared range is narrower than the storage, and the channel runs
  out of type rather than out of measurement.
- **Overshoot** — flow channels and a debug volume channel exceed their
  declared bounds modestly, without ever reaching an int16 extreme.

A reader that clamps to the declared range would silently alter data; one that
raises on every excursion would refuse sessions whose decode is correct.
Reporting them is the only option that neither hides nor exaggerates.

---

## Time

- Offsets are signed `±HHHH:MM:SS.mmm` from a reference midnight. Hours
  routinely exceed 24 and may be negative.
- **The therapy day runs noon to noon.**
- **Three different reference midnights are in play.** The day-level log counts
  from midnight of the archive's own date throughout. A session's *start*
  counts from midnight of the day that session began. A session's *end* may
  count from midnight of the day it **ended**.

  Converting a session's start and stop against the same midnight displaces the
  value by exactly 24 hours whenever a session crosses midnight — and a night
  shifted by a whole day still reads as a perfectly ordinary night.
- The trailing `Z` in the device logs does **not** mean UTC.
- The device clock runs local time, confirmed against the device display. The
  manufacturer documents no time source, so it drifts.

---

## Events, alarms and parameters

`RespEvent` entries carry a time in whole seconds from session start, a numeric
id, a `begin`/`end` phase and a strength. **They are not chronologically
ordered**; sort on read.

**No event or alarm id has been identified.** The names are known — the
manufacturer's manual lists the display strings, and `statistic.proto` carries
alarm keys as configuration — but nothing joins those names to the numeric ids,
which live in a different id space. Events can be counted and placed in time.
They must not be named, and no index that depends on knowing what an event *is*
may be derived from them.

### Parameters

A snapshot holds a leading block of device parameters followed by one block per
therapy program, each block carrying the same id sequence. The decoder
**detects** that structure and validates it rather than hardcoding the block
sizes.

Therapy parameters carry an explicit `program` attribute; device parameters do
not. `parametersmap.xml` maps ids to names — **for parameters only**. Event ids
live in a different space and collide numerically.

**Reconstructing a settings history takes two sources.** A change made while
the device is recording appears as its own single-entry record. A change made
while the device was off leaves no record at all, and shows only as a
difference between the full snapshots either side. A reader using either alone
produces a plausible, gap-free-looking history that is wrong.

**Nothing states a parameter's scale.** A pressure is an integer with the
decimal point implied. **One** has a factor confirmed — inspiratory pressure,
against two distinct display readings giving one ratio; every other value is
reported raw.

Expiratory pressure is a candidate rather than confirmed. The card carries the
**same raw value in every programme block of every archive**, so however many
readings corroborate it, they corroborate one point — and one point cannot
establish a factor, however convincing each corroboration is on its own.

**What is known, and how well, is machine-readable.** The export's `scales` and
`scale_candidates` say which conversions are confirmed, which rest on a single
reference point, and — by a parameter's absence from both — which are unknown.
Before that existed, a consumer had to read this paragraph and hard-code the
answer, and at least one did so wrongly. See `export-schema-v1.md`.

**Not every parameter is a quantity.** Modes, trigger types and the pressure
ramps are named states or steps, for which a scale is the wrong question
entirely. They are listed in `value_domains` instead, with the labels observed
for the values seen — a map that is partial by construction, because observing
what a value displays as can never show that no other values exist.

**A signal channel and a therapy parameter may share a name and share no
scale.** `Frequency` is both: a `.wmedf` channel, whose header declares its own
digital-to-physical mapping, and a setting in `parameter.xml`, which declares
nothing. The two are different quantities from different files, and a channel
scale is evidence about that channel only. Counting a channel among the
confirmed *parameter* factors is the mistake the shared name invites, which is
why this section names the parameters it means rather than giving a count.

**A settable range does not establish a factor.** The manual documents the
ranges and step sizes a clinician may dial in (see `device-reference.md`).
Those bound the front panel, not the encoding: they can exclude some candidate
factors, and a single observed value inside the range cannot establish a unique
factor.

---

## `statistic.proto`

A serialised Protocol Buffers payload — not a schema. It needs no `.proto`
file: the wire format is self-delimiting, so the structure can be walked and
only the field *names* require a schema.

| Field | Content |
|---|---|
| 1 | a format version string |
| 2 | one record per session, newest first |
| 3 | a JSON configuration with parameters **named**, plus short version strings |
| 4 | lifetime therapy time in minutes |
| 5 | a second lifetime counter, **unidentified** |

Each record carries a timestamp, a duration in whole minutes, a second
unidentified quantity, and both quantities broken down into three values and
into eleven. **A breakdown is a partition of its total**, so all four sum
identities are a property of the format rather than an observation, and the
decoder enforces them.

- The timestamp is a seconds counter running **exactly twelve hours behind**
  Unix epoch time, and it tracks the device's own local clock. It carries no
  zone information — it is local time in epoch clothing.
- Three is the program slots. **Eleven is not named**, and no source gives an
  ordering for eleven positions. A list of eleven modes found elsewhere would
  not establish one either: a matching count is not an ordering.
- The lifetime therapy counter can be checked between consecutive days against
  that day's session durations, since every archive carries its own copy. The
  decoder exposes both figures so a caller can do it; what the check produced
  here is not published.
- Per-program histogram blocks are present and undecoded.

The embedded JSON configuration is the best independent check available on the
parameter decoding: it is keyed by name where `parameter.xml` is keyed by
number, so the two can be compared and a disagreement would point at the block
detection, the id-to-name map or the program ordering. The decoder exposes
both. It could **not** confirm the scales in any case — both state the same
integers, and neither says where the point goes.

---

## `.tc` trend curves

A JSON header followed by 9-byte records, each populated record standing for
two minutes of therapy. That interval was established by comparing days that
carry both a trend curve and a day archive; the comparison is repeatable by
anyone with a card, and its figures are not published here.

**The nine bytes are not decoded.** Two of the nine are zero throughout and two
more are near-constant with the high bit set, which looks like packed fields
rather than integers. The decoder hands them back untouched.

---

## What remains unknown

- Every event and alarm id.
- The scale of most parameters.
- The device clock's relationship to UTC, and daylight saving behaviour.
- Whether the stored traces are raw sensor output or the filtered values the
  device displays. The manufacturer documents the *displayed* values as
  low-pass filtered and says nothing about what is written.
- The meaning of the eleven categories in the long-term record, the second
  lifetime counter, and the histogram blocks.
- The `statistic.proto` record timestamp's exact semantics beyond matching a
  session start.
