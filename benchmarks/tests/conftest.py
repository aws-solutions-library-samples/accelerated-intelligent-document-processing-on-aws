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


FIRST_PARTY_UNDER_TEST = ("idp_common",)


# Fail fast if a first-party package resolves outside this checkout. These tests import
# the library as an external dependency, so nothing puts its source on `sys.path` and the
# editable-install pointer alone decides which revision runs. On a machine sharing one
# interpreter between checkouts that pointer is rewritten by whoever last ran
# `make test-cicd`, and the wrong tree produces a green run describing other code.
#
# The imports sit inside the function deliberately: this block is appended after a
# module's own imports, and module-level imports there are an E402 lint failure.
#
# Rationale, and the identity-vs-ancestry distinction:
# scripts/tests/first_party_provenance.py
def _assert_first_party_provenance(packages):
    import pathlib as _pathlib
    import sys as _sys

    for ancestor in _pathlib.Path(__file__).resolve().parents:
        gate = ancestor / "scripts" / "tests" / "first_party_provenance.py"
        if not gate.is_file():
            continue
        _sys.path.insert(0, str(gate.parent))
        try:
            from first_party_provenance import assert_resolves_in

            for package in packages:
                assert_resolves_in(package, __file__)
        finally:
            _sys.path.pop(0)
        return


_assert_first_party_provenance(FIRST_PARTY_UNDER_TEST)
