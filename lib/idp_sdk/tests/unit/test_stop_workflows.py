# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``WorkflowStopper`` — the emergency brake for a running IDP deployment.

``idp_sdk._core.stop_workflows`` backs ``idp-cli stop-workflows``, which an
operator reaches for when a batch is running away: it purges the ingestion SQS
queue, stops every running Step Functions execution, and marks every document
still sitting in ``QUEUED`` as ``ABORTED`` in the tracking table. The whole value
of the command is the answer it prints afterwards, so the tests here are built
around two questions: **did the state actually change**, and **is the reported
count true**.

The suite is ``moto``-backed for all three services. Step Functions executions
are real fake executions started from a real fake state machine, so
``list_executions(statusFilter="RUNNING")`` is what decides whether a stop
worked; the DynamoDB tracking table is a real fake table, and every abort test
reads ``ObjectStatus`` back out of it rather than trusting the returned count.
That distinction is load-bearing for the abort path in particular, because it
goes through ``idp_common``'s ``DocumentDynamoDBService``, which builds an
``UpdateExpression`` — a mock accepts a malformed one and a real table does not.

``time.sleep`` is replaced everywhere, and the replacement records its argument:
the half-second pause between passes is the only thing keeping the retry loop
from hammering the API, so its absence is worth noticing.

Three defects are pinned rather than fixed, all in the "reported count is not
true" family:

* a listing failure is reported as success with nothing to do
  (``test_a_listing_failure_is_reported_as_a_successful_no_op``);
* a stop that reports success without taking effect is counted once per retry
  pass (``test_a_stop_that_never_takes_effect_is_counted_once_per_pass``);
* a queued document whose record cannot be fetched is counted as aborted
  (``test_a_document_that_cannot_be_fetched_is_counted_as_aborted``).
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from idp_common.models import Status
from idp_sdk._core.stop_workflows import WorkflowStopper

STACK_NAME = "IDP-test"
TABLE_NAME = "IDP-test-TrackingTable-1AB"
REGION = "us-east-1"

# A Wait state keeps a moto execution in RUNNING indefinitely, which is what a
# real IDP execution waiting on Textract or Bedrock looks like to this code.
WAITING_DEFINITION = json.dumps(
    {
        "StartAt": "Wait",
        "States": {"Wait": {"Type": "Wait", "Seconds": 3600, "End": True}},
    }
)


def build_stack(state_machine_arn: str, *, with_queue: bool = True) -> None:
    """Create the CloudFormation stack ``StackInfo`` will resolve.

    The state machine ARN has to exist before the template does, because the
    template carries it as a literal output — which is how the real deployment
    exposes it too.
    """
    resources: Dict[str, Any] = {
        "TrackingTable": {
            "Type": "AWS::DynamoDB::Table",
            "Properties": {
                "TableName": TABLE_NAME,
                "KeySchema": [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
                "AttributeDefinitions": [
                    {"AttributeName": "PK", "AttributeType": "S"},
                    {"AttributeName": "SK", "AttributeType": "S"},
                ],
                "BillingMode": "PAY_PER_REQUEST",
            },
        }
    }
    if with_queue:
        resources["DocumentQueue"] = {
            "Type": "AWS::SQS::Queue",
            "Properties": {"QueueName": "idp-test-document-queue"},
        }

    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "AWS GenAI IDP Accelerator - test fixture",
        "Resources": resources,
        "Outputs": {
            "S3InputBucketName": {"Value": "idp-test-inputbucket-1a2b"},
            "StateMachineArn": {"Value": state_machine_arn},
        },
    }
    boto3.client("cloudformation", region_name=REGION).create_stack(
        StackName=STACK_NAME, TemplateBody=json.dumps(template)
    )


def create_state_machine() -> str:
    iam = boto3.client("iam")
    role_arn = iam.create_role(
        RoleName="sfn-role", AssumeRolePolicyDocument=json.dumps({})
    )["Role"]["Arn"]
    return boto3.client("stepfunctions", region_name=REGION).create_state_machine(
        name="IDP-test-StateMachine", definition=WAITING_DEFINITION, roleArn=role_arn
    )["stateMachineArn"]


@pytest.fixture
def no_sleep(monkeypatch) -> List[float]:
    """Replace ``time.sleep`` and record what it was asked to wait for."""
    waits: List[float] = []

    def record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(time, "sleep", record)
    return waits


@pytest.fixture
def stopper(aws_credentials, no_sleep):
    """A ``WorkflowStopper`` wired to moto's SQS, Step Functions and DynamoDB."""
    with mock_aws():
        state_machine_arn = create_state_machine()
        build_stack(state_machine_arn)
        instance = WorkflowStopper(stack_name=STACK_NAME, region=REGION)
        # Everything the stopper needs must have resolved from the stack; a
        # silent `None` here would make several tests below pass vacuously
        # through the "not found in stack outputs" guards.
        assert instance.state_machine_arn == state_machine_arn
        assert instance.documents_table == TABLE_NAME
        assert instance.queue_url and instance.queue_url.endswith(
            "idp-test-document-queue"
        )
        yield instance


def start_executions(stopper: WorkflowStopper, count: int) -> List[str]:
    sfn = boto3.client("stepfunctions", region_name=REGION)
    return [
        sfn.start_execution(
            stateMachineArn=stopper.state_machine_arn, name=f"execution-{index}"
        )["executionArn"]
        for index in range(count)
    ]


def running_execution_arns(stopper: WorkflowStopper) -> set:
    sfn = boto3.client("stepfunctions", region_name=REGION)
    return {
        execution["executionArn"]
        for page in sfn.get_paginator("list_executions").paginate(
            stateMachineArn=stopper.state_machine_arn, statusFilter="RUNNING"
        )
        for execution in page["executions"]
    }


def execution_statuses(stopper: WorkflowStopper) -> Dict[str, str]:
    sfn = boto3.client("stepfunctions", region_name=REGION)
    return {
        execution["executionArn"]: execution["status"]
        for execution in sfn.list_executions(stateMachineArn=stopper.state_machine_arn)[
            "executions"
        ]
    }


class ScriptedSfn:
    """A Step Functions client that delegates to moto with injected faults.

    Every fault below models something the real service does — a throttle on
    ``StopExecution``, an execution that has already finished, a listing that
    fails — while leaving the rest of the conversation with the real fake intact,
    so the loop's own arithmetic is measured against a service that really
    changes state.
    """

    def __init__(
        self,
        real: Any,
        *,
        stop_error: Optional[Dict[str, Exception]] = None,
        stop_is_a_no_op: bool = False,
        fail_listing_after: Optional[int] = None,
    ):
        self._real = real
        self._stop_error = stop_error or {}
        self._stop_is_a_no_op = stop_is_a_no_op
        self._fail_listing_after = fail_listing_after
        self.listing_calls = 0
        self.stop_calls: List[str] = []

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    def stop_execution(self, executionArn: str, error: str, cause: str):
        self.stop_calls.append(executionArn)
        if executionArn in self._stop_error:
            raise self._stop_error[executionArn]
        if self._stop_is_a_no_op:
            return {}
        return self._real.stop_execution(
            executionArn=executionArn, error=error, cause=cause
        )

    def get_paginator(self, operation_name: str):
        if operation_name != "list_executions":
            return self._real.get_paginator(operation_name)
        return _ScriptedPaginator(self)

    def _paginate_executions(self, **kwargs):
        self.listing_calls += 1
        if (
            self._fail_listing_after is not None
            and self.listing_calls > self._fail_listing_after
        ):
            raise RuntimeError("ThrottlingException on ListExecutions")
        return self._real.get_paginator("list_executions").paginate(**kwargs)


class _ScriptedPaginator:
    def __init__(self, client: ScriptedSfn):
        self._client = client

    def paginate(self, **kwargs):
        return self._client._paginate_executions(**kwargs)


# ---------------------------------------------------------------------------
# purge_queue
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_purging_the_queue_removes_every_pending_message(stopper):
    """The queue must actually be empty afterwards, not merely "purged".

    ``PurgeQueue`` is the step that stops new work from being picked up, so a
    call that silently addressed the wrong queue would leave the runaway batch
    draining. The message count is read back from the fake queue.
    """
    sqs = boto3.client("sqs", region_name=REGION)
    for index in range(5):
        sqs.send_message(QueueUrl=stopper.queue_url, MessageBody=f"doc-{index}")
    assert (
        sqs.get_queue_attributes(
            QueueUrl=stopper.queue_url, AttributeNames=["ApproximateNumberOfMessages"]
        )["Attributes"]["ApproximateNumberOfMessages"]
        == "5"
    )

    result = stopper.purge_queue()

    assert result == {"success": True, "queue_url": stopper.queue_url}
    assert (
        sqs.get_queue_attributes(
            QueueUrl=stopper.queue_url, AttributeNames=["ApproximateNumberOfMessages"]
        )["Attributes"]["ApproximateNumberOfMessages"]
        == "0"
    )


@pytest.mark.unit
def test_purging_without_a_resolved_queue_url_is_an_error_not_a_crash(stopper):
    """A stack whose outputs did not yield a queue URL must report, not raise.

    ``stop_all`` calls this unconditionally, so an exception here would take the
    execution-stopping step down with it — and stopping executions is the part
    that matters most.
    """
    stopper.queue_url = None

    assert stopper.purge_queue() == {
        "success": False,
        "error": "SQS queue URL not found in stack outputs",
    }


@pytest.mark.unit
def test_a_purge_that_the_service_refuses_is_reported_as_a_failure(stopper):
    """A queue that no longer exists is reported with the service's own message."""
    stopper.queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/deleted-queue"

    result = stopper.purge_queue()

    assert result["success"] is False
    assert "NonExistentQueue" in result["error"] or "does not exist" in result["error"]


# ---------------------------------------------------------------------------
# count_running_executions
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_running_executions_are_counted_across_pages(stopper):
    """The count comes from a paginator, so it must not stop at one page.

    ``stop_executions`` uses this number both to decide whether there is work and
    to decide whether the work succeeded, so an undercount ends the command
    early while executions are still running.
    """
    start_executions(stopper, 3)
    assert stopper.count_running_executions() == 3

    sfn = boto3.client("stepfunctions", region_name=REGION)
    sfn.stop_execution(
        executionArn=sorted(running_execution_arns(stopper))[0],
        error="UserAborted",
        cause="test",
    )
    assert stopper.count_running_executions() == 2


@pytest.mark.unit
def test_a_count_that_cannot_be_taken_reports_zero(stopper, monkeypatch):
    """Defect-adjacent: a failed listing is indistinguishable from an empty one.

    ``count_running_executions`` swallows the exception and returns 0. On its own
    that is a defensible choice for a counter, but ``stop_executions`` treats 0
    as "nothing to do" — see
    ``test_a_listing_failure_is_reported_as_a_successful_no_op`` for the
    consequence. Pinned here as the mechanism.
    """
    start_executions(stopper, 2)
    monkeypatch.setattr(stopper, "sfn", ScriptedSfn(stopper.sfn, fail_listing_after=0))

    assert stopper.count_running_executions() == 0


# ---------------------------------------------------------------------------
# stop_executions
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stopping_with_nothing_running_reports_success_and_does_not_sleep(
    stopper, no_sleep
):
    result = stopper.stop_executions()

    assert result == {
        "success": True,
        "total_stopped": 0,
        "total_failed": 0,
        "remaining": 0,
    }
    assert no_sleep == []


@pytest.mark.unit
def test_every_running_execution_is_aborted_with_the_documented_error_and_cause(
    stopper, no_sleep
):
    """The executions end up ABORTED in the fake, and the request says why.

    ``error`` and ``cause`` are what an operator sees in the Step Functions
    console months later when asking why a batch stopped, so the two literal
    strings are asserted from the outgoing request parameters. moto does not
    echo them back on ``describe_execution``, which is why they are captured at
    the client rather than read from the fake's state; the ``ABORTED`` status is
    read from the fake.
    """
    arns = start_executions(stopper, 4)
    sent: List[Dict[str, Any]] = []
    stopper.sfn.meta.events.register(
        "provide-client-params.sfn.StopExecution",
        lambda params=None, **_kwargs: sent.append(dict(params or {})),
    )

    result = stopper.stop_executions()

    assert execution_statuses(stopper) == {arn: "ABORTED" for arn in arns}
    assert result["success"] is True
    assert result["total_stopped"] == 4
    assert result["total_failed"] == 0
    assert result["remaining"] == 0
    assert {entry["executionArn"] for entry in sent} == set(arns)
    assert {entry["error"] for entry in sent} == {"UserAborted"}
    assert {entry["cause"] for entry in sent} == {
        "Stopped by idp-cli stop-workflows command"
    }
    # One pause after the batch, before the verification pass.
    assert no_sleep == [0.5]


@pytest.mark.unit
def test_stopping_reports_the_rate_it_achieved(stopper, no_sleep):
    """``elapsed_seconds`` and ``rate_per_minute`` must be present and coherent.

    They are printed by the CLI. The rate is derived by division, so the guard
    against a zero elapsed time is the part worth exercising — a wall-clock of
    exactly 0.0 would otherwise raise ``ZeroDivisionError`` and lose the result
    of a successful stop.
    """
    start_executions(stopper, 2)

    result = stopper.stop_executions()

    assert result["elapsed_seconds"] >= 0
    assert result["rate_per_minute"] >= 0


@pytest.mark.unit
def test_stopping_without_a_state_machine_arn_is_an_error_not_a_crash(stopper):
    stopper.state_machine_arn = None

    assert stopper.stop_executions() == {
        "success": False,
        "error": "State machine ARN not found in stack outputs",
    }


@pytest.mark.unit
def test_an_execution_that_has_already_gone_counts_as_stopped(stopper, no_sleep):
    """``ExecutionDoesNotExist`` means somebody else finished the job.

    The source matches the *error code* rather than catching
    ``self.sfn.exceptions.ExecutionDoesNotExist``, and its comment explains why:
    a misspelled exception attribute raises ``AttributeError`` from inside the
    ``except`` clause and escapes every handler below it. This test feeds the
    real ``ClientError`` moto raises for a non-existent execution ARN, injected
    into the listing so the stop is attempted against it, and asserts it lands in
    ``total_stopped`` rather than ``total_failed``.
    """
    real_arns = start_executions(stopper, 2)
    ghost_arn = real_arns[0].rsplit(":", 1)[0] + ":already-finished"
    real_sfn = stopper.sfn

    class ListingWithAGhost(ScriptedSfn):
        def _paginate_executions(self, **kwargs):
            pages = list(super()._paginate_executions(**kwargs))
            if pages and pages[0]["executions"]:
                pages[0] = dict(pages[0])
                pages[0]["executions"] = list(pages[0]["executions"]) + [
                    {"executionArn": ghost_arn, "status": "RUNNING"}
                ]
            return pages

    stopper.sfn = ListingWithAGhost(real_sfn)

    result = stopper.stop_executions()

    assert ghost_arn in stopper.sfn.stop_calls
    assert result["total_failed"] == 0
    assert result["total_stopped"] >= 3
    assert execution_statuses(stopper) == {arn: "ABORTED" for arn in real_arns}


@pytest.mark.unit
def test_a_stop_the_service_refuses_is_counted_as_a_failure(stopper, no_sleep):
    """A throttled stop must be reported, and the command must not claim success.

    ``remaining`` is what makes the difference visible: one execution is still
    running at the end, so ``success`` has to be ``False`` however many stops
    succeeded.
    """
    arns = start_executions(stopper, 3)
    throttle = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "StopExecution",
    )
    stopper.sfn = ScriptedSfn(stopper.sfn, stop_error={arns[0]: throttle})

    result = stopper.stop_executions(max_retries=1)

    assert result["success"] is False
    assert result["total_failed"] == 1
    assert result["total_stopped"] == 2
    assert result["remaining"] == 1
    assert execution_statuses(stopper)[arns[0]] == "RUNNING"


@pytest.mark.unit
def test_a_stop_that_raises_something_other_than_a_client_error_is_a_failure(
    stopper, no_sleep
):
    """A non-``ClientError`` must be caught too, or the thread pool swallows it.

    ``as_completed`` re-raises whatever the worker raised at ``future.result()``,
    which is *outside* the worker's own ``try``. So an error that is not a
    ``ClientError`` — a connection pool exhaustion, a JSON decode failure in
    botocore — would escape ``stop_executions`` entirely and abort the command
    mid-batch, leaving the remaining executions running and reporting nothing at
    all. The broad handler is what prevents that, and this test is what shows it
    is doing so: the result is returned, the failure is counted, and the other
    execution is still stopped.
    """
    arns = start_executions(stopper, 2)
    stopper.sfn = ScriptedSfn(
        stopper.sfn, stop_error={arns[0]: RuntimeError("connection pool is full")}
    )

    result = stopper.stop_executions(max_retries=1)

    assert result["total_failed"] == 1
    assert result["total_stopped"] == 1
    assert result["remaining"] == 1
    assert result["success"] is False
    assert execution_statuses(stopper) == {arns[0]: "RUNNING", arns[1]: "ABORTED"}


@pytest.mark.unit
def test_a_retry_pass_picks_up_an_execution_the_first_pass_missed(stopper, no_sleep):
    """The retry loop exists because a stop can fail transiently.

    The first pass is throttled for one execution; the second pass lists it again
    and succeeds. Nothing is left running, so the command reports success — and
    the pause between passes happened.
    """
    arns = start_executions(stopper, 2)
    throttle = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "StopExecution",
    )

    class FlakyOnce(ScriptedSfn):
        def stop_execution(self, executionArn: str, error: str, cause: str):
            if executionArn == arns[0] and executionArn not in self.stop_calls:
                self.stop_calls.append(executionArn)
                raise throttle
            return super().stop_execution(
                executionArn=executionArn, error=error, cause=cause
            )

    stopper.sfn = FlakyOnce(stopper.sfn)

    result = stopper.stop_executions()

    assert execution_statuses(stopper) == {arn: "ABORTED" for arn in arns}
    assert result["success"] is True
    assert result["remaining"] == 0
    assert result["total_failed"] == 1
    assert len(no_sleep) >= 2


@pytest.mark.unit
def test_a_stop_that_never_takes_effect_is_counted_once_per_pass(stopper, no_sleep):
    """Defect: ``total_stopped`` counts successes per pass, not distinct executions.

    ``stop_workflows.py:191-197`` increments ``total_stopped`` for every future
    that returns ``True``, and the enclosing ``while`` loop re-lists and re-stops
    whatever is still running. A ``StopExecution`` that returns 200 without
    taking effect — which is what an execution in a terminal-but-not-yet-visible
    state looks like — is therefore counted again on each of the five passes.

    Here one execution yields ``total_stopped == 3`` over three passes while
    ``remaining`` is still 1. Observable consequence: the CLI prints "3
    executions stopped" for one execution that is still running. ``success`` is
    correctly ``False``, which is what keeps this a reporting defect rather than a
    silent failure — but an operator reading the count would conclude the command
    worked and the verification was wrong.
    """
    arns = start_executions(stopper, 1)
    stopper.sfn = ScriptedSfn(stopper.sfn, stop_is_a_no_op=True)

    result = stopper.stop_executions(max_retries=3)

    assert result["total_stopped"] == 3
    assert result["total_failed"] == 0
    assert result["remaining"] == 1
    assert result["success"] is False
    assert execution_statuses(stopper) == {arns[0]: "RUNNING"}
    # One pause per pass, because each pass "stopped" something.
    assert no_sleep == [0.5, 0.5, 0.5]


@pytest.mark.unit
def test_a_listing_failure_is_reported_as_a_successful_no_op(stopper, no_sleep):
    """Defect: a Step Functions outage is reported as "nothing to stop".

    ``count_running_executions`` (``stop_workflows.py:78-89``) swallows every
    exception and returns 0, and ``stop_executions`` (``:108-118``) treats 0 as
    "no running executions to stop" and returns ``success: True``. So when
    ``ListExecutions`` fails — throttling, a permissions gap, a region outage —
    the operator is told there was nothing to stop while the runaway batch keeps
    running.

    This is the most consequential of the three reporting defects: the command's
    purpose is to stop a batch that is costing money, and this failure mode tells
    the operator they have succeeded. A ``remaining`` value the caller could
    check would not help, because it is 0 for the same reason.
    """
    arns = start_executions(stopper, 2)
    stopper.sfn = ScriptedSfn(stopper.sfn, fail_listing_after=0)

    result = stopper.stop_executions()

    assert result == {
        "success": True,
        "total_stopped": 0,
        "total_failed": 0,
        "remaining": 0,
    }
    assert execution_statuses(stopper) == {arn: "RUNNING" for arn in arns}
    assert stopper.sfn.stop_calls == []


@pytest.mark.unit
def test_a_listing_failure_midway_abandons_the_remaining_passes(stopper, no_sleep):
    """A listing that fails *after* the initial count breaks out of the loop.

    The initial count succeeds, so the command knows there is work; the retry
    loop's own listing then fails and the loop breaks rather than spinning
    through all five passes. The final verification count fails too and reports
    0, so ``success`` comes out ``True`` again — the same defect as above,
    reached by the other route, and asserted here so that fixing one does not
    leave the other.
    """
    arns = start_executions(stopper, 2)
    stopper.sfn = ScriptedSfn(stopper.sfn, fail_listing_after=1)

    result = stopper.stop_executions()

    assert result["total_stopped"] == 0
    assert result["success"] is True
    assert execution_statuses(stopper) == {arn: "RUNNING" for arn in arns}
    assert no_sleep == []


# ---------------------------------------------------------------------------
# abort_queued_documents
# ---------------------------------------------------------------------------


def tracking_table():
    return boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)


def seed_document(object_key: str, status: str, *, stored_key: Optional[str] = None):
    """Write one tracking-table row in the shape ``DocumentDynamoDBService`` uses.

    ``stored_key`` decouples the item's partition key from its ``ObjectKey``
    attribute, which is how the "cannot be fetched" case below is built.
    """
    tracking_table().put_item(
        Item={
            "PK": f"doc#{stored_key or object_key}",
            "SK": "none",
            "ObjectKey": object_key,
            "ObjectStatus": status,
            "QueuedTime": "2026-01-01T00:00:00.000Z",
            "InitialEventTime": "2026-01-01T00:00:00.000Z",
            "ItemType": "document",
        }
    )


def stored_status(object_key: str) -> Optional[str]:
    item = tracking_table().get_item(Key={"PK": f"doc#{object_key}", "SK": "none"})
    return item.get("Item", {}).get("ObjectStatus")


@pytest.mark.unit
def test_queued_documents_become_aborted_in_the_table(stopper):
    """The status must change in DynamoDB, not just in the returned count.

    This is the path that goes through ``DocumentDynamoDBService.update_document``
    and therefore through a generated ``UpdateExpression``; a mock would accept a
    malformed expression and report two documents aborted. Reading
    ``ObjectStatus`` back from the fake table is the only assertion that
    distinguishes the two.

    The ``COMPLETED`` document must be untouched: aborting a document that
    already finished would lose its result in the UI.
    """
    seed_document("in/queued-a.pdf", Status.QUEUED.value)
    seed_document("in/queued-b.pdf", Status.QUEUED.value)
    seed_document("in/finished.pdf", Status.COMPLETED.value)

    result = stopper.abort_queued_documents()

    assert result == {
        "success": True,
        "documents_aborted": 2,
        "documents_failed": 0,
    }
    assert stored_status("in/queued-a.pdf") == Status.ABORTED.value
    assert stored_status("in/queued-b.pdf") == Status.ABORTED.value
    assert stored_status("in/finished.pdf") == Status.COMPLETED.value


@pytest.mark.unit
def test_no_queued_documents_is_reported_as_success_with_a_zero(stopper):
    seed_document("in/finished.pdf", Status.COMPLETED.value)

    assert stopper.abort_queued_documents() == {
        "success": True,
        "documents_aborted": 0,
    }
    assert stored_status("in/finished.pdf") == Status.COMPLETED.value


@pytest.mark.unit
def test_a_row_without_an_object_key_is_counted_as_a_failure(stopper):
    """A malformed row must be counted as failed, not skipped silently.

    The tracking table also holds list-partition rows that carry no
    ``ObjectKey``; those cannot be aborted, and the count has to say so or the
    totals will not add up for an operator comparing them against the table.
    """
    seed_document("in/queued.pdf", Status.QUEUED.value)
    tracking_table().put_item(
        Item={
            "PK": "list#2026-01-01",
            "SK": "queued#01",
            "ObjectStatus": Status.QUEUED.value,
        }
    )

    result = stopper.abort_queued_documents()

    assert result == {
        "success": True,
        "documents_aborted": 1,
        "documents_failed": 1,
    }
    assert stored_status("in/queued.pdf") == Status.ABORTED.value


@pytest.mark.unit
def test_a_document_that_cannot_be_fetched_is_counted_as_aborted(stopper):
    """Defect: the ``else`` arm counts an un-fetchable document as aborted.

    ``stop_workflows.py:289-297`` fetches the document by ``ObjectKey`` and, when
    the fetch returns ``None`` or a status other than ``QUEUED``, increments
    ``aborted_count`` anyway on the assumption that the document "already changed
    status". A row whose ``ObjectKey`` does not match its own partition key — the
    shape below, and the shape of any row written with an inconsistent key — is
    therefore reported as aborted while remaining ``QUEUED`` in the table
    forever.

    Observable consequence: the command reports every queued document aborted
    and the UI still shows them queued; nothing in the result distinguishes the
    two cases. Counting those separately, or re-reading the row, is the fix; this
    test pins the current arithmetic.
    """
    seed_document("in/mismatched.pdf", Status.QUEUED.value, stored_key="other/key.pdf")

    result = stopper.abort_queued_documents()

    assert result == {
        "success": True,
        "documents_aborted": 1,
        "documents_failed": 0,
    }
    assert stored_status("other/key.pdf") == Status.QUEUED.value
    assert stored_status("in/mismatched.pdf") is None


@pytest.mark.unit
def test_one_document_that_raises_does_not_stop_the_rest(stopper, monkeypatch):
    """A single bad row must cost one failure, not the whole abort pass.

    The per-document ``try`` is inside the loop, so a document whose record
    cannot be read — a corrupt attribute, a conditional-check failure on write —
    is counted and skipped. The assertion that matters is the *other* document's
    stored status: if the handler were outside the loop, everything after the
    first bad row would stay ``QUEUED`` while the command reported success.
    """
    from idp_common.dynamodb.service import DocumentDynamoDBService

    seed_document("in/poison.pdf", Status.QUEUED.value)
    seed_document("in/healthy.pdf", Status.QUEUED.value)

    real_get_document = DocumentDynamoDBService.get_document

    def selective_get(self, object_key: str):
        if object_key == "in/poison.pdf":
            raise RuntimeError("corrupt item")
        return real_get_document(self, object_key)

    monkeypatch.setattr(DocumentDynamoDBService, "get_document", selective_get)

    result = stopper.abort_queued_documents()

    assert result == {
        "success": True,
        "documents_aborted": 1,
        "documents_failed": 1,
    }
    assert stored_status("in/healthy.pdf") == Status.ABORTED.value
    assert stored_status("in/poison.pdf") == Status.QUEUED.value


@pytest.mark.unit
def test_every_page_of_queued_documents_is_aborted(stopper, monkeypatch):
    """The scan is paginated, and the continuation key must be threaded through.

    A million-document tracking table returns ``LastEvaluatedKey`` and one page
    of results; a loop that ignored it would abort the first page and report that
    number as the total. A real fake cannot be made to paginate without 1 MB of
    rows, so ``DynamoDBClient.scan`` is wrapped to split its own real result into
    two pages. The wrapper asserts the second call arrived with the key from the
    first — the argument, not merely the fact of a second call — and the table is
    still read back afterwards for the actual statuses.
    """
    from idp_common.dynamodb.client import DynamoDBClient

    for index in range(4):
        seed_document(f"in/queued-{index}.pdf", Status.QUEUED.value)

    real_scan = DynamoDBClient.scan
    start_keys: List[Any] = []

    def paged_scan(self, **kwargs):
        start_key = kwargs.get("exclusive_start_key")
        start_keys.append(start_key)
        response = real_scan(self, **kwargs)
        items = sorted(response.get("Items", []), key=lambda item: item["PK"])
        if start_key is None:
            first, rest = items[:2], items[2:]
            out = {"Items": first}
            if rest:
                out["LastEvaluatedKey"] = {"PK": first[-1]["PK"], "SK": first[-1]["SK"]}
            return out
        resume_after = start_key["PK"]
        return {"Items": [item for item in items if item["PK"] > resume_after]}

    monkeypatch.setattr(DynamoDBClient, "scan", paged_scan)

    result = stopper.abort_queued_documents()

    assert len(start_keys) == 2
    assert start_keys[0] is None
    assert start_keys[1] == {"PK": "doc#in/queued-1.pdf", "SK": "none"}
    assert result["documents_aborted"] == 4
    for index in range(4):
        assert stored_status(f"in/queued-{index}.pdf") == Status.ABORTED.value


@pytest.mark.unit
def test_aborting_without_a_resolved_table_is_an_error_not_a_crash(stopper):
    stopper.documents_table = None

    assert stopper.abort_queued_documents() == {
        "success": False,
        "error": "DocumentsTable not found in stack resources",
    }


@pytest.mark.unit
def test_a_table_that_cannot_be_scanned_is_reported_as_a_failure(stopper):
    """A wrong or deleted table name must come back as an error string.

    The guard above only catches an *absent* table name. A name that is present
    but wrong reaches DynamoDB and raises, and the operator needs the message
    rather than a traceback, because ``stop_all`` calls this last and the earlier
    steps have already run.
    """
    stopper.documents_table = "IDP-test-TrackingTable-does-not-exist"

    result = stopper.abort_queued_documents()

    assert result["success"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# stop_all
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stop_all_runs_the_three_steps_and_returns_each_result(stopper, monkeypatch):
    """All three keys are always present, so a caller can index them safely."""
    calls: List[str] = []

    def recorder(name: str, value: Any):
        def record(*_args, **_kwargs):
            calls.append(name)
            return value

        return record

    monkeypatch.setattr(stopper, "purge_queue", recorder("purge", {"success": True}))
    monkeypatch.setattr(
        stopper, "stop_executions", recorder("stop", {"total_stopped": 7})
    )
    monkeypatch.setattr(
        stopper, "abort_queued_documents", recorder("abort", {"documents_aborted": 3})
    )

    results = stopper.stop_all()

    assert calls == ["purge", "stop", "abort"]
    assert results == {
        "queue_purge": {"success": True},
        "executions_stopped": {"total_stopped": 7},
        "documents_aborted": {"documents_aborted": 3},
    }


@pytest.mark.unit
def test_skipping_the_purge_also_skips_the_document_abort(stopper, monkeypatch):
    """The abort step is gated on ``skip_purge``, not on a flag of its own.

    ``--skip-purge`` therefore leaves every ``QUEUED`` document sitting at
    ``QUEUED`` even though the executions were stopped, and the returned
    ``documents_aborted`` is ``None`` rather than a count. That coupling is not
    obviously intended — the comment in the source says "always abort queued
    documents after purge" — so it is pinned here: an operator who skips the
    purge because the queue is already empty silently also skips the database
    cleanup, and the tracking table keeps showing work that will never run.
    """
    calls: List[str] = []
    for name in ("purge_queue", "stop_executions", "abort_queued_documents"):
        monkeypatch.setattr(
            stopper,
            name,
            lambda *a, _n=name, **k: (calls.append(_n), {"ok": True})[1],
        )

    results = stopper.stop_all(skip_purge=True)

    assert calls == ["stop_executions"]
    assert results == {
        "queue_purge": None,
        "executions_stopped": {"ok": True},
        "documents_aborted": None,
    }


@pytest.mark.unit
def test_skipping_the_stop_still_purges_and_aborts(stopper, monkeypatch):
    calls: List[str] = []
    for name in ("purge_queue", "stop_executions", "abort_queued_documents"):
        monkeypatch.setattr(
            stopper,
            name,
            lambda *a, _n=name, **k: (calls.append(_n), {"ok": True})[1],
        )

    results = stopper.stop_all(skip_stop=True)

    assert calls == ["purge_queue", "abort_queued_documents"]
    assert results["executions_stopped"] is None
    assert results["queue_purge"] == {"ok": True}
    assert results["documents_aborted"] == {"ok": True}


@pytest.mark.unit
def test_stop_all_end_to_end_changes_all_three_kinds_of_state(stopper, no_sleep):
    """One unmocked run: queue drained, executions aborted, documents aborted.

    The per-step tests each hold the other two still. This one lets the real
    sequence run against the three fakes together, because the ordering is part
    of the contract — purging before stopping is what keeps the queue processor
    from starting a replacement execution for a document it had already dequeued.
    """
    sqs = boto3.client("sqs", region_name=REGION)
    sqs.send_message(QueueUrl=stopper.queue_url, MessageBody="pending")
    arns = start_executions(stopper, 2)
    seed_document("in/queued.pdf", Status.QUEUED.value)

    results = stopper.stop_all()

    assert results["queue_purge"]["success"] is True
    assert (
        sqs.get_queue_attributes(
            QueueUrl=stopper.queue_url, AttributeNames=["ApproximateNumberOfMessages"]
        )["Attributes"]["ApproximateNumberOfMessages"]
        == "0"
    )
    assert results["executions_stopped"]["success"] is True
    assert execution_statuses(stopper) == {arn: "ABORTED" for arn in arns}
    assert results["documents_aborted"] == {
        "success": True,
        "documents_aborted": 1,
        "documents_failed": 0,
    }
    assert stored_status("in/queued.pdf") == Status.ABORTED.value


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_construction_fails_when_the_stack_has_no_document_queue(
    aws_credentials, no_sleep
):
    """``StackInfo`` raises when ``DocumentQueue`` is missing, and that propagates.

    ``WorkflowStopper.__init__`` resolves stack resources eagerly, so a stack
    that is not an IDP stack — or one whose queue resource has been renamed —
    fails at construction rather than producing a stopper with a ``None`` queue
    URL. That is the better failure: the guards inside ``purge_queue`` and
    ``stop_executions`` exist for outputs that resolve to an empty string, not
    for a stack that cannot be read at all.
    """
    with mock_aws():
        build_stack(create_state_machine(), with_queue=False)

        with pytest.raises(ValueError, match="DocumentQueue not found"):
            WorkflowStopper(stack_name=STACK_NAME, region=REGION)


@pytest.mark.unit
def test_the_clients_are_built_in_the_requested_region(stopper):
    """A stopper pointed at one region must not purge another region's queue."""
    assert stopper.sqs.meta.region_name == REGION
    assert stopper.sfn.meta.region_name == REGION
    assert stopper.region == REGION
    assert stopper.stack_name == STACK_NAME
