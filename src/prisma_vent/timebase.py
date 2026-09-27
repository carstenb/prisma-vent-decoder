"""Time handling for prisma VENT50 recordings.

This module exists as a separate unit because the device's notion of time is
the single easiest way to produce output that is wrong and still looks
entirely reasonable. Three properties drive the design:

1.  Times in the XML files are **signed offsets** from a reference midnight,
    written as ``±HHHH:MM:SS.mmm``. The hour field is four digits, routinely
    exceeds 24, and can be negative.

2.  The **therapy day runs noon to noon**, not midnight to midnight. Binning a
    night by calendar date splits it across two days.

3.  The day-level and session-level files **do not share a reference point**.
    ``event.xml`` counts from midnight of the archive's own date throughout,
    passing 24 h to reach the next morning. ``event_NNNN.xml`` counts from
    midnight of the calendar day on which *that session* starts. Assuming a
    single reference places every post-midnight session exactly 24 hours
    early — which still looks like a perfectly ordinary night, on the wrong
    date.

To keep (3) visible rather than implied, the two references are produced by
two differently named functions. There is deliberately no single
``reference()`` that callers could reach for without choosing.

Timestamps are carried as :class:`DeviceLocalTime`, a wrapper that exists so
that nobody can treat a device reading as an ordinary timestamp and convert
it. Whether the device clock runs local time or UTC is **not established** —
see the project notes — so no conversion is correct yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

__all__ = [
    "TimebaseError",
    "DeviceLocalTime",
    "parse_offset",
    "format_offset",
    "day_reference",
    "session_reference",
    "therapy_day",
    "THERAPY_DAY_BOUNDARY_HOUR",
]


class TimebaseError(ValueError):
    """A time value did not match the format the device is known to write.

    Raised rather than guessed. An unparseable offset means the file is not
    what this decoder was written for, and continuing would put a made-up
    instant into the output.
    """


#: The therapy day starts at this hour and runs to the same hour next day.
#: Established from the day-level logs, whose first ``<start>`` sits at
#: ``+0012:00:00.xxx`` — an exact twelve-hour offset from the archive's own
#: reference midnight, not an approximate one.
THERAPY_DAY_BOUNDARY_HOUR = 12

# Sign is mandatory: every offset the device writes carries one, and accepting
# an unsigned value would mean accepting a format we have not seen.
_OFFSET_RE = re.compile(r"^([+-])(\d{4}):(\d{2}):(\d{2})\.(\d{3})$")


@dataclass(frozen=True, order=True)
class DeviceLocalTime:
    """An instant as the device's own clock recorded it.

    Deliberately **not** a bare :class:`datetime`. The device's timezone is
    unresolved, so any conversion — ``astimezone``, comparison against a
    zone-aware value, storage as UTC — would assert something unknown. Keeping
    the value in a distinct type means such a conversion cannot happen by
    accident; it has to be written deliberately, at which point the question
    has to be answered.
    """

    value: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.value, datetime):
            raise TimebaseError(f"expected datetime, got {type(self.value).__name__}")
        if self.value.tzinfo is not None:
            raise TimebaseError(
                "DeviceLocalTime must be naive: the device clock's zone is not "
                "established, so attaching one asserts something unknown"
            )

    def shift(self, offset: timedelta) -> "DeviceLocalTime":
        """Return this instant moved by *offset*, still device-local."""
        return DeviceLocalTime(self.value + offset)

    def __str__(self) -> str:
        return f"{self.value.isoformat(sep=' ', timespec='milliseconds')} (device clock)"


def parse_offset(text: str) -> timedelta:
    """Parse a ``±HHHH:MM:SS.mmm`` offset into a signed :class:`timedelta`.

    The hour field is four digits and is *not* bounded to 24 — values past a
    full day are normal, since a day-level log reaches the following noon at
    ``+0036:00:00``. Minutes and seconds are bounded, because a value outside
    them means the field is not what it claims to be.

    ``-0000:00:00.000`` is canonicalised to zero: a sign on zero carries no
    information, and rejecting it would fail on a value the device may
    legitimately write.
    """
    if not isinstance(text, str):
        raise TimebaseError(f"expected a string offset, got {type(text).__name__}")

    match = _OFFSET_RE.match(text)
    if match is None:
        raise TimebaseError(
            f"offset {text!r} does not match the expected ±HHHH:MM:SS.mmm form"
        )

    sign, hours, minutes, seconds, millis = match.groups()

    if int(minutes) > 59:
        raise TimebaseError(f"offset {text!r} has a minute field above 59")
    if int(seconds) > 59:
        raise TimebaseError(f"offset {text!r} has a second field above 59")

    magnitude = timedelta(
        hours=int(hours),
        minutes=int(minutes),
        seconds=int(seconds),
        milliseconds=int(millis),
    )
    return -magnitude if sign == "-" else magnitude


def format_offset(offset: timedelta) -> str:
    """Render a :class:`timedelta` back into the device's offset notation.

    Round-trips :func:`parse_offset` for every value the device can express.
    Used for diagnostics, so that a reported time can be compared against the
    file by eye without mental arithmetic.
    """
    sign = "-" if offset < timedelta(0) else "+"
    total_ms = round(abs(offset).total_seconds() * 1000)
    millis = total_ms % 1000
    total_seconds = total_ms // 1000
    seconds = total_seconds % 60
    minutes = (total_seconds // 60) % 60
    hours = total_seconds // 3600
    if hours > 9999:
        raise TimebaseError(
            f"offset of {hours} hours exceeds the four-digit field the device writes"
        )
    return f"{sign}{hours:04d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def day_reference(archive_date: date) -> DeviceLocalTime:
    """Midnight from which ``event.xml``, ``alarm.xml`` and ``parameter.xml`` count.

    These day-level files carry the archive's own date and count from its
    midnight *throughout*, crossing into the following day by counting past
    24 hours rather than by rolling over to a new date.

    Pair with :func:`session_reference`, never substitute for it.
    """
    _require_date(archive_date, "archive_date")
    return DeviceLocalTime(datetime(archive_date.year, archive_date.month, archive_date.day))


def session_reference(session_start_date: date) -> DeviceLocalTime:
    """Midnight from which ``event_NNNN.xml`` counts.

    A session file counts from midnight of the calendar day on which that
    session *starts* — which for any session beginning after midnight is a
    different day from the archive's own date. The session's start date comes
    from the paired ``.wmedf`` header, which is the reliable anchor.

    Pair with :func:`day_reference`, never substitute for it.
    """
    _require_date(session_start_date, "session_start_date")
    return DeviceLocalTime(
        datetime(session_start_date.year, session_start_date.month, session_start_date.day)
    )


def therapy_day(instant: DeviceLocalTime) -> date:
    """The therapy day an instant belongs to, using the noon-to-noon boundary.

    Therapy day *D* runs from ``D 12:00`` to ``D+1 12:00``. An instant at or
    after noon belongs to its own calendar date; anything before noon belongs
    to the previous one.
    """
    if not isinstance(instant, DeviceLocalTime):
        raise TimebaseError(
            f"expected DeviceLocalTime, got {type(instant).__name__} — a bare "
            "datetime would hide whether its zone had been assumed"
        )
    if instant.value.hour >= THERAPY_DAY_BOUNDARY_HOUR:
        return instant.value.date()
    return (instant.value - timedelta(days=1)).date()


def _require_date(value: object, name: str) -> None:
    # datetime is a subclass of date, and accepting one here would silently
    # discard its time component.
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TimebaseError(f"{name} must be a date, got {type(value).__name__}")
