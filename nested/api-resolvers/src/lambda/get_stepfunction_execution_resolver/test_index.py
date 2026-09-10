# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
from datetime import datetime
from unittest.mock import MagicMock

import index
import pytest
from index import find_step_name_for_failure_event, parse_execution_history


@pytest.mark.unit
def test_parse_execution_history_with_failure():
    """Test that parse_execution_history correctly handles task failures"""
    
    # Mock execution history events that simulate a failure scenario
    events = [
        {
            'id': 1,
            'type': 'ExecutionStarted',
            'timestamp': datetime(2024, 1, 1, 10, 0, 0),
            'executionStartedEventDetails': {}
        },
        {
            'id': 2,
            'type': 'TaskStateEntered',
            'timestamp': datetime(2024, 1, 1, 10, 0, 1),
            'stateEnteredEventDetails': {
                'name': 'ClassificationStep',
                'input': '{"document": "test.pdf"}'
            }
        },
        {
            'id': 3,
            'type': 'TaskFailed',
            'timestamp': datetime(2024, 1, 1, 10, 0, 5),
            'previousEventId': 2,
            'taskFailedEventDetails': {
                'error': 'ValidationException',
                'cause': '{"errorType": "ValidationException", "errorMessage": "Invalid document format"}'
            }
        }
    ]
    
    steps = parse_execution_history(events)
    
    # Should have one step
    assert len(steps) == 1
    
    # Check step details
    step = steps[0]
    assert step['name'] == 'ClassificationStep'
    assert step['status'] == 'FAILED'
    assert step['error'] == 'ValidationException: Invalid document format'
    assert step['startDate'] == '2024-01-01T10:00:01'
    assert step['stopDate'] == '2024-01-01T10:00:05'

@pytest.mark.unit
def test_parse_execution_history_with_success():
    """Test that parse_execution_history correctly handles successful execution"""
    
    events = [
        {
            'id': 1,
            'type': 'ExecutionStarted',
            'timestamp': datetime(2024, 1, 1, 10, 0, 0),
            'executionStartedEventDetails': {}
        },
        {
            'id': 2,
            'type': 'TaskStateEntered',
            'timestamp': datetime(2024, 1, 1, 10, 0, 1),
            'stateEnteredEventDetails': {
                'name': 'ClassificationStep',
                'input': '{"document": "test.pdf"}'
            }
        },
        {
            'id': 3,
            'type': 'TaskStateExited',
            'timestamp': datetime(2024, 1, 1, 10, 0, 5),
            'stateExitedEventDetails': {
                'name': 'ClassificationStep',
                'output': '{"classification": "invoice"}'
            }
        }
    ]
    
    steps = parse_execution_history(events)
    
    # Should have one step
    assert len(steps) == 1
    
    # Check step details
    step = steps[0]
    assert step['name'] == 'ClassificationStep'
    assert step['status'] == 'SUCCEEDED'
    assert step['error'] is None
    assert step['output'] == '{"classification": "invoice"}'

@pytest.mark.unit
def test_find_step_name_for_failure_event():
    """Test that find_step_name_for_failure_event correctly identifies the failed step"""
    
    failure_event = {
        'id': 3,
        'type': 'TaskFailed',
        'previousEventId': 2,
        'taskFailedEventDetails': {
            'error': 'ValidationException',
            'cause': 'Invalid input'
        }
    }
    
    all_events = [
        {
            'id': 1,
            'type': 'ExecutionStarted',
            'timestamp': datetime(2024, 1, 1, 10, 0, 0)
        },
        {
            'id': 2,
            'type': 'TaskStateEntered',
            'timestamp': datetime(2024, 1, 1, 10, 0, 1),
            'stateEnteredEventDetails': {
                'name': 'ClassificationStep'
            }
        },
        failure_event
    ]
    
    event_id_to_step = {2: 'ClassificationStep'}
    
    step_name = find_step_name_for_failure_event(failure_event, all_events, event_id_to_step)
    
    assert step_name == 'ClassificationStep'

@pytest.mark.unit
def test_parse_execution_history_with_timeout():
    """Test that parse_execution_history correctly handles task timeouts"""
    
    events = [
        {
            'id': 1,
            'type': 'ExecutionStarted',
            'timestamp': datetime(2024, 1, 1, 10, 0, 0),
            'executionStartedEventDetails': {}
        },
        {
            'id': 2,
            'type': 'TaskStateEntered',
            'timestamp': datetime(2024, 1, 1, 10, 0, 1),
            'stateEnteredEventDetails': {
                'name': 'ProcessingStep',
                'input': '{"document": "test.pdf"}'
            }
        },
        {
            'id': 3,
            'type': 'TaskTimedOut',
            'timestamp': datetime(2024, 1, 1, 10, 5, 0),
            'previousEventId': 2,
            'taskTimedOutEventDetails': {
                'error': 'States.Timeout',
                'cause': 'Task timed out after 300 seconds'
            }
        }
    ]
    
    steps = parse_execution_history(events)
    
    # Should have one step
    assert len(steps) == 1
    
    # Check step details
    step = steps[0]
    assert step['name'] == 'ProcessingStep'
    assert step['status'] == 'FAILED'
    assert 'Task timed out' in step['error']
    assert step['startDate'] == '2024-01-01T10:00:01'
    assert step['stopDate'] == '2024-01-01T10:05:00'

@pytest.mark.unit
def test_parse_execution_history_multiple_steps():
    """Test parsing execution history with multiple steps including failures"""
    
    events = [
        {
            'id': 1,
            'type': 'ExecutionStarted',
            'timestamp': datetime(2024, 1, 1, 10, 0, 0),
            'executionStartedEventDetails': {}
        },
        # First step - succeeds
        {
            'id': 2,
            'type': 'TaskStateEntered',
            'timestamp': datetime(2024, 1, 1, 10, 0, 1),
            'stateEnteredEventDetails': {
                'name': 'UploadStep',
                'input': '{"document": "test.pdf"}'
            }
        },
        {
            'id': 3,
            'type': 'TaskStateExited',
            'timestamp': datetime(2024, 1, 1, 10, 0, 3),
            'stateExitedEventDetails': {
                'name': 'UploadStep',
                'output': '{"uploaded": true}'
            }
        },
        # Second step - fails
        {
            'id': 4,
            'type': 'TaskStateEntered',
            'timestamp': datetime(2024, 1, 1, 10, 0, 4),
            'stateEnteredEventDetails': {
                'name': 'ClassificationStep',
                'input': '{"document": "test.pdf"}'
            }
        },
        {
            'id': 5,
            'type': 'TaskFailed',
            'timestamp': datetime(2024, 1, 1, 10, 0, 8),
            'previousEventId': 4,
            'taskFailedEventDetails': {
                'error': 'ValidationException',
                'cause': '{"errorType": "ValidationException", "errorMessage": "Invalid document format"}'
            }
        }
    ]
    
    steps = parse_execution_history(events)
    
    # Should have two steps
    assert len(steps) == 2
    
    # Check first step (successful)
    upload_step = steps[0]
    assert upload_step['name'] == 'UploadStep'
    assert upload_step['status'] == 'SUCCEEDED'
    assert upload_step['error'] is None
    
    # Check second step (failed)
    classification_step = steps[1]
    assert classification_step['name'] == 'ClassificationStep'
    assert classification_step['status'] == 'FAILED'
    assert classification_step['error'] == 'ValidationException: Invalid document format'


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


def _execution_arn(state_machine=THIS_SM, execution_id="11111111-2222-3333-4444-555555555555"):
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
    def _configure(allowed_versions):
        table = MagicMock()
        table.query.return_value = {
            "Items": [{"allowedConfigVersions": allowed_versions}] if allowed_versions else []
        }
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
        arn = _execution_arn(state_machine="OTHER-STACK-DocumentProcessingStateMachine-QQQ")

        with pytest.raises(PermissionError):
            index.lambda_handler(_event(arn), None)

    def test_does_not_call_step_functions_for_a_rejected_arn(self, sfn):
        """A rejected ARN is never sent to the Step Functions data plane."""
        arn = _execution_arn(state_machine="OTHER-STACK-DocumentProcessingStateMachine-QQQ")

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

    def test_accepts_an_execution_of_this_state_machine(self, sfn, monkeypatch):
        monkeypatch.delenv("USERS_TABLE_NAME", raising=False)

        result = index.lambda_handler(_event(_execution_arn()), None)

        assert result["status"] == "SUCCEEDED"
        sfn.describe_execution.assert_called_once()

    def test_accepts_a_distributed_map_child_execution(self, sfn, monkeypatch):
        """`NAME/mapRunLabel` in the ARN still names this state machine."""
        monkeypatch.delenv("USERS_TABLE_NAME", raising=False)
        arn = _execution_arn(state_machine=f"{THIS_SM}/mapRunLabel")
        sfn.describe_execution.return_value = _describe_response(execution_arn=arn)

        result = index.lambda_handler(_event(arn), None)

        assert result["status"] == "SUCCEEDED"


@pytest.mark.unit
class TestConfigVersionScope:
    def test_scoped_caller_may_read_an_in_scope_execution(self, sfn, scoped_caller):
        scoped_caller(["tenant-a"])
        sfn.describe_execution.return_value = _describe_response(config_version="tenant-a")

        result = index.lambda_handler(_event(_execution_arn()), None)

        assert result["status"] == "SUCCEEDED"

    def test_scoped_caller_may_not_read_an_out_of_scope_execution(self, sfn, scoped_caller):
        """An out-of-scope execution names another tenant's document and state URI."""
        scoped_caller(["tenant-a"])
        sfn.describe_execution.return_value = _describe_response(config_version="tenant-b")

        with pytest.raises(PermissionError):
            index.lambda_handler(_event(_execution_arn()), None)

    def test_scoped_caller_denied_when_execution_names_no_version(self, sfn, scoped_caller):
        """No version to authorize against means no authorization — deny."""
        scoped_caller(["tenant-a"])
        sfn.describe_execution.return_value = _describe_response(config_version=None)

        with pytest.raises(PermissionError):
            index.lambda_handler(_event(_execution_arn()), None)

    def test_unscoped_caller_reads_any_execution_of_this_stack(self, sfn, scoped_caller):
        """An empty allowedConfigVersions list means unrestricted, not denied."""
        scoped_caller([])
        sfn.describe_execution.return_value = _describe_response(config_version="tenant-b")

        result = index.lambda_handler(_event(_execution_arn()), None)

        assert result["status"] == "SUCCEEDED"

    def test_admin_is_not_config_scoped(self, sfn, scoped_caller):
        table = scoped_caller(["tenant-a"])
        sfn.describe_execution.return_value = _describe_response(config_version="tenant-b")

        result = index.lambda_handler(_event(_execution_arn(), groups=["Admin"]), None)

        assert result["status"] == "SUCCEEDED"
        table.query.assert_not_called()

    def test_no_users_table_configured_is_unrestricted(self, sfn, monkeypatch):
        """Pre-RBAC / single-user deployments keep working (fail-open, AUTH.T07)."""
        monkeypatch.delenv("USERS_TABLE_NAME", raising=False)
        sfn.describe_execution.return_value = _describe_response(config_version="tenant-b")

        result = index.lambda_handler(_event(_execution_arn()), None)

        assert result["status"] == "SUCCEEDED"

    def test_scope_lookup_is_cached_per_caller(self, sfn, scoped_caller):
        """A flow-viewer poll loop must not re-Query UsersTable on every call."""
        table = scoped_caller(["tenant-a"])
        sfn.describe_execution.return_value = _describe_response(config_version="tenant-a")

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
