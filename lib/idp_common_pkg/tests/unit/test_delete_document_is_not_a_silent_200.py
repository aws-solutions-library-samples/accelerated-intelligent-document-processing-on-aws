# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A ``deleteDocument`` that did not delete may not answer HTTP 200.

This is the half of #1366 that the permission and the expression do not cover, and the
half that decides whether the next fault of this kind is noticed. Both of the reported
faults were caught per document, appended to a list that only reached the log, and
rolled up into ``failed_count == 0`` — a value, returned from a *successful*
invocation. Nothing downstream looked at it: the REST dispatcher answers 200 for any
value a resolver yields, and the UI logged the result at debug level and navigated
away. So a delete that removed a document's version rows, could not remove its list
row and kept its tracking record reported an untroubled success to the browser.

⚠️ **The two halves fail differently and a test of either alone is not enough.** A
test that asserts the permission is granted and the expression is valid passes forever
once both are fixed, and says nothing about the next operation on this path that
swallows its own error. So what is asserted here is the *transport* outcome, driven
through the real dispatcher with the real resolver behind it: the resolver's handler
is invoked by a double that reproduces what Lambda does with an uncaught exception —
``FunctionError`` plus ``errorType``/``errorMessage``, the only two things that survive
an invoke — and the status code is read off the dispatcher's response.

The two faults parametrised below are the ones observed on a live stack: an
``AccessDenied`` on ``ListObjectVersions`` and a ``ValidationException`` on the
list-entry query. They are injected as failures of ``delete_single_document`` rather
than reproduced from AWS, because what is under test here is what happens to a
failure, not which failures occur — those are covered in
``test_delete_documents_coverage.py`` and ``scripts/tests/test_delete_document_s3_grants.py``.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "nested" / "api-resolvers").is_dir():
            return parent
    raise RuntimeError("Could not locate repo root containing nested/api-resolvers")


_REPO = _find_repo_root()
_LAMBDA_DIR = _REPO / "nested" / "api-resolvers" / "src" / "lambda"
_DISPATCHER_DIR = _LAMBDA_DIR / "http_api_dispatcher"

ARN = "arn:aws:lambda:us-east-1:123456789012:function:delete-document"

#: The two faults #1366 reported, as the error strings they arrive with.
OBSERVED_FAULTS = {
    "list-object-versions-denied": (
        "Error deleting from output bucket: An error occurred (AccessDenied) when "
        "calling the ListObjectVersions operation"
    ),
    "list-entry-query-rejected": (
        "Error in list entry deletion: An error occurred (ValidationException) when "
        "calling the Query operation: Filter Expression can only contain non-primary "
        "key attributes: Primary key attribute: SK"
    ),
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def resolver(monkeypatch):
    """The real delete resolver, with its module-level AWS resources mocked out."""
    monkeypatch.setenv("TRACKING_TABLE_NAME", "tracking")
    monkeypatch.setenv("INPUT_BUCKET", "in")
    monkeypatch.setenv("OUTPUT_BUCKET", "out")
    with patch("boto3.resource"), patch("boto3.client"):
        return _load_module(
            "delete_document_resolver_e2e",
            _LAMBDA_DIR / "delete_document_resolver" / "index.py",
        )


@pytest.fixture
def dispatcher(monkeypatch):
    """The real dispatcher, with no AWS client constructed."""
    import boto3

    monkeypatch.setattr(boto3, "client", lambda service, *a, **k: object())
    for directory in (str(_DISPATCHER_DIR),):
        if directory not in sys.path:
            sys.path.insert(0, directory)
    monkeypatch.chdir(_DISPATCHER_DIR)
    _load_module("authz", _DISPATCHER_DIR / "authz.py")
    _load_module("ddb_direct", _DISPATCHER_DIR / "ddb_direct.py")
    _load_module("validation", _DISPATCHER_DIR / "validation.py")
    return _load_module("dispatcher_index_e2e", _DISPATCHER_DIR / "index.py")


class _LambdaRunningTheRealResolver:
    """An ``invoke`` that actually runs the resolver, the way Lambda reports it.

    An uncaught exception in a handler does not propagate to the caller of
    ``Invoke`` — the response carries ``FunctionError`` and a JSON payload holding the
    exception's class **name** and message, and nothing else. Reproducing that here is
    what makes this an end-to-end assertion rather than two separate ones: if the
    resolver swallowed the failure again, this double would faithfully report a
    successful invoke and the dispatcher would faithfully answer 200.
    """

    def __init__(self, handler):
        self._handler = handler

    def invoke(self, **kwargs):
        event = json.loads(kwargs["Payload"])
        try:
            result = self._handler(event, MagicMock())
        except BaseException as exc:  # noqa: BLE001 - mirroring the Lambda boundary
            payload = {
                "errorType": type(exc).__name__,
                "errorMessage": str(exc),
            }
            return {
                "FunctionError": "Unhandled",
                "Payload": io.BytesIO(json.dumps(payload).encode("utf-8")),
            }
        return {"Payload": io.BytesIO(json.dumps(result).encode("utf-8"))}


def _event(object_keys):
    return {
        "requestContext": {
            "http": {"method": "POST"},
            "authorizer": {"jwt": {"claims": {"cognito:groups": ["Admin"]}}},
        },
        "pathParameters": {"field": "deleteDocument"},
        "body": json.dumps({"arguments": {"objectKeys": object_keys}}),
        "headers": {},
    }


def _call(dispatcher, resolver, object_keys, monkeypatch):
    monkeypatch.setattr(
        dispatcher, "_lambda", _LambdaRunningTheRealResolver(resolver.handler)
    )
    dispatcher.FIELD_FUNCTION_MAP["deleteDocument"] = ARN
    response = dispatcher.handler(_event(object_keys))
    return response["statusCode"], json.loads(response["body"])


def _per_document(key, error=None):
    return {
        "success": error is None,
        "object_key": key,
        "deleted": {
            "input_file": True,
            "output_files": 0,
            "list_entries": False,
            "document_record": False,
            "run_records": 3,
        },
        "errors": [] if error is None else [error],
    }


def test_the_harness_reports_a_successful_delete_as_200(
    dispatcher, resolver, monkeypatch
):
    """The control. Without it every assertion below could be passing for the wrong
    reason — a harness that cannot produce a 200 at all proves nothing by not
    producing one."""
    with patch.object(
        resolver, "delete_single_document", return_value=_per_document("a.pdf")
    ):
        status, body = _call(dispatcher, resolver, ["a.pdf"], monkeypatch)

    assert status == 200, body
    assert body is True


@pytest.mark.parametrize("fault_id", sorted(OBSERVED_FAULTS))
def test_neither_observed_fault_can_answer_200(
    dispatcher, resolver, monkeypatch, fault_id
):
    """⚠️ The assertion #1366 is about: a logged error may not be a 200.

    This is what fails if the error-swallowing is restored in any form — the
    per-document ``errors`` list ceasing to reach the return value, the rollup going
    back to a boolean, or a new step on this path catching its own fault. The status is
    read from the transport, so it does not depend on which value the resolver chose.
    """
    error = OBSERVED_FAULTS[fault_id]
    with patch.object(
        resolver,
        "delete_single_document",
        return_value=_per_document("a.pdf", error=error),
    ):
        status, body = _call(dispatcher, resolver, ["a.pdf"], monkeypatch)

    assert status != 200, (
        f"a delete that failed with {fault_id} answered 200 with body {body!r}; "
        "this is the shape that reached a release"
    )
    assert status >= 500, f"a server-side fault should be a 5xx, got {status}: {body}"


@pytest.mark.parametrize("fault_id", sorted(OBSERVED_FAULTS))
def test_the_cause_reaches_the_response_body(
    dispatcher, resolver, monkeypatch, fault_id
):
    """A non-200 with nothing in it is only half an improvement.

    The recovery advice in the log — that the record is kept because a retry needs it
    to find the remaining list entry — could not be taken, since the retry re-ran the
    same rejected query. A caller who is told only "it failed" is in the same position.
    """
    error = OBSERVED_FAULTS[fault_id]
    with patch.object(
        resolver,
        "delete_single_document",
        return_value=_per_document("a.pdf", error=error),
    ):
        _status, body = _call(dispatcher, resolver, ["a.pdf"], monkeypatch)

    message = json.dumps(body)
    assert "a.pdf" in message, f"the failing document is not named: {message}"
    distinguishing = (
        "AccessDenied" if fault_id == "list-object-versions-denied" else "SK"
    )
    assert distinguishing in message, f"the cause is not reported: {message}"


def test_no_account_id_or_arn_reaches_the_response_body(
    dispatcher, resolver, monkeypatch
):
    """⚠️ Asserted end to end, because every layer passes the message through.

    The resolver redacts, but the dispatcher copies ``errorMessage`` into the 500 body
    verbatim and the UI interpolates whatever it is given — so the only place this
    property can be *established* is at the boundary the browser reads. A redactor that
    is correct and not wired in would pass the resolver's own unit tests.
    """
    denial = (
        "Error deleting from output bucket: An error occurred (AccessDenied) when "
        "calling the ListObjectVersions operation: User: "
        "arn:aws:sts::000000000000:assumed-role/Stack-DeleteDocumentRole-Ab/Stack-x "
        "is not authorized to perform: s3:ListBucketVersions on resource: "
        '"arn:aws:s3:::stack-output-bucket"'
    )
    with patch.object(
        resolver,
        "delete_single_document",
        return_value=_per_document("a.pdf", error=denial),
    ):
        status, body = _call(dispatcher, resolver, ["a.pdf"], monkeypatch)

    rendered = json.dumps(body)
    assert status != 200
    assert "000000000000" not in rendered, f"an account id reached the body: {rendered}"
    assert "arn:aws:sts::" not in rendered
    assert "arn:aws:s3:::" not in rendered
    # The actionable half still arrives.
    assert "AccessDenied" in rendered
    assert "s3:ListBucketVersions" in rendered


def test_a_partial_failure_in_a_multi_select_is_not_a_200(
    dispatcher, resolver, monkeypatch
):
    """The ordinary UI path: the Document List sends the whole selection in one call.

    Nine removed and one tracking row orphaned used to read exactly like ten removed.
    """
    outcomes = [
        _per_document("a.pdf"),
        _per_document("b.pdf"),
        _per_document("c.pdf", error=OBSERVED_FAULTS["list-entry-query-rejected"]),
    ]
    with patch.object(
        resolver, "delete_single_document", side_effect=outcomes
    ) as deleter:
        status, body = _call(
            dispatcher, resolver, ["a.pdf", "b.pdf", "c.pdf"], monkeypatch
        )

    assert deleter.call_count == 3, "a failure must not abandon the rest of the batch"
    assert status != 200, f"a partial delete answered 200 with body {body!r}"
    assert "c.pdf" in json.dumps(body)


def test_the_environment_this_runs_in_holds_no_aws_credentials():
    """This suite constructs no client and must not acquire one by accident.

    The whole chain here is doubles. Asserted rather than assumed, because a resolver
    module that started constructing a real client at import would make this suite
    reach for the ambient environment instead of failing.
    """
    for variable in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        value = os.environ.get(variable)
        assert value in (None, "", "testing", "test"), (
            f"{variable} is set to a real-looking value for this suite"
        )
