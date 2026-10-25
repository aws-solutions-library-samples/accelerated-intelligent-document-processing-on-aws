# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Aborting a test run stops its documents, and says so when it cannot (#1609).

The abort resolver (``abort_test_runs``) tells a run's documents to stop by
invoking the ``abortWorkflow`` resolver. That invoke carried no caller identity,
and ``abortWorkflow`` refuses any event without an Admin or Author identity, so
every run was marked ABORTED while its documents went on processing; the invoke
was asynchronous, so the refusal was never seen.

These tests drive the real handlers end to end, in process: the REST dispatcher,
the abort resolver and ``abortWorkflow``, joined by a real botocore Lambda client
whose ``Invoke`` is answered by calling the target handler (``_InProcessLambda``),
against moto's DynamoDB and Step Functions. What is asserted is the outcome in
those services -- the execution is ABORTED in Step Functions, the document is
ABORTED in the tracking table -- not the shape of a call.

The authorization half is asserted on the same path: a caller the REST route
refuses for ``abortWorkflow`` is still refused, the dispatcher can never hand
``abortWorkflow`` the identity-less event it now accepts from a direct invoke,
and the identity ``abortWorkflow`` evaluates for a test-run abort is the one the
dispatcher verified, not one a caller wrote into the request.
"""

from __future__ import annotations

import io
import json
import sys
import threading

import boto3
import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ReadTimeoutError
from botocore.response import StreamingBody
from botocore.validate import validate_parameters
from moto import mock_aws

from idp_common.models import Document, Status
from tests.unit.lambdas._lambda_loader import REPO_ROOT, load_lambda

pytestmark = pytest.mark.unit

_REGION = "us-east-1"
_TABLE = "abort-e2e-tracking"
_STATE_MACHINE = "abort-e2e-workflow"
_ABORT_TEST_RUNS_ARN = f"arn:aws:lambda:{_REGION}:123456789012:function:abort-test-runs"
_ABORT_WORKFLOW_ARN = f"arn:aws:lambda:{_REGION}:123456789012:function:abort-workflow"
_DISPATCHER_DIR = REPO_ROOT / "nested/api-resolvers/src/lambda/http_api_dispatcher"


class _InProcessLambda:
    """A real botocore Lambda client whose ``Invoke`` runs a handler in process.

    botocore serialises and validates the request against the Lambda service
    model as it would for a real call; the answer is then built the way the
    Lambda service builds one -- a handler that raises answers 200 with
    ``FunctionError: Unhandled`` and an ``errorType``/``errorMessage`` body, an
    ``Event`` invoke answers 202 with an empty body and its result is never seen
    -- and is checked against the operation's output shape before the client
    returns it. Nothing reaches the network: the answer is returned from
    ``before-call``, the hook botocore's own ``Stubber`` uses.
    """

    def __init__(self, handlers):
        self.handlers = handlers  # function name or ARN -> handler(event, context)
        self.calls = []  # (function name, invocation type, event)
        self.fail = {}  # function name -> exception raised instead of invoking
        self._lock = threading.Lock()

    def client(self):
        client = boto3.client(
            "lambda",
            region_name=_REGION,
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        client.meta.events.register(
            "before-parameter-build.lambda.Invoke", self._capture
        )
        client.meta.events.register_first("before-call.lambda.Invoke", self._answer)
        return client

    @staticmethod
    def _capture(params, context, **_kwargs):
        context["invoke_params"] = dict(params)

    def _answer(self, model, context, **_kwargs):
        params = context["invoke_params"]
        name = params["FunctionName"]
        invocation_type = params.get("InvocationType", "RequestResponse")
        payload = params.get("Payload") or b"{}"
        event = json.loads(payload)
        with self._lock:
            self.calls.append((name, invocation_type, event))
        if name in self.fail:
            raise self.fail[name]
        handler = self.handlers[name]
        function_error = None
        try:
            result = handler(event, None)
        except Exception as e:  # noqa: BLE001 - the Lambda runtime's own contract
            function_error = "Unhandled"
            result = {"errorType": type(e).__name__, "errorMessage": str(e)}
        if invocation_type == "Event":
            status, body = 202, b""
        else:
            status, body = 200, json.dumps(result).encode()
        response = {
            "StatusCode": status,
            "Payload": StreamingBody(io.BytesIO(body), len(body)),
        }
        if function_error and invocation_type != "Event":
            response["FunctionError"] = function_error
        validate_parameters(response, model.output_shape)
        return AWSResponse(None, status, {}, None), response


class _Clock:
    """Stands in for the abort resolver's time: sleeping advances it."""

    def __init__(self):
        self.now = 1_000.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        if seconds < 0:
            raise ValueError("sleep length must be non-negative")
        self.sleeps.append(seconds)
        self.now += seconds


def _load_dispatcher(monkeypatch):
    """The real REST dispatcher, routing the two abort operations to their ARNs."""
    import importlib.util

    monkeypatch.setenv(
        "FIELD_FUNCTION_MAP",
        json.dumps(
            {
                "abortTestRuns": _ABORT_TEST_RUNS_ARN,
                "abortWorkflow": _ABORT_WORKFLOW_ARN,
            }
        ),
    )
    monkeypatch.syspath_prepend(str(_DISPATCHER_DIR))
    real_client = boto3.client
    # Its module-scope clients are never used here: the Lambda client is
    # replaced below, and the field map comes from the environment.
    boto3.client = lambda *a, **k: object()
    try:
        for name in ("authz", "ddb_direct", "validation", "index"):
            spec = importlib.util.spec_from_file_location(
                name, _DISPATCHER_DIR / f"{name}.py"
            )
            module = importlib.util.module_from_spec(spec)
            monkeypatch.setitem(sys.modules, name, module)
            spec.loader.exec_module(module)
    finally:
        boto3.client = real_client
    return sys.modules["index"]


def _http_event(field, arguments, groups, extra_claims=None, body_extra=None):
    """A REST API proxy event (payload 1.0) with the Cognito authorizer's claims.

    body_extra adds top-level keys to the request body, beside arguments.
    """
    claims = {
        "sub": "11111111-2222-3333-4444-555555555555",
        "email": "author@example.com",
        "cognito:username": "author",
    }
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
        "body": json.dumps({"arguments": arguments, **(body_extra or {})}),
        "headers": {},
        "isBase64Encoded": False,
    }


@pytest.fixture
def stack(monkeypatch):
    """Moto services, one RUNNING test run of three documents, and the handlers.

    Each document has a RUNNING Step Functions execution and a tracking record
    pointing at it, written through the real document service.
    """
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setenv("TRACKING_TABLE", _TABLE)
    monkeypatch.setenv("TRACKING_TABLE_NAME", _TABLE)
    monkeypatch.setenv("ABORT_WORKFLOW_FUNCTION_NAME", _ABORT_WORKFLOW_ARN)
    monkeypatch.delenv("TEST_RESULT_CACHE_UPDATE_QUEUE_URL", raising=False)
    monkeypatch.delenv("HITL_TABLE_NAME", raising=False)

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=_REGION)
        table = ddb.create_table(
            TableName=_TABLE,
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
        sfn = boto3.client("stepfunctions", region_name=_REGION)
        machine = sfn.create_state_machine(
            name=_STATE_MACHINE,
            definition=json.dumps(
                {"StartAt": "Wait", "States": {"Wait": {"Type": "Pass", "End": True}}}
            ),
            roleArn="arn:aws:iam::123456789012:role/sfn",
        )["stateMachineArn"]

        workflow = load_lambda(
            "abort_e2e_abort_workflow_index",
            "nested/api-resolvers/src/lambda/abort_workflow_resolver",
        )
        monkeypatch.setattr(
            workflow,
            "get_setting",
            lambda key, default=None: {"StateMachineName": _STATE_MACHINE}.get(
                key, default
            ),
        )
        abort = load_lambda(
            "abort_e2e_abort_test_runs_index",
            "nested/api-resolvers/src/lambda/abort_test_runs",
        )

        run_id = "run-1609"
        files = ["a.pdf", "b.pdf", "c.pdf"]
        table.put_item(
            Item={
                "PK": f"testrun#{run_id}",
                "SK": "metadata",
                "Status": "RUNNING",
                "Files": files,
            }
        )
        executions = {}
        for name in files:
            key = f"{run_id}/{name}"
            arn = sfn.start_execution(stateMachineArn=machine, name=name)[
                "executionArn"
            ]
            executions[key] = arn
            document = Document(
                id=key,
                input_key=key,
                status=Status.QUEUED,
                queued_time="2026-10-25T03:17:30+00:00",
            )
            workflow.document_service.create_document(document)
            document.status = Status.EXTRACTING
            document.workflow_execution_arn = arn
            workflow.document_service.update_document(document)

        lam = _InProcessLambda(
            {
                _ABORT_WORKFLOW_ARN: workflow.handler,
                _ABORT_TEST_RUNS_ARN: abort.lambda_handler,
            }
        )
        monkeypatch.setattr(abort, "lambda_client", lam.client())
        clock = _Clock()
        monkeypatch.setattr(abort, "time", clock)
        # The abort resolver's clock is replaced, so the settle wait over
        # documents that were not stopped costs no wall clock. Where they were
        # stopped it returns on its first poll and never sleeps.
        yield {
            "abort": abort,
            "workflow": workflow,
            "lambda": lam,
            "sfn": sfn,
            "table": table,
            "run_id": run_id,
            "executions": executions,
            "clock": clock,
        }


def _execution_statuses(stack):
    return {
        key: stack["sfn"].describe_execution(executionArn=arn)["status"]
        for key, arn in stack["executions"].items()
    }


def _document_statuses(stack):
    return {
        key: stack["table"].get_item(Key={"PK": f"doc#{key}", "SK": "none"})["Item"][
            "ObjectStatus"
        ]
        for key in stack["executions"]
    }


def _run_status(stack):
    item = stack["table"].get_item(
        Key={"PK": f"testrun#{stack['run_id']}", "SK": "metadata"}
    )["Item"]
    return item["Status"]


def _workflow_events(stack):
    return [
        (kind, event)
        for name, kind, event in stack["lambda"].calls
        if name == _ABORT_WORKFLOW_ARN
    ]


def test_the_fixture_documents_start_in_flight(stack):
    """The premise every test below rests on: three running executions."""
    assert set(_execution_statuses(stack).values()) == {"RUNNING"}
    assert set(_document_statuses(stack).values()) == {"EXTRACTING"}


def test_an_abort_through_the_rest_route_stops_every_document(stack, monkeypatch):
    """REST dispatcher -> abortTestRuns -> abortWorkflow, all of them real."""
    dispatcher = _load_dispatcher(monkeypatch)
    monkeypatch.setattr(dispatcher, "_lambda", stack["lambda"].client())

    response = dispatcher.handler(
        _http_event("abortTestRuns", {"testRunIds": [stack["run_id"]]}, ["Author"])
    )

    assert response["statusCode"] == 200, response
    result = json.loads(response["body"])
    assert result["success"] is True, result
    assert result["abortedCount"] == 1, result
    assert result["errors"] is None, result
    assert set(_execution_statuses(stack).values()) == {"ABORTED"}
    assert set(_document_statuses(stack).values()) == {"ABORTED"}
    assert _run_status(stack) == "ABORTED"


_FORGED = {
    "claims": {"cognito:groups": ["Admin"], "sub": "someone-else"},
    "username": "someone-else@example.com",
    "sub": "someone-else",
}


def test_the_identity_abort_workflow_checks_is_the_one_the_dispatcher_verified(
    stack, monkeypatch
):
    """Forwarded from the event's verified identity, never from the request body.

    The request body carries its own identity claiming Admin, beside
    arguments, where a REST caller can write it. The identity abortWorkflow
    receives must be the dispatcher's, built from the authorizer's claims.
    """
    dispatcher = _load_dispatcher(monkeypatch)
    monkeypatch.setattr(dispatcher, "_lambda", stack["lambda"].client())

    response = dispatcher.handler(
        _http_event(
            "abortTestRuns",
            {"testRunIds": [stack["run_id"]]},
            ["Author"],
            body_extra={"identity": _FORGED},
        )
    )

    assert response["statusCode"] == 200, response
    events = _workflow_events(stack)
    assert events, "abortWorkflow was never invoked"
    for kind, event in events:
        assert kind == "RequestResponse"
        identity = event["identity"]
        assert identity["claims"]["cognito:groups"] == ["Author"], identity
        assert identity["sub"] == "11111111-2222-3333-4444-555555555555"
        assert identity["username"] == "author@example.com"


def test_an_identity_written_into_the_arguments_is_refused_before_any_abort(
    stack, monkeypatch
):
    """The other place a REST caller can write: refused, and nothing is stopped."""
    dispatcher = _load_dispatcher(monkeypatch)
    monkeypatch.setattr(dispatcher, "_lambda", stack["lambda"].client())

    response = dispatcher.handler(
        _http_event(
            "abortTestRuns",
            {"testRunIds": [stack["run_id"]], "identity": _FORGED},
            ["Viewer"],
        )
    )

    assert response["statusCode"] == 403, response
    assert not stack["lambda"].calls
    assert set(_execution_statuses(stack).values()) == {"RUNNING"}


def test_an_abort_invoked_directly_stops_every_document(stack):
    """The SDK and CLI path: abortTestRuns invoked with no Cognito identity."""
    result = stack["abort"].lambda_handler(
        {
            "info": {"fieldName": "abortTestRuns"},
            "arguments": {"testRunIds": [stack["run_id"]]},
        },
        None,
    )

    assert result["abortedCount"] == 1, result
    assert result["errors"] is None, result
    assert set(_execution_statuses(stack).values()) == {"ABORTED"}
    assert set(_document_statuses(stack).values()) == {"ABORTED"}
    assert [event["identity"] for _, event in _workflow_events(stack)] == [None]


@pytest.mark.parametrize(
    "groups",
    [
        pytest.param(["Viewer"], id="viewer"),
        pytest.param(["Reviewer"], id="reviewer"),
        pytest.param([], id="no-group"),
        pytest.param(None, id="no-groups-claim"),
    ],
)
def test_abort_workflow_through_the_rest_route_still_refuses_other_callers(
    stack, monkeypatch, groups
):
    """The REST route's refusal is unchanged, and nothing is stopped."""
    dispatcher = _load_dispatcher(monkeypatch)
    monkeypatch.setattr(dispatcher, "_lambda", stack["lambda"].client())
    key = next(iter(stack["executions"]))

    response = dispatcher.handler(
        _http_event("abortWorkflow", {"objectKeys": [key]}, groups)
    )

    assert response["statusCode"] == 403, response
    assert set(_execution_statuses(stack).values()) == {"RUNNING"}


@pytest.mark.parametrize(
    "groups",
    [
        pytest.param(["Viewer"], id="viewer"),
        pytest.param([], id="no-group"),
        pytest.param(None, id="no-groups-claim"),
    ],
)
def test_abort_workflow_itself_refuses_any_event_the_dispatcher_builds_for_others(
    stack, groups
):
    """abortWorkflow's own check, past the dispatcher's floor.

    The event is the one the dispatcher would forward, built by the real
    ``normalize_event`` from a REST proxy event. It always carries an identity --
    even with no groups claim at all -- so it can never take the branch
    abortWorkflow keeps for an identity-less direct invoke.
    """
    from idp_common.api_adapter import normalize_event

    key = next(iter(stack["executions"]))
    event = normalize_event(_http_event("abortWorkflow", {"objectKeys": [key]}, groups))
    assert event["identity"] is not None

    with pytest.raises(PermissionError, match="Admin or Author"):
        stack["workflow"].handler(event, None)
    assert set(_execution_statuses(stack).values()) == {"RUNNING"}


def test_the_dispatcher_refuses_an_identity_less_abort_workflow_event(
    stack, monkeypatch
):
    """The only identity-less shape the dispatcher passes on is refused for this op.

    An IAM principal invoking the dispatcher directly can send the resolver-shaped
    event with a null identity, which ``normalize_event`` passes through unchanged.
    For abortWorkflow -- a group-scoped operation -- the dispatcher's group floor
    refuses it, and abortWorkflow is never invoked.
    """
    dispatcher = _load_dispatcher(monkeypatch)
    monkeypatch.setattr(dispatcher, "_lambda", stack["lambda"].client())
    key = next(iter(stack["executions"]))

    response = dispatcher.handler(
        {
            "arguments": {"objectKeys": [key]},
            "identity": None,
            "info": {"fieldName": "abortWorkflow"},
        }
    )

    assert response["statusCode"] == 403, response
    assert not _workflow_events(stack)
    assert set(_execution_statuses(stack).values()) == {"RUNNING"}


def test_a_refused_abort_is_reported_not_hidden(stack, monkeypatch):
    """When abortWorkflow refuses, the abort response names the documents.

    abortWorkflow is made to refuse the way it refused every test-run abort
    before this fix -- as if it did not accept the identity-less direct invoke --
    and the response must say the run's documents were not confirmed stopped,
    with the refusal, instead of reporting a clean abort.
    """
    monkeypatch.setattr(stack["workflow"], "is_direct_invoke", lambda event: False)

    result = stack["abort"].lambda_handler(
        {"arguments": {"testRunIds": [stack["run_id"]]}}, None
    )

    assert result["abortedCount"] == 1, result
    # Counted once: the three refused documents are also still EXTRACTING, and
    # are not reported a second time as unsettled.
    assert result["message"].endswith("; 3 document(s) not confirmed stopped"), result
    (error,) = result["errors"]
    assert error.startswith(f"{stack['run_id']}: 3 of 3 document(s)"), error
    assert "PermissionError" in error and "Admin or Author" in error, error
    assert "terminal state" not in error, error
    assert set(_execution_statuses(stack).values()) == {"RUNNING"}


def test_a_document_reported_stopped_that_goes_on_running_is_reported(stack):
    """The settle wait's last reading decides, not the abort workflow's answer.

    abortWorkflow can answer that it stopped a QUEUED document that the queue
    processor then started anyway (#1612). Here it answers that it stopped all
    three while one stays in flight; the response must say so rather than
    report a clean abort.
    """
    workflow = stack["workflow"]
    still_running = sorted(stack["executions"])[0]

    def handler(event, context):
        keys = event["arguments"]["objectKeys"]
        for key in keys:
            if key != still_running:
                document = workflow.document_service.get_document(key)
                document.status = Status.ABORTED
                workflow.document_service.update_document(document)
        return {
            "success": True,
            "message": f"Aborted {len(keys)} document(s)",
            "abortedCount": len(keys),
            "failedCount": 0,
            "errors": None,
        }

    stack["lambda"].handlers[_ABORT_WORKFLOW_ARN] = handler

    result = stack["abort"].lambda_handler(
        {"arguments": {"testRunIds": [stack["run_id"]]}}, None
    )

    assert result["abortedCount"] == 1, result
    assert result["message"].endswith("; 1 document(s) not seen to finish"), result
    assert result["errors"] == [
        f"{stack['run_id']}: 1 of 3 document(s) had not been seen to reach a terminal state "
        "when the abort returned and may still be processing"
    ], result
    # The wait ran to its deadline rather than giving up early.
    assert sum(stack["clock"].sleeps) >= 8, stack["clock"].sleeps


def test_unconfirmed_and_unsettled_documents_are_each_counted_once(stack):
    """One document refused by name, another reported stopped but still running.

    The refused one is still in flight too; it is reported once, as not
    confirmed stopped, and the other is reported as one more still processing.
    """
    workflow = stack["workflow"]
    refused, still_running, stopped = sorted(stack["executions"])

    def handler(event, context):
        document = workflow.document_service.get_document(stopped)
        document.status = Status.ABORTED
        workflow.document_service.update_document(document)
        return {
            "success": True,
            "message": "Aborted 2 document(s), 1 failed",
            "abortedCount": 2,
            "failedCount": 1,
            "errors": [f"{refused}: Failed to update status: boom"],
        }

    stack["lambda"].handlers[_ABORT_WORKFLOW_ARN] = handler

    result = stack["abort"].lambda_handler(
        {"arguments": {"testRunIds": [stack["run_id"]]}}, None
    )

    assert result["message"].endswith(
        "; 1 document(s) not confirmed stopped; 1 more document(s) not seen to finish"
    ), result
    (error,) = result["errors"]
    assert error.startswith(f"{stack['run_id']}: 1 of 3 document(s) were not"), error
    assert error.endswith(
        "; 1 other of 3 document(s) had not been seen to reach a terminal state when the "
        "abort returned and may still be processing"
    ), error


def test_an_invoke_with_no_answer_in_time_is_reported(stack):
    """A read timeout is unconfirmed: the batch may well be running."""
    stack["lambda"].fail[_ABORT_WORKFLOW_ARN] = ReadTimeoutError(
        endpoint_url="https://lambda"
    )

    result = stack["abort"].lambda_handler(
        {"arguments": {"testRunIds": [stack["run_id"]]}}, None
    )

    (error,) = result["errors"]
    assert "3 of 3 document(s) were not confirmed stopped" in error, error
    assert "Read timeout" in error, error


def test_documents_already_finished_are_not_reported_as_unstopped(stack):
    """A document abortWorkflow declines because it had finished is not a failure.

    The decline is abortWorkflow's own, produced by its handler here rather than
    restated, so the wording the abort resolver recognises cannot drift from it.
    """
    finished = sorted(stack["executions"])[0]
    document = stack["workflow"].document_service.get_document(finished)
    document.status = Status.COMPLETED
    # Finished includes its evaluation, which is what the settle wait reads.
    document.evaluation_status = "COMPLETED"
    stack["workflow"].document_service.update_document(document)

    result = stack["abort"].lambda_handler(
        {"arguments": {"testRunIds": [stack["run_id"]]}}, None
    )

    assert result["errors"] is None, result
    ((_, event),) = _workflow_events(stack)
    assert finished in event["arguments"]["objectKeys"]


def test_a_document_abort_workflow_cannot_find_is_reported(stack):
    """A document with no tracking record yet has not been stopped."""
    stack["table"].update_item(
        Key={"PK": f"testrun#{stack['run_id']}", "SK": "metadata"},
        UpdateExpression="SET Files = list_append(Files, :late)",
        ExpressionAttributeValues={":late": ["not-yet-recorded.pdf"]},
    )

    result = stack["abort"].lambda_handler(
        {"arguments": {"testRunIds": [stack["run_id"]]}}, None
    )

    (error,) = result["errors"]
    assert error.startswith(f"{stack['run_id']}: 1 of 4 document(s)"), error
    assert "Document not found" in error, error
    assert set(_execution_statuses(stack).values()) == {"ABORTED"}


def test_a_batch_reached_too_late_to_wait_for_is_still_sent(stack, monkeypatch):
    """No time left to wait: the batch goes asynchronously and is reported."""
    abort = stack["abort"]
    monkeypatch.setattr(abort, "ABORT_INVOKE_READ_TIMEOUT_SECONDS", 3600)

    result = abort.lambda_handler(
        {"arguments": {"testRunIds": [stack["run_id"]]}}, None
    )

    assert [kind for kind, _ in _workflow_events(stack)] == ["Event"]
    (error,) = result["errors"]
    assert "sent without waiting for a result" in error, error
    # Sent all the same: abortWorkflow ran and stopped them.
    assert set(_execution_statuses(stack).values()) == {"ABORTED"}


def test_the_abort_workflow_client_is_bounded_and_never_retries():
    """The module's own client carries the bounds the deadline arithmetic assumes."""
    abort = load_lambda(
        "abort_e2e_abort_test_runs_client_index",
        "nested/api-resolvers/src/lambda/abort_test_runs",
    )
    config = abort.lambda_client.meta.config
    assert config.connect_timeout == abort.ABORT_INVOKE_CONNECT_TIMEOUT_SECONDS
    assert config.read_timeout == abort.ABORT_INVOKE_READ_TIMEOUT_SECONDS
    assert config.retries["total_max_attempts"] == 1
    assert config.max_pool_connections >= abort.ABORT_INVOKE_CONCURRENCY
    assert (
        abort.ABORT_INVOKE_CONNECT_TIMEOUT_SECONDS
        + abort.ABORT_INVOKE_READ_TIMEOUT_SECONDS
        <= abort.ABORT_WAIT_BUDGET_SECONDS - abort.METRICS_START_PAUSE_SECONDS
    )


def test_a_late_batch_has_left_before_the_handler_returns(stack, monkeypatch):
    """With the budget already spent, the asynchronous send is still waited for.

    Once the handler returns the runtime may freeze its threads, so a batch whose
    request had not left would never be told to stop. The budget is zero here, so
    every batch goes asynchronously and the only wait left is the grace for that
    send; the send itself is made to take a moment of real time.
    """
    import time as real_time

    abort = stack["abort"]
    monkeypatch.setattr(abort, "ABORT_WAIT_BUDGET_SECONDS", 0)
    workflow_handler = stack["lambda"].handlers[_ABORT_WORKFLOW_ARN]

    def slow_handler(event, context):
        real_time.sleep(0.3)
        return workflow_handler(event, context)

    stack["lambda"].handlers[_ABORT_WORKFLOW_ARN] = slow_handler

    result = abort.lambda_handler(
        {"arguments": {"testRunIds": [stack["run_id"]]}}, None
    )

    assert [kind for kind, _ in _workflow_events(stack)] == ["Event"]
    assert set(_execution_statuses(stack).values()) == {"ABORTED"}
    assert result["abortedCount"] == 1, result


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        pytest.param(None, "returned an unreadable result", id="null"),
        pytest.param(
            ["not", "a", "result"], "returned an unreadable result", id="list"
        ),
        # Read as a result, then failing on its fields.
        pytest.param(
            {"errors": 7, "failedCount": "x"},
            "result could not be read",
            id="malformed-dict",
        ),
    ],
)
def test_an_unreadable_abort_workflow_result_is_reported(stack, answer, reason):
    """A result the abort resolver cannot read is unconfirmed, not a clean abort."""
    stack["lambda"].handlers[_ABORT_WORKFLOW_ARN] = lambda event, context: answer

    result = stack["abort"].lambda_handler(
        {"arguments": {"testRunIds": [stack["run_id"]]}}, None
    )

    assert result["abortedCount"] == 1, result
    (error,) = result["errors"]
    assert error.startswith(
        f"{stack['run_id']}: 3 of 3 document(s) were not confirmed stopped"
    ), error
    assert "terminal state" not in error, error
    assert reason in error, error


def test_a_batch_still_unanswered_when_the_wait_ends_is_reported(stack, monkeypatch):
    """An invoke the handler stops waiting for is unconfirmed, not a clean abort.

    The bounds are shrunk so that the wait ends a fraction of a second after the
    invokes start, and the abort workflow is made to answer only once the test
    releases it -- after the handler has returned, and without touching any
    service, so the late answer reaches nothing outside this test.
    """
    abort = stack["abort"]
    monkeypatch.setattr(abort, "ABORT_INVOKE_CONNECT_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(abort, "ABORT_INVOKE_READ_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(
        abort, "ABORT_WAIT_BUDGET_SECONDS", abort.METRICS_START_PAUSE_SECONDS + 0.2
    )
    release = threading.Event()

    def unanswered(event, context):
        release.wait(5)
        return {"abortedCount": len(event["arguments"]["objectKeys"])}

    stack["lambda"].handlers[_ABORT_WORKFLOW_ARN] = unanswered
    try:
        result = abort.lambda_handler(
            {"arguments": {"testRunIds": [stack["run_id"]]}}, None
        )
    finally:
        release.set()

    assert [kind for kind, _ in _workflow_events(stack)] == ["RequestResponse"]
    (error,) = result["errors"]
    assert "3 of 3 document(s) were not confirmed stopped" in error, error
    assert "no answer from the abort workflow in time" in error, error


def test_an_identity_written_into_an_authors_arguments_never_reaches_an_abort(
    stack, monkeypatch
):
    """An Author's request with its own identity in the arguments is refused.

    The dispatcher validates the arguments against the schema before routing, so
    the only caller-written place an identity could be read from never reaches the
    abort resolver at all.
    """
    dispatcher = _load_dispatcher(monkeypatch)
    monkeypatch.setattr(dispatcher, "_lambda", stack["lambda"].client())

    response = dispatcher.handler(
        _http_event(
            "abortTestRuns",
            {"testRunIds": [stack["run_id"]], "identity": _FORGED},
            ["Author"],
        )
    )

    assert response["statusCode"] == 400, response
    assert not stack["lambda"].calls
    assert set(_execution_statuses(stack).values()) == {"RUNNING"}


def _unabortable_in_flight_statuses(workflow):
    """Every Status that is neither abortable nor finished: still processing."""
    from idp_common.models import Status as S

    finished = load_lambda(
        "abort_e2e_abort_test_runs_status_index",
        "nested/api-resolvers/src/lambda/abort_test_runs",
    ).FINISHED_DOCUMENT_STATUSES
    return sorted(
        s.value
        for s in S
        if s not in workflow.ABORTABLE_STATUSES and s.value not in finished
    )


def _finished_statuses():
    """The statuses that end a document's life, from the document service.

    The authority is idp_common's own map from a final ObjectStatus to the
    WorkflowStatus it derives, not the abort resolver's copy under test.
    """
    from idp_common.dynamodb.service import _TERMINAL_WORKFLOW_STATUS

    finished = {s.value for s in _TERMINAL_WORKFLOW_STATUS}
    assert finished, "no terminal status found: the authority is broken"
    return finished


def test_the_finished_statuses_are_the_document_services_and_none_is_abortable(
    stack,
):
    """The abort resolver's list of final statuses, held against the authority."""
    abort, workflow = stack["abort"], stack["workflow"]
    assert abort.FINISHED_DOCUMENT_STATUSES == _finished_statuses()
    assert not abort.FINISHED_DOCUMENT_STATUSES & {
        s.value for s in workflow.ABORTABLE_STATUSES
    }
    # Non-vacuity: there are in-flight statuses abortWorkflow does not abort,
    # which is what the test below exercises.
    assert _unabortable_in_flight_statuses(workflow)


def _not_abortable_statuses():
    """Every Status abortWorkflow refuses, read from its module at collection."""
    from unittest.mock import MagicMock

    workflow = load_lambda(
        "abort_e2e_abort_workflow_statuses_index",
        "nested/api-resolvers/src/lambda/abort_workflow_resolver",
        patches={
            "idp_common.docs_service.create_document_service": lambda *a, **k: (
                MagicMock()
            )
        },
    )
    statuses = sorted(s.value for s in Status if s not in workflow.ABORTABLE_STATUSES)
    assert statuses, "no refused status found: the parametrisation is broken"
    return statuses


@pytest.mark.parametrize("status", _not_abortable_statuses())
def test_a_document_abort_workflow_declines_is_reported_unless_it_had_finished(
    stack, status
):
    """Declined because finished: not counted. Declined while in flight: reported.

    Every status abortWorkflow does not abort is driven through its real handler,
    so the classification is checked against the refusal it actually produces.
    """
    abort, workflow = stack["abort"], stack["workflow"]
    key = sorted(stack["executions"])[0]
    document = workflow.document_service.get_document(key)
    document.status = Status(status)
    # A COMPLETED document has also finished evaluating; for every other status
    # the settle wait does not read this attribute.
    document.evaluation_status = "COMPLETED"
    workflow.document_service.update_document(document)

    result = abort.lambda_handler(
        {"arguments": {"testRunIds": [stack["run_id"]]}}, None
    )

    if status in _finished_statuses():
        assert result["errors"] is None, result
    else:
        (error,) = result["errors"]
        assert error.startswith(f"{stack['run_id']}: 1 of 3 document(s)"), error
        assert f"with status {status}" in error, error


def test_every_batch_is_sent_at_once_and_every_runs_first_is_waited_for(
    stack, monkeypatch
):
    """More batches than are waited for: none is queued behind another.

    Each run's settle window is measured from the moment its documents are told
    to stop, so a run whose first batch waited behind earlier runs' batches
    would get a shorter one. Here every synchronous invoke is held until all of
    them are in flight together and every asynchronous send has arrived, which
    can only happen if all of them are made at once; and every run's first batch
    must be among those waited for.
    """
    abort, table, lam = stack["abort"], stack["table"], stack["lambda"]
    run_ids = [f"bulk-{n}" for n in range(4)]
    per_run = 250
    for run_id in run_ids:
        table.put_item(
            Item={
                "PK": f"testrun#{run_id}",
                "SK": "metadata",
                "Status": "RUNNING",
                "Files": [f"doc-{i:03d}.pdf" for i in range(per_run)],
            }
        )
    batches = len(run_ids) * -(-per_run // abort.ABORT_BATCH_SIZE)
    waited = min(batches, abort.ABORT_INVOKE_CONCURRENCY)
    assert batches > waited, "the request must have more batches than are waited for"
    together = threading.Barrier(waited, timeout=5)
    all_sent = threading.Event()
    sent = []
    lock = threading.Lock()

    def handler(event, context):
        keys = event["arguments"]["objectKeys"]
        return {"abortedCount": len(keys), "failedCount": 0, "errors": None}

    def answer(params):
        kind = params.get("InvocationType")
        if kind == "Event":
            with lock:
                sent.append(params)
                if len(sent) == batches - waited:
                    all_sent.set()
        else:
            together.wait()
            assert all_sent.wait(5), "asynchronous sends were queued behind"

    original = lam._answer

    def held(model, context, **kwargs):
        answer(context["invoke_params"])
        return original(model, context, **kwargs)

    lam.handlers[_ABORT_WORKFLOW_ARN] = handler
    client = lam.client()
    client.meta.events.unregister("before-call.lambda.Invoke", lam._answer)
    client.meta.events.register_first("before-call.lambda.Invoke", held)
    monkeypatch.setattr(abort, "lambda_client", client)

    result = abort.lambda_handler({"arguments": {"testRunIds": run_ids}}, None)

    events = _workflow_events(stack)
    assert len(events) == batches
    first_keys = {f"{run_id}/doc-000.pdf" for run_id in run_ids}
    synchronous_keys = {
        key
        for kind, event in events
        if kind == "RequestResponse"
        for key in event["arguments"]["objectKeys"]
    }
    assert first_keys <= synchronous_keys
    assert all(
        len(event["arguments"]["objectKeys"]) <= abort.ABORT_BATCH_SIZE
        for _, event in events
    )
    assert len(result["errors"]) == len(run_ids), result
    assert all("more batches than are waited for" in e for e in result["errors"])
    # Counted per run, out of the run's own documents: 40 batches, the first 20
    # waited for round-robin, so each run had 5 of its 10 batches sent unanswered.
    unanswered = (batches - waited) // len(run_ids) * abort.ABORT_BATCH_SIZE
    for run_id, error in zip(run_ids, result["errors"]):
        assert error.startswith(f"{run_id}: {unanswered} of {per_run} document(s)"), (
            error
        )
    assert "no answer" not in " ".join(result["errors"])


def test_a_request_of_500_documents_is_confirmed_in_one_round(stack, monkeypatch):
    """The design figure: up to 500 documents, every batch answered, at once.

    Pins the batch size and the concurrency together: each synchronous invoke
    carries at most 25 documents -- the abort workflow stops them one after
    another, and a batch has to answer inside the read timeout -- and all of a
    500-document request's batches are in flight at the same time.
    """
    abort, table, lam = stack["abort"], stack["table"], stack["lambda"]
    run_ids = ["half-a", "half-b"]
    for run_id in run_ids:
        table.put_item(
            Item={
                "PK": f"testrun#{run_id}",
                "SK": "metadata",
                "Status": "RUNNING",
                "Files": [f"doc-{i:03d}.pdf" for i in range(250)],
            }
        )
    together = threading.Barrier(20, timeout=5)

    def handler(event, context):
        keys = event["arguments"]["objectKeys"]
        assert len(keys) <= 25
        together.wait()
        # Stopped, as the answer says: the settle wait then reads them ABORTED.
        for key in keys:
            table.put_item(
                Item={"PK": f"doc#{key}", "SK": "none", "ObjectStatus": "ABORTED"}
            )
        return {"abortedCount": len(keys), "failedCount": 0, "errors": None}

    lam.handlers[_ABORT_WORKFLOW_ARN] = handler

    result = abort.lambda_handler({"arguments": {"testRunIds": run_ids}}, None)

    kinds = [kind for kind, _ in _workflow_events(stack)]
    assert kinds == ["RequestResponse"] * 20, kinds
    assert result["errors"] is None, result


def test_an_empty_identity_is_not_a_direct_invoke(stack):
    """Only an absent or null identity skips the group check; ``{}`` does not."""
    key = next(iter(stack["executions"]))

    with pytest.raises(PermissionError, match="Admin or Author"):
        stack["workflow"].handler(
            {"arguments": {"objectKeys": [key]}, "identity": {}}, None
        )
    assert set(_execution_statuses(stack).values()) == {"RUNNING"}


def test_the_wait_for_late_sends_is_the_connect_bound_and_no_longer(stack, monkeypatch):
    """With the budget spent, the handler waits only the connect bound for sends.

    That grace is the only part of the request allowed past the point where the
    metrics pause would begin, so it is what bounds the request's overrun. The
    bounds are shrunk to fractions of a second and the asynchronous sends made
    to hang, so the real time the handler takes is the grace itself.
    """
    import time as real_time

    abort = stack["abort"]
    monkeypatch.setattr(abort, "ABORT_WAIT_BUDGET_SECONDS", 0)
    monkeypatch.setattr(abort, "ABORT_INVOKE_CONNECT_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(abort, "ABORT_INVOKE_READ_TIMEOUT_SECONDS", 1.5)
    release = threading.Event()
    stack["lambda"].handlers[_ABORT_WORKFLOW_ARN] = lambda event, context: (
        release.wait(5) and {}
    )
    started = real_time.monotonic()
    try:
        result = abort.lambda_handler(
            {"arguments": {"testRunIds": [stack["run_id"]]}}, None
        )
        elapsed = real_time.monotonic() - started
    finally:
        release.set()

    assert 0.3 <= elapsed < 1.0, elapsed
    (error,) = result["errors"]
    assert "3 of 3 document(s)" in error, error


def _answer_and_stop(stack, failed_count, errors, stop):
    """An abortWorkflow that stops the documents named in ``stop`` and answers so."""
    workflow = stack["workflow"]

    def handler(event, context):
        keys = event["arguments"]["objectKeys"]
        for key in keys:
            if key.rsplit("/", 1)[-1] in stop:
                document = workflow.document_service.get_document(key)
                document.status = Status.ABORTED
                workflow.document_service.update_document(document)
        return {
            "success": True,
            "abortedCount": len(keys) - failed_count,
            "failedCount": failed_count,
            "errors": [error.format(run=stack["run_id"]) for error in errors]
            if errors is not None
            else None,
        }

    stack["lambda"].handlers[_ABORT_WORKFLOW_ARN] = handler


def _tail(unstopped, more):
    tail = f"; {unstopped} document(s) not confirmed stopped" if unstopped else ""
    if more:
        tail += f"; {more} {'more ' if unstopped else ''}document(s) not seen to finish"
    return tail


@pytest.mark.parametrize(
    ("failed_count", "errors", "stop", "unstopped", "more"),
    [
        # abortWorkflow's own catch-all: every document counted, none named.
        pytest.param(3, ["boom"], set(), 3, 0, id="catch-all"),
        pytest.param(3, None, set(), 3, 0, id="count-without-errors"),
        # One counted unnamed, three unfinished: two of them are certainly others.
        pytest.param(1, None, set(), 1, 2, id="fewer-unnamed-than-unsettled"),
        # Two counted unnamed, one unfinished: it may be one of the two.
        pytest.param(
            2, None, {"b.pdf", "c.pdf"}, 2, 0, id="more-unnamed-than-unsettled"
        ),
        # A count larger than the batch counts the batch, not the count.
        pytest.param(5, None, set(), 3, 0, id="count-beyond-the-batch"),
        # ... and the named one is not counted again among the unnamed.
        pytest.param(5, ["{run}/a.pdf: boom"], set(), 3, 0, id="beyond-with-a-name"),
        # Named and still unfinished: counted once, as not confirmed stopped.
        pytest.param(1, ["{run}/a.pdf: boom"], {"b.pdf"}, 1, 1, id="named-unsettled"),
        # Named but then settled: the two unfinished ones are other documents.
        pytest.param(1, ["{run}/a.pdf: boom"], {"a.pdf"}, 1, 2, id="named-settled"),
    ],
)
def test_each_document_is_counted_once_whatever_the_abort_workflow_names(
    stack, failed_count, errors, stop, unstopped, more
):
    """The two counts together never count a document twice, nor drop one.

    A failure abortWorkflow counts without naming the document is some document
    of that batch it did not name, so an unfinished document there may be the
    same one. The report gives the fewest documents consistent with both
    readings: every unfinished document is either named, or one an unnamed
    failure may stand for, or reported as one more.
    """
    _answer_and_stop(stack, failed_count, errors, stop)

    result = stack["abort"].lambda_handler(
        {"arguments": {"testRunIds": [stack["run_id"]]}}, None
    )

    assert result["abortedCount"] == 1, result
    assert result["message"] == "Aborted 1 test run(s)" + _tail(unstopped, more), result
    (error,) = result["errors"]
    assert error.startswith(
        f"{stack['run_id']}: {unstopped} of 3 document(s) were not confirmed stopped"
    ), error
    if more:
        assert error.endswith(
            f"; {more} other of 3 document(s) had not been seen to reach a terminal state when "
            "the abort returned and may still be processing"
        ), error
    else:
        assert "terminal state" not in error, error


def test_an_unnamed_failure_never_stands_for_another_batchs_document(
    stack, monkeypatch
):
    """Batches are disjoint, so one batch's unnamed failure is not another's document.

    The run's three documents go in two batches, [a, b] and [c]. The first batch
    answers one failure without naming it, and both of its documents then
    settle; the second answers that it stopped c, which goes on running. c
    cannot be the first batch's unnamed failure, so it is one more.
    """
    monkeypatch.setattr(stack["abort"], "ABORT_BATCH_SIZE", 2)
    workflow = stack["workflow"]
    run = stack["run_id"]

    def handler(event, context):
        keys = event["arguments"]["objectKeys"]
        if f"{run}/b.pdf" in keys:
            for key in keys:
                document = workflow.document_service.get_document(key)
                document.status = Status.ABORTED
                workflow.document_service.update_document(document)
            return {"abortedCount": 1, "failedCount": 1, "errors": None}
        return {"abortedCount": len(keys), "failedCount": 0, "errors": None}

    stack["lambda"].handlers[_ABORT_WORKFLOW_ARN] = handler

    result = stack["abort"].lambda_handler({"arguments": {"testRunIds": [run]}}, None)

    assert len(_workflow_events(stack)) == 2
    assert result["message"] == "Aborted 1 test run(s)" + _tail(1, 1), result


def test_an_error_is_attributed_to_the_longest_key_it_names(stack):
    """A key can be another key followed by ": "; only the longer one failed.

    The run gains a fourth document, ``a.pdf: copy.pdf``. abortWorkflow answers
    that it stopped all but that one, while a.pdf goes on running. The error
    names ``a.pdf: copy.pdf``; a.pdf must still be reported as unfinished
    rather than taken to be the failed one.
    """
    run = stack["run_id"]
    stack["table"].update_item(
        Key={"PK": f"testrun#{run}", "SK": "metadata"},
        UpdateExpression="SET Files = list_append(Files, :more)",
        ExpressionAttributeValues={":more": ["a.pdf: copy.pdf"]},
    )
    _answer_and_stop(
        stack,
        1,
        ["{run}/a.pdf: copy.pdf: Failed to update status: boom"],
        {"b.pdf", "c.pdf"},
    )

    result = stack["abort"].lambda_handler({"arguments": {"testRunIds": [run]}}, None)

    assert result["message"] == "Aborted 1 test run(s)" + _tail(1, 1), result
    (error,) = result["errors"]
    assert error.startswith(f"{run}: 1 of 4 document(s) were not confirmed"), error


@pytest.mark.parametrize(
    ("errors", "named"),
    [
        pytest.param(
            ["run/a.pdf: copy.pdf: boom"], ["run/a.pdf: copy.pdf"], id="longer"
        ),
        pytest.param(["run/a.pdf: boom"], ["run/a.pdf"], id="shorter"),
        pytest.param(
            ["run/a.pdf: boom", "run/a.pdf: copy.pdf: boom"],
            ["run/a.pdf", "run/a.pdf: copy.pdf"],
            id="both",
        ),
        # Only a "<key>: " prefix names a key; abortWorkflow's catch-all error
        # is the exception's text, which can begin with anything.
        pytest.param(["run/a.pdf is not a key"], [], id="no-separator"),
    ],
)
def test_the_batch_answer_names_each_failed_key_once(stack, monkeypatch, errors, named):
    """``_stop_batch`` names exactly the keys the errors belong to."""
    abort = stack["abort"]
    batch = ["run/a.pdf", "run/a.pdf: copy.pdf", "run/b.pdf"]
    answer = {
        "abortedCount": 3 - len(errors),
        "failedCount": len(errors),
        "errors": errors,
    }

    class _Answer:
        def invoke(self, **kwargs):
            return {"Payload": io.BytesIO(json.dumps(answer).encode())}

    monkeypatch.setattr(abort, "lambda_client", _Answer())

    reasons, keys, unnamed = abort._stop_batch(
        "fn", batch, None, confirm_by=stack["clock"].time() + 3600
    )

    assert (keys, unnamed) == (named, len(errors) - len(named))
    assert reasons == errors


def test_a_send_still_queued_when_the_wait_ends_is_reported(stack, monkeypatch):
    """A batch whose send had not even started is counted, with its reason.

    One document per batch and one send at a time: with the budget spent every
    batch goes asynchronously, the first of each group hangs in its send, and
    the third is still queued behind the second when the wait ends.
    """
    abort = stack["abort"]
    monkeypatch.setattr(abort, "ABORT_BATCH_SIZE", 1)
    monkeypatch.setattr(abort, "ABORT_INVOKE_CONCURRENCY", 1)
    monkeypatch.setattr(abort, "ABORT_WAIT_BUDGET_SECONDS", 0)
    monkeypatch.setattr(abort, "ABORT_INVOKE_CONNECT_TIMEOUT_SECONDS", 0.2)
    release = threading.Event()
    stack["lambda"].handlers[_ABORT_WORKFLOW_ARN] = lambda event, context: (
        release.wait(5) and {}
    )
    try:
        result = abort.lambda_handler(
            {"arguments": {"testRunIds": [stack["run_id"]]}}, None
        )
        sent_by_return = len(_workflow_events(stack))
    finally:
        release.set()
        # The queued send goes out once the one ahead of it returns. Wait for
        # it here, while the patched client is still in place, so that it never
        # reaches the module's real client after this test.
        import time as real_time

        give_up = real_time.monotonic() + 5
        while len(_workflow_events(stack)) < 3 and real_time.monotonic() < give_up:
            real_time.sleep(0.01)

    assert sent_by_return == 2
    assert len(_workflow_events(stack)) == 3
    (error,) = result["errors"]
    assert error.startswith(f"{stack['run_id']}: 3 of 3 document(s)"), error
    assert "no answer from the abort workflow in time" in error, error
    assert "the stop request had not been sent when the abort returned" in error, error
    assert result["message"].endswith("; 3 document(s) not confirmed stopped"), result


def test_a_document_the_wait_never_read_is_reported_as_not_seen_to_finish(
    stack, monkeypatch
):
    """With a send still unanswered, the settle wait has no time left to read.

    The grace for a late send ends at the request's deadline, after the point
    the settle wait must end, so no status is read. Every document not already
    counted is then reported as not seen to finish -- here a.pdf, which was
    confirmed stopped and is ABORTED -- and the wording says only that. Real
    clock, bounds scaled down; one document per batch and one send at a time,
    so a is waited for and b and c are sent, c's send hanging.
    """
    import time as real_time

    abort = stack["abort"]
    monkeypatch.setattr(abort, "time", real_time)
    monkeypatch.setattr(abort, "ABORT_BATCH_SIZE", 1)
    monkeypatch.setattr(abort, "ABORT_INVOKE_CONCURRENCY", 1)
    monkeypatch.setattr(abort, "ABORT_WAIT_BUDGET_SECONDS", 3)
    monkeypatch.setattr(abort, "ABORT_INVOKE_CONNECT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(abort, "ABORT_INVOKE_READ_TIMEOUT_SECONDS", 0.5)
    run = stack["run_id"]
    workflow = stack["workflow"]
    release = threading.Event()
    reads = []
    real_batch_get = abort._batch_get_document_items
    monkeypatch.setattr(
        abort,
        "_batch_get_document_items",
        lambda *a, **k: reads.append(1) or real_batch_get(*a, **k),
    )

    def handler(event, context):
        (key,) = event["arguments"]["objectKeys"]
        if key.endswith("c.pdf"):
            release.wait(5)
        if key.endswith("a.pdf"):
            document = workflow.document_service.get_document(key)
            document.status = Status.ABORTED
            workflow.document_service.update_document(document)
        return {"abortedCount": 1, "failedCount": 0, "errors": None}

    stack["lambda"].handlers[_ABORT_WORKFLOW_ARN] = handler
    try:
        result = abort.lambda_handler({"arguments": {"testRunIds": [run]}}, None)
    finally:
        release.set()

    assert not reads, "the premise: the settle wait read nothing"
    assert _document_statuses(stack)[f"{run}/a.pdf"] == "ABORTED"
    assert result["message"] == "Aborted 1 test run(s)" + _tail(2, 1), result
    (error,) = result["errors"]
    assert error.startswith(f"{run}: 2 of 3 document(s) were not confirmed"), error
    assert error.endswith(
        "; 1 other of 3 document(s) had not been seen to reach a terminal state "
        "when the abort returned and may still be processing"
    ), error


def test_a_completed_document_still_evaluating_is_not_seen_to_finish(stack):
    """COMPLETED counts as finished only once its evaluation has a final status.

    abortWorkflow declines the COMPLETED document as finished, so it is not
    counted as unstopped; the settle wait still reads its evaluation running,
    so it is reported as not seen to finish. The other two are stopped.
    """
    workflow = stack["workflow"]
    evaluating = sorted(stack["executions"])[0]
    document = workflow.document_service.get_document(evaluating)
    document.status = Status.COMPLETED
    document.evaluation_status = "RUNNING"
    workflow.document_service.update_document(document)

    result = stack["abort"].lambda_handler(
        {"arguments": {"testRunIds": [stack["run_id"]]}}, None
    )

    assert result["message"] == "Aborted 1 test run(s)" + _tail(0, 1), result
    assert result["errors"] == [
        f"{stack['run_id']}: 1 of 3 document(s) had not been seen to reach a "
        "terminal state when the abort returned and may still be processing"
    ], result


@pytest.mark.parametrize("dynamodb_error", [True, False], ids=["dynamodb", "other"])
def test_a_failed_status_write_in_the_document_abort_is_reported(
    stack, monkeypatch, dynamodb_error
):
    """abortWorkflow's own write of ABORTED failing is a failure, not a finish.

    The write is made to fail for one document, with a DynamoDB error that is
    not a refused condition and with any other exception. That document must be
    reported as not confirmed stopped with the failure, and not mistaken for
    one that had already finished.
    """
    workflow = stack["workflow"]
    failing = sorted(stack["executions"])[0]
    service = workflow.document_service
    real_update = service.update_document

    def update_document(document, *args, **kwargs):
        if document.input_key == failing:
            if dynamodb_error:
                raise workflow.DynamoDBError("boom", "InternalServerError")
            raise RuntimeError("boom")
        return real_update(document, *args, **kwargs)

    monkeypatch.setattr(service, "update_document", update_document)

    result = stack["abort"].lambda_handler(
        {"arguments": {"testRunIds": [stack["run_id"]]}}, None
    )

    (error,) = result["errors"]
    assert error.startswith(
        f"{stack['run_id']}: 1 of 3 document(s) were not confirmed stopped and may "
        f"still be processing: {failing}: Failed to update status: boom"
    ), error
    assert "terminal state" not in error, error
