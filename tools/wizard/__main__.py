"""Entry point for ``python -m tools.wizard`` and ``python tools/wizard``.

When the directory is executed directly, Python puts ``tools/wizard`` on
``sys.path`` instead of the repository root, so the root is added here
before the package import.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.wizard.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
