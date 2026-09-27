"""The ``prisma-vent`` command.

One installed entry point with explicit subcommands, so that a person who
installed this package has something to run — until now using the decoder
meant writing Python against its API, and the module paths it exposes carry no
compatibility guarantee.

**Exit codes are a contract.** Scripts will branch on them, so the meanings are
fixed and tested rather than incidental:

======  ======================================================================
``0``   success
``1``   structural decode or validation failure — a file was not what it claimed
``2``   command-line usage error
``3``   copy or I/O failure
``4``   a privacy or safety condition was violated
======  ======================================================================

The exact numbers matter less than their stability. What matters is that a
caller can distinguish "the data is wrong" from "you called this wrongly" from
"the disk did not cooperate", because those want different responses.

**stdout carries findings, stderr carries diagnostics.** Anything a caller
might want to read or redirect goes to stdout; messages about files that could
not be opened go to stderr. Mixing them would make the output unusable in a
pipeline, which is a promise worth keeping even while nothing yet emits
machine-readable output.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable, Sequence, TextIO

from . import __version__
from . import copy_card as copy_card_command
from . import decode as decode_command
from . import inspect as inspect_command
from . import usage as usage_command
from . import validate as validate_command

__all__ = [
    "main",
    "EXIT_OK",
    "EXIT_STRUCTURAL",
    "EXIT_USAGE",
    "EXIT_IO",
    "EXIT_SAFETY",
]

EXIT_OK = 0
EXIT_STRUCTURAL = 1
EXIT_USAGE = 2
EXIT_IO = 3
EXIT_SAFETY = 4


#: Subcommands, each contributing its own arguments and its own runner. Adding
#: one here is the only place it needs registering; the two functions come from
#: the module that implements it, so the installed command and the module entry
#: point cannot describe different interfaces.
_COMMANDS: dict[str, tuple[str, Callable, Callable]] = {
    "copy-card": (
        "copy a mounted card into a private directory, verifying every file",
        copy_card_command.add_arguments,
        copy_card_command.run,
    ),
    "decode": (
        "export archives in the documented, versioned machine-readable format",
        decode_command.add_arguments,
        decode_command.run,
    ),
    "inspect": (
        "print what is in a day archive or trend curve, changing nothing",
        inspect_command.add_arguments,
        inspect_command.run,
    ),
    "usage": (
        "therapy time per day or month, from the device's year-long record",
        usage_command.add_arguments,
        usage_command.run,
    ),
    "validate": (
        "cross-check decoded archives against themselves",
        validate_command.add_arguments,
        validate_command.run,
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prisma-vent",
        description=(
            "Read personal prisma VENT50 data. Read-only: nothing this command "
            "does writes to the files it is pointed at. Not medical software."
        ),
        epilog=(
            "Exit codes: 0 success, 1 structural failure, 2 usage error, "
            "3 I/O failure, 4 safety condition violated."
        ),
    )
    parser.add_argument("--version", action="version", version=f"prisma-vent {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, (help_text, add_arguments, _) in _COMMANDS.items():
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        add_arguments(sub)
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        # A bare invocation is a usage error, not a silent success: the caller
        # asked for nothing and got nothing, and a zero would say otherwise.
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    if getattr(args, "samples", 0) < 0:
        parser.error("--samples must not be negative")  # exits with EXIT_USAGE

    _, _, run = _COMMANDS[args.command]
    return run(args, out if out is not None else sys.stdout)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
