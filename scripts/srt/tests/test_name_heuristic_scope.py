# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one exemption in the SRT gate that is a scope decision rather than a list.

``NAME_HEURISTIC_EXEMPT`` stops Bandit's B105/B106 identifier-name heuristics from
gating **inside test-only files** and nowhere else (#1086). Its members are not a
list of paths somebody wrote down; they are whatever
``ci_paths.is_test_only_path`` answers True for, which is why the ratchet here is
universe closure rather than staleness: there is no entry to go stale, and the
question worth asking is whether any path is *unaccounted for* — demoted without
being test code, or classified as neither.

So this file measures three things:

* the classification itself, over the real tree rather than over invented paths.
  Every tracked Python file is test-only or shipped, and the three shipped Lambda
  directories whose names begin with ``test`` — ``src/lambda/test_file_copier``,
  ``nested/api-resolvers/src/lambda/test_runner`` and
  ``nested/api-resolvers/src/lambda/test_set_resolver`` — are the ones a substring
  match would have got wrong;
* that the partition demotes only these two checks and only in test code, so no
  other finding can ride the exemption;
* that a credential-shaped name in **shipped** code still gates, which is the
  property the whole scope decision is built to keep. Blinding B105 repo-wide
  would have been one line in a ``.bandit`` file; this is what that line would
  have cost.
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
    partition_by_name_heuristic_scope,
)

pytestmark = pytest.mark.unit


def _tracked_python_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "*.py"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return [p for p in result.stdout.split("\0") if p]


#: Shipped code whose path *looks* like test code. Each is a Lambda handler or a
#: harness that runs in a deployed stack or a pipeline, so a credential-shaped
#: constant in any of them must keep gating.
SHIPPED_BUT_TEST_SHAPED = (
    "src/lambda/test_file_copier/index.py",
    "nested/api-resolvers/src/lambda/test_runner/index.py",
    "nested/api-resolvers/src/lambda/test_set_resolver/index.py",
)

#: Test code in each of the shapes the classifier recognises: under a ``tests``
#: directory, a ``test_*.py`` beside the code it covers, and a ``conftest.py``.
TEST_ONLY_SAMPLES = (
    "lib/idp_common_pkg/tests/unit/test_utils.py",
    "src/lambda/queue_sender/test_index.py",
    "lib/idp_common_pkg/tests/conftest.py",
)


@pytest.mark.parametrize("rel", SHIPPED_BUT_TEST_SHAPED)
def test_shipped_code_with_a_test_shaped_path_is_not_demoted(rel: str) -> None:
    """A directory beginning with ``test`` is not a test directory."""
    assert (PROJECT_ROOT / rel).is_file(), f"fixture path moved: {rel}"
    assert not is_test_only_path(rel), (
        f"{rel} is shipped code — a Lambda handler deployed into a customer's "
        "account — and would stop gating on a hardcoded credential if it were "
        "classified as test code. The classifier must match whole path segments, "
        "not prefixes."
    )


@pytest.mark.parametrize("rel", TEST_ONLY_SAMPLES)
def test_each_recognised_test_shape_is_demoted(rel: str) -> None:
    assert (PROJECT_ROOT / rel).is_file(), f"fixture path moved: {rel}"
    assert is_test_only_path(rel)


def test_every_tracked_python_file_is_classified_one_way_or_the_other() -> None:
    """Universe closure: no third state, and neither side may be empty.

    A classifier that answered False for everything would be indistinguishable
    from having no exemption, and one that answered True for everything would be
    the repo-wide blinding this exemption exists to avoid. Both are measured here
    against the tree rather than asserted.
    """
    tracked = _tracked_python_files()
    assert tracked, "git ls-files returned no Python files — cannot judge closure"

    test_only = [rel for rel in tracked if is_test_only_path(rel)]
    shipped = [rel for rel in tracked if not is_test_only_path(rel)]

    assert test_only, "no tracked file classifies as test-only; the exemption is dead"
    assert shipped, "every tracked file classifies as test-only; the check is blinded"
    assert len(test_only) + len(shipped) == len(tracked)

    # The library and the Lambda sources are what ships. Nothing under them may be
    # demoted, whatever it is called.
    shipped_trees = ("lib/idp_common_pkg/idp_common/", "src/lambda/", "nested/")
    leaked = sorted(
        rel
        for rel in test_only
        if rel.startswith(shipped_trees) and "/tests/" not in f"/{rel}"
    )
    unaccounted = [
        rel
        for rel in leaked
        if not any(
            part in ("test", "tests", "manual_tests") for part in rel.split("/")[:-1]
        )
        and not (
            Path(rel).name == "conftest.py"
            or Path(rel).name.startswith("test_")
            or Path(rel).name.endswith("_test.py")
        )
    ]
    assert not unaccounted, (
        "these paths under a shipped tree are demoted for no reason the classifier "
        f"can name: {unaccounted}"
    )


def test_the_partition_demotes_only_these_checks_and_only_in_test_code() -> None:
    """Nothing else may ride the exemption, and nothing is dropped."""
    issues = [
        {"check_id": "B105", "path": "lib/idp_common_pkg/tests/unit/test_x.py"},
        {"check_id": "b106", "path": "lib/idp_common_pkg/tests/conftest.py"},
        # Same checks, shipped path: still gating.
        {"check_id": "B105", "path": "lib/idp_common_pkg/idp_common/bedrock/client.py"},
        # Test path, different check: still gating. A test fixture that disables
        # TLS verification or runs a shell is not an identifier-name match.
        {"check_id": "B501", "path": "lib/idp_common_pkg/tests/unit/test_x.py"},
        # No path at all — a repo-wide observation. Fails closed.
        {"check_id": "B105", "path": None},
    ]

    gating, demoted = partition_by_name_heuristic_scope(issues)

    assert [i["check_id"] for i in demoted] == ["B105", "b106"]
    assert len(gating) == 3
    assert len(gating) + len(demoted) == len(issues), "a finding was dropped"
    assert all(i.get("path") != "lib/idp_common_pkg/tests/conftest.py" for i in gating)


def test_only_the_two_promoted_checks_are_in_scope() -> None:
    """The exemption covers the promotion, so it covers exactly what is promoted.

    B107 (a hardcoded password as a function *default*) is the third member of
    Bandit's hardcoded-password family and SRT does **not** promote it, so it
    arrives at its own LOW severity and never reaches the gate. Adding it here
    would exempt something nothing was gating on.
    """
    assert set(NAME_HEURISTIC_EXEMPT) == {"B105", "B106"}
