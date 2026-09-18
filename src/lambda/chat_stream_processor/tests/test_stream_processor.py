# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the chat streaming endpoint helpers and sink wiring.

These cover the parts that are independent of FastAPI/Bedrock:

1. ``sse`` emits a well-formed SSE frame (``data: {json}\\n\\n``) that round-trips.
2. ``caller_sub_from_request_context`` extracts the Cognito sub from the
   Function URL request context.
3. ``resolve_caller_sub`` prefers the transport-verified caller identity and
   refuses a request-body identity that contradicts it.
4. The doc-chat processor's ``set_sink`` redirects emission away from AppSync
   (skipped when ``idp_common`` is not importable in the test environment).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


@pytest.mark.unit
def test_sse_frame_roundtrips():
    from sse import sse

    frame = sse({"method": "assistant_stream", "content": "hi"})
    assert frame.endswith("\n\n")
    assert frame.startswith("data: ")
    decoded = json.loads(frame[len("data: ") : -2])
    assert decoded["method"] == "assistant_stream"
    assert decoded["content"] == "hi"


@pytest.mark.unit
def test_sse_frame_has_single_blank_line_separator():
    """A frame must contain exactly one terminating blank line so the UI's
    split-on-`\\n\\n` framing yields one event per frame."""
    from sse import sse

    frame = sse({"content": "line1\nline2"})
    # Embedded newlines in the JSON value are preserved (json escapes them),
    # so the only literal "\n\n" is the frame terminator.
    assert frame.count("\n\n") == 1


@pytest.mark.unit
def test_caller_sub_from_assumed_role_arn():
    from sse import caller_sub_from_request_context

    ctx = {
        "authorizer": {
            "iam": {
                "userArn": (
                    "arn:aws:sts::123456789012:assumed-role/"
                    "MyAuthRole/the-cognito-sub"
                ),
            }
        }
    }
    assert caller_sub_from_request_context(json.dumps(ctx)) == "the-cognito-sub"


@pytest.mark.unit
def test_caller_sub_missing_or_bad_context_is_empty():
    from sse import caller_sub_from_request_context

    assert caller_sub_from_request_context(None) == ""
    assert caller_sub_from_request_context("") == ""
    assert caller_sub_from_request_context("not-json") == ""


# --- caller identity resolution ---------------------------------------------
#
# The Function URL authenticates the caller with SigV4, and the browser also
# sends a `callerSub` in the request body (it is the chat-history key). Which of
# the two wins decides who a chat turn is attributed to and whose history it is
# written against, so the precedence is a security property, not a detail.
#
# These live here rather than against app.py because app.py imports FastAPI and
# the two processor modules; sse.py is the dependency-free half by design.


@pytest.mark.unit
def test_resolve_caller_sub_prefers_the_verified_identity():
    """A body value equal to nothing does not displace the verified identity."""
    from sse import resolve_caller_sub

    assert resolve_caller_sub("verified-sub", "") == "verified-sub"


@pytest.mark.unit
def test_resolve_caller_sub_rejects_a_contradicting_body_identity():
    """A body identity that disagrees with the verified one is refused.

    Not "the verified one silently wins": a client asserting an identity that
    contradicts the one the transport proved is never legitimate, so the request
    does not proceed at all.
    """
    from sse import CallerIdentityConflict, resolve_caller_sub

    with pytest.raises(CallerIdentityConflict):
        resolve_caller_sub("verified-sub", "someone-else")


@pytest.mark.unit
def test_resolve_caller_sub_accepts_an_agreeing_body_identity():
    from sse import resolve_caller_sub

    assert resolve_caller_sub("verified-sub", "verified-sub") == "verified-sub"


@pytest.mark.unit
def test_resolve_caller_sub_falls_back_when_transport_has_no_user_identity():
    """A pool-wide session name is not a claim about WHICH user is calling.

    Under the Cognito Identity Pool enhanced flow the assumed-role session name
    is the same constant for every user of the pool, so it must not be compared
    against the per-user value in the body — doing so would refuse every
    legitimate request. The body value is the fallback in that case.
    """
    from sse import resolve_caller_sub

    assert (
        resolve_caller_sub("CognitoIdentityCredentials", "user@example.com")
        == "user@example.com"
    )


@pytest.mark.unit
def test_resolve_caller_sub_keeps_the_transport_value_when_body_is_silent():
    """No behaviour change for a client that sends no identity of its own.

    Chat-with-Document does not send one, so it must keep receiving exactly what
    it received before: whatever the transport yielded.
    """
    from sse import resolve_caller_sub

    assert (
        resolve_caller_sub("CognitoIdentityCredentials", "")
        == "CognitoIdentityCredentials"
    )


@pytest.mark.unit
def test_resolve_caller_sub_with_no_identity_at_all_is_empty():
    """Both processors treat an empty caller as unattributable and refuse work."""
    from sse import resolve_caller_sub

    assert resolve_caller_sub("", "") == ""


@pytest.mark.unit
@pytest.mark.parametrize(
    ("caller_sub", "expected"),
    [
        ("a-real-cognito-sub", True),
        ("user@example.com", True),
        ("CognitoIdentityCredentials", False),
        ("CognitoIdentityCredentialsUnauthenticated", False),
        ("", False),
    ],
)
def test_is_user_specific_identity(caller_sub, expected):
    from sse import is_user_specific_identity

    assert is_user_specific_identity(caller_sub) is expected


@pytest.mark.unit
def test_both_routes_share_one_identity_resolution():
    """Neither route may resolve the caller identity its own way.

    The defect this guards against was precedence drift between the two routes:
    /chat/document read the verified identity first, /chat/agent read the body
    first. Asserting on the source keeps the check meaningful without importing
    FastAPI (not installed in the unit environment).
    """
    app_src = open(os.path.join(_HERE, "app.py")).read()
    for route in ("/chat/document", "/chat/agent"):
        start = app_src.index(f'@app.post("{route}")')
        end = app_src.find("@app.", start + 1)
        body = app_src[start : end if end > 0 else len(app_src)]
        assert "_resolve_caller_sub(request, body.callerSub)" in body, route
        # No route may reach for the body value on its own.
        assert body.count("body.callerSub") == 1, route


@pytest.mark.unit
@pytest.mark.skipif(
    importlib.util.find_spec("idp_common") is None,
    reason="idp_common not installed in this test environment",
)
def test_doc_processor_set_sink_redirects_emission():
    """Installing a sink must redirect _emit away from the AppSync _publish."""
    doc_dir = os.path.join(os.path.dirname(_HERE), "chat_with_document_processor")
    if doc_dir not in sys.path:
        sys.path.insert(0, doc_dir)
    if "index" in sys.modules:
        del sys.modules["index"]
    import index as doc_proc

    captured = []

    def _sink(**kwargs):
        captured.append(kwargs)

    doc_proc.set_sink(_sink)
    try:
        doc_proc._emit(
            session_id="s1",
            method="assistant_stream",
            status="STREAMING",
            content="delta",
            model_id="m",
            is_processing=True,
        )
    finally:
        doc_proc.set_sink(None)

    assert len(captured) == 1
    assert captured[0]["method"] == "assistant_stream"
    assert captured[0]["content"] == "delta"
    assert doc_proc._active_sink is None
    del sys.modules["index"]
