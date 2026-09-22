# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one exemption in the SRT gate that is a scope decision rather than a list.

``NAME_HEURISTIC_EXEMPT`` stops Bandit's B105/B106 identifier-name heuristics from
gating in test code that no deployment artifact is built from, and nowhere else
(#1086). Its members are not a list of paths somebody wrote down; they are whatever
``ci_paths.is_test_only_path`` answers True for, which is why the ratchet here is
universe closure rather than staleness: there is no entry to go stale, and the
question worth asking is whether any path is demoted that should not be.

**The assertion that carries that ratchet has to be able to fail, and the obvious
way of writing it cannot.** Filtering the demoted set through a predicate derived
from the classifier makes the result empty by construction rather than by the state
of the tree: it holds for any tree, so it measures nothing. The closure assertion
below is therefore stated against an *independent* fact — the `CodeUri` and
`ContentUri` directories declared by the templates, parsed from the templates — and
it fails today against the shape-only classifier, which is what makes it a test.

That independence is the substance of the fix, not a technicality. `sam build`
copies a `CodeUri` directory verbatim; nothing in this tree prunes it. So
``src/lambda/queue_processor/test_reconcile_counter.py`` and
``src/lambda/test_file_copier/test_index.py`` ship into a customer's account
alongside the handlers beside them, and a classifier reading path shape alone
demoted 99 such files while correctly keeping their `index.py` neighbours gating.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SRT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SRT_DIR.parent.parent
sys.path.insert(0, str(SRT_DIR))

from ci_paths import (  # noqa: E402
    NAME_HEURISTIC_EXEMPT,
    is_test_only_path,
    packaged_source_dirs,
    partition_by_name_heuristic_scope,
)

pytestmark = pytest.mark.unit

#: How many directories the templates declare as a packaged source today. Pinned so
#: that a discovery regression — a changed key name, a parse that silently yields
#: nothing, a template that stops being found — fails here instead of quietly
#: restoring the shape-only behaviour this exemption was narrowed to avoid. Bump it
#: deliberately when a Lambda is added or removed.
EXPECTED_PACKAGED_DIRS = 120


def _tracked(pattern: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", pattern],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return [p for p in result.stdout.split("\0") if p]


@pytest.fixture(scope="module")
def packaged() -> frozenset[str]:
    dirs = packaged_source_dirs(PROJECT_ROOT)
    assert dirs is not None, "packaged source directories could not be established"
    return dirs


#: Shipped code whose path *looks* like test code. The first three are handlers; the
#: last two are test files that `sam build` copies into the artifact beside one.
SHIPPED_BUT_TEST_SHAPED = (
    "src/lambda/test_file_copier/index.py",
    "nested/api-resolvers/src/lambda/test_runner/index.py",
    "nested/api-resolvers/src/lambda/test_set_resolver/index.py",
    "src/lambda/queue_processor/test_reconcile_counter.py",
    "src/lambda/test_file_copier/test_index.py",
)

#: Test code in each shape the classifier recognises, none of it packaged.
TEST_ONLY_SAMPLES = (
    "lib/idp_common_pkg/tests/unit/test_utils.py",
    "lib/idp_common_pkg/tests/conftest.py",
    "scripts/srt/tests/test_name_heuristic_scope.py",
)


def test_the_packaged_set_is_discovered_and_its_size_is_pinned(
    packaged: frozenset[str],
) -> None:
    """Non-vacuity for the discovery itself: an empty set would demote everything."""
    assert len(packaged) == EXPECTED_PACKAGED_DIRS, (
        f"templates declare {len(packaged)} packaged source directories, pinned at "
        f"{EXPECTED_PACKAGED_DIRS}. If a Lambda was added or removed, bump the pin. "
        "If the number collapsed, the discovery is broken and every test-shaped "
        "file inside a Lambda directory is being demoted again."
    )
    for rel in packaged:
        assert (PROJECT_ROOT / rel).is_dir(), f"{rel} is not a directory"


def test_an_unknowable_packaged_set_demotes_nothing() -> None:
    """The fail-closed direction. `None` means "cannot tell", never "nothing ships"."""
    assert not is_test_only_path("lib/idp_common_pkg/tests/unit/test_utils.py", None)


@pytest.mark.parametrize("rel", SHIPPED_BUT_TEST_SHAPED)
def test_a_test_shaped_path_that_ships_is_not_demoted(
    rel: str, packaged: frozenset[str]
) -> None:
    """Either half of the rule alone gets one of these wrong."""
    assert (PROJECT_ROOT / rel).is_file(), f"fixture path moved: {rel}"
    assert not is_test_only_path(rel, packaged), (
        f"{rel} is copied into a deployment artifact by `sam build` and would stop "
        "gating on a hardcoded credential if it were classified as test-only."
    )


@pytest.mark.parametrize("rel", TEST_ONLY_SAMPLES)
def test_each_recognised_test_shape_is_demoted(
    rel: str, packaged: frozenset[str]
) -> None:
    assert (PROJECT_ROOT / rel).is_file(), f"fixture path moved: {rel}"
    assert is_test_only_path(rel, packaged)


def test_nothing_demoted_is_inside_a_packaged_source_directory(
    packaged: frozenset[str],
) -> None:
    """The closure assertion, stated against the templates rather than the classifier.

    This is the one that fails on a shape-only classifier, and the reason it can
    fail is that neither side of the comparison is derived from the other: the
    demoted set comes from `is_test_only_path`, the packaged set from parsing
    `CodeUri`/`ContentUri` out of every content-discovered template.
    """
    tracked_python = _tracked("*.py")
    assert tracked_python, "git ls-files returned no Python files"

    demoted = [rel for rel in tracked_python if is_test_only_path(rel, packaged)]
    offenders = sorted(
        rel
        for rel in demoted
        if any(rel == d or rel.startswith(d.rstrip("/") + "/") for d in packaged)
    )
    assert not offenders, (
        f"{len(offenders)} demoted path(s) sit inside a directory a deployment "
        "artifact is built from, so a hardcoded credential in them would reach a "
        "customer's account without gating:\n  " + "\n  ".join(offenders[:20])
    )


def test_both_sides_of_the_classification_are_populated(
    packaged: frozenset[str],
) -> None:
    """A classifier that answers one way for everything is not a scope decision.

    All-False is indistinguishable from having no exemption; all-True is the
    repo-wide blinding this exemption exists to avoid. Both are measured against the
    tree rather than asserted.
    """
    tracked_python = _tracked("*.py")
    demoted = [rel for rel in tracked_python if is_test_only_path(rel, packaged)]
    gating = [rel for rel in tracked_python if not is_test_only_path(rel, packaged)]

    assert demoted, "no tracked file is demoted; the exemption is dead"
    assert gating, "every tracked file is demoted; the check is blinded"
    assert len(demoted) + len(gating) == len(tracked_python)

    # The library and the Lambda sources are what ships. Whatever the classifier
    # says about shape, nothing under a packaged tree may be demoted, and the
    # library's own module code must gate.
    assert not [
        rel for rel in demoted if rel.startswith("lib/idp_common_pkg/idp_common/")
    ]


def test_the_partition_demotes_only_these_checks_and_only_unpackaged_test_code(
    packaged: frozenset[str],
) -> None:
    """Nothing else may ride the exemption, and nothing is dropped."""
    issues = [
        {"check_id": "B105", "path": "lib/idp_common_pkg/tests/unit/test_x.py"},
        {"check_id": "b106", "path": "lib/idp_common_pkg/tests/conftest.py"},
        # Same checks, shipped path: still gating.
        {"check_id": "B105", "path": "lib/idp_common_pkg/idp_common/bedrock/client.py"},
        # Test-shaped, but inside a Lambda's CodeUri: still gating.
        {"check_id": "B105", "path": "src/lambda/test_file_copier/test_index.py"},
        # Test path, different check: still gating. A fixture that disables TLS
        # verification is not an identifier-name match.
        {"check_id": "B501", "path": "lib/idp_common_pkg/tests/unit/test_x.py"},
        # No path at all — a repo-wide observation. Fails closed.
        {"check_id": "B105", "path": None},
    ]

    gating, demoted = partition_by_name_heuristic_scope(issues, PROJECT_ROOT)

    assert [i["check_id"] for i in demoted] == ["B105", "b106"]
    assert len(gating) == 4
    assert len(gating) + len(demoted) == len(issues), "a finding was dropped"
    assert {i["path"] for i in demoted} == {
        "lib/idp_common_pkg/tests/unit/test_x.py",
        "lib/idp_common_pkg/tests/conftest.py",
    }
    assert packaged  # the fixture is what made the fourth case gate


def test_only_the_two_promoted_checks_are_in_scope() -> None:
    """The exemption covers the promotion, so it covers exactly what is promoted.

    B107 (a hardcoded password as a function *default*) is the third member of
    Bandit's hardcoded-password family and SRT does **not** promote it, so it
    arrives at its own LOW severity and never reaches the gate. Adding it here
    would exempt something nothing was gating on.
    """
    assert set(NAME_HEURISTIC_EXEMPT) == {"B105", "B106"}
