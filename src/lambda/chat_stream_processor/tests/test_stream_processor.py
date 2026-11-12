# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the chat streaming endpoint helpers and sink wiring.

These cover the parts that are independent of FastAPI/Bedrock:

1. ``sse`` emits a well-formed SSE frame (``data: {json}\\n\\n``) that round-trips.
2. Every chat route takes the caller from the verified ID token, once, and refuses
   before the stream opens (asserted on ``app.py``'s structure; the behaviour is
   driven end to end in ``test_route_identity.py``).
3. The doc-chat processor's ``set_sink`` redirects emission away from AppSync
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

_ROUTES = ("POST /chat/document", "POST /chat/agent")


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
            "def _require_a_group_or_403("
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

        identity = _caller_identity(claims)
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


def _top_level_index(fn, call) -> int | None:
    """Index in ``fn.body`` of the bare statement holding ``call``, else None.

    "Bare" means an expression statement or a plain assignment directly in the
    route body, so nothing between it and the function can swallow or skip what
    it raises.
    """
    for i, st in enumerate(fn.body):
        if isinstance(st, ast.Expr) and st.value is call:
            return i
        if isinstance(st, ast.Assign) and st.value is call:
            return i
    return None


@pytest.mark.unit
@pytest.mark.parametrize("route", _ROUTES)
def test_every_route_verifies_the_caller_once_before_anything_else(route):
    """The verified token is the only identity source, read once, read first.

    ``_verified_caller`` raises the 401. It has to be a bare top-level statement
    ahead of the response (or a 401 becomes an error frame on a 200), ahead of
    the session claim (which needs the verified ``sub``), and the name it binds
    must be bound once, or a later statement could put another value there.
    """
    fn = _route_functions(_app_ast())[route]
    calls = _calls_named(fn, "_verified_caller")
    assert len(calls) == 1, f"{route}: expected one _verified_caller call"
    idx = _top_level_index(fn, calls[0])
    assert idx is not None, f"{route}: _verified_caller is nested where it can be skipped"
    target = fn.body[idx].targets[0]
    assert isinstance(target, ast.Name)
    assert len(_bindings_of(fn, target.id)) == 1, (
        f"{route}: '{target.id}' holds the verified claims and is bound more than once"
    )
    claim = _calls_named(fn, "_claim_session_or_403")
    assert len(claim) == 1, f"{route}: the session claim must run once"
    claim_idx = _top_level_index(fn, claim[0])
    assert claim_idx is not None and claim_idx > idx, (
        f"{route}: the session claim must be a bare statement after verification"
    )
    streams = [i for i, st in enumerate(fn.body) if _calls_named(st, "StreamingResponse")]
    assert streams and max(idx, claim_idx) < min(streams), (
        f"{route}: verification and the session claim must precede the response"
    )


@pytest.mark.unit
def test_nothing_reads_an_identity_from_the_request_body():
    """No ``callerSub`` field on the request models and no read of one anywhere.

    Earlier UI builds still send ``callerSub``; with no field declared it is an
    unknown key and is dropped by the model before any route code sees it.
    """
    tree = _app_ast()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "callerSub", f"app.py line {node.lineno} reads callerSub"
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assert node.target.id != "callerSub", (
                f"app.py line {node.lineno} declares a callerSub request field"
            )
