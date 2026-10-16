# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`OWNERSHIP_REFUSAL_PATTERNS` recognises refusals and admits nothing else.

The S3 arm this backs says: an operation declaring ``ownership: true`` must
either refuse a caller who is not the owner, or aim every data access at the
caller's own partition. That arm decides whether a declaration is believed, so
its vocabulary widens *what satisfies a claim* — and a pattern there that matches
nothing is not harmless. It is pre-approval of whatever next occupies the path,
which is the same hazard the gate-exemption registry's own evidence markers
carry.

Two directions, because either alone passes trivially:

- **Non-vacuity.** Every pattern must match at least one file the scan actually
  reads. A dead pattern is deleted, not kept "in case".
- **Discrimination.** The arm must reject a file that resolves a caller, deletes
  by a key that does not name one, and never refuses — which is the shape the
  arm was added for. A vocabulary broad enough to admit that is inert.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SCAN = REPO_ROOT / "scripts" / "sdlc" / "scan_api_rbac.py"
EXPECTATIONS = REPO_ROOT / "scripts" / "api_rbac_expectations.yaml"


def _scan_module():
    spec = importlib.util.spec_from_file_location("scan_api_rbac_under_test", SCAN)
    module = importlib.util.module_from_spec(spec)
    sys.argv = ["scan_api_rbac"]
    spec.loader.exec_module(module)
    return module


def _ownership_sources() -> dict[str, str]:
    """``enforced_in`` text for every operation declaring ``ownership: true``.

    This is the universe the vocabulary is read against — not the whole tree,
    because a pattern matching some unrelated file is not evidence that it does
    any work where the arm consults it.
    """
    spec = yaml.safe_load(EXPECTATIONS.read_text(encoding="utf-8"))
    sources = {}
    for group in spec.values():
        if not isinstance(group, dict):
            continue
        for op, policy in group.items():
            if not isinstance(policy, dict) or not policy.get("ownership"):
                continue
            path = REPO_ROOT / str(policy.get("enforced_in", ""))
            if path.is_file():
                sources[op] = path.read_text(encoding="utf-8")
    return sources


def test_there_is_an_ownership_declaring_operation_to_read_at_all():
    """Guards the two tests below from passing over an empty universe."""
    sources = _ownership_sources()
    assert sources, (
        "no operation in api_rbac_expectations.yaml declares `ownership: true` "
        "with a readable `enforced_in` file, so the assertions below would pass "
        "having examined nothing"
    )


def test_no_refusal_pattern_is_dead():
    """Each pattern shields at least one real file; a dead one is a loophole.

    If this fails, the named pattern matched nothing in any ownership-declaring
    resolver. Delete it rather than keeping it: an unmatched pattern is not
    inert, it silently admits the next file that happens to contain the string.
    """
    module = _scan_module()
    sources = _ownership_sources()
    dead = []
    for pattern in module.OWNERSHIP_REFUSAL_PATTERNS:
        if not any(pattern in text for text in sources.values()):
            dead.append(pattern)
    assert not dead, (
        f"{len(dead)} refusal pattern(s) match nothing in any of the "
        f"{len(sources)} ownership-declaring resolvers: {dead}. A pattern that "
        f"matches nothing is pre-approval of whatever next contains that string "
        f"— delete it rather than keeping it against a future need."
    )


def test_the_arm_still_rejects_a_resolver_that_never_refuses():
    """The shape this arm exists for must not satisfy it.

    A resolver that resolves the caller, mentions it in a log line, and then
    deletes by a key naming no caller — and never refuses — has to be reported.
    Vocabulary wide enough to admit this makes the arm vacuous, and the whole
    point is that naming the *subject* of an ownership check is not the check.
    """
    module = _scan_module()
    never_refuses = (
        "def handler(event, context):\n"
        "    user_id = event['identity']['username']\n"
        "    session_id = event['arguments']['sessionId']\n"
        "    logger.info(f'deleting session {session_id} (user: {user_id})')\n"
        "    rows = table.query(KeyConditionExpression='PK = :sid')\n"
        "    for row in rows['Items']:\n"
        "        table.delete_item(Key={'PK': row['PK'], 'SK': row['SK']})\n"
    )

    # It names the subject, which is what the first arm asks for...
    assert any(p in never_refuses for p in module.OWNERSHIP_PATTERNS)
    # ...and neither of the two things that would justify believing it.
    assert not any(p in never_refuses for p in module.OWNERSHIP_REFUSAL_PATTERNS), (
        "the refusal vocabulary admits a resolver that never refuses; the arm is "
        "satisfiable without any refusal and is therefore inert"
    )
    assert module.unscoped_key_accesses(never_refuses), (
        "the per-access predicate reports no unscoped access in a body whose only "
        "accesses are keyed by a session id; see its docstring on why a regex "
        "reads 1 access of 3 here"
    )


def test_a_resolver_that_does_refuse_is_accepted():
    """The other direction, so the two tests above cannot both pass by refusing everything."""
    module = _scan_module()
    refuses = (
        "def handler(event, context):\n"
        "    user_id = event['identity']['username']\n"
        "    if not _owns(session_id, user_id):\n"
        "        raise ResourceAccessDenied('Unauthorized: session not found.')\n"
    )
    assert any(p in refuses for p in module.OWNERSHIP_REFUSAL_PATTERNS)
