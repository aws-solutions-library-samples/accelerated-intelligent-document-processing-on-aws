# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Assert that every ``src/lambda`` test directory is actually run by CI.

``make test-packages-cicd`` is the only recipe that runs the per-Lambda suites in
either CI (`.github/workflows/developer-tests.yml` and `.gitlab-ci.yml` both
invoke it). It enumerates paths by hand, so a test directory that nobody adds to
it is never run by CI — silently, and with a green pipeline.

Nothing detected that before this test. ``scripts/tests/test_ci_gate_parity.py``
compares gate *names* between the two CI configs, so a suite missing from **both**
sides is invisible to it by construction. And ``scripts/run_all_tests.py`` does
discover every test root, but it backs ``make test``, which runs in neither CI —
so its registry proves a suite is *runnable*, not that it is *run*.

Three instances of this gap are known, all found by hand and late:

* ``make cfn-lint`` and ``make validate-buildspec`` sat in ``lint``/``fastlint``
  but in neither CI for months.
* 12 workflow-tracker tests (``test_decrement_durability.py``,
  ``test_redacted_superseded.py``, ``test_timed_out_status.py``) ran in neither
  CI because the recipe named a single file rather than the directory.
* 9 more ``src/lambda`` directories — 157 tests — were in neither CI until the
  same change that added this test.

Both sides are DERIVED, never listed here: the required set comes from walking the
filesystem (via ``run_all_tests.discover_test_roots``), and the covered set comes
from parsing the recipe. A guard that enumerated the directories it knows about
would reproduce the defect it exists to prevent.

Deliberately excluded directories are taken from ``run_all_tests.QUARANTINE``,
which already carries a written reason for each one, so there is exactly one place
to record "this suite cannot run headless" instead of two.

Scope note: this asserts DIRECTORY coverage. A recipe line that names individual
files inside a covered directory (``cd src/lambda/queue_sender && pytest
test_index.py``) still counts as covering it, so a second test file added beside
an enumerated one would not be caught here. That directory currently holds only
the file it names, so there is no live gap; closing the file-level case would mean
requiring bare-directory invocations throughout, which is a larger change.

Only ``src/lambda/`` is asserted. The same argument applies to
``patterns/unified/tests`` and the ``nested/api-resolvers`` Lambdas, and issue
#980 tracks exactly that generalisation — this file is the ``src/lambda``-scoped
instance of the check #980 asks for. It is deliberately not generalised here
because PR #953 is concurrently editing this recipe; widening the subtree would
collide. The generalised version belongs with whichever of the two lands second.
Issues #974 and #980 are two further known instances of the same shape.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
RECIPE_TARGET = "test-packages-cicd"
SUBTREE = "src/lambda/"

pytestmark = pytest.mark.unit


def _run_all_tests_module():
    """Import scripts/run_all_tests.py by path (it is a script, not a package)."""
    path = REPO_ROOT / "scripts" / "run_all_tests.py"
    spec = importlib.util.spec_from_file_location("_run_all_tests_for_ci_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recipe_body() -> list[str]:
    """The tab-indented lines of the test-packages-cicd recipe."""
    lines = MAKEFILE.read_text().splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith(RECIPE_TARGET + ":")]
    assert len(starts) == 1, (
        f"expected exactly one '{RECIPE_TARGET}:' rule in the Makefile, "
        f"found {len(starts)}"
    )
    body: list[str] = []
    for ln in lines[starts[0] + 1 :]:
        if ln.startswith("\t"):
            body.append(ln)
        elif ln.strip() == "":
            continue
        else:
            break
    assert body, f"parsed an empty recipe body for {RECIPE_TARGET}"
    return body


def _enumerated_paths() -> set[str]:
    """src/lambda paths the recipe actually runs pytest against.

    ``@echo`` and ``@#`` lines are dropped first: a path mentioned in a progress
    message or a comment must not be able to satisfy this test.

    The lookbehind matters. Without it ``src/lambda/`` also matches inside
    ``nested/api-resolvers/src/lambda/test_runner``, which would make an
    unrelated nested-stack Lambda appear to cover a top-level ``src/lambda``
    directory of the same name.
    """
    paths: set[str] = set()
    for ln in _recipe_body():
        stripped = ln.lstrip("\t").lstrip()
        if stripped.startswith("@echo") or stripped.startswith("@#"):
            continue
        for match in re.findall(
            r"(?<![A-Za-z0-9_./-])src/lambda/[A-Za-z0-9_][A-Za-z0-9_./-]*", stripped
        ):
            paths.add(match.rstrip("/"))
    return paths


def _covered_by(directory: str, candidates: set[str]) -> bool:
    """True if `directory` equals, or sits under, any path in `candidates`."""
    return any(
        directory == c or directory.startswith(c.rstrip("/") + "/") for c in candidates
    )


def _discovered_dirs() -> set[str]:
    mod = _run_all_tests_module()
    return {d for d in mod.discover_test_roots() if d.startswith(SUBTREE)}


def _quarantined_dirs() -> set[str]:
    mod = _run_all_tests_module()
    return {q for q in mod.QUARANTINE if q.startswith(SUBTREE)}


def test_derivation_is_not_vacuous():
    """A guard whose two derived sets are empty passes for the wrong reason.

    Neither side is checked against a fixed list — that would be the defect this
    file exists to prevent. Instead: the walk must find a directory that provably
    holds tests, and every path the recipe parse yields must exist on disk. A
    broken walk, a broken parse, or a recipe naming a path that no longer exists
    all fail here rather than quietly turning the assertion below into a no-op.
    """
    discovered = _discovered_dirs()
    assert "src/lambda/workflow_tracker" in discovered, (
        "filesystem walk found no tests in src/lambda/workflow_tracker, which "
        f"does hold test_*.py; discovery is broken. Got: {sorted(discovered)}"
    )

    enumerated = _enumerated_paths()
    assert enumerated, (
        f"recipe parse found no src/lambda paths at all in {RECIPE_TARGET}; the "
        "parse is broken, and the coverage assertion below would pass vacuously."
    )
    stale = sorted(p for p in enumerated if not (REPO_ROOT / p).exists())
    assert not stale, (
        f"{RECIPE_TARGET} names src/lambda paths that do not exist:\n  "
        + "\n  ".join(stale)
    )


def test_every_src_lambda_test_dir_is_run_by_cicd():
    discovered = _discovered_dirs()
    quarantined = _quarantined_dirs()
    enumerated = _enumerated_paths()

    required = {d for d in discovered if not _covered_by(d, quarantined)}
    missing = sorted(d for d in required if not _covered_by(d, enumerated))

    assert not missing, (
        "These directories hold test_*.py but are not run by `make "
        f"{RECIPE_TARGET}`, which is the only recipe either CI uses to run the "
        "per-Lambda suites — so their tests run in NEITHER CI:\n  "
        + "\n  ".join(missing)
        + f"\n\nAdd each to the {RECIPE_TARGET} recipe in the Makefile (give each "
        "its own `cd <dir> && $(PYTHON) -m pytest` line — these Lambdas all "
        "define a module named `index`, so a combined invocation fails "
        "collection on the basename collision). If a suite genuinely cannot run "
        "headless, add it to QUARANTINE in scripts/run_all_tests.py with a "
        "reason instead."
    )


def test_quarantined_src_lambda_dirs_carry_a_reason():
    """An exemption without a stated reason is indistinguishable from an oversight."""
    mod = _run_all_tests_module()
    for path, reason in mod.QUARANTINE.items():
        if not path.startswith(SUBTREE):
            continue
        assert reason and reason.strip(), f"QUARANTINE['{path}'] has no reason"
