"""Published bounds from the manufacturer, used as the one external check.

Every other check in this package is self-referential. The range check in
:mod:`prisma_vent.wmedf` compares a sample against the range *the same header*
declares, which catches a misaligned record but not a misread header: if the
scale factors or the channel order were wrong, the file would still agree with
itself perfectly and every value would look fine.

These bounds come from outside the file — the manufacturer's patient
instructions for use, cited with edition, section and page in
``docs/device-reference.md`` — which is why they can catch a header read
wrongly but consistently.

What may become a bound, and what may not
-----------------------------------------

**A published figure is only usable in the direction it is published in.**
The manual gives maximum flow as "> 220 l/min at 20 hPa". That is a *lower*
bound on what the device can deliver — a guarantee of capability. It says
nothing about how large a flow the machine may reach, so it cannot be turned
into a ceiling, and no value derived from it can either. A tidal volume
ceiling computed as 220 l/min x 4 s is that same lower bound multiplied by a
time; it is not an upper bound on anything.

An earlier version of this table did exactly that, and also carried ceilings
for rate and pulse justified only by an assertion about what a human body can
do. Both kinds have been removed rather than reworded. **A channel with no
publicly cited upper bound is simply not checked** — see :func:`bound_for`,
where absence means *not checked* and never *unbounded*.

**Settable ranges bound settings, not measurements.** IPAP 4 to 50 hPa and
target volume 100 to 2000 ml say what a clinician may dial in. They bound the
channels that report a setting and nothing else. Reading the second kind as
the first is a mistake this module made on its first run: tidal volume was
bounded by the manual's settable target-volume range, and correct decodes were
failed for exceeding it. The device's own ``.wmedf`` header declares that
channel wider than the manual's settable maximum, so the device contradicts
the reading directly.

**Every retained bound is one-sided unless arithmetic closes the other side.**
Each figure below is a published maximum. None of the floors that used to sit
beside them survived: "a little sub-atmospheric excursion" and "the channel
reads zero outside therapy" are descriptions of one recording, not published
figures, and a floor chosen that way rejects on evidence that cannot be shown.
``minimum`` is therefore ``None`` on every bound except the percentages, where
the unit the file itself declares closes both ends by definition.

Why loose bounds still earn their place
---------------------------------------

The bounds that remain are far looser than anything a patient produces, which
looks like it makes them worthless. It does not, because the failures they
exist to catch are not subtle. Byte order, a misread width marker and a
channel misalignment are wrong by factors of hundreds, not by ten: read
little-endian data as big-endian and an ordinary airway pressure becomes two
orders of magnitude larger, far past the 60 hPa the device is specified never
to exceed. A bound tightened until it hugs a recording would start reporting
the patient instead of the parser, which is the error this module already made
once.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "PhysicalBound",
    "BOUNDS",
    "bound_for",
    "SETTABLE",
    "CAPABILITY",
    "ARITHMETIC",
]

#: A number the clinician dials in. The device cannot be set outside it, so a
#: value outside means the decoding is wrong. Applies only to channels that
#: report a setting.
SETTABLE = "settable range"

#: A published limit of the hardware — a pressure the device is specified not
#: to exceed. Applies to measured channels, because a sensor cannot report what
#: the machine cannot produce. Only usable where the manual states it as a
#: maximum; a stated *minimum* capability is not a ceiling and is not used.
CAPABILITY = "hardware capability"

#: Closed by the unit itself rather than by any published figure: a channel the
#: file declares in per cent runs from 0 to 100 by definition of the unit. This
#: replaces the former "physically impossible" basis, which rested on
#: assertions about the human body that no cited source supported.
ARITHMETIC = "arithmetic"


@dataclass(frozen=True)
class PhysicalBound:
    """A published range for one channel, in that channel's own unit.

    ``minimum`` and ``maximum`` are independently optional. A bound with only a
    ``maximum`` checks only the upper end, which is the honest shape for a
    figure the manufacturer publishes as a maximum and says nothing below.
    ``None`` on a side means *that side is not checked*, never *unbounded in
    that direction is fine*.
    """

    label: str
    minimum: float | None
    maximum: float | None
    unit: str
    #: One of :data:`SETTABLE`, :data:`CAPABILITY`, :data:`ARITHMETIC`. Which
    #: kind of figure this is — the distinction the module docstring explains,
    #: and the first thing to check when a bound fires.
    basis: str
    #: Where the number comes from and how it was derived, kept with the number
    #: so a later reader can judge whether a failure means the decode or the
    #: bound. Manual figures name the section in ``docs/device-reference.md``.
    source: str

    def __post_init__(self) -> None:
        if self.minimum is None and self.maximum is None:
            raise ValueError(f"{self.label}: a bound must constrain something")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum >= self.maximum
        ):
            raise ValueError(f"{self.label}: minimum is not below maximum")

    def describe(self) -> str:
        """The bound as a range, with an open side shown as such."""
        low = "unbounded" if self.minimum is None else f"{self.minimum:g}"
        high = "unbounded" if self.maximum is None else f"{self.maximum:g}"
        return f"{low}..{high}"

    def exceeded_by(self, low: float, high: float) -> bool:
        """Whether an observed ``low..high`` span falls outside this bound."""
        if self.minimum is not None and low < self.minimum:
            return True
        if self.maximum is not None and high > self.maximum:
            return True
        return False


#: Keyed by the channel label the device writes. Channels absent from this
#: table are simply not checked — an unlisted channel is not an error, and
#: inventing a bound for one would be exactly the guessing this package avoids.
#:
#: The table is short on purpose. Flow, volume, minute volume, breath rate and
#: pulse rate all used to appear here; none of them had a published upper bound
#: behind the number, so all of them were removed. ``docs/device-reference.md``
#: records what was removed and why, so the absence is a documented decision
#: rather than an oversight.
BOUNDS: dict[str, PhysicalBound] = {
    # -- measured pressure --------------------------------------------------
    # The one genuinely published ceiling on a measured channel: the device is
    # specified not to exceed 60 hPa even under a fault. It bounds the pressure
    # delivered to the patient, which is the channel named here and no other.
    # The internal debug pressure sensors are deliberately absent: the manual's
    # figure is about the device's output, and nothing published says what an
    # upstream valve-control sensor may read.
    "Airway Pressure": PhysicalBound(
        "Airway Pressure", None, 60.0, "hPa", CAPABILITY,
        "maximum pressure under a fault condition is stated as below 60 hPa "
        "(see docs/device-reference.md, 'Maximum pressure under fault'). The "
        "looser fault figure is used rather than the 50 hPa therapy maximum so "
        "a fault condition is not reported as a decode error. No floor: no "
        "published figure states how far below zero this sensor may read",
    ),

    # -- settings -----------------------------------------------------------
    # These report what is dialled in, so a settable range really does bound
    # them — at its published top end only. The old floors of zero came from
    # noticing that the channels read zero outside therapy, which is an
    # observation of a recording rather than a published figure.
    "IPAPsoll": PhysicalBound(
        "IPAPsoll", None, 50.0, "hPa", SETTABLE,
        "IPAP is settable up to 50 hPa (docs/device-reference.md, 'IPAP "
        "range'). The published minimum of 4 hPa is not applied: it bounds "
        "what may be set during therapy and not what the channel reports at "
        "any other moment",
    ),
    "EPAPsoll": PhysicalBound(
        "EPAPsoll", None, 25.0, "hPa", SETTABLE,
        "PEEP is settable up to 25 hPa on both circuit types "
        "(docs/device-reference.md, 'PEEP range'). No floor: the published "
        "minimum differs between circuit types and bounds a setting in "
        "therapy rather than the channel at large",
    ),
    "Trigger Level": PhysicalBound(
        "Trigger Level", None, 8.0, "", SETTABLE,
        "the inspiratory trigger runs to level 8 (docs/device-reference.md, "
        "'Inspiratory trigger'). No floor: level 1 is the lowest selectable "
        "trigger, not the lowest number this channel may carry",
    ),

    # -- percentages --------------------------------------------------------
    # Bounded by the unit the file itself declares rather than by the manual,
    # which makes these the only two-sided bounds in the table and the tightest
    # honest ones. A channel declared in per cent that decodes outside 0..100
    # is being read wrongly whatever it measures.
    "SpO2 Value": PhysicalBound(
        "SpO2 Value", 0.0, 100.0, "%", ARITHMETIC,
        "a proportion expressed in per cent, so 0 to 100 by definition of the "
        "unit the file declares",
    ),
    "SpO2 Sig. Qual.": PhysicalBound(
        "SpO2 Sig. Qual.", 0.0, 100.0, "%", ARITHMETIC,
        "a proportion expressed in per cent, so 0 to 100 by definition of the "
        "unit the file declares",
    ),
    "Part Inspiration": PhysicalBound(
        "Part Inspiration", 0.0, 100.0, "%", ARITHMETIC,
        "a proportion expressed in per cent, so 0 to 100 by definition of the "
        "unit the file declares",
    ),
    "Part Spont Insp.": PhysicalBound(
        "Part Spont Insp.", 0.0, 100.0, "%", ARITHMETIC,
        "a proportion expressed in per cent, so 0 to 100 by definition of the "
        "unit the file declares",
    ),
    "Part Spont Exp.": PhysicalBound(
        "Part Spont Exp.", 0.0, 100.0, "%", ARITHMETIC,
        "a proportion expressed in per cent, so 0 to 100 by definition of the "
        "unit the file declares",
    ),
}


#: Channels a previous version of this table bounded, and the reason each
#: number was withdrawn. Kept in the code rather than only in a document
#: because the temptation to reinstate one of them is real, and the answer
#: needs to be where the table is.
WITHDRAWN_BOUNDS: dict[str, str] = {
    "Patient Flow": (
        "bounded at +/-400 l/min from a published maximum flow of *above* "
        "220 l/min. That figure is a guaranteed minimum capability, so it "
        "cannot yield a ceiling in either direction"
    ),
    "Patient Flow Dbg": "the same measurement on a debug channel, same reason",
    "Leakage Flow": "derived from the same withdrawn flow figure",
    "TotalLeakage": "derived from the same withdrawn flow figure",
    "Tidal Volume": (
        "bounded at 15 l from 220 l/min sustained for 4 s. Both the flow term "
        "and therefore the product are lower bounds, not upper ones"
    ),
    "Online Volume": "derived from the same withdrawn volume figure",
    "Minute Volume": (
        "bounded from the flow the blower sustains, which is the same "
        "withdrawn lower bound. The manual's 0 to 99 l/min is a display range, "
        "which bounds what the screen can show and not what may be measured"
    ),
    "Frequency": (
        "bounded at 300/min by an assertion about sustainable human breathing. "
        "No cited source supports it, and the manual's 0 to 60 is the settable "
        "and displayed range rather than a measurement limit"
    ),
    "Pulse Rate": (
        "bounded at 300/min by an assertion about human physiology, with no "
        "cited source and no published range for the channel at all"
    ),
    "P main 2 Dbg": (
        "given the device's 60 hPa fault ceiling by analogy. That figure is "
        "published for the pressure delivered to the patient; nothing states "
        "what an internal sensor may read"
    ),
    "P valve ctrl Dbg": "same reason as P main 2 Dbg",
    "P valve air Dbg": "same reason as P main 2 Dbg",
}


def bound_for(label: str) -> PhysicalBound | None:
    """The published bound for a channel, or ``None`` if there is none.

    Absence means *not checked*, never *unbounded*. Most channels are absent:
    see :data:`WITHDRAWN_BOUNDS` for the ones that used to be here.
    """
    return BOUNDS.get(label)
