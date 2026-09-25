# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``idp_sdk._core.test_studio_processor.TestStudioProcessor``.

The processor is the SDK's client for Test Studio. It has no API of its own: it
locates two resolver Lambdas in a **nested** CloudFormation stack and then calls
them with an AppSync-shaped envelope —
``{"info": {"fieldName": <operation>}, "arguments": {...}}`` — parsing the
returned JSON payload itself. So everything that can go wrong is either a
*lookup* (wrong nested stack, wrong output key) or a *payload* (wrong
``fieldName``, wrong argument name), and both fail the same way: the resolver
returns something plausible, or nothing, and the SDK reports a processing error
that names none of it.

That shaped the tests in two ways.

**The Lambda calls go through ``botocore.stub.Stubber``, not a ``MagicMock``.**
A `MagicMock` accepts `FunctionName=None` and any `Payload` whatsoever, so an
assertion that it "was called" proves nothing about the envelope. Each stub here
declares the exact `FunctionName`, `InvocationType` and serialised `Payload` the
processor must send; a changed ``fieldName``, a renamed argument or a dropped
``InvocationType`` fails the call itself rather than an assertion after it. moto
cannot stand in here — invoking a moto Lambda needs Docker and a real
deployment package — so this is the "monkeypatch narrowly but still assert on
the request payload and the parsed result" case.

**The nested-stack lookup is tested through both names.** The resolver ARNs live
in a nested stack whose logical id is ``APIRESOLVERSTACK`` on current templates
and was ``APPSYNCSTACK`` before AppSync was removed, and the fallback from one to
the other is the only thing keeping the SDK working against a stack deployed
from an older template. It is a `try`/`except ValueError` around a call that
raises `ValueError` for both "no such nested stack" and "no such output", so the
fallback is easy to break silently in either direction.

One structural wart is pinned rather than fixed, and it is why several assertions
below match a doubled message: in ``_get_resolver_function_arn``,
``get_test_run_status``, ``get_test_result`` and ``compare_test_runs`` the
"expected" error is raised *inside* a `try` whose `except Exception` re-wraps it,
so the final message contains its own prefix twice. The exception *type* is
unchanged, so this is cosmetic — but a test written against the single-prefix
message would be wrong about what a caller sees.
"""

from __future__ import annotations

import io
import json
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from botocore.stub import Stubber

from idp_sdk._core.test_studio_processor import configuration_differences
from idp_sdk.exceptions import IDPProcessingError, IDPResourceNotFoundError

pytestmark = pytest.mark.unit

REGION = "us-east-1"
STACK = "idp-stack"
NESTED_PHYSICAL_ID = (
    f"arn:aws:cloudformation:{REGION}:123456789012:stack/{STACK}-APIRESOLVERSTACK-A1/g"
)
#: ``get_nested_stack_output`` takes the name out of the physical id with
#: ``.split("/")[1]``, so this is the name the follow-up DescribeStacks uses.
NESTED_STACK_NAME = f"{STACK}-APIRESOLVERSTACK-A1"
RESOLVER_ARN = f"arn:aws:lambda:{REGION}:123456789012:function:{STACK}-TestResults"
ABORT_ARN = f"arn:aws:lambda:{REGION}:123456789012:function:{STACK}-AbortTestRuns"

_TS = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures and stub builders
# ---------------------------------------------------------------------------


def _make_processor(region: str = REGION):
    """A processor with real (but stubbable) clients and no AWS traffic.

    ``StackInfo.__init__`` only constructs clients, so nothing here reaches AWS —
    which is why the constructor can be exercised directly instead of bypassed
    with ``__new__``.
    """
    from idp_sdk._core.test_studio_processor import TestStudioProcessor

    return TestStudioProcessor(stack_name=STACK, region=region)


@contextmanager
def _stubs(proc):
    """Stubbers over the processor's CloudFormation and Lambda clients.

    Both are asserted to have no pending responses on exit, so a test that
    queues a call the processor never makes fails rather than passing quietly.
    """
    cfn = Stubber(proc.stack_info.cfn)
    lam = Stubber(proc.lambda_client)
    cfn.activate()
    lam.activate()
    try:
        yield cfn, lam
        cfn.assert_no_pending_responses()
        lam.assert_no_pending_responses()
    finally:
        lam.deactivate()
        cfn.deactivate()


def _nested_stack_resources(logical_id: str = "APIRESOLVERSTACK") -> dict:
    """A DescribeStackResources response containing one nested stack."""
    return {
        "StackResources": [
            {
                "StackName": STACK,
                "LogicalResourceId": "DocumentQueue",
                "PhysicalResourceId": "https://sqs.example/q",
                "ResourceType": "AWS::SQS::Queue",
                "Timestamp": _TS,
                "ResourceStatus": "CREATE_COMPLETE",
            },
            {
                "StackName": STACK,
                "LogicalResourceId": logical_id,
                "PhysicalResourceId": NESTED_PHYSICAL_ID,
                "ResourceType": "AWS::CloudFormation::Stack",
                "Timestamp": _TS,
                "ResourceStatus": "CREATE_COMPLETE",
            },
        ]
    }


def _no_nested_stacks() -> dict:
    """A stack whose only resource is not a nested stack."""
    return {
        "StackResources": [
            {
                "StackName": STACK,
                "LogicalResourceId": "DocumentQueue",
                "PhysicalResourceId": "https://sqs.example/q",
                "ResourceType": "AWS::SQS::Queue",
                "Timestamp": _TS,
                "ResourceStatus": "CREATE_COMPLETE",
            }
        ]
    }


def _nested_outputs(**outputs: str) -> dict:
    """A DescribeStacks response for the nested stack, carrying ``outputs``."""
    return {
        "Stacks": [
            {
                "StackName": NESTED_STACK_NAME,
                "CreationTime": _TS,
                "StackStatus": "CREATE_COMPLETE",
                "Outputs": [
                    {"OutputKey": key, "OutputValue": value}
                    for key, value in outputs.items()
                ],
            }
        ]
    }


def _queue_resolver_lookup(
    cfn, arn: str = RESOLVER_ARN, key: str | None = None
) -> None:
    """Queue the two CloudFormation calls that resolve one nested-stack output."""
    cfn.add_response(
        "describe_stack_resources",
        _nested_stack_resources(),
        {"StackName": STACK},
    )
    cfn.add_response(
        "describe_stacks",
        _nested_outputs(**{key or "TestResultsResolverFunctionArn": arn}),
        {"StackName": NESTED_STACK_NAME},
    )


class _FakeClock:
    """A stand-in for the ``time`` module, scoped to the module under test.

    ``sleep`` advances a counter instead of waiting, and ``time`` reports it, so
    ``get_test_result``'s poll loop can be driven to its deadline instantly and
    deterministically.

    It is installed with ``monkeypatch.setattr(module, "time", clock)`` —
    replacing the *module's own* ``time`` attribute. The distinction matters and
    is the reason this class exists: ``monkeypatch.setattr("idp_sdk._core.
    test_studio_processor.time.sleep", ...)`` looks equally local but resolves
    through the module's attribute to the **shared ``time`` module object**, so
    for the duration of the test every caller in the process — botocore's retry
    logic, moto, pytest itself — gets the fake instead. That is invisible when
    the file is run alone and shows up only in a full-suite run: a fake clock
    driven by a finite iterator was exhausted by another library's ``time.time()``
    call, and the test failed with ``StopIteration`` in the whole-directory run
    while passing on its own. Patching the module attribute cannot reach anyone
    else.
    """

    def __init__(self, start: float = 0.0):
        self.now = start
        #: Every ``poll_interval`` the code under test asked to wait, in order.
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _install_clock(monkeypatch, clock: _FakeClock) -> _FakeClock:
    """Give only ``test_studio_processor`` the fake clock."""
    from idp_sdk._core import test_studio_processor

    monkeypatch.setattr(test_studio_processor, "time", clock)
    return clock


def _payload(body: dict) -> dict:
    """An ``invoke`` response whose ``Payload`` reads back as ``body``."""
    return {
        "StatusCode": 200,
        "Payload": io.BytesIO(json.dumps(body).encode("utf-8")),
    }


def _expect_invoke(function_arn: str, field_name: str, arguments: dict) -> dict:
    """The exact ``invoke`` parameters the processor must send.

    Serialised the same way the processor does, so a renamed ``fieldName`` or
    argument key is a byte difference the Stubber rejects.
    """
    return {
        "FunctionName": function_arn,
        "InvocationType": "RequestResponse",
        "Payload": json.dumps(
            {"info": {"fieldName": field_name}, "arguments": arguments}
        ),
    }


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_clients_are_built_for_the_stacks_region(self, aws_credentials):
        """The Lambda client must follow the region the caller asked for.

        ``StackInfo`` resolves the region and the processor reads it back off the
        stack info rather than the raw argument, so a client built against the
        ambient default would call a resolver in the wrong region — which
        presents as "function not found" against a stack that is perfectly fine.
        """
        proc = _make_processor(region=REGION)

        assert proc.stack_info.stack_name == STACK
        assert proc.lambda_client.meta.region_name == REGION
        assert proc._resolver_arn is None

    def test_the_class_is_not_collected_as_a_test(self):
        """``__test__ = False`` is load-bearing, not decoration.

        The class is named ``TestStudioProcessor``, which matches this suite's
        ``python_classes = Test*``. Without the opt-out pytest tries to collect
        it and its ``__init__`` makes the whole module a collection error.
        """
        from idp_sdk._core.test_studio_processor import TestStudioProcessor

        assert TestStudioProcessor.__test__ is False


# ---------------------------------------------------------------------------
# Resolver ARN lookup
# ---------------------------------------------------------------------------


class TestResolverArnLookup:
    def test_the_arn_comes_from_the_api_resolver_nested_stack(self, aws_credentials):
        proc = _make_processor()
        with _stubs(proc) as (cfn, _):
            _queue_resolver_lookup(cfn)

            assert proc._get_resolver_function_arn() == RESOLVER_ARN

    def test_a_pre_appsync_removal_stack_is_found_under_the_old_logical_id(
        self, aws_credentials
    ):
        """The fallback to ``APPSYNCSTACK`` is the SDK's backward compatibility.

        A stack deployed before the AppSync removal names the nested stack
        ``APPSYNCSTACK``. If this fallback stopped being taken, the SDK would
        report "Test Studio not enabled" against a stack where it is enabled —
        so the test drives the real two-attempt sequence rather than asserting
        the pattern string.
        """
        proc = _make_processor()
        with _stubs(proc) as (cfn, _):
            # First attempt: "apiresolver" matches nothing.
            cfn.add_response(
                "describe_stack_resources", _no_nested_stacks(), {"StackName": STACK}
            )
            # Second attempt: "appsync" matches.
            cfn.add_response(
                "describe_stack_resources",
                _nested_stack_resources(logical_id="APPSYNCSTACK"),
                {"StackName": STACK},
            )
            cfn.add_response(
                "describe_stacks",
                _nested_outputs(TestResultsResolverFunctionArn=RESOLVER_ARN),
                {"StackName": NESTED_STACK_NAME},
            )

            assert proc._get_resolver_function_arn() == RESOLVER_ARN

    def test_the_arn_is_resolved_once_and_cached(self, aws_credentials):
        """Every operation begins with this lookup, so it must not re-query.

        Two CloudFormation calls per Test Studio call would be a throttling
        source on a loop over test runs. The Stubber enforces the count: a second
        lookup would find no queued response and fail.
        """
        proc = _make_processor()
        with _stubs(proc) as (cfn, _):
            _queue_resolver_lookup(cfn)

            first = proc._get_resolver_function_arn()
            second = proc._get_resolver_function_arn()

        assert first == second == RESOLVER_ARN

    def test_a_missing_nested_stack_becomes_a_resource_not_found_error(
        self, aws_credentials
    ):
        """Both logical ids miss, so the caller gets the actionable message."""
        proc = _make_processor()
        with _stubs(proc) as (cfn, _):
            for _ in range(2):
                cfn.add_response(
                    "describe_stack_resources",
                    _no_nested_stacks(),
                    {"StackName": STACK},
                )

            with pytest.raises(IDPResourceNotFoundError) as exc:
                proc._get_resolver_function_arn()

        message = str(exc.value)
        assert "Failed to get TestResultsResolverFunction ARN" in message
        assert "Ensure Test Studio is enabled" in message

    def test_a_missing_output_reports_the_fallback_stack_not_the_real_cause(
        self, aws_credentials
    ):
        """DEFECT, pinned as-is: the diagnosis names the wrong nested stack.

        This is a current-template stack (``APIRESOLVERSTACK`` present) deployed
        with Test Studio disabled, so the resolver *output* is absent. The first
        attempt therefore fails with "Output ... not found in nested stack
        matching 'apiresolver'" — the accurate message — and
        ``_get_api_resolver_stack_output`` swallows it to try ``appsync``, which
        does not match at all. Only the second failure reaches the caller, so the
        message is "Nested stack matching 'appsync' not found", pointing at a
        stack the operator does not have and never will.

        The exception type is right, so nothing breaks; the operator is sent to
        the wrong place. Pinned rather than fixed: returning the more specific
        of the two failures is a production change.

        Note also that the second attempt consumes only *one* CloudFormation
        call, because the pattern match fails before any DescribeStacks — which
        is the detail that makes the two failure modes distinguishable at all.
        """
        proc = _make_processor()
        with _stubs(proc) as (cfn, _):
            cfn.add_response(
                "describe_stack_resources",
                _nested_stack_resources(),
                {"StackName": STACK},
            )
            cfn.add_response(
                "describe_stacks",
                _nested_outputs(SomeOtherOutput="x"),
                {"StackName": NESTED_STACK_NAME},
            )
            cfn.add_response(
                "describe_stack_resources",
                _nested_stack_resources(),
                {"StackName": STACK},
            )

            with pytest.raises(IDPResourceNotFoundError) as exc:
                proc._get_resolver_function_arn()

        message = str(exc.value)
        assert "appsync" in message
        assert "TestResultsResolverFunctionArn" not in message, (
            "if the specific failure now survives the fallback, this defect has "
            "been fixed — assert the accurate message instead of relaxing this"
        )

    def test_an_empty_output_value_is_treated_as_missing(self, aws_credentials):
        """A present-but-empty output must not be used as a function name.

        An empty ``FunctionName`` would reach Lambda as a parameter-validation
        error naming nothing the operator can act on. The doubled prefix in the
        asserted message is the re-wrapping described in the module docstring:
        the ``IDPResourceNotFoundError`` raised here is caught by this method's
        own ``except Exception`` and wrapped again.
        """
        proc = _make_processor()
        with _stubs(proc) as (cfn, _):
            _queue_resolver_lookup(cfn, arn="")

            with pytest.raises(IDPResourceNotFoundError) as exc:
                proc._get_resolver_function_arn()

        assert str(exc.value).count("not found in nested API resolver stack") == 1
        assert "Failed to get TestResultsResolverFunction ARN" in str(exc.value)


# ---------------------------------------------------------------------------
# get_test_run_status
# ---------------------------------------------------------------------------


class TestGetTestRunStatus:
    def test_the_status_query_sends_the_getTestRunStatus_envelope(
        self, aws_credentials
    ):
        """The resolver dispatches on ``info.fieldName``; a typo returns nothing.

        The Stubber's ``expected_params`` is the assertion: the serialised
        payload must be exactly this envelope with exactly this argument name.
        """
        proc = _make_processor()
        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(cfn)
            lam.add_response(
                "invoke",
                _payload({"status": "RUNNING"}),
                _expect_invoke(
                    RESOLVER_ARN, "getTestRunStatus", {"testRunId": "run-1"}
                ),
            )

            assert proc.get_test_run_status("run-1") == "RUNNING"

    def test_a_response_without_a_status_reads_as_unknown(self, aws_credentials):
        """Absent is not the same as failed, so it is reported, not raised."""
        proc = _make_processor()
        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(cfn)
            lam.add_response(
                "invoke",
                _payload({"testRunId": "run-1"}),
                _expect_invoke(
                    RESOLVER_ARN, "getTestRunStatus", {"testRunId": "run-1"}
                ),
            )

            assert proc.get_test_run_status("run-1") == "UNKNOWN"

    def test_a_resolver_error_message_becomes_a_processing_error(self, aws_credentials):
        """A Lambda that returns an ``errorMessage`` is a failure, not a status.

        The resolver reports its own failures in the payload rather than through
        ``FunctionError``, so this branch is the only thing between the caller
        and a status of ``"UNKNOWN"`` for a run the resolver could not read.
        """
        proc = _make_processor()
        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(cfn)
            lam.add_response(
                "invoke",
                _payload({"errorMessage": "test run not found"}),
                _expect_invoke(RESOLVER_ARN, "getTestRunStatus", {"testRunId": "gone"}),
            )

            with pytest.raises(IDPProcessingError, match="test run not found"):
                proc.get_test_run_status("gone")


# ---------------------------------------------------------------------------
# get_test_result
# ---------------------------------------------------------------------------


class TestGetTestResult:
    def test_the_result_query_sends_the_getTestRun_envelope(self, aws_credentials):
        """``getTestRun``, not ``getTestRunStatus`` — a different resolver field."""
        proc = _make_processor()
        run = {
            "testRunId": "run-1",
            "testSetName": "fake-w2",
            "status": "COMPLETE",
            "overallAccuracy": 0.95,
        }
        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(cfn)
            lam.add_response(
                "invoke",
                _payload(run),
                _expect_invoke(RESOLVER_ARN, "getTestRun", {"testRunId": "run-1"}),
            )

            assert proc.get_test_result("run-1") == run

    def test_wait_polls_status_until_a_final_state_then_fetches(
        self, aws_credentials, monkeypatch
    ):
        """``wait=True`` must stop on a final state and not on a transient one.

        ``EVALUATING`` is the state the run passes through *after* processing, so
        treating it as final is the specific mistake here: it returns a result
        with no metrics in it. The sleep is recorded rather than performed, so the
        poll interval is asserted instead of waited on.
        """
        clock = _install_clock(monkeypatch, _FakeClock())
        proc = _make_processor()

        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(cfn)
            for status in ("RUNNING", "EVALUATING", "COMPLETE"):
                lam.add_response(
                    "invoke",
                    _payload({"status": status}),
                    _expect_invoke(
                        RESOLVER_ARN, "getTestRunStatus", {"testRunId": "run-1"}
                    ),
                )
            lam.add_response(
                "invoke",
                _payload({"testRunId": "run-1", "status": "COMPLETE"}),
                _expect_invoke(RESOLVER_ARN, "getTestRun", {"testRunId": "run-1"}),
            )

            result = proc.get_test_result("run-1", wait=True, poll_interval=7)

        assert result["status"] == "COMPLETE"
        # One sleep per non-final status, at the interval the caller chose.
        assert clock.sleeps == [7, 7]

    @pytest.mark.parametrize("status", ["PARTIAL_COMPLETE", "FAILED", "CANCELED"])
    def test_wait_treats_every_terminal_status_as_finished(
        self, aws_credentials, monkeypatch, status
    ):
        """A run that failed or was cancelled never reaches ``COMPLETE``.

        Omitting any of these from the final-state set would spin until the
        timeout and report a timeout for a run that finished long before. The
        recorded sleeps are what detect that: a second poll means the status was
        not accepted as terminal.
        """
        clock = _install_clock(monkeypatch, _FakeClock())
        proc = _make_processor()

        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(cfn)
            lam.add_response(
                "invoke",
                _payload({"status": status}),
                _expect_invoke(
                    RESOLVER_ARN, "getTestRunStatus", {"testRunId": "run-1"}
                ),
            )
            lam.add_response(
                "invoke",
                _payload({"testRunId": "run-1", "status": status}),
                _expect_invoke(RESOLVER_ARN, "getTestRun", {"testRunId": "run-1"}),
            )

            assert proc.get_test_result("run-1", wait=True)["status"] == status

        assert clock.sleeps == [], "a terminal status must not be polled again"

    def test_a_deadline_already_passed_fetches_nothing_at_all(self, aws_credentials):
        """A timeout must not be followed by a ``getTestRun`` for a running test.

        The elapsed check sits at the top of the poll loop, ahead of both the
        status poll and the result fetch, so a deadline already in the past has to
        raise before either. The Stubber is the assertion: the resolver lookup is
        queued and nothing else is, so any Lambda call at all fails the test. A
        fetch here would return a half-finished run that reads like a finished one.

        A negative ``timeout`` puts the deadline in the past against the **real**
        clock, which keeps this test free of any clock patching: ``elapsed`` is
        never negative, so ``elapsed > -1`` holds on the first iteration
        regardless of timer resolution. ``timeout=0`` would depend on two
        consecutive ``time.time()`` calls differing, which is not guaranteed.
        """
        proc = _make_processor()

        with _stubs(proc) as (cfn, _):
            _queue_resolver_lookup(cfn)

            with pytest.raises(
                IDPProcessingError, match="Timeout waiting for test run"
            ):
                proc.get_test_result("run-1", wait=True, timeout=-1)

    def test_a_deadline_reached_while_polling_stops_before_the_fetch(
        self, aws_credentials, monkeypatch
    ):
        """The realistic timeout: several polls, then the deadline, then no fetch.

        Driven by the scoped fake clock, whose ``sleep`` advances the time the
        loop then reads — so the deadline is crossed by the polling itself rather
        than by a preset value. Two polls fit inside a 300s budget at 200s per
        interval and the third check fails, and the Stubber proves no
        ``getTestRun`` followed: a result fetched after a timeout is a partial run
        presented as a complete one.
        """
        clock = _install_clock(monkeypatch, _FakeClock())
        proc = _make_processor()

        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(cfn)
            for _ in range(2):
                lam.add_response(
                    "invoke",
                    _payload({"status": "RUNNING"}),
                    _expect_invoke(
                        RESOLVER_ARN, "getTestRunStatus", {"testRunId": "run-1"}
                    ),
                )

            with pytest.raises(IDPProcessingError, match="to complete after 300s"):
                proc.get_test_result("run-1", wait=True, timeout=300, poll_interval=200)

        assert clock.sleeps == [200, 200]

    def test_a_still_evaluating_error_suggests_wait(self, aws_credentials):
        """The remedy for this failure is an argument, so the message names it."""
        proc = _make_processor()
        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(cfn)
            lam.add_response(
                "invoke",
                _payload({"errorMessage": "Test run is still EVALUATING"}),
                _expect_invoke(RESOLVER_ARN, "getTestRun", {"testRunId": "run-1"}),
            )

            with pytest.raises(IDPProcessingError, match="Use wait=True"):
                proc.get_test_result("run-1")

    def test_any_other_resolver_error_is_reported_verbatim(self, aws_credentials):
        proc = _make_processor()
        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(cfn)
            lam.add_response(
                "invoke",
                _payload({"errorMessage": "DynamoDB throttled"}),
                _expect_invoke(RESOLVER_ARN, "getTestRun", {"testRunId": "run-1"}),
            )

            with pytest.raises(IDPProcessingError, match="DynamoDB throttled"):
                proc.get_test_result("run-1")


# ---------------------------------------------------------------------------
# compare_test_runs
# ---------------------------------------------------------------------------


def _run(run_id: str, **overrides) -> dict:
    run = {
        "testRunId": run_id,
        "testSetName": "fake-w2",
        "status": "COMPLETE",
        "filesCount": 100,
        "completedFiles": 98,
        "failedFiles": 2,
        "overallAccuracy": 0.95,
        "accuracyBreakdown": {"precision": 0.96},
        "totalCost": 12.5,
        "createdAt": "2026-04-10T12:00:00Z",
        "completedAt": "2026-04-10T12:30:00Z",
    }
    run.update(overrides)
    return run


class TestCompareTestRuns:
    def test_fewer_than_two_runs_is_a_value_error_before_any_aws_call(
        self, aws_credentials
    ):
        """The guard is ahead of the lookup, so a bad call costs nothing.

        It is also a plain ``ValueError`` rather than an ``IDPProcessingError``,
        because it sits before the ``try``; the operation layer is what turns it
        into the SDK's exception type.
        """
        proc = _make_processor()
        with _stubs(proc) as (_, _lam):
            with pytest.raises(ValueError, match="At least 2 test run IDs"):
                proc.compare_test_runs(["only-one"])

    def test_each_run_contributes_exactly_the_comparison_metrics(self, aws_credentials):
        """The projection is the contract — extra keys are dropped, none renamed.

        A comparison table is read column by column, so a key silently absent
        (or carrying the resolver's name instead of this one) shows as a blank
        cell rather than an error.
        """
        proc = _make_processor()
        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(cfn)
            for run_id in ("run-1", "run-2"):
                lam.add_response(
                    "invoke",
                    _payload(_run(run_id, internalCursor="drop-me")),
                    _expect_invoke(RESOLVER_ARN, "getTestRun", {"testRunId": run_id}),
                )

            result = proc.compare_test_runs(["run-1", "run-2"])

        assert set(result) == {"metrics", "configs"}
        assert set(result["metrics"]) == {"run-1", "run-2"}
        assert result["metrics"]["run-1"] == {
            "testRunId": "run-1",
            "testSetName": "fake-w2",
            "status": "COMPLETE",
            "filesCount": 100,
            "completedFiles": 98,
            "failedFiles": 2,
            "overallAccuracy": 0.95,
            "accuracyBreakdown": {"precision": 0.96},
            "totalCost": 12.5,
            "createdAt": "2026-04-10T12:00:00Z",
            "completedAt": "2026-04-10T12:30:00Z",
        }

    def test_an_unfinished_run_keeps_its_missing_accuracy_as_none(
        self, aws_credentials
    ):
        """Counts default to zero but accuracy and cost do not both behave alike.

        ``overallAccuracy`` has no default, so an unfinished run reports ``None``
        — distinguishable from a run that genuinely scored zero. ``totalCost``
        defaults to ``0.0`` and ``filesCount`` to ``0``, which is the pragmatic
        choice for a summable column; the asymmetry is deliberate and is pinned
        here so it is not "tidied" into a uniform ``None`` or a uniform zero.
        """
        proc = _make_processor()
        bare = {"testRunId": "run-2", "status": "RUNNING"}
        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(cfn)
            for run_id, body in (("run-1", _run("run-1")), ("run-2", bare)):
                lam.add_response(
                    "invoke",
                    _payload(body),
                    _expect_invoke(RESOLVER_ARN, "getTestRun", {"testRunId": run_id}),
                )

            metrics = proc.compare_test_runs(["run-1", "run-2"])["metrics"]

        assert metrics["run-2"]["overallAccuracy"] is None
        assert metrics["run-2"]["totalCost"] == 0.0
        assert metrics["run-2"]["filesCount"] == 0
        assert metrics["run-2"]["accuracyBreakdown"] == {}

    def test_one_unreadable_run_is_skipped_and_the_rest_are_compared(
        self, aws_credentials
    ):
        """A partial comparison is more useful than no comparison.

        The skipped run is absent from ``metrics`` rather than present with
        empty values, which is what lets a caller tell "not retrieved" from
        "retrieved and scored nothing".
        """
        proc = _make_processor()
        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(cfn)
            lam.add_response(
                "invoke",
                _payload(_run("run-1")),
                _expect_invoke(RESOLVER_ARN, "getTestRun", {"testRunId": "run-1"}),
            )
            lam.add_response(
                "invoke",
                _payload({"errorMessage": "expired"}),
                _expect_invoke(RESOLVER_ARN, "getTestRun", {"testRunId": "run-2"}),
            )

            metrics = proc.compare_test_runs(["run-1", "run-2"])["metrics"]

        assert list(metrics) == ["run-1"]

    def test_no_readable_runs_raises_rather_than_returning_an_empty_table(
        self, aws_credentials
    ):
        """An empty comparison would read as "the runs are identical"."""
        proc = _make_processor()
        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(cfn)
            for run_id in ("run-1", "run-2"):
                lam.add_response(
                    "invoke",
                    _payload({"errorMessage": "expired"}),
                    _expect_invoke(RESOLVER_ARN, "getTestRun", {"testRunId": run_id}),
                )

            with pytest.raises(IDPProcessingError, match="No test runs could be"):
                proc.compare_test_runs(["run-1", "run-2"])

    def test_the_configurations_the_runs_captured_are_compared(self, aws_credentials):
        """`getTestRun` already returns each run's captured configuration.

        So the comparison needs no extra call, and this is the wiring: the `config`
        key on each run's payload reaches `configuration_differences`, whose result
        is what the CLI renders. Before this the CLI assigned `configs = []` with a
        `TODO` and printed "No configuration differences to display" for every
        input, including two runs on different models.
        """
        proc = _make_processor()
        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(cfn)
            for run_id, model in (("run-1", "nova-lite"), ("run-2", "nova-pro")):
                lam.add_response(
                    "invoke",
                    _payload(
                        _run(
                            run_id,
                            config={"Config": {"extraction": {"model": model}}},
                        )
                    ),
                    _expect_invoke(RESOLVER_ARN, "getTestRun", {"testRunId": run_id}),
                )

            result = proc.compare_test_runs(["run-1", "run-2"])

        assert result["configs"] == [
            {
                "setting": "extraction.model",
                "values": {"run-1": "nova-lite", "run-2": "nova-pro"},
            }
        ]

    def test_a_run_that_captured_no_configuration_leaves_configs_unanswered(
        self, aws_credentials
    ):
        """`None`, not `[]`: with one configuration there is nothing to compare.

        A run whose evaluation aggregate has not been written yet returns no
        `config` key. Treating that as an empty configuration would report every
        setting the other run has as a difference, and reporting `[]` would tell
        the user the configurations matched when they were never read.
        """
        proc = _make_processor()
        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(cfn)
            lam.add_response(
                "invoke",
                _payload(
                    _run("run-1", config={"Config": {"extraction": {"model": "x"}}})
                ),
                _expect_invoke(RESOLVER_ARN, "getTestRun", {"testRunId": "run-1"}),
            )
            lam.add_response(
                "invoke",
                _payload(_run("run-2")),
                _expect_invoke(RESOLVER_ARN, "getTestRun", {"testRunId": "run-2"}),
            )

            result = proc.compare_test_runs(["run-1", "run-2"])

        assert result["configs"] is None

    def test_identical_captured_configurations_compare_to_an_empty_list(
        self, aws_credentials
    ):
        """`[]` is the answer that means "compared, and they match"."""
        proc = _make_processor()
        body = {"Config": {"extraction": {"model": "nova-lite"}}}
        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(cfn)
            for run_id in ("run-1", "run-2"):
                lam.add_response(
                    "invoke",
                    _payload(_run(run_id, config=body)),
                    _expect_invoke(RESOLVER_ARN, "getTestRun", {"testRunId": run_id}),
                )

            result = proc.compare_test_runs(["run-1", "run-2"])

        assert result["configs"] == []


# ---------------------------------------------------------------------------
# configuration_differences
# ---------------------------------------------------------------------------


class TestConfigurationDifferences:
    """The pure diff behind `test-compare`'s configuration table.

    Pure, so every case here is measured rather than argued: no AWS, no stubs.
    """

    @staticmethod
    def _entry(run_id, body):
        return {"testRunId": run_id, "config": {"Config": body}}

    def test_fewer_than_two_configurations_is_unanswered_rather_than_empty(self):
        """`None` and `[]` are different answers a caller must be able to tell apart."""
        assert configuration_differences([]) is None
        assert configuration_differences([self._entry("run-1", {"a": 1})]) is None

    def test_identical_configurations_produce_no_differences(self):
        body = {"extraction": {"model": "nova-lite", "temperature": 0}}
        assert (
            configuration_differences(
                [self._entry("run-1", body), self._entry("run-2", dict(body))]
            )
            == []
        )

    def test_a_differing_scalar_is_reported_with_every_runs_value(self):
        assert configuration_differences(
            [
                self._entry("run-1", {"extraction": {"model": "nova-lite"}}),
                self._entry("run-2", {"extraction": {"model": "nova-pro"}}),
            ]
        ) == [
            {
                "setting": "extraction.model",
                "values": {"run-1": "nova-lite", "run-2": "nova-pro"},
            }
        ]

    def test_a_setting_only_one_run_has_reads_missing_for_the_other(self):
        """Present-versus-absent is a difference, and it is labelled as absence.

        A blank cell would read as "the same as the other run", which is the one
        thing it is not.
        """
        assert configuration_differences(
            [
                self._entry("run-1", {"assessment": {"enabled": True}}),
                self._entry("run-2", {}),
            ]
        ) == [
            {
                "setting": "assessment.enabled",
                "values": {"run-1": "True", "run-2": "<missing>"},
            }
        ]

    def test_list_elements_are_compared_per_index(self):
        """One difference at the index that moved, not one opaque "the list changed"."""
        differences = configuration_differences(
            [
                self._entry("run-1", {"steps": ["ocr", "extract"]}),
                self._entry("run-2", {"steps": ["ocr", "assess"]}),
            ]
        )

        assert differences == [
            {"setting": "steps.1", "values": {"run-1": "extract", "run-2": "assess"}}
        ]

    def test_the_metadata_and_class_keys_are_not_compared(self):
        """Save timestamps move on every save and class schemas would fill the table.

        Each key is asserted individually, because the reason for omitting it is a
        property of that key rather than of the group — and a key that is in the set
        for no reason is a difference the user will never be shown.
        """
        for key in (
            "UpdatedAt",
            "Description",
            "CreatedAt",
            "IsActive",
            "Configuration",
            "version_name",
            "classes",
        ):
            assert (
                configuration_differences(
                    [
                        self._entry("run-1", {key: "a"}),
                        self._entry("run-2", {key: "b"}),
                    ]
                )
                == []
            ), key

    def test_a_real_difference_beside_an_ignored_key_is_still_reported(self):
        """The skip is per key, not a short circuit over the whole configuration."""
        assert configuration_differences(
            [
                self._entry("run-1", {"CreatedAt": "a", "extraction": {"model": "x"}}),
                self._entry("run-2", {"CreatedAt": "b", "extraction": {"model": "y"}}),
            ]
        ) == [{"setting": "extraction.model", "values": {"run-1": "x", "run-2": "y"}}]

    def test_differences_are_ordered_by_setting_path(self):
        """A table read top to bottom needs a stable order across invocations."""
        differences = configuration_differences(
            [
                self._entry("run-1", {"z": 1, "a": 1, "m": 1}),
                self._entry("run-2", {"z": 2, "a": 2, "m": 2}),
            ]
        )

        assert differences is not None
        assert [d["setting"] for d in differences] == ["a", "m", "z"]

    def test_a_run_whose_captured_object_is_missing_its_body_contributes_nothing(self):
        """The captured object wraps the configuration under `Config`.

        An entry without that key is read as an empty configuration rather than
        raising, so one malformed record cannot take the whole comparison down.
        """
        assert configuration_differences(
            [
                {"testRunId": "run-1", "config": {}},
                self._entry("run-2", {"extraction": {"model": "x"}}),
            ]
        ) == [
            {
                "setting": "extraction.model",
                "values": {"run-1": "<missing>", "run-2": "x"},
            }
        ]


# ---------------------------------------------------------------------------
# abort_test_runs
# ---------------------------------------------------------------------------


class TestAbortTestRuns:
    def test_abort_uses_its_own_resolver_and_the_plural_argument(self, aws_credentials):
        """A different output key, a different function, a different argument.

        ``abort_test_runs`` resolves ``AbortTestRunsResolverFunctionArn`` — not
        the cached ``TestResultsResolverFunctionArn`` — and sends ``testRunIds``
        (plural, a list). Sending the singular key would abort nothing while
        reporting success, because the resolver would see no ids.
        """
        proc = _make_processor()
        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(
                cfn, arn=ABORT_ARN, key="AbortTestRunsResolverFunctionArn"
            )
            lam.add_response(
                "invoke",
                _payload({"abortedCount": 2, "failedCount": 0, "errors": []}),
                _expect_invoke(
                    ABORT_ARN, "abortTestRuns", {"testRunIds": ["run-1", "run-2"]}
                ),
            )

            result = proc.abort_test_runs(["run-1", "run-2"])

        assert result == {"abortedCount": 2, "failedCount": 0, "errors": []}
        # The abort ARN must not be cached as the results-resolver ARN.
        assert proc._resolver_arn is None

    def test_a_stack_without_the_abort_resolver_says_so(self, aws_credentials):
        """This message was dead code until the caught type was corrected.

        ``get_nested_stack_output`` raises ``ValueError``, and the handler here
        used to catch ``IDPResourceNotFoundError`` — which it never raises — so
        the ``ValueError`` escaped and the "upgrade your stack" guidance was
        never shown. The stack here is a current one whose API resolver stack
        carries the results resolver but no abort resolver, which is what an
        older deployed template looks like.
        """
        proc = _make_processor()
        with _stubs(proc) as (cfn, _):
            cfn.add_response(
                "describe_stack_resources",
                _nested_stack_resources(),
                {"StackName": STACK},
            )
            cfn.add_response(
                "describe_stacks",
                _nested_outputs(TestResultsResolverFunctionArn=RESOLVER_ARN),
                {"StackName": NESTED_STACK_NAME},
            )
            # The ``appsync`` fallback matches no logical id, so it costs one
            # DescribeStackResources and no DescribeStacks.
            cfn.add_response(
                "describe_stack_resources",
                _nested_stack_resources(),
                {"StackName": STACK},
            )

            with pytest.raises(IDPResourceNotFoundError) as exc:
                proc.abort_test_runs(["run-1"])

        assert "stack version that supports test run abort" in str(exc.value)

    def test_a_resolver_reported_abort_failure_becomes_a_processing_error(
        self, aws_credentials
    ):
        proc = _make_processor()
        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(
                cfn, arn=ABORT_ARN, key="AbortTestRunsResolverFunctionArn"
            )
            lam.add_response(
                "invoke",
                _payload({"errorMessage": "run already finished"}),
                _expect_invoke(ABORT_ARN, "abortTestRuns", {"testRunIds": ["run-1"]}),
            )

            with pytest.raises(IDPProcessingError, match="run already finished"):
                proc.abort_test_runs(["run-1"])

    def test_a_partial_abort_is_returned_rather_than_raised(self, aws_credentials):
        """Some ids aborted, some did not — the caller needs the breakdown.

        The resolver reports this in ``failedCount``/``errors`` with no
        ``errorMessage``, so it must come back as data. Raising here would lose
        the record of which runs *were* aborted.
        """
        proc = _make_processor()
        body = {
            "abortedCount": 1,
            "failedCount": 1,
            "errors": [{"testRunId": "run-2", "message": "already COMPLETE"}],
        }
        with _stubs(proc) as (cfn, lam):
            _queue_resolver_lookup(
                cfn, arn=ABORT_ARN, key="AbortTestRunsResolverFunctionArn"
            )
            lam.add_response(
                "invoke",
                _payload(body),
                _expect_invoke(
                    ABORT_ARN, "abortTestRuns", {"testRunIds": ["run-1", "run-2"]}
                ),
            )

            assert proc.abort_test_runs(["run-1", "run-2"]) == body
