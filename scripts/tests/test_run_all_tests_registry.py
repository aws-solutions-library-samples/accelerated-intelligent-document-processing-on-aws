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

import gate_premises

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


# ---------------------------------------------------------------------------
# QUARANTINE: the reasons, not just the shape.
# ---------------------------------------------------------------------------
#
# `QUARANTINE` excludes a directory from `make test` entirely, so each entry is a
# gate exemption and each reason is a premise. The reasons are heterogeneous -- an
# unavailable dependency, a mis-collection, a source tree, an absence of tests -- so
# there is no single predicate for the constant. But one KIND of reason is
# mechanically checkable, and it is the kind that has already been wrong here.
#
# `samples/lambda-hook-inference/GENAIIDP-w2-copy-consistency` was excluded on the
# stated ground that it "collects zero pytest tests". It collects six, all passing, so
# six tests sat outside every gate on the strength of a sentence nobody ran. That
# entry is now in RUN_ROOTS with the correction recorded beside it -- and the identical
# wording still justifies another entry, unchecked. This closes that.

#: Substrings that mark a QUARANTINE reason as claiming pytest finds nothing there.
_ZERO_COLLECTION_CLAIMS = ("collects zero", "collects no", "not a test suite")


def _entries_claiming_zero_collection() -> list[str]:
    quarantine = _run_all_tests_module().QUARANTINE
    return sorted(
        root
        for root, reason in quarantine.items()
        if any(claim in reason.lower() for claim in _ZERO_COLLECTION_CLAIMS)
    )


@pytest.mark.unit
def test_the_zero_collection_claim_is_still_made_by_something() -> None:
    """Guard the discovery: a silent zero would make the check below vacuous."""
    claiming = _entries_claiming_zero_collection()
    assert claiming, (
        "no QUARANTINE reason matches _ZERO_COLLECTION_CLAIMS any more. If the wording "
        "changed, update the patterns; if every such entry is gone, delete this check "
        "rather than leaving it passing on an empty set."
    )


@pytest.mark.unit
@pytest.mark.parametrize("root", _entries_claiming_zero_collection())
def test_a_root_quarantined_for_collecting_nothing_really_collects_nothing(
    root: str,
) -> None:
    """Run the collector rather than believing the sentence.

    A collection *error* fails this too, deliberately: "there is nothing here" and
    "this cannot be imported in this environment" are different premises, and only the
    first justifies exclusion on these grounds. The second means the tests exist and
    nobody runs them, which is the direction that costs something.
    """
    reason = _run_all_tests_module().QUARANTINE[root]
    holds, explanation = gate_premises.collects_zero_tests(root)

    # `scripts` is quarantined for MIS-collection (its live RBAC harness has a
    # `test_email()` helper pytest picks up), not for collecting nothing. Its reason
    # matches the pattern above because it opens "Not a test suite", so the claim it
    # actually makes is about what pytest does with it, which this predicate cannot
    # judge. Named explicitly so the pattern match cannot quietly acquire it.
    if root == "scripts":
        assert "mis-collect" in reason, (
            "the 'scripts' exemption no longer says it is mis-collected, so the reason "
            f"it is skipped here no longer applies. Reason: {reason}"
        )
        return

    assert holds, (
        f"QUARANTINE excludes {root!r} from `make test` on the stated ground that "
        f"{reason!r}, but {explanation}. Either the tests are real and belong in "
        "RUN_ROOTS, or the reason is about something else and should say so -- the "
        "identical claim was already false once, for "
        "samples/lambda-hook-inference/GENAIIDP-w2-copy-consistency, which collects six."
    )
