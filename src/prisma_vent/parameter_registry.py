"""What is known about converting a raw parameter, and how well.

Not a scale the decoder applies — it applies none. These tables say which
conversions are established, which rest on a single reference point, and which
parameters are not quantities at all, so that a consumer is not left to invent
the answer. One was invented downstream while this knowledge lived only in
prose, which is why it is data now.

**This module holds knowledge about the device, confirmed from outside the
files.** That is a different thing from the parsers next to it, which describe
what a file says about itself, and it is why the tables are here rather than
beside the exporter that first needed them. A second consumer has since
arrived: `validate` reads the volume-target candidate to hold a setting against
a measurement.
"""

from __future__ import annotations

#: The map and configuration versions the scale evidence was gathered against.
#: Both, not just the map: the readings were taken from one device state, and
#: binding to either alone would claim more than was observed.
#:
#: The device firmware seen alongside them was 3.9.0017. It is **not** checked
#: here — `device.xml` need not be present in an archive, and a third gate would
#: only widen the set of cases where the registries come back empty for a reason
#: nobody can explain. This comment is where that version is recorded: it is an
#: observation of one device, not a manufacturer figure, so it does not belong
#: in `docs/device-reference.md`, which is a citation list. An earlier version
#: of this comment named that file as holding it, and it never did.
SUPPORTED_PARAMETER_SCALE_VERSION = ("6.3.0", "6.3.0")

#: Evidence methods an entry may cite. Each names *how* a factor was
#: corroborated and never the value it was corroborated against.
EVIDENCE_METHODS = frozenset(
    {
        # A raw value held against a photographed device display.
        "device_display_reference",
        # A setting changed to a value known beforehand — stronger, because it
        # pins the id-to-name mapping at the same time.
        "known_setting_change",
        # Several distinct raw/display pairs giving one ratio.
        "multi_point_reference",
        # One display is the arithmetic sum of two others.
        "arithmetic_cross_check",
        # A measured channel agrees with the setting. Corroborates meaning and
        # magnitude only: a channel declares its own scale, and that says
        # nothing about how a setting of the same name is stored.
        "signal_cross_check",
    }
)

#: Confirmed conversions: several distinct raw/display pairs giving one ratio,
#: consistent with proportional scaling through the origin.
#:
#: ``physical_value = value_raw x physical_delta / raw_delta``. A ratio of two
#: integers rather than one number, so that a factor which later turns out to
#: be non-integral is not a type change; and no offset term, because these
#: describe multiplication only.
#:
#: **Nothing here is applied.** ``value_raw`` is written unchanged.
PARAMETER_SCALES: tuple[dict, ...] = (
    {
        "parameter_id": 69,
        "name": "IPAP",
        "unit": "hPa",
        "raw_delta": 100,
        "physical_delta": 1,
        "status": "confirmed",
        "evidence": ("device_display_reference", "multi_point_reference"),
    },
)

#: Observed conversions resting on a single reference point. Published so a
#: consumer is not left to invent one — silence is what produced an invented
#: factor downstream once already — and kept apart from the confirmed set so
#: that reading one cannot accidentally yield the other.
#:
#: **Every entry is written out in full, and that is deliberate.** The four
#: inspiratory-time parameters share a ratio because each has its own display
#: reading, not because their names look alike. Generating them from one
#: constant would hide four observations behind a resemblance — which is the
#: reasoning that put a third confirmed factor into this project's
#: documentation for months. The same goes for the three pressure parameters,
#: only one of which is confirmed.
PARAMETER_SCALE_CANDIDATES: tuple[dict, ...] = (
    {
        "parameter_id": 37,
        "name": "EPAP",
        "unit": "hPa",
        "raw_delta": 100,
        "physical_delta": 1,
        "status": "single_point_corroborated",
        "evidence": ("device_display_reference",),
    },
    {
        "parameter_id": 41,
        "name": "Frequency",
        "unit": "/min",
        "raw_delta": 10,
        "physical_delta": 1,
        "status": "single_point_corroborated",
        "evidence": ("device_display_reference",),
    },
    {
        "parameter_id": 93,
        "name": "Ti",
        "unit": "s",
        "raw_delta": 1000,
        "physical_delta": 1,
        "status": "single_point_corroborated",
        "evidence": ("device_display_reference",),
    },
    {
        "parameter_id": 94,
        "name": "Ti_max",
        "unit": "s",
        "raw_delta": 1000,
        "physical_delta": 1,
        "status": "single_point_corroborated",
        "evidence": ("device_display_reference",),
    },
    {
        "parameter_id": 95,
        "name": "Ti_min",
        "unit": "s",
        "raw_delta": 1000,
        "physical_delta": 1,
        "status": "single_point_corroborated",
        "evidence": ("device_display_reference",),
    },
    {
        "parameter_id": 96,
        "name": "Ti_timed",
        "unit": "s",
        "raw_delta": 1000,
        "physical_delta": 1,
        "status": "single_point_corroborated",
        "evidence": ("device_display_reference",),
    },
    {
        "parameter_id": 105,
        "name": "TriggerSensitivityExspiration",
        "unit": "%",
        "raw_delta": 1,
        "physical_delta": 1,
        "status": "single_point_corroborated",
        "evidence": ("device_display_reference",),
    },
    {
        # A measured channel agrees with the display here, and the validator
        # had already held that channel against integrated flow. Three accounts
        # of one setting value — still not a second setting value.
        "parameter_id": 111,
        "name": "VolumeTarget",
        "unit": "ml",
        "raw_delta": 1,
        "physical_delta": 1,
        "status": "single_point_corroborated",
        "evidence": ("device_display_reference", "signal_cross_check"),
    },
    {
        # The device shows an upper pressure equal to the lower plus this. That
        # establishes what the parameter means and reuses the same value, so it
        # is not a second point on its scaling line.
        "parameter_id": 113,
        "name": "VolumeTargetDeltaPressure",
        "unit": "hPa",
        "raw_delta": 100,
        "physical_delta": 1,
        "status": "single_point_corroborated",
        "evidence": ("device_display_reference",),
    },
)


#: Parameters whose values are **not quantities**: named states and steps.
#:
#: A scale would be the wrong question for these. What matters instead is that
#: a consumer does not render a step as a measurement — and that it knows the
#: raw value is shown as-is rather than translated, which the labels below say
#: by repeating it.
#:
#: ``observed_labels`` is **partial by construction and always will be.**
#: Observation can show what a value displays as; it can never show that no
#: further values exist. A device writing a value absent from this map is not
#: an error, and a consumer meets that case by showing the number.
#:
#: The labels are the device's own display text, quoted as written. They were
#: read from a device configured in German; a mode designation like ``ST`` is
#: the manufacturer's across languages, a word like ``Manuell`` is not.
PARAMETER_VALUE_DOMAINS: tuple[dict, ...] = (
    {
        "parameter_id": 39,
        "name": "ExpirationRamp",
        "kind": "ordinal",
        "observed_labels": {"2": "2"},
        "evidence": ("device_display_reference",),
    },
    {
        "parameter_id": 74,
        "name": "InspirationRamp",
        "kind": "ordinal",
        "observed_labels": {"1": "1", "4": "4"},
        "evidence": ("device_display_reference",),
    },
    {
        "parameter_id": 92,
        "name": "TherapyMode",
        "kind": "categorical",
        "observed_labels": {"2": "ST", "8": "MPVp"},
        "evidence": ("device_display_reference",),
    },
    {
        # Id 106, one away from `TriggerSensitivityExspiration` at 105, which
        # is a percentage and carries a scale candidate instead. The device
        # calls both "Inspiration" and "Exspiration" on screens that look
        # alike; the ids are the only thing that tells them apart reliably.
        "parameter_id": 106,
        "name": "TriggerSensitivityInspiration",
        "kind": "ordinal",
        "observed_labels": {"3": "3"},
        "evidence": ("device_display_reference",),
    },
    {
        "parameter_id": 107,
        "name": "TriggerType",
        "kind": "categorical",
        "observed_labels": {"0": "Manuell", "1": "Auto"},
        "evidence": ("device_display_reference",),
    },
    {
        "parameter_id": 112,
        "name": "VolumeTargetControl",
        "kind": "ordinal",
        "observed_labels": {"2": "Geschwindigkeit III"},
        "evidence": ("device_display_reference",),
    },
)


def parameter_registries(parameter_map, config_version) -> tuple[dict, dict, dict]:
    """The three parameter registries for this archive, or three empty ones.

    A factor was established against one device state. Emitting it for a map
    that only *looks* right would turn "confirmed for the version examined"
    into "confirmed wherever this name appears", across firmware nobody has
    seen. So four things must hold together: both versions match, the id is in
    the map, that id carries the expected name, and the name is unique there —
    `read_parameter_map` rejects a repeated id but not a repeated name, so two
    ids could otherwise share one.

    **Any inconsistency empties all three.** A partly trustworthy table under a
    version claimed to be known is not something anyone could act on.
    """
    everything = (
        *PARAMETER_SCALES,
        *PARAMETER_SCALE_CANDIDATES,
        *PARAMETER_VALUE_DOMAINS,
    )
    empty: tuple[dict, dict, dict] = ({}, {}, {})
    if (parameter_map.version, config_version) != SUPPORTED_PARAMETER_SCALE_VERSION:
        return empty

    names = list(parameter_map.names.values())
    for entry in everything:
        if parameter_map.names.get(entry["parameter_id"]) != entry["name"]:
            return empty
        if names.count(entry["name"]) != 1:
            return empty

    def registry(entries: tuple[dict, ...]) -> dict:
        # Sorted by name for the callers that iterate this mapping. It is not
        # what makes the exported file reproducible — the exporter serialises
        # with sorted keys, so the file would come out ordered either way. A
        # comment here once claimed otherwise, and a test written to match it
        # could not fail.
        return {
            entry["name"]: {**entry, "evidence": list(entry["evidence"])}
            for entry in sorted(entries, key=lambda e: e["name"])
        }

    return (
        registry(PARAMETER_SCALES),
        registry(PARAMETER_SCALE_CANDIDATES),
        registry(PARAMETER_VALUE_DOMAINS),
    )
