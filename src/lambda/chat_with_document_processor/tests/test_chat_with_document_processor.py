# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the chat_with_document_processor Lambda.

Covers the critical behaviors of the processor:

1. **Happy path (streaming)** — Bedrock ``converse_stream`` yields several
   deltas; the processor publishes status → stream × N → final events in
   order, with the final text equal to the concatenation of deltas.
2. **RBAC scope enforcement** — with the caller's ``allowedConfigVersions``
   resolved through the real UsersTable lookup, an out-of-scope (or unstamped)
   document publishes a single ``assistant_error`` and never reaches Bedrock,
   while an in-scope one is allowed. The index and key the lookup names are
   checked against ``template.yaml``, and the key attribute against the writer
   that stores it — a query naming an index the table does not declare fails only
   at runtime, and one naming an attribute nobody writes does not fail at all.
3. **RBAC fail-closed** — when the scope cannot be *evaluated* (no
   ``USERS_TABLE_NAME``, no caller email, any DynamoDB failure, an event
   carrying no ``identity`` key), the processor denies rather than treating the
   caller as unrestricted. The single unrestricted-on-no-identity path is an
   explicit ``identity: None`` from a transport that verified no caller, and it
   must log its stand-down every turn.
4. **Missing fields** — incomplete events return early with ``assistant_error``.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


@pytest.fixture(autouse=True)
def _reload_handler():
    """Ensure each test imports a fresh module so patches stick."""
    if "index" in sys.modules:
        del sys.modules["index"]
    # Adjust sys.path so `import index` works whether pytest is invoked
    # from the repo root or from the Lambda directory.
    import os

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if here not in sys.path:
        sys.path.insert(0, here)
    import index  # noqa: F401

    yield
    if "index" in sys.modules:
        del sys.modules["index"]


def _make_stream_events(deltas: list[str]) -> list[dict]:
    events: list[dict] = [{"messageStart": {"role": "assistant"}}]
    for chunk in deltas:
        events.append({"contentBlockDelta": {"delta": {"text": chunk}}})
    events.append({"contentBlockStop": {}})
    events.append({"messageStop": {"stopReason": "end_turn"}})
    return events


#: The UsersTable indexes this double will serve a Query from. Anything else is
#: answered the way DynamoDB answers it — with a ValidationException — because a
#: stub that accepts ANY ``IndexName`` is what let a query against a
#: never-declared ``SubIndex`` sit in this module for months while the suite
#: stayed green. ``test_scope_index_is_declared_in_the_template`` pins the same
#: name against ``template.yaml``, so the two cannot drift together.
_DECLARED_USERS_TABLE_INDEXES = {"EmailIndex": "email"}


def _dynamodb_resource(
    tracking_table,
    users_items: list[dict] | None = None,
    users_query_error: Exception | None = None,
    users_store: dict | None = None,
    users_get_item_error: Exception | None = None,
):
    """A ``boto3.resource('dynamodb')`` double that dispatches by table name.

    The processor reads TWO tables: TrackingTable (the document) and UsersTable
    (the RBAC scope). A single MagicMock for both makes the scope lookup return
    a MagicMock and only *accidentally* fail open, which would hide a
    regression in the fail-closed contract. Dispatching keeps the real
    ``_get_user_allowed_config_versions`` body under test.

    The UsersTable double validates the query the way the service does: an
    ``IndexName`` the table does not declare, or a key condition on an attribute
    that is not that index's hash key, raises instead of returning an empty page.
    ``users_query_error`` injects a failure for the fail-closed tests.

    It serves **both** key spaces the lookup reads. ``users_items`` is the
    ``EmailIndex`` query page; ``users_store`` maps a raw ``PK`` to its item, which
    is how the ``sub`` join is modelled — a ``SUB#<sub>`` pointer carrying a
    ``userId``, and the ``USER#<userId>`` row it names. Leaving ``get_item``
    unmodelled would answer with a truthy Mock, so a test could pass against a row
    that does not exist. ``users_get_item_error`` injects a failure on that leg
    alone, which is the case the email leg cannot cover.
    """
    users_table = MagicMock()

    def _query(**kwargs):
        if users_query_error is not None:
            raise users_query_error
        index_name = kwargs.get("IndexName")
        key_attr = _DECLARED_USERS_TABLE_INDEXES.get(index_name)
        if key_attr is None:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": (
                            f"The table does not have the specified index: "
                            f"{index_name}"
                        ),
                    }
                },
                "Query",
            )
        condition = kwargs.get("KeyConditionExpression")
        queried_attr = getattr(
            getattr(condition, "_values", [None])[0], "name", None
        )
        assert queried_attr == key_attr, (
            f"{index_name} is keyed on {key_attr!r}, but the query conditions on "
            f"{queried_attr!r} — DynamoDB would reject this"
        )
        return {"Items": list(users_items or [])}

    users_table.query.side_effect = _query

    _store = dict(users_store or {})

    def _get_item(Key):
        if users_get_item_error is not None:
            raise users_get_item_error
        assert set(Key) == {"PK", "SK"} and Key["PK"] == Key["SK"], (
            f"the UsersTable is keyed on PK and SK; got {Key!r}"
        )
        return {"Item": _store[Key["PK"]]} if Key["PK"] in _store else {}

    users_table.get_item.side_effect = _get_item
    # Captured eagerly: tests that override USERS_TABLE_NAME to exercise the
    # unset case must not also repoint this dispatch at the tracking table.
    users_table_name = os.environ["USERS_TABLE_NAME"]

    def _table(name):
        return users_table if name == users_table_name else tracking_table

    resource = MagicMock()
    resource.Table.side_effect = _table
    return resource


#: An ``identity`` shaped the way a transport that verified the caller's claims
#: supplies it. The processor resolves the scope from the email in here.
_VERIFIED_IDENTITY = {"claims": {"email": "scoped.user@example.com"}}


def _install_capture_sink(index, publishes: list[dict]) -> None:
    """Install an emission sink on the processor that records every event.

    The streaming endpoint installs a sink via ``set_sink``; tests do the same
    and assert against the captured events. Each captured dict exposes both the
    snake_case key emitted by the processor (``is_processing``) and the
    camelCase alias (``isProcessing``) the original assertions used.
    """

    def _sink(
        session_id,
        method,
        status,
        content,
        role="assistant",
        model_id="",
        is_processing=True,
    ):
        publishes.append(
            {
                "sessionId": session_id,
                "method": method,
                "status": status,
                "content": content,
                "role": role,
                "modelId": model_id,
                "is_processing": is_processing,
                "isProcessing": is_processing,
            }
        )

    index.set_sink(_sink)


class TestProcessorHappyPath:
    @pytest.mark.unit
    def test_streams_deltas_and_publishes_final(self):
        import index

        tracking_table = MagicMock()
        tracking_table.get_item.return_value = {
            "Item": {
                "PK": "doc#uploads/x.pdf",
                "SK": "none",
                "ConfigVersion": "default",
                "Pages": [
                    {"Id": 1, "TextUri": "s3://output-bucket/uploads/x.pdf/pages/1.txt"},
                ],
            }
        }
        dyn_resource = _dynamodb_resource(tracking_table)

        # Pretend the cached fulltext already exists so we don't need to
        # exercise the page-assembly branch.
        s3 = MagicMock()
        s3.head_object.return_value = {}
        s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=b"FULL DOC TEXT")),
        }

        # Bedrock mock: return a streaming response with several deltas.
        bedrock = MagicMock()
        bedrock.converse_stream.return_value = {
            "stream": iter(_make_stream_events(["Hello, ", "world", "!"]))
        }

        # Capture every emitted event so we can assert ordering.
        publishes: list[dict] = []
        _install_capture_sink(index, publishes)

        with (
            patch.object(index, "_s3", s3),
            patch.object(index, "_dynamodb", dyn_resource),
            patch.object(index, "_get_bedrock_runtime", return_value=bedrock),
            patch.object(
                index, "_resolve_chat_settings",
                return_value={
                    "model_id": "us.anthropic.claude-opus-4-8:1m",
                    "system_prompt": "sys",
                    "temperature": 0.0,
                    "max_tokens": 128,
                },
            ),
            # Bypass streaming throttling so every delta triggers a publish.
            patch.object(index, "STREAM_FLUSH_INTERVAL_S", 0.0),
            patch.object(index, "STREAM_FLUSH_CHAR_THRESHOLD", 1),
        ):
            result = index.handler(
                {
                    "sessionId": "s-1",
                    "turnId": "t-1",
                    "prompt": "what is this?",
                    "s3Uri": "uploads/x.pdf",
                    "modelId": "",
                    "identity": _VERIFIED_IDENTITY,
                },
                None,
            )

        assert result == {"ok": True, "turnId": "t-1"}

        methods = [p.get("method") for p in publishes]
        # We expect: LOADING status, CALLING status, STREAMING status (on first
        # delta), one or more assistant_stream deltas, then assistant_final.
        assert methods[0] == "assistant_status"
        assert publishes[0]["status"] == "LOADING_DOCUMENT"
        assert "assistant_status" in methods
        assert methods.count("assistant_stream") >= 1
        assert methods[-1] == "assistant_final"

        # Final text should be the concatenation of streamed deltas.
        final = publishes[-1]
        assert final["status"] == "COMPLETE"
        assert final["content"] == "Hello, world!"
        assert final["isProcessing"] is False


class TestProcessorOpenAIResponses:
    @pytest.mark.unit
    def test_openai_model_streams_via_responses_api_not_converse_stream(self):
        """An openai.gpt-5.* chat model streams via the Responses generator."""
        import index

        tracking_table = MagicMock()
        tracking_table.get_item.return_value = {
            "Item": {
                "PK": "doc#uploads/x.pdf",
                "SK": "none",
                "ConfigVersion": "default",
                "Pages": [
                    {"Id": 1, "TextUri": "s3://output-bucket/uploads/x.pdf/pages/1.txt"},
                ],
            }
        }
        dyn_resource = _dynamodb_resource(tracking_table)

        s3 = MagicMock()
        s3.head_object.return_value = {}
        s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=b"FULL DOC TEXT")),
        }

        # converse_stream must NOT be called for OpenAI models.
        bedrock_runtime = MagicMock()

        publishes: list[dict] = []
        _install_capture_sink(index, publishes)

        # Fake streaming generator: several text deltas, then a final metering dict.
        captured: dict = {}

        def _fake_stream(**kwargs):
            captured.update(kwargs)
            yield "GPT-5 "
            yield "answer."
            yield {
                "metering": {"ChatWithDocument/bedrock/openai.gpt-5.4": {"requests": 1}},
                "text": "GPT-5 answer.",
            }

        with (
            patch.object(index, "_s3", s3),
            patch.object(index, "_dynamodb", dyn_resource),
            patch.object(index, "_get_bedrock_runtime", return_value=bedrock_runtime),
            patch("idp_common.bedrock.stream_responses_api", _fake_stream),
            # Bypass throttling so each delta publishes.
            patch.object(index, "STREAM_FLUSH_INTERVAL_S", 0.0),
            patch.object(index, "STREAM_FLUSH_CHAR_THRESHOLD", 1),
            patch.object(
                index,
                "_resolve_chat_settings",
                return_value={
                    "model_id": "openai.gpt-5.4",
                    "system_prompt": "sys",
                    "temperature": 0.0,
                    "max_tokens": 128,
                    "reasoning_effort": "high",
                },
            ),
        ):
            result = index.handler(
                {
                    "sessionId": "s-1",
                    "turnId": "t-1",
                    "prompt": "what is this?",
                    "s3Uri": "uploads/x.pdf",
                    "modelId": "",
                    "identity": _VERIFIED_IDENTITY,
                },
                None,
            )

        assert result == {"ok": True, "turnId": "t-1"}
        # Routed to the Responses streaming generator, not converse_stream.
        bedrock_runtime.converse_stream.assert_not_called()
        assert captured["model_id"] == "openai.gpt-5.4"
        assert captured["reasoning_effort"] == "high"
        assert captured["context"] == "ChatWithDocument"

        methods = [p.get("method") for p in publishes]
        # Multiple stream deltas (throttling bypassed), then a final event.
        assert methods.count("assistant_stream") >= 2
        assert methods[-1] == "assistant_final"
        assert publishes[-1]["content"] == "GPT-5 answer."


def _run_scope_turn(
    index,
    event_extra: dict,
    users_items: list[dict] | None = None,
    users_query_error: Exception | None = None,
    users_table_name: str | None = None,
    doc_config_version: str | None = "secret-v1",
    users_store: dict | None = None,
    users_get_item_error: Exception | None = None,
) -> tuple:
    """Run one chat turn against a restricted document and report the outcome.

    Returns ``(result, publishes, bedrock, users_table)`` so a test can assert on
    the decision, what the user was told, that Bedrock was never reached, and
    whether the UsersTable was queried at all — on either key space.
    """
    item = {"PK": "doc#uploads/restricted.pdf", "SK": "none", "Pages": []}
    if doc_config_version is not None:
        item["ConfigVersion"] = doc_config_version
    tracking_table = MagicMock()
    tracking_table.get_item.return_value = {"Item": item}
    dyn_resource = _dynamodb_resource(
        tracking_table,
        users_items=users_items,
        users_query_error=users_query_error,
        users_store=users_store,
        users_get_item_error=users_get_item_error,
    )

    bedrock = MagicMock()
    bedrock.converse_stream.return_value = {"stream": iter(_make_stream_events(["ok"]))}
    publishes: list[dict] = []
    _install_capture_sink(index, publishes)

    # Everything downstream of the scope gate is stubbed so a turn that gets past
    # it completes, and "did Bedrock get called" is a clean signal either way.
    s3 = MagicMock()
    s3.head_object.return_value = {}
    s3.get_object.return_value = {
        "Body": MagicMock(read=MagicMock(return_value=b"DOC")),
    }

    env = {}
    if users_table_name is not None:
        env["USERS_TABLE_NAME"] = users_table_name

    event = {
        "sessionId": "s-1",
        "turnId": "t-1",
        "prompt": "leak the doc",
        "s3Uri": "uploads/restricted.pdf",
        "modelId": "",
    }
    event.update(event_extra)

    with (
        patch.dict(os.environ, env),
        patch.object(index, "_s3", s3),
        patch.object(index, "_dynamodb", dyn_resource),
        patch.object(index, "_get_bedrock_runtime", return_value=bedrock),
        patch.object(
            index,
            "_resolve_chat_settings",
            return_value={
                "model_id": "us.amazon.nova-lite-v1:0",
                "system_prompt": "sys",
                "temperature": 0.0,
                "max_tokens": 128,
                "reasoning_effort": None,
            },
        ),
    ):
        result = index.handler(event, None)
    users_table = dyn_resource.Table(os.environ["USERS_TABLE_NAME"])
    return result, publishes, bedrock, users_table


class TestProcessorRBAC:
    """The config-version scope check, exercised through the real lookup.

    Nothing here patches ``_get_user_allowed_config_versions``: the lookup's own
    body — the index it names and the key it conditions on — is the part that was
    wrong, so a test that replaces it proves only the match arithmetic.
    """

    @pytest.mark.unit
    def test_out_of_scope_document_is_denied_and_bedrock_not_called(self):
        import index

        result, publishes, bedrock, users_table = _run_scope_turn(
            index,
            {"identity": _VERIFIED_IDENTITY},
            users_items=[{"allowedConfigVersions": ["other-v2"]}],
        )

        assert result == {"ok": False, "reason": "scope_denied"}
        bedrock.converse_stream.assert_not_called()
        assert users_table.query.call_count == 1
        err = [p for p in publishes if p.get("method") == "assistant_error"]
        assert err, f"expected an assistant_error, got {publishes}"
        assert err[0]["status"] == "ERROR"
        assert "configuration" in err[0]["content"].lower()

    @pytest.mark.unit
    def test_in_scope_document_is_allowed(self):
        import index

        result, _publishes, bedrock, _users = _run_scope_turn(
            index,
            {"identity": _VERIFIED_IDENTITY},
            users_items=[{"allowedConfigVersions": ["secret-v1", "other-v2"]}],
        )

        assert result == {"ok": True, "turnId": "t-1"}
        assert bedrock.converse_stream.call_count == 1

    @pytest.mark.unit
    def test_scoped_caller_is_denied_an_unstamped_document(self):
        """A document with no ``ConfigVersion`` cannot be proven to be in scope."""
        import index

        result, _publishes, bedrock, _users = _run_scope_turn(
            index,
            {"identity": _VERIFIED_IDENTITY},
            users_items=[{"allowedConfigVersions": ["secret-v1"]}],
            doc_config_version=None,
        )

        assert result == {"ok": False, "reason": "scope_denied"}
        bedrock.converse_stream.assert_not_called()

    @pytest.mark.unit
    def test_query_names_the_declared_index_and_key(self):
        import index

        _result, _publishes, _bedrock, users_table = _run_scope_turn(
            index, {"identity": _VERIFIED_IDENTITY}, users_items=[]
        )

        kwargs = users_table.query.call_args.kwargs
        assert kwargs["IndexName"] == index.USERS_TABLE_SCOPE_INDEX == "EmailIndex"
        assert index.USERS_TABLE_SCOPE_KEY == "email"
        # The value queried is the email from the verified claims, not any other
        # identifier: it is the only one that joins to a UsersTable row.
        assert kwargs["KeyConditionExpression"]._values[1] == (
            _VERIFIED_IDENTITY["claims"]["email"]
        )

    @pytest.mark.unit
    def test_scope_index_is_declared_in_the_template(self):
        """The index the lookup names must be one ``UsersTable`` actually declares.

        This is the check that was missing. An index name is just a string: a
        wrong one raises only at runtime, against a real table, and the query is
        stubbed everywhere else. Reading the template here ties the two together
        so neither can move alone.
        """
        import index

        repo_root = Path(__file__).resolve().parents[4]
        template = (repo_root / "template.yaml").read_text()
        block = re.search(
            r"\n  UsersTable:\n(?P<body>(?:    .*\n|\n)+)", template
        )
        assert block, "UsersTable resource not found in template.yaml"
        body = block.group("body")

        declared = re.findall(r"- IndexName: (\S+)", body)
        assert index.USERS_TABLE_SCOPE_INDEX in declared, (
            f"the scope lookup queries {index.USERS_TABLE_SCOPE_INDEX!r} but "
            f"UsersTable declares {declared} — a query against an index the "
            f"table does not have raises ValidationException on every call"
        )
        # The key it conditions on must be that index's HASH key, and must be in
        # AttributeDefinitions — a GSI cannot be keyed on an undeclared attribute,
        # and an attribute no writer stores would return an empty page rather
        # than erroring, which is worse.
        attr_defs = re.findall(r"- AttributeName: (\S+)", body)
        assert index.USERS_TABLE_SCOPE_KEY in attr_defs, (
            f"{index.USERS_TABLE_SCOPE_KEY!r} is not an AttributeDefinition on "
            f"UsersTable (has {attr_defs})"
        )
        index_block = body.split(f"- IndexName: {index.USERS_TABLE_SCOPE_INDEX}", 1)[1]
        hash_key = re.search(
            r"- AttributeName: (\S+)\s*\n\s*KeyType: HASH", index_block
        )
        assert hash_key and hash_key.group(1) == index.USERS_TABLE_SCOPE_KEY, (
            f"{index.USERS_TABLE_SCOPE_INDEX} is not keyed on "
            f"{index.USERS_TABLE_SCOPE_KEY!r}"
        )

    @pytest.mark.unit
    def test_writer_stores_the_attribute_the_index_is_keyed_on(self):
        """Something must actually write the key attribute onto a user row.

        A GSI on an attribute nobody writes does not error — it returns an empty
        page, which this lookup reads as "unrestricted". That failure mode is
        silent, so it is pinned here rather than left to a live test.
        """
        import index

        repo_root = Path(__file__).resolve().parents[4]
        writer = (repo_root / "src/lambda/user_management/index.py").read_text()
        assert f'"{index.USERS_TABLE_SCOPE_KEY}": ' in writer, (
            f"user_management does not write a {index.USERS_TABLE_SCOPE_KEY!r} "
            f"attribute onto the user record, so "
            f"{index.USERS_TABLE_SCOPE_INDEX} would never match a row"
        )


class TestProcessorScopeFailsClosed:
    """AppSec: an *unevaluatable* scope must DENY, not mean "unrestricted".

    Returning ``None`` (unrestricted) when the lookup cannot be evaluated
    silently disables config-version RBAC for every caller as soon as the stack
    wiring, the IAM grant or the index name drifts — AUTH.T07, fail-open scope
    lookup. The pii-anonymizer feature API (``_caller_allowed_versions``) is the
    reference for this contract.

    The one path that is deliberately unrestricted is an explicit ``identity:
    None`` from a transport that verified no caller at all
    (``test_null_identity_stands_the_check_down``). Every other failure denies.
    """

    @pytest.mark.unit
    def test_unset_users_table_denies_instead_of_unrestricted(self):
        import index

        result, publishes, bedrock, _users = _run_scope_turn(
            index, {"identity": _VERIFIED_IDENTITY}, users_table_name=""
        )

        assert result == {"ok": False, "reason": "scope_unavailable"}
        bedrock.converse_stream.assert_not_called()
        err = [p for p in publishes if p.get("method") == "assistant_error"]
        assert err, f"expected an assistant_error, got {publishes}"
        assert err[0]["status"] == "ERROR"
        assert err[0]["isProcessing"] is False

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "identity",
        [
            {"claims": {"cognito:groups": ["Viewer"]}},
            # Claims carrying every identifier EXCEPT an email, and a ``sub`` that
            # no pointer item records. This is the shape to hold the line on:
            # substituting a ``cognito:username`` or a ``username`` would query
            # EmailIndex with a value no user row carries — an empty page, which
            # reads as "unrestricted". And an unrecorded ``sub`` is not an answer
            # about this caller either: their row may simply predate the pointer
            # writer, and with no email there is no second key to try. Denying is
            # the only safe reading of both, so no fallback may be added.
            {
                "claims": {
                    "sub": "d47cb94a-1c2e-4f3a-9b8d-0e1f2a3b4c5d",
                    "cognito:username": "federated_d47cb94a",
                    "username": "federated_d47cb94a",
                },
                "username": "d47cb94a-1c2e-4f3a-9b8d-0e1f2a3b4c5d",
                "sub": "d47cb94a-1c2e-4f3a-9b8d-0e1f2a3b4c5d",
            },
            {"claims": {"email": ""}},
            {"claims": {}},
            # Present but empty. Distinct from `identity: None`, and must NOT
            # collapse into it: `if identity is None` broadened to `if not
            # identity` would stand the check down on this input.
            {},
        ],
        ids=["groups-only", "every-id-but-email", "empty-email", "no-claims", "empty"],
    )
    def test_identity_without_an_email_denies(self, identity):
        import index

        result, _publishes, bedrock, users_table = _run_scope_turn(
            index, {"identity": identity}
        )

        assert result == {"ok": False, "reason": "scope_unavailable"}
        bedrock.converse_stream.assert_not_called()
        # And no substituted identifier is ever put to the email-keyed index. The
        # ``sub``-carrying case does read the pointer key space — which is the key
        # space that indexes a ``sub`` — and still denies.
        users_table.query.assert_not_called()

    @pytest.mark.unit
    def test_a_diverged_email_still_resolves_the_scope_through_the_sub(self):
        """The residual this join exists to close.

        The caller's ``email`` claim no longer matches the address on their row —
        an IdP remapped it, they changed it, or it differs only in case. The email
        query therefore finds nothing, which on its own means "unrestricted" and
        silently lifts the restriction. The ``sub`` pointer finds the row anyway, so
        the out-of-scope document is still refused.
        """
        import index

        sub = "d47cb94a-1c2e-4f3a-9b8d-0e1f2a3b4c5d"
        result, _publishes, bedrock, users_table = _run_scope_turn(
            index,
            {"identity": {"claims": {"email": "Renamed.User@example.com", "sub": sub}}},
            # The email query matches nothing, exactly as it would in production.
            users_items=[],
            users_store={
                f"SUB#{sub}": {"userId": "u-1", "cognitoSub": sub},
                "USER#u-1": {
                    "userId": "u-1",
                    "email": "scoped.user@example.com",
                    "allowedConfigVersions": ["tenant-a"],
                },
            },
        )

        assert result == {"ok": False, "reason": "scope_denied"}
        bedrock.converse_stream.assert_not_called()
        # Resolved on the sub alone: the email leg is never reached.
        users_table.query.assert_not_called()

    @pytest.mark.unit
    def test_a_row_with_no_pointer_is_still_found_by_email(self):
        """The transition case: every row predates the pointer writer.

        A deployment upgrading to this code has no pointer items until its writer
        or back-fill runs. The email join must keep resolving those rows unchanged,
        or the upgrade silently lifts every scope it is meant to enforce.
        """
        import index

        result, _publishes, bedrock, users_table = _run_scope_turn(
            index,
            {
                "identity": {
                    "claims": {
                        "email": "scoped.user@example.com",
                        "sub": "d47cb94a-1c2e-4f3a-9b8d-0e1f2a3b4c5d",
                    }
                }
            },
            users_items=[{"allowedConfigVersions": ["tenant-a"]}],
            users_store={},
        )

        assert result == {"ok": False, "reason": "scope_denied"}
        bedrock.converse_stream.assert_not_called()
        users_table.query.assert_called_once()

    @pytest.mark.unit
    def test_a_pointer_read_failure_denies(self):
        """A failure on the new key space denies, like one on the old one.

        The ``sub`` leg is a second place the lookup can fail — a throttle, a
        permissions boundary allowing Query and not GetItem — and "cannot read"
        must not fall through to the email leg and then to "unrestricted".
        """
        import index

        result, _publishes, bedrock, _users = _run_scope_turn(
            index,
            {
                "identity": {
                    "claims": {
                        "email": "scoped.user@example.com",
                        "sub": "d47cb94a-1c2e-4f3a-9b8d-0e1f2a3b4c5d",
                    }
                }
            },
            users_items=[],
            users_get_item_error=Exception("AccessDeniedException: dynamodb:GetItem"),
        )

        assert result == {"ok": False, "reason": "scope_unavailable"}
        bedrock.converse_stream.assert_not_called()

    @pytest.mark.unit
    def test_caller_email_reads_only_the_email_claim(self):
        """Guard the absence of a fallback directly, at the resolution point.

        The parametrized handler cases above prove the outcome; this names the
        rule, so a diff that reintroduces ``or claims["sub"]`` fails against the
        rule rather than only against one of its consequences.
        """
        import index

        assert index._caller_email({"claims": {"email": "a@example.com"}}) == (
            "a@example.com"
        )
        assert (
            index._caller_email(
                {
                    "claims": {"sub": "a-uuid", "cognito:username": "a-name"},
                    "username": "another-name",
                    "sub": "a-uuid",
                }
            )
            == ""
        )

    @pytest.mark.unit
    def test_absent_identity_key_denies(self):
        """An event that says nothing about the caller is a wiring regression.

        It is NOT the same as a transport reporting that it verified nobody, and
        must not be read as one — otherwise dropping the field from either
        producer would silently switch the control off.
        """
        import index

        result, _publishes, bedrock, users_table = _run_scope_turn(index, {})

        assert result == {"ok": False, "reason": "scope_unavailable"}
        bedrock.converse_stream.assert_not_called()
        users_table.query.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "identity", ["not-a-dict", ["also", "wrong"], 7], ids=["str", "list", "int"]
    )
    def test_unusable_identity_type_denies(self, identity):
        """An identity of the wrong type is unreadable, not permission to proceed.

        The producer side is held to the same rule
        (`test_forwarded_identity_denies_on_a_non_dict_identity`), so neither end
        can start treating a broken identity as the stand-down marker.
        """
        import index

        result, _publishes, bedrock, users_table = _run_scope_turn(
            index, {"identity": identity}
        )

        assert result == {"ok": False, "reason": "scope_unavailable"}
        bedrock.converse_stream.assert_not_called()
        users_table.query.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "error",
        [
            ClientError(
                {"Error": {"Code": "ValidationException", "Message": "bad index"}},
                "Query",
            ),
            ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no grant"}},
                "Query",
            ),
            ClientError(
                {
                    "Error": {
                        "Code": "ProvisionedThroughputExceededException",
                        "Message": "throttled",
                    }
                },
                "Query",
            ),
            RuntimeError("anything else"),
        ],
        ids=["validation", "access-denied", "throttled", "unexpected"],
    )
    def test_any_dynamodb_failure_denies(self, error):
        """No lookup failure may be read as "unrestricted" — that was the defect.

        Parametrized over the shapes that actually occur (a wrong index name, a
        missing IAM grant, throttling) plus an unexpected exception, because a
        single case would let a narrowed ``except`` reintroduce the fail-open for
        the others.
        """
        import index

        result, publishes, bedrock, _users = _run_scope_turn(
            index, {"identity": _VERIFIED_IDENTITY}, users_query_error=error
        )

        assert result == {"ok": False, "reason": "scope_unavailable"}
        bedrock.converse_stream.assert_not_called()
        assert any(p.get("method") == "assistant_error" for p in publishes)

    @pytest.mark.unit
    def test_null_identity_stands_the_check_down(self, caplog):
        """The one unrestricted-on-no-identity path, and it must be loud.

        A transport that verified no caller identity says so with an explicit
        null. There is no principal to resolve a scope for, so the turn proceeds
        — but never silently, and without pretending to have consulted the table.
        """
        import index

        with caplog.at_level("WARNING"):
            result, _publishes, bedrock, users_table = _run_scope_turn(
                index, {"identity": None}
            )

        assert result == {"ok": True, "turnId": "t-1"}
        assert bedrock.converse_stream.call_count == 1
        users_table.query.assert_not_called()
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelname == "WARNING" and "scope NOT enforced" in r.getMessage()
        ]
        assert warnings, f"the stand-down must be logged; got {caplog.records}"
        assert "GAP-07" in warnings[0]

    @pytest.mark.unit
    def test_lookup_helper_raises_rather_than_returning_none(self):
        """Guard the helper's contract directly, not just the handler branch."""
        import index

        with patch.dict(os.environ, {"USERS_TABLE_NAME": ""}):
            with pytest.raises(index.ScopeLookupError):
                index._get_user_allowed_config_versions("someone@example.com")

        with patch.dict(os.environ, {"USERS_TABLE_NAME": "users-table"}):
            with pytest.raises(index.ScopeLookupError):
                index._get_user_allowed_config_versions("")

    @pytest.mark.unit
    def test_absent_scope_row_is_still_unrestricted(self):
        """Scoping is opt-in: a user with no UsersTable row is NOT denied.

        The fail-closed contract must not turn "this user has no restriction"
        into a denial, or it locks out every ordinary user.
        """
        import index

        tracking_table = MagicMock()
        dyn_resource = _dynamodb_resource(tracking_table, users_items=[])

        with patch.object(index, "_dynamodb", dyn_resource):
            assert (
                index._get_user_allowed_config_versions("nobody@example.com") is None
            )

    @pytest.mark.unit
    def test_scope_row_returns_allowed_versions(self):
        import index

        tracking_table = MagicMock()
        dyn_resource = _dynamodb_resource(
            tracking_table,
            users_items=[{"allowedConfigVersions": ["lending", "uc?-prod"]}],
        )

        with patch.object(index, "_dynamodb", dyn_resource):
            assert index._get_user_allowed_config_versions("a@example.com") == [
                "lending",
                "uc?-prod",
            ]

    @pytest.mark.unit
    def test_lookup_has_no_unrestricted_on_failure_path(self):
        """Source guard: the lookup's ``except`` must raise, never return None.

        ⚠️ This is documentation with an assertion attached, **not** the control
        that stops the fail-open coming back. It reads one function's exception
        handlers, so it is defeated by any rewrite that moves the swallow
        elsewhere — a ``contextlib.suppress``, a raising decoy handler beside a
        returning one, or lifting the query into a helper. Both of those were
        demonstrated against it. ``test_any_dynamodb_failure_denies`` is the real
        control, because it asserts the outcome regardless of how the code is
        shaped; this one exists to state the rule in the place a future diff will
        be read.
        """
        import ast
        import inspect
        import textwrap

        import index

        src = textwrap.dedent(
            inspect.getsource(index._get_user_allowed_config_versions)
        )
        handlers = [
            node
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.ExceptHandler)
        ]
        assert handlers, "expected the lookup to handle the query's failures"
        for handler in handlers:
            returns = [
                node for node in ast.walk(handler) if isinstance(node, ast.Return)
            ]
            assert not returns, (
                "the scope lookup's failure path must raise ScopeLookupError, not "
                "return (every caller reads a None return as 'unrestricted')"
            )
            raises = [
                node.exc
                for node in ast.walk(handler)
                if isinstance(node, ast.Raise) and node.exc is not None
            ]
            assert any(
                getattr(getattr(exc, "func", None), "id", None) == "ScopeLookupError"
                for exc in raises
            ), "the lookup's except clause must raise ScopeLookupError"


class TestProcessorValidation:
    @pytest.mark.unit
    def test_missing_prompt_publishes_error(self):
        import index

        publishes: list[dict] = []
        _install_capture_sink(index, publishes)

        result = index.handler(
            {
                "sessionId": "s-1",
                "turnId": "t-1",
                "prompt": "",  # missing
                "s3Uri": "uploads/x.pdf",
            },
            None,
        )

        assert result == {"ok": False, "reason": "invalid_event"}
        assert publishes and publishes[0]["method"] == "assistant_error"


class TestProcessorModelIdSuffixes:
    """Verify the processor's model-ID-suffix handling for Bedrock Converse:

      * ``:1m``        → strip suffix, add ``additionalModelRequestFields.anthropic_beta``
      * ``:priority``  → strip suffix, pass ``performanceConfig={"latency": "priority"}``
      * ``:flex``      → strip suffix, pass ``performanceConfig={"latency": "flex"}``
      * Any combination of the above

    These must match idp_common.bedrock.client.BedrockClient behavior so the
    Chat-with-Document feature supports the same model ID forms used
    elsewhere in the pipeline.
    """

    def _invoke_with_model(
        self, model_id: str, reasoning_effort: str | None = None
    ) -> dict:
        """Run the processor end-to-end with the given model ID and return the
        kwargs that Bedrock ``converse_stream`` was called with.
        """
        import index

        tracking_table = MagicMock()
        tracking_table.get_item.return_value = {
            "Item": {
                "PK": "doc#uploads/x.pdf",
                "SK": "none",
                "ConfigVersion": "default",
                "Pages": [
                    {"Id": 1, "TextUri": "s3://output-bucket/uploads/x.pdf/pages/1.txt"},
                ],
            }
        }
        dyn_resource = _dynamodb_resource(tracking_table)

        s3 = MagicMock()
        s3.head_object.return_value = {}
        s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=b"DOC")),
        }

        bedrock = MagicMock()
        bedrock.converse_stream.return_value = {
            "stream": iter(_make_stream_events(["ok"]))
        }

        _install_capture_sink(index, [])

        with (
            patch.object(index, "_s3", s3),
            patch.object(index, "_dynamodb", dyn_resource),
            patch.object(index, "_get_bedrock_runtime", return_value=bedrock),
            patch.object(
                index, "_resolve_chat_settings",
                return_value={
                    "model_id": model_id,
                    "system_prompt": "sys",
                    "temperature": 0.0,
                    "max_tokens": 128,
                    "reasoning_effort": reasoning_effort,
                },
            ),
            patch.object(index, "STREAM_FLUSH_INTERVAL_S", 0.0),
            patch.object(index, "STREAM_FLUSH_CHAR_THRESHOLD", 1),
        ):
            result = index.handler(
                {
                    "sessionId": "s-tier",
                    "turnId": "t-tier",
                    "prompt": "q?",
                    "s3Uri": "uploads/x.pdf",
                    "modelId": "",
                    "identity": _VERIFIED_IDENTITY,
                },
                None,
            )

        assert result["ok"] is True, f"processor failed: {result}"
        assert bedrock.converse_stream.call_count == 1
        return bedrock.converse_stream.call_args.kwargs

    @pytest.mark.unit
    def test_1m_suffix_stripped_and_anthropic_beta_passed(self):
        kwargs = self._invoke_with_model("us.anthropic.claude-opus-4-8:1m")
        # Suffix stripped from modelId
        assert kwargs["modelId"] == "us.anthropic.claude-opus-4-8"
        # Beta flag sent via additionalModelRequestFields
        assert kwargs.get("additionalModelRequestFields") == {
            "anthropic_beta": ["context-1m-2025-08-07"]
        }
        # Claude 4.7+ → temperature omitted
        assert "temperature" not in kwargs["inferenceConfig"]
        # No service tier for :1m alone
        assert "serviceTier" not in kwargs
        assert "performanceConfig" not in kwargs

    @pytest.mark.unit
    def test_priority_suffix_stripped_and_service_tier_passed(self):
        kwargs = self._invoke_with_model("global.amazon.nova-2-lite-v1:0:priority")
        # Suffix stripped from modelId
        assert kwargs["modelId"] == "global.amazon.nova-2-lite-v1:0"
        # serviceTier populated (NOT performanceConfig — those are separate
        # Bedrock params; see the processor's _invoke_bedrock_stream_and_publish
        # docstring).
        assert kwargs.get("serviceTier") == {"type": "priority"}
        assert "performanceConfig" not in kwargs
        # Non-Claude-4.7 model → temperature preserved
        assert kwargs["inferenceConfig"].get("temperature") == 0.0
        # No 1M beta flag
        assert "additionalModelRequestFields" not in kwargs

    @pytest.mark.unit
    def test_grok_temperature_omitted(self):
        """Grok hard-rejects `temperature` with a 400 naming the field, and
        chat.temperature always resolves to a float (never None) — so sending it
        would fail EVERY Grok chat turn. Regression guard for the gate having
        been keyed on is_claude_4_7_model instead of strips_sampling_params."""
        kwargs = self._invoke_with_model("us.xai.grok-4.6")
        assert kwargs["modelId"] == "us.xai.grok-4.6"
        assert "temperature" not in kwargs["inferenceConfig"]
        assert "topP" not in kwargs["inferenceConfig"]

    @pytest.mark.unit
    def test_grok_reasoning_effort_uses_reasoning_carrier(self):
        """Grok reads reasoning.effort; Claude's output_config.effort is silently
        ignored by it, so the wrong carrier would lose the setting invisibly."""
        kwargs = self._invoke_with_model("us.xai.grok-4.6", reasoning_effort="xhigh")
        arf = kwargs.get("additionalModelRequestFields") or {}
        assert arf.get("reasoning") == {"effort": "xhigh"}
        assert "output_config" not in arf

    @pytest.mark.unit
    def test_grok_rejects_claude_only_effort_value(self):
        """`max` is Claude-only — Grok 400s on it, and Bedrock ignores unknown
        additionalModelRequestFields keys, so it must be dropped not forwarded."""
        kwargs = self._invoke_with_model("us.xai.grok-4.6", reasoning_effort="max")
        arf = kwargs.get("additionalModelRequestFields") or {}
        assert "reasoning" not in arf

    @pytest.mark.unit
    def test_astra_temperature_omitted(self):
        """GPT-6 Astra hard-rejects `temperature` with a 400 naming the field, the
        same as Grok — so every Astra chat turn would fail without the gate."""
        kwargs = self._invoke_with_model("us.openai.gpt-6-astra")
        assert kwargs["modelId"] == "us.openai.gpt-6-astra"
        assert "temperature" not in kwargs["inferenceConfig"]
        assert "topP" not in kwargs["inferenceConfig"]

    @pytest.mark.unit
    def test_astra_reasoning_effort_uses_reasoning_carrier(self):
        """Astra reads reasoning.effort and REJECTS Claude's output_config."""
        kwargs = self._invoke_with_model(
            "us.openai.gpt-6-astra", reasoning_effort="xhigh"
        )
        arf = kwargs.get("additionalModelRequestFields") or {}
        assert arf.get("reasoning") == {"effort": "xhigh"}
        assert "output_config" not in arf

    @pytest.mark.unit
    def test_astra_accepts_max_effort_unlike_grok(self):
        """Astra and Grok share the carrier but not the vocabulary: `max` is valid
        for Astra and a 400 for Grok."""
        kwargs = self._invoke_with_model("us.openai.gpt-6-astra", reasoning_effort="max")
        arf = kwargs.get("additionalModelRequestFields") or {}
        assert arf.get("reasoning") == {"effort": "max"}

    @pytest.mark.unit
    def test_astra_rejects_gpt5_only_effort_value(self):
        """`minimal` is valid for GPT-5.x on the Responses API and rejected by
        Astra, so it must be dropped not forwarded."""
        kwargs = self._invoke_with_model(
            "us.openai.gpt-6-astra", reasoning_effort="minimal"
        )
        arf = kwargs.get("additionalModelRequestFields") or {}
        assert "reasoning" not in arf

    @pytest.mark.unit
    def test_claude_reasoning_effort_uses_output_config_carrier(self):
        """The other half of the carrier split — and proof that wiring effort on
        this path (it was previously resolved then dropped) works for Claude."""
        kwargs = self._invoke_with_model(
            "us.anthropic.claude-sonnet-5", reasoning_effort="high"
        )
        arf = kwargs.get("additionalModelRequestFields") or {}
        assert arf.get("output_config") == {"effort": "high"}
        assert "reasoning" not in arf

    @pytest.mark.unit
    def test_nova_gets_no_effort_field(self):
        """Nova has no effort control; an effort value must not reach it."""
        kwargs = self._invoke_with_model(
            "us.amazon.nova-pro-v1:0", reasoning_effort="high"
        )
        arf = kwargs.get("additionalModelRequestFields") or {}
        assert "reasoning" not in arf
        assert "output_config" not in arf
        assert kwargs["inferenceConfig"].get("temperature") == 0.0

    @pytest.mark.unit
    def test_flex_suffix_stripped_and_service_tier_passed(self):
        kwargs = self._invoke_with_model("eu.amazon.nova-2-lite-v1:0:flex")
        assert kwargs["modelId"] == "eu.amazon.nova-2-lite-v1:0"
        assert kwargs.get("serviceTier") == {"type": "flex"}
        assert "performanceConfig" not in kwargs

    @pytest.mark.unit
    def test_plain_model_id_no_extra_fields(self):
        kwargs = self._invoke_with_model("us.amazon.nova-lite-v1:0")
        assert kwargs["modelId"] == "us.amazon.nova-lite-v1:0"
        assert "serviceTier" not in kwargs
        assert "performanceConfig" not in kwargs
        assert "additionalModelRequestFields" not in kwargs
