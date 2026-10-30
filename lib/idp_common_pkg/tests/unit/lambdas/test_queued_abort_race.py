# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Aborting a QUEUED document cannot lose a race with the queue processor (#1612).

``abortWorkflow`` has no execution to stop for a QUEUED document: it writes
ABORTED, and the queue processor is relied on not to start a document that
reads ABORTED. The processor checked once, when it read the message, so an
abort written after that read was started anyway and its success overwrote
ABORTED. Separately, an abort that read the document as QUEUED and wrote ABORTED
after the processor had started it stopped nothing.

These tests drive the real queue processor and the real ``abortWorkflow``
handler, in process, against moto's DynamoDB and Step Functions, and force each
interleaving deterministically by running one handler from inside a call the
other makes. What is asserted is the state of those services afterwards -- the
executions that exist and their status, the tracking record, the concurrency
counter -- not the shape of a call. The conditional writes are evaluated by
moto's expression engine, not DynamoDB's.
"""

from __future__ import annotations

import hashlib
import json

import boto3
import pytest
from moto import mock_aws

from idp_common.models import Document, Status
from tests.unit.lambdas._lambda_loader import load_lambda

pytestmark = pytest.mark.unit

_REGION = "us-east-1"
_TRACKING = "queued-abort-tracking"
_CONCURRENCY = "queued-abort-concurrency"
_STATE_MACHINE = "queued-abort-workflow"


def _table(ddb, name, key):
    return ddb.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": k, "KeyType": t} for k, t in key],
        AttributeDefinitions=[
            {"AttributeName": k, "AttributeType": "S"} for k, _ in key
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def stack(monkeypatch):
    """Moto services, the real queue processor and the real abortWorkflow."""
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setenv("TRACKING_TABLE", _TRACKING)
    monkeypatch.setenv("CONCURRENCY_TABLE", _CONCURRENCY)
    monkeypatch.setenv("MAX_CONCURRENT", "50")
    for var in (
        "CONFIG_TABLE",
        "WORKING_BUCKET",
        "DOCUMENT_QUEUE_URL",
        "HITL_TABLE_NAME",
        "CIRCUIT_BREAKER_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=_REGION)
        tracking = _table(ddb, _TRACKING, [("PK", "HASH"), ("SK", "RANGE")])
        counter = _table(ddb, _CONCURRENCY, [("counter_id", "HASH")])
        counter.put_item(Item={"counter_id": "workflow_counter", "active_count": 0})
        sfn = boto3.client("stepfunctions", region_name=_REGION)
        machine = sfn.create_state_machine(
            name=_STATE_MACHINE,
            definition=json.dumps(
                {"StartAt": "Wait", "States": {"Wait": {"Type": "Pass", "End": True}}}
            ),
            roleArn="arn:aws:iam::123456789012:role/sfn",
        )["stateMachineArn"]
        monkeypatch.setenv("STATE_MACHINE_ARN", machine)

        processor = load_lambda(
            "queued_abort_queue_processor_index", "src/lambda/queue_processor"
        )
        # The X-Ray recorder is a stub here; no segment means no trace id.
        monkeypatch.setattr(processor.xray_recorder, "current_segment", lambda: None)
        workflow = load_lambda(
            "queued_abort_abort_workflow_index",
            "nested/api-resolvers/src/lambda/abort_workflow_resolver",
        )
        monkeypatch.setattr(
            workflow,
            "get_setting",
            lambda key, default=None: {"StateMachineName": _STATE_MACHINE}.get(
                key, default
            ),
        )
        yield {
            "processor": processor,
            "workflow": workflow,
            "sfn": sfn,
            "machine": machine,
            "tracking": tracking,
            "counter": counter,
        }


def _queue(stack, key, status=Status.QUEUED, record=True):
    """Record a document and return the SQS record the queue sender would send."""
    document = Document(
        id=key,
        input_key=key,
        status=status,
        queued_time="2026-10-25T03:17:30+00:00",
    )
    if record:
        stack["workflow"].document_service.create_document(document)
        if status != Status.QUEUED:
            stack["workflow"].document_service.update_document(document)
    queued = Document(id=key, input_key=key, status=Status.QUEUED)
    return {
        "body": json.dumps(queued.to_dict()),
        "messageId": "msg-" + hashlib.sha256(key.encode()).hexdigest()[:16],
        "receiptHandle": "",
    }


def _abort(stack, key):
    """abortWorkflow as abortTestRuns invokes it (identity-less direct invoke)."""
    return stack["workflow"].handler({"arguments": {"objectKeys": [key]}}, None)


def _executions(stack):
    return stack["sfn"].list_executions(stateMachineArn=stack["machine"])["executions"]


def _status(stack, key):
    item = stack["tracking"].get_item(Key={"PK": f"doc#{key}", "SK": "none"})["Item"]
    return item["ObjectStatus"]


def _active(stack):
    item = stack["counter"].get_item(Key={"counter_id": "workflow_counter"})["Item"]
    return int(item["active_count"])


def _abort_after_first_call(stack, monkeypatch, target, name, key):
    """Run abortWorkflow for ``key`` right after the first ``target.name`` call."""
    real = getattr(target, name)
    fired = []

    def wrapped(*args, **kwargs):
        result = real(*args, **kwargs)
        if not fired:
            fired.append(_abort(stack, key))
        return result

    monkeypatch.setattr(target, name, wrapped)
    return fired


@pytest.mark.parametrize(
    "after",
    [
        # The interleaving observed live: the processor read the document as
        # QUEUED, then the abort wrote ABORTED, then the processor started it.
        pytest.param("read", id="after-the-processors-read"),
        # Later still: the slot is already taken when the abort lands.
        pytest.param("increment", id="after-the-counter-increment"),
    ],
)
def test_an_abort_landing_before_the_start_prevents_it(stack, monkeypatch, after):
    key = "run-1612/late-abort.pdf"
    record = _queue(stack, key)
    processor = stack["processor"]
    if after == "read":
        fired = _abort_after_first_call(
            stack, monkeypatch, processor.document_service, "get_document", key
        )
    else:
        real = processor.update_counter
        fired = []

        def update_counter(increment=True):
            result = real(increment)
            if increment and not fired:
                fired.append(_abort(stack, key))
            return result

        monkeypatch.setattr(processor, "update_counter", update_counter)

    assert processor.process_message(record) == (True, record["messageId"])

    assert fired == [
        {
            "success": True,
            "message": "Aborted 1 document(s)",
            "abortedCount": 1,
            "failedCount": 0,
            "errors": None,
        }
    ]
    assert _executions(stack) == []
    assert _status(stack, key) == "ABORTED"
    # The slot taken for a start that did not happen is handed back.
    assert _active(stack) == 0


def test_an_abort_that_read_queued_and_writes_after_the_start_stops_it(
    stack, monkeypatch
):
    """The abort read QUEUED; the processor started the document; then it wrote.

    Its ABORTED write must not land over a running document as if there were
    nothing to stop: it is refused, and the abort stops the execution instead.
    """
    key = "run-1612/started-under-the-abort.pdf"
    record = _queue(stack, key)
    workflow = stack["workflow"]
    real = workflow.document_service.get_document
    outcome = []

    def get_document(object_key, **kwargs):
        document = real(object_key, **kwargs)
        if not outcome:
            outcome.append(stack["processor"].process_message(record))
        return document

    monkeypatch.setattr(workflow.document_service, "get_document", get_document)

    result = _abort(stack, key)

    assert outcome == [(True, record["messageId"])]
    assert result["abortedCount"] == 1, result
    (execution,) = _executions(stack)
    assert execution["status"] == "ABORTED"
    assert _status(stack, key) == "ABORTED"
    # The execution owns its slot; the workflow tracker releases it on the
    # ABORTED event, so the processor did not.
    assert _active(stack) == 1


def test_every_queued_document_still_starts_when_none_is_aborted(stack):
    keys = [f"run-1612/normal-{n:02d}.pdf" for n in range(12)]
    records = [_queue(stack, key) for key in keys]
    reads = []
    events = stack["processor"].document_service.client.table.meta.client.meta.events
    events.register(
        "provide-client-params.dynamodb.GetItem",
        lambda params, **_: reads.append(params),
    )

    outcomes = [stack["processor"].process_message(r) for r in records]

    # A granted claim costs no second read of the document.
    assert len(reads) == len(keys), reads

    assert outcomes == [(True, r["messageId"]) for r in records]
    executions = _executions(stack)
    assert len(executions) == len(keys)
    assert {e["status"] for e in executions} == {"RUNNING"}
    assert {_status(stack, key) for key in keys} == {"RUNNING"}
    # One slot per execution, each owned by its execution.
    assert _active(stack) == len(keys)


@pytest.mark.parametrize(
    "status",
    sorted(s.value for s in Status if s != Status.ABORTED),
)
def test_no_status_but_aborted_holds_a_document_back(stack, status):
    """A message is started whatever status its document has, unless ABORTED.

    Messages reach the queue for documents that are not QUEUED -- a review
    finalize, a reprocess -- so the start condition is "not ABORTED", never
    "is QUEUED".
    """
    key = f"run-1612/status-{status.lower()}.pdf"
    record = _queue(stack, key, status=Status(status))

    assert stack["processor"].process_message(record) == (True, record["messageId"])

    (execution,) = _executions(stack)
    assert execution["status"] == "RUNNING"
    # The post-start write is refused only for ABORTED, so it lands here.
    assert _status(stack, key) == "RUNNING"
    assert _active(stack) == 1


def test_a_document_with_no_tracking_record_still_starts(stack):
    """The claim refuses a missing record; the start goes ahead, as it did before."""
    key = "run-1612/unrecorded.pdf"
    record = _queue(stack, key, record=False)

    assert stack["processor"].process_message(record) == (True, record["messageId"])

    (execution,) = _executions(stack)
    assert execution["status"] == "RUNNING"
    # The conditional post-start write still creates the record, as the
    # unconditional one did.
    assert _status(stack, key) == "RUNNING"
    assert _active(stack) == 1


def test_an_aborted_document_is_skipped_without_taking_a_slot(stack):
    key = "run-1612/aborted-first.pdf"
    record = _queue(stack, key)
    assert _abort(stack, key)["abortedCount"] == 1

    assert stack["processor"].process_message(record) == (True, record["messageId"])

    assert _executions(stack) == []
    assert _status(stack, key) == "ABORTED"
    assert _active(stack) == 0


def test_a_redelivery_does_not_rewind_a_finished_document(stack):
    """The claim writes nothing, so a duplicate delivery leaves the record alone.

    The first delivery started the execution; by the redelivery the document
    has COMPLETED. The redelivery is refused by Step Functions as a duplicate
    and acked, and the document must still read COMPLETED.
    """
    key = "run-1612/redelivered.pdf"
    record = _queue(stack, key)
    processor = stack["processor"]
    assert processor.process_message(record) == (True, record["messageId"])
    document = stack["workflow"].document_service.get_document(key)
    document.status = Status.COMPLETED
    stack["workflow"].document_service.update_document(document)

    assert processor.process_message(record) == (True, record["messageId"])

    assert len(_executions(stack)) == 1
    assert _status(stack, key) == "COMPLETED"
    assert _active(stack) == 1


def test_the_re_read_after_a_refused_claim_is_strongly_consistent(stack, monkeypatch):
    """The re-read that decides a refused claim must see the write that refused it.

    moto's reads are always current, so no outcome here can tell a consistent
    read from an eventually consistent one; what is asserted instead is the
    request the processor's document service sends, read off the botocore
    client at parameter-build time.
    """
    key = "run-1612/consistent.pdf"
    record = _queue(stack, key)
    processor = stack["processor"]
    reads = []
    events = processor.document_service.client.table.meta.client.meta.events
    events.register(
        "provide-client-params.dynamodb.GetItem",
        lambda params, **_: reads.append(dict(params)),
    )
    _abort_after_first_call(
        stack, monkeypatch, processor.document_service, "get_document", key
    )

    processor.process_message(record)

    assert [r.get("ConsistentRead", False) for r in reads] == [False, True], reads


def test_an_abort_whose_document_keeps_changing_is_not_reported_done(
    stack, monkeypatch
):
    """Refused twice, the abort reports a failure rather than claiming success."""
    workflow = stack["workflow"]
    # The resolver's own references: the class the real document service
    # raises, which is the one it catches.
    DynamoDBError = workflow.DynamoDBError  # noqa: N806
    CONDITIONAL_CHECK_FAILED = workflow.CONDITIONAL_CHECK_FAILED  # noqa: N806

    key = "run-1612/keeps-changing.pdf"
    _queue(stack, key)
    calls = []

    def refused(document, **kwargs):
        calls.append(kwargs)
        raise DynamoDBError("The conditional request failed", CONDITIONAL_CHECK_FAILED)

    monkeypatch.setattr(workflow.document_service, "update_document", refused)

    result = _abort(stack, key)

    assert result["abortedCount"] == 0 and result["failedCount"] == 1, result
    assert "changed while being aborted" in result["errors"][0], result
    assert calls == [{"expected_status": Status.QUEUED}] * 2, calls


@pytest.mark.parametrize("failing", ["claim_for_execution", "get_document"])
def test_a_claim_that_cannot_be_decided_starts_the_document(
    stack, monkeypatch, failing
):
    """A failed claim or re-read starts the document, as before the claim existed.

    The claim exists to hold back an aborted document; a DynamoDB fault while
    making it must not hold back one that was not.
    """
    key = f"run-1612/undecided-{failing}.pdf"
    record = _queue(stack, key)
    service = stack["processor"].document_service
    if failing == "get_document":
        # The claim is refused for want of a record, so the re-read runs.
        stack["tracking"].delete_item(Key={"PK": f"doc#{key}", "SK": "none"})
    real = getattr(service, failing)
    calls = []

    def throttled(*args, **kwargs):
        calls.append(kwargs)
        if failing == "get_document" and not kwargs:
            return real(*args, **kwargs)  # the read at the top of the message
        raise RuntimeError("ProvisionedThroughputExceededException")

    monkeypatch.setattr(service, failing, throttled)

    assert stack["processor"].process_message(record) == (True, record["messageId"])

    assert len(_executions(stack)) == 1
    assert _active(stack) == 1
    assert calls, "the failing call was never made"


def test_the_aborts_re_read_after_a_refused_write_is_strongly_consistent(
    stack, monkeypatch
):
    """The abort's re-read must see the start that refused its write.

    An eventually consistent re-read could still return QUEUED, so the abort
    would report "retry" without stopping the execution. moto cannot show the
    difference, so the request is asserted, as for the processor's re-read.
    """
    key = "run-1612/abort-reread.pdf"
    record = _queue(stack, key)
    workflow = stack["workflow"]
    reads = []
    events = workflow.document_service.client.table.meta.client.meta.events
    events.register(
        "provide-client-params.dynamodb.GetItem",
        lambda params, **_: reads.append(dict(params)),
    )
    real = workflow.document_service.get_document
    started = []

    def get_document(object_key, **kwargs):
        document = real(object_key, **kwargs)
        if not started:
            started.append(stack["processor"].process_message(record))
        return document

    monkeypatch.setattr(workflow.document_service, "get_document", get_document)

    _abort(stack, key)

    assert [r.get("ConsistentRead", False) for r in reads] == [False, True], reads


def test_the_skip_path_deletes_the_message(stack, monkeypatch):
    """An aborted document's message is acked at once, not left to redeliver."""
    key = "run-1612/acked.pdf"
    record = dict(_queue(stack, key), receiptHandle="handle-1612")
    processor = stack["processor"]
    acked = []
    monkeypatch.setattr(processor, "ack_message", acked.append)
    _abort_after_first_call(
        stack, monkeypatch, processor.document_service, "get_document", key
    )

    assert processor.process_message(record) == (True, record["messageId"])

    assert acked == ["handle-1612"]
    assert _executions(stack) == []


def test_the_claim_is_the_last_step_before_the_start(stack, monkeypatch):
    """The claim follows the configuration reads, so an abort during them is seen.

    The configuration manager is replaced with one that records its calls; the
    order of those, the claim and StartExecution is what is asserted.
    """
    processor = stack["processor"]
    order = []

    class _Manager:
        def __init__(self, table_name):
            pass

        def resolve_active_version(self):
            order.append("config")
            return "default"

        def resolve_published_revision(self, version):
            order.append("config")
            return None

        def get_merged_configuration(self, version):
            order.append("config")
            return None

    monkeypatch.setenv("CONFIG_TABLE", "config")
    monkeypatch.setattr(processor, "ConfigurationManager", _Manager)
    service = processor.document_service
    real_claim = service.claim_for_execution

    def claim(object_key):
        order.append("claim")
        return real_claim(object_key)

    monkeypatch.setattr(service, "claim_for_execution", claim)
    real_sfn = processor.sfn
    real_start = real_sfn.start_execution

    def start_execution(**kwargs):
        order.append("start")
        return real_start(**kwargs)

    monkeypatch.setattr(real_sfn, "start_execution", start_execution)
    record = _queue(stack, "run-1612/claim-last.pdf")

    assert processor.process_message(record) == (True, record["messageId"])

    assert "config" in order and order[-2:] == ["claim", "start"], order


@pytest.mark.parametrize(
    "after",
    [
        # The claim has been granted; StartExecution has not been called yet.
        pytest.param("claim", id="after-the-start-claim"),
        # The execution exists; the post-start tracking write has not been made.
        pytest.param("start", id="after-start-execution"),
    ],
)
def test_an_abort_landing_after_the_claim_stops_the_started_execution(
    stack, monkeypatch, after
):
    """The window #1614 left open: an abort after the claim, before the write.

    The abort reads the document as QUEUED (the claim does not change the
    status), finds nothing to stop, and writes ABORTED. The processor's write
    after StartExecution is refused for that, so it stops the execution it
    started and leaves the document ABORTED. The abort's report is then exact.
    """
    key = f"run-1612/after-{after}.pdf"
    record = _queue(stack, key)
    processor = stack["processor"]
    if after == "claim":
        fired = _abort_after_first_call(
            stack, monkeypatch, processor.document_service, "claim_for_execution", key
        )
    else:
        fired = _abort_after_first_call(
            stack, monkeypatch, processor.sfn, "start_execution", key
        )

    assert processor.process_message(record) == (True, record["messageId"])

    assert [r["abortedCount"] for r in fired] == [1], fired
    (execution,) = _executions(stack)
    assert execution["status"] == "ABORTED"
    assert _status(stack, key) == "ABORTED"
    # The execution owns its slot and the workflow tracker releases it on the
    # ABORTED event, so the processor does not hand it back.
    assert _active(stack) == 1


def test_a_failed_stop_still_leaves_the_document_aborted(stack, monkeypatch):
    """If the stop is refused, the abort's ABORTED is still not overwritten."""
    key = "run-1612/stop-refused.pdf"
    record = _queue(stack, key)
    processor = stack["processor"]
    _abort_after_first_call(stack, monkeypatch, processor.sfn, "start_execution", key)
    stops = []

    def stop_execution(**kwargs):
        stops.append(kwargs["executionArn"])
        raise RuntimeError("ThrottlingException")

    monkeypatch.setattr(processor.sfn, "stop_execution", stop_execution)

    assert processor.process_message(record) == (True, record["messageId"])

    (execution,) = _executions(stack)
    assert stops == [execution["executionArn"]]
    assert _status(stack, key) == "ABORTED"
    assert _active(stack) == 1


@pytest.mark.parametrize(
    "status",
    sorted(s.value for s in Status if s != Status.ABORTED),
)
def test_unless_status_writes_every_status_but_the_refused_one(stack, status):
    """``update_document(unless_status=ABORTED)`` against moto's expression engine."""
    service = stack["processor"].document_service
    key = f"run-1612/unless-{status.lower()}.pdf"
    _queue(stack, key, status=Status(status))
    document = Document(id=key, input_key=key, status=Status.RUNNING)

    service.update_document(document, unless_status=Status.ABORTED)

    assert _status(stack, key) == "RUNNING"


def test_unless_status_refuses_the_named_status_and_writes_nothing(stack):
    workflow = stack["workflow"]
    service = stack["processor"].document_service
    key = "run-1612/unless-aborted.pdf"
    _queue(stack, key, status=Status.ABORTED)
    document = Document(id=key, input_key=key, status=Status.RUNNING)

    # Matched by error code: the loaded handlers each hold their own import of
    # the client module, so the exception class is not one shared object here.
    with pytest.raises(Exception) as refused:
        service.update_document(document, unless_status=Status.ABORTED)

    assert getattr(refused.value, "error_code", None) == (
        workflow.CONDITIONAL_CHECK_FAILED
    ), refused.value
    assert _status(stack, key) == "ABORTED"


def test_unless_status_and_expected_status_are_exclusive(stack):
    service = stack["processor"].document_service
    document = Document(id="k", input_key="k", status=Status.RUNNING)
    with pytest.raises(ValueError):
        service.update_document(
            document, expected_status=Status.QUEUED, unless_status=Status.ABORTED
        )


def test_the_post_start_write_allows_a_missing_status_explicitly(stack):
    """The condition names a missing status as allowed, not left to ``<>``.

    moto evaluates ``#ObjectStatus <> :v`` as true for an item with no status,
    so dropping ``attribute_not_exists`` leaves every outcome here unchanged
    (measured: that mutant passed this whole file). Whether DynamoDB's own
    engine agrees is not something a moto test can establish, so what is
    asserted is the request: the post-start write sends the explicit guard.
    """
    key = "run-1612/condition-shape.pdf"
    record = _queue(stack, key, record=False)
    processor = stack["processor"]
    writes = []
    events = processor.document_service.client.table.meta.client.meta.events
    events.register(
        "provide-client-params.dynamodb.UpdateItem",
        lambda params, **_: writes.append(dict(params)),
    )

    assert processor.process_message(record) == (True, record["messageId"])

    conditions = [w.get("ConditionExpression") for w in writes]
    post_start = [c for c in conditions if c and ":UnlessObjectStatus" in c]
    assert post_start == [
        "attribute_not_exists(#ObjectStatus) OR #ObjectStatus <> :UnlessObjectStatus"
    ], conditions
    (aborted,) = [
        w["ExpressionAttributeValues"][":UnlessObjectStatus"]
        for w in writes
        if ":UnlessObjectStatus" in (w.get("ConditionExpression") or "")
    ]
    assert aborted == "ABORTED"
    assert _status(stack, key) == "RUNNING"


def test_an_abort_in_the_window_is_stopped_when_the_post_start_write_fails(
    stack, monkeypatch
):
    """A post-start write that fails for another reason (a throttle) is not a refusal.

    It says nothing about the abort, so the processor re-reads the document
    consistently and stops the execution if it is ABORTED.
    """
    key = "run-1612/write-throttled.pdf"
    record = _queue(stack, key)
    processor = stack["processor"]
    _abort_after_first_call(stack, monkeypatch, processor.sfn, "start_execution", key)
    service = processor.document_service
    real_update = service.update_document

    def update_document(document, **kwargs):
        if kwargs.get("unless_status") is not None:
            raise RuntimeError("ProvisionedThroughputExceededException")
        return real_update(document, **kwargs)

    monkeypatch.setattr(service, "update_document", update_document)

    assert processor.process_message(record) == (True, record["messageId"])

    (execution,) = _executions(stack)
    assert execution["status"] == "ABORTED"
    assert _status(stack, key) == "ABORTED"
    assert _active(stack) == 1
