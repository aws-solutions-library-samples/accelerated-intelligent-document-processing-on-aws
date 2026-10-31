# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The operations #1304 named answer a 4xx for the arguments the RBAC matrix sends.

Issue #1304 was a *measurement* defect wearing a status code. The live RBAC
harness drives every operation with a deliberately bogus argument to prove the
group check ran before the argument was used, and its positive arms can only ask
"was this caller NOT denied?". A 5xx satisfies that question while establishing
nothing — whether the resolver refused this caller before or after doing the work
is exactly what a 500 does not say — so ``inconclusive()`` records it as ERROR and
the cell is reported as a check that could not be run. Fifteen operations 500ed on
a not-found, and the **39 positive cells** over them (derived below, not quoted)
were unproven rather than passed.

The resolver-side half of the fix — ``ResourceNotFound`` for a named object that is
absent, ``ValueError``/``KeyError`` for a malformed argument — is pinned by
``scripts/tests/test_resolver_error_status_codes.py``. **Everything that file
asserts, it asserts by reading source text.** Its own docstring bounds that: it
reads the literal at the raise site, and it matches not-found wording. What it
therefore cannot see is whether the refusal it found is the refusal the matrix
*provokes*. Three ways to lose all 39 cells again with that gate green:

* the probe argument in ``scripts/api_rbac_expectations.yaml`` changes, so the
  request takes a branch that never reaches the ``ResourceNotFound`` line;
* a lookup ahead of that line starts raising something outside the 4xx map — a
  ``DynamoDBError``, a ``RuntimeError``, a ``KeyError`` on an environment variable
  (that last one maps to 400, so a status-only assertion would call it a pass,
  which is why every case below pins the message as well as the status);
* the exception is raised correctly and swallowed by a catch-all in a frame the
  AST ordering check does not reach.

So this module drives them. For each operation it runs the **real** resolver
handler against an empty table with the **arguments the expectations file
declares**, carries the exception across a Lambda invoke boundary the way the
deployed stack does, reads the status off the **real** dispatcher, and hands that
status to the **real** ``classify()`` from ``scripts/test_api_rbac.py``. The
assertion is on the verdict the live matrix would record, and the companion
assertion is the discrimination: the same cell, with the status put back to 5xx,
must become ERROR. A test that only checked "the verdict is PASS" would be
satisfied by a harness that passes on anything.

No AWS and no network: DynamoDB and S3 are moto, and the Lambda invoke is a double.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import boto3
import pytest
import yaml
from moto import mock_aws

pytestmark = pytest.mark.unit


def _find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "nested" / "api-resolvers").is_dir():
            return parent
    raise RuntimeError("Could not locate repo root containing nested/api-resolvers")


_REPO = _find_repo_root()
_RESOLVER = _REPO / "nested/api-resolvers/src/lambda/test_set_resolver/index.py"
_DISPATCHER_DIR = _REPO / "nested/api-resolvers/src/lambda/http_api_dispatcher"
_EXPECTATIONS = _REPO / "scripts/api_rbac_expectations.yaml"
_HARNESS = _REPO / "scripts/test_api_rbac.py"

# A well-formed ARN so the dispatcher's routing branch behaves as it does in the
# stack; the invoke itself is a double, so nothing resolves it.
_RESOLVER_ARN = "arn:aws:lambda:us-east-1:123456789012:function:test-set-resolver"

# The environment the deployed function has. Named here rather than left to the
# machine: `make/hermetic_aws.mk` strips the region and the credentials from every
# offline invocation, and this resolver builds three boto3 clients at module scope
# — botocore freezes a session's credentials into a client when the client is
# constructed, so a client built with none resolvable can never sign however many
# appear later (#988).
_RESOLVER_ENV = {
    "TRACKING_TABLE": "tracking-table",
    "INPUT_BUCKET": "input-bucket",
    "TEST_SET_BUCKET": "test-set-bucket",
    "TEST_SET_COPY_QUEUE_URL": (
        "https://sqs.us-east-1.amazonaws.com/123456789012/copy-queue"
    ),
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - fake moto credential  # pragma: allowlist secret
    "AWS_SESSION_TOKEN": "testing",  # nosec B105 - fake moto credential
}

# The Admin identity in the shape a REST Cognito authorizer delivers it. Admin is
# the role every one of these operations admits, so a refusal reaching the
# assertions below is the resolver's answer about the OBJECT and not about the
# caller — which is the whole point of the cells being measured.
#
# One role's live answer is NOT Admin's by construction: `assert_can_access_test_set`
# requires an Annotator to name the test set in their `allowedTestSets`, so an
# unseeded Annotator is refused before the lookup these statuses come from. The live
# harness seeds that scope with the ids the expectations file declares
# (`_seed_annotator_scope` / `test_set_ids_in_play` in `scripts/test_api_rbac.py`),
# which is what makes the seven Annotator cells reach the same not-found Admin does;
# `scripts/sdlc/tests/test_api_rbac_groupless_and_annotator.py` asserts the seed
# covers exactly those ids. Without it, those cells would measure the per-object
# control and score ERROR, not PASS.
_ADMIN_IDENTITY = {
    "claims": {"cognito:groups": ["Admin"], "email": "admin@example.com"}
}

# ---------------------------------------------------------------------------
# What each operation must answer, and in which words
# ---------------------------------------------------------------------------
# `(errorType, http status, a fragment of the message)`.
#
# The message fragment is load-bearing and not decoration. Two of these
# operations read an environment variable with `os.environ[...]` further down
# their own body, and a `KeyError` from that maps to 400 exactly as a missing
# argument does — so a case asserting only `400` would read a deployment
# misconfiguration as the client-error refusal it is measuring. The fragment is
# what separates them.
#
# The errorType is the string that crosses the lambda:Invoke boundary. The
# exception object does not: the dispatcher re-raises from `data["errorType"]`, so
# the class NAME is the contract and is what this table pins.
_NOT_FOUND = ("ResourceNotFound", 404, "not found")

PROBED_OPERATIONS = {
    "getTestSetDocuments": _NOT_FOUND,
    "getAnnotationQueue": _NOT_FOUND,
    "getDraftLabelJob": ("ResourceNotFound", 404, "Labeling job"),
    "estimateReviewEffort": _NOT_FOUND,
    "openTestSetAnnotationDraft": _NOT_FOUND,
    "updateTestSetDocumentSections": _NOT_FOUND,
    "reextractTestSetDocument": _NOT_FOUND,
    "resetTestSetLabels": _NOT_FOUND,
    "clearDraftLabels": _NOT_FOUND,
    "removeDocumentsFromTestSet": _NOT_FOUND,
    "addDocumentsToTestSet": _NOT_FOUND,
}

# `createEmptyTestSet` is the twelfth test-set operation #1304 listed and it is
# here rather than above because it answers 200: the harness's `{name: "__nope__"}`
# is a valid creation request and the resolver creates the set (the expectations
# file carries a note about the harness reaping such sets). A 200 is conclusive —
# "not denied" is observed, which is what the positive arm asks — so the cell
# proves its point without a 4xx. It still belongs in the derivation below, and in
# the verdict assertions, because it is one of the 15 and two of the 39.
CREATES_ON_A_BOGUS_ARGUMENT = "createEmptyTestSet"

# The three operations #1304 listed that are served by `feature-platform/` Lambdas
# rather than by this resolver. Each is already driven handler-first in its own
# suite under `feature-platform/main-stack-extensions/tests/`, with an id the
# deployment does not have and an assertion on `ResourceNotFound` — the launch-URL
# one uses the live harness's own `"__nope__"`, the other two a plausible feature
# id, which is the same condition. They are named here so the derivation of 39
# covers all fifteen operations, and they are not re-driven; the mapping from name
# to suite is checked below rather than left as a claim in this comment.
FEATURE_PLATFORM_OPERATIONS = {
    "getFeatureLaunchUrl": "test_get_feature_launch_url.py",
    "subscribeFeature": "test_subscribe_feature.py",
    "unsubscribeFeature": "test_unsubscribe_feature.py",
}
_FEATURE_PLATFORM_TESTS = _REPO / "feature-platform/main-stack-extensions/tests"

# The other half of #1304, and the half the live matrix does NOT provoke: the
# issue reports `KeyError: 'testSetId' / 'id' / 'fileName'` answering 500 where 400
# is right. The matrix sends a well-formed-but-absent id, so it lands on the
# not-found above and never on an operation's argument validation — which is why
# that validation needs its own measurement here rather than inheriting one.
#
# Blanking the id is the shape used because it is the one refusal every one of
# these operations has a rule for. Eight answer 400 `BadRequest` from their own
# `Invalid test set id` guard; three reach the lookup instead and answer the same
# 404 as above. Both are conclusive and neither is a 5xx, which is the property
# under test — but each operation's answer is pinned individually rather than
# accepted as "any 4xx", because "a 4xx of some kind" is satisfied by an operation
# that stopped validating its argument at all.
_BAD_REQUEST = ("ValueError", 400, "Invalid test set id")

BLANK_ID_ANSWERS = {
    "getTestSetDocuments": _BAD_REQUEST,
    "getAnnotationQueue": _BAD_REQUEST,
    "estimateReviewEffort": _BAD_REQUEST,
    "updateTestSetDocumentSections": _BAD_REQUEST,
    "reextractTestSetDocument": _BAD_REQUEST,
    "resetTestSetLabels": _BAD_REQUEST,
    "clearDraftLabels": _BAD_REQUEST,
    "removeDocumentsFromTestSet": _BAD_REQUEST,
    # These three look the blank id up rather than rejecting it, and answer the
    # not-found. `getDraftLabelJob` resolves the job id first, so blanking the set
    # id leaves it refusing the job.
    "openTestSetAnnotationDraft": _NOT_FOUND,
    "addDocumentsToTestSet": _NOT_FOUND,
    "getDraftLabelJob": ("ResourceNotFound", 404, "Labeling job"),
}

ISSUE_OPERATIONS = (
    tuple(PROBED_OPERATIONS)
    + (CREATES_ON_A_BOGUS_ARGUMENT,)
    + tuple(FEATURE_PLATFORM_OPERATIONS)
)


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def expectations() -> dict:
    return yaml.safe_load(_EXPECTATIONS.read_text())["operations"]


@pytest.fixture(scope="module")
def harness():
    """``scripts/test_api_rbac.py``, for its real ``classify`` and ``ROLES``.

    Loaded by path. The module inserts its own directory on ``sys.path`` for its
    siblings, so it imports the same way from any working directory.
    """
    return _load("_rbac_harness_for_probe_statuses", _HARNESS)


@pytest.fixture
def dispatcher(monkeypatch):
    """The real dispatcher, with its module-scope boto3 clients stubbed out."""
    monkeypatch.setattr(boto3, "client", lambda service, *a, **k: object())
    if str(_DISPATCHER_DIR) not in sys.path:
        sys.path.insert(0, str(_DISPATCHER_DIR))
    _load("authz", _DISPATCHER_DIR / "authz.py")
    _load("ddb_direct", _DISPATCHER_DIR / "ddb_direct.py")
    _load("validation", _DISPATCHER_DIR / "validation.py")
    return _load("index", _DISPATCHER_DIR / "index.py")


@pytest.fixture(scope="module")
def resolver():
    """The real test-set resolver, imported under its own module name.

    ``DynamoDBClient`` is patched for the import only — the module constructs one
    at module scope from ``TRACKING_TABLE`` — and each test replaces it with a
    real client pointed at a real (moto) table, so no double decides what the
    table contains.
    """
    with patch.dict(os.environ, _RESOLVER_ENV):
        with patch("idp_common.dynamodb.DynamoDBClient"):
            return _load("_test_set_resolver_for_probe_statuses", _RESOLVER)


@pytest.fixture
def empty_deployment(resolver):
    """An empty tracking table and the two buckets, wired into the resolver.

    Empty is the condition under test: every one of these operations names an
    object by id, and the matrix names one that does not exist.
    """
    from idp_common.dynamodb.client import DynamoDBClient

    with mock_aws(), patch.dict(os.environ, _RESOLVER_ENV):
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=_RESOLVER_ENV["TRACKING_TABLE"],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        for bucket in (_RESOLVER_ENV["INPUT_BUCKET"], _RESOLVER_ENV["TEST_SET_BUCKET"]):
            s3.create_bucket(Bucket=bucket)
        real_db = DynamoDBClient(
            table_name=_RESOLVER_ENV["TRACKING_TABLE"], region="us-east-1"
        )
        with (
            patch.object(resolver, "db_client", real_db),
            patch.object(resolver, "s3_client", s3),
        ):
            yield SimpleNamespace(resolver=resolver, s3=s3)


# ---------------------------------------------------------------------------
# the invoke boundary
# ---------------------------------------------------------------------------
class _FakeLambdaRaising:
    """The Lambda service, returning the response a HANDLED error produces.

    This is the boundary the fix turns on. A resolver exception does not reach the
    dispatcher as an object — Lambda serialises it to ``{"errorType": <class
    name>, "errorMessage": str(e)}`` and sets ``FunctionError`` — so the class
    *name* is the entire contract between the two halves, and reconstructing the
    payload here is what makes this an end-to-end measurement rather than two
    assertions that happen to agree.
    """

    def __init__(self, exc: BaseException):
        self.payload = {
            "errorType": type(exc).__name__,
            "errorMessage": str(exc),
            "stackTrace": [],
        }

    def invoke(self, **_kwargs):
        return {
            "FunctionError": "Handled",
            "Payload": io.BytesIO(json.dumps(self.payload).encode("utf-8")),
        }


class _FakeLambdaReturning:
    """The same boundary for an operation that succeeds."""

    def __init__(self, result):
        self.payload = result

    def invoke(self, **_kwargs):
        # `default=str` for the same reason the deployed path needs none: a
        # DynamoDB read hands back `Decimal`, which Lambda's own serializer
        # handles and `json.dumps` does not.
        return {
            "Payload": io.BytesIO(json.dumps(self.payload, default=str).encode("utf-8"))
        }


def _http_event(field: str, arguments: dict, groups: list[str]) -> dict:
    """An API Gateway event in the shape the live API delivers.

    The API is an ``AWS::ApiGateway::RestApi``, so a Lambda proxy integration
    sends payload format 1.0 and a ``COGNITO_USER_POOLS`` authorizer's claims
    arrive at ``requestContext.authorizer.claims`` — not the 2.0
    ``...authorizer.jwt.claims``. Groups go only where the authorizer puts them,
    never in the body, which the caller controls.
    """
    return {
        "resource": "/op/{field}",
        "path": f"/op/{field}",
        "httpMethod": "POST",
        "requestContext": {
            "resourcePath": "/op/{field}",
            "httpMethod": "POST",
            "path": f"/prod/op/{field}",
            "authorizer": {
                "claims": {
                    "sub": "11111111-2222-3333-4444-555555555555",
                    "email": "admin@example.com",
                    "cognito:groups": groups,
                }
            },
        },
        "pathParameters": {"field": field},
        "body": json.dumps({"arguments": arguments}),
        "headers": {},
        "isBase64Encoded": False,
    }


def _run_resolver(deployment, field: str, arguments: dict):
    """Drive the resolver and hand back whatever it did: ``(result, exception)``."""
    event = {
        "info": {"fieldName": field},
        "arguments": arguments,
        "identity": _ADMIN_IDENTITY,
    }
    try:
        return deployment.resolver.handler(event, None), None
    except Exception as exc:  # noqa: BLE001 — the exception IS the measurement
        return None, exc


def _through_the_dispatcher(idx, monkeypatch, field, arguments, exc, result):
    """The status an Admin caller receives, end to end."""
    fake = _FakeLambdaRaising(exc) if exc is not None else _FakeLambdaReturning(result)
    monkeypatch.setattr(idx, "_lambda", fake)
    # Routed under the ALIAS TARGET, not under the field's own name: every
    # test-set operation but one is an alias of `addDocumentsToTestSet` in
    # `FIELD_ALIASES`, because the SSM parameter carrying the real map is at its
    # 8 KB ceiling and cannot hold a key per field. Installing the ARN under the
    # field's own name instead leaves `mapped_arn` empty, and the dispatcher then
    # answers the *unroutable-operation* 404 — status 404, errorType "NotFound",
    # which the harness's conditional-feature probe reads as "this deployment
    # lacks the feature" and would make these cases pass for the wrong reason.
    monkeypatch.setitem(
        idx.FIELD_FUNCTION_MAP, idx.FIELD_ALIASES.get(field, field), _RESOLVER_ARN
    )
    resp = idx.handler(_http_event(field, arguments, ["Admin"]))
    body = json.loads(resp["body"])
    errors = body.get("errors") or []
    return resp["statusCode"], (errors[0].get("errorType") if errors else None), body


def _probe_arguments(expectations, field):
    args = expectations[field].get("args")
    assert args is not None, (
        f"{field} declares no `args` in scripts/api_rbac_expectations.yaml, so the "
        "live matrix is no longer sending the request this measures"
    )
    return args


# ===========================================================================
# the derivation: where 39 comes from
# ===========================================================================
def test_the_fifteen_operations_are_still_declared_operations(expectations):
    """Guards the guard. A renamed operation would leave every parametrised case
    below silently measuring nothing, because there would be no probe argument to
    read and no cell in the live matrix to speak for."""
    missing = [op for op in ISSUE_OPERATIONS if op not in expectations]
    assert not missing, (
        "operations #1304 named that are no longer declared in "
        f"scripts/api_rbac_expectations.yaml: {missing}"
    )
    assert len(set(ISSUE_OPERATIONS)) == 15, (
        f"#1304 named fifteen operations; this module lists {len(set(ISSUE_OPERATIONS))}"
    )


def test_the_unproven_cell_count_is_derived_and_is_thirty_nine(expectations, harness):
    """The 39 is a consequence of two files, so compute it from them.

    A *positive* authorization assertion is one cell of the group matrix where the
    role is admitted: the arm that can only ask "was this caller NOT denied?" and
    that a 5xx therefore satisfied without observing anything. The population is
    the roles the live matrix actually drives, and that population changed: issue
    #1065 added ``Annotator`` to ``ROLES``, so the seven test-set operations that
    admit it contribute three cells each where they contributed two, and the total
    moved from **25 to 32**; Reviewers then gained those seven operations (they work
    the test sets their profile scope admits), each a fourth cell, so **32 to 39**. The derivation is what reported the move; nothing here
    was retuned to accommodate it.

    Quoting the number instead of deriving it is how a count goes stale while
    reading correct: add a role to ``ROLES``, or an operation to the list above,
    and it moves. This fails rather than drifting.
    """
    per_operation = {
        op: [
            role
            for role in harness.ROLES
            if role in (expectations[op].get("groups") or [])
        ]
        for op in ISSUE_OPERATIONS
    }
    assert all(per_operation.values()), (
        "an operation #1304 named now admits none of the roles the live matrix "
        f"drives, so it contributes no positive cell: {per_operation}"
    )
    total = sum(len(roles) for roles in per_operation.values())
    assert total == 39, (
        "the positive cells over the fifteen operations #1304 named, for the roles "
        f"scripts/test_api_rbac.py drives, now number {total} rather than 39. If "
        "that is intended, say what the new number is and why; it is the count of "
        "assertions this module exists to keep conclusive.\n"
        + "\n".join(f"  {op}: {roles}" for op, roles in per_operation.items())
    )


@pytest.mark.parametrize("field", sorted(FEATURE_PLATFORM_OPERATIONS))
def test_the_three_feature_platform_operations_are_driven_in_their_own_suite(field):
    """The three operations this module does not drive are driven somewhere.

    They are counted in the 39 above, so a prose note saying "covered elsewhere"
    is the one thing that must not be left unchecked: delete those suites and the
    derivation would still report 39 while three of them were measured by nothing.
    Checked by AST rather than by substring — a mention of ``ResourceNotFound`` in
    a comment or a docstring would satisfy a textual search, and in those files
    most mentions are prose.
    """
    path = _FEATURE_PLATFORM_TESTS / FEATURE_PLATFORM_OPERATIONS[field]
    assert path.is_file(), (
        f"{path.relative_to(_REPO)} is gone, so {field}'s not-found answer — one "
        "of the 39 cells derived above — is measured nowhere"
    )
    tree = ast.parse(path.read_text())
    raises_not_found = False
    drives_the_handler = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "raises" and node.args:
            first = node.args[0]
            arg_name = (
                first.attr
                if isinstance(first, ast.Attribute)
                else getattr(first, "id", "")
            )
            if arg_name == "ResourceNotFound":
                raises_not_found = True
        if name == "handler":
            drives_the_handler = True
    assert raises_not_found, (
        f"{path.name} no longer asserts ResourceNotFound anywhere, so {field} has "
        "lost the 404 assertion this module is relying on it for"
    )
    assert drives_the_handler, (
        f"{path.name} no longer calls a handler, so whatever it asserts it is not "
        f"driving {field}"
    )


# ===========================================================================
# layer 1 — the resolver, driven with the matrix's own arguments
# ===========================================================================
@pytest.mark.parametrize("field", sorted(PROBED_OPERATIONS))
def test_the_matrix_argument_produces_a_refusal_the_dispatcher_can_map(
    field, expectations, empty_deployment
):
    """The resolver's own answer, measured rather than read.

    What this adds over the AST gate is the coupling: the argument comes from the
    expectations file the live harness reads, so the two cannot drift apart, and
    the exception is whatever the code path actually reaches rather than whatever
    the nearest textual match says.
    """
    expected_type, _status, fragment = PROBED_OPERATIONS[field]
    _result, exc = _run_resolver(
        empty_deployment, field, _probe_arguments(expectations, field)
    )

    assert exc is not None, (
        f"{field} returned instead of refusing the object the matrix names, so "
        "this case no longer measures a refusal at all"
    )
    assert type(exc).__name__ == expected_type, (
        f"{field} refused with {type(exc).__name__}({str(exc)[:120]!r}). The "
        f"dispatcher maps by class NAME across the invoke boundary and {expected_type} "
        "is what it maps to the status this operation must answer; anything "
        "outside that map reaches the caller as 500 InternalError and puts this "
        "operation's cells back to INCONCLUSIVE. See issue #1304."
    )
    assert fragment.lower() in str(exc).lower(), (
        f"{field} raised {expected_type} for a different reason than the one being "
        f"measured: {str(exc)[:160]!r} does not mention {fragment!r}. The status "
        "alone cannot tell these apart — a KeyError on an environment variable "
        "maps to the same 400 a missing argument does."
    )


# ===========================================================================
# layer 2 — the status the caller receives, through the real dispatcher
# ===========================================================================
@pytest.mark.parametrize("field", sorted(PROBED_OPERATIONS))
def test_the_caller_receives_the_four_hundred_class_status(
    field, expectations, empty_deployment, dispatcher, monkeypatch
):
    expected_type, expected_status, _fragment = PROBED_OPERATIONS[field]
    arguments = _probe_arguments(expectations, field)
    result, exc = _run_resolver(empty_deployment, field, arguments)
    status, error_type, _body = _through_the_dispatcher(
        dispatcher, monkeypatch, field, arguments, exc, result
    )

    assert (status, error_type) == (
        expected_status,
        "ResourceNotFound" if expected_type == "ResourceNotFound" else "BadRequest",
    ), (
        f"{field} answers {status} {error_type!r} for the argument the live matrix "
        f"sends; {expected_status} is what the cell needs to be conclusive"
    )
    assert status < 500, "a 5xx is what #1304 was"


def _with_a_blank_id(arguments: dict) -> dict:
    """The matrix's arguments with ``testSetId`` emptied, wherever it sits.

    Two argument shapes are in use — a flat ``{testSetId: ...}`` and a nested
    ``{input: {testSetId: ...}}`` — so this handles both and asserts it changed
    something. An operation whose argument shape moved would otherwise be driven
    with the unmodified arguments and measure the not-found case twice.
    """
    blanked = json.loads(json.dumps(arguments))
    touched = False
    if isinstance(blanked.get("input"), dict) and "testSetId" in blanked["input"]:
        blanked["input"]["testSetId"] = ""
        touched = True
    if "testSetId" in blanked:
        blanked["testSetId"] = ""
        touched = True
    assert touched, (
        f"no testSetId to blank in {arguments!r}; this operation's argument shape "
        "changed and this case is no longer measuring its id validation"
    )
    return blanked


@pytest.mark.parametrize("field", sorted(BLANK_ID_ANSWERS))
def test_a_malformed_identifier_is_a_client_error_not_a_server_fault(
    field, expectations, empty_deployment, dispatcher, monkeypatch
):
    """The 400 half of #1304, measured on the operations that own the rule.

    ``raise Exception("Invalid test set id")`` and ``raise ValueError("Invalid test
    set id")`` are the same text one word apart and the caller sees 500 or 400
    accordingly. Nothing that reads source could tell the difference matters, and
    the live matrix does not reach these guards at all — it sends a well-formed id
    — so this is the only place the distinction is observed.
    """
    expected_type, expected_status, fragment = BLANK_ID_ANSWERS[field]
    arguments = _with_a_blank_id(_probe_arguments(expectations, field))
    result, exc = _run_resolver(empty_deployment, field, arguments)

    assert exc is not None, f"{field} accepted a blank identifier"
    assert type(exc).__name__ == expected_type, (
        f"{field} refused a blank identifier with {type(exc).__name__}"
        f"({str(exc)[:120]!r}); {expected_type} is what the dispatcher maps to "
        f"{expected_status}. A class outside that map reaches the caller as 500 "
        "InternalError and is counted in the API's 5xx rate."
    )
    assert fragment.lower() in str(exc).lower(), (
        f"{field} refused for a different reason than the one measured: "
        f"{str(exc)[:160]!r} does not mention {fragment!r}"
    )

    status, error_type, _body = _through_the_dispatcher(
        dispatcher, monkeypatch, field, arguments, exc, result
    )
    assert (status, error_type) == (
        expected_status,
        "ResourceNotFound" if expected_type == "ResourceNotFound" else "BadRequest",
    )


def test_the_blank_identifier_table_covers_every_driven_operation():
    """Guards the guard. The two tables must name the same operations, or an
    operation gains a not-found case and quietly keeps none for its argument
    validation."""
    assert set(BLANK_ID_ANSWERS) == set(PROBED_OPERATIONS), {
        "only in the blank-id table": sorted(
            set(BLANK_ID_ANSWERS) - set(PROBED_OPERATIONS)
        ),
        "only in the not-found table": sorted(
            set(PROBED_OPERATIONS) - set(BLANK_ID_ANSWERS)
        ),
    }
    # And the two tables must not have collapsed into the same expectation for
    # every operation, which would mean one of them stopped measuring anything of
    # its own.
    assert BLANK_ID_ANSWERS != PROBED_OPERATIONS


def test_the_operation_that_creates_is_conclusive_without_a_refusal(
    expectations, empty_deployment, dispatcher, monkeypatch
):
    """``createEmptyTestSet`` is the one of the fifteen that succeeds.

    Its probe argument is a well-formed creation request, so the resolver creates
    the set and the caller gets a 2xx. That is conclusive for the positive arm —
    "not denied" is observed — and the point of asserting it here is that the cell
    is conclusive for a *different* reason from the other eleven, so a future
    change that turned this into a 500 would otherwise go unnoticed by both this
    module and the AST gate (which only reads not-found wording).
    """
    field = CREATES_ON_A_BOGUS_ARGUMENT
    arguments = _probe_arguments(expectations, field)
    result, exc = _run_resolver(empty_deployment, field, arguments)
    assert exc is None, f"{field} refused the matrix's argument: {exc!r}"
    status, error_type, _body = _through_the_dispatcher(
        dispatcher, monkeypatch, field, arguments, exc, result
    )
    assert (status, error_type) == (200, None)


# ===========================================================================
# layer 3 — the verdict the live matrix would record, and the discrimination
# ===========================================================================
def _measured_statuses(expectations, empty_deployment, dispatcher, monkeypatch):
    """``{operation: status}`` for every operation this module drives."""
    measured = {}
    for field in list(PROBED_OPERATIONS) + [CREATES_ON_A_BOGUS_ARGUMENT]:
        arguments = _probe_arguments(expectations, field)
        result, exc = _run_resolver(empty_deployment, field, arguments)
        status, error_type, _body = _through_the_dispatcher(
            dispatcher, monkeypatch, field, arguments, exc, result
        )
        measured[field] = (status, error_type)
    return measured


def test_every_positive_cell_over_these_operations_is_now_conclusive(
    expectations, empty_deployment, dispatcher, monkeypatch, harness
):
    """The deliverable: each admitted role's cell records PASS, not ERROR.

    ``classify`` is the harness's own function, consulted with the status this
    module measured. Before the fix the same call returned
    ``ERROR``/``GAP-SEC-INCONCLUSIVE-5XX`` for every one of these cells, which the
    report prints under "Checks that could not be run" — visible, counted
    separately, and never a pass. What each cell proves now is that the operation
    admitted the role it declares: an answer about the object, reached after the
    group floor let the caller through.
    """
    measured = _measured_statuses(
        expectations, empty_deployment, dispatcher, monkeypatch
    )
    unproven = []
    for field, (status, error_type) in measured.items():
        for role in harness.ROLES:
            if role not in (expectations[field].get("groups") or []):
                continue
            verdict = harness.classify(
                role, set(expectations[field]["groups"]), status, error_type, None
            )
            if verdict.outcome != "PASS":
                unproven.append(f"{field}[{role}]: {verdict.outcome} {verdict.detail}")
    assert not unproven, (
        "cells over the operations #1304 named that the live matrix still could "
        "not draw a conclusion from:\n  " + "\n  ".join(unproven)
    )
    # Non-vacuity: the loop must have judged cells. An empty `measured`, or a
    # `groups` list that stopped naming any driven role, would satisfy the
    # assertion above by iterating zero times.
    judged = sum(
        1
        for field in measured
        for role in harness.ROLES
        if role in (expectations[field].get("groups") or [])
    )
    assert judged == 36, (
        f"this module drives twelve of the fifteen operations and judged {judged} "
        "of the 39 positive cells; the other three are the feature-platform "
        "resolvers named in FEATURE_PLATFORM_OPERATIONS and are driven in their "
        "own suite"
    )


@pytest.mark.parametrize("crashed_status", [500, 502, 503])
def test_the_same_cells_would_be_inconclusive_again_at_five_hundred(
    expectations, harness, crashed_status
):
    """The discrimination, which is what makes the test above worth anything.

    "The verdict is PASS" is a weak claim if the harness would also report PASS on
    a 500 — and that is precisely the defect #1304 is about, so asserting the
    positive direction alone would reproduce it in the test. Every cell judged
    above must come back as ERROR against the status it used to receive, carrying
    the gap id that puts it in the report's "could not be run" section.
    """
    driven = list(PROBED_OPERATIONS) + [CREATES_ON_A_BOGUS_ARGUMENT]
    passing = []
    for field in driven:
        groups = expectations[field].get("groups") or []
        for role in harness.ROLES:
            if role not in groups:
                continue
            verdict = harness.classify(
                role, set(groups), crashed_status, "InternalError", None
            )
            if verdict.outcome != "ERROR":
                passing.append(f"{field}[{role}]: {verdict.outcome} {verdict.detail}")
            elif verdict.gap != harness.GAP_INCONCLUSIVE_5XX:
                passing.append(f"{field}[{role}]: ERROR with gap {verdict.gap!r}")
    assert not passing, (
        f"a {crashed_status} no longer makes these cells inconclusive, so a "
        "resolver that went back to answering 5xx would be reported as having "
        "passed its authorization checks:\n  " + "\n  ".join(passing)
    )


def test_a_four_oh_four_is_not_read_as_a_refusal(harness):
    """The other direction of the same discrimination.

    A harness that treated any non-2xx as a denial would turn all 39 of these
    cells into failures the moment the fix landed — the mirror-image defect, and a
    tempting one, since "not 200" is the easy reading. ``_denied`` must answer on
    401/403 and an ``Unauthorized`` errorType and on nothing else here.
    """
    assert not harness._denied(404, "ResourceNotFound")
    assert not harness._denied(400, "BadRequest")
    assert not harness._denied(200, None)
    # And it still answers for the shapes a refusal really takes, or the positive
    # claim above would be satisfied by a function that always says "not denied".
    assert harness._denied(403, "Unauthorized")
    assert harness._denied(401, None)
    assert harness._denied(200, None, in_band="Unauthorized")


def test_a_resolver_sourced_404_is_not_read_as_an_absent_feature(
    expectations, empty_deployment, dispatcher, monkeypatch, harness
):
    """The 404 that would be worse than the 500 it replaced.

    The dispatcher answers 404 for two unrelated conditions, and the harness's
    conditional-feature probe reads one of them as "this deployment lacks the
    feature, skip the whole row". If a resolver-sourced not-found were read that
    way, these operations' cells would not be inconclusive — they would be absent,
    with no finding and nothing printed. The two are told apart by ``errorType``,
    and this drives a real one through the real probe rather than comparing the
    strings.
    """
    field = "getTestSetDocuments"
    arguments = _probe_arguments(expectations, field)
    result, exc = _run_resolver(empty_deployment, field, arguments)
    status, error_type, body = _through_the_dispatcher(
        dispatcher, monkeypatch, field, arguments, exc, result
    )
    assert status == 404
    assert body.get("errors"), "the 404 carried no error body to read"
    assert not harness._is_feature_absent_404(status, error_type), (
        "a resolver-sourced not-found is being read as a missing feature; the "
        "operation's entire row would be skipped and no authorization assertion "
        "would run"
    )
