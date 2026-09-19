# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for send_chat_document_message_resolver.

Exercises the three call paths:

1. **UI path** (``method="chat"``) — records session ownership in DDB,
   async-invokes the processor Lambda, returns a ``QUEUED`` ACK.
2. **Processor passthrough** (``method in {"assistant_*"}``) — returns the
   event directly without invoking anything.
3. **Ownership mismatch** — second user trying to use another user's session
   is rejected.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reload_handler():
    if "index" in sys.modules:
        del sys.modules["index"]
    import os

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if here not in sys.path:
        sys.path.insert(0, here)
    import index  # noqa: F401

    yield
    if "index" in sys.modules:
        del sys.modules["index"]


_CALLER_EMAIL = "caller@example.com"


def _make_event(
    arguments: dict,
    sub: str | None = "caller-sub",
    email: str | None = _CALLER_EMAIL,
) -> dict:
    """An event shaped the way ``api_adapter.normalize_event`` builds one.

    That helper rebuilds ``identity`` from the claims API Gateway's Cognito
    authorizer verified, setting ``identity.username`` to the email and carrying
    the full claims dict — so a fixture without an ``email`` claim would not
    represent any real invocation, and the processor resolves the caller's
    config-version scope by email.
    """
    identity: dict = {}
    if sub is not None:
        claims: dict = {"sub": sub}
        if email is not None:
            claims["email"] = email
        identity = {"sub": sub, "username": email or sub, "claims": claims}
    return {"arguments": arguments, "identity": identity}


class TestResolverUIPath:
    @pytest.mark.unit
    def test_first_message_records_ownership_and_invokes_processor(self):
        import index

        # Session does not exist yet — resolver will put_item and claim ownership.
        table = MagicMock()
        table.get_item.return_value = {}
        dyn = MagicMock()
        dyn.Table.return_value = table

        lam = MagicMock()
        lam.invoke.return_value = {"StatusCode": 202}

        with (
            patch.object(index, "_dynamodb", dyn),
            patch.object(index, "_lambda", lam),
        ):
            result = index.handler(
                _make_event(
                    {
                        "sessionId": "s-1",
                        "prompt": "hi",
                        "s3Uri": "uploads/x.pdf",
                        "modelId": "us.anthropic.claude-opus-4-8:1m",
                        "method": "chat",
                    }
                ),
                None,
            )

        # ACK shape
        assert result["sessionId"] == "s-1"
        assert result["method"] == "chat"
        assert result["status"] == "QUEUED"
        assert result["role"] == "user"
        assert result["isProcessing"] is True

        # Claimed ownership
        table.put_item.assert_called_once()
        claim = table.put_item.call_args.kwargs["Item"]
        assert claim["sessionId"] == "s-1"
        assert claim["ownerSub"] == "caller-sub"

        # Async invoke (Event)
        lam.invoke.assert_called_once()
        call_kwargs = lam.invoke.call_args.kwargs
        assert call_kwargs["InvocationType"] == "Event"
        payload = json.loads(call_kwargs["Payload"].decode("utf-8"))
        assert payload["sessionId"] == "s-1"
        assert payload["prompt"] == "hi"
        assert payload["s3Uri"] == "uploads/x.pdf"
        # The verified caller's email, which is what the processor resolves
        # `allowedConfigVersions` by. Nothing else from the claims is forwarded:
        # the processor logs its event, and the email is all its check needs.
        assert payload["identity"] == {"claims": {"email": _CALLER_EMAIL}}

    @pytest.mark.unit
    def test_identity_key_is_present_even_with_no_caller(self):
        """An IAM-gated invocation forwards an explicit null, not a missing key.

        The processor distinguishes the two: a null ``identity`` means the caller
        was authorized by IAM on the function ARN and its scope check stands down;
        an absent key means nobody told it anything about the caller, which it
        treats as a wiring regression and denies. Dropping the key here would turn
        a fail-closed deny into a silent fail-open.
        """
        import index

        table = MagicMock()
        table.get_item.return_value = {}
        dyn = MagicMock()
        dyn.Table.return_value = table
        lam = MagicMock()

        with (
            patch.object(index, "_dynamodb", dyn),
            patch.object(index, "_lambda", lam),
        ):
            index.handler(
                {
                    "arguments": {
                        "sessionId": "s-1",
                        "prompt": "hi",
                        "s3Uri": "uploads/x.pdf",
                        "method": "chat",
                    },
                    "identity": None,
                },
                None,
            )

        payload = json.loads(lam.invoke.call_args.kwargs["Payload"].decode("utf-8"))
        assert "identity" in payload
        assert payload["identity"] is None

    @pytest.mark.unit
    def test_forwarded_identity_carries_only_the_email_claim(self):
        """Forward the one claim the processor's check reads, and nothing else.

        The processor logs its invocation event, so a full claims dict would write
        the caller's tokens and group list into its log group for no benefit.
        """
        import index

        forwarded = index._forwarded_identity(
            {
                "identity": {
                    "sub": "caller-sub",
                    "username": _CALLER_EMAIL,
                    "sourceIp": "203.0.113.4",
                    "claims": {
                        "sub": "caller-sub",
                        "email": _CALLER_EMAIL,
                        "cognito:groups": ["Admin"],
                        "token_use": "id",
                    },
                }
            }
        )
        assert forwarded == {"claims": {"email": _CALLER_EMAIL}}

    @pytest.mark.unit
    def test_forwarded_identity_falls_back_to_username_without_an_email_claim(self):
        """``identity.username`` is the email under the dispatcher's adapter.

        A token with no ``email`` claim still yields a principal name there, and
        using it keeps the lookup resolvable rather than denying the turn.
        """
        import index

        forwarded = index._forwarded_identity(
            {"identity": {"username": _CALLER_EMAIL, "claims": {"sub": "caller-sub"}}}
        )
        assert forwarded == {"claims": {"email": _CALLER_EMAIL}}

    @pytest.mark.unit
    def test_missing_s3uri_raises(self):
        import index

        table = MagicMock()
        table.get_item.return_value = {}
        dyn = MagicMock()
        dyn.Table.return_value = table

        with (
            patch.object(index, "_dynamodb", dyn),
            patch.object(index, "_lambda", MagicMock()),
            pytest.raises(Exception, match="s3Uri is required"),
        ):
            index.handler(
                _make_event({"sessionId": "s-1", "prompt": "hi", "method": "chat"}),
                None,
            )


class TestResolverProcessorPassthrough:
    @pytest.mark.unit
    def test_assistant_stream_passes_through_without_invoking_processor(self):
        import index

        lam = MagicMock()
        dyn = MagicMock()
        with (
            patch.object(index, "_dynamodb", dyn),
            patch.object(index, "_lambda", lam),
        ):
            result = index.handler(
                _make_event(
                    {
                        "sessionId": "s-1",
                        "prompt": "",
                        "method": "assistant_stream",
                        "content": "Hello",
                        "status": "STREAMING",
                        "role": "assistant",
                        "isProcessing": True,
                    },
                    sub="",  # IAM/system call, no Cognito sub
                ),
                None,
            )

        # No invoke, no put_item
        lam.invoke.assert_not_called()

        assert result["method"] == "assistant_stream"
        assert result["content"] == "Hello"
        assert result["status"] == "STREAMING"
        assert result["role"] == "assistant"
        assert result["isProcessing"] is True

    @pytest.mark.unit
    def test_assistant_final_default_is_processing_false(self):
        import index

        # When the processor publishes a terminal event without explicitly
        # passing isProcessing, the resolver should default it to False so the
        # UI knows the bubble is done.
        lam = MagicMock()
        dyn = MagicMock()
        with (
            patch.object(index, "_dynamodb", dyn),
            patch.object(index, "_lambda", lam),
        ):
            result = index.handler(
                _make_event(
                    {
                        "sessionId": "s-1",
                        "prompt": "",
                        "method": "assistant_final",
                        "content": "answer",
                        "status": "COMPLETE",
                    },
                    sub="",
                ),
                None,
            )

        assert result["isProcessing"] is False


class TestResolverOwnership:
    @pytest.mark.unit
    def test_different_user_rejected(self):
        import index

        table = MagicMock()
        table.get_item.return_value = {
            "Item": {"sessionId": "s-1", "ownerSub": "owner-a"}
        }
        dyn = MagicMock()
        dyn.Table.return_value = table

        with (
            patch.object(index, "_dynamodb", dyn),
            patch.object(index, "_lambda", MagicMock()),
            pytest.raises(Exception, match="Unauthorized"),
        ):
            index.handler(
                _make_event(
                    {
                        "sessionId": "s-1",
                        "prompt": "hi",
                        "s3Uri": "uploads/x.pdf",
                        "method": "chat",
                    },
                    sub="attacker",
                ),
                None,
            )
