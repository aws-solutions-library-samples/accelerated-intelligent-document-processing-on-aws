# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
The vendored review-queue modules must never drift from their canonical copies.

Both document-list resolvers carry no idp_common layer, so they vendor
``idp_common/review_queues/statuses.py`` (as ``review_statuses.py``) verbatim,
the same way they vendor ``config_scope.py``. A status vocabulary that differs
between call sites would let one resolver read a document as in review while
another does not.
"""

import importlib.util
import sys
from pathlib import Path

import pytest


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "lib" / "idp_common_pkg").is_dir():
            return parent
    raise RuntimeError("repo root not found")


REPO_ROOT = _repo_root()
CANONICAL_DIR = REPO_ROOT / "lib/idp_common_pkg/idp_common/review_queues"
RESOLVERS = [
    REPO_ROOT / "nested/api-resolvers/src/lambda/list_documents_gsi_resolver",
    REPO_ROOT / "nested/api-resolvers/src/lambda/list_documents_range_resolver",
]
#: canonical file name -> vendored file name
MODULES = {"statuses.py": "review_statuses.py", "visibility.py": "review_visibility.py"}

PAIRS = [
    (CANONICAL_DIR / canonical, resolver / vendored)
    for canonical, vendored in MODULES.items()
    for resolver in RESOLVERS
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("canonical", "vendored"), PAIRS, ids=lambda p: f"{p.parent.name}/{p.name}"
)
def test_vendored_copy_matches_canonical(canonical, vendored):
    assert canonical.exists(), f"canonical module missing: {canonical}"
    assert vendored.exists(), f"missing vendored copy: {vendored}"
    assert vendored.read_text() == canonical.read_text(), (
        f"{vendored} has drifted from {canonical}. Re-sync with:\n"
        f"  cp {canonical.relative_to(REPO_ROOT)} {vendored.relative_to(REPO_ROOT)}"
    )


@pytest.mark.unit
def test_vendored_statuses_normalise():
    """Smoke-test the copy that actually ships in this function's bundle."""
    spec = importlib.util.spec_from_file_location(
        "vendored_review_statuses", RESOLVERS[0] / "review_statuses.py"
    )
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)

    assert module.normalize_hitl_status("in_review").queue_managed is True
    assert module.normalize_hitl_status("PendingReview").kind == "pending"
    assert module.normalize_hitl_status(None).kind == "none"
