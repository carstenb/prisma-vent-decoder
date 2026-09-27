#!/usr/bin/env python3
"""Check that every Markdown link in this repository points somewhere real.

Two failures this catches, and they are different from each other.

**A relative link to a file that does not exist.** The ordinary case, and the
one a rename produces.

**A relative link in the README.** The README is this package's long
description on PyPI, where ``docs/format.md`` resolves against nothing at all
and renders as a dead link on the project page. Links out of the README must
therefore be absolute URLs into the repository — which also means a reader of
the README inside a text editor, a tarball or a documentation viewer can follow
them. Links *between* files under ``docs/`` may stay relative, because those
files are read in the repository.

No network is used. An absolute URL is checked for shape and left alone:
fetching them would make this job fail for reasons that have nothing to do
with this repository, which is how a check gets disabled.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent

#: Inline links only. Reference-style links and bare autolinks are not used in
#: this repository, and pretending to parse Markdown properly would be worse
#: than saying so.
_LINK = re.compile(r"\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")

#: The README is rendered off GitHub, so its relative links break there.
_MUST_BE_ABSOLUTE = {"README.md"}

_SCHEMES = ("http://", "https://", "mailto:")


#: Directories that hold generated files rather than this repository's own.
#: pytest writes a README.md into its cache, and checking that one would let a
#: tool's own scratch directory decide whether CI passes.
_NOT_OURS = {
    ".git",
    "dist",
    "build",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}


def _markdown_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not (_NOT_OURS & set(path.parts))
        and not any(part.startswith(".venv") for part in path.parts)
    )


def main() -> int:
    problems: list[str] = []
    checked = 0

    for path in _markdown_files():
        relative = path.relative_to(ROOT).as_posix()
        for match in _LINK.finditer(path.read_text(encoding="utf-8")):
            target = match.group("target")
            checked += 1

            if target.startswith("#"):
                continue  # an anchor within the same document
            if target.startswith(_SCHEMES):
                parts = urlsplit(target)
                if not parts.netloc and not target.startswith("mailto:"):
                    problems.append(f"{relative}: {target!r} has no host")
                continue

            if relative in _MUST_BE_ABSOLUTE:
                problems.append(
                    f"{relative}: {target!r} is relative. This file is rendered "
                    "off GitHub — as a package's long description, in a "
                    "tarball, in an editor — where a relative link resolves "
                    "against nothing. Use the full URL"
                )
                continue

            resolved = (path.parent / target.split("#", 1)[0]).resolve()
            if not resolved.exists():
                problems.append(f"{relative}: {target!r} does not exist")

    if problems:
        print(f"{len(problems)} broken or unrenderable link(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"{checked} markdown link(s) checked, all resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
