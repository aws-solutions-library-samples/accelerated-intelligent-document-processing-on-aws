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
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import gate_premises
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
    missing = [
        r for r in _run_all_tests_module().RUN_ROOTS if not (REPO_ROOT / r).is_dir()
    ]
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


# ---------------------------------------------------------------------------
# QUARANTINE: a root held back by ONE failing test.
# ---------------------------------------------------------------------------
#
# A root kept out of `make test` because a single test in it fails is the most
# perishable reason in the registry: the day somebody fixes that test, the reason is
# stale and nothing says so, and the root then sits outside the gate on the strength of
# a sentence that is no longer true. That is how the cfnresponse reason on
# `nested/bedrockkb/src/s3_vectors_manager` outlived its own remedy -- a `cfnresponse`
# stub had been written one directory down, and the entry above it still named the
# missing module as the obstruction.
#
# So the claim is computed instead of believed, in three directions: the named test must
# still EXIST, it must still fail, and everything else in its file must still pass.
#
# Existence is not redundant with failure, and leaving it out was a real hole. `pytest
# <path>::<name>` for a name that is gone exits 4 (usage error, "no match in any of
# ...") rather than 0, so a `returncode != 0` check alone is satisfied by DELETING or
# renaming the test. The entry would then stand forever naming a node id nothing can
# resolve, and the file's four passing tests would stay outside the gate with nothing
# prompting a revisit -- the same "reason that outlived its subject" failure this whole
# section exists to stop, reached by a different route. So the first check requires
# `--collect-only` to find exactly one item.

#: root -> the one pytest node id whose failure holds the root back.
_ROOTS_HELD_BACK_BY_ONE_TEST = {
    "nested/bedrockkb/src/s3_vectors_manager": (
        "test_handler.py::test_get_s3_vector_info_function"
    ),
}


def _pytest(root: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *arguments],
        cwd=REPO_ROOT / root,
        capture_output=True,
        text=True,
    )


@pytest.mark.unit
@pytest.mark.parametrize("root", sorted(_ROOTS_HELD_BACK_BY_ONE_TEST))
def test_the_one_test_holding_a_root_back_still_exists(root: str) -> None:
    """Rename or delete the named test and this fails, not the one below."""
    node = _ROOTS_HELD_BACK_BY_ONE_TEST[root]
    result = _pytest(root, node, "--collect-only")
    collected = re.search(r"^(\d+) tests? collected", result.stdout, re.M)
    assert result.returncode == 0 and collected and collected.group(1) == "1", (
        f"`pytest {node} --collect-only` does not resolve to exactly one test, so "
        f"QUARANTINE['{root}'] names something that no longer exists. If the test was "
        "renamed, re-key this entry; if it was deleted, the root is no longer held back "
        "and belongs in RUN_ROOTS.\n\n" + (result.stdout + result.stderr)[-2000:]
    )


@pytest.mark.unit
@pytest.mark.parametrize("root", sorted(_ROOTS_HELD_BACK_BY_ONE_TEST))
def test_the_one_test_holding_a_root_back_still_fails(root: str) -> None:
    """Fix the named test and this fails, which is the point."""
    node = _ROOTS_HELD_BACK_BY_ONE_TEST[root]
    result = _pytest(root, node)
    assert result.returncode != 0, (
        f"{node} passes now, so QUARANTINE['{root}'] no longer describes anything. "
        f"Move {root} into RUN_ROOTS, point the `make test-packages-cicd` recipe line "
        "at the directory instead of its `tests` subdirectory, and delete this entry "
        f"along with the QUARANTINE entries for {root} and {root}/tests.\n\n"
        + result.stdout[-2000:]
    )


@pytest.mark.unit
@pytest.mark.parametrize("root", sorted(_ROOTS_HELD_BACK_BY_ONE_TEST))
def test_only_the_named_test_holds_the_root_back(root: str) -> None:
    """Everything else in that file must pass, or the reason understates the problem."""
    node = _ROOTS_HELD_BACK_BY_ONE_TEST[root]
    module, _, name = node.partition("::")
    # ``-k`` rather than ``--deselect``: the node id ``--deselect`` wants is relative
    # to pytest's rootdir, which is the repository root here (pytest.ini lives there)
    # rather than the directory this runs in, so the obvious spelling silently
    # deselects nothing and the assertion below passes for the wrong reason.
    result = _pytest(root, module, "-k", f"not {name}")
    assert result.returncode == 0, (
        f"QUARANTINE['{root}'] names {node} as the only thing holding the root back, "
        f"but {module} still fails with that test deselected -- so the reason is "
        "incomplete and a reader would underestimate the work. Update it.\n\n"
        + result.stdout[-2000:]
    )
