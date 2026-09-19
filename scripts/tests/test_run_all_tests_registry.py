# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``run_all_tests.RUN_ROOTS`` is a set of roots, so it must not repeat one.

The registry is a hand-maintained list, and each entry carries a comment saying why
that root needs registering. That makes a duplicate easy to add and hard to see: when
a second group of gates landed under ``scripts/tests``, the root was appended again
with its own comment rather than the existing comment being extended, and the two
entries sat 7 lines apart. Nothing complained, because a duplicate is harmless to
correctness — ``make test`` simply ran that whole suite twice, ~3.5 minutes each, and
printed it twice in the failed-roots summary, which read as three failing roots when
there were two.

Duplicates also quietly distort anything that counts the registry: the CONTRIBUTING
doc check derives its stated root count from ``len(RUN_ROOTS)``.
"""

from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_all_tests_module():
    path = REPO_ROOT / "scripts" / "run_all_tests.py"
    spec = importlib.util.spec_from_file_location("_run_all_tests_registry", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_run_roots_has_no_duplicates() -> None:
    roots = list(_run_all_tests_module().RUN_ROOTS)
    repeated = sorted(root for root, n in Counter(roots).items() if n > 1)
    assert not repeated, (
        "these roots are registered more than once in run_all_tests.RUN_ROOTS, so "
        f"`make test` runs each of their suites twice: {repeated}. Extend the "
        "existing entry's comment instead of appending a second entry."
    )


@pytest.mark.unit
def test_every_run_root_exists() -> None:
    """A renamed or deleted directory should fail here, not silently run nothing."""
    missing = [r for r in _run_all_tests_module().RUN_ROOTS if not (REPO_ROOT / r).is_dir()]
    assert not missing, (
        f"registered test roots that no longer exist: {missing}. A root that is gone "
        "collects zero tests, which passes — so the gate silently stops covering it."
    )
