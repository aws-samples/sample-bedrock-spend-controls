#!/usr/bin/env python3
"""Configuration wizard for Bedrock Spend Controls.

    python setup.py --help

Thin entry point: the implementation is the ``tools.wizard`` package. Run it
from the repository root with the CDK Python environment active (the wizard
imports ``cdk/stacks/configuration.py`` so its validation matches ``cdk synth``).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.wizard import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
