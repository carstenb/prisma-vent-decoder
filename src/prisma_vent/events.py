"""Readers for the XML files in a day archive.

Five files, three shapes:

*   ``event_NNNN.xml`` — one per session, root ``<desc>``, carrying the
    session's start and stop plus its respiratory events.
*   ``event.xml`` and ``alarm.xml`` — day-level, root ``<log>``, carrying
    recording windows plus device state changes or alarms.
*   ``parameter.xml`` — day-level settings snapshots; ``parametersmap.xml`` —
    the id-to-name table for them.

Two decisions run through all of it.

**Times are returned as offsets, not as instants.** Resolving an offset needs
a reference midnight, and the day-level and session-level files do not share
one. Choosing the reference is therefore not this module's business — it
belongs where both the archive date and the session's own start date are
known, so that the choice is made once and visibly rather than five times by
accident.

**Events are never named.** No id in any of these files has been identified,
and nothing in the archive maps them. The dataclasses deliberately have no name
field: there is nothing to be tempted to fill in, and a plausible-looking
guess is exactly the failure this project exists to avoid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Mapping
from xml.etree import ElementTree

from .timebase import parse_offset

__all__ = [
    "XmlError",
    "RespEvent",
    "SessionEvents",
    "DeviceStateEvent",
    "RecordingWindow",
    "DayLog",
    "AlarmEvent",
    "AlarmLog",
    "ParameterValue",
    "TherapyProgram",
    "ParameterSnapshot",
    "ParameterLog",
    "ParameterMap",
    "read_session_events",
    "read_day_log",
    "read_alarm_log",
    "read_parameter_log",
    "read_parameter_map",
    "DEFAULT_MAX_XML_BYTES",
]


class XmlError(ValueError):
    """The XML is not the shape this decoder was written for."""


#: The XML members of a day archive are well under 100 kB. The cap exists
#: to bound work before parsing, not to accommodate growth, so it is generous
#: rather than tight while still refusing anything absurd.
DEFAULT_MAX_XML_BYTES = 8 * 1024 * 1024

_ALLOWED_ENCODINGS = {"utf-8", "ascii", "us-ascii"}

_DECL_RE = re.compile(rb"^<\?xml\s[^>]*\?>")
_ENCODING_RE = re.compile(rb"encoding\s*=\s*[\"']([^\"']+)[\"']")


def _parse_xml(data: bytes, *, max_bytes: int = DEFAULT_MAX_XML_BYTES) -> ElementTree.Element:
    """Parse *data*, accepting only the shape this device is known to write.

    The order of these checks matters. Size is bounded before any parsing
    happens. Encoding is settled before the document is searched for forbidden
    declarations, because a scan for ``DOCTYPE`` over raw bytes is defeated by
    UTF-16, where the same text is spelled with interleaved null bytes.
    """
    if len(data) > max_bytes:
        raise XmlError(f"document is {len(data)} bytes, over the {max_bytes}-byte cap")

    if data.startswith(b"\xef\xbb\xbf"):
        raise XmlError(
            "document starts with a UTF-8 byte order mark, which this "
            "device does not write"
        )
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        raise XmlError("document is UTF-16; only UTF-8 and ASCII are accepted")

    match = _DECL_RE.match(data)
    if match is None:
        # Every file this device writes has a declaration. Guessing the
        # encoding of one that does not is precisely the hole the encoding
        # rule exists to close.
        raise XmlError("document has no XML declaration")

    encoding_match = _ENCODING_RE.search(match.group(0))
    if encoding_match is None:
        raise XmlError("XML declaration does not state an encoding")
    encoding = encoding_match.group(1).decode("ascii", "replace").lower()
    if encoding not in _ALLOWED_ENCODINGS:
        raise XmlError(
            f"declared encoding {encoding!r} is not accepted; "
            f"only {sorted(_ALLOWED_ENCODINGS)} are"
        )

    # Decode with what the document declared, not with what is convenient.
    # Reading an ascii-declared document as UTF-8 would accept non-ASCII bytes
    # in a file that says it has none, which is a false declaration passing
    # unnoticed rather than a harmless leniency.
    codec = "ascii" if encoding in ("ascii", "us-ascii") else "utf-8"
    try:
        text = data.decode(codec)
    except UnicodeDecodeError as exc:
        raise XmlError(
            f"document declares {encoding} but its bytes are not valid {codec}"
        ) from exc

    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise XmlError(
            "document declares a DOCTYPE or ENTITY; this device writes neither, "
            "and entity expansion is a denial-of-service vector"
        )

    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise XmlError(f"document is not well-formed XML: {exc}") from exc


# --------------------------------------------------------------------------
# Session events — event_NNNN.xml
# --------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class RespEvent:
    """One respiratory event.

    ``seconds`` counts from the session start, as an integer, and is a
    different quantity from the ``±HHHH:MM:SS.mmm`` offsets elsewhere.

    There is no name field, and that is deliberate: no id has been identified,
    so naming one would be a guess dressed as a fact.
    """

    seconds: int
    id: int
    phase: str  # "begin" or "end"
    strength: int


@dataclass(frozen=True)
class SessionEvents:
    """The contents of one ``event_NNNN.xml``."""

    start: timedelta
    stop: timedelta
    events: tuple[RespEvent, ...]


def read_session_events(data: bytes, **kwargs) -> SessionEvents:
    root = _parse_xml(data, **kwargs)
    _require_tag(root, "desc")

    start = stop = None
    events: list[RespEvent] = []

    for child in root:
        if child.tag == "start":
            # A second one is not a correction of the first. Taking either
            # would be a choice this decoder has no grounds to make.
            if start is not None:
                raise XmlError("session event file carries more than one <start>")
            _require_leaf(child)
            _require_attrs(child, {"time"})
            start = _offset_attr(child, "time")
        elif child.tag == "stop":
            if stop is not None:
                raise XmlError("session event file carries more than one <stop>")
            _require_leaf(child)
            _require_attrs(child, {"time"})
            stop = _offset_attr(child, "time")
        elif child.tag == "RespEvent":
            _require_leaf(child)
            _require_attrs(child, {"time", "id", "event", "Strength"})
            phase = _text_attr(child, "event")
            if phase not in ("begin", "end"):
                raise XmlError(f"RespEvent event attribute is {phase!r}, expected begin or end")
            events.append(
                RespEvent(
                    seconds=_int_attr(child, "time"),
                    id=_int_attr(child, "id"),
                    phase=phase,
                    strength=_int_attr(child, "Strength"),
                )
            )
        else:
            raise XmlError(f"unexpected element <{child.tag}> in a session event file")

    if start is None or stop is None:
        raise XmlError("session event file is missing its start or stop")

    # Sorted on read. The device writes these out of chronological order, so
    # anything downstream that assumes monotonic time - a merge, a duration
    # from adjacent rows, a streaming reader - would be quietly wrong.
    events.sort(key=lambda e: (e.seconds, e.id, 0 if e.phase == "begin" else 1))
    return SessionEvents(start=start, stop=stop, events=tuple(events))


# --------------------------------------------------------------------------
# Day-level logs — event.xml and alarm.xml
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordingWindow:
    """One ``<start>``/``<stop>`` pair.

    These are the device's recording windows, **not** therapy sessions: one
    window can contain several sessions, so counting windows undercounts.
    """

    start: timedelta
    stop: timedelta | None


@dataclass(frozen=True)
class DeviceStateEvent:
    time: timedelta
    id: int
    status: bool | None


@dataclass(frozen=True)
class DayLog:
    date: date
    version: str
    windows: tuple[RecordingWindow, ...]
    events: tuple[DeviceStateEvent, ...]


@dataclass(frozen=True)
class AlarmEvent:
    time: timedelta
    id: int
    phase: str  # "begin", "end" or "reset"


@dataclass(frozen=True)
class AlarmLog:
    date: date
    version: str
    windows: tuple[RecordingWindow, ...]
    alarms: tuple[AlarmEvent, ...]


def read_day_log(data: bytes, **kwargs) -> DayLog:
    root, log_date, version = _read_log_root(data, **kwargs)
    windows: list[RecordingWindow] = []
    events: list[DeviceStateEvent] = []

    for child in root:
        if child.tag in ("start", "stop"):
            _collect_window(child, windows)
        elif child.tag == "event":
            _require_leaf(child)
            _require_attrs(child, {"time", "id"}, optional={"status"})
            events.append(
                DeviceStateEvent(
                    time=_offset_attr(child, "time"),
                    id=_int_attr(child, "id"),
                    # Absent status is normal: some ids mark a moment rather
                    # than a state, so None means "not a state change" and is
                    # not the same as False.
                    status=_optional_bool_attr(child, "status"),
                )
            )
        else:
            raise XmlError(f"unexpected element <{child.tag}> in the day log")

    events.sort(key=lambda e: (e.time, e.id))
    return DayLog(
        date=log_date, version=version, windows=tuple(windows), events=tuple(events)
    )


def read_alarm_log(data: bytes, **kwargs) -> AlarmLog:
    root, log_date, version = _read_log_root(data, **kwargs)
    windows: list[RecordingWindow] = []
    alarms: list[AlarmEvent] = []

    for child in root:
        if child.tag in ("start", "stop"):
            _collect_window(child, windows)
        elif child.tag == "alarm":
            _require_leaf(child)
            _require_attrs(child, {"time", "id", "event"})
            phase = _text_attr(child, "event")
            # "ack" is written by the device when an alarm is acknowledged.
            # It belongs in this list because the format uses it, established
            # by reading archives rather than by guessing what a device might
            # write.
            if phase not in ("begin", "end", "reset", "ack"):
                raise XmlError(f"alarm event attribute is {phase!r}")
            alarms.append(
                AlarmEvent(
                    time=_offset_attr(child, "time"),
                    id=_int_attr(child, "id"),
                    phase=phase,
                )
            )
        else:
            raise XmlError(f"unexpected element <{child.tag}> in the alarm log")

    alarms.sort(key=lambda a: (a.time, a.id))
    return AlarmLog(
        date=log_date, version=version, windows=tuple(windows), alarms=tuple(alarms)
    )


# --------------------------------------------------------------------------
# Parameters — parameter.xml and parametersmap.xml
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParameterValue:
    """One parameter entry, keeping its position as well as its id.

    Position is kept because it is what the file's layout says, and it is
    cross-checked against the explicit ``program`` attribute the device
    writes. An earlier reading of these files concluded that position was the
    *only* thing distinguishing programs; that was wrong, and the explicit
    attribute is what shows it.
    """

    position: int
    id: int
    value: str
    #: Therapy programs carry an explicit ``program`` attribute; device-level
    #: parameters do not. ``None`` therefore means "device parameter", which
    #: is a different statement from "program 0".
    program: int | None = None


@dataclass(frozen=True)
class TherapyProgram:
    position: int
    parameters: tuple[ParameterValue, ...]


@dataclass(frozen=True)
class ParameterSnapshot:
    """One full settings dump.

    ``active_program_raw`` keeps its entry rather than being reduced to a bare
    value, and is **never resolved to a program**.

    The convention itself is not in doubt: the attribute is zero-based, while
    the device's own display counts from one, so ``file program N`` is the
    display's ``program N+1``. ``docs/export-schema-v1.md`` records it, and it
    has since been confirmed against photographs of two programmes that differ
    from one another exactly as their blocks do. What is withheld here is the
    *resolution*, not the knowledge — a caller that wants the block can index
    it, and a caller that wants to show the user a program number can add one,
    but doing either inside this dataclass would bake one reading into a value
    whose provenance is the interesting part.

    **Which block is which programme and what ``active_program_raw`` means are
    two questions.** The first is settled; the second is not, because a
    programme chosen on a settings page need not be the one running therapy.
    Resolving this field would answer the second by assuming the first, which
    is why it stays unresolved even though the mapping is known.

    An earlier version of this docstring called the convention unresolved,
    which contradicted the documentation and led at least one consumer to
    record the ambiguity as still open.
    """

    time: timedelta
    device_parameters: tuple[ParameterValue, ...]
    therapy_programs: tuple[TherapyProgram, ...]
    active_program_raw: ParameterValue | None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParameterLog:
    date: date
    version: str
    config_version: str | None
    windows: tuple[RecordingWindow, ...]
    snapshots: tuple[ParameterSnapshot, ...]


@dataclass(frozen=True)
class ParameterMap:
    version: str
    names: Mapping[int, str]

    def name(self, parameter_id: int) -> str:
        try:
            return self.names[parameter_id]
        except KeyError:
            raise XmlError(
                f"no parameter named {parameter_id}. Ids from the event and alarm "
                "files share no namespace with parameters and must not be looked "
                "up here — they would resolve to a plausible but wrong name."
            ) from None


#: How many therapy programs the device exposes. Read from the data by
#: division rather than assumed, but the expected value is checked so that an
#: archive with a different structure is noticed rather than reinterpreted.
EXPECTED_THERAPY_PROGRAMS = 3

_ACTIVE_PROGRAM_NAME = "ActiveProgram"


def read_parameter_log(
    data: bytes, *, parameter_map: ParameterMap | None = None, **kwargs
) -> ParameterLog:
    root, log_date, version = _read_log_root(data, **kwargs)
    windows: list[RecordingWindow] = []
    config_version: str | None = None
    by_time: dict[timedelta, list[ParameterValue]] = {}
    order: list[timedelta] = []

    for child in root:
        if child.tag in ("start", "stop"):
            _collect_window(child, windows)
        elif child.tag == "config":
            _require_leaf(child)
            _require_attrs(child, {"version"})
            config_version = _text_attr(child, "version")
        elif child.tag == "parameter":
            _require_leaf(child)
            _require_attrs(child, {"time", "id", "value"}, optional={"program"})
            when = _offset_attr(child, "time")
            bucket = by_time.get(when)
            if bucket is None:
                bucket = by_time[when] = []
                order.append(when)
            bucket.append(
                ParameterValue(
                    position=len(bucket),
                    id=_int_attr(child, "id"),
                    value=_text_attr(child, "value"),
                    program=(
                        _int_attr(child, "program") if "program" in child.attrib else None
                    ),
                )
            )
        else:
            raise XmlError(f"unexpected element <{child.tag}> in the parameter log")

    snapshots = tuple(
        _split_snapshot(when, by_time[when], parameter_map) for when in order
    )
    return ParameterLog(
        date=log_date,
        version=version,
        config_version=config_version,
        windows=tuple(windows),
        snapshots=snapshots,
    )


def _split_snapshot(
    when: timedelta, entries: list[ParameterValue], parameter_map: ParameterMap | None
) -> ParameterSnapshot:
    """Divide one snapshot into device parameters and per-program blocks.

    The device writes an explicit ``program`` attribute on therapy parameters
    and omits it on device-level ones, so that attribute is authoritative. The
    positional rule below is kept as an independent cross-check rather than
    deleted: two groupings derived from different evidence agreeing is worth
    more than either alone, and a disagreement is worth reporting.

    The positional rule is also the fallback for a snapshot that carries no
    ``program`` attribute at all — the shape rather than the numbers: a
    leading run of ids that occur once, then a remainder dividing into equal
    blocks with identical id sequences.
    """
    if any(entry.program is not None for entry in entries):
        return _split_by_program_attribute(when, entries, parameter_map)
    return _split_by_position(when, entries, parameter_map)


def _split_by_program_attribute(
    when: timedelta, entries: list[ParameterValue], parameter_map: ParameterMap | None
) -> ParameterSnapshot:
    """Split a snapshot that declares its programs explicitly.

    Once any entry carries ``program``, the attribute governs the whole
    snapshot and the layout must agree with it. A therapy parameter whose
    attribute happened to be missing would otherwise be reported as a device
    parameter — a wrong classification a caller cannot detect, since the
    remaining programs still look identical to one another. A note is not
    enough for that; it has to fail.
    """
    notes: list[str] = []
    untagged = [e for e in entries if e.program is None]
    tagged = [e for e in entries if e.program is not None]

    # An untagged id appearing more than once is the signature of a therapy
    # parameter that lost its attribute: device parameters occur exactly once.
    seen: dict[int, int] = {}
    for entry in untagged:
        seen[entry.id] = seen.get(entry.id, 0) + 1
    repeated = sorted(i for i, n in seen.items() if n > 1)
    if repeated:
        raise XmlError(
            f"parameter id(s) {repeated} appear more than once without a program "
            "attribute. A device parameter occurs once per snapshot, so these are "
            "therapy parameters missing their attribute, and reporting them as "
            "device settings would be wrong in a way nothing downstream could see."
        )

    # The untagged entries must be exactly the leading run. Anything else means
    # the layout and the attributes disagree about what this snapshot is.
    lead = 0
    while lead < len(entries) and entries[lead].program is None:
        lead += 1
    if lead != len(untagged):
        stray = entries[lead].id if lead < len(entries) else None
        raise XmlError(
            f"parameter id {stray} carries no program attribute but follows "
            "entries that do. The device writes its device-level parameters as a "
            "single leading block; this snapshot's layout contradicts its own "
            "attributes."
        )

    if not tagged:
        return ParameterSnapshot(
            time=when,
            device_parameters=tuple(untagged),
            therapy_programs=(),
            active_program_raw=_find_active(untagged, parameter_map),
            notes=tuple(notes),
        )

    # Each program must occupy one contiguous block, in ascending order.
    numbers_in_order = [e.program for e in tagged]
    if numbers_in_order != sorted(numbers_in_order):
        raise XmlError(
            "therapy parameters are interleaved between programs rather than "
            "written as one block per program; the ordering and the attributes "
            "disagree about the snapshot's structure"
        )

    grouped: dict[int, list[ParameterValue]] = {}
    for entry in tagged:
        grouped.setdefault(entry.program, []).append(entry)  # type: ignore[arg-type]

    numbers = sorted(grouped)
    reference = [p.id for p in grouped[numbers[0]]]
    for number in numbers[1:]:
        if [p.id for p in grouped[number]] != reference:
            raise XmlError(
                f"therapy program {number} lists different parameter ids from "
                f"program {numbers[0]}, so the snapshot is not the repeating "
                "structure it declares itself to be"
            )

    # These two remain observations rather than errors: an unusual program
    # count or numbering is a statement about the device, not a contradiction
    # within the file.
    if numbers != list(range(len(numbers))):
        notes.append(f"program numbers are {numbers}, not a run from zero")
    if len(numbers) != EXPECTED_THERAPY_PROGRAMS:
        notes.append(
            f"snapshot carries {len(numbers)} program blocks, not {EXPECTED_THERAPY_PROGRAMS}"
        )
    if not untagged:
        notes.append("snapshot has no leading block of device parameters")

    programs = tuple(
        TherapyProgram(position=number, parameters=tuple(grouped[number]))
        for number in numbers
    )
    return ParameterSnapshot(
        time=when,
        device_parameters=tuple(untagged),
        therapy_programs=programs,
        active_program_raw=_find_active(untagged, parameter_map),
        notes=tuple(notes),
    )


def _split_by_position(
    when: timedelta, entries: list[ParameterValue], parameter_map: ParameterMap | None
) -> ParameterSnapshot:
    notes: list[str] = []
    counts: dict[int, int] = {}
    for entry in entries:
        counts[entry.id] = counts.get(entry.id, 0) + 1

    lead = 0
    while lead < len(entries) and counts[entries[lead].id] == 1:
        lead += 1

    device = tuple(entries[:lead])
    remainder = entries[lead:]

    if lead == 0 and remainder:
        # Structurally permitted - the block division is the rule that matters -
        # but never seen, so it is surfaced rather than passed over.
        notes.append("snapshot has no leading block of device parameters")

    if not remainder:
        return ParameterSnapshot(
            time=when,
            device_parameters=device,
            therapy_programs=(),
            active_program_raw=_find_active(device, parameter_map),
            notes=tuple(notes),
        )

    repeats = counts[remainder[0].id]
    if repeats < 2 or len(remainder) % repeats != 0:
        raise XmlError(
            f"snapshot tail of {len(remainder)} entries does not divide into "
            f"{repeats} equal program blocks"
        )

    size = len(remainder) // repeats
    blocks = [remainder[i * size : (i + 1) * size] for i in range(repeats)]
    reference = [p.id for p in blocks[0]]
    for index, block in enumerate(blocks[1:], start=1):
        if [p.id for p in block] != reference:
            raise XmlError(
                f"program block {index} lists different parameter ids from block 0, "
                "so the snapshot is not the repeating structure it appears to be"
            )

    if repeats != EXPECTED_THERAPY_PROGRAMS:
        notes.append(
            f"snapshot carries {repeats} program blocks, not {EXPECTED_THERAPY_PROGRAMS}"
        )

    programs = tuple(
        TherapyProgram(position=i, parameters=tuple(block))
        for i, block in enumerate(blocks)
    )
    return ParameterSnapshot(
        time=when,
        device_parameters=device,
        therapy_programs=programs,
        active_program_raw=_find_active(device, parameter_map),
        notes=tuple(notes),
    )


def _find_active(
    device: Iterable[ParameterValue], parameter_map: ParameterMap | None
) -> ParameterValue | None:
    if parameter_map is None:
        return None
    wanted = [i for i, n in parameter_map.names.items() if n == _ACTIVE_PROGRAM_NAME]
    if len(wanted) != 1:
        return None
    for entry in device:
        if entry.id == wanted[0]:
            return entry
    return None


def read_parameter_map(data: bytes, **kwargs) -> ParameterMap:
    root = _parse_xml(data, **kwargs)
    _require_tag(root, "parameters")
    _require_attrs(root, {"version", "count"})
    version = _text_attr(root, "version")
    declared = _int_attr(root, "count")

    names: dict[int, str] = {}
    for child in root:
        if child.tag != "parameter":
            raise XmlError(f"unexpected element <{child.tag}> in the parameter map")
        _require_leaf(child)
        _require_attrs(child, {"name", "id"})
        parameter_id = _int_attr(child, "id")
        if parameter_id in names:
            raise XmlError(f"parameter id {parameter_id} is listed twice")
        names[parameter_id] = _text_attr(child, "name")

    if len(names) != declared:
        raise XmlError(
            f"parameter map declares {declared} entries but lists {len(names)}"
        )
    return ParameterMap(version=version, names=names)


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------


def _read_log_root(data: bytes, **kwargs) -> tuple[ElementTree.Element, date, str]:
    root = _parse_xml(data, **kwargs)
    _require_tag(root, "log")
    _require_attrs(root, {"version", "date"})
    return root, _date_attr(root, "date"), _text_attr(root, "version")


def _collect_window(element: ElementTree.Element, windows: list[RecordingWindow]) -> None:
    """Pair a ``<start>`` with the ``<stop>`` that follows it.

    A window left open in the middle of a log is a structural error and is
    refused here. A window left open at the *end* is not: the card is pulled
    while the device is running, so the final day legitimately has no stop.
    """
    _require_leaf(element)
    _require_attrs(element, {"time"})
    when = _offset_attr(element, "time")
    if element.tag == "start":
        if windows and windows[-1].stop is None:
            raise XmlError("a recording window starts before the previous one stops")
        windows.append(RecordingWindow(start=when, stop=None))
    else:
        if not windows or windows[-1].stop is not None:
            raise XmlError("a recording window stops without having started")
        windows[-1] = RecordingWindow(start=windows[-1].start, stop=when)


def _require_leaf(element: ElementTree.Element) -> None:
    """Reject children and text on an element the format defines as empty.

    Every element this decoder reads carries its content in attributes. A
    child or a text body means the document is not the shape assumed, and
    ignoring it would silently discard whatever it holds.

    Whitespace is tolerated because the device indents its day-level files;
    this is not a blanket ban on text elsewhere in the document.
    """
    child = next(iter(element), None)
    if child is not None:
        raise XmlError(
            f"<{element.tag}> contains a nested <{child.tag}>; this element is "
            "defined by its attributes and carries no children"
        )
    if element.text is not None and element.text.strip():
        raise XmlError(
            f"<{element.tag}> contains text {element.text.strip()[:40]!r}; this "
            "element is defined by its attributes and carries no body"
        )


def _require_tag(element: ElementTree.Element, expected: str) -> None:
    if element.tag != expected:
        raise XmlError(f"root element is <{element.tag}>, expected <{expected}>")


def _require_attrs(
    element: ElementTree.Element, required: set[str], optional: set[str] = frozenset()
) -> None:
    present = set(element.attrib)
    missing = required - present
    if missing:
        raise XmlError(f"<{element.tag}> is missing attribute(s) {sorted(missing)}")
    unexpected = present - required - set(optional)
    if unexpected:
        raise XmlError(
            f"<{element.tag}> carries unexpected attribute(s) {sorted(unexpected)}; "
            "this decoder accepts only the known shape"
        )


def _text_attr(element: ElementTree.Element, name: str) -> str:
    return element.attrib[name]


def _int_attr(element: ElementTree.Element, name: str) -> int:
    raw = element.attrib[name]
    try:
        return int(raw)
    except ValueError as exc:
        raise XmlError(f"<{element.tag}> attribute {name}={raw!r} is not an integer") from exc


def _offset_attr(element: ElementTree.Element, name: str) -> timedelta:
    return parse_offset(element.attrib[name])


def _optional_bool_attr(element: ElementTree.Element, name: str) -> bool | None:
    raw = element.attrib.get(name)
    if raw is None:
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise XmlError(f"<{element.tag}> attribute {name}={raw!r} is not true or false")


def _date_attr(element: ElementTree.Element, name: str) -> date:
    raw = element.attrib[name]
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if match is None:
        raise XmlError(f"<{element.tag}> attribute {name}={raw!r} is not YYYY-MM-DD")
    try:
        return date(*(int(p) for p in match.groups()))
    except ValueError as exc:
        raise XmlError(f"<{element.tag}> attribute {name}={raw!r} is not a real date") from exc
