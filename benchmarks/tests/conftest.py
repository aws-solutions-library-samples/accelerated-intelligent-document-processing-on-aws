# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Make this directory's suites reachable from any working directory (GitHub #1079).

pytest imports the ``conftest.py`` of a test file's directory before the test module
itself, from every invocation that collects these files. So this is the one place that
can put both ``benchmarks/harness`` and this directory on ``sys.path`` using paths
derived from ``__file__`` rather than from the shell's working directory — which is
what the suites did, and what turned a run from ``benchmarks/`` into ``1 skipped`` with
a green exit.

``harness_import`` holds the import helper. It is a module rather than something
defined here because ``conftest`` is not a reliable import target for a sibling test
file: several directories in this repository carry a ``conftest.py`` and the basename
is not unique, while ``harness_import`` is.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_HARNESS = os.path.join(os.path.dirname(_HERE), "harness")

for _path in (_HERE, _HARNESS):
    if _path not in sys.path:
        sys.path.insert(0, _path)
