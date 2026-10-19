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
MODULES = {"statuses.py": "review_statuses.py"}

#: The document lookup function also carries no idp_common layer and reads the
#: review status values from its own ``review_statuses.py`` copy.
LOOKUP_FUNCTION = REPO_ROOT / "src/lambda/lookup_function"

PAIRS = [
    (CANONICAL_DIR / canonical, resolver / vendored)
    for canonical, vendored in MODULES.items()
    for resolver in RESOLVERS
] + [(CANONICAL_DIR / "statuses.py", LOOKUP_FUNCTION / "review_statuses.py")]


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


def _load_vendored(resolver):
    spec = importlib.util.spec_from_file_location(
        f"vendored_review_statuses_{resolver.name}", resolver / "review_statuses.py"
    )
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


@pytest.mark.unit
@pytest.mark.parametrize("resolver", RESOLVERS, ids=lambda p: p.name)
@pytest.mark.parametrize(
    ("raw", "kind", "queue_managed", "in_review"),
    [
        (" in_review ", "in_review", True, True),
        ("complete", "complete", True, False),
        ("rejected", "rejected", True, False),
        ("Review Pending", "pending", False, False),
        ("ReviewInProgress", "in_progress", False, False),
        ("Review Skipped", "skipped", False, False),
        ("In_Review", "none", False, False),  # exact match only
        ("", "none", False, False),
    ],
)
def test_the_shipped_copy_reads_every_status_spelling(
    resolver, raw, kind, queue_managed, in_review
):
    """The bundled copy decides which documents the list shows as in review."""
    view = _load_vendored(resolver).normalize_hitl_status(raw)
    assert (view.kind, view.queue_managed, view.in_review) == (
        kind,
        queue_managed,
        in_review,
    )


@pytest.mark.unit
@pytest.mark.parametrize("resolver", RESOLVERS, ids=lambda p: p.name)
def test_the_shipped_copy_maps_queue_item_states_to_review_statuses(resolver):
    module = _load_vendored(resolver)
    status = module.review_status_for_state
    for state in ("HOLDING", "OPEN", "CLAIMED", "FINALIZING", "FINALIZE_FAILED"):
        assert status(state) is module.ReviewStatus.IN_REVIEW
    assert status(module.QueueItemState.COMPLETE) is module.ReviewStatus.COMPLETE
    assert status("REJECTED") is module.ReviewStatus.REJECTED
    assert status("WITHDRAWN") is None
    with pytest.raises(ValueError):
        status("NOT_A_STATE")
