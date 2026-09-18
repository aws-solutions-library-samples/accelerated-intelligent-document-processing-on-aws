# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The dispatcher denies by default, and its manifest cannot drift.

Under AppSync a field with no ``cognito_groups`` directive the caller satisfied
was rejected at the API layer. The REST API's Cognito authorizer only
authenticates, so before this layer existed authorization was opt-in per
resolver: an operation whose resolver omitted its check was reachable by any
authenticated caller, and a newly added operation was open until somebody
remembered. ``authz.py`` restores the closed default — every request must name an
operation in the bundled manifest AND satisfy its groups.

Two things are asserted here, and both are enumerated FROM THE CODE:

* **Parity.** The committed ``api_rbac_manifest.json`` is exactly what the
  generator produces from ``scripts/api_rbac_expectations.yaml``; every field
  reachable through the dispatcher (``FIELD_ALIASES`` read off the loaded module,
  ``ddb_direct._HANDLED`` read off the loaded module, and the template's
  field->function map) has an entry; no entry is stale; and
  ``ddb_direct._REQUIRED_GROUPS`` agrees with the manifest for the 11 ops it
  covers. Nothing here is a hardcoded inventory of operations — a list typed into
  a test is the same defect as a list typed into the code, and this repo has been
  bitten by hardcoded gate inventories before. The enumerations are floor-checked
  instead, so a parser or import that silently yields nothing fails loudly rather
  than passing vacuously.
* **The deny paths.** No group, insufficient group, unmapped field, an IAM-only
  op, a body that tries to assert its own groups, and an unreadable manifest all
  produce 403; a correctly grouped caller still gets through.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "nested" / "api-resolvers").is_dir():
            return parent
    raise RuntimeError("Could not locate repo root containing nested/api-resolvers")


_REPO = _find_repo_root()
_API_RESOLVERS = _REPO / "nested" / "api-resolvers"
_DISPATCHER_DIR = _API_RESOLVERS / "src" / "lambda" / "http_api_dispatcher"
_MANIFEST_PATH = _DISPATCHER_DIR / "api_rbac_manifest.json"
_GENERATOR = _REPO / "scripts" / "sdlc" / "generate_api_rbac_manifest.py"
_TEMPLATE = _API_RESOLVERS / "template.yaml"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_dispatcher(monkeypatch):
    """The dispatcher and its siblings, with boto3 clients stubbed."""
    import boto3

    monkeypatch.setattr(boto3, "client", lambda service, *a, **k: object())
    if str(_DISPATCHER_DIR) not in sys.path:
        sys.path.insert(0, str(_DISPATCHER_DIR))
    _load_module("authz", _DISPATCHER_DIR / "authz.py")
    _load_module("ddb_direct", _DISPATCHER_DIR / "ddb_direct.py")
    _load_module("validation", _DISPATCHER_DIR / "validation.py")
    return _load_module("index", _DISPATCHER_DIR / "index.py")


def _load_scanner():
    """scan_api_rbac's op-universe extractors (the same ones CI's S1 check uses)."""
    scripts_dir = _REPO / "scripts" / "sdlc"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return _load_module("scan_api_rbac", scripts_dir / "scan_api_rbac.py")


def _load_generator():
    scripts_dir = _REPO / "scripts" / "sdlc"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return _load_module("generate_api_rbac_manifest", _GENERATOR)


@pytest.fixture
def idx(monkeypatch):
    return _load_dispatcher(monkeypatch)


@pytest.fixture
def authz(monkeypatch):
    _load_dispatcher(monkeypatch)
    return sys.modules["authz"]


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(_MANIFEST_PATH.read_text())["operations"]


# ------------------------------ test plumbing ------------------------------- #
ARN = "arn:aws:lambda:us-east-1:123456789012:function:x"


class _FakeLambda:
    """Stand-in for the boto3 lambda client that returns a fixed payload."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def invoke(self, **kwargs):
        self.calls += 1
        return {"Payload": io.BytesIO(json.dumps(self.payload).encode("utf-8"))}


def _http_event(field, arguments=None, groups=None, extra_claims=None):
    """An API Gateway event whose groups claim comes from the authorizer.

    ``cognito:groups`` is placed where the JWT authorizer puts it, which is the
    only place ``api_adapter.normalize_event`` reads it from — deliberately not
    in the body, which the caller controls.
    """
    claims = {"sub": "11111111-2222-3333-4444-555555555555", "email": "u@example.com"}
    if groups is not None:
        claims["cognito:groups"] = groups
    claims.update(extra_claims or {})
    return {
        "requestContext": {
            "http": {"method": "POST"},
            "authorizer": {"jwt": {"claims": claims}},
        },
        "pathParameters": {"field": field},
        "body": json.dumps({"arguments": arguments or {}}),
        "headers": {},
    }


def _error(resp) -> dict:
    return json.loads(resp["body"])["errors"][0]


# ================================== parity ================================== #
def test_manifest_matches_expectations_no_drift():
    """The committed manifest is what the generator produces, byte for byte."""
    gen = _load_generator()
    assert gen.render() == _MANIFEST_PATH.read_text(), (
        "api_rbac_manifest.json is out of date with scripts/api_rbac_expectations.yaml; "
        "regenerate with scripts/sdlc/generate_api_rbac_manifest.py"
    )


def test_every_dispatcher_reachable_field_has_a_manifest_entry(idx, manifest):
    """Enumerated from the loaded dispatcher + the template, not from a list here.

    ``FIELD_ALIASES`` and ``ddb_direct._HANDLED`` are read off the imported
    modules, so a field added to either is covered the moment it exists. The
    field->function map lives only in the template, so that one is extracted with
    the scanner's own parser (the parser CI's S1 check depends on).
    """
    scanner = _load_scanner()
    ddb_direct = sys.modules["ddb_direct"]

    aliases = set(idx.FIELD_ALIASES)
    ddb_handled = set(ddb_direct._HANDLED)
    mapped = scanner.field_function_map_ops(_TEMPLATE.read_text())

    # Floor checks: an enumeration that silently collapses to nothing would make
    # every assertion below pass while proving nothing.
    assert len(aliases) > 40, "FIELD_ALIASES enumeration looks empty/broken"
    assert len(ddb_handled) >= 10, "ddb_direct._HANDLED enumeration looks broken"
    assert len(mapped) > 30, "field->function map extraction looks broken"

    reachable = aliases | ddb_handled | mapped
    missing = sorted(reachable - set(manifest))
    assert not missing, (
        f"{len(missing)} field(s) reachable through the dispatcher have no "
        f"required-groups entry and would be DENIED at runtime: {missing}. "
        "Declare them in scripts/api_rbac_expectations.yaml and regenerate the "
        "manifest."
    )


def test_manifest_carries_no_field_that_is_not_reachable(idx, manifest):
    """A stale entry is a policy nobody can reach — and hides a rename."""
    scanner = _load_scanner()
    ddb_direct = sys.modules["ddb_direct"]
    reachable = (
        set(idx.FIELD_ALIASES)
        | set(ddb_direct._HANDLED)
        | scanner.field_function_map_ops(_TEMPLATE.read_text())
    )
    stale = sorted(set(manifest) - reachable)
    assert not stale, f"manifest entries for non-routable ops: {stale}"


def test_every_manifest_entry_is_a_group_list_or_a_known_sentinel(manifest):
    gen = _load_generator()
    for field, required in manifest.items():
        if isinstance(required, str):
            assert required in gen.SENTINELS, f"{field}: unknown policy {required!r}"
        else:
            assert isinstance(required, list) and required, f"{field}: empty groups"
            assert all(isinstance(g, str) for g in required), f"{field}: bad group"


def test_ddb_direct_required_groups_agrees_with_the_manifest(authz, manifest):
    """The DynamoDB-direct table is defence in depth, so it must not contradict.

    ``ddb_direct`` keeps its own group check (it serves those ops without a
    resolver hop). Both tables now describe the same policy, and the failure mode
    of two copies is silent divergence — so compare them, over every key
    ``ddb_direct`` declares.
    """
    ddb_direct = sys.modules["ddb_direct"]
    required_groups = ddb_direct._REQUIRED_GROUPS
    assert len(required_groups) >= 10, "_REQUIRED_GROUPS looks empty/broken"

    for field, required in required_groups.items():
        assert field in manifest, f"{field}: enforced in ddb_direct but not declared"
        expected = manifest[field]
        if required is None:
            assert expected == "ANY", (
                f"{field}: ddb_direct allows any authenticated caller but the "
                f"manifest requires {expected}"
            )
        elif required is ddb_direct._IAM_ONLY:
            assert expected == "IAM_ONLY", (
                f"{field}: ddb_direct rejects all Cognito callers but the manifest "
                f"says {expected}"
            )
        else:
            assert sorted(required) == expected, (
                f"{field}: ddb_direct requires {sorted(required)} but the manifest "
                f"requires {expected}"
            )

    # And every op ddb_direct serves has a policy of its own (not just an entry
    # in the manifest) — the fail-open shape the manifest is meant to close.
    assert set(ddb_direct._HANDLED) == set(required_groups), (
        "ddb_direct serves an op with no _REQUIRED_GROUPS entry: "
        f"{sorted(set(ddb_direct._HANDLED) - set(required_groups))}"
    )


def test_generator_rejects_a_group_the_stack_does_not_create():
    """A typo'd group name denies an operation to everyone, so it must not build."""
    gen = _load_generator()
    with pytest.raises(gen.GeneratorError, match="does not create"):
        gen.build_manifest(
            {"operations": {"someOp": {"groups": ["Admn"]}}},
            valid_groups={"Admin", "Author"},
        )


def test_generator_rejects_an_unknown_policy_sentinel():
    gen = _load_generator()
    with pytest.raises(gen.GeneratorError):
        gen.build_manifest({"operations": {"someOp": {"groups": "EVERYONE"}}})


def test_cognito_group_names_come_from_the_root_template():
    """The generator's group vocabulary is the stack's, not a copy in the script."""
    gen = _load_generator()
    names = gen.cognito_group_names((_REPO / "template.yaml").read_text())
    # Floor + the two ends of the precedence order, which cannot disappear
    # without the whole authorization model changing.
    assert len(names) >= 4, f"UserPoolGroup extraction looks broken: {names}"
    assert {"Admin", "Viewer"} <= names


# ================================ deny paths ================================ #
def test_caller_in_no_group_is_denied(idx):
    """Self-signup can produce a user in no group at all."""
    resp = idx.handler(_http_event("listUsers", groups=None))
    assert resp["statusCode"] == 403
    assert _error(resp)["errorType"] == "Unauthorized"


def test_caller_in_an_insufficient_group_is_denied(idx):
    resp = idx.handler(_http_event("listUsers", groups=["Viewer"]))
    assert resp["statusCode"] == 403
    assert _error(resp)["errorType"] == "Unauthorized"


def test_field_with_no_manifest_entry_is_denied(idx):
    """Unmapped means denied. This is the default the layer exists to change."""
    resp = idx.handler(_http_event("someNewOperation", groups=["Admin"]))
    assert resp["statusCode"] == 403, (
        "an operation with no declared groups must be denied, not dispatched"
    )
    assert _error(resp)["errorType"] == "Unauthorized"


def test_iam_only_op_is_denied_even_for_admin(idx):
    resp = idx.handler(
        _http_event("updateAgentJobStatus", {"jobId": "x"}, groups=["Admin"])
    )
    assert resp["statusCode"] == 403


def test_groups_asserted_in_the_request_body_are_ignored(idx):
    """The caller's groups come from the verified claim, never from the payload."""
    event = _http_event(
        "listUsers",
        {"cognito:groups": ["Admin"], "groups": ["Admin"], "identity": {"claims": {}}},
        groups=["Viewer"],
    )
    resp = idx.handler(event)
    assert resp["statusCode"] == 403


def test_authorization_runs_before_argument_validation(idx):
    """A denied caller must not learn the operation's argument shape."""
    # getDocument requires ObjectKey; omitting it is a 400 for an allowed caller.
    # listUsers is Admin-only, so a Viewer sending garbage args must see the 403.
    resp = idx.handler(_http_event("listUsers", {"bogusArg": 1}, groups=["Viewer"]))
    assert resp["statusCode"] == 403
    assert _error(resp)["errorType"] == "Unauthorized"


def test_unreadable_manifest_denies_everything(idx, monkeypatch):
    """Fail closed: no manifest is not 'no restrictions'."""
    authz_mod = sys.modules["authz"]
    monkeypatch.setattr(authz_mod, "REQUIRED_GROUPS", {})
    resp = idx.handler(_http_event("getDocument", {"ObjectKey": "k"}, groups=["Admin"]))
    assert resp["statusCode"] == 403


def test_manifest_load_failure_yields_an_empty_map(authz, monkeypatch):
    monkeypatch.setattr(authz, "_MANIFEST_PATH", "/nonexistent/api_rbac_manifest.json")
    assert authz._load_manifest() == {}


def test_manifest_with_an_unsupported_version_is_refused(authz, tmp_path, monkeypatch):
    bad = tmp_path / "api_rbac_manifest.json"
    bad.write_text(json.dumps({"version": 99, "operations": {"getDocument": "ANY"}}))
    monkeypatch.setattr(authz, "_MANIFEST_PATH", str(bad))
    assert authz._load_manifest() == {}


# ================================ allow paths =============================== #
def test_correctly_grouped_caller_reaches_the_resolver(idx, monkeypatch):
    fake = _FakeLambda({"users": []})
    monkeypatch.setattr(idx, "_lambda", fake)
    # listUsers is an alias routed to the UserManagement resolver, registered in
    # the map under createUser.
    assert idx.FIELD_ALIASES["listUsers"] == "createUser"
    idx.FIELD_FUNCTION_MAP["createUser"] = ARN

    resp = idx.handler(_http_event("listUsers", groups=["Admin"]))

    assert resp["statusCode"] == 200
    assert fake.calls == 1
    assert json.loads(resp["body"]) == {"users": []}


def test_any_auth_op_is_allowed_without_a_group(authz):
    """ANY ops stay open to any authenticated caller (the gateway authenticates)."""
    assert authz.REQUIRED_GROUPS["getDocument"] == "ANY"
    authz.enforce("getDocument", {"identity": {"claims": {}}})


def test_one_matching_group_is_enough(authz):
    required = authz.REQUIRED_GROUPS["listDiscoveryJobs"]
    assert set(required) == {"Admin", "Author"}
    authz.enforce(
        "listDiscoveryJobs",
        {"identity": {"claims": {"cognito:groups": ["Author"]}}},
    )


def test_a_string_groups_claim_is_tolerated(authz):
    """The authorizer can flatten the claim to a single string."""
    assert authz.caller_groups(
        {"identity": {"claims": {"cognito:groups": "Admin"}}}
    ) == ["Admin"]
