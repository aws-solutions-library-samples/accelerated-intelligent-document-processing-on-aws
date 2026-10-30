# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Creating and deleting the S3 Vectors knowledge base, through ``lambda_handler``.

Two defects are pinned here. The Delete branch logged every DeleteKnowledgeBase
error and reported SUCCESS, so a refused delete left the knowledge base behind
with the stack gone. And CreateKnowledgeBase's idempotency token was built from a
seconds timestamp, so a retry of the same Create sent a different token and could
create a second knowledge base.

The client is a real ``bedrock-agent`` client under ``botocore.stub.Stubber``, so
every request is validated against the installed service model and every error
code is one that model declares for the operation. Each stub is consumed once and
``assert_no_pending_responses`` checks every queued call was made, in order.
"""

from __future__ import annotations

import itertools
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import botocore.session
import botocore.validate
import handler
import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import (
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from botocore.stub import ANY, Stubber

REGION = "us-east-1"
KB_ID = "KB12345678"
PHYSICAL_ID = f"bedrock-kb://{KB_ID}"
MODEL = botocore.session.get_session().get_service_model("bedrock-agent")
CREATED = "2026-01-01T00:00:00Z"


def _errors(operation: str) -> set[str]:
    return {shape.name for shape in MODEL.operation_model(operation).error_shapes}


@pytest.fixture
def agent(monkeypatch):
    """A stubbed real ``bedrock-agent`` client that ``handler`` will receive."""
    client = botocore.session.get_session().create_client(
        "bedrock-agent", region_name=REGION
    )
    calls: list[str] = []
    client.meta.events.register(
        "provide-client-params.bedrock-agent",
        lambda model, **_kwargs: calls.append(model.name),
    )
    tokens: list[str] = []
    client.meta.events.register(
        "provide-client-params.bedrock-agent.CreateKnowledgeBase",
        lambda params, **_kwargs: tokens.append(params.get("clientToken")),
    )
    stubber = Stubber(client)
    monkeypatch.setattr(handler.boto3, "client", lambda name, **_kwargs: client)
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    send = MagicMock()
    monkeypatch.setattr(handler.cfnresponse, "send", send)
    with stubber:
        yield SimpleNamespace(
            stubber=stubber, send=send, sleeps=sleeps, calls=calls, tokens=tokens
        )
        stubber.assert_no_pending_responses()


def _event(request_type: str, **overrides) -> dict:
    event = {
        "RequestType": request_type,
        "ResourceProperties": {
            "ResourceType": "S3VectorsKnowledgeBase",
            "Name": "IDP-DOCUMENTKB-S3Vectors",
            "Description": "Bedrock Knowledge Base with S3 Vectors",
            "RoleArn": "arn:aws:iam::123456789012:role/kb",
            "EmbeddingModelArn": (
                "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0"
            ),
            "IndexArn": "arn:aws:s3vectors:us-east-1:123456789012:bucket/b/index/i",
            "Region": REGION,
        },
        "PhysicalResourceId": PHYSICAL_ID,
        "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/s/x",
        "RequestId": "req-00000000",
        "LogicalResourceId": "KnowledgeBaseS3VectorsCustom",
        "ResponseURL": "https://example.invalid/response",
    }
    event.update(overrides)
    return event


def _run(event: dict, agent, context=None) -> tuple[str, str]:
    handler.lambda_handler(event, context or SimpleNamespace(log_stream_name="ls"))
    assert agent.send.call_count == 1, agent.send.call_args_list
    return agent.send.call_args.args[2], agent.send.call_args.kwargs.get("reason", "")


def _delete(stubber, status="DELETING", **error):
    params = {"knowledgeBaseId": KB_ID}
    if error:
        stubber.add_client_error(
            "delete_knowledge_base", expected_params=params, **error
        )
    else:
        stubber.add_response(
            "delete_knowledge_base",
            {"knowledgeBaseId": KB_ID, "status": status},
            params,
        )


def _knowledge_base(status: str, failure_reasons=None) -> dict:
    kb = {
        "knowledgeBaseId": KB_ID,
        "name": "IDP-DOCUMENTKB-S3Vectors",
        "knowledgeBaseArn": f"arn:aws:bedrock:{REGION}:123456789012:knowledge-base/{KB_ID}",
        "roleArn": "arn:aws:iam::123456789012:role/kb",
        "knowledgeBaseConfiguration": {"type": "VECTOR"},
        "status": status,
        "createdAt": CREATED,
        "updatedAt": CREATED,
    }
    if failure_reasons:
        kb["failureReasons"] = failure_reasons
    return {"knowledgeBase": kb}


def _get(stubber, status=None, failure_reasons=None, **error):
    params = {"knowledgeBaseId": KB_ID}
    if error:
        stubber.add_client_error("get_knowledge_base", expected_params=params, **error)
    else:
        stubber.add_response(
            "get_knowledge_base", _knowledge_base(status, failure_reasons), params
        )


NOT_FOUND = {"service_error_code": "ResourceNotFoundException", "http_status_code": 404}
INVALID = {"service_error_code": "ValidationException", "http_status_code": 400}
CONFLICT = {"service_error_code": "ConflictException", "http_status_code": 409}
THROTTLED = {"service_error_code": "ThrottlingException", "http_status_code": 429}
DENIED = {"service_error_code": "AccessDeniedException", "http_status_code": 403}
INTERNAL = {"service_error_code": "InternalServerException", "http_status_code": 500}


def test_error_codes_come_from_the_service_model():
    """Every code the handler branches on is one the operation can send."""
    assert handler.KB_ABSENT_CODES, "an empty set would make every check vacuous"
    assert handler.KB_DELETE_RETRYABLE_CODES
    assert handler.KB_ABSENT_CODES <= _errors("GetKnowledgeBase")
    assert handler.KB_ABSENT_CODES <= _errors("DeleteKnowledgeBase")
    assert handler.KB_DELETE_RETRYABLE_CODES <= _errors("DeleteKnowledgeBase")
    statuses = set(MODEL.shape_for("KnowledgeBaseStatus").enum)
    assert {"DELETING", "DELETE_UNSUCCESSFUL"} <= statuses


def test_delete_waits_for_the_knowledge_base_to_disappear(agent):
    _delete(agent.stubber)
    _get(agent.stubber, "DELETING")
    _get(agent.stubber, **NOT_FOUND)

    assert _run(_event("Delete"), agent)[0] == "SUCCESS"
    assert agent.calls == [
        "DeleteKnowledgeBase",
        "GetKnowledgeBase",
        "GetKnowledgeBase",
    ]


def test_delete_of_a_knowledge_base_already_gone_succeeds(agent):
    _delete(agent.stubber, **NOT_FOUND)

    assert _run(_event("Delete"), agent)[0] == "SUCCESS"
    assert agent.calls == ["DeleteKnowledgeBase"]


def test_delete_retries_a_conflict_then_succeeds(agent):
    """A knowledge base still CREATING or UPDATING refuses delete for a while."""
    _delete(agent.stubber, **CONFLICT)
    _delete(agent.stubber, **THROTTLED)
    _delete(agent.stubber, **INTERNAL)
    _delete(agent.stubber)
    _get(agent.stubber, **NOT_FOUND)

    assert _run(_event("Delete"), agent)[0] == "SUCCESS"
    assert agent.calls.count("DeleteKnowledgeBase") == 4
    assert agent.sleeps[:3] == [2, 4, 8]


@pytest.mark.parametrize(
    "remaining", ["ACTIVE", "CREATING", "UPDATING", "FAILED", "DELETE_UNSUCCESSFUL"]
)
def test_delete_fails_while_a_conflicting_knowledge_base_still_exists(agent, remaining):
    """Retries are bounded, and SUCCESS is not reported while it demonstrably exists."""
    for _ in range(handler.KB_DELETE_ATTEMPTS):
        _delete(agent.stubber, **CONFLICT)
    _get(agent.stubber, remaining)

    status, reason = _run(_event("Delete"), agent)

    assert status == "FAILED"
    assert KB_ID in reason and remaining in reason
    assert "ConflictException" in reason, "the reason must carry the last refusal"
    assert agent.calls.count("DeleteKnowledgeBase") == handler.KB_DELETE_ATTEMPTS
    # Doubling from 2 seconds, capped at 30.
    assert agent.sleeps == [2, 4, 8, 16] + [30] * (handler.KB_DELETE_ATTEMPTS - 5)


def test_the_worst_case_sleep_leaves_room_in_the_timeout():
    """Every sleep the delete can take, summed, against the 600-second timeout.

    The longest path is every retry but the last refused, the delete then
    accepted, and the whole wait spent polling. This bounds the sleeps only; the
    time the calls themselves take is what the deadline test below covers.
    """
    retries = sum(
        min(
            handler.KB_DELETE_BACKOFF_SECONDS * 2 ** (attempt - 1),
            handler.KB_DELETE_MAX_BACKOFF_SECONDS,
        )
        for attempt in range(1, handler.KB_DELETE_ATTEMPTS)
    )
    wait = handler.KB_DELETE_WAIT_POLLS * handler.KB_DELETE_WAIT_SECONDS
    status_checks = sum(
        handler.KB_DELETE_BACKOFF_SECONDS * attempt
        for attempt in range(1, handler.KB_STATUS_ATTEMPTS)
    )
    assert retries >= 240, "long enough for a CREATING knowledge base to settle"
    assert retries + wait + status_checks <= 420


@pytest.mark.parametrize(
    ("final", "expected"),
    [
        ({"status": "DELETING"}, "SUCCESS"),
        (NOT_FOUND, "SUCCESS"),
    ],
    ids=["already-deleting", "gone"],
)
def test_conflicts_on_a_knowledge_base_being_deleted_do_not_fail_the_delete(
    agent, final, expected
):
    for _ in range(handler.KB_DELETE_ATTEMPTS):
        _delete(agent.stubber, **CONFLICT)
    _get(agent.stubber, **final)

    assert _run(_event("Delete"), agent)[0] == expected


def test_a_refused_delete_of_a_knowledge_base_already_deleting_succeeds(agent):
    """A Lambda retry can reach a knowledge base the first attempt already deleted."""
    _delete(agent.stubber, **INVALID)
    _get(agent.stubber, "DELETING")

    assert _run(_event("Delete"), agent)[0] == "SUCCESS"


def test_a_refused_delete_fails_when_the_knowledge_base_still_exists(agent):
    """The original defect: AccessDenied was logged and the stack delete succeeded."""
    _delete(agent.stubber, **DENIED)
    _get(agent.stubber, "ACTIVE")

    status, reason = _run(_event("Delete"), agent)

    assert status == "FAILED"
    assert KB_ID in reason and "AccessDeniedException" in reason
    assert agent.calls == ["DeleteKnowledgeBase", "GetKnowledgeBase"]


@pytest.mark.parametrize("on_get", [NOT_FOUND, INVALID], ids=["not-found", "invalid"])
@pytest.mark.parametrize("on_delete", [DENIED, INVALID], ids=["denied", "invalid"])
def test_a_refused_delete_of_a_knowledge_base_that_does_not_exist_succeeds(
    agent, on_delete, on_get
):
    """A rollback after a failed Create must not end in DELETE_FAILED."""
    _delete(agent.stubber, **on_delete)
    _get(agent.stubber, **on_get)

    assert _run(_event("Delete"), agent)[0] == "SUCCESS"


def test_a_throttled_existence_check_is_retried_before_it_decides(agent):
    """A transient read error must not turn a refused delete into DELETE_FAILED."""
    _delete(agent.stubber, **DENIED)
    _get(agent.stubber, **THROTTLED)
    _get(agent.stubber, **NOT_FOUND)

    assert _run(_event("Delete"), agent)[0] == "SUCCESS"
    assert agent.calls == [
        "DeleteKnowledgeBase",
        "GetKnowledgeBase",
        "GetKnowledgeBase",
    ]


def test_the_existence_check_retries_are_bounded(agent):
    _delete(agent.stubber, **DENIED)
    for _ in range(handler.KB_STATUS_ATTEMPTS):
        _get(agent.stubber, **THROTTLED)

    status, reason = _run(_event("Delete"), agent)

    assert status == "FAILED"
    assert "ThrottlingException" in reason
    assert agent.calls.count("GetKnowledgeBase") == handler.KB_STATUS_ATTEMPTS
    assert agent.sleeps == [2, 4]


def test_a_throttled_check_after_exhausted_retries_is_retried(agent):
    for _ in range(handler.KB_DELETE_ATTEMPTS):
        _delete(agent.stubber, **CONFLICT)
    _get(agent.stubber, **THROTTLED)
    _get(agent.stubber, "DELETING")

    assert _run(_event("Delete"), agent)[0] == "SUCCESS"


def test_delete_unsuccessful_fails_with_the_service_reason(agent):
    _delete(agent.stubber)
    _get(agent.stubber, "DELETING")
    _get(agent.stubber, "DELETE_UNSUCCESSFUL", ["vector index unreachable"])

    status, reason = _run(_event("Delete"), agent)

    assert status == "FAILED"
    assert "DELETE_UNSUCCESSFUL" in reason and "vector index unreachable" in reason


@pytest.mark.parametrize("error", [THROTTLED, DENIED], ids=["throttled", "denied"])
def test_an_unreadable_first_poll_keeps_waiting(agent, error):
    """A read error while the delete runs is not a verdict on the delete."""
    _delete(agent.stubber)
    _get(agent.stubber, **error)
    _get(agent.stubber, **NOT_FOUND)

    assert _run(_event("Delete"), agent)[0] == "SUCCESS"
    assert agent.calls.count("GetKnowledgeBase") == 2


def test_a_delete_still_in_progress_after_the_wait_is_reported_deleted(agent):
    """The delete was accepted; failing here would strand the stack for a transient state."""
    _delete(agent.stubber)
    for poll in range(handler.KB_DELETE_WAIT_POLLS):
        if poll == 3:
            _get(agent.stubber, **THROTTLED)
        else:
            _get(agent.stubber, "DELETING")

    assert _run(_event("Delete"), agent)[0] == "SUCCESS"
    assert agent.calls.count("GetKnowledgeBase") == handler.KB_DELETE_WAIT_POLLS
    assert sum(agent.sleeps) >= 120, "the wait gives Bedrock two minutes"
    assert (
        agent.sleeps == [handler.KB_DELETE_WAIT_SECONDS] * handler.KB_DELETE_WAIT_POLLS
    )


def test_delete_of_a_resource_whose_create_failed_makes_no_call(agent):
    event = _event("Delete", PhysicalResourceId="bedrock-kb-failed://x-req-0000")

    assert _run(event, agent)[0] == "SUCCESS"
    assert agent.calls == []


def _create_response():
    return {
        "knowledgeBase": _knowledge_base("CREATING")["knowledgeBase"],
    }


def _expect_create(stubber):
    stubber.add_response(
        "create_knowledge_base",
        _create_response(),
        {
            "clientToken": ANY,
            "name": ANY,
            "description": ANY,
            "roleArn": ANY,
            "knowledgeBaseConfiguration": ANY,
            "storageConfiguration": ANY,
        },
    )


def test_a_retried_create_sends_the_same_token(agent, monkeypatch):
    """The token must not depend on when the attempt ran."""
    clock = itertools.count(1_761_300_000)
    monkeypatch.setattr("time.time", lambda: float(next(clock)))
    _expect_create(agent.stubber)
    _expect_create(agent.stubber)

    for _ in range(2):
        agent.send.reset_mock()
        assert _run(_event("Create"), agent)[0] == "SUCCESS"

    assert len(agent.tokens) == 2
    assert agent.tokens[0] == agent.tokens[1]


@pytest.mark.parametrize("field", ["RequestId", "StackId", "LogicalResourceId"])
def test_a_different_request_sends_a_different_token(agent, field):
    _expect_create(agent.stubber)
    _expect_create(agent.stubber)

    _run(_event("Create"), agent)
    agent.send.reset_mock()
    _run(_event("Create", **{field: "other"}), agent)

    assert len(agent.tokens) == 2
    assert agent.tokens[0] != agent.tokens[1]


def test_the_token_meets_the_service_model_constraints(agent):
    """botocore checks the length but not the pattern, so the pattern is checked here."""
    _expect_create(agent.stubber)
    _run(_event("Create"), agent)

    shape = MODEL.operation_model("CreateKnowledgeBase").input_shape.members[
        "clientToken"
    ]
    token = agent.tokens[0]
    assert shape.metadata["min"] <= len(token) <= shape.metadata["max"]
    assert re.fullmatch(shape.metadata["pattern"], token), token


def test_the_update_fallback_create_also_sends_a_derived_token(agent):
    _expect_create(agent.stubber)

    event = _event("Update", PhysicalResourceId="")
    assert _run(event, agent)[0] == "SUCCESS"

    assert agent.tokens == [handler.knowledge_base_client_token(event)]


class _SlowBedrock:
    """A real ``bedrock-agent`` client answered by a ``before-call`` hook, not Stubber.

    Stubber needs every call queued in advance, and how many calls the handler
    makes here depends on the deadline under test; Stubber also cannot raise the
    transport errors botocore raises when a connection fails. Each operation gets
    a list of answers, used in order with the last one repeating. An answer is
    either ``(http_status, body)``, validated against the service model's output
    shape, or an exception to raise. Every call costs ``call_seconds`` of the
    invocation, standing for what one call can take before the handler sees its
    answer, botocore's own retries included.
    """

    def __init__(self, monkeypatch, answers, call_seconds):
        self.remaining = 600.0
        self.remaining_at_send: float | None = None
        self.sleeps: list[float] = []
        self.calls: list[str] = []
        self.client_kwargs: dict = {}
        self.log_stream_name = "ls"
        self.client = botocore.session.get_session().create_client(
            "bedrock-agent", region_name=REGION
        )
        queues = {name: list(queue) for name, queue in answers.items()}

        def answer(model, **_kwargs):
            self.calls.append(model.name)
            self.remaining -= call_seconds
            queue = queues[model.name]
            reply = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(reply, Exception):
                raise reply
            status, body = reply
            body = dict(body, ResponseMetadata={"HTTPStatusCode": status})
            if status < 300:
                validate = botocore.validate.ParamValidator().validate
                report = validate(
                    {k: v for k, v in body.items() if k != "ResponseMetadata"},
                    model.output_shape,
                )
                assert not report.has_errors(), report.generate_report()
            return AWSResponse("https://example.invalid", status, {}, None), body

        def client(name, **kwargs):
            self.client_kwargs = kwargs
            return self.client

        self.client.meta.events.register_first("before-call.bedrock-agent", answer)
        monkeypatch.setattr(handler.boto3, "client", client)
        monkeypatch.setattr("time.sleep", self._sleep)
        send = MagicMock(side_effect=self._send)
        monkeypatch.setattr(handler.cfnresponse, "send", send)
        self.send = send

    def get_remaining_time_in_millis(self) -> int:
        return int(self.remaining * 1000)

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.remaining -= seconds

    def _send(self, *_args, **_kwargs) -> None:
        self.remaining_at_send = self.remaining

    def run(self) -> str:
        handler.lambda_handler(_event("Delete"), self)
        assert self.send.call_count == 1
        return self.send.call_args.args[2]


ENDPOINT = "https://bedrock-agent.us-east-1.amazonaws.com"
THROTTLED_ANSWER = (429, {"Error": {"Code": "ThrottlingException", "Message": ""}})
DENIED_ANSWER = (403, {"Error": {"Code": "AccessDeniedException", "Message": ""}})
GONE_ANSWER = (404, {"Error": {"Code": "ResourceNotFoundException", "Message": ""}})
DELETE_STARTED_ANSWER = (202, {"knowledgeBaseId": KB_ID, "status": "DELETING"})


def _read_timeout():
    return ReadTimeoutError(endpoint_url=ENDPOINT)


def _status_answer(status: str):
    return (200, _knowledge_base(status))


def test_the_client_bounds_each_call():
    """The per-call bound the deadline relies on follows from the client config.

    Read back from a client built with the config, because botocore reads
    ``max_attempts`` as a retry count and rewrites it to one more total attempt;
    measured against a local server, ``max_attempts: 2`` sent three requests.
    The bound is the total attempts, each at most a connect plus a read timeout,
    plus the standard-mode backoff between them, at most one second each.
    """
    client = botocore.session.get_session().create_client(
        "bedrock-agent", region_name=REGION, config=handler.KB_CLIENT_CONFIG
    )
    config = client.meta.config
    retries = config.retries or {}
    assert retries.get("mode") == "standard"
    attempts = retries["total_max_attempts"]
    connect, read = config.connect_timeout, config.read_timeout
    assert isinstance(connect, (int, float)) and isinstance(read, (int, float))
    assert attempts * (connect + read) + (attempts - 1) <= handler.KB_CALL_SECONDS
    assert handler.KB_DEADLINE_MARGIN_SECONDS >= (
        2 * handler.KB_CALL_SECONDS + handler.KB_RESPONSE_SECONDS
    )


@pytest.mark.parametrize("request_type", ["Create", "Update"])
def test_create_and_update_keep_the_default_retry_budget(monkeypatch, request_type):
    """Only Delete adds retries of its own, so only Delete gets the bounded client."""
    built: list[dict] = []

    def client(name, **kwargs):
        built.append(kwargs)
        raise RuntimeError("stop after the client is built")

    monkeypatch.setattr(handler.boto3, "client", client)
    monkeypatch.setattr(handler.cfnresponse, "send", MagicMock())

    handler.lambda_handler(_event(request_type), SimpleNamespace(log_stream_name="ls"))

    assert built and "config" not in built[0]


def test_the_handler_builds_the_bounded_client(monkeypatch):
    bedrock = _SlowBedrock(monkeypatch, {"DeleteKnowledgeBase": [GONE_ANSWER]}, 0)

    assert bedrock.run() == "SUCCESS"
    assert bedrock.client_kwargs.get("config") is handler.KB_CLIENT_CONFIG


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (
            {
                "DeleteKnowledgeBase": [
                    EndpointConnectionError(endpoint_url=ENDPOINT),
                    DELETE_STARTED_ANSWER,
                ],
                "GetKnowledgeBase": [GONE_ANSWER],
            },
            ["DeleteKnowledgeBase", "DeleteKnowledgeBase", "GetKnowledgeBase"],
        ),
        (
            {
                "DeleteKnowledgeBase": [DELETE_STARTED_ANSWER],
                "GetKnowledgeBase": [
                    ConnectionClosedError(endpoint_url=ENDPOINT),
                    GONE_ANSWER,
                ],
            },
            ["DeleteKnowledgeBase", "GetKnowledgeBase", "GetKnowledgeBase"],
        ),
        (
            {
                "DeleteKnowledgeBase": [DENIED_ANSWER],
                "GetKnowledgeBase": [_read_timeout(), GONE_ANSWER],
            },
            ["DeleteKnowledgeBase", "GetKnowledgeBase", "GetKnowledgeBase"],
        ),
    ],
    ids=["on-delete", "while-waiting", "on-the-existence-check"],
)
def test_a_transport_error_is_transient(monkeypatch, answers, expected):
    """A connection that fails says nothing about the knowledge base; try again."""
    bedrock = _SlowBedrock(monkeypatch, answers, 0)

    assert bedrock.run() == "SUCCESS"
    assert bedrock.calls == expected


@pytest.mark.parametrize(
    ("delete", "get", "expected"),
    [
        (THROTTLED_ANSWER, THROTTLED_ANSWER, "FAILED"),
        (THROTTLED_ANSWER, _status_answer("ACTIVE"), "FAILED"),
        (_read_timeout(), _read_timeout(), "FAILED"),
        (DELETE_STARTED_ANSWER, _status_answer("DELETING"), "SUCCESS"),
        (DELETE_STARTED_ANSWER, _read_timeout(), "SUCCESS"),
    ],
    ids=[
        "all-throttled",
        "delete-throttled-kb-active",
        "all-timing-out",
        "accepted-never-finishes",
        "accepted-then-unreadable",
    ],
)
def test_the_delete_answers_before_the_function_times_out(
    monkeypatch, delete, get, expected
):
    """Every call taking as long as the client allows must not outrun the timeout.

    Each call here costs ``KB_CALL_SECONDS``, the most one call can take under
    ``KB_CLIENT_CONFIG``. The response must still go out with
    ``KB_RESPONSE_SECONDS`` left for the request that carries it. A delete Bedrock
    accepted is reported as done even when it cannot be read afterwards.
    """
    bedrock = _SlowBedrock(
        monkeypatch,
        {"DeleteKnowledgeBase": [delete], "GetKnowledgeBase": [get]},
        call_seconds=handler.KB_CALL_SECONDS,
    )

    assert bedrock.run() == expected
    assert bedrock.remaining_at_send is not None
    assert bedrock.remaining_at_send >= handler.KB_RESPONSE_SECONDS, (
        bedrock.remaining_at_send
    )
    assert bedrock.sleeps, "the scenario must reach the handler's retries or wait"


def test_the_deadline_counts_the_sleep_it_is_asked_about():
    margin = handler.KB_DEADLINE_MARGIN_SECONDS
    context = SimpleNamespace(get_remaining_time_in_millis=lambda: (margin + 30) * 1000)

    assert handler.has_time_for(context, 30)
    assert not handler.has_time_for(context, 31)
    assert handler.has_time_for(SimpleNamespace(), 10_000), "no context, no deadline"
