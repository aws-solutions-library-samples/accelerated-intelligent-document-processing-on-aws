# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Python the host-side story tools need, said before an import can fail.

``stories.py``, ``changelog_select.py``, ``story_verdict.py``, ``story_report.py``
and ``scripts/sdlc/ai_ux_test_stories.py`` run on a developer's host, where
``python3`` can be older than they are written for; macOS's ``/usr/bin/python3``
is. Each calls :func:`require` before it imports anything that needs a newer
Python, so an interpreter that can still parse them (3.9 can) ends with one
``FAILED:`` line rather than a traceback. ``docs_index.py`` runs on 3.9, so it
does not call it, and ``run_in_docker.py`` carries its own, stricter floor.
"""

import platform
import sys
from pathlib import Path

MINIMUM = (3, 11)


def require(script: str) -> None:
    """Exit with one ``FAILED:`` line when this Python is older than :data:`MINIMUM`."""
    if tuple(sys.version_info[:2]) >= MINIMUM:
        return
    shown = ".".join(str(part) for part in MINIMUM)
    raise SystemExit(
        f"FAILED: {Path(script).name} needs Python {shown} or newer, and this is "
        f"{platform.python_version()}: run it with a newer one (PYTHON=python3.12 "
        "on a make command line, py on Windows)"
    )
