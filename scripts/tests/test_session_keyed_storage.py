# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Storage keyed on a chat session id alone must sit behind an ownership check.

A chat session id comes from the request body. A DynamoDB key built from it with
no user component names the same item for every user who sends that id, which is
how conversation memory and chat history were both readable by a second user who
reused a first user's session id. ``session_keyed_storage`` finds every such key
construction in the tracked Python sources (see its docstring for exactly what it
recognises and what it cannot see); this module fails on any it finds that is not
listed below.

Listing a file is not a way to turn the check off for it. Each entry is one file,
with the number of sites audited when it was written pinned beside it, the file
holding the ownership check those sites rely on, and the reason. So:

* a site added to a listed file fails (the pinned count no longer matches);
* a file that stops having such sites fails (the entry is stale);
* a finding in a file that is not listed fails, naming the line;
* the guard each entry names must actually define or call
  ``claim_chat_session``, the function that decides whose a session id is.

What is not checked is that the guard runs before the site on every path. That is
the reason column, and it is a reviewer's judgement.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gate_premises  # noqa: E402
import session_keyed_storage as sk  # noqa: E402

REPO = gate_premises.REPO_ROOT

_REGISTRY = "lib/idp_common_pkg/idp_common/chat_session_owner.py"
_REGISTRY_REASON = (
    "the owner registry itself: ChatSessionOwnersTable is keyed by sessionId alone "
    "on purpose, because a conditional write on that key is what makes one user the "
    "owner of an id, and has_stored_messages reads ChatMessagesTable by sessionId "
    "only to tell an unused id from one with history"
)


def _vendored(resolver: str) -> str:
    return f"nested/api-resolvers/src/lambda/{resolver}/chat_session_owner.py"


#: path -> (sites pinned when audited, file holding the guard, reason).
SESSION_KEYED_EXEMPT: dict[str, tuple[int, str, str]] = {
    _REGISTRY: (4, _REGISTRY, _REGISTRY_REASON),
    _vendored("agent_chat_resolver"): (
        4,
        _vendored("agent_chat_resolver"),
        "byte-identical copy of the owner registry (test_chat_session_owner_vendored)",
    ),
    _vendored("get_agent_chat_messages_resolver"): (
        4,
        _vendored("get_agent_chat_messages_resolver"),
        "byte-identical copy of the owner registry (test_chat_session_owner_vendored)",
    ),
    _vendored("delete_agent_chat_session_resolver"): (
        4,
        _vendored("delete_agent_chat_session_resolver"),
        "byte-identical copy of the owner registry (test_chat_session_owner_vendored)",
    ),
    _vendored("send_chat_document_message_resolver"): (
        4,
        _vendored("send_chat_document_message_resolver"),
        "byte-identical copy of the owner registry (test_chat_session_owner_vendored)",
    ),
    "nested/api-resolvers/src/lambda/agent_chat_resolver/index.py": (
        1,
        "nested/api-resolvers/src/lambda/agent_chat_resolver/index.py",
        "stores a message under PK=sessionId; a signed-in caller may send only "
        "user messages, and _require_session_owner runs before any of them is "
        "stored. Assistant messages come only from the identity-less backend path",
    ),
    "nested/api-resolvers/src/lambda/get_agent_chat_messages_resolver/index.py": (
        2,
        "nested/api-resolvers/src/lambda/get_agent_chat_messages_resolver/index.py",
        "queries messages by PK=sessionId only after _verify_session_ownership; the "
        "other site is the Chat-with-Document ownership table read as earlier "
        "evidence inside that check",
    ),
    "nested/api-resolvers/src/lambda/delete_agent_chat_session_resolver/index.py": (
        2,
        "nested/api-resolvers/src/lambda/delete_agent_chat_session_resolver/index.py",
        "queries messages by PK=sessionId and batch-deletes the keys it read, both "
        "only after _caller_owns_session",
    ),
    "nested/api-resolvers/src/lambda/send_chat_document_message_resolver/index.py": (
        1,
        "nested/api-resolvers/src/lambda/send_chat_document_message_resolver/index.py",
        "reads its earlier ownership table (keyed by sessionId) as evidence inside "
        "_check_session_ownership",
    ),
    "src/lambda/agent_chat_processor/index.py": (
        2,
        "src/lambda/agent_chat_processor/index.py",
        "_persist_chat_turn writes the user and assistant messages under "
        "PK=sessionId; _enforce_session_owner runs before the turn starts, and "
        "with no verified owner nothing is persisted",
    ),
    "src/lambda/chat_stream_processor/vendored/agent_chat_processor.py": (
        2,
        "src/lambda/chat_stream_processor/vendored/agent_chat_processor.py",
        "byte-identical copy of the agent chat processor (test_vendored_in_sync)",
    ),
    "src/lambda/chat_with_document_processor/index.py": (
        2,
        "nested/api-resolvers/src/lambda/send_chat_document_message_resolver/index.py",
        "_persist_chat_messages writes under PK=sessionId only on the REST path "
        "(it is a no-op when the streaming sink is installed), and that path is "
        "reached only through the Chat-with-Document resolver, which registers or "
        "checks the owner before it invokes this processor",
    ),
    "src/lambda/chat_stream_processor/vendored/chat_with_document_processor.py": (
        2,
        "nested/api-resolvers/src/lambda/send_chat_document_message_resolver/index.py",
        "copy of the document chat processor; behind the streaming endpoint the "
        "writing function is a no-op because a sink is installed",
    ),
}


def _is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return "/tests/" in f"/{path}" or name.startswith("test_") or name == "conftest.py"


def _universe() -> list[str]:
    """Every tracked (or not-yet-committed) non-test Python file."""
    return [
        p
        for p in gate_premises.tracked_files("*.py", include_untracked=True)
        if not _is_test_path(p)
    ]


def _templates() -> dict[str, dict]:
    docs = {}
    for p in gate_premises.tracked_files(
        "*.yaml", "*.yml", "*.json", include_untracked=True
    ):
        text = (REPO / p).read_text(encoding="utf-8", errors="ignore")
        if "AWSTemplateFormatVersion" in text:
            docs[p] = gate_premises.load_template(p)
    return docs


@pytest.fixture(scope="module")
def key_attributes() -> frozenset[str]:
    return sk.table_key_attributes(_templates())


@pytest.fixture(scope="module")
def findings(key_attributes) -> list[sk.Finding]:
    return sk.scan_paths(REPO, _universe(), key_attributes)


@pytest.mark.unit
def test_the_key_attributes_are_derived_from_the_templates(key_attributes):
    # Non-empty and holding the chat tables' keys, or every Key= check below would
    # silently consider no entry at all.
    assert {"PK", "SK", "sessionId", "userId"} <= key_attributes


@pytest.mark.unit
def test_the_scan_reads_the_chat_code():
    universe = set(_universe())
    assert "lib/idp_common_pkg/idp_common/agents/utils/memory_provider.py" in universe
    assert "src/lambda/agent_chat_processor/index.py" in universe


@pytest.mark.unit
def test_every_session_keyed_site_is_audited(findings):
    by_path: dict[str, list[sk.Finding]] = {}
    for f in findings:
        by_path.setdefault(f.path, []).append(f)
    unaccounted = {p: fs for p, fs in by_path.items() if p not in SESSION_KEYED_EXEMPT}
    assert not unaccounted, (
        "DynamoDB keys built from a session or conversation id with no user term, "
        "in files not audited here:\n"
        + "\n".join(f"  {f}" for fs in unaccounted.values() for f in fs)
        + "\nPut the user in the key, or put the site behind claim_chat_session and "
        "list the file in SESSION_KEYED_EXEMPT with its guard and reason."
    )
    counts = Counter(f.path for f in findings)
    drifted = {
        p: (counts[p], pinned)
        for p, (pinned, _, _) in SESSION_KEYED_EXEMPT.items()
        if counts[p] != pinned
    }
    assert not drifted, (
        "a listed file's site count differs from the count pinned when it was "
        "audited (found, pinned): "
        + ", ".join(f"{p} {v}" for p, v in sorted(drifted.items()))
        + ". Audit the new or removed sites, then update the pinned count."
    )


@pytest.mark.unit
@pytest.mark.parametrize("path", sorted(SESSION_KEYED_EXEMPT))
def test_each_entry_names_a_guard_that_decides_ownership(path):
    _, guard, reason = SESSION_KEYED_EXEMPT[path]
    assert reason.strip(), f"{path}: no reason"
    source = (REPO / guard).read_text(encoding="utf-8")
    tree = ast.parse(source)
    defines_or_calls = any(
        (isinstance(n, ast.FunctionDef) and n.name == "claim_chat_session")
        or (
            isinstance(n, ast.Call)
            and (
                getattr(n.func, "id", None) == "claim_chat_session"
                or getattr(n.func, "attr", None) == "claim_chat_session"
            )
        )
        for n in ast.walk(tree)
    )
    assert defines_or_calls, (
        f"{path} is listed as guarded by {guard}, which neither defines nor calls "
        "claim_chat_session"
    )


# --- the rule fires: synthetic sources ---------------------------------------
# Each case is a key construction the scanner must flag or must leave alone,
# driven through the same scan_source the gate uses.

_KEYS = frozenset({"PK", "SK", "sessionId", "userId"})

FLAGGED = {
    "memory-key-by-session": 'def f(self):\n    return f"conversation#{self.session_id}"\n',
    "messages-pk": 'def f(t, session_id):\n    t.get_item(Key={"PK": session_id})\n',
    "through-a-local": (
        'def f(t, session_id):\n    pk = session_id\n    t.put_item(Item={"PK": pk, "SK": "x"})\n'
    ),
    "from-the-event": 'def f(t, event):\n    t.get_item(Key={"PK": event["sessionId"]})\n',
    "query-values": (
        "def f(t, session_id):\n"
        '    t.query(KeyConditionExpression="PK = :s", '
        'ExpressionAttributeValues={":s": session_id})\n'
    ),
    "key-condition-object": (
        "def f(t, session_id):\n"
        '    t.query(KeyConditionExpression=Key("PK").eq(session_id))\n'
    ),
    "concatenated-key": (
        'def f(t, session_id):\n    return "conversation#" + session_id\n'
    ),
    "formatted-key": (
        'def f(session_id):\n    return "conversation#{}".format(session_id)\n'
    ),
    "dict-call": "def f(t, session_id):\n    t.get_item(Key=dict(PK=session_id))\n",
    "annotated-assignment": (
        "def f(t, session_id):\n"
        '    key: dict = {"PK": session_id}\n'
        "    t.get_item(Key=key)\n"
    ),
    "annotated-values": (
        "def f(t, session_id):\n"
        '    values: dict = {":s": session_id}\n'
        '    t.query(KeyConditionExpression="PK = :s", ExpressionAttributeValues=values)\n'
    ),
    "subscript-built": (
        "def f(t, session_id):\n"
        "    key = {}\n"
        '    key["PK"] = session_id\n'
        "    t.get_item(Key=key)\n"
    ),
    "module-level-subscript": (
        'key = {}\nkey["PK"] = session_id\ndef f(t):\n    t.get_item(Key=key)\n'
    ),
    "splat": (
        "def f(t, session_id):\n"
        '    kw = {"Key": {"PK": session_id}}\n'
        "    t.get_item(**kw)\n"
    ),
    "batch-get": (
        "def f(c, session_id):\n"
        '    c.batch_get_item(RequestItems={"T": {"Keys": [{"PK": session_id}]}})\n'
    ),
    "transaction": (
        "def f(c, session_id):\n"
        '    c.transact_write_items(TransactItems=[{"Put": {"Item": {"PK": session_id}}}])\n'
    ),
    "legacy-key-conditions": (
        "def f(t, session_id):\n"
        '    t.query(KeyConditions={"PK": {"AttributeValueList": [session_id]}})\n'
    ),
    "partiql": (
        "def f(c, session_id):\n"
        '    c.execute_statement(Statement="SELECT * FROM T WHERE PK=?", '
        'Parameters=[{"S": session_id}])\n'
    ),
    "loop-variable": (
        "def f(t, session_ids):\n"
        "    for sid in session_ids:\n"
        '        t.get_item(Key={"PK": sid})\n'
    ),
    "comprehension": (
        "def f(t, session_ids):\n"
        '    [t.get_item(Key={"PK": sid}) for sid in session_ids]\n'
    ),
    "with-as": (
        "def f(t, open_session_id):\n"
        "    with open_session_id() as sid:\n"
        '        t.get_item(Key={"PK": sid})\n'
    ),
    "hash-join": (
        'def f(session_id):\n    return "#".join(("conversation", session_id))\n'
    ),
    "percent-format": 'def f(session_id):\n    return "conversation#%s" % session_id\n',
    "a-user-sounding-sort-key": (
        "def f(t, session_id, caller_ts):\n"
        '    t.put_item(Item={"PK": session_id, "SK": caller_ts})\n'
    ),
    "either-branch-may-be-the-session": (
        "def f(t, session_id, owner_hint):\n"
        '    t.get_item(Key={"PK": session_id or owner_hint})\n'
    ),
    "a-session-or-a-user": (
        "def f(t, session_id, owner_sub):\n"
        '    t.get_item(Key={"PK": session_id or owner_sub})\n'
    ),
    "self-attribute-across-methods": (
        "class C:\n"
        "    def __init__(self, session_id):\n"
        '        self.key = {"PK": session_id}\n'
        "    def read(self, t):\n"
        "        t.get_item(Key=self.key)\n"
    ),
    "comprehension-of-keys": (
        "def f(c, session_ids):\n"
        '    c.batch_get_item(RequestItems={"T": {"Keys": [{"PK": s} for s in session_ids]}})\n'
    ),
    "comprehension-over-anything": (
        "def f(c, session_id):\n"
        '    c.batch_get_item(RequestItems={"T": {"Keys": [{"PK": session_id} for _ in range(1)]}})\n'
    ),
    "comprehension-through-a-name": (
        "def f(c, session_ids):\n"
        '    keys = [{"PK": s} for s in session_ids]\n'
        '    c.batch_get_item(RequestItems={"T": {"Keys": keys}})\n'
    ),
    "dict-comprehension": (
        'def f(t, session_id):\n    t.get_item(Key={k: session_id for k in ["PK"]})\n'
    ),
    "update-keywords": (
        "def f(t, session_id):\n"
        "    key = {}\n"
        "    key.update(PK=session_id)\n"
        "    t.get_item(Key=key)\n"
    ),
    "update-dict": (
        "def f(t, session_id):\n"
        "    key = {}\n"
        '    key.update({"PK": session_id})\n'
        "    t.get_item(Key=key)\n"
    ),
    "setdefault": (
        "def f(t, session_id):\n"
        "    key = {}\n"
        '    key.setdefault("PK", session_id)\n'
        "    t.get_item(Key=key)\n"
    ),
    "merge": (
        'def f(t, session_id):\n    t.get_item(Key={"SK": "x"} | {"PK": session_id})\n'
    ),
    "merge-in-place": (
        "def f(t, session_id):\n"
        '    key = {"SK": "x"}\n'
        '    key |= {"PK": session_id}\n'
        "    t.get_item(Key=key)\n"
    ),
    "a-user-word-in-a-literal-prefix": (
        'def f(t, session_id):\n    t.get_item(Key={"PK": f"userId#{session_id}"})\n'
    ),
    "a-user-word-as-a-literal-sort-key": (
        'def f(t, session_id):\n    t.get_item(Key={"PK": session_id, "SK": "sub"})\n'
    ),
    "closure-over-a-local": (
        "def f(t, event):\n"
        '    sid = event["sessionId"]\n'
        "    def read():\n"
        '        return t.get_item(Key={"PK": sid})\n'
        "    return read()\n"
    ),
    "self-attribute-holding-the-id": (
        "class C:\n"
        "    def __init__(self, session_id):\n"
        "        self.sid = session_id\n"
        "    def read(self, t):\n"
        '        t.get_item(Key={"PK": self.sid})\n'
    ),
    "a-user-message-timestamp-is-not-a-user": (
        "def f(t, session_id, user_ts):\n"
        '    t.put_item(Item={"PK": session_id, "SK": user_ts})\n'
    ),
}
CLEAN = {
    "memory-key-by-user-and-session": (
        'def f(self):\n    return f"conversation#{self.user_id}#{self.session_id}"\n'
    ),
    "sessions-table": (
        "def f(t, user_id, session_id):\n"
        '    t.get_item(Key={"userId": user_id, "sessionId": session_id})\n'
    ),
    "non-key-attribute": (
        'def f(t, pk, session_id):\n    t.put_item(Item={"PK": pk, "session_id": session_id})\n'
    ),
    "concatenated-with-the-user": (
        'def f(user_id, session_id):\n    return "c#" + user_id + "#" + session_id\n'
    ),
    "every-branch-is-a-user": (
        "def f(user_id, owner_sub, session_id):\n"
        '    return f"{user_id or owner_sub}#{session_id}"\n'
    ),
    "partiql-with-the-user": (
        "def f(c, user_id, session_id):\n"
        '    c.execute_statement(Statement="SELECT * FROM T WHERE userId=? AND sessionId=?", '
        'Parameters=[{"S": user_id}, {"S": session_id}])\n'
    ),
    "a-field-read-by-a-user-name": (
        "def f(t, claims, session_id):\n"
        '    t.get_item(Key={"PK": f"{claims[\'sub\']}#{session_id}"})\n'
    ),
    "arithmetic": "def f(session_id, n):\n    return n + 1\n",
    "a-log-line": 'def f(session_id):\n    print(f"session {session_id}")\n',
    "session-sort-key-under-a-user-partition": (
        "def f(t, user_id, session_id):\n"
        '    t.put_item(Item={"PK": f"USER#{user_id}", "SK": f"SESSION#{session_id}"})\n'
    ),
    "owner-in-the-key": (
        "def f(t, owner_sub, session_id):\n"
        '    t.get_item(Key={"PK": f"{owner_sub}#{session_id}"})\n'
    ),
}


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(FLAGGED))
def test_the_rule_flags(name):
    assert sk.scan_source("probe.py", FLAGGED[name], _KEYS), name


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(CLEAN))
def test_the_rule_leaves_alone(name):
    assert not sk.scan_source("probe.py", CLEAN[name], _KEYS), name


@pytest.mark.unit
def test_an_unparseable_file_is_an_error_not_a_clean_file(tmp_path):
    (tmp_path / "broken.py").write_text("def f(:\n")
    with pytest.raises(SyntaxError):
        sk.scan_paths(tmp_path, ["broken.py"], _KEYS)
