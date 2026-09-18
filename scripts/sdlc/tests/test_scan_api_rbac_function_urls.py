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


# --- driving run_checks over a deliberately defective fixture tree -----------
#
# Everything above tests the PREDICATES in isolation, which is necessary but not
# sufficient: the predicates were re-implemented here as `_prefers_body` and
# `_refuses_conflict`, so deleting the S7 and S8 blocks from `run_checks`
# altogether left this whole suite green (measured: 11 passed, and the full
# 1007-test suite byte-identical). The tests below call `run_checks` itself
# against a fixture repository that CONTAINS the defect, so the check has to be
# wired up and reachable for them to pass.

_GOOD_APP = '''
@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/chat/agent")
async def chat_agent(request: Request, body: AgentChatRequest):
    caller_sub = _resolve_caller_sub(request, body.callerSub)
    return caller_sub
'''

_GOOD_SSE = '''
def resolve_caller_sub(verified, claimed):
    if claimed and claimed != verified:
        raise CallerIdentityConflict("mismatch")
    return verified or claimed
'''

_GROUP_GATE = '''
_AGENT_CHAT_GROUPS = ("Admin", "Author", "Viewer")


def _enforce_agent_chat_groups(event):
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups")
    if groups is not None and not set(groups) & set(_AGENT_CHAT_GROUPS):
        raise PermissionError("Unauthorized")
'''


def _make_fixture(
    tmp_path: Path,
    *,
    app_src: str = _GOOD_APP,
    sse_src: str = _GOOD_SSE,
    gate_src: str = _GROUP_GATE,
    target_line: str = "TargetFunctionArn: !Ref StreamFunction",
    code_uri: str = "src/lambda/stream/",
    route_policy: str = (
        "        groups: [Admin, Author, Viewer]\n"
        "        enforced_in: src/lambda/proc/index.py\n"
    ),
) -> Path:
    """A minimal repository tree `run_checks` can be pointed at.

    Only the files the checks actually open are created. `operations` is empty and
    the dispatcher stubs are empty, so S1-S5 contribute nothing and the S6-S9
    findings under test stand alone.
    """
    (tmp_path / "scripts").mkdir()
    nested = tmp_path / "nested" / "api-resolvers"
    disp = nested / "src" / "lambda" / "http_api_dispatcher"
    disp.mkdir(parents=True)
    (disp / "index.py").write_text("")
    (disp / "ddb_direct.py").write_text("")
    api = nested / "src" / "api"
    api.mkdir(parents=True)
    (api / "schema.graphql").write_text("type Query {\n  noop: String\n}\n")
    (nested / "template.yaml").write_text("Resources: {}\n")

    stream = tmp_path / "src" / "lambda" / "stream"
    stream.mkdir(parents=True)
    (stream / "app.py").write_text(app_src)
    (stream / "sse.py").write_text(sse_src)
    proc = tmp_path / "src" / "lambda" / "proc"
    proc.mkdir(parents=True)
    (proc / "index.py").write_text(gate_src)

    (tmp_path / "template.yaml").write_text(
        "Resources:\n"
        "  StreamUrl:\n"
        "    Type: AWS::Lambda::Url\n"
        "    Properties:\n"
        f"      {target_line}\n"
        "      AuthType: AWS_IAM\n"
        "      InvokeMode: RESPONSE_STREAM\n"
        "  StreamFunction:\n"
        "    Type: AWS::Serverless::Function\n"
        "    Properties:\n"
        f"      CodeUri: {code_uri}\n"
    )
    (tmp_path / "scripts" / "api_rbac_expectations.yaml").write_text(
        "operations: {}\n"
        "known_gaps:\n"
        "  GAP-99:\n"
        "    description: a residual transport limitation\n"
        "function_url_endpoints:\n"
        "  StreamUrl:\n"
        "    auth_type: AWS_IAM\n"
        "    handler: src/lambda/stream/app.py\n"
        "    routes:\n"
        # GET /health is deliberately NOT declared: it is in
        # FUNCTION_URL_OPEN_ROUTES, so the live expectations do not declare it
        # either, and declaring it would subject a liveness probe to S7.
        "      POST /chat/agent:\n" + route_policy
    )
    return tmp_path


def _fails(tmp_path: Path, check: str, strict: bool = False) -> list[str]:
    return [
        f.message
        for f in scanner.run_checks(strict=strict, repo=tmp_path)
        if f.check == check and f.level == "FAIL"
    ]


def _levels(tmp_path: Path, check: str, strict: bool = False) -> list[str]:
    return [
        f.level
        for f in scanner.run_checks(strict=strict, repo=tmp_path)
        if f.check == check
    ]


@pytest.mark.unit
def test_fixture_baseline_is_clean(tmp_path):
    """The fixture must pass before each mutation, or nothing below means anything."""
    repo = _make_fixture(tmp_path)
    for check in ("S6", "S7", "S8", "S9"):
        assert not _fails(repo, check)


@pytest.mark.unit
def test_run_checks_reports_s7_on_a_body_first_route(tmp_path):
    repo = _make_fixture(
        tmp_path,
        app_src=_GOOD_APP.replace(
            "caller_sub = _resolve_caller_sub(request, body.callerSub)",
            'caller_sub = body.callerSub or _caller_sub(request)',
        ),
    )
    messages = _fails(repo, "S7")
    assert any("prefers the request-body caller identity" in m for m in messages), (
        messages
    )


@pytest.mark.unit
def test_run_checks_reports_s7_when_no_verified_identity_is_resolved(tmp_path):
    repo = _make_fixture(
        tmp_path,
        app_src=_GOOD_APP.replace(
            "caller_sub = _resolve_caller_sub(request, body.callerSub)",
            "caller_sub = body.callerSub",
        ),
    )
    assert any(
        "resolves no transport-verified caller identity" in m
        for m in _fails(repo, "S7")
    )


@pytest.mark.unit
def test_run_checks_reports_s8_when_nothing_refuses_a_conflict(tmp_path):
    """The route reads a body identity but no function in the package refuses one."""
    repo = _make_fixture(
        tmp_path,
        sse_src="def resolve_caller_sub(verified, claimed):\n"
        "    return verified or claimed\n",
    )
    assert any("never refuses one that contradicts" in m for m in _fails(repo, "S8")), (
        _fails(repo, "S8")
    )


@pytest.mark.unit
def test_run_checks_reports_s9_when_the_handler_has_no_group_gate(tmp_path):
    repo = _make_fixture(tmp_path, gate_src="def handler(event, ctx):\n    return {}\n")
    assert any("no group-enforcement pattern found" in m for m in _fails(repo, "S9"))


@pytest.mark.unit
def test_run_checks_reports_s9_when_the_group_lists_disagree(tmp_path):
    """Both directions: the code naming MORE groups than declared also fails.

    Widening was the direction the original containment test missed.
    """
    repo = _make_fixture(
        tmp_path,
        gate_src=_GROUP_GATE.replace(
            '("Admin", "Author", "Viewer")', '("Admin", "Author", "Viewer", "Reviewer")'
        ),
    )
    assert any("but the route declares" in m for m in _fails(repo, "S9"))


# --- Fix 4: the TargetFunctionArn must actually be resolved ------------------
#
# S6 resolves the URL's target function and checks the declared handler lives
# under that function's CodeUri, so the scan cannot be pointed at a different
# (still-clean) source file. Reading only `!Ref` made this fail OPEN: with
# `!GetAtt` the target came back empty, `function_code_uri` returned empty, and
# the containment check was skipped entirely. Measured on a decoy handler
# package: `!Ref` gave 1 FAIL, `!GetAtt` gave 0 FAIL.


@pytest.mark.unit
@pytest.mark.parametrize(
    "target_line",
    [
        "TargetFunctionArn: !Ref StreamFunction",
        "TargetFunctionArn: !GetAtt StreamFunction.Arn",
        "TargetFunctionArn: !GetAtt StreamFunction",
        "TargetFunctionArn:\n        Ref: StreamFunction",
        "TargetFunctionArn:\n        Fn::GetAtt: [StreamFunction, Arn]",
    ],
)
def test_s6_resolves_the_target_in_every_reference_form(tmp_path, target_line):
    urls = scanner.lambda_url_resources(
        "Resources:\n"
        "  StreamUrl:\n"
        "    Type: AWS::Lambda::Url\n"
        "    Properties:\n"
        f"      {target_line}\n"
        "      AuthType: AWS_IAM\n"
    )
    assert urls["StreamUrl"]["target"] == "StreamFunction", urls


@pytest.mark.unit
@pytest.mark.parametrize(
    "target_line",
    [
        "TargetFunctionArn: !Ref StreamFunction",
        "TargetFunctionArn: !GetAtt StreamFunction.Arn",
    ],
)
def test_s6_catches_a_handler_outside_the_targets_code_uri(tmp_path, target_line):
    """A decoy: the URL targets one function, the expectations name another's file.

    Under the old `!Ref`-only regex the `!GetAtt` case validated the decoy file
    and never read the real one.
    """
    repo = _make_fixture(tmp_path, target_line=target_line)
    (repo / "src" / "lambda" / "decoy").mkdir(parents=True)
    (repo / "src" / "lambda" / "decoy" / "app.py").write_text(_GOOD_APP)
    text = (repo / "scripts" / "api_rbac_expectations.yaml").read_text()
    (repo / "scripts" / "api_rbac_expectations.yaml").write_text(
        text.replace("src/lambda/stream/app.py", "src/lambda/decoy/app.py")
    )
    assert any("but expectations name handler" in m for m in _fails(repo, "S6")), (
        _fails(repo, "S6")
    )


@pytest.mark.unit
def test_s6_fails_when_the_target_cannot_be_resolved_at_all(tmp_path):
    """A scanner that cannot find its subject must say so, not pass silently."""
    repo = _make_fixture(
        tmp_path,
        target_line=(
            'TargetFunctionArn: !Sub "arn:${AWS::Partition}:lambda:...:function:x"'
        ),
    )
    assert any("could not resolve TargetFunctionArn" in m for m in _fails(repo, "S6"))


@pytest.mark.unit
def test_s6_fails_when_the_target_has_no_resolvable_code_uri(tmp_path):
    repo = _make_fixture(tmp_path)
    text = (repo / "template.yaml").read_text()
    (repo / "template.yaml").write_text(
        text.replace(
            "      CodeUri: src/lambda/stream/\n", "      Runtime: python3.12\n"
        )
    )
    assert any("has no resolvable CodeUri" in m for m in _fails(repo, "S6"))


# --- Fix 2: residual_gap must not downgrade, and a typo must not be silent ---


@pytest.mark.unit
def test_residual_gap_does_not_downgrade_a_finding(tmp_path):
    """`residual_gap:` records an accepted transport limitation for the register.

    It must NOT soften the S6-S9 checks on the same route, unlike `known_gap:`.
    The distinction is load-bearing and was unenforced: changing one word
    (`residual_gap` -> `known_gap`) on the live expectations file turned the FAIL
    into a WARN with the whole suite still green.
    """
    repo = _make_fixture(
        tmp_path,
        gate_src="def handler(event, ctx):\n    return {}\n",
        route_policy=(
            "        groups: [Admin, Author, Viewer]\n"
            "        enforced_in: src/lambda/proc/index.py\n"
            "        residual_gap: GAP-99\n"
        ),
    )
    assert _levels(repo, "S9") == ["FAIL"], _levels(repo, "S9")


@pytest.mark.unit
def test_known_gap_does_downgrade_the_same_finding(tmp_path):
    """The other half of the distinction, so the test above cannot pass vacuously."""
    repo = _make_fixture(
        tmp_path,
        gate_src="def handler(event, ctx):\n    return {}\n",
        route_policy=(
            "        groups: [Admin, Author, Viewer]\n"
            "        enforced_in: src/lambda/proc/index.py\n"
            "        known_gap: GAP-99\n"
        ),
    )
    assert _levels(repo, "S9") == ["WARN"]
    # ...and --strict still fails on it, which is what the release gate uses.
    assert _levels(repo, "S9", strict=True) == ["FAIL"]


@pytest.mark.unit
@pytest.mark.parametrize("typo", ["residual_gapp", "enforced_ni", "group"])
def test_a_misspelled_route_key_is_a_failure_not_a_silent_no_op(tmp_path, typo):
    """Every route key is read by name, so a typo makes the setting VANISH.

    Measured before this check existed: writing `residual_gapp:` for
    `residual_gap:` left the scan at 0 FAIL / 2 WARN and exit 0, while removing
    GAP-07 from the accepted-risk register that `--strict` and the published
    security snapshot read. The only symptom was an S0 WARN about an unreferenced
    gap definition, which reads like housekeeping.
    """
    repo = _make_fixture(
        tmp_path,
        route_policy=(
            "        groups: [Admin, Author, Viewer]\n"
            "        enforced_in: src/lambda/proc/index.py\n"
            f"        {typo}: GAP-99\n"
        ),
    )
    assert any(
        f"unknown key '{typo}'" in m for m in _fails(repo, "S6")
    ), _fails(repo, "S6")


@pytest.mark.unit
def test_a_misspelled_endpoint_key_is_also_a_failure(tmp_path):
    repo = _make_fixture(tmp_path)
    text = (repo / "scripts" / "api_rbac_expectations.yaml").read_text()
    (repo / "scripts" / "api_rbac_expectations.yaml").write_text(
        text.replace(
            "    auth_type: AWS_IAM\n", "    auth_type: AWS_IAM\n    notes: x\n"
        )
    )
    assert any("unknown key 'notes'" in m for m in _fails(repo, "S6"))


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
