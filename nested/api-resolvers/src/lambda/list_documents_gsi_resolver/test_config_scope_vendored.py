# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
The vendored config_scope copies must never drift from the canonical module.

Both document-list resolvers enforce configuration-profile scope but carry no
idp_common layer: they are on the hottest UI query and are deliberately kept
dependency-free. So they vendor idp_common/config_scope.py verbatim, following
the same pattern as src/lambda/chat_stream_processor/vendored/.

A scope matcher that differs between call sites is a privilege-escalation bug —
one resolver would admit what another denies. This test is the guard.
"""

from pathlib import Path

import pytest


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "lib" / "idp_common_pkg").is_dir():
            return parent
    raise RuntimeError("repo root not found")


REPO_ROOT = _repo_root()
CANONICAL = REPO_ROOT / "lib/idp_common_pkg/idp_common/config_scope.py"
VENDORED = [
    REPO_ROOT
    / "nested/api-resolvers/src/lambda/list_documents_gsi_resolver/config_scope.py",
    REPO_ROOT
    / "nested/api-resolvers/src/lambda/list_documents_range_resolver/config_scope.py",
]


@pytest.mark.unit
def test_canonical_module_exists():
    assert CANONICAL.exists(), f"canonical scope module missing: {CANONICAL}"


@pytest.mark.unit
@pytest.mark.parametrize("vendored", VENDORED, ids=lambda p: p.parent.name)
def test_vendored_copy_matches_canonical(vendored):
    assert vendored.exists(), f"missing vendored copy: {vendored}"
    assert vendored.read_text() == CANONICAL.read_text(), (
        f"{vendored} has drifted from {CANONICAL}. Re-sync with:\n"
        f"  cp lib/idp_common_pkg/idp_common/config_scope.py "
        f"{vendored.relative_to(REPO_ROOT)}"
    )


@pytest.mark.unit
def test_vendored_module_enforces_the_fail_closed_rule():
    """Smoke-test the copy that actually ships in this function's bundle."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("vendored_config_scope", VENDORED[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.scope_allows(None, "anything") is True
    assert module.scope_allows(["lending"], "lending") is True
    assert module.scope_allows(["lending"], "claims") is False
    # The document-list fail-closed case this vendoring exists to fix.
    assert module.scope_allows(["lending"], None) is False
    assert module.scope_allows(["lending-*"], "lending-2") is True
