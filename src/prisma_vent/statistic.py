"""Reader for ``statistic.proto``, the device's own long-term usage record.

This file reaches **much further back than anything else a card holds**. Day
archives and trend curves each cover a few weeks; this one carries a rolling
year, one record per session. For a device whose internal buffer holds fourteen
days, that is the difference between a month of history and a year of it.

It had been written off in this project's notes as "a serialised payload
without a schema", and therefore expensive. That was wrong: the protobuf wire
format is self-delimiting, so the structure can be walked without a schema.
See :mod:`prisma_vent.protobuf`.

What is established
-------------------

Established against recordings, and enforced here rather than assumed:

* **One record per session.** A record's timestamp matches an archive session's
  start, to the second.
* **The timestamp is a count of seconds that runs 12 hours behind Unix epoch
  time** — exactly 43200 s. Twelve hours is also where this device's therapy
  day begins, which is suggestive but not demonstrated.
* **Field 2 is the session's duration in whole minutes**, truncated rather than
  rounded.
* **Every total is broken down twice, and both breakdowns sum to it.** Three
  values per record correspond to the device's three program slots and eleven
  to something with eleven categories. A breakdown is a partition of its total,
  so this module treats all four sum identities as a contract and raises when
  one fails.
* **A file-level counter holds lifetime therapy time in minutes.** Each archive
  carries its own copy of this file, so a card gives one reading per therapy
  day, and consecutive readings can be checked against that day's session
  durations truncated to whole minutes. This module exposes both figures; it
  does not record what any particular card's comparison produced.

What is not
-----------

* **The eleven categories are not named**, and no source gives an ordering for
  eleven positions. Anything that would name them here would be an inference
  from a coincidence, and a wrong ordering labels a therapy mode incorrectly
  while looking entirely reasonable. Indices are exposed; names are not.
* **The second quantity, field 3, is unnamed.** The format constrains it — it
  does not exceed the duration, and the reader enforces that — but nothing
  states what it measures, and no rule that would explain how it relates to
  the duration has been found. It is reported as an unnamed quantity rather
  than guessed at.
* **The per-program histograms are handed back raw.** Each record carries three
  blocks — one per program slot — of 553 numbers across 28 fields with fixed
  cardinalities. They are plainly distributions of something. Which
  distribution is which is unknown, so nothing is derived from them.
* **The timestamp does not settle the device's timezone.** It looked like it
  would, being absolute. But it is 12 hours off Unix time and tracks the
  device's own local clock, so it is a local-time counter in epoch clothing.
  It carries no zone information at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .protobuf import ProtobufError, collect, walk
from .timebase import DeviceLocalTime

__all__ = [
    "StatisticError",
    "PROGRAM_SLOTS",
    "CATEGORY_COUNT",
    "EPOCH_OFFSET",
    "UsageRecord",
    "DeviceConfiguration",
    "StatisticFile",
    "read_statistic",
]


class StatisticError(ValueError):
    """``statistic.proto`` is not shaped the way this reader requires."""


#: The device has three program slots. Confirmed independently by the embedded
#: configuration, which lists every therapy parameter as a three-element array,
#: and by ``ProgramEnabled1``/``2``/``3``.
PROGRAM_SLOTS = 3

#: The second breakdown has eleven categories. **Deliberately not named.** The
#: manual lists eleven ventilation modes, which is suggestive and not proof.
CATEGORY_COUNT = 11

#: Record timestamps run exactly this far behind Unix epoch time. The offset
#: is exact rather than approximate, which is what identifies it as a fixed
#: encoding rather than a timezone. It is applied as an offset and not treated
#: as a zone: the result is the device's own local clock, which has no
#: established relationship to UTC.
EPOCH_OFFSET = timedelta(hours=12)

#: A device that runs for years still writes a bounded file, a few megabytes at
#: most. The cap exists so a corrupt length field cannot make this reader
#: allocate without limit.
MAX_FILE_BYTES = 64 * 1024 * 1024

#: A record is a few kilobytes. The cap leaves room for a firmware that records
#: more, while still refusing something absurd.
MAX_RECORD_BYTES = 40 * 1024

#: Guards the same way for the embedded configuration document.
MAX_CONFIGURATION_BYTES = 4 * 1024 * 1024

#: A year of sessions is roughly a thousand records. The cap is far above any
#: plausible device lifetime at that rate.
MAX_RECORDS = 200_000

_UNIX_EPOCH = datetime(1970, 1, 1)


@dataclass(frozen=True)
class UsageRecord:
    """One session as the device's own long-term statistics recorded it."""

    #: When the session started, on the device's own clock. Derived by adding
    #: :data:`EPOCH_OFFSET` to the stored counter.
    start: DeviceLocalTime

    #: The counter exactly as stored, before the offset. Kept so a wrong time
    #: can be attributed to the offset rather than to the decoding, the same
    #: reason raw and physical sample values are both kept elsewhere.
    raw_timestamp: int

    #: Session duration in whole minutes, as the device truncated it.
    duration_minutes: int

    #: A second quantity with the same breakdowns. **Not identified.** It never
    #: exceeds :attr:`duration_minutes`.
    second_quantity: int

    #: :data:`PROGRAM_SLOTS` values summing to :attr:`duration_minutes`.
    duration_by_program: tuple[int, ...]

    #: :data:`CATEGORY_COUNT` values summing to :attr:`duration_minutes`. The
    #: categories are **not named**.
    duration_by_category: tuple[int, ...]

    #: The same two breakdowns of :attr:`second_quantity`.
    second_by_program: tuple[int, ...]
    second_by_category: tuple[int, ...]

    #: One raw block per program slot, exactly as stored. Kept unparsed
    #: because parsing them is most of the cost of reading this file and
    #: almost nothing needs them — see :meth:`histograms`.
    histogram_blocks: tuple[bytes, ...] = ()

    def histograms(self) -> tuple[dict[int, tuple[int, ...]], ...]:
        """Parse the per-program blocks into field number to values.

        **A method rather than an attribute, because it is expensive.** These
        blocks hold about 1.5 million values per file and parsing them takes
        some 2.7 seconds against 0.05 for everything else — so a caller that
        wants session times and durations, which is every caller so far, would
        otherwise pay fifty times over for data it never looks at.

        The contents are unidentified distributions. Nothing is derived from
        them here; they are returned as stored.
        """
        blocks = []
        for blob in self.histogram_blocks:
            block: dict[int, list[int]] = {}
            for field_ in walk(blob):
                if isinstance(field_.value, int):
                    block.setdefault(field_.number, []).append(field_.value)
            blocks.append({k: tuple(v) for k, v in block.items()})
        return tuple(blocks)

    @property
    def active_programs(self) -> tuple[int, ...]:
        """Zero-based slots that account for any of this session's time.

        Zero-based to match the device's own ``ActiveProgram``, which the
        embedded configuration reports as ``0`` while the device's display
        counts from one.
        """
        return tuple(i for i, v in enumerate(self.duration_by_program) if v)

    @property
    def active_categories(self) -> tuple[int, ...]:
        """Indices of categories accounting for any time. **Unnamed.**"""
        return tuple(i for i, v in enumerate(self.duration_by_category) if v)


@dataclass(frozen=True)
class DeviceConfiguration:
    """The named settings snapshot embedded alongside the records.

    Unlike ``parameter.xml``, which is keyed by numeric id, this is keyed by
    name, so the two describe the same settings in two different formats and
    can be compared. That makes it the best available independent check on the
    parameter decoding: a disagreement would point at the block detection, the
    id-to-name map or the program ordering. This class exposes the named side
    so a caller can run the comparison; what any particular card's comparison
    produced is not recorded here.

    It does **not** settle the scales. Both sources state the same integers and
    neither says where the decimal point belongs.
    """

    #: Device-level settings, by name.
    device: dict[str, Any]

    #: Therapy settings, by name; each value has one entry per program slot.
    therapy: dict[str, list[Any]]

    #: Two short strings stored beside the document. The second looks like a
    #: firmware version and is **inferred from shape and position**, not stated
    #: anywhere as such.
    labels: tuple[str, ...]

    #: The document as parsed, so nothing this class does not model is lost.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def probable_firmware_version(self) -> str | None:
        """The label that looks like a firmware version, or ``None``.

        Named "probable" because nothing in the file identifies it. Do not
        display it as the firmware version without saying it is inferred.
        """
        for label in self.labels:
            parts = label.split(".")
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                return label
        return None


@dataclass(frozen=True)
class StatisticFile:
    """A parsed ``statistic.proto``."""

    format_version: str
    records: tuple[UsageRecord, ...]
    configuration: DeviceConfiguration | None

    #: **Confirmed: the device's lifetime therapy time in minutes.** Each
    #: archive on a card carries its own copy of this file, which gives one
    #: reading per therapy day. Between consecutive days the increase equals
    #: the sum of that day's session durations **truncated to whole minutes**,
    #: exactly, with no rounding slack. The truncation is per session, the same
    #: one :attr:`UsageRecord.duration_minutes` shows.
    cumulative_therapy_minutes: int | None = None

    #: A second lifetime counter, **not identified**. It grows faster than the
    #: therapy counter, at a rate that would read as most of a day if the unit
    #: were minutes — plausible for time powered on, and that is a guess rather
    #: than a finding. No independent source measures it, so unlike the counter
    #: above it cannot be checked.
    unidentified_total: int | None = None

    @property
    def covered_span(self) -> timedelta | None:
        """How far back the records reach. ``None`` when there are none."""
        if not self.records:
            return None
        times = [r.start.value for r in self.records]
        return max(times) - min(times)


def _require_count(values: list[int], expected: int, label: str) -> tuple[int, ...]:
    if len(values) != expected:
        raise StatisticError(
            f"{label}: expected {expected} values, found {len(values)}"
        )
    return tuple(values)


def _require_sum(parts: tuple[int, ...], total: int, label: str) -> None:
    """Enforce a breakdown against its total.

    The breakdown is a property of the format: the parts are the total,
    partitioned. That makes it a contract rather than a heuristic. A file where
    it fails is not being read the way it was written, and continuing would
    produce plausible numbers from a wrong reading.
    """
    actual = sum(parts)
    if actual != total:
        raise StatisticError(
            f"{label}: parts sum to {actual} but the total is {total}. The "
            f"breakdown does not belong to this total, so the record is not "
            f"being read as it was written"
        )


def _non_negative(values: tuple[int, ...], label: str) -> None:
    for value in values:
        if value < 0:
            raise StatisticError(f"{label}: negative value {value}")


def _read_record(data: bytes) -> UsageRecord:
    if len(data) > MAX_RECORD_BYTES:
        raise StatisticError(
            f"a record is {len(data)} bytes, over the {MAX_RECORD_BYTES}-byte cap"
        )
    grouped = collect(data)

    required = (1, 2, 3, 4, 5, 6, 7)
    missing = [n for n in required if n not in grouped]
    if missing:
        raise StatisticError(f"record is missing field(s) {missing}")

    def varints(number: int) -> list[int]:
        return [f.as_int for f in grouped[number]]

    stamps = varints(1)
    if len(stamps) != 1:
        raise StatisticError(f"record has {len(stamps)} timestamps, expected one")
    raw_timestamp = stamps[0]

    totals = varints(2)
    seconds = varints(3)
    if len(totals) != 1 or len(seconds) != 1:
        raise StatisticError("record has more than one total")
    duration, second = totals[0], seconds[0]
    if duration < 0 or second < 0:
        raise StatisticError("record has a negative total")
    if second > duration:
        raise StatisticError(
            f"the second quantity ({second}) exceeds the duration ({duration}), "
            f"which the format does not allow, so the fields are not being read "
            f"as written"
        )

    by_program = _require_count(varints(4), PROGRAM_SLOTS, "duration by program")
    by_category = _require_count(varints(5), CATEGORY_COUNT, "duration by category")
    second_program = _require_count(varints(6), PROGRAM_SLOTS, "second by program")
    second_category = _require_count(
        varints(7), CATEGORY_COUNT, "second by category"
    )

    for parts, label in (
        (by_program, "duration by program"),
        (by_category, "duration by category"),
        (second_program, "second by program"),
        (second_category, "second by category"),
    ):
        _non_negative(parts, label)

    _require_sum(by_program, duration, "duration by program")
    _require_sum(by_category, duration, "duration by category")
    _require_sum(second_program, second, "second by program")
    _require_sum(second_category, second, "second by category")

    histogram_blocks = tuple(blob.as_bytes for blob in grouped.get(8, []))

    try:
        moment = _UNIX_EPOCH + timedelta(seconds=raw_timestamp) + EPOCH_OFFSET
    except (OverflowError, OSError) as exc:
        raise StatisticError(
            f"timestamp {raw_timestamp} is not a representable date"
        ) from exc

    return UsageRecord(
        start=DeviceLocalTime(moment),
        raw_timestamp=raw_timestamp,
        duration_minutes=duration,
        second_quantity=second,
        duration_by_program=by_program,
        duration_by_category=by_category,
        second_by_program=second_program,
        second_by_category=second_category,
        histogram_blocks=histogram_blocks,
    )


def _read_configuration(data: bytes) -> DeviceConfiguration:
    if len(data) > MAX_CONFIGURATION_BYTES:
        raise StatisticError(
            f"the configuration block is {len(data)} bytes, over the "
            f"{MAX_CONFIGURATION_BYTES}-byte cap"
        )
    parts = [f for f in walk(data)]
    documents = [p.as_bytes for p in parts if p.wire_type == 2]
    if not documents:
        raise StatisticError("the configuration block holds no document")

    try:
        text = documents[0].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StatisticError("the configuration document is not UTF-8") from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StatisticError(f"the configuration document is not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise StatisticError("the configuration document is not a JSON object")

    inner = parsed.get("configuration")
    if not isinstance(inner, dict):
        raise StatisticError("the configuration document has no 'configuration' object")

    device = inner.get("device")
    therapy = inner.get("therapy")
    if not isinstance(device, dict) or not isinstance(therapy, dict):
        raise StatisticError("the configuration lacks 'device' or 'therapy'")

    for name, values in therapy.items():
        if not isinstance(values, list) or len(values) != PROGRAM_SLOTS:
            raise StatisticError(
                f"therapy parameter {name!r} has "
                f"{len(values) if isinstance(values, list) else 'no'} entries, "
                f"expected one per program slot"
            )

    labels = []
    for part in documents[1:]:
        try:
            labels.append(part.decode("ascii"))
        except UnicodeDecodeError:
            continue

    return DeviceConfiguration(
        device=dict(device),
        therapy={k: list(v) for k, v in therapy.items()},
        labels=tuple(labels),
        raw=parsed,
    )


def read_statistic(data: bytes) -> StatisticFile:
    """Parse ``statistic.proto``. Read-only; nothing is written.

    Takes the member's bytes rather than a path, because the file lives inside
    a day archive and this package reads archive members as streams rather than
    extracting them.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise StatisticError(f"expected bytes, got {type(data).__name__}")
    if len(data) > MAX_FILE_BYTES:
        raise StatisticError(
            f"the file is {len(data)} bytes, over the {MAX_FILE_BYTES}-byte cap"
        )

    try:
        grouped = collect(bytes(data))
    except ProtobufError as exc:
        raise StatisticError(f"not readable as protobuf: {exc}") from exc

    versions = grouped.get(1, [])
    if len(versions) != 1:
        raise StatisticError(
            f"expected one format-version field, found {len(versions)}"
        )
    try:
        format_version = versions[0].as_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise StatisticError("the format version is not ASCII") from exc

    raw_records = grouped.get(2, [])
    if len(raw_records) > MAX_RECORDS:
        raise StatisticError(
            f"the file holds {len(raw_records)} records, over the "
            f"{MAX_RECORDS} cap"
        )
    try:
        records = tuple(_read_record(r.as_bytes) for r in raw_records)
    except ProtobufError as exc:
        raise StatisticError(f"a record is not readable as protobuf: {exc}") from exc

    configuration = None
    blocks = grouped.get(3, [])
    if len(blocks) > 1:
        raise StatisticError(
            f"expected at most one configuration block, found {len(blocks)}"
        )
    if blocks:
        try:
            configuration = _read_configuration(blocks[0].as_bytes)
        except ProtobufError as exc:
            raise StatisticError(
                f"the configuration block is not readable as protobuf: {exc}"
            ) from exc

    def single(number: int, label: str) -> int | None:
        found = grouped.get(number, [])
        if not found:
            return None
        if len(found) > 1:
            raise StatisticError(
                f"expected at most one {label}, found {len(found)}"
            )
        value = found[0].as_int
        if value < 0:
            raise StatisticError(f"{label} is negative")
        return value

    therapy_total = single(4, "lifetime therapy counter")
    other_total = single(5, "second lifetime counter")

    if therapy_total is not None and records:
        # The lifetime counter cannot be smaller than the records it summarises.
        # Those records cover about a year while the counter covers the device's
        # whole life, so this is a weak check by design — it catches the two
        # fields being swapped or misread, not a subtle error.
        recorded = sum(r.duration_minutes for r in records)
        if therapy_total < recorded:
            raise StatisticError(
                f"the lifetime therapy counter ({therapy_total} min) is below "
                f"the {recorded} min the records themselves account for, so "
                f"the two are not what this reader takes them to be"
            )

    return StatisticFile(
        format_version=format_version,
        records=records,
        configuration=configuration,
        cumulative_therapy_minutes=therapy_total,
        unidentified_total=other_total,
    )
