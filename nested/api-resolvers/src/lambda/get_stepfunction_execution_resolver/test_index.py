# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock

# Set BEFORE `import index`. That import reaches
# idp_common.utils.log_sanitizer, whose package __init__ pulls settings_helper,
# which builds an SSM client at module scope — and botocore raises NoRegionError at
# COLLECTION with no region configured. The Lambda runtime always sets AWS_REGION, so
# this is a test-harness assumption rather than a defect in the handler, but it means
# the suite passes on a developer machine (which has an ambient region) and aborts on
# a CI runner. No credentials are needed or used. Same trap as #988, and the reason
# `make test-packages-cicd` pins AWS_DEFAULT_REGION for three src/lambda suites.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import index  # noqa: E402
import pytest  # noqa: E402
from index import (  # noqa: E402
    find_step_name_for_failure_event,
    parse_execution_history,
)

from idp_common.stepfunctions_history import (  # noqa: E402
    FAILURE_RECOVERY_EVENTS,
    TASK_LEVEL_FAILURE_EVENTS,
    failure_detail_key,
)


@pytest.mark.unit
def test_parse_execution_history_with_failure():
    """Test that parse_execution_history correctly handles task failures"""

    # Mock execution history events that simulate a failure scenario
    events = [
        {
            "id": 1,
            "type": "ExecutionStarted",
            "timestamp": datetime(2024, 1, 1, 10, 0, 0),
            "executionStartedEventDetails": {},
        },
        {
            "id": 2,
            "type": "TaskStateEntered",
            "timestamp": datetime(2024, 1, 1, 10, 0, 1),
            "stateEnteredEventDetails": {
                "name": "ClassificationStep",
                "input": '{"document": "test.pdf"}',
            },
        },
        {
            "id": 3,
            "type": "TaskFailed",
            "timestamp": datetime(2024, 1, 1, 10, 0, 5),
            "previousEventId": 2,
            "taskFailedEventDetails": {
                "error": "ValidationException",
                "cause": '{"errorType": "ValidationException", "errorMessage": "Invalid document format"}',
            },
        },
    ]

    steps = parse_execution_history(events)

    # Should have one step
    assert len(steps) == 1

    # Check step details
    step = steps[0]
    assert step["name"] == "ClassificationStep"
    assert step["status"] == "FAILED"
    assert step["error"] == "ValidationException: Invalid document format"
    assert step["startDate"] == "2024-01-01T10:00:01"
    assert step["stopDate"] == "2024-01-01T10:00:05"


@pytest.mark.unit
def test_parse_execution_history_with_success():
    """Test that parse_execution_history correctly handles successful execution"""

    events = [
        {
            "id": 1,
            "type": "ExecutionStarted",
            "timestamp": datetime(2024, 1, 1, 10, 0, 0),
            "executionStartedEventDetails": {},
        },
        {
            "id": 2,
            "type": "TaskStateEntered",
            "timestamp": datetime(2024, 1, 1, 10, 0, 1),
            "stateEnteredEventDetails": {
                "name": "ClassificationStep",
                "input": '{"document": "test.pdf"}',
            },
        },
        {
            "id": 3,
            "type": "TaskStateExited",
            "timestamp": datetime(2024, 1, 1, 10, 0, 5),
            "stateExitedEventDetails": {
                "name": "ClassificationStep",
                "output": '{"classification": "invoice"}',
            },
        },
    ]

    steps = parse_execution_history(events)

    # Should have one step
    assert len(steps) == 1

    # Check step details
    step = steps[0]
    assert step["name"] == "ClassificationStep"
    assert step["status"] == "SUCCEEDED"
    assert step["error"] is None
    assert step["output"] == '{"classification": "invoice"}'


@pytest.mark.unit
def test_find_step_name_for_failure_event():
    """Test that find_step_name_for_failure_event correctly identifies the failed step"""

    failure_event = {
        "id": 3,
        "type": "TaskFailed",
        "previousEventId": 2,
        "taskFailedEventDetails": {
            "error": "ValidationException",
            "cause": "Invalid input",
        },
    }

    all_events = [
        {
            "id": 1,
            "type": "ExecutionStarted",
            "timestamp": datetime(2024, 1, 1, 10, 0, 0),
        },
        {
            "id": 2,
            "type": "TaskStateEntered",
            "timestamp": datetime(2024, 1, 1, 10, 0, 1),
            "stateEnteredEventDetails": {"name": "ClassificationStep"},
        },
        failure_event,
    ]

    event_id_to_step = {2: "ClassificationStep"}

    step_name = find_step_name_for_failure_event(
        failure_event, all_events, event_id_to_step
    )

    assert step_name == "ClassificationStep"


@pytest.mark.unit
def test_parse_execution_history_with_timeout():
    """Test that parse_execution_history correctly handles task timeouts"""

    events = [
        {
            "id": 1,
            "type": "ExecutionStarted",
            "timestamp": datetime(2024, 1, 1, 10, 0, 0),
            "executionStartedEventDetails": {},
        },
        {
            "id": 2,
            "type": "TaskStateEntered",
            "timestamp": datetime(2024, 1, 1, 10, 0, 1),
            "stateEnteredEventDetails": {
                "name": "ProcessingStep",
                "input": '{"document": "test.pdf"}',
            },
        },
        {
            "id": 3,
            "type": "TaskTimedOut",
            "timestamp": datetime(2024, 1, 1, 10, 5, 0),
            "previousEventId": 2,
            "taskTimedOutEventDetails": {
                "error": "States.Timeout",
                "cause": "Task timed out after 300 seconds",
            },
        },
    ]

    steps = parse_execution_history(events)

    # Should have one step
    assert len(steps) == 1

    # Check step details
    step = steps[0]
    assert step["name"] == "ProcessingStep"
    assert step["status"] == "FAILED"
    assert "Task timed out" in step["error"]
    assert step["startDate"] == "2024-01-01T10:00:01"
    assert step["stopDate"] == "2024-01-01T10:05:00"


@pytest.mark.unit
def test_parse_execution_history_multiple_steps():
    """Test parsing execution history with multiple steps including failures"""

    events = [
        {
            "id": 1,
            "type": "ExecutionStarted",
            "timestamp": datetime(2024, 1, 1, 10, 0, 0),
            "executionStartedEventDetails": {},
        },
        # First step - succeeds
        {
            "id": 2,
            "type": "TaskStateEntered",
            "timestamp": datetime(2024, 1, 1, 10, 0, 1),
            "stateEnteredEventDetails": {
                "name": "UploadStep",
                "input": '{"document": "test.pdf"}',
            },
        },
        {
            "id": 3,
            "type": "TaskStateExited",
            "timestamp": datetime(2024, 1, 1, 10, 0, 3),
            "stateExitedEventDetails": {
                "name": "UploadStep",
                "output": '{"uploaded": true}',
            },
        },
        # Second step - fails
        {
            "id": 4,
            "type": "TaskStateEntered",
            "timestamp": datetime(2024, 1, 1, 10, 0, 4),
            "stateEnteredEventDetails": {
                "name": "ClassificationStep",
                "input": '{"document": "test.pdf"}',
            },
        },
        {
            "id": 5,
            "type": "TaskFailed",
            "timestamp": datetime(2024, 1, 1, 10, 0, 8),
            "previousEventId": 4,
            "taskFailedEventDetails": {
                "error": "ValidationException",
                "cause": '{"errorType": "ValidationException", "errorMessage": "Invalid document format"}',
            },
        },
    ]

    steps = parse_execution_history(events)

    # Should have two steps
    assert len(steps) == 2

    # Check first step (successful)
    upload_step = steps[0]
    assert upload_step["name"] == "UploadStep"
    assert upload_step["status"] == "SUCCEEDED"
    assert upload_step["error"] is None

    # Check second step (failed)
    classification_step = steps[1]
    assert classification_step["name"] == "ClassificationStep"
    assert classification_step["status"] == "FAILED"
    assert (
        classification_step["error"] == "ValidationException: Invalid document format"
    )


# ============================================================================
# Authorization
#
# The caller supplies `executionArn`, so the resolver has to decide two things
# itself: that the ARN names one of THIS deployment's executions, and that the
# caller's config-version scope covers the document that execution processed.
# Without them any authenticated user could read the input, output and step
# history of any execution the function's IAM role can describe — including the
# working-bucket S3 URI of the document's full state.
# ============================================================================

THIS_SM = "IDP-PATTERNUNIFIED-ABC-DocumentProcessingStateMachine-XYZ"
THIS_SM_ARN = f"arn:aws:states:us-west-2:123456789012:stateMachine:{THIS_SM}"


def _execution_arn(
    state_machine=THIS_SM, execution_id="11111111-2222-3333-4444-555555555555"
):
    return f"arn:aws:states:us-west-2:123456789012:execution:{state_machine}:{execution_id}"


def _event(execution_arn, groups=None, email="viewer@example.com"):
    """A dispatcher-normalized resolver event."""
    return {
        "arguments": {"executionArn": execution_arn},
        "identity": {
            "username": email,
            "claims": {
                "email": email,
                "cognito:username": email,
                "cognito:groups": groups if groups is not None else ["Viewer"],
            },
        },
        "info": {"fieldName": "getStepFunctionExecution"},
    }


def _describe_response(config_version="default", execution_arn=None):
    """A describe_execution response whose input is the compressed-document wrapper."""
    document = {
        "document_id": "acme/statement.pdf",
        "s3_uri": "s3://working-bucket/compressed_documents/acme/statement.pdf/1_state.json",
        "num_pages": 3,
    }
    if config_version is not None:
        document["config_version"] = config_version
    return {
        "executionArn": execution_arn or _execution_arn(),
        "status": "SUCCEEDED",
        "startDate": datetime(2024, 1, 1, 10, 0, 0),
        "input": json.dumps({"document": document}),
        "output": json.dumps({"document": document}),
    }


@pytest.fixture
def sfn(monkeypatch):
    """Stub Step Functions, pin this stack's state machine, empty the scope cache."""
    client = MagicMock()
    client.describe_execution.return_value = _describe_response()
    client.get_execution_history.return_value = {"events": []}
    monkeypatch.setattr(index, "stepfunctions", client)
    monkeypatch.setattr(index, "_STATE_MACHINE_ARN", THIS_SM_ARN)
    index._user_scope_cache.clear()
    return client


@pytest.fixture
def scoped_caller(monkeypatch):
    """Make UsersTable report a caller restricted to the versions the test names."""

    def _configure(allowed_versions, store=None):
        """The UsersTable double, covering both key spaces the lookup reads.

        ``store`` maps a raw ``PK`` to its item, which is how the ``sub`` join is
        modelled (a ``SUB#<sub>`` pointer, and the ``USER#<userId>`` row it names).
        Without it ``get_item`` would answer with a truthy Mock and a test could
        pass against a row that does not exist.
        """
        table = MagicMock()
        table.query.return_value = {
            "Items": [{"allowedConfigVersions": allowed_versions}]
            if allowed_versions
            else []
        }
        _store = dict(store or {})
        table.get_item.side_effect = lambda Key: (
            {"Item": _store[Key["PK"]]} if Key["PK"] in _store else {}
        )
        resource = MagicMock()
        resource.Table.return_value = table
        monkeypatch.setattr(index, "_dynamodb", resource)
        monkeypatch.setenv("USERS_TABLE_NAME", "IDP-UsersTable")
        index._user_scope_cache.clear()
        return table

    return _configure


@pytest.mark.unit
class TestExecutionBelongsToThisStack:
    def test_rejects_execution_of_another_state_machine(self, sfn):
        """An ARN naming a different state machine is refused.

        The IAM grant is a `<stack-name>-*` prefix wildcard, which also matches a
        sibling deployment whose stack name extends this one's (`IDP` matching
        `IDP-prod-...`). The name comparison is what closes that.
        """
        arn = _execution_arn(
            state_machine="OTHER-STACK-DocumentProcessingStateMachine-QQQ"
        )

        with pytest.raises(PermissionError):
            index.lambda_handler(_event(arn), None)

    def test_does_not_call_step_functions_for_a_rejected_arn(self, sfn):
        """A rejected ARN is never sent to the Step Functions data plane."""
        arn = _execution_arn(
            state_machine="OTHER-STACK-DocumentProcessingStateMachine-QQQ"
        )

        with pytest.raises(PermissionError):
            index.lambda_handler(_event(arn), None)

        sfn.describe_execution.assert_not_called()
        sfn.get_execution_history.assert_not_called()

    def test_rejects_when_state_machine_arn_is_unset(self, sfn, monkeypatch):
        """With nothing to compare against, deny rather than fall back to IAM."""
        monkeypatch.setattr(index, "_STATE_MACHINE_ARN", "")

        with pytest.raises(PermissionError):
            index.lambda_handler(_event(_execution_arn()), None)

    def test_rejects_a_malformed_arn(self, sfn):
        with pytest.raises(PermissionError):
            index.lambda_handler(_event("not-an-arn"), None)

    def test_accepts_an_execution_of_this_state_machine(self, sfn, scoped_caller):
        # An unrestricted caller (no scope row), not an unwired UsersTable: this
        # test is about the ARN check, and an unwired table now denies.
        scoped_caller([])

        result = index.lambda_handler(_event(_execution_arn()), None)

        assert result["status"] == "SUCCEEDED"
        sfn.describe_execution.assert_called_once()

    def test_accepts_a_distributed_map_child_execution(self, sfn, scoped_caller):
        """`NAME/mapRunLabel` in the ARN still names this state machine."""
        scoped_caller([])
        arn = _execution_arn(state_machine=f"{THIS_SM}/mapRunLabel")
        sfn.describe_execution.return_value = _describe_response(execution_arn=arn)

        result = index.lambda_handler(_event(arn), None)

        assert result["status"] == "SUCCEEDED"


@pytest.mark.unit
class TestConfigVersionScope:
    def test_scoped_caller_may_read_an_in_scope_execution(self, sfn, scoped_caller):
        scoped_caller(["tenant-a"])
        sfn.describe_execution.return_value = _describe_response(
            config_version="tenant-a"
        )

        result = index.lambda_handler(_event(_execution_arn()), None)

        assert result["status"] == "SUCCEEDED"

    def test_scoped_caller_may_not_read_an_out_of_scope_execution(
        self, sfn, scoped_caller
    ):
        """An out-of-scope execution names another tenant's document and state URI."""
        scoped_caller(["tenant-a"])
        sfn.describe_execution.return_value = _describe_response(
            config_version="tenant-b"
        )

        with pytest.raises(PermissionError):
            index.lambda_handler(_event(_execution_arn()), None)

    def test_scoped_caller_denied_when_execution_names_no_version(
        self, sfn, scoped_caller
    ):
        """No version to authorize against means no authorization — deny."""
        scoped_caller(["tenant-a"])
        sfn.describe_execution.return_value = _describe_response(config_version=None)

        with pytest.raises(PermissionError):
            index.lambda_handler(_event(_execution_arn()), None)

    def test_unscoped_caller_reads_any_execution_of_this_stack(
        self, sfn, scoped_caller
    ):
        """An empty allowedConfigVersions list means unrestricted, not denied."""
        scoped_caller([])
        sfn.describe_execution.return_value = _describe_response(
            config_version="tenant-b"
        )

        result = index.lambda_handler(_event(_execution_arn()), None)

        assert result["status"] == "SUCCEEDED"

    def test_admin_is_not_config_scoped(self, sfn, scoped_caller):
        table = scoped_caller(["tenant-a"])
        sfn.describe_execution.return_value = _describe_response(
            config_version="tenant-b"
        )

        result = index.lambda_handler(_event(_execution_arn(), groups=["Admin"]), None)

        assert result["status"] == "SUCCEEDED"
        table.query.assert_not_called()

    def test_no_users_table_configured_denies(self, sfn, monkeypatch):
        """An unwired UsersTable means the scope cannot be evaluated — deny.

        The parent template passes `UsersTableName` unconditionally to this nested
        stack, so an empty value is a wiring regression rather than a deployment
        that opted out of RBAC. Treating it as "unrestricted" made the control
        switch itself off on exactly the drift it exists to survive (AUTH.T07).
        """
        monkeypatch.delenv("USERS_TABLE_NAME", raising=False)
        sfn.describe_execution.return_value = _describe_response(
            config_version="tenant-b"
        )

        with pytest.raises(PermissionError):
            index.lambda_handler(_event(_execution_arn()), None)

    def test_dynamodb_failure_denies(self, sfn, monkeypatch):
        """A failed scope Query denies rather than reading as "unrestricted"."""
        table = MagicMock()
        table.query.side_effect = Exception("AccessDeniedException: dynamodb:Query")
        resource = MagicMock()
        resource.Table.return_value = table
        monkeypatch.setattr(index, "_dynamodb", resource)
        monkeypatch.setenv("USERS_TABLE_NAME", "IDP-UsersTable")
        index._user_scope_cache.clear()
        sfn.describe_execution.return_value = _describe_response(
            config_version="tenant-b"
        )

        with pytest.raises(PermissionError):
            index.lambda_handler(_event(_execution_arn()), None)

    def test_a_pattern_scope_admits_the_executions_it_covers(self, sfn, scoped_caller):
        """Scope entries may be globs; a membership test would deny all of them."""
        scoped_caller(["tenant-a_*"])
        sfn.describe_execution.return_value = _describe_response(
            config_version="tenant-a_v3"
        )

        assert index.lambda_handler(_event(_execution_arn()), None)["status"] == (
            "SUCCEEDED"
        )

    def test_a_pattern_scope_still_denies_what_it_does_not_cover(
        self, sfn, scoped_caller
    ):
        scoped_caller(["tenant-a_*"])
        sfn.describe_execution.return_value = _describe_response(
            config_version="tenant-b_v1"
        )

        with pytest.raises(PermissionError):
            index.lambda_handler(_event(_execution_arn()), None)

    def test_identity_without_an_email_claim_denies_without_querying(
        self, sfn, scoped_caller
    ):
        """No email claim means no lookup key — deny, and issue no query.

        An access token carries no `email` and no `cognito:username`. The old
        fallback chain resolved such a caller to their bare `sub`, which matches
        no UsersTable row; the empty page then read as "no restriction".
        """
        table = scoped_caller(["tenant-a"])
        event = _event(_execution_arn())
        event["identity"]["claims"] = {
            "sub": "11111111-2222-3333-4444-555555555555",
            "cognito:groups": ["Viewer"],
        }
        sfn.describe_execution.return_value = _describe_response(
            config_version="tenant-b"
        )

        with pytest.raises(PermissionError):
            index.lambda_handler(event, None)

        table.query.assert_not_called()

    def test_scope_lookup_is_cached_per_caller(self, sfn, scoped_caller):
        """A flow-viewer poll loop must not re-Query UsersTable on every call."""
        table = scoped_caller(["tenant-a"])
        sfn.describe_execution.return_value = _describe_response(
            config_version="tenant-a"
        )

        index.lambda_handler(_event(_execution_arn()), None)
        index.lambda_handler(_event(_execution_arn()), None)

        assert table.query.call_count == 1


@pytest.mark.unit
class TestDenialShape:
    def test_denial_is_raised_not_returned_as_an_error_payload(self, sfn):
        """The catch-all returns HTTP 200 with an error string; a denial must not.

        http_api_dispatcher only produces a 403 for a raised PermissionError, so
        swallowing the denial into the normal error payload would present it to
        the UI as an ordinary retrieval failure and answer 200.
        """
        arn = _execution_arn(state_machine="OTHER-STACK-SM")

        with pytest.raises(PermissionError) as excinfo:
            index.lambda_handler(_event(arn), None)

        assert str(excinfo.value).startswith("Unauthorized")

    def test_operational_failures_still_return_an_error_payload(self, sfn, monkeypatch):
        """Only denials raise; a Step Functions failure keeps the old behaviour."""
        monkeypatch.delenv("USERS_TABLE_NAME", raising=False)
        sfn.describe_execution.side_effect = RuntimeError("ExecutionDoesNotExist")

        result = index.lambda_handler(_event(_execution_arn()), None)

        assert result["status"] == "ERROR"
        assert "Failed to retrieve execution details" in result["error"]


# ============================================================================
# #1185 — naming the step a failure belongs to, and the failure that ended the
# execution rather than the first one.
#
# Every event below is checked against the botocore service model as it is built:
# the type must be a real `HistoryEventType` and each detail member a real member of
# that type's detail shape. The fixtures that were here before this were not, and two
# of them describe a history the service cannot produce — a `TaskFailed` whose
# `previousEventId` names the `TaskStateEntered` directly. That is the shape that made
# the single-hop correlation look correct.
# ============================================================================


def _model():
    import botocore.session

    return botocore.session.get_session().get_service_model("stepfunctions")


class _Hist:
    """History events with real ids, a real `previousEventId` chain, and model checking."""

    _FILLERS = {
        "resource": "arn:aws:lambda:us-west-2:123456789012:function:placeholder",
        "resourceType": "lambda",
        "region": "us-west-2",
        "parameters": "{}",
        "state": "PlaceholderState",
        "name": "PlaceholderState",
    }

    def __init__(self):
        self.model = _model()
        self.types = set(self.model.shape_for("HistoryEventType").enum)
        self.members = set(self.model.shape_for("HistoryEvent").members)
        self.events = []

    def add(self, event_type, previous=None, **detail):
        """Append an event. `previous` is the `previousEventId`; defaults to the last id."""
        assert event_type in self.types, (
            f"{event_type!r} is not a HistoryEventType; a fixture using it tests a "
            "service that does not exist"
        )
        event_id = len(self.events) + 1
        event = {
            "id": event_id,
            "type": event_type,
            "timestamp": datetime(2026, 3, 1, 10, 0, 0) + timedelta(seconds=event_id),
        }
        if previous is not None:
            event["previousEventId"] = previous
        elif event_id > 1:
            event["previousEventId"] = event_id - 1

        if event_type.endswith("StateEntered"):
            key = "stateEnteredEventDetails"
        elif event_type.endswith("StateExited"):
            key = "stateExitedEventDetails"
        else:
            candidate = f"{event_type[0].lower()}{event_type[1:]}EventDetails"
            key = candidate if candidate in self.members else None

        if key is not None:
            shape = self.model.shape_for(f"{key[0].upper()}{key[1:]}")
            for member in detail:
                assert member in shape.members, (
                    f"{shape.name} has no member {member!r}; the model lists "
                    f"{sorted(shape.members)}"
                )
            body = dict(detail)
            for member in shape.metadata.get("required") or []:
                body.setdefault(member, self._FILLERS[member])
            event[key] = body
        self.events.append(event)
        return self


@pytest.mark.unit
class TestTheFixtureBuilderIsDerivedFromTheServiceModel:
    def test_the_enum_was_read(self):
        assert len(_Hist().types) > 50

    def test_no_failure_detail_shape_declares_a_scheduled_event_id(self):
        """Why one correlation arm was removed rather than kept as a fallback.

        It read `taskFailedEventDetails["scheduledEventId"]`. No detail shape in the
        service model declares that member, on any event type, so the arm could not run
        against a real history — it was reachable only from a hand-built event. An arm
        that cannot run is not a fallback; it is a claim of coverage that is not there.
        """
        members = _model().shape_for("HistoryEvent").members
        with_scheduled = [
            name
            for name, shape in members.items()
            if hasattr(shape, "members") and "scheduledEventId" in shape.members
        ]
        assert with_scheduled == []

    def test_task_aborted_is_not_an_event_type(self):
        """The other arm removed: `TaskAborted` was matched, and does not exist. The
        real spelling is `TaskStateAborted`, which carries no error detail at all."""
        types = _Hist().types
        assert "TaskAborted" not in types
        assert "TaskStateAborted" in types


def _interleaved_branches():
    """Two `Parallel` branches in flight at once; the FIRST-entered state is the one
    that fails.

    Branch A enters, then branch B enters, then A's task fails. The iterations of a
    `Parallel` (and of a concurrent `Map`) share one history and interleave exactly like
    this, which is why the nearest-preceding-transition rule is unsound: the transition
    physically closest to A's failure belongs to B.
    """
    return (
        _Hist()
        .add("ExecutionStarted")
        .add("ParallelStateEntered", name="ProcessInParallel")
        .add("TaskStateEntered", name="BranchAExtraction")  # id 3
        .add("TaskScheduled", previous=3)  # id 4
        .add("TaskStateEntered", name="BranchBSummarisation")  # id 5
        .add("TaskScheduled", previous=5)  # id 6
        .add("TaskStarted", previous=4)  # id 7  — A's
        .add(
            "TaskFailed",
            previous=7,
            error="ExtractionBoom",
            cause='{"errorType": "ValueError", "errorMessage": "bad page"}',
        )  # id 8 — A failed
    )


@pytest.mark.unit
class TestTheCausalChainNamesTheStepThatFailed:
    """Correlation follows `previousEventId`, not position in the history."""

    def test_an_interleaved_sibling_branch_is_not_blamed(self):
        steps = parse_execution_history(_interleaved_branches().events)
        failed = [s for s in steps if s["status"] == "FAILED"]
        assert [s["name"] for s in failed] == ["BranchAExtraction"]

    def test_the_sibling_is_what_the_positional_rule_named(self):
        """The discriminator: `BranchBSummarisation` is the nearest preceding
        `TaskStateEntered`, so a positional rule returns it — and it did not fail."""
        events = _interleaved_branches().events
        failure = next(e for e in events if e["type"] == "TaskFailed")
        nearest = [
            e
            for e in events
            if e["type"] == "TaskStateEntered" and e["id"] < failure["id"]
        ][-1]
        assert nearest["stateEnteredEventDetails"]["name"] == "BranchBSummarisation"
        steps = parse_execution_history(events)
        summarisation = next(s for s in steps if s["name"] == "BranchBSummarisation")
        assert summarisation["status"] == "RUNNING"

    def test_the_chain_is_walked_not_hopped_once(self):
        """A real `TaskFailed` names `TaskStarted`, which names `TaskScheduled`, which
        names the transition — three hops. A single hop reaches none of them."""
        history = (
            _Hist()
            .add("ExecutionStarted")
            .add("TaskStateEntered", name="Classification")
            .add("TaskScheduled")
            .add("TaskStarted")
            .add("TaskFailed", error="Boom", cause="a traceback")
        )
        failure = history.events[-1]
        assert failure["previousEventId"] == 4  # TaskStarted, not the transition
        step_key = find_step_name_for_failure_event(
            failure, history.events, {2: "Classification_2"}
        )
        assert step_key == "Classification_2"

    def test_a_chain_spanning_retries_is_still_walked(self):
        """Each `Retry` attempt lengthens the chain, so any fixed hop limit is a guess
        about the retry count. Six attempts here."""
        history = _Hist().add("ExecutionStarted").add("TaskStateEntered", name="OCR")
        for _ in range(6):
            history.add("TaskScheduled").add("TaskStarted").add(
                "TaskFailed", error="Throttling", cause="rate exceeded"
            )
        failure = history.events[-1]
        assert (
            find_step_name_for_failure_event(failure, history.events, {2: "OCR_2"})
            == "OCR_2"
        )

    def test_a_cyclic_chain_terminates_instead_of_hanging(self):
        """`previousEventId` always names a lower id, and that ordering is what makes the
        walk finite. A malformed event pointing forward must not be followed."""
        history = (
            _Hist()
            .add("ExecutionStarted")
            .add("TaskStateEntered", name="OCR")
            .add("TaskScheduled", previous=4)  # points FORWARD, at itself+1
            .add("TaskFailed", previous=3, error="Boom", cause="c")
        )
        # Returns the positional answer rather than looping.
        assert (
            find_step_name_for_failure_event(
                history.events[-1], history.events, {2: "OCR_2"}
            )
            == "OCR_2"
        )


@pytest.mark.unit
class TestEveryTaskLevelFailureTypeEndsItsStep:
    """A failure type the loop did not name left its step showing RUNNING for good."""

    @pytest.mark.parametrize(
        "event_type,detail_key",
        [
            ("LambdaFunctionTimedOut", "lambdaFunctionTimedOutEventDetails"),
            (
                "LambdaFunctionScheduleFailed",
                "lambdaFunctionScheduleFailedEventDetails",
            ),
            ("TaskSubmitFailed", "taskSubmitFailedEventDetails"),
            ("TaskStartFailed", "taskStartFailedEventDetails"),
            ("ActivityFailed", "activityFailedEventDetails"),
            ("ActivityTimedOut", "activityTimedOutEventDetails"),
        ],
    )
    def test_the_step_is_marked_failed_and_carries_the_error(
        self, event_type, detail_key
    ):
        history = (
            _Hist()
            .add("ExecutionStarted")
            .add("TaskStateEntered", name="Assessment")
            .add(event_type, error="SomethingBroke", cause="the reason")
        )
        steps = parse_execution_history(history.events)
        assessment = next(s for s in steps if s["name"] == "Assessment")
        assert assessment["status"] == "FAILED", (
            f"{event_type} left the step RUNNING, so the flow viewer shows it as still "
            "in progress on a workflow that stopped"
        )
        assert assessment["error"]
        assert "SomethingBroke" in assessment["error"]

    def test_every_type_in_the_shared_vocabulary_is_handled(self):
        """Closure: the dispatch is keyed on the shared set, so this cannot drift from
        it — but it can drift from what the *messages* handle, which is what this reads."""
        for event_type in TASK_LEVEL_FAILURE_EVENTS:
            message = index._failure_message(
                event_type,
                {
                    "type": event_type,
                    failure_detail_key(event_type): {
                        "error": "E",
                        "cause": "C",
                        "state": "S",
                    },
                },
            )
            assert message, event_type
            assert "E" in message, event_type


@pytest.mark.unit
class TestTheErrorShownForTheExecutionIsTheTerminalOne:
    """After a recovered retry, the first failed step's error is one the workflow
    survived."""

    def test_the_execution_level_failure_is_preferred_over_an_earlier_step(self):
        steps = [
            {"name": "OCR", "type": "Task", "status": "FAILED", "error": "Throttling"},
            {
                "name": "Execution",
                "type": "Execution",
                "status": "FAILED",
                "error": "States.TaskFailed",
            },
        ]
        assert index._terminal_failure_error(steps) == "States.TaskFailed"

    def test_the_order_of_the_list_does_not_decide_it(self):
        """The discriminator for the rule. Today the synthetic `Execution` step happens to
        sort first — it is the one step with no `startDate`, and the sort key maps that to
        `""` — so `failed_steps[0]` returns the right answer for the wrong reason. Given
        it a start time, or any second step sorting ahead of it, and the positional form
        returns the recovered failure. This form does not depend on the order.
        """
        steps = [
            {"name": "OCR", "type": "Task", "status": "FAILED", "error": "Throttling"},
            {
                "name": "Execution",
                "type": "Execution",
                "status": "FAILED",
                "error": "States.TaskFailed",
            },
        ]
        assert index._terminal_failure_error(steps) != steps[0]["error"]
        assert (
            index._terminal_failure_error(list(reversed(steps))) == "States.TaskFailed"
        )

    def test_with_no_execution_level_step_the_latest_failed_step_wins(self):
        steps = [
            {
                "name": "OCR",
                "type": "Task",
                "status": "FAILED",
                "error": "Throttling",
                "stopDate": "2026-03-01T10:00:05",
            },
            {
                "name": "Extraction",
                "type": "Task",
                "status": "FAILED",
                "error": "Boom",
                "stopDate": "2026-03-01T10:00:09",
            },
        ]
        assert index._terminal_failure_error(steps) == "Boom"

    def test_with_no_stop_dates_at_all_the_last_failure_wins_not_the_first(self):
        """No real response produces this — a recorded failure always sets `stopDate` — but
        the tie has to break away from "first", which is the answer this function exists to
        avoid."""
        steps = [
            {"name": "OCR", "type": "Task", "status": "FAILED", "error": "Throttling"},
            {"name": "Extraction", "type": "Task", "status": "FAILED", "error": "Boom"},
        ]
        assert index._terminal_failure_error(steps) == "Boom"

    def test_nothing_failed_returns_none_rather_than_an_empty_string(self):
        """`None` and `""` are different to the caller: it only sets the key when there
        is an error, and an empty string would render as a failed execution with a blank
        reason."""
        assert index._terminal_failure_error([]) is None
        assert (
            index._terminal_failure_error(
                [{"name": "OCR", "type": "Task", "status": "SUCCEEDED", "error": None}]
            )
            is None
        )


@pytest.mark.unit
class TestTheExistingErrorTextIsUnchanged:
    """The four per-type formatters became one. These pin the strings a user reads."""

    def test_a_lambda_error_payload_still_renders_as_type_and_message(self):
        assert (
            index._failure_message(
                "TaskFailed",
                {
                    "type": "TaskFailed",
                    "taskFailedEventDetails": {
                        "error": "ValidationException",
                        "cause": '{"errorType": "ValidationException", "errorMessage": "Invalid document format"}',
                    },
                },
            )
            == "ValidationException: Invalid document format"
        )

    def test_a_stack_trace_is_appended(self):
        message = index._failure_message(
            "LambdaFunctionFailed",
            {
                "type": "LambdaFunctionFailed",
                "lambdaFunctionFailedEventDetails": {
                    "error": "Unhandled",
                    "cause": '{"errorType": "KeyError", "errorMessage": "k", "stackTrace": ["line one", "line two"]}',
                },
            },
        )
        assert message.startswith("KeyError: k")
        assert "Stack trace:\nline one\nline two" in message

    def test_a_timeout_keeps_its_wording(self):
        message = index._failure_message(
            "TaskTimedOut",
            {
                "type": "TaskTimedOut",
                "taskTimedOutEventDetails": {
                    "error": "States.Timeout",
                    "cause": "Task timed out after 300 seconds",
                },
            },
        )
        assert "Task timed out" in message
        assert "States.Timeout" in message

    def test_a_non_json_cause_is_appended_verbatim(self):
        assert (
            index._failure_message(
                "TaskFailed",
                {
                    "type": "TaskFailed",
                    "taskFailedEventDetails": {"error": "Boom", "cause": "not json"},
                },
            )
            == "Boom: not json"
        )

    def test_a_missing_error_falls_back_to_something_nameable(self):
        """Never the empty string: a red step with no reason is the worst outcome."""
        for event_type in TASK_LEVEL_FAILURE_EVENTS:
            message = index._failure_message(event_type, {"type": event_type})
            assert message, event_type


@pytest.mark.unit
class TestARecoveredRetryDoesNotLeaveARedStep:
    """A step whose task failed and then SUCCEEDED on retry must not read `FAILED`.

    This is the half of the fix that is about an execution that **worked**. A `Retry`
    re-runs the task without re-entering the state, so there is one step for every attempt,
    and nothing else in this parser takes a terminal status back: the `*StateExited` handler
    matches only a step still `RUNNING`, so once a failure sets `FAILED` the later success
    cannot reach it. The flow viewer then shows a red step — and auto-selects it, with its
    error panel — on a document that processed successfully.

    ⚠️ Widening the failure vocabulary is what made this urgent rather than pre-existing.
    Measured against the base revision, **nine** of the twelve recoverable types previously
    left the step `RUNNING` at the failure, which let `*StateExited` mark it `SUCCEEDED` —
    the correct answer: `ActivityFailed`, `ActivityScheduleFailed`, `ActivityTimedOut`,
    `LambdaFunctionScheduleFailed`, `LambdaFunctionStartFailed`, `LambdaFunctionTimedOut`,
    `MapRunFailed`, `TaskStartFailed` and `TaskSubmitFailed`. Marking them `FAILED` without
    also clearing on the later success turns a correct answer into an incorrect one. The
    remaining three — `TaskFailed`, `TaskTimedOut` and `LambdaFunctionFailed` — were in the
    base dispatch and already read `FAILED` here, so they were wrong before and are fixed
    with the other nine.

    `MapRunFailed` is the one that nearly shipped: it was first classified as having no
    retryable attempt, on the ground that a distributed map run is not re-run into success
    inside one history. `ExtractionShardMap` in `patterns/unified` is a `DISTRIBUTED` map
    with a `Retry` on the **Map state**, so it is.
    """

    #: One (failure, success) pair per task integration. The success event is the signal;
    #: see `FAILURE_RECOVERY_EVENTS` for why it is not the state-exit transition.
    RECOVERABLE = [
        ("TaskFailed", "TaskSucceeded"),
        ("TaskTimedOut", "TaskSucceeded"),
        ("TaskStartFailed", "TaskSucceeded"),
        ("TaskSubmitFailed", "TaskSucceeded"),
        ("LambdaFunctionFailed", "LambdaFunctionSucceeded"),
        ("LambdaFunctionTimedOut", "LambdaFunctionSucceeded"),
        ("LambdaFunctionScheduleFailed", "LambdaFunctionSucceeded"),
        ("LambdaFunctionStartFailed", "LambdaFunctionSucceeded"),
        ("ActivityFailed", "ActivitySucceeded"),
        ("ActivityScheduleFailed", "ActivitySucceeded"),
        ("ActivityTimedOut", "ActivitySucceeded"),
        ("MapRunFailed", "MapRunSucceeded"),
    ]

    @staticmethod
    def _recovered(failure_type, success_type):
        """One state, one failed attempt, one successful attempt, then the state exits.

        A `MapRun` lives on a `Map` state rather than a `Task` one and its run events sit
        between the transitions, so that subject gets the shape the service actually
        produces instead of a Task-shaped stand-in.
        """
        if failure_type.startswith("MapRun"):
            return (
                _Hist()
                .add("ExecutionStarted")
                .add("MapStateEntered", name="OCR")
                .add("MapStateStarted", length=3)
                .add("MapRunStarted", mapRunArn="arn:aws:states:::mapRun:sm/first")
                .add(
                    failure_type,
                    error="States.ExceedToleratedFailureThreshold",
                    cause="one shard failed",
                )
                .add("MapRunStarted", mapRunArn="arn:aws:states:::mapRun:sm/second")
                .add(success_type)
                .add("MapStateExited", name="OCR")
            )
        return (
            _Hist()
            .add("ExecutionStarted")
            .add("TaskStateEntered", name="OCR")
            .add("TaskScheduled")
            .add(failure_type, error="ThrottlingException", cause="rate exceeded")
            .add("TaskScheduled")
            .add(success_type)
            .add("TaskStateExited", name="OCR")
        )

    @pytest.mark.parametrize("failure_type,success_type", RECOVERABLE)
    def test_the_step_reads_succeeded(self, failure_type, success_type):
        steps = parse_execution_history(
            self._recovered(failure_type, success_type).events
        )
        ocr = next(s for s in steps if s["name"] == "OCR")
        assert ocr["status"] == "SUCCEEDED", (
            f"a recovered {failure_type} left the step {ocr['status']}, so the flow "
            "viewer shows a red step on an execution that succeeded"
        )
        assert ocr["error"] is None

    def test_every_recoverable_failure_type_in_the_vocabulary_is_covered(self):
        """Closure over the parametrisation, so a type added to the shared vocabulary cannot
        arrive with no recovered-retry case.

        A failure type is recoverable exactly when the service declares a
        ``<Subject>Succeeded`` for its subject — derived from `FAILURE_RECOVERY_EVENTS`
        rather than from a list of subjects, because the list is what got `MapRun` wrong.
        """
        covered = {failure_type for failure_type, _ in self.RECOVERABLE}
        recoverable_subjects = {
            event_type[: -len("Succeeded")] for event_type in FAILURE_RECOVERY_EVENTS
        }
        expected = {
            event_type
            for event_type in TASK_LEVEL_FAILURE_EVENTS
            if any(event_type.startswith(s) for s in recoverable_subjects)
        }
        assert covered == expected

    def test_each_pair_uses_the_success_event_for_its_own_subject(self):
        """A pair naming the wrong subject's success event would test nothing about the
        type it claims to cover."""
        for failure_type, success_type in self.RECOVERABLE:
            assert success_type in FAILURE_RECOVERY_EVENTS, success_type
            subject = success_type[: -len("Succeeded")]
            assert failure_type.startswith(subject), (failure_type, success_type)

    def test_no_failed_step_remains_for_the_flow_viewer_to_select(self):
        """The UI auto-selects the first `FAILED` step and opens its error panel, so what
        matters is that the response carries none at all."""
        steps = parse_execution_history(
            self._recovered("LambdaFunctionTimedOut", "LambdaFunctionSucceeded").events
        )
        assert [s for s in steps if s["status"] == "FAILED"] == []


@pytest.mark.unit
class TestClearingARecoveredFailureCannotHideARealOne:
    """The fail-safe half. Clearing on a SUCCESS event, never on `*StateExited`.

    Whether a failure routed through a `Catch` also emits a state-exit transition for the
    state that failed is a property of the service, not of its model, and is not
    established offline. The last test here is the one that matters: it supplies that
    transition, so if the service does emit one, a rule keyed on it would mark a genuinely
    failed state as succeeded — #1139 again — and this asserts that it does not.
    """

    def test_a_terminal_failure_still_reads_failed(self):
        history = (
            _Hist()
            .add("ExecutionStarted")
            .add("TaskStateEntered", name="OCR")
            .add("TaskScheduled")
            .add("TaskFailed", error="Boom", cause="a traceback")
        )
        steps = parse_execution_history(history.events)
        assert next(s for s in steps if s["name"] == "OCR")["status"] == "FAILED"

    def test_a_retry_that_failed_again_still_reads_failed(self):
        history = (
            _Hist()
            .add("ExecutionStarted")
            .add("TaskStateEntered", name="OCR")
            .add("TaskScheduled")
            .add("TaskFailed", error="Attempt1", cause="c")
            .add("TaskScheduled")
            .add("TaskFailed", error="Attempt2", cause="c")
        )
        steps = parse_execution_history(history.events)
        assert next(s for s in steps if s["name"] == "OCR")["status"] == "FAILED"

    def test_a_caught_failure_whose_state_exits_still_reads_failed(self):
        """⚠️ The assertion the design rests on. `TaskStateExited` for the failing state is
        present here; the step must stay `FAILED` regardless."""
        history = (
            _Hist()
            .add("ExecutionStarted")
            .add("TaskStateEntered", name="Extraction")
            .add("TaskScheduled")
            .add("TaskFailed", error="ExtractionBoom", cause="a traceback")
            .add("TaskStateExited", name="Extraction")
            .add("FailStateEntered", name="ExtractionShardMapFailed")
            .add("ExecutionFailed", error="States.TaskFailed", cause="propagated")
        )
        steps = parse_execution_history(history.events)
        extraction = next(s for s in steps if s["name"] == "Extraction")
        assert extraction["status"] == "FAILED"
        assert extraction["error"]

    def test_a_success_for_a_different_state_does_not_clear_this_ones_failure(self):
        """Correlation is causal, so a sibling state succeeding must not clear it."""
        history = (
            _Hist()
            .add("ExecutionStarted")
            .add("TaskStateEntered", name="Extraction")  # id 2
            .add("TaskScheduled", previous=2)  # id 3
            .add("TaskFailed", previous=3, error="Boom", cause="c")  # id 4
            .add("TaskStateEntered", name="Summarisation")  # id 5
            .add("TaskScheduled", previous=5)  # id 6
            .add("TaskSucceeded", previous=6)  # id 7 — Summarisation's
        )
        steps = parse_execution_history(history.events)
        assert next(s for s in steps if s["name"] == "Extraction")["status"] == "FAILED"


@pytest.mark.unit
class TestTheTerminalErrorIsOrderIndependentWithoutAnExecutionStep:
    """`_terminal_failure_error`'s second arm orders by `stopDate`, not list position."""

    @staticmethod
    def _two_failures():
        return [
            {
                "name": "OCR",
                "type": "Task",
                "status": "FAILED",
                "error": "RecoveredThrottling",
                "stopDate": "2026-03-01T10:00:05",
            },
            {
                "name": "Extraction",
                "type": "Task",
                "status": "FAILED",
                "error": "TerminalBoom",
                "stopDate": "2026-03-01T10:00:09",
            },
        ]

    def test_the_latest_failure_wins_in_either_order(self):
        steps = self._two_failures()
        assert index._terminal_failure_error(steps) == "TerminalBoom"
        assert index._terminal_failure_error(list(reversed(steps))) == "TerminalBoom"

    def test_a_step_with_no_stop_date_does_not_displace_one_that_has_it(self):
        steps = self._two_failures()
        steps[0]["stopDate"] = None
        assert index._terminal_failure_error(list(reversed(steps))) == "TerminalBoom"


@pytest.mark.unit
class TestTheClearOnlyAcceptsACausalCorrelation:
    """A misattributed clear ERASES a failure, which nobody can see. That asymmetry is
    why the recovered-retry clear consults the `previousEventId` chain alone.

    The failure direction and the success direction are not symmetric. `find_step_name_for_
    failure_event` falls back to a positional search and then to "the most recently
    correlated step", and when a *failure* lands on the wrong step an operator sees a red
    mark in the wrong place. When a *clear* lands on the wrong step, a real failure vanishes
    from the response. `get_execution_history` always populates `previousEventId`, so this is
    unreachable from the service — but the guard costs nothing and the comment beside it
    claims the clear "cannot" hide a failure, so something has to hold that claim.
    """

    @staticmethod
    def _failure_then_uncorrelated_success(success_previous):
        history = (
            _Hist()
            .add("ExecutionStarted")
            .add("TaskStateEntered", name="Extraction")
            .add("TaskScheduled")
            .add("TaskFailed", error="ExtractionBoom", cause="a traceback")
        )
        history.add("TaskSucceeded", previous=success_previous)
        # `_Hist.add` defaults `previous` to the preceding id, so spell out the absence.
        if success_previous is None:
            history.events[-1].pop("previousEventId", None)
        return history

    @pytest.mark.parametrize(
        "success_previous,label",
        [
            (None, "no previousEventId at all"),
            (99, "previousEventId naming an event that is not in the history"),
            (9, "previousEventId pointing forward"),
            (0, "previousEventId of zero"),
            (-1, "negative previousEventId"),
        ],
    )
    def test_an_uncorrelated_success_clears_nothing(self, success_previous, label):
        steps = parse_execution_history(
            self._failure_then_uncorrelated_success(success_previous).events
        )
        failed = [
            s for s in steps if s["status"] == "FAILED" and s["type"] != "Execution"
        ]
        assert [s["name"] for s in failed] == ["Extraction"], (
            f"a success event with {label} erased a real failure"
        )

    def test_a_boolean_previous_event_id_is_not_followed(self):
        """`bool` is an `int` subclass, so `isinstance(x, int)` admits `True`. Inert on a
        real history — id 1 is `ExecutionStarted` and never owns a step — but the guard is
        claimed, so it is measured."""
        history = self._failure_then_uncorrelated_success(None)
        history.events[-1]["previousEventId"] = True
        steps = parse_execution_history(history.events)
        failed = [
            s for s in steps if s["status"] == "FAILED" and s["type"] != "Execution"
        ]
        assert [s["name"] for s in failed] == ["Extraction"]
        assert (
            index._causal_step_key(
                history.events[-1],
                {1: "Extraction_2"},
                {e["id"]: e for e in history.events},
            )
            is None
        )

    def test_a_correlated_success_does_still_clear(self):
        """Non-vacuity: the guard must not simply disable the clear."""
        history = (
            _Hist()
            .add("ExecutionStarted")
            .add("TaskStateEntered", name="Extraction")
            .add("TaskScheduled")
            .add("TaskFailed", error="ExtractionBoom", cause="a traceback")
            .add("TaskScheduled")
            .add("TaskSucceeded")
            .add("TaskStateExited", name="Extraction")
        )
        steps = parse_execution_history(history.events)
        assert (
            next(s for s in steps if s["name"] == "Extraction")["status"] == "SUCCEEDED"
        )


@pytest.mark.unit
class TestTheCausalWalkTerminates:
    """No hop limit: `previousEventId` names a strictly lower id, so the ordering makes the
    walk finite. These are the malformed shapes that would hang or mislead a walk that
    trusted the pointer instead."""

    @staticmethod
    def _index(events):
        return {e["id"]: e for e in events}

    @pytest.mark.parametrize("previous", [None, 5, 6, 0, -1, True, "4", 4.0, 99])
    def test_a_malformed_pointer_yields_no_correlation_instead_of_hanging(
        self, previous
    ):
        events = [
            {"id": 1, "type": "TaskStateEntered"},
            {"id": 5, "type": "TaskSucceeded", "previousEventId": previous},
        ]
        assert (
            index._causal_step_key(events[-1], {1: "OCR_1"}, self._index(events))
            is None
        )

    def test_a_two_cycle_terminates(self):
        events = [
            {"id": 1, "type": "TaskStateEntered"},
            {"id": 4, "type": "TaskScheduled", "previousEventId": 5},
            {"id": 5, "type": "TaskSucceeded", "previousEventId": 4},
        ]
        assert (
            index._causal_step_key(events[-1], {1: "OCR_1"}, self._index(events))
            is None
        )

    def test_a_long_chain_is_walked_to_the_end(self):
        """The reason there is no hop limit: chain length grows with the retry count."""
        events = [{"id": 1, "type": "TaskStateEntered"}]
        for i in range(2, 2002):
            events.append({"id": i, "type": "TaskScheduled", "previousEventId": i - 1})
        assert (
            index._causal_step_key(events[-1], {1: "OCR_1"}, self._index(events))
            == "OCR_1"
        )


@pytest.mark.unit
class TestACaughtFailureOnASucceededExecutionReadsFailed:
    """⚠️ The third operator-visible change on an execution that succeeded, pinned.

    A `Catch` that routes onto a recovery path means "this step failed and the workflow
    handled it", and the honest picture is a red step inside a green execution. Base already
    showed that for `TaskFailed`, `TaskTimedOut` and `LambdaFunctionFailed` — the three types
    its dispatch covered. For the **other ten** it showed `RUNNING` (a step still in progress
    on a finished execution) or, where the failing state also emitted `TaskStateExited`,
    `SUCCEEDED` — which is the one answer that is plainly wrong, since the state did fail.

    These now all read `FAILED`, which makes the ten consistent with the three rather than
    introducing a new behaviour. It is **not** the recovered-retry case: no later attempt
    succeeded, so there is nothing to clear.

    Parametrised over the **whole** task-level vocabulary, including `EvaluationFailed` and
    `MapRunFailed`. An earlier form of this class carved those two out as "neither is a
    Task-state failure", which is true of `MapRunFailed` — it belongs to a `Map` state, so it
    gets the Map-shaped history — and false of `EvaluationFailed`, whose state can be a
    `Task` and which is exactly the type most likely to be mis-handled, since it is the one
    that names its own state.
    """

    @staticmethod
    def _caught_then_recovered(failure_type, with_exit):
        if failure_type.startswith("MapRun"):
            history = (
                _Hist()
                .add("ExecutionStarted")
                .add("MapStateEntered", name="ExtractionShardMap")
                .add("MapStateStarted", length=3)
                .add("MapRunStarted", mapRunArn="arn:aws:states:::mapRun:sm/only")
                .add(failure_type, error="Boom", cause="a traceback")
            )
            if with_exit:
                history.add("MapStateExited", name="ExtractionShardMap")
            failing_state_name = "ExtractionShardMap"
        else:
            history = (
                _Hist()
                .add("ExecutionStarted")
                .add("TaskStateEntered", name="EvaluationStep")
                .add("TaskScheduled")
            )
            if failure_type == "EvaluationFailed":
                history.add(
                    failure_type, state="EvaluationStep", error="Boom", cause="c"
                )
            else:
                history.add(failure_type, error="Boom", cause="a traceback")
            if with_exit:
                history.add("TaskStateExited", name="EvaluationStep")
            failing_state_name = "EvaluationStep"
        history.add("TaskStateEntered", name="RecordEvaluationFailure").add(
            "TaskStateExited", name="RecordEvaluationFailure"
        ).add("ExecutionSucceeded")
        return history, failing_state_name

    @pytest.mark.parametrize("failure_type", sorted(TASK_LEVEL_FAILURE_EVENTS))
    @pytest.mark.parametrize("with_exit", [False, True])
    def test_the_failing_step_reads_failed(self, failure_type, with_exit):
        history, failing_state_name = self._caught_then_recovered(
            failure_type, with_exit
        )
        steps = parse_execution_history(history.events)
        failing = next(s for s in steps if s["name"] == failing_state_name)
        assert failing["status"] == "FAILED", (
            f"{failure_type} with_exit={with_exit}: a state that failed reads "
            f"{failing['status']} on a succeeded execution"
        )
        assert failing["error"]

    def test_the_parametrisation_covers_the_whole_vocabulary(self):
        """Closure, so a type added to the shared set cannot arrive uncovered here."""
        assert len(TASK_LEVEL_FAILURE_EVENTS) > 10

    def test_the_handler_step_is_not_the_one_marked_failed(self):
        """The #1139 shape at step scope: the state the `Catch` moved into succeeded."""
        history, _ = self._caught_then_recovered("LambdaFunctionTimedOut", True)
        steps = parse_execution_history(history.events)
        handler = next(s for s in steps if s["name"] == "RecordEvaluationFailure")
        assert handler["status"] == "SUCCEEDED"

    def test_the_execution_carries_no_top_level_error(self):
        """The envelope is unchanged: the top-level error is gated on the execution's own
        status, so a succeeded execution gains nothing from a red step."""
        history, _ = self._caught_then_recovered("LambdaFunctionTimedOut", True)
        steps = parse_execution_history(history.events)
        assert not [s for s in steps if s["type"] == "Execution"]
