# Manufacturer figures this decoder relies on

Every figure below is cited from one publicly downloadable document, with the
page it appears on.

> **Löwenstein Medical Technology GmbH + Co. KG**,
> *prisma VENT — Instructions for use for patients*,
> **WM 68431 / LMT 68431b, 03/2023**, English, 64 pages.
> Covers prisma VENT30 / VENT30-C / VENT40 / VENT50 / VENT50-C
> (device types WM110TD and WM120TD).
>
> <https://loewensteinmedical.com/media/user_upload/pdf/gebrauchsanweisung/prismaVent-ventilation-user-manual-EN-68431b.pdf>
>
> Linked from the product page at
> <https://loewensteinmedical.com/en/ventilation/prisma-vent50-50-c>, under
> *All important downloads at a glance*. Page numbers are the printed page
> numbers in the footer, which for this document match the PDF page numbers.

**That edition is the reference for everything below.** Every figure here was
read in it, and each row names the section and page it came from. Where editions
differ, this one is what the code is derived from.

**This is a citation list, not a reproduction.** It carries only the figures a
derivation in this repository actually uses, so a reader can check the
reasoning. For anything else — the full specification tables, the alarm
catalogue, operating instructions — consult the manual itself. It is the
manufacturer's copyrighted work and is not reproduced here.

---

## Figures used in derivations

| Quantity | Figure | Section, page | Used for |
|---|---|---|---|
| Maximum air flow at 20 hPa | above 220 l/min | 11.1.1, p. 47 | **nothing.** See "A lower bound is not a ceiling" below |
| IPAP pressure range, VENT50 | 4 hPa to 50 hPa | 11.1.1, p. 49 | the `IPAPsoll` maximum |
| PEEP pressure range | up to 25 hPa | 11.1.1, p. 49 | the `EPAPsoll` maximum |
| Maximum pressure in the event of a fault (PLS max) | ≤ 60 hPa | 11.1.1, p. 49 | the `Airway Pressure` maximum |
| Maximum therapy pressure (PWmax), VENT50 | 50 hPa | 11.1.1, p. 49 | context for the above; not itself a bound |
| Respiratory frequency, precision | ± 0.5 bpm | 11.1.1, p. 49 | the breath-rate tolerance in `validate.py` |
| Respiratory frequency, settable | 0 to 60 bpm, step 0.5 bpm | 11.1.1, p. 49 | **nothing.** A settable range — see "A settable range is not a scale factor" |
| Inspiratory time, shortest settable | 0.2 s | 11.1.1, p. 49 | the phase duty-cycle floor |
| Trigger stage, inspiration | 1 to 8 | 11.1.1, p. 50 | the `Trigger Level` maximum |
| Target volume, settable | 100 ml to 2,000 ml, step 10 ml | 11.1.1, p. 49 | **nothing.** A settable range, not a measurement limit |
| Tidal volume, specified range | 100 ml to 2,000 ml, ± 20 % | 11.1.1, p. 50 | **nothing.** See "A specified range is not a limit" |
| Minute volume, specified range | 0 l/min to 99 l/min, ± 20 % | 11.1.1, p. 50 | **nothing.** Same reason |

The derivations themselves — how each figure becomes a number in the code,
what range it applies over, what happens exactly at the boundary, and which
test pins it — are in [`thresholds.md`](thresholds.md). This file is only the
citation.

### A lower bound is not a ceiling

The manual gives maximum air flow as **"> 220 l/min"** (p. 47). That is a
guarantee about what the device can deliver — a *minimum* capability. It says
nothing about how large a flow the machine may reach, and a value above it is
therefore not evidence of anything.

An earlier version of this decoder used it twice as though it were a ceiling:
a flow bound of ±400 l/min "leaving room above the figure quoted", and a tidal
volume ceiling of about 15 litres computed as 220 l/min sustained for the
longest settable inspiratory time of 4 s. Multiplying a lower bound by a time
gives a lower bound, not an upper one. **Both were withdrawn**, along with the
channels that borrowed from them. See `WITHDRAWN_BOUNDS` in
`src/prisma_vent/device_limits.py`, which records each removal and its reason
next to the table it was removed from.

### A specified range is not a limit

Tidal volume and minute volume both appear in the specification table with a
range and a tolerance — 100 to 2,000 ml at ± 20 %, and 0 to 99 l/min at ± 20 %
(p. 50). That is the range over which the measurement's **accuracy is
specified**. Outside it the manual makes no promise about accuracy; it does not
say the device cannot report a larger value, and a decoder that treats it as a
ceiling fails correct decodes.

That is not hypothetical. The first version of the plausibility check bounded
tidal volume by the settable target-volume maximum and failed sessions whose
decode was correct — as isolated samples rather than as a shifted
distribution, which is what a wrong scale factor produces. The device's own
`.wmedf` header declares that channel wider than the manual's figure, so the
device contradicts the reading directly.

### A settable range is not a scale factor

The settable ranges above — 0 to 60 bpm, 100 to 2,000 ml, 4 to 50 hPa — say
what a clinician may dial in on the front panel. They are silent about how the
device then writes that setting into `parameter.xml`, which states no scale for
anything.

The temptation is to close the gap by arithmetic: if a raw setting divided by
some factor lands inside the manual's range and every other factor lands
outside, the factor looks settled. It is not, for two reasons.

**One value cannot establish a unique factor.** A raw value that already sits
inside the range is consistent with a factor of one, and also with any other
encoding that happens to produce a number in that range. Such a value does
rule candidates out — a factor that would push it outside the range is
excluded, and that is worth having — but ruling some out is not the same as
establishing one. The step size does not close the gap either without several
observed values.

**A range cannot separate neighbouring factors.** Where two candidate units sit
within a few percent of each other, a range spanning an order of magnitude
cannot tell them apart. That is precisely the case for pressures, where hPa and
cmH₂O differ by about two percent and the manual's range admits both readings.

What did establish the one confirmed pressure factor was not a range at all:
two programmes hold different raw values for it, both were read off the device
display, and the two pairs give one ratio consistent with scaling through the
origin. Two points, not one — which is the whole difference, since a single
pair is consistent with any number of encodings. `format.md` records which
parameters that covers and which it does not.

### The breath-rate tolerance is absolute, not relative

The stated ± 0.5 bpm (p. 49) is an absolute accuracy. It is 10 % of a rate of 5
and 2.5 % of a rate of 20, so a fixed percentage is not equivalent to it at any
rate but ten. The bound this decoder computes adds the arithmetic error of
counting breaths over a finite span: `0.5 + 1/span_minutes`, both terms public.
It is used to **report** a difference and never to fail one — see
[`thresholds.md`](thresholds.md) for why.

---

## Properties that constrain interpretation

These change what the numbers *mean*, and no amount of careful decoding removes
them.

**Two unit systems coexist in one file** (11.1.1, p. 52). Patient flow, target
volume, breath volume and minute volume are displayed in BTPS; all other flow
and volume values are in STPD. Mixing them produces a consistent few-percent
error that looks like noise rather than like a mistake.

**Alarms lag their own cause** (11.1.1, p. 51). Physiological alarms are
triggered three breaths after the alarm limit is reached; the pulse and SpO₂
alarms three seconds after; rebreathing ten breaths after; and the ARP alarm
limit occurs at most twenty breaths after. Correlating an alarm against the
signal traces must allow for this, or the alarm appears to precede what
triggered it.

**Displayed pressure, flow and leakage are low-pass filtered** (11.1.1,
p. 51). Whether the values written to the card are the displayed ones or raw
sensor output is not stated anywhere, so calling them raw is an assumption.

**The clock has no time source** (7, p. 43). The device carries a *Clock not
set* alarm whose stated remedy is to have the clock set by a specialist dealer
"so that course of therapy is recorded correctly". Nothing synchronises it.

**Data is held internally for at most fourteen days** (4.10, p. 25). Therapy
data and settings are stored inside the device for a maximum of 14 days. A
freshly inserted card therefore arrives already carrying history, and days
older than that window are not on the device at all.

**The circuit type changes what the numbers mean** (11.1.1, p. 49). The PEEP
range and the minimum therapy pressure both differ between a leakage circuit
and a single circuit with a valve — PEEP from 4 hPa in one and from 0 hPa in
the other, and PWmin 4 hPa against 0 hPa. Volumes should not be interpreted
without knowing which circuit was in use.

**Sessions may end with a low-pressure tail** (4.9, p. 24). softSTOP runs a
ramp, after which the device continues at a low pressure until it is put into
standby. Those minutes are not a therapy setting.

**The ventilation modes are not enumerated here.** The patient instructions
name modes in passing — S, S/T, autoS/T, T, aPCV, PSV and PCV appear together
on p. 24 as those supporting softSTART — but that is a list of modes with one
property, not the device's complete mode set in a stated order.

The long-term record has eleven categories, and nothing published here maps
them to names. **Two counts matching would not establish an ordering** even if
a list of eleven modes could be found, so the categories stay unnamed.

---

## Open-source note

The manual states (11.1.1, p. 52) that devices of types WM110TD and WM120TD use
FreeRTOS, and that the device software contains code subject to the GPL, with
the source code and the licence available on request.

If that source is ever obtained, **keep it strictly separate from this
repository**: this project is MIT licensed and must not take code from a
GPL-licensed body of work.
