"""A reader for the protobuf wire format, sufficient to walk a message.

Protocol Buffers normally need the ``.proto`` schema that produced them. They
do not need it to be *walked*: the wire format is self-delimiting, carrying a
field number and a wire type before every value and a length before every
variable-length one. What the schema supplies is the field *names* and *types*,
which this module does without — it yields numbered fields and raw values, and
leaves the meaning to the caller.

That distinction is why ``statistic.proto`` was readable at all. It had been
written off in this project's notes as needing a schema nobody has.

Only what the device's files actually use is implemented: varints, and
length-delimited, 32-bit and 64-bit fields. Groups (wire types 3 and 4) were
deprecated long before this device existed and are rejected rather than
skipped, because silently ignoring a field would be exactly the quiet data loss
this package avoids everywhere else.

This is a *structural* reader. It does not know signed zig-zag encoding from
unsigned, or a float from a fixed-width integer, because both distinctions live
in the schema. A caller that knows a field is signed must apply the zig-zag
transform itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

__all__ = [
    "ProtobufError",
    "WireType",
    "Field",
    "walk",
    "collect",
    "zigzag_decode",
]


class ProtobufError(ValueError):
    """A message that is not valid protobuf wire format, or uses no-go features."""


class WireType:
    """The wire types this reader accepts. Names follow the specification."""

    VARINT = 0
    FIXED64 = 1
    LENGTH_DELIMITED = 2
    START_GROUP = 3   # deprecated; rejected
    END_GROUP = 4     # deprecated; rejected
    FIXED32 = 5


#: A varint is capped at ten bytes: 64 bits at seven bits per byte needs ten.
#: Anything longer is malformed, and without this cap a run of 0x80 bytes would
#: spin until it fell off the end of the buffer.
_MAX_VARINT_BYTES = 10

#: Field numbers are 1 to 2**29 - 1 by specification. 19000-19999 are reserved
#: for the implementation and must not appear in a message.
_MAX_FIELD_NUMBER = (1 << 29) - 1
_RESERVED_FIELD_RANGE = range(19000, 20000)


@dataclass(frozen=True)
class Field:
    """One field as it appears on the wire.

    ``value`` is an ``int`` for varints and ``bytes`` for everything else. The
    bytes are handed over exactly as stored — a fixed-width field is not
    unpacked here, because whether it is a float, a signed or an unsigned
    integer is a schema question.
    """

    number: int
    wire_type: int
    value: int | bytes

    @property
    def as_int(self) -> int:
        if not isinstance(self.value, int):
            raise ProtobufError(
                f"field {self.number} is wire type {self.wire_type}, not a varint"
            )
        return self.value

    @property
    def as_bytes(self) -> bytes:
        if not isinstance(self.value, bytes):
            raise ProtobufError(
                f"field {self.number} is a varint, not a length-delimited field"
            )
        return self.value


def _read_varint(data: bytes, index: int) -> tuple[int, int]:
    result = 0
    shift = 0
    start = index
    while True:
        if index >= len(data):
            raise ProtobufError(
                f"varint at offset {start} runs past the end of the message"
            )
        if index - start >= _MAX_VARINT_BYTES:
            raise ProtobufError(f"varint at offset {start} is longer than 64 bits")
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, index
        shift += 7


def walk(data: bytes) -> Iterator[Field]:
    """Yield every top-level field of one protobuf message, in order.

    Repeated fields appear repeatedly, which is how they are stored. Nothing is
    merged or collapsed, because a reader cannot know from the wire alone
    whether two entries with the same number are a repeated field or a
    last-one-wins scalar.
    """
    index = 0
    end = len(data)
    while index < end:
        key, index = _read_varint(data, index)
        number, wire_type = key >> 3, key & 0x07

        if number == 0 or number > _MAX_FIELD_NUMBER:
            raise ProtobufError(f"field number {number} is outside the valid range")
        if number in _RESERVED_FIELD_RANGE:
            raise ProtobufError(
                f"field number {number} is in the range reserved by the specification"
            )

        if wire_type == WireType.VARINT:
            value, index = _read_varint(data, index)
        elif wire_type == WireType.LENGTH_DELIMITED:
            length, index = _read_varint(data, index)
            if length < 0 or index + length > end:
                raise ProtobufError(
                    f"field {number} declares {length} bytes but only "
                    f"{end - index} remain"
                )
            value = data[index : index + length]
            index += length
        elif wire_type in (WireType.FIXED32, WireType.FIXED64):
            width = 4 if wire_type == WireType.FIXED32 else 8
            if index + width > end:
                raise ProtobufError(
                    f"field {number} needs {width} bytes but only "
                    f"{end - index} remain"
                )
            value = data[index : index + width]
            index += width
        elif wire_type in (WireType.START_GROUP, WireType.END_GROUP):
            raise ProtobufError(
                f"field {number} uses deprecated group encoding, which this "
                f"reader rejects rather than skipping"
            )
        else:
            raise ProtobufError(f"field {number} has unknown wire type {wire_type}")

        yield Field(number, wire_type, value)


def collect(data: bytes) -> dict[int, list[Field]]:
    """Walk a message and group its fields by number, preserving order within.

    Convenient for a message whose shape is known, where a caller wants to
    assert cardinality rather than iterate.
    """
    grouped: dict[int, list[Field]] = {}
    for field in walk(data):
        grouped.setdefault(field.number, []).append(field)
    return grouped


def zigzag_decode(value: int) -> int:
    """Undo protobuf's signed-integer encoding.

    Provided because a caller who knows a field is ``sint32``/``sint64`` cannot
    get it right without this, and getting it wrong turns small negatives into
    large positives — a wrong number that still looks like a number.
    """
    return (value >> 1) ^ -(value & 1)
