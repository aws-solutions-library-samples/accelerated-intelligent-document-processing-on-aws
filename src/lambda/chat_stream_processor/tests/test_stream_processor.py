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
    """The trailing ARN segment is returned verbatim, whatever it is.

    The session name is deliberately the MEASURED one rather than a placeholder
    like ``the-cognito-sub``: an earlier version of this fixture used that
    placeholder, which reads as though the segment were a per-user Cognito
    ``sub``. It is not — on a live deployment it is the pool-wide constant below
    (499 CloudTrail ``AssumeRoleWithWebIdentity`` events, zero counter-examples).
    A placeholder passes identically under either assumption, which is exactly
    why the misconception survived.
    """
    from sse import caller_sub_from_request_context

    ctx = {
        "authorizer": {
            "iam": {
                "userArn": (
                    "arn:aws:sts::123456789012:assumed-role/"
                    "MyAuthRole/CognitoIdentityCredentials"
                ),
            }
        }
    }
    assert (
        caller_sub_from_request_context(json.dumps(ctx))
        == "CognitoIdentityCredentials"
    )


@pytest.mark.unit
def test_caller_sub_userid_fallback_is_also_pool_wide():
    """The ``userId`` fallback is no more per-user than the ARN is.

    For an assumed role ``userId`` is ``<role-unique-id>:<session-name>``, so on
    this transport it carries the same pool-wide constant with a role id glued to
    the front. It is extracted (it is all there is) but must not be mistaken for
    an identity to compare a client claim against.
    """
    from sse import caller_sub_from_request_context, is_user_specific_identity

    ctx = {
        "authorizer": {"iam": {"userId": "AROAEXAMPLEID:CognitoIdentityCredentials"}}
    }
    extracted = caller_sub_from_request_context(json.dumps(ctx))
    assert extracted == "AROAEXAMPLEID:CognitoIdentityCredentials"
    assert is_user_specific_identity(extracted) is False


@pytest.mark.unit
def test_caller_sub_ignores_cognito_identity_key_entirely():
    """Function URLs may set ``cognitoIdentity`` to null OR omit it.

    AWS documents both outcomes ("Lambda sets this to null or excludes this from
    the JSON"), so a reader must not be able to assume one. Nothing here reads
    the key, which is why the two cases cannot be distinguished incorrectly —
    pinned so that a future change reading it has to face the question.
    """
    from sse import caller_sub_from_request_context

    arn = "arn:aws:sts::123456789012:assumed-role/R/CognitoIdentityCredentials"
    absent = {"authorizer": {"iam": {"userArn": arn}}}
    explicit_null = {"authorizer": {"iam": {"userArn": arn, "cognitoIdentity": None}}}
    assert caller_sub_from_request_context(
        json.dumps(absent)
    ) == caller_sub_from_request_context(json.dumps(explicit_null))


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


# Realistic per-user values. A Cognito User Pool `sub` is a UUID and an Identity
# Pool identity id is `<region>:<uuid>`; the predicate under test recognises those
# shapes and only those, so placeholders like "verified-sub" would exercise the
# wrong branch.
_SUB_A = "d47cb94a-1c2e-4f3a-9b8d-0e1f2a3b4c5d"
_SUB_B = "9f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a"
_IDENTITY_ID = "us-west-2:d47cb94a-1c2e-4f3a-9b8d-0e1f2a3b4c5d"


@pytest.mark.unit
def test_resolve_caller_sub_prefers_the_verified_identity():
    """A body value equal to nothing does not displace the verified identity."""
    from sse import resolve_caller_sub

    assert resolve_caller_sub(_SUB_A, "") == _SUB_A


@pytest.mark.unit
@pytest.mark.parametrize("verified", [_SUB_A, _IDENTITY_ID])
def test_resolve_caller_sub_rejects_a_contradicting_body_identity(verified):
    """A body identity that disagrees with the verified one is refused.

    Not "the verified one silently wins": a client asserting an identity that
    contradicts the one the transport proved is never legitimate, so the request
    does not proceed at all. Both per-user shapes are covered.
    """
    from sse import CallerIdentityConflict, resolve_caller_sub

    with pytest.raises(CallerIdentityConflict):
        resolve_caller_sub(verified, _SUB_B)


@pytest.mark.unit
def test_resolve_caller_sub_discards_a_body_value_of_another_kind():
    """A claim that is not a Cognito identifier is discarded, not treated as conflict.

    The web UI sends an email, which can never equal a session name or a `sub`.
    If AWS ever made the transport principal per-user, comparing the two as
    though they were the same kind of name would 403 every single agent-chat
    turn — a fail-closed outage caused by an upstream change with no code change
    here. The proven identity wins instead; the client value is dropped, so this
    is strictly no more permissive than refusing.
    """
    from sse import resolve_caller_sub

    assert resolve_caller_sub(_SUB_A, "user@example.com") == _SUB_A


@pytest.mark.unit
def test_resolve_caller_sub_accepts_an_agreeing_body_identity():
    from sse import resolve_caller_sub

    assert resolve_caller_sub(_SUB_A, _SUB_A) == _SUB_A


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
        # The two shapes Cognito uses for a per-user identifier.
        (_SUB_A, True),
        (_IDENTITY_ID, True),
        ("D47CB94A-1C2E-4F3A-9B8D-0E1F2A3B4C5D", True),  # case-insensitive
        # The measured value on the live commercial transport, and its
        # unauthenticated sibling.
        ("CognitoIdentityCredentials", False),
        ("CognitoIdentityCredentialsUnauthenticated", False),
        # The `userId` fallback shape — a role id plus the same constant.
        ("AROAEXAMPLEID:CognitoIdentityCredentials", False),
        ("", False),
        # Anything that is not one of the recognised per-user shapes is NOT
        # treated as per-user. This is the deliberate direction: the session name
        # is undocumented, so an unrecognised future value must fall through to
        # the body-supplied fallback rather than 403 every request.
        ("SomeFutureSessionName", False),
        ("user@example.com", False),
        ("d47cb94a-1c2e-4f3a-9b8d", False),  # truncated UUID
    ],
)
def test_is_user_specific_identity(caller_sub, expected):
    from sse import is_user_specific_identity

    assert is_user_specific_identity(caller_sub) is expected


@pytest.mark.unit
def test_an_unrecognised_transport_principal_does_not_deny_the_request():
    """The whole point of the positive shape test, stated as behaviour.

    If AWS substituted some other opaque string for the session name, the old
    denylist-based predicate would have classed it as a per-user identity and
    `resolve_caller_sub` would have raised on every turn, because the browser
    sends an email. It must fall back instead.
    """
    from sse import resolve_caller_sub

    assert (
        resolve_caller_sub("SomeFutureOpaqueSessionName", "user@example.com")
        == "user@example.com"
    )


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
def test_agent_route_denies_before_the_stream_opens():
    """The group gate must run BEFORE StreamingResponse is constructed.

    Once the response object exists the status is fixed at 200, and
    ``_run_in_thread`` renders anything the producer raises as an
    ``assistant_error`` frame in the body — so a denial applied only inside the
    processor is downgraded from a 403 to a 200. Asserting on the source rather
    than by calling the route because app.py imports FastAPI, which is not
    installed in the unit environment.
    """
    app_src = open(os.path.join(_HERE, "app.py")).read()
    start = app_src.index('@app.post("/chat/agent")')
    body = app_src[start:]
    gate_at = body.index("_enforce_groups_or_403(")
    stream_at = body.index("StreamingResponse(")
    assert gate_at < stream_at, "the group gate must precede StreamingResponse"
    # And the gate must be what turns the processor's PermissionError into a 403,
    # not a re-implementation of the group policy in this file.
    gate_src = app_src[
        app_src.index("def _enforce_groups_or_403(") : app_src.index(
            'def _run_in_thread('
        )
    ]
    assert "_enforce_agent_chat_groups(" in gate_src
    assert "status_code=403" in gate_src
    # The policy itself must not be duplicated here.
    assert "Admin" not in gate_src


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
