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
* **Fail-closed on a malformed manifest, in every shape.** A bare-string policy
  (``"Admin"`` rather than ``["Admin"]``) used to fail OPEN, because
  ``set("Admin")`` is a set of CHARACTERS that a caller in a one-character group
  satisfies; a non-iterable policy failed as a 500 rather than a 403. Both, plus
  a JSON ``null``, an empty list, a non-string member and an unknown sentinel,
  are now rejected when the manifest is loaded, and the deny-everything state is
  announced under an alarmable marker instead of being inferred from a wall of
  403s.

The events here are payload format **1.0** (``requestContext.authorizer.claims``),
which is what a REST API's Lambda proxy integration sends — see ``_http_event``.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

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
def dispatcher(monkeypatch):
    """Load the dispatcher and its siblings ONCE per test, and hand back all three.

    ``idx``, ``authz`` and ``ddb_direct`` are all derived from this one fixture on
    purpose. ``_load_dispatcher`` re-executes each module, so two independent
    loader fixtures in one test would leave the earlier one holding a module
    object that ``index`` no longer references — monkeypatching that stale copy's
    ``REQUIRED_GROUPS`` would then have no effect on the handler under test, and
    the test would pass while proving nothing. Deriving them from a single load
    makes the three fixtures the same objects ``index`` imported.

    Each module is re-executed per test, so ``authz.REQUIRED_GROUPS`` starts from
    the real committed manifest every time and no test can leak a policy into the
    next one.
    """
    idx_mod = _load_dispatcher(monkeypatch)
    return SimpleNamespace(
        index=idx_mod,
        authz=sys.modules["authz"],
        ddb_direct=sys.modules["ddb_direct"],
    )


@pytest.fixture
def idx(dispatcher):
    return dispatcher.index


@pytest.fixture
def authz(dispatcher):
    return dispatcher.authz


@pytest.fixture
def ddb_direct(dispatcher):
    return dispatcher.ddb_direct


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

    def invoke(self, **_kwargs):
        # Signature padding: boto3's invoke() is called with keyword arguments
        # this double does not need to inspect.
        self.calls += 1
        return {"Payload": io.BytesIO(json.dumps(self.payload).encode("utf-8"))}


def _http_event(field, arguments=None, groups=None, extra_claims=None):
    """An API Gateway event in the shape the LIVE API actually delivers.

    The API is an ``AWS::ApiGateway::RestApi`` (REST was required: only REST
    supports ``EndpointConfiguration: PRIVATE`` behind a VPC interface endpoint
    and can be fronted by WAFv2), despite the logical id reading like v2. A REST
    API with a Lambda proxy integration sends **payload format 1.0**, so a
    ``COGNITO_USER_POOLS`` authorizer's claims arrive at
    ``requestContext.authorizer.claims`` — NOT the v2.0
    ``requestContext.authorizer.jwt.claims``, and the method is
    ``requestContext.httpMethod``, not ``requestContext.http.method``. Testing
    against the v2.0 shape would exercise a payload production never sends.

    ``cognito:groups`` is placed where the authorizer puts it, which is the only
    place ``api_adapter.normalize_event`` reads it from — deliberately not in the
    body, which the caller controls. A REST Cognito authorizer flattens a
    multi-valued claim to a comma-joined string; ``_coerce_groups`` normalizes
    that and a list alike, and ``test_a_string_groups_claim_is_tolerated`` covers
    the flattened form, so the list form is used here for legibility.
    """
    claims = {"sub": "11111111-2222-3333-4444-555555555555", "email": "u@example.com"}
    if groups is not None:
        claims["cognito:groups"] = groups
    claims.update(extra_claims or {})
    return {
        "resource": "/op/{field}",
        "path": f"/op/{field}",
        "httpMethod": "POST",
        "requestContext": {
            "resourcePath": "/op/{field}",
            "httpMethod": "POST",
            "path": f"/prod/op/{field}",
            "authorizer": {"claims": claims},
        },
        "pathParameters": {"field": field},
        "body": json.dumps({"arguments": arguments or {}}),
        "headers": {},
        "isBase64Encoded": False,
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


def test_every_dispatcher_reachable_field_has_a_manifest_entry(
    idx, ddb_direct, manifest
):
    """Enumerated from the loaded dispatcher + the template, not from a list here.

    ``FIELD_ALIASES`` and ``ddb_direct._HANDLED`` are read off the imported
    modules, so a field added to either is covered the moment it exists. The
    field->function map lives only in the template, so that one is extracted with
    the scanner's own parser (the parser CI's S1 check depends on).
    """
    scanner = _load_scanner()

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


def test_manifest_carries_no_field_that_is_not_reachable(idx, ddb_direct, manifest):
    """A stale entry is a policy nobody can reach — and hides a rename."""
    scanner = _load_scanner()
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


def test_ddb_direct_required_groups_agrees_with_the_manifest(ddb_direct, manifest):
    """The DynamoDB-direct table is defence in depth, so it must not contradict.

    ``ddb_direct`` keeps its own group check (it serves those ops without a
    resolver hop). Both tables now describe the same policy, and the failure mode
    of two copies is silent divergence — so compare them, over every key
    ``ddb_direct`` declares.
    """
    required_groups = ddb_direct._REQUIRED_GROUPS
    assert len(required_groups) >= 10, "_REQUIRED_GROUPS looks empty/broken"

    app_groups = sorted(
        _load_generator().cognito_group_names((_REPO / "template.yaml").read_text())
    )
    for field, required in required_groups.items():
        assert field in manifest, f"{field}: enforced in ddb_direct but not declared"
        expected = manifest[field]
        if required is ddb_direct._ANY_AUTHENTICATED:
            assert expected == "ANY", (
                f"{field}: ddb_direct allows any authenticated caller but the "
                f"manifest requires {expected}"
            )
        elif required is ddb_direct._ANY_GROUP:
            # ddb_direct asserts only "the caller holds SOME group" (it has no
            # copy of the template to read the vocabulary from — see the note on
            # _ANY_GROUP there). That is only a faithful stand-in while the
            # manifest's policy really is the whole vocabulary: a narrower list
            # like ["Admin"] would be satisfied by a Viewer here. Pin it.
            assert expected == app_groups, (
                f"{field}: ddb_direct requires 'any assigned group', which stands "
                f"in for the full vocabulary {app_groups}, but the manifest "
                f"requires {expected} — name those groups in _REQUIRED_GROUPS "
                "instead of using _ANY_GROUP"
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


# ============================ the ANY_GROUP policy ========================== #
# "An assigned group, whichever one" — the product decision recorded in issue
# #979 for the document-content reads and the three mutations that used to be
# `ANY`. `ANY` means authenticated, not vetted: with AllowedSignUpEmailDomain set,
# template.yaml sets AllowAdminCreateUserOnly: false, so a user can self-register
# and holds a valid token whose cognito:groups claim is EMPTY.
#
# This list IS hardcoded, deliberately, unlike every enumeration in the parity
# section above. Those enumerate the op UNIVERSE, where a hardcoded inventory
# silently drops an operation out of coverage. This one is the DECISION itself, and
# pinning it is the point: widening any of these eleven back to `ANY` must fail a
# test rather than pass quietly as "one fewer group-restricted operation".
TIGHTENED_TO_ANY_GROUP = (
    # mutations
    "deleteAgentJob",
    "deleteChatSession",
    "sendChatDocumentMessage",
    # document content, and the means of obtaining it
    "getFileContents",
    "getFilePresignedUrl",
    "getDocument",
    "getDocumentVersion",
    "compareDocumentVersions",
    "listDocuments",
    "listDocumentsByDateRange",
    "queryKnowledgeBase",
)


def _app_groups() -> list[str]:
    """The groups the stack creates, read from the root template."""
    gen = _load_generator()
    return sorted(gen.cognito_group_names((_REPO / "template.yaml").read_text()))


@pytest.mark.parametrize("field", TIGHTENED_TO_ANY_GROUP)
def test_tightened_op_requires_an_assigned_group(field, manifest):
    """Each of the eleven names the full group vocabulary, not `ANY`."""
    assert manifest[field] == _app_groups(), (
        f"{field} is declared '{manifest[field]}' but the #979 decision is "
        "ANY_GROUP — an assigned group is required. Declaring it ANY again means "
        "a self-registered user in no group may call it."
    )


@pytest.mark.parametrize("field", TIGHTENED_TO_ANY_GROUP)
def test_tightened_op_denies_a_groupless_authenticated_caller(field, idx):
    """403 through the real handler, for a token with an empty groups claim.

    Driven through ``handler`` rather than ``authz.enforce`` so the assertion
    covers the whole request path a self-registered user's call takes — including
    that the denial happens before the resolver is invoked (``_lambda`` is never
    stubbed here, so reaching one would raise rather than quietly succeed).
    """
    resp = idx.handler(_http_event(field, groups=None))
    assert resp["statusCode"] == 403, (
        f"{field} must refuse a caller in no group; got {resp['statusCode']}"
    )
    assert _error(resp)["errorType"] == "Unauthorized"


@pytest.mark.parametrize("field", TIGHTENED_TO_ANY_GROUP)
def test_tightened_op_allows_a_caller_holding_any_single_app_group(field, authz):
    """Any ONE of the stack's groups is enough — the policy is not a narrowing.

    Every group is exercised separately, so an accidental `[Admin]` (which
    ``test_tightened_op_requires_an_assigned_group`` would also catch) or a
    vocabulary that stopped including a real group fails here with the group
    named.
    """
    groups = _app_groups()
    assert len(groups) >= 4, f"group vocabulary looks broken: {groups}"
    for group in groups:
        authz.enforce(field, {"identity": {"claims": {"cognito:groups": [group]}}})


def test_any_group_expands_to_the_template_vocabulary_not_a_list_in_the_yaml():
    """The sentinel is resolved against template.yaml on every build.

    This is the property that makes ANY_GROUP worth a new concept: a sixth
    ``AWS::Cognito::UserPoolGroup`` joins the set without editing any operation,
    where five names written out per operation would silently stop covering it.
    """
    import yaml

    gen = _load_generator()
    spec = yaml.safe_load(
        (_REPO / "scripts" / "api_rbac_expectations.yaml").read_text()
    )
    declared = sorted(
        name
        for name, entry in spec["operations"].items()
        if entry.get("groups") == gen.ANY_GROUP
    )
    assert declared == sorted(TIGHTENED_TO_ANY_GROUP), (
        "the operations declared ANY_GROUP have changed; update the #979 decision "
        "list in this test deliberately, not to make it pass"
    )

    rendered = json.loads(gen.render())["operations"]
    for name in declared:
        assert rendered[name] == _app_groups()

    # And a different vocabulary really does produce a different manifest, so the
    # equality above is not an accident of the two happening to be the same list.
    six = gen.build_manifest(
        {"operations": {"someOp": {"groups": gen.ANY_GROUP}}},
        valid_groups={"Admin", "Viewer", "Auditor"},
    )
    assert six["operations"]["someOp"] == ["Admin", "Auditor", "Viewer"]


def test_generator_refuses_any_group_when_the_template_declares_no_groups():
    """An empty vocabulary would expand to `[]`, which denies everyone silently."""
    gen = _load_generator()
    with pytest.raises(gen.GeneratorError, match="cannot be resolved"):
        gen.build_manifest(
            {"operations": {"someOp": {"groups": gen.ANY_GROUP}}}, valid_groups=set()
        )


def test_any_group_reaching_the_manifest_is_refused_not_read_as_permissive(
    idx, authz, tmp_path, monkeypatch
):
    """The runtime has no way to resolve the sentinel, so it must not try.

    ``ANY_GROUP`` in ``api_rbac_manifest.json`` means the generator did not run —
    a build fault. The dispatcher treats it as one: the policy is a bare string
    that is not one of its two sentinels, so the whole manifest is rejected and
    every operation is denied. The failure mode that matters is the opposite one,
    an unknown string being waved through, so assert an ADMIN is refused too.
    """
    bad = tmp_path / "api_rbac_manifest.json"
    bad.write_text(
        json.dumps({"version": 1, "operations": {"getDocument": "ANY_GROUP"}}, indent=2)
    )
    monkeypatch.setattr(authz, "_MANIFEST_PATH", str(bad))
    loaded = authz._load_manifest()
    assert loaded == {}, "an unresolved ANY_GROUP must reject the whole manifest"

    monkeypatch.setattr(authz, "REQUIRED_GROUPS", loaded)
    resp = idx.handler(_http_event("getDocument", {"ObjectKey": "x"}, groups=["Admin"]))
    assert resp["statusCode"] == 403


@pytest.mark.parametrize(
    "field",
    ("getDocument", "deleteAgentJob"),  # the ddb_direct-served pair
)
def test_ddb_direct_also_refuses_a_groupless_caller(field, ddb_direct):
    """The in-process handlers keep their own check, and it is not a no-op.

    ``authz.enforce`` is the floor, but ``ddb_direct`` serves these two without a
    resolver hop and keeps a second check. Assert both directions so the sentinel
    cannot be mistaken for ``_ANY_AUTHENTICATED``.
    """
    assert ddb_direct._REQUIRED_GROUPS[field] is ddb_direct._ANY_GROUP
    with pytest.raises(PermissionError, match="requires an assigned group"):
        ddb_direct._enforce_rbac(field, {"identity": {"claims": {}}})
    ddb_direct._enforce_rbac(
        field, {"identity": {"claims": {"cognito:groups": ["Viewer"]}}}
    )


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


def test_an_invocation_cannot_supply_the_identity_the_group_check_reads(
    idx, manifest, monkeypatch
):
    """A direct invocation cannot state its own groups and be believed (issue #978).

    ``authz.enforce`` reads the groups out of ``identity.claims`` on the normalized
    event, which is trustworthy exactly as far as ``api_adapter.normalize_event``
    makes it so. Before the fix, an event carrying its own top-level ``arguments``
    and ``identity`` was passed through that function unchanged, so the group
    comparison was made against the caller's own claim and a payload asserting the
    operation's required group was dispatched.

    Nothing here is written down as a constant: the operation and the groups it
    needs both come from the committed manifest, so the probe follows the policy
    rather than a copy of it, and the contrast arm proves the refusal is about
    where the groups came from rather than about the operation being unroutable.
    """
    group_scoped = sorted(f for f, p in manifest.items() if isinstance(p, list))
    assert group_scoped, "no group-scoped operation in the manifest — probe is broken"
    field = group_scoped[0]
    required = manifest[field]

    fake = _FakeLambda({"leaked": "resolver output"})
    monkeypatch.setattr(idx, "_lambda", fake)
    idx.FIELD_FUNCTION_MAP[idx.FIELD_ALIASES.get(field, field)] = ARN

    # The legacy resolver event shape: top-level arguments + identity + info, with
    # the groups the operation requires asserted by the caller itself.
    asserted = {
        "arguments": {},
        "identity": {
            "claims": {"cognito:groups": list(required), "email": "self@example.com"},
            "username": "self@example.com",
        },
        "info": {"fieldName": field},
    }
    resp = idx.handler(asserted)

    assert resp["statusCode"] == 403, (
        f"{field} requires {required}; an invocation that merely ASSERTS those "
        "groups must be refused, not dispatched"
    )
    assert _error(resp)["errorType"] == "Unauthorized"
    assert fake.calls == 0, "the resolver must not be invoked for a refused identity"

    # Contrast: the same operation and the same groups, arriving where the gateway
    # authorizer puts them, are not refused. Only the status is compared (argument
    # validation may still answer 400 for an operation with required arguments) —
    # what matters is that the 403 above was about the identity's provenance.
    verified = idx.handler(_http_event(field, groups=list(required)))
    assert verified["statusCode"] != 403, (
        f"{field} with the same groups VERIFIED must not be refused — the probe "
        "above would then prove nothing about where the groups came from"
    )


def test_authorization_runs_before_argument_validation(idx):
    """A denied caller must not learn the operation's argument shape."""
    # getDocument requires ObjectKey; omitting it is a 400 for an allowed caller.
    # listUsers is Admin-only, so a Viewer sending garbage args must see the 403.
    resp = idx.handler(_http_event("listUsers", {"bogusArg": 1}, groups=["Viewer"]))
    assert resp["statusCode"] == 403
    assert _error(resp)["errorType"] == "Unauthorized"


def test_unreadable_manifest_denies_everything(idx, authz, monkeypatch):
    """Fail closed: no manifest is not 'no restrictions'."""
    monkeypatch.setattr(authz, "REQUIRED_GROUPS", {})
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


def _install_manifest(authz, tmp_path, monkeypatch, operations):
    """Write a manifest, reload the policy from it, and install the result.

    The module reads its manifest at IMPORT time, so pointing ``_MANIFEST_PATH``
    at a new file proves nothing on its own — ``REQUIRED_GROUPS`` still holds the
    policy loaded from the committed manifest. This re-runs ``_load_manifest`` and
    assigns the result, and returns it so the caller can assert that
    ``authz.REQUIRED_GROUPS`` really reflects the file just written before
    asserting on any behaviour.
    """
    path = tmp_path / "api_rbac_manifest.json"
    path.write_text(json.dumps({"version": 1, "operations": operations}))
    monkeypatch.setattr(authz, "_MANIFEST_PATH", str(path))
    loaded = authz._load_manifest()
    monkeypatch.setattr(authz, "REQUIRED_GROUPS", loaded)
    assert authz.REQUIRED_GROUPS is loaded, "the reloaded policy was not installed"
    return loaded


def test_a_bare_string_policy_is_refused_not_read_as_a_character_set(
    idx, authz, tmp_path, monkeypatch
):
    """``"Admin"`` is not ``["Admin"]``: ``set("Admin")`` is ``{'A','d','m','i','n'}``.

    A policy value that is a bare string rather than a list turns the membership
    test in ``enforce`` into a CHARACTER comparison, so a caller whose only group
    is the single-character group ``A`` satisfies an Admin-only operation. The
    generator emits only lists and the two sentinels, so this shape is not
    reachable through it and was never a live exposure — but the module docstring
    promises that a malformed manifest fails CLOSED, and in this one shape it
    failed OPEN. Rejecting it at load time is what makes the promise true.

    Driven through ``idx.handler``, not through a local copy of the predicate, so
    what is asserted is the dispatcher's real response.
    """
    loaded = _install_manifest(authz, tmp_path, monkeypatch, {"listUsers": "Admin"})
    assert loaded == {}, (
        "a bare-string policy must make the manifest unusable (deny everything), "
        f"not be carried through and compared character by character: {loaded!r}"
    )

    resp = idx.handler(_http_event("listUsers", groups=["A"]))
    assert resp["statusCode"] == 403, (
        "a caller whose only group is the single character 'A' must not satisfy an "
        f"Admin-only operation; got {resp['statusCode']}"
    )
    assert _error(resp)["errorType"] == "Unauthorized"


def test_a_non_iterable_policy_is_refused_rather_than_raising_at_comparison_time(
    idx, authz, tmp_path, monkeypatch
):
    """A number where a group list belongs must deny with 403, not 500.

    ``set(7)`` raises ``TypeError`` inside ``enforce``, which the dispatcher maps
    to 500 / ``InternalError`` — closed, but reported as an availability fault and
    with the Python exception text in the response body.
    """
    loaded = _install_manifest(authz, tmp_path, monkeypatch, {"getDocument": 7})
    assert loaded == {}

    resp = idx.handler(_http_event("getDocument", {"ObjectKey": "k"}, groups=["Admin"]))
    assert resp["statusCode"] == 403, (
        f"a malformed policy must deny, not error; got {resp['statusCode']} "
        f"{resp['body'][:120]}"
    )
    assert _error(resp)["errorType"] == "Unauthorized"


@pytest.mark.parametrize(
    "policy",
    [None, [], ["Admin", 3], "EVERYONE", {"groups": ["Admin"]}],
    ids=["json-null", "empty-list", "non-string-member", "unknown-sentinel", "object"],
)
def test_every_other_malformed_policy_shape_denies_everything(
    authz, tmp_path, monkeypatch, policy
):
    assert _install_manifest(authz, tmp_path, monkeypatch, {"listUsers": policy}) == {}


def test_a_well_formed_manifest_still_loads(authz, tmp_path, monkeypatch):
    """The validation must not reject the shapes the generator really emits."""
    good = {
        "listUsers": ["Admin"],
        "getDocument": "ANY",
        "updateAgentJobStatus": "IAM_ONLY",
    }
    assert _install_manifest(authz, tmp_path, monkeypatch, good) == good


# =========================== operability of deny-all ======================== #
def test_denying_everything_is_announced_under_a_stable_marker(authz, caplog):
    """An empty policy map must not look like a generic availability incident.

    Every request 403s, which per-request is indistinguishable from a legitimate
    authorization denial, so the cause is announced once at cold start under a
    fixed string an operator can alarm on. The marker's value is asserted here
    because an alarm elsewhere is keyed on it: renaming it silently would leave
    the alarm matching nothing.
    """
    assert authz.DENY_ALL_MARKER == "API_RBAC_MANIFEST_UNAVAILABLE"

    with caplog.at_level("ERROR"):
        authz._announce_if_denying_everything({})
    assert any(
        authz.DENY_ALL_MARKER in r.getMessage() and r.levelname == "ERROR"
        for r in caplog.records
    ), "deny-all must be announced at ERROR with the marker"

    caplog.clear()
    with caplog.at_level("ERROR"):
        authz._announce_if_denying_everything({"getDocument": "ANY"})
    assert not caplog.records, "a healthy manifest must not raise the marker"


def test_the_two_required_groups_tables_use_distinct_named_sentinels(authz, ddb_direct):
    """``None`` must not mean 'deny' in one table and 'allow anyone' in the other.

    ``authz.REQUIRED_GROUPS`` treats an absent entry as DENY; ``ddb_direct``
    treats its own sentinel as ALLOW-ANY-AUTHENTICATED. Both are consulted on the
    same request, so each names its own object and neither uses a bare ``None``.
    """
    sentinels = [
        authz._UNDECLARED,
        ddb_direct._ANY_AUTHENTICATED,
        ddb_direct._ANY_GROUP,
        ddb_direct._IAM_ONLY,
    ]
    for i, a in enumerate(sentinels):
        for b in sentinels[i + 1 :]:
            assert a is not b, "two policy sentinels are the same object"
    assert None not in ddb_direct._REQUIRED_GROUPS.values(), (
        "ddb_direct must not use a bare None for 'any authenticated caller'"
    )
    assert None not in authz.REQUIRED_GROUPS.values(), (
        "a null policy is rejected at load time, so None cannot appear here"
    )


# ============================ generator exit codes ========================== #
def test_generator_exits_2_when_the_expectations_file_is_missing(tmp_path):
    """Documented exit codes: 1 is drift, 2 is 'a file or an entry is wrong'."""
    gen = _load_generator()
    with pytest.raises(SystemExit) as excinfo:
        gen._load_yaml(tmp_path / "no_such_expectations.yaml")
    assert excinfo.value.code == 2


def test_generator_check_exits_2_when_the_committed_manifest_is_missing(
    tmp_path, monkeypatch
):
    gen = _load_generator()
    monkeypatch.setattr(gen, "MANIFEST_OUT", tmp_path / "absent.json")
    monkeypatch.setattr(sys, "argv", ["generate_api_rbac_manifest.py", "--check"])
    assert gen.main() == 2


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
    """ANY ops stay open to any authenticated caller (the gateway authenticates).

    ``getMyProfile`` rather than a document read: it returns the caller's own
    record and is the clearest operation a user in no group must still reach, so
    it is the one least likely to be retightened and make this test misleading.
    """
    assert authz.REQUIRED_GROUPS["getMyProfile"] == "ANY"
    authz.enforce("getMyProfile", {"identity": {"claims": {}}})


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
