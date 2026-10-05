"""Interactive configuration wizard for Bedrock Spend Controls.

``python setup.py`` (from the repository root) asks the deployment questions
in the order of ``cdk/stacks/configuration.KEY_DOCS``, validates every answer
with the ``validate_mapping`` that ``cdk synth`` uses, optionally runs the
``tools.preflight`` checks live against the target account while asking, and
writes ``cdk/config/<name>.local.json`` (plus ``workloads.json``). ``--answers``
with ``--yes`` replays a saved session without prompts; ``--deploy`` hands
the written file to ``install.sh``.

Modules: ``cli`` (arguments and orchestration), ``flow`` (sections, keys,
validation), ``console`` (``input()``-based prompts), ``live`` (preflight
checks and Bedrock lookups), ``summary`` (values, cost, tasks for other
teams), ``deploy`` (preflight and ``install.sh``).
"""

from .cli import EXIT_FAILED, EXIT_OK, EXIT_USAGE, build_parser, main

__all__ = ["EXIT_FAILED", "EXIT_OK", "EXIT_USAGE", "build_parser", "main"]
