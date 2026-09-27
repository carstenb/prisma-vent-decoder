"""Reader for ``.tc`` trend curve files.

These matter because they reach further back than the daily archives do: a card
carries trend curves for days whose archives the device has already dropped.

**What is established.** A file is a JSON header followed by a body of 9-byte
records. Records are either all-zero or populated, and each populated record
stands for two minutes of therapy. That interval was established by comparing
days that carry both a trend curve and a day archive, where therapy time can be
summed independently from the signal files' own record counts.

**What is not.** The nine bytes themselves are not decoded. Two of the nine are
zero throughout and two more are near-constant with the high bit set, which
looks like packed fields rather than integers; correlating them against
two-minute means from matching archives produced something short of a mapping.
So this module hands the record bytes back untouched and derives nothing from
them.

**A trend curve is not an archive, and this module will not pretend otherwise.**
The therapy time it yields is an *estimate*, while an archive's is a count.
Where both exist for a day they are two readings of different provenance, not
one value in two qualities, and deciding which to prefer belongs to whatever
consumes them — not here.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator

from .archive import AdapterVersion, Provenance, _sha256_of_file

__all__ = [
    "TrendCurveError",
    "TrendRecord",
    "TrendCurve",
    "read_trend_curve",
    "RECORD_BYTES",
    "RECORD_INTERVAL",
]


class TrendCurveError(ValueError):
    """The file is not the trend curve format this decoder was written for."""


#: Every trend curve file divides by this with no remainder.
RECORD_BYTES = 9

#: Therapy time one populated record stands for. Established by comparing days
#: that carry both a trend curve and a day archive, rather than assumed from
#: the file alone.
RECORD_INTERVAL = timedelta(seconds=120)


#: The device type the header names. Same designation the manufacturer's
#: parameter-set library carries. A different one is a different format.
EXPECTED_TYPE = "P32"

_FILE_NAME_RE = re.compile(r"^(\d{4})_(\d{4})-(\d{2})-(\d{2})\.tc$")
_HEADER_DAY_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")

#: Generous, and bounded before anything is read. A trend curve file
#: is a few kilobytes.
MAX_FILE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class TrendRecord:
    """One 9-byte record, handed back exactly as it was stored.

    ``raw`` is deliberately not interpreted. The layout is unknown, and a
    plausible-looking guess at it is the failure this project exists to avoid.
    """

    index: int
    raw: bytes

    @property
    def populated(self) -> bool:
        """Whether the record holds anything at all.

        The all-zero records are padding; only populated ones correspond to
        therapy.
        """
        return any(self.raw)


@dataclass(frozen=True)
class TrendCurve:
    """One day's trend curve."""

    day: date
    day_number: int
    device_type: str
    device_id: str
    #: The header's ``Offset`` field, kept verbatim. Its meaning is unknown: it
    #: varies per file and matches neither the header length nor the record
    #: count, so it is carried rather than interpreted.
    offset_field: str
    #: The device serial from the header. Identifying — never print it.
    serial: str
    records: tuple[TrendRecord, ...]
    provenance: Provenance

    @property
    def populated_records(self) -> int:
        return sum(1 for record in self.records if record.populated)

    @property
    def estimated_therapy_time(self) -> timedelta:
        """Therapy time for the day, **estimated** from the record count.

        An archive, where one exists, counts this exactly from its signal
        files. This is the estimate for days where no archive does.

        **How close the two come is deliberately not quantified here.** Any
        figure would be a measurement of particular recordings rather than a
        property of the format, and this package does not publish those.
        The two are readings of different provenance and should not be merged
        into one figure.
        """
        return self.populated_records * RECORD_INTERVAL


def read_trend_curve(path: str | Path) -> TrendCurve:
    """Read one ``.tc`` file. Read-only; nothing is written."""
    path = Path(path)
    day, day_number = _parse_file_name(path.name)

    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise TrendCurveError(
            f"{path.name} is {size} bytes, over the {MAX_FILE_BYTES}-byte cap"
        )

    data = path.read_bytes()
    header, body = _split(data, path.name)

    header_day = _parse_header_day(header, path.name)
    if header_day != day:
        raise TrendCurveError(
            f"{path.name} declares {header_day.isoformat()} in its header but is "
            f"named for {day.isoformat()}. Either the file was renamed or it was "
            "filed under the wrong day; both would attach it to the wrong date."
        )

    device_type = str(header.get("Type", ""))
    if device_type != EXPECTED_TYPE:
        raise TrendCurveError(
            f"{path.name} declares device type {device_type!r}, not "
            f"{EXPECTED_TYPE!r}. The record layout is established for that type "
            "only, and reading another as if it matched is how a different "
            "format gets decoded as this one."
        )

    if len(body) % RECORD_BYTES:
        raise TrendCurveError(
            f"{path.name} has a {len(body)}-byte body, which is not a whole "
            f"number of {RECORD_BYTES}-byte records"
        )

    records = tuple(
        TrendRecord(index=i, raw=body[i * RECORD_BYTES : (i + 1) * RECORD_BYTES])
        for i in range(len(body) // RECORD_BYTES)
    )

    return TrendCurve(
        day=day,
        day_number=day_number,
        device_type=device_type,
        device_id=str(header.get("devid", "")),
        offset_field=str(header.get("Offset", "")),
        serial=str(header.get("SN", "")),
        records=records,
        provenance=Provenance(
            archive_sha256=_sha256_of_file(path),
            archive_name=path.name,
            member_name=path.name,
            session_number=None,
            adapter=AdapterVersion.current(),
        ),
    )


@contextmanager
def open_trend_curves(paths: list[Path]) -> Iterator[list[TrendCurve]]:
    """Convenience for reading several files; present for symmetry with archives."""
    yield [read_trend_curve(p) for p in paths]


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _split(data: bytes, name: str) -> tuple[dict, bytes]:
    """Separate the JSON header from the binary body.

    The header is the first JSON object; the body follows immediately. The
    brace is located rather than assumed at a fixed offset, because the header
    length varies with the values it carries.
    """
    end = data.find(b"}")
    if not data.startswith(b"{") or end == -1:
        raise TrendCurveError(f"{name} does not begin with a JSON header")
    try:
        header = json.loads(data[: end + 1])
    except json.JSONDecodeError as exc:
        raise TrendCurveError(f"{name} has an unreadable JSON header: {exc}") from exc
    if not isinstance(header, dict):
        raise TrendCurveError(f"{name} header is not an object")
    return header, data[end + 1 :]


def _parse_file_name(name: str) -> tuple[date, int]:
    match = _FILE_NAME_RE.match(name)
    if match is None:
        raise TrendCurveError(
            f"{name!r} is not named NNNN_YYYY-MM-DD.tc; the day counter and the "
            "date are both part of the file's identity"
        )
    number, year, month, day = match.groups()
    try:
        return date(int(year), int(month), int(day)), int(number)
    except ValueError as exc:
        raise TrendCurveError(f"{name!r} carries an impossible date") from exc


def _parse_header_day(header: dict, name: str) -> date:
    raw = str(header.get("Day", ""))
    match = _HEADER_DAY_RE.match(raw)
    if match is None:
        raise TrendCurveError(
            f"{name} header day {raw!r} is not exactly dd.mm.yyyy"
        )
    day, month, year = (int(p) for p in match.groups())
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise TrendCurveError(f"{name} header day {raw!r} is not a real date") from exc
