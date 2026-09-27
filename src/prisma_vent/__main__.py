"""Allow ``python -m prisma_vent`` to reach the same command as ``prisma-vent``.

Present so the package has one behaviour whether it is invoked by its
installed script or as a module — a package that runs one way and errors the
other is a small trap for no benefit.
"""

import sys

from .cli import main

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
