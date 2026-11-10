# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the chat streaming endpoint helpers and sink wiring.

These cover the parts that are independent of FastAPI/Bedrock:

1. ``sse`` emits a well-formed SSE frame (``data: {json}\\n\\n``) that round-trips.
2. ``history_key`` derives the chat-history key from verified claims the way
   the REST path does, and ``resolve_caller_sub`` refuses a request-body identity
   that contradicts it.
3. Both routes verify the caller once, before the stream opens, and hand every
   gate and the processor that one verified identity.
4. The doc-chat processor's ``set_sink`` redirects emission away from AppSync
   (skipped when ``idp_common`` is not importable in the test environment).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# --- structural helpers ------------------------------------------------------
# The two tests below assert on app.py's *structure*, not on the order of
# substrings in its text. A textual index comparison cannot tell
# `_enforce_groups_or_403(identity)` apart from the same call wrapped in
# `try: ... except HTTPException: pass`, nor a single assignment to the caller
# identity apart from a second one that overwrites it -- both mutations keep every
# token in place and at the same offset. app.py imports FastAPI, which is not
# installed in this environment, so it is parsed rather than imported.

# How the handler can name the identity the TRANSPORT verified. Mirrors
# VERIFIED_IDENTITY_TOKENS in scripts/sdlc/scan_api_rbac.py.
_RESOLVER_NAMES = (
    "_resolve_caller_sub",
    "resolve_caller_sub",
    "_caller_sub",
    "caller_sub_from_request_context",
)


def _app_ast():
    with open(os.path.join(_HERE, "app.py")) as fh:
        return ast.parse(fh.read())


def _route_functions(tree) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Map ``"<METHOD> <path>"`` -> the route's function node."""
    routes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and isinstance(dec.func.value, ast.Name)
                and dec.func.value.id == "app"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                routes[f"{dec.func.attr.upper()} {dec.args[0].value}"] = node
    return routes


def _calls_named(node, name: str) -> list[ast.Call]:
    """Every ``ast.Call`` in ``node``'s subtree whose callee is ``name``."""
    out = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        called = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if called == name:
            out.append(sub)
    return out


def _own_scope_statements(fn):
    """Statements in ``fn``'s own scope, not descending into nested functions.

    A nested ``def`` gets its own scope, so an assignment there shadows rather
    than rebinds -- counting it would be a false positive.
    """
    out = []
    stack = list(fn.body)
    while stack:
        st = stack.pop()
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        out.append(st)
        for field in ("body", "orelse", "finalbody", "handlers"):
            stack.extend(getattr(st, field, []) or [])
    return out


def _bindings_of(fn, name: str) -> list[ast.stmt]:
    """Statements in ``fn``'s own scope that bind ``name``."""
    found = []
    for st in _own_scope_statements(fn):
        targets = []
        if isinstance(st, ast.Assign):
            targets = list(st.targets)
        elif isinstance(st, (ast.AugAssign, ast.AnnAssign)):
            targets = [st.target]
        elif isinstance(st, (ast.For, ast.AsyncFor)):
            targets = [st.target]
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            targets = [i.optional_vars for i in st.items if i.optional_vars]
        for tgt in targets:
            for sub in ast.walk(tgt):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    if sub.id == name:
                        found.append(st)
        # A walrus anywhere in the statement also binds.
        for sub in ast.walk(st):
            if (
                isinstance(sub, ast.NamedExpr)
                and isinstance(sub.target, ast.Name)
                and sub.target.id == name
            ):
                found.append(st)
    return found


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
def test_now_iso_is_a_utc_timestamp_with_microseconds():
    """The UI orders streamed events by this field, so its shape is a contract."""
    from datetime import datetime

    from sse import now_iso

    stamp = now_iso()
    assert stamp.endswith("Z")
    assert datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ")


def _identity(**claims):
    return {"claims": claims}


@pytest.mark.unit
def test_history_key_is_the_rest_paths_username():
    """email, else cognito:username, else sub — what the REST path files under."""
    from sse import history_key

    full = _identity(email="a@example.com", **{"cognito:username": "a"}, sub="s")
    assert history_key(full) == "a@example.com"
    assert history_key(_identity(**{"cognito:username": "a"}, sub="s")) == "a"
    assert history_key(_identity(sub="s")) == "s"
    assert history_key(_identity()) == ""
    assert history_key(None) == ""


@pytest.mark.unit
def test_the_web_uis_caller_sub_matches_the_history_key():
    """The UI derives the body value from the same token claims in the same order
    (use-agent-chat.ts), so an honest client never trips the conflict refusal."""
    ui_src = open(
        os.path.join(
            os.path.dirname(os.path.dirname(_HERE)),
            "..",
            "src",
            "ui",
            "src",
            "hooks",
            "use-agent-chat.ts",
        )
    ).read()
    assert (
        "idClaims?.email || idClaims?.['cognito:username'] || idClaims?.sub" in ui_src
    )


@pytest.mark.unit
def test_resolve_caller_sub_returns_the_verified_key():
    from sse import resolve_caller_sub

    assert resolve_caller_sub("a@example.com", "") == "a@example.com"
    assert resolve_caller_sub("a@example.com", "a@example.com") == "a@example.com"


@pytest.mark.unit
@pytest.mark.parametrize(
    "claimed", ["b@example.com", "9f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a", "a"]
)
def test_resolve_caller_sub_refuses_a_body_identity_that_differs(claimed):
    """Whatever its shape, a body value that is not the verified key is refused:
    it would file the turn under someone else's history."""
    from sse import CallerIdentityConflict, resolve_caller_sub

    with pytest.raises(CallerIdentityConflict):
        resolve_caller_sub("a@example.com", claimed)


@pytest.mark.unit
def test_resolve_caller_sub_refuses_when_nothing_was_verified():
    from sse import CallerIdentityConflict, resolve_caller_sub

    with pytest.raises(CallerIdentityConflict):
        resolve_caller_sub("", "a@example.com")


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
        assert "_resolve_caller_sub(identity, body.callerSub)" in body, route
        # No route may reach for the body value on its own.
        assert body.count("body.callerSub") == 1, route


@pytest.mark.unit
def test_neither_route_rebinds_the_caller_identity_after_resolving_it():
    """The resolved caller identity must be assigned exactly once per route.

    ``test_both_routes_share_one_identity_resolution`` above counts the literal
    ``body.callerSub``, so it cannot see a body value re-admitted under another
    spelling. This mutation defeats it while restoring the original defect from
    issue #920 -- the client's claimed identity wins unconditionally::

        _claimed = body.model_dump().get('callerSub') or ''
        if _claimed:
            caller_sub = _claimed

    Every token the textual check looks for is still present and still in the
    same order. What actually changed is that the name holding the verified
    identity is bound twice. So assert the single binding, which is the property
    that matters and is independent of how the second value is spelled.
    """
    routes = _route_functions(_app_ast())
    for route in ("POST /chat/document", "POST /chat/agent"):
        fn = routes.get(route)
        assert fn is not None, f"{route} not found in app.py"

        # The resolution must happen exactly once, and where its refusal can
        # escape: `_resolve_caller_sub` raises a 403 for a body identity that
        # contradicts the verified one, and a call nested in a `try` or an `if`
        # could swallow or skip that.
        calls = _calls_named(fn, "_resolve_caller_sub")
        assert len(calls) == 1, (
            f"{route}: expected exactly one _resolve_caller_sub call, found "
            f"{len(calls)}"
        )
        guard = _enclosing(fn, calls[0])
        assert guard is fn, (
            f"{route}: the caller-identity resolution is nested inside a "
            f"{type(guard).__name__} at app.py line {calls[0].lineno}, where its "
            f"403 can be skipped or swallowed"
        )

        # The name(s) that receive a transport-verified identity resolution. A
        # route may bind none — /chat/document calls the resolver for its refusal
        # and reads the caller from `identity` instead of a body field — but one
        # that binds a name must bind it once, or a later assignment can put a
        # client-supplied value where the verified one was.
        resolved: set[str] = set()
        for st in _own_scope_statements(fn):
            if not isinstance(st, ast.Assign):
                continue
            if not any(_calls_named(st.value, n) for n in _RESOLVER_NAMES):
                continue
            for tgt in st.targets:
                if isinstance(tgt, ast.Name):
                    resolved.add(tgt.id)

        assert len(resolved) <= 1, (
            f"{route}: more than one variable receives the transport-verified "
            f"caller identity ({sorted(resolved)}) — resolve it once"
        )
        for name in resolved:
            bindings = _bindings_of(fn, name)
            lines = sorted(st.lineno for st in bindings)
            assert len(bindings) == 1, (
                f"{route}: '{name}' holds the transport-verified caller identity "
                f"but is bound {len(bindings)} times (app.py lines {lines}). A "
                f"second binding lets a client-supplied value overwrite the "
                f"verified one regardless of how it is spelled."
            )


@pytest.mark.unit
def test_doc_route_forwards_an_identity_key_to_the_processor():
    """The doc processor denies a turn whose event carries no ``identity`` key.

    That is deliberate: it is how a producer that stops forwarding the caller
    fails closed instead of silently switching the config-version scope check
    off. So this transport must always include the key, and its value must be
    the identity ``_caller_identity`` verified, never anything from the request
    body, which the caller chooses.
    """
    routes = _route_functions(_app_ast())
    fn = routes.get("POST /chat/document")
    assert fn is not None, "POST /chat/document not found in app.py"

    identity_values = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "identity":
                identity_values.append(value)
    assert len(identity_values) == 1, (
        "the processor event built by POST /chat/document must carry exactly one "
        f"'identity' key, found {len(identity_values)}"
    )
    value = identity_values[0]
    assert isinstance(value, ast.Name) and value.id == "identity", (
        "'identity' must be the identity the route verified with "
        "_caller_identity(request), not anything from the request body"
    )


@pytest.mark.unit
@pytest.mark.parametrize("route", ["POST /chat/document", "POST /chat/agent"])
def test_each_route_verifies_the_caller_once_before_anything_else(route):
    """``identity`` is bound once, from ``_caller_identity(request)``, as a bare
    top-level statement ahead of every gate and of ``StreamingResponse``.

    ``_caller_identity`` is where a missing or invalid ID token becomes a 403, so
    nested in a ``try`` or an ``if`` the refusal could be swallowed or skipped, and
    after the response is built it could only be an error frame on a 200. Every
    gate then reads that one name, so none can be handed a different identity.
    """
    fn = _route_functions(_app_ast())[route]
    bindings = _bindings_of(fn, "identity")
    assert len(bindings) == 1, f"{route}: 'identity' bound {len(bindings)} times"
    (binding,) = bindings
    assert binding in fn.body, f"{route}: 'identity' is bound inside a block"
    assert (
        isinstance(binding.value, ast.Call)
        and getattr(binding.value.func, "id", None) == "_caller_identity"
        and [getattr(a, "id", None) for a in binding.value.args] == ["request"]
    ), f"{route}: 'identity' must be _caller_identity(request)"

    at = fn.body.index(binding)
    gates = (
        "_resolve_caller_sub",
        "_refuse_unverified_when_unscoped_is_denied_or_403",
        "_enforce_groups_or_403",
        "_enforce_any_group_or_403",
        "_refuse_foreign_session_or_403",
    )
    for i, st in enumerate(fn.body):
        for gate in gates:
            for call in _calls_named(st, gate):
                assert i > at, f"{route}: {gate} runs before the caller is verified"
                assert getattr(call.args[0], "id", None) == "identity", (
                    f"{route}: {gate} must be given the verified identity"
                )
    streams = [i for i, st in enumerate(fn.body) if _calls_named(st, "StreamingResponse")]
    assert at < min(streams)


@pytest.mark.unit
def test_doc_route_applies_its_group_floor_before_the_stream_opens():
    """The REST operation's ANY_GROUP floor is the dispatcher's, which this
    transport bypasses, so the route applies it, as a bare statement ahead of the
    response."""
    fn = _route_functions(_app_ast())["POST /chat/document"]
    calls = _calls_named(fn, "_enforce_any_group_or_403")
    assert len(calls) == 1
    idx = next(
        i
        for i, st in enumerate(fn.body)
        if isinstance(st, ast.Expr) and st.value is calls[0]
    )
    streams = [i for i, st in enumerate(fn.body) if _calls_named(st, "StreamingResponse")]
    assert idx < min(streams)


@pytest.mark.unit
def test_doc_route_group_floor_is_the_rest_operations():
    """``_ANY_GROUP`` must be exactly what the dispatcher admits to the REST
    operation, so the two routes into Chat-with-Document refuse the same users."""
    manifest_path = os.path.join(
        os.path.dirname(os.path.dirname(_HERE)),
        "..",
        "nested",
        "api-resolvers",
        "src",
        "lambda",
        "http_api_dispatcher",
        "api_rbac_manifest.json",
    )
    with open(manifest_path) as fh:
        manifest = json.load(fh)
    entry = json.dumps(manifest)
    assert "sendChatDocumentMessage" in entry

    def find(node):
        if isinstance(node, dict):
            if "sendChatDocumentMessage" in node:
                return node["sendChatDocumentMessage"]
            for value in node.values():
                hit = find(value)
                if hit is not None:
                    return hit
        return None

    groups = find(manifest)
    if isinstance(groups, dict):
        groups = groups.get("groups")
    tree = _app_ast()
    declared = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            getattr(t, "id", None) == "_ANY_GROUP"
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    )
    assert sorted(declared) == sorted(groups)


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
def test_agent_route_gate_is_an_unguarded_top_level_statement():
    """The group gate's denial must be able to escape the route function.

    ``test_agent_route_denies_before_the_stream_opens`` above compares the string
    offset of ``_enforce_groups_or_403(`` against ``StreamingResponse(``. That
    check passes unchanged against a mutation which keeps the call, and its
    position, but swallows the denial::

        identity = _caller_identity()
        try:
            _enforce_groups_or_403(identity)
        except HTTPException:
            pass

    The gate is then present, first, and completely inert. Measured: with that
    mutation applied, the whole suite was byte-identical to the clean baseline.
    So assert the structure instead -- the call must be a bare expression
    statement directly in the route function's body, with no ``try``/``if``/loop
    between it and the function, and it must come before the response is built.
    """
    routes = _route_functions(_app_ast())
    fn = routes.get("POST /chat/agent")
    assert fn is not None, "POST /chat/agent not found in app.py"

    gate_calls = _calls_named(fn, "_enforce_groups_or_403")
    assert len(gate_calls) == 1, (
        f"expected exactly one _enforce_groups_or_403 call in the agent route, "
        f"found {len(gate_calls)}"
    )

    # It must be a bare `ast.Expr` statement directly in fn.body -- not nested in
    # a Try (which could swallow the HTTPException), an If (which could skip it),
    # or any other compound statement.
    gate_idx = None
    for i, st in enumerate(fn.body):
        if isinstance(st, ast.Expr) and st.value is gate_calls[0]:
            gate_idx = i
            break
    assert gate_idx is not None, (
        "_enforce_groups_or_403 must be called as a bare statement at the top "
        "level of the agent route, so its HTTPException propagates. It is "
        f"currently nested inside a {type(_enclosing(fn, gate_calls[0])).__name__} "
        "at app.py line "
        f"{gate_calls[0].lineno}, where the denial can be skipped or swallowed."
    )

    # And it must precede every StreamingResponse construction: once the response
    # is returned the status is pinned to 200 and a 403 is no longer expressible.
    stream_idxs = [
        i for i, st in enumerate(fn.body) if _calls_named(st, "StreamingResponse")
    ]
    assert stream_idxs, "the agent route must construct a StreamingResponse"
    assert gate_idx < min(stream_idxs), (
        f"the group gate (statement {gate_idx}) must precede the "
        f"StreamingResponse (statement {min(stream_idxs)})"
    )


def _enclosing(fn, target):
    """The innermost compound statement guarding ``target``, for the message.

    Names the ``Try``/``If``/loop/``With`` that makes the gate skippable, which is
    the useful diagnostic -- not the ``Expr`` immediately wrapping the call.
    """
    guards = (
        ast.Try,
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.With,
        ast.AsyncWith,
    )
    best = fn
    for sub in ast.walk(fn):
        if not isinstance(sub, guards):
            continue
        if any(child is target for child in ast.walk(sub)):
            best = sub
    return best


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

@pytest.mark.unit
@pytest.mark.parametrize("route", ["POST /chat/document", "POST /chat/agent"])
def test_tenant_isolation_refuses_an_unattributed_turn_before_the_stream(route):
    """Under ``TenantIsolationMode=Strict``, or an external ``TenantGrantSource``
    (``IdpClaim`` or ``Api``), a turn with no verified identity must be refused
    with a 403, on BOTH routes.

    Asserted structurally for the reason the group-gate tests above give: the
    call has to be a bare top-level statement ahead of ``StreamingResponse`` or
    its 403 is either swallowed or downgraded to an error frame on a 200.
    """
    fn = _route_functions(_app_ast())[route]
    calls = _calls_named(fn, "_refuse_unverified_when_unscoped_is_denied_or_403")
    assert len(calls) == 1, f"{route} must apply the tenant-isolation refusal once"
    idx = next(
        i
        for i, st in enumerate(fn.body)
        if isinstance(st, ast.Expr) and st.value is calls[0]
    )
    streams = [
        i for i, st in enumerate(fn.body) if _calls_named(st, "StreamingResponse")
    ]
    assert idx < min(streams)


@pytest.mark.unit
def test_the_refusal_is_conditional_on_unscoped_is_denied_and_a_403():
    src = open(os.path.join(_HERE, "app.py")).read()
    body = src[
        src.index("def _refuse_unverified_when_unscoped_is_denied_or_403(") : src.index(
            "def _resolve_caller_sub("
        )
    ]
    assert "identity is None and unscoped_is_denied()" in body
    assert "status_code=403" in body


@pytest.mark.unit
def test_agent_route_refuses_another_users_session_before_the_stream_opens():
    """A turn for a session another user owns must be refused while a 403 is still
    expressible, with the verified identity and the resolved caller key."""
    fn = _route_functions(_app_ast())["POST /chat/agent"]
    calls = _calls_named(fn, "_refuse_foreign_session_or_403")
    assert len(calls) == 1
    (call,) = calls
    idx = next(
        i
        for i, st in enumerate(fn.body)
        if isinstance(st, ast.Expr) and st.value is call
    )
    streams = [i for i, st in enumerate(fn.body) if _calls_named(st, "StreamingResponse")]
    assert idx < min(streams)
    assert [getattr(a, "id", None) for a in call.args] == [
        "identity",
        "session_id",
        "caller_sub",
    ]
