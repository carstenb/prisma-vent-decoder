# Every number this decoder asserts on

A decoder that refuses to guess must not smuggle guesses in as thresholds. A
threshold fitted to one person's recordings is two bad things at once: nobody
else can check it, and the number itself is a measurement of that person's
therapy published in a public file.

So this document lists **every numeric threshold that can make this decoder
fail, refuse or reject something**, with:

- the exact public source, or the statement that there is none;
- the manual edition, section and page where the source is a manual figure;
- the complete derivation;
- the range it applies over;
- what happens exactly at the boundary;
- the synthetic test that pins it.

The manual is cited throughout as *IFU p. N*, meaning
[`device-reference.md`](device-reference.md)'s single source:
**Löwenstein Medical Technology, *prisma VENT — Instructions for use for
patients*, WM 68431 / LMT 68431b, 03/2023**,
<https://loewensteinmedical.com/media/user_upload/pdf/gebrauchsanweisung/prismaVent-ventilation-user-manual-EN-68431b.pdf>.

**If a number is not in this document, it must not be able to fail a run.**
Adding a threshold means adding a row here in the same commit, and adding a
boundary test. If no sound public derivation exists, the check reports instead
of asserting, or it does not exist.

---

## Summary

| Threshold | Where | Asserts? | Basis |
|---|---|---|---|
| `Airway Pressure` ≤ 60 hPa | `device_limits.py` | yes | IFU p. 49 |
| `IPAPsoll` ≤ 50 hPa | `device_limits.py` | yes | IFU p. 49 |
| `EPAPsoll` ≤ 25 hPa | `device_limits.py` | yes | IFU p. 49 |
| `Trigger Level` ≤ 8 | `device_limits.py` | yes | IFU p. 50 |
| percentage channels 0–100 % | `device_limits.py` | yes | the declared unit |
| start skew | `archive.py` | **no** — reported | nothing public bounds it |
| `_IMPLAUSIBLE_SESSION_SPAN` = 24 h | `archive.py` | yes | the size of the correction it guards |
| `_LONG_TERM_SKEW` = 60 s | `validate.py` | yes | whole-minute storage |
| `_LONG_TERM_FLOOR` = 1 min | `validate.py` | yes | whole-minute storage |
| `MIN_SECONDS_FOR_RATE` = 120 s | `validate.py` | precondition | IFU p. 49 plus arithmetic |
| `MIN_PHASE_DUTY_CYCLE` = 0.001 | `validate.py` | precondition | IFU p. 49 plus arithmetic |
| `_TARGET_VOLUME_SETTABLE_ML` = 100–2,000 ml | `validate.py` | precondition | IFU p. 49 |
| `DEVICE_RATE_ACCURACY_PER_MIN` = 0.5 | `validate.py` | **no** — reported | IFU p. 49 |
| declared digital range | `wmedf.py` | yes | the file's own header |
| structural sum identities | `statistic.py` | yes | the format |
| resource caps | several | yes | not about decoded values |

Numbers that were considered and are **not** applied are listed at the end,
each with the reason. `WITHDRAWN_BOUNDS` in
`src/prisma_vent/device_limits.py` carries the same list beside the table it
would have belonged to, so a later reader meets the answer where the
temptation is.

---

## Published bounds on decoded values

These live in `src/prisma_vent/device_limits.py` and are the only check in the
package that compares decoded values against something **outside the file**.
Every other check is self-referential and would be satisfied by a header read
wrongly but consistently.

Every one of them is **one-sided** except the percentages. That is not
squeamishness: a floor of the kind that suggests itself — a sub-atmospheric
allowance on pressure, a zero floor "because the channel reads zero outside
therapy" — describes one recording rather than a published figure.

### `Airway Pressure` ≤ 60 hPa

- **Source.** IFU section 11.1.1, p. 49: *PLS max (maximum stable limit
  pressure), maximum pressure in the event of a fault — ≤ 60 hPa*.
- **Derivation.** None. The published figure is used directly, in the
  direction it is published in. The looser fault ceiling is used rather than
  the 50 hPa maximum therapy pressure on the same page, so that a device in a
  fault condition is not reported as a decode error.
- **Applies to.** The `Airway Pressure` channel only. The internal debug
  pressure sensors (`P main 2 Dbg`, `P valve ctrl Dbg`, `P valve air Dbg`) are
  **not** bounded: the published figure is about the pressure the device
  delivers, and nothing states what an upstream valve-control sensor reads.
- **Boundary.** The manual says *≤ 60*, so 60 hPa is inside. A decoded maximum
  of exactly 60.0 passes; 60.001 fails.
- **Open below.** No published figure states how far below zero this sensor may
  read, so the minimum is `None` and the low side is not checked at all.
- **Tests.** `test_each_retained_maximum_is_the_cited_figure_and_is_inclusive`,
  `test_an_open_side_is_not_checked`,
  `test_a_self_consistent_file_with_impossible_values_fails`,
  `test_the_internal_pressure_sensors_are_not_bounded_by_analogy`.

### `IPAPsoll` ≤ 50 hPa, `EPAPsoll` ≤ 25 hPa, `Trigger Level` ≤ 8

- **Source.** IFU p. 49 for the two pressures (*IPAP pressure range, prisma
  VENT50: 4 hPa to 50 hPa*; *PEEP pressure range: … to 25 hPa*), IFU p. 50 for
  the trigger (*Trigger stage, inspiration: 1 (high sensitivity) to 8 (low
  sensitivity)*).
- **Derivation.** None; the published maxima are used directly.
- **Applies to.** Channels that report a **setting** and no others. A settable
  range says what a clinician may dial in, so applying one to a measured
  channel is a category error — and is the specific error that produced false
  failures on correct decodes the first time this module ran.
- **Boundary.** Inclusive: exactly 50, 25 and 8 pass.
- **Open below.** The published minima (IPAP 4 hPa, PEEP 4 hPa on a leakage
  circuit and 0 hPa with a valve, trigger level 1) bound what may be *set
  during therapy*. They do not bound what the channel carries at another
  moment, so no floor is applied. The PEEP minimum also differs by circuit
  type, which the decoder does not always know.
- **Tests.** `test_each_retained_maximum_is_the_cited_figure_and_is_inclusive`,
  `test_no_measured_channel_is_bounded_by_a_settable_range`,
  `test_every_retained_bound_is_one_sided_except_the_percentages`.

### Percentage channels, 0 to 100 %

`SpO2 Value`, `SpO2 Sig. Qual.`, `Part Inspiration`, `Part Spont Insp.`,
`Part Spont Exp.`

- **Source.** The unit the file's own header declares, not the manual.
- **Derivation.** A proportion expressed in per cent runs from 0 to 100 by
  definition of the unit. This needs no manufacturer figure and no recording,
  which makes it the tightest honest bound in the table and the only two-sided
  one.
- **Boundary.** Inclusive at both ends: 0.0 and 100.0 pass, −0.001 and 100.001
  fail.
- **Note.** These bounds are frequently *narrower* than the range the file's
  header declares. That combination is the signature of the settable-range
  mistake, so `plausibility()` says so in the failure detail and tells the
  reader to check the bound before suspecting the decoder.
- **Tests.** `test_a_percentage_below_zero_is_out_of_bounds`,
  `test_a_bound_narrower_than_the_header_says_so`.

---

## Time-base thresholds

### Start skew — reported, with no bound at all (`archive.py`)

How far the session XML's start sits from the whole-second start in the signal
header.

- **Source.** None, and that is the finding. The signal header stores whole
  seconds and the session XML stores milliseconds, so truncation accounts for
  up to one second. Beyond that the two files are stamped by different
  subsystems, and **nothing in the format and nothing the manufacturer
  publishes bounds the difference between them.**
- **Two withdrawn bounds.** 2 s had no public derivation. 60 s was justified as
  "the coarsest quantum below a day" — but this format works in seconds and
  milliseconds too, so a minute is not one of its invariants and the number
  was chosen rather than derived. Neither is described here in terms of the
  data it was set against, because a bound stated as "just above what was
  measured" publishes that measurement.
- **Both failed in both directions.** A bound accepts a mispaired file whose
  skew falls under it, and refuses correct data whose skew exceeds it. Neither
  error is detectable from the file.
- **What replaces it.** The skew is measured, carried on
  `Session.start_skew`, and exported as `start_skew_seconds`, unbounded and
  possibly negative. Nothing rejects on it.
- **What still rejects a wrong reference.** The therapy day a session resolves
  into must be the day the archive is named for. A start resolved against the
  wrong reference midnight moves the session by a whole day, so this catches
  the failure the skew bound was aimed at — and it needs no chosen number.
- **The residual risk, stated plainly.** A mispaired session file whose skew
  is smaller than a therapy day is now accepted, where a 60 s bound would
  sometimes have refused it. That is a deliberate trade: refusing on an
  underivable number is the error this project exists to avoid, and the skew
  is exported so a consumer with grounds to judge it can.
- **Applies to.** Every session in every archive.
- **Boundary.** There is none. Every value, including a negative one, is
  accepted and reported verbatim.
- **Tests.** `test_no_start_skew_bound_exists_any_more`,
  `test_any_start_skew_is_accepted_and_reported_verbatim` (parametrised from
  zero to hours, in both directions),
  `test_the_therapy_day_is_what_still_rejects_a_wrong_reference`,
  `test_the_skew_reaches_the_export`,
  `test_a_negative_skew_survives_the_export_as_a_negative_number`.

### `_IMPLAUSIBLE_SESSION_SPAN` = 24 hours (`archive.py`)

- **Source.** The size of the correction it guards.
- **Derivation.** A session's stop may count from midnight of the day it
  *ended*, so `_resolve_stop` adds exactly one day when the stop resolves
  before the start. A correction applied wrongly therefore produces a span of
  exactly 24 hours or more. The number is that correction's own size, not a
  figure chosen to fit any session length.
- **Boundary.** The comparison is `>=`, because exactly 24 hours is precisely
  what the wrong correction produces.
- **Tests.** `test_empty_session_with_equal_offsets_has_zero_duration`,
  `test_session_crossing_midnight_resolves_its_stop_to_the_next_day`.

### `_LONG_TERM_SKEW` = 60 s and `_LONG_TERM_FLOOR` = 1 minute (`validate.py`)

- **Source.** The format: the device's long-term record stores durations in
  whole minutes.
- **Derivation.** One storage quantum. A record's idea of when a session began
  can differ from the archive's by up to one minute, so that is the matching
  window; and a session shorter than one minute truncates to zero and leaves no
  record at all, so its absence is expected rather than a finding.
- **The window's width cannot produce a wrong match.** Where more than one
  record falls inside it, the session is reported *not comparable* rather than
  attributed to one of them. Ambiguity is refused, never resolved.
- **Boundary.** `abs(record_time - session_start) <= 60 s` matches. A session
  spanning less than 60 s is not comparable; 60 s exactly is compared.
- **Tests.** `test_the_floor_is_measured_against_the_span_the_device_banks`,
  `test_a_missing_entry_for_a_banked_minute_still_fails`.

---

## Preconditions on the breath-rate report

Neither of these can fail a run. They decide whether a figure is produced at
all, and producing a meaningless one would be worse than producing none.

### `MIN_SECONDS_FOR_RATE` = 120 s

- **Source.** IFU p. 49 (*Respiratory frequency, precision: ± 0.5 bpm*) plus
  arithmetic.
- **Derivation.** Counting breaths over a span of *T* minutes can be out by one
  breath at either end, which is `1/T` breaths per minute. Requiring that term
  to be no larger than the device's own published accuracy, so neither error
  source dominates: `1/T <= 0.5`, hence `T >= 2 minutes`.
- **Expressed in seconds of recording, not in records.** A record's duration is
  stated in the header and read from it; assuming one second per record would
  make this threshold wrong by exactly that factor on other firmware.
- **Boundary.** A session of exactly 120 s is compared; shorter is *not
  comparable*.
- **Tests.** `test_the_minimum_span_is_where_the_two_error_terms_are_equal`,
  `test_minimum_length_is_measured_in_seconds_not_records`.

### `MIN_PHASE_DUTY_CYCLE` = 0.001

- **Source.** IFU p. 49 (*Ti min, Ti max, Ti timed: 0.2 s to 4 s*) plus
  arithmetic.
- **Derivation.**

  ```
  shortest settable inspiratory time                    0.2 s
  longest period two rising edges can span in a
  session of MIN_SECONDS_FOR_RATE                       120 s
  smallest duty cycle the device can produce      0.2/120 = 0.00167
  rounded DOWN to the next power of ten                 0.001
  ```

- **Rounded down, on purpose.** This is a precondition for "is anything
  alternating at all", so a value above what the device can produce would
  refuse legitimate sessions. Rounding down cannot.
- **Corrects an earlier value.** It was 0.01, derived from a shortest
  inspiratory time of 0.5 s over a 60 s period. The manual gives 0.5 s for
  `Ti/Ti max` but **0.2 s** for `Ti min` (same table, same page), so the old
  floor sat above the device's own minimum.
- **Boundary.** Exactly 0.001 is accepted; below it the check reports *not
  comparable*, with a message that says on-demand ventilation and an idle
  machine both look like this and the channel cannot tell them apart.
- **Tests.** `test_a_session_without_regular_breathing_is_not_compared`.

### `_TARGET_VOLUME_SETTABLE_ML` = 100 ml to 2,000 ml

- **Source.** IFU section 11.1.1, p. 49 (*Target volume, settable: 100 ml to
  2,000 ml*), quoted in [`device-reference.md`](device-reference.md).
- **What it decides.** Whether a **stored** value is a setting at all — and
  nothing else. A programme with a target outside this range has not had one
  dialled in; the device writes zero there, and zero is not a target of zero
  millilitres.
- **A settable range, used the one way a settable range may be used.** The
  same figure was once applied to the *measured* tidal volume channel and
  failed correct decodes, because the device's own header declares that
  channel wider — see "A specified range is not a limit" in
  `device-reference.md`. Bounding a setting is the opposite use and the
  legitimate one: this is what the manufacturer says the front panel accepts.
- **Applicable range.** Values read from the `VolumeTarget` parameter, through
  its **candidate** scale. If that candidate is wrong the filter is wrong with
  it, which is one more reason the check it gates only ever reports.
- **Boundary.** Inclusive: exactly 100 ml and exactly 2,000 ml count as set.
- **Outside it, only zero means "no target".** Zero is the documented off
  value, and a programme carrying it is passed over; where no enabled programme
  has a target the check reports *not comparable*. Any **other** value outside
  the range refuses the check instead of being passed over, naming the value.
  Skipping it would leave another programme's target standing as if it were
  unambiguous — a clean answer produced by discarding the evidence against it.
- **Asserts nothing.** It can only make the target-volume check refuse to run.
  No decode fails on it and no exit code moves.
- **Tests.** `test_the_settable_range_includes_both_its_endpoints` pins both
  endpoints, `test_a_stored_zero_is_not_a_target_of_zero_millilitres` the off
  value, `test_a_non_zero_target_outside_the_settable_range_is_refused` the
  difference between the two, and
  `test_the_target_volume_check_never_passes_or_fails` that none of it can
  move an exit code.

---

## Thresholds that are not about decoded values

Listed for completeness, since they can also make a run fail.

- **The declared digital range** (`wmedf.py`). A sample outside the range *its
  own header* declares is reported as a range violation, which fails normal
  validation. The bound comes from the file being read, so there is nothing to
  derive and nothing to fit.
- **Structural sum identities** (`statistic.py`). Each breakdown must sum to
  its total, and the second quantity must not exceed the duration. These are
  properties of the format — a partition sums to what it partitions — and not
  numbers chosen by anyone.
- **Resource caps.** `ArchiveLimits` in `archive.py`, `MAX_FILE_BYTES` and
  `MAX_RECORD_BYTES` in `statistic.py`, the manifest size cap in
  `copy_card.py`, and the guard's binary-size cap in `scripts/pre-commit`.
  These bound what an untrusted file may make this code allocate. They say
  nothing about whether a value is right, and a file refused by one of them is
  refused loudly.

---

## Considered and not applied

Each of these could fail a run, and each suggests itself readily enough that
it is worth saying why it is absent. None has a public derivation that
survives being checked. None is reworded into something weaker either: a
private threshold under new wording is still a private threshold.

| Channel or figure | The number it would have been | Why it is not applied |
|---|---|---|
| `Patient Flow`, `Patient Flow Dbg`, `Leakage Flow`, `TotalLeakage` | ±400 l/min | Derived from *maximum air flow > 220 l/min* (IFU p. 47), which is a guaranteed **minimum** capability. A lower bound cannot become a ceiling |
| `Tidal Volume`, `Online Volume` | −1,000 to 15,000 ml | 15 l was 220 l/min × 4 s — the same lower bound multiplied by a time, which is still a lower bound |
| `Minute Volume` | −50 to 250 l/min | Same withdrawn flow figure. The manual's 0–99 l/min (IFU p. 50) is the range over which ± 20 % accuracy is specified, not a limit on what may be measured |
| `Frequency` | 0 to 300 /min | "Beyond any breathing a human sustains" — an assertion about the body with no cited source. The manual's 0–60 bpm is the settable and displayed range |
| `Pulse Rate` | 0 to 300 /min | "Beyond any physiological pulse" — no cited source, and no published range for the channel at all |
| `P main 2 Dbg`, `P valve ctrl Dbg`, `P valve air Dbg` | −10 to 60 hPa | The 60 hPa fault ceiling is published for the pressure delivered to the patient. Extending it to internal sensors by analogy is an inference |
| every negative floor | −10, −50, −1,000, 0.0 | Each described a recording — a sub-atmospheric allowance, "reads zero outside therapy" — rather than a published figure |
| `GROSS_DISAGREEMENT_FACTOR` | 2.0 | Justified by the ratios some misparses produced during development, plus an unsourced claim that nothing physiological approaches a factor of two. The breath-rate check now **reports** and never fails |
| the `IMPOSSIBLE` basis | "physically impossible" | The category itself rested on assertions about the human body. Replaced by `ARITHMETIC`, which means only "the unit closes this end by definition" |
| `_MAX_START_SKEW` | 2 s, then 60 s | Neither had a public derivation. Both are gone: the skew is now reported, never asserted on |
| `MIN_PHASE_DUTY_CYCLE` | 0.01 | Derived from the wrong inspiratory-time figure. Replaced by 0.001, above |
| the long-term index's de-duplication | — | `{start: record}` silently kept one record per timestamp, so the same file passed or failed depending on record order. Duplicates are now all kept and reported ambiguous |

The volume check has never asserted and still does not: integrating
inspiratory flow was expected to land at or above the reported tidal volume,
and it does not reliably do so. The competing explanations cannot be told apart
from the data, so the ratio is reported with no bound.

Both unasserted checks now carry the status `reported` rather than `passed`.
Those are different claims, and calling a check passed when nothing was
asserted credits the decode with surviving a test that was never applied.

---

## Publishing this repository

Recorded here because it is the same rule applied to the history rather than
to a threshold, and because it cannot be fixed by another commit.

**A deletion commit does not remove anything.** Git keeps every version of
every file, so a private observation removed in commit *n* is still readable
in commit *n−1*, and a commit's author address is part of the commit itself.
Rewriting either means rewriting the commits that carry them, which changes
every hash after the first.

So publication requires a history that never contained them:

1. A fresh object store — a new `git init`, not a rewrite of an existing
   `.git`, and not `--amend`. Amending leaves the pre-amend blobs unreachable
   but present, which `scripts/pre-commit --history` reports and a publication
   audit should not have to explain away.
2. A root commit authored with a **GitHub noreply address**
   (`<id>+<login>@users.noreply.github.com`), not a personal one.
3. `scripts/pre-commit --history` clean in that new repository, with an object
   inventory showing zero unreachable blobs.

**Private Vulnerability Reporting cannot be enabled first.** GitHub offers it
for public repositories only, so the API answers 404 while a repository is
private. The ordering is therefore: publish, then enable, then verify. Until
it is on, `SECURITY.md` tells a reporter that a missing *Report a
vulnerability* option means this document is wrong rather than that they are
in the wrong place, so the gap fails safe.
