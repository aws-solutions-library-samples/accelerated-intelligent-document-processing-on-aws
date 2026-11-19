# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Whether a test run is in CI: the one reading every suite under ``scripts/tests`` uses.

Several suites here turn an absent toolchain (``node``, the UI's npm dependencies,
basedpyright) into a *skip* on a developer machine and a *failure* in CI, because in
CI the toolchain is installed on purpose and its absence is a broken job. That makes
"is this CI?" a decision with a cost in both directions, and each suite used to
answer it with its own copy of ``bool(os.getenv("CI") or ...)``. That spelling reads
``CI=false`` — which people do set, to get a developer's behaviour out of a tool that
honours ``CI`` — as *in* CI, because ``"false"`` is a non-empty string, and so turns
a skip into a failure on exactly the machine that asked not to be treated as CI.

The rule, in one place:

* ``CI`` is what both platforms set (GitHub Actions and GitLab each do). ``GITLAB_CI``
  and ``GITHUB_ACTIONS`` are read as well, so a runner that sets only its own name is
  still recognised.
* A variable counts when it is set to anything other than an explicit negative:
  empty, ``0``, ``false``, ``no`` or ``off``, compared case-insensitively after
  trimming whitespace.

The environment is read when :func:`in_ci` is called, never cached here, so a test
can drive it with ``monkeypatch``. A module that wants a constant (``IN_CI =
in_ci()``) gets the value at import time, which is what the existing guards
monkeypatch.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: The environment variables that mark a CI run, in the order they are read.
CI_VARIABLES = ("CI", "GITLAB_CI", "GITHUB_ACTIONS")

#: Values of those variables that mean "not CI", after ``strip().lower()``.
FALSE_SPELLINGS = frozenset({"", "0", "false", "no", "off"})


def in_ci(environ: Mapping[str, str] | None = None) -> bool:
    """Whether any of :data:`CI_VARIABLES` is set to something other than a negative.

    ``environ`` defaults to :data:`os.environ`, read at call time.
    """
    env = os.environ if environ is None else environ
    return any(
        env.get(name, "").strip().lower() not in FALSE_SPELLINGS
        for name in CI_VARIABLES
    )
