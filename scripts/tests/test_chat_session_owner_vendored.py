# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The vendored ``chat_session_owner`` copies must never drift from the canonical one.

The chat resolvers that decide whose a session id is ship without the
``idp_common`` layer, so they carry ``idp_common/chat_session_owner.py`` verbatim,
the same pattern as the vendored ``config_scope.py`` copies. Two copies that
disagreed would let one path register or read an id another path refuses.

The set of copies is derived — every ``chat_session_owner.py`` in the tracked tree
other than the canonical file — so a new copy is held to the same rule without an
edit here, and the test fails if the derivation finds none.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gate_premises  # noqa: E402

REPO = gate_premises.REPO_ROOT
CANONICAL = "lib/idp_common_pkg/idp_common/chat_session_owner.py"


def _copies() -> list[str]:
    return [
        p
        for p in gate_premises.tracked_files(
            "*chat_session_owner.py", include_untracked=True
        )
        if p != CANONICAL and p.rsplit("/", 1)[-1] == "chat_session_owner.py"
    ]


@pytest.mark.unit
def test_the_copies_are_found():
    copies = _copies()
    assert copies, "no vendored chat_session_owner.py found; the derivation is broken"
    for resolver in (
        "agent_chat_resolver",
        "get_agent_chat_messages_resolver",
        "delete_agent_chat_session_resolver",
        "send_chat_document_message_resolver",
    ):
        assert (
            f"nested/api-resolvers/src/lambda/{resolver}/chat_session_owner.py"
            in copies
        ), resolver


@pytest.mark.unit
@pytest.mark.parametrize("copy", _copies())
def test_the_copy_matches_the_canonical_module(copy):
    assert (REPO / copy).read_text() == (REPO / CANONICAL).read_text(), (
        f"{copy} has drifted from {CANONICAL}. Re-sync with:\n  cp {CANONICAL} {copy}"
    )


@pytest.mark.unit
def test_the_module_imports_nothing_a_layer_free_lambda_lacks():
    """Only the standard library, ``typing`` and ``botocore`` (in every runtime)."""
    tree = ast.parse((REPO / CANONICAL).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= {"typing", "botocore"}, sorted(roots)
