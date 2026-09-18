# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The RBAC scanner's Function-URL checks (S6-S9) must not be inert.

Background — the gap this suite exists to prevent
-------------------------------------------------
`scan_api_rbac.py` used to scan only the REST dispatcher. Chat streaming is
served by a Lambda Function URL whose FastAPI app calls the chat processors
directly, so its routes were outside every check: an authorization defect there
was invisible to `make api-test-static`. S6-S9 close that.

A scanner check that cannot fail is worse than no check, because the green run
is taken as evidence. These tests pin the S7/S8/S9 rules against source snippets
that model the shapes the checks are meant to catch, so the rules keep their
teeth independently of the live repo state (which, once fixed, exercises only
the passing side).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SDLC_DIR = Path(__file__).resolve().parents[1]
if str(_SDLC_DIR) not in sys.path:
    sys.path.insert(0, str(_SDLC_DIR))


def _load_scanner():
    spec = importlib.util.spec_from_file_location(
        "scan_api_rbac", _SDLC_DIR / "scan_api_rbac.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["scan_api_rbac"] = module
    spec.loader.exec_module(module)
    return module


scanner = _load_scanner()


# --- S6: route + Function URL discovery --------------------------------------

_APP_SNIPPET = '''
@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/chat/document")
async def chat_document(request: Request, body: DocumentChatRequest):
    caller_sub = _resolve_caller_sub(request, body.callerSub)
    return caller_sub


@app.post("/chat/agent")
async def chat_agent(request: Request, body: AgentChatRequest):
    caller_sub = _resolve_caller_sub(request, body.callerSub)
    return caller_sub
'''


@pytest.mark.unit
def test_app_routes_discovers_every_route():
    routes = scanner.app_routes(_APP_SNIPPET)
    assert set(routes) == {"GET /health", "POST /chat/document", "POST /chat/agent"}
    # Each body must be the route's own, not a run-on of the following route —
    # otherwise a clean route would launder a defective neighbour.
    assert "chat_agent" not in routes["POST /chat/document"]


@pytest.mark.unit
def test_lambda_url_resources_reads_auth_type_and_target():
    template = """Resources:
  SomeOtherThing:
    Type: AWS::SQS::Queue
  MyStreamUrl:
    Type: AWS::Lambda::Url
    Properties:
      TargetFunctionArn: !Ref MyStreamFunction
      AuthType: AWS_IAM
      InvokeMode: RESPONSE_STREAM
  MyStreamFunction:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/lambda/my_stream/
"""
    urls = scanner.lambda_url_resources(template)
    assert urls == {
        "MyStreamUrl": {"auth_type": "AWS_IAM", "target": "MyStreamFunction"}
    }
    assert scanner.function_code_uri(template, "MyStreamFunction") == (
        "src/lambda/my_stream/"
    )


# --- S7: the verified identity must be resolved first ------------------------
#
# S7 compares the position of the first client-supplied-identity token against
# the first transport-verified one within the route body.


def _prefers_body(route_body: str) -> bool:
    v_at = scanner._first_index(route_body, scanner.VERIFIED_IDENTITY_TOKENS)
    c_at = scanner._first_index(route_body, scanner.CLIENT_IDENTITY_TOKENS)
    return v_at < 0 or 0 <= c_at < v_at


@pytest.mark.unit
def test_s7_flags_body_first_precedence():
    """The shape the defect had: body value tried first, verified as fallback."""
    assert _prefers_body(
        'caller_sub = str(body.get("callerSub") or "") or _caller_sub(request)'
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        'caller_sub = _caller_sub(request) or str(body.get("callerSub") or "")',
        "caller_sub = _resolve_caller_sub(request, body.callerSub)",
    ],
)
def test_s7_accepts_verified_first_precedence(line):
    assert not _prefers_body(line)


@pytest.mark.unit
def test_s7_flags_a_route_with_no_verified_identity_at_all():
    assert _prefers_body('caller_sub = str(body.get("callerSub") or "")')


# --- S8: a contradicting body identity must be refused -----------------------
#
# S8 requires ONE function that names the verified identity, compares it (!=)
# and refuses. Each half alone must not satisfy it.


def _refuses_conflict(module_text: str) -> bool:
    return any(
        any(t in fn for t in scanner.VERIFIED_IDENTITY_TOKENS)
        and "!=" in fn
        and any(t in fn for t in scanner.IDENTITY_REJECT_TOKENS)
        for fn in scanner.module_functions(module_text)
    )


@pytest.mark.unit
def test_s8_flags_silent_preference():
    """Resolving a precedence without refusing a contradiction is not enough."""
    assert not _refuses_conflict(
        "def _resolve(request, claimed):\n"
        "    return _caller_sub(request) or claimed\n"
    )


@pytest.mark.unit
def test_s8_accepts_an_explicit_refusal():
    assert _refuses_conflict(
        "def resolve_caller_sub(verified, claimed):\n"
        "    if claimed and claimed != verified:\n"
        "        raise CallerIdentityConflict('mismatch')\n"
        "    return verified or claimed\n"
    )


@pytest.mark.unit
def test_s8_is_not_satisfied_by_an_unrelated_refusal_elsewhere():
    """A 403 in a different function must not count as the conflict check."""
    assert not _refuses_conflict(
        "def _resolve(request, claimed):\n"
        "    return _caller_sub(request) or claimed\n"
        "\n"
        "def other(x):\n"
        "    if x != 1:\n"
        "        raise HTTPException(status_code=403)\n"
    )


# --- S9 + live repo state ----------------------------------------------------


@pytest.mark.unit
def test_live_scan_declares_every_function_url_and_route():
    """Every AWS::Lambda::Url in the repo template is declared, with its routes.

    This is the S6 half that has to run against the real files: it is what makes
    a NEW Function URL (or a new route on this one) fail the gate until its
    authorization is declared.
    """
    findings = scanner.run_checks(strict=False)
    s6 = [f for f in findings if f.check == "S6" and f.level == "FAIL"]
    assert not s6, [f.message for f in s6]


@pytest.mark.unit
def test_live_scan_has_no_function_url_failures():
    findings = scanner.run_checks(strict=False)
    fails = [
        f for f in findings if f.check in ("S6", "S7", "S8", "S9") and f.level == "FAIL"
    ]
    assert not fails, [f"[{f.check}] {f.op}: {f.message}" for f in fails]
