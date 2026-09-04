# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import importlib.util
import json
import os
from unittest.mock import patch

import pytest

# Mock environment variables and dependencies before importing
with patch.dict(
    os.environ,
    {
        "TRACKING_TABLE": "test-table",
        "CONFIG_TABLE": "test-config-table",
        "FILE_COPY_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue",
        "AWS_REGION": "us-east-1",
    },
):
    with patch("boto3.client"), patch("boto3.resource"):
        # Import the specific lambda module
        spec = importlib.util.spec_from_file_location(
            "test_runner_index",
            os.path.join(
                os.path.dirname(__file__),
                "../../../../nested/api-resolvers/src/lambda/test_runner/index.py",
            ),
        )
        if spec is None or spec.loader is None:
            raise ImportError("Could not load test_runner module")
        test_runner_index = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(test_runner_index)


# Test Studio test runner operations are Admin+Author; supply an authorized
# Cognito identity on handler events so the defense-in-depth group gate passes.
_ADMIN_IDENTITY = {
    "claims": {"cognito:groups": ["Admin"], "email": "admin@example.com"}
}


@pytest.mark.unit
class TestTestRunnerRBAC:
    """RBAC tests for test runner Lambda function"""

    def test_handler_rejects_viewer(self):
        """Defense-in-depth: a Viewer must not reach startTestRun operation."""
        event = {
            "arguments": {
                "input": {
                    "testSetId": "test-set-123",
                    "context": "test",
                }
            },
            "identity": {"claims": {"cognito:groups": ["Viewer"]}},
        }
        with pytest.raises(Exception, match="requires Admin or Author group"):
            test_runner_index.handler(event, {})

    @patch.dict(
        os.environ,
        {
            "TRACKING_TABLE": "test-table",
            "CONFIG_TABLE": "test-config-table",
            "FILE_COPY_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue",
        },
    )
    def test_handler_allows_direct_lambda_invoke_no_identity(self):
        """RBAC bypass: direct Lambda invocation (no identity) proceeds for CI/automation."""
        with (
            patch.object(test_runner_index, "_get_test_set") as mock_get_test_set,
            patch.object(test_runner_index, "_capture_config") as mock_capture_config,
            patch.object(
                test_runner_index, "_active_config_version", return_value="v1"
            ),
            patch.object(
                test_runner_index, "_store_test_run_metadata"
            ) as _mock_store_metadata,
            patch.object(test_runner_index.sqs, "send_message") as mock_sqs,
            patch("datetime.datetime") as mock_datetime,
        ):
            # Mock return values
            mock_get_test_set.return_value = {
                "name": "Test-Set",
                "fileCount": 3,
            }
            mock_capture_config.return_value = {"Config": {"key": "value"}}
            mock_datetime.utcnow.return_value.strftime.return_value = "20260611-120000"

            # Direct Lambda invoke: no 'identity' field (CI/automation path)
            event = {
                "arguments": {
                    "input": {
                        "testSetId": "test-set-123",
                        "context": "CI test",
                    }
                }
            }

            # Should NOT raise - bypass works as designed
            result = test_runner_index.handler(event, {})
            # RBAC bypass worked - we got a result instead of "Unauthorized" exception
            assert "testRunId" in result
            assert result["status"] == "QUEUED"
            mock_sqs.assert_called_once()

    @patch.dict(
        os.environ,
        {
            "TRACKING_TABLE": "test-table",
            "CONFIG_TABLE": "test-config-table",
            "FILE_COPY_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue",
        },
    )
    def test_handler_allows_direct_lambda_invoke_identity_none(self):
        """RBAC bypass: direct Lambda invocation (identity=None) proceeds for CI/automation."""
        with (
            patch.object(test_runner_index, "_get_test_set") as mock_get_test_set,
            patch.object(test_runner_index, "_capture_config") as mock_capture_config,
            patch.object(
                test_runner_index, "_active_config_version", return_value="v1"
            ),
            patch.object(
                test_runner_index, "_store_test_run_metadata"
            ) as _mock_store_metadata,
            patch.object(test_runner_index.sqs, "send_message") as mock_sqs,
            patch("datetime.datetime") as mock_datetime,
        ):
            # Mock return values
            mock_get_test_set.return_value = {
                "name": "Test-Set",
                "fileCount": 3,
            }
            mock_capture_config.return_value = {"Config": {"key": "value"}}
            mock_datetime.utcnow.return_value.strftime.return_value = "20260611-120000"

            # Direct Lambda invoke: identity explicitly None
            event = {
                "arguments": {
                    "input": {
                        "testSetId": "test-set-123",
                        "context": "CI test",
                    }
                },
                "identity": None,
            }

            # Should NOT raise - bypass works as designed
            result = test_runner_index.handler(event, {})
            # RBAC bypass worked - we got a result instead of "Unauthorized" exception
            assert "testRunId" in result
            assert result["status"] == "QUEUED"
            mock_sqs.assert_called_once()

    def test_handler_still_enforces_rbac_for_appsync_viewer(self):
        """Regression guard: AppSync invocation with non-Admin/Author still raises."""
        event = {
            "arguments": {
                "input": {
                    "testSetId": "test-set-123",
                    "context": "test",
                }
            },
            "identity": {"claims": {"cognito:groups": ["Viewer"]}},
        }
        with pytest.raises(Exception, match="requires Admin or Author group"):
            test_runner_index.handler(event, {})

    @patch.dict(
        os.environ,
        {
            "TRACKING_TABLE": "test-table",
            "CONFIG_TABLE": "test-config-table",
            "FILE_COPY_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue",
        },
    )
    def test_handler_allows_admin(self):
        """Admin user can invoke startTestRun via AppSync."""
        with (
            patch.object(test_runner_index, "_get_test_set") as mock_get_test_set,
            patch.object(test_runner_index, "_capture_config") as mock_capture_config,
            patch.object(
                test_runner_index, "_active_config_version", return_value="v1"
            ),
            patch.object(
                test_runner_index, "_store_test_run_metadata"
            ) as _mock_store_metadata,
            patch.object(test_runner_index.sqs, "send_message") as mock_sqs,
            patch("datetime.datetime") as mock_datetime,
        ):
            # Mock return values
            mock_get_test_set.return_value = {
                "name": "Test-Set",
                "fileCount": 3,
            }
            mock_capture_config.return_value = {"Config": {"key": "value"}}
            mock_datetime.utcnow.return_value.strftime.return_value = "20260611-120000"

            event = {
                "arguments": {
                    "input": {
                        "testSetId": "test-set-123",
                        "context": "UI test",
                    }
                },
                "identity": _ADMIN_IDENTITY,
            }

            # Should succeed - Admin has permission
            result = test_runner_index.handler(event, {})
            # Admin RBAC check passed - we got a result
            assert "testRunId" in result
            assert result["status"] == "QUEUED"
            mock_sqs.assert_called_once()

    @patch.dict(
        os.environ,
        {
            "TRACKING_TABLE": "test-table",
            "CONFIG_TABLE": "test-config-table",
            "FILE_COPY_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue",
        },
    )
    def test_handler_allows_author(self):
        """Author user can invoke startTestRun via AppSync."""
        with (
            patch.object(test_runner_index, "_get_test_set") as mock_get_test_set,
            patch.object(test_runner_index, "_capture_config") as mock_capture_config,
            patch.object(
                test_runner_index, "_active_config_version", return_value="v1"
            ),
            patch.object(
                test_runner_index, "_store_test_run_metadata"
            ) as _mock_store_metadata,
            patch.object(test_runner_index.sqs, "send_message") as mock_sqs,
            patch("datetime.datetime") as mock_datetime,
        ):
            # Mock return values
            mock_get_test_set.return_value = {
                "name": "Test-Set",
                "fileCount": 3,
            }
            mock_capture_config.return_value = {"Config": {"key": "value"}}
            mock_datetime.utcnow.return_value.strftime.return_value = "20260611-120000"

            event = {
                "arguments": {
                    "input": {
                        "testSetId": "test-set-123",
                        "context": "UI test",
                    }
                },
                "identity": {
                    "claims": {
                        "cognito:groups": ["Author"],
                        "email": "author@example.com",
                    }
                },
            }

            # Should succeed - Author has permission
            result = test_runner_index.handler(event, {})
            # Author RBAC check passed - we got a result
            assert "testRunId" in result
            assert result["status"] == "QUEUED"
            mock_sqs.assert_called_once()

    @patch.dict(
        os.environ,
        {
            "TRACKING_TABLE": "test-table",
            "CONFIG_TABLE": "test-config-table",
            "FILE_COPY_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue",
        },
    )
    def test_scores_current_labels_by_default_not_the_active_reference(self):
        """A run started mid-annotation must see the corrections.

        Pinning to activeReference by default made the ordinary loop — correct
        documents, run, look for the improvement — silently score the labels from
        before the corrections, once a snapshot existed for that version. Current
        labels are the default; the open draft is recorded so the result can say so.
        """
        with (
            patch.object(test_runner_index, "_get_test_set") as mock_get_test_set,
            patch.object(test_runner_index, "_capture_config") as mock_capture_config,
            patch.object(
                test_runner_index, "_active_config_version", return_value="v1"
            ),
            patch.object(
                test_runner_index, "_store_test_run_metadata"
            ) as mock_store_metadata,
            patch.object(test_runner_index.sqs, "send_message") as mock_send,
            patch("datetime.datetime") as mock_datetime,
        ):
            mock_get_test_set.return_value = {
                "name": "Test-Set",
                "fileCount": 3,
                "activeReference": 2,
                "latestVersion": 2,
                "draftVersion": 3,
            }
            mock_capture_config.return_value = {"Config": {}}
            mock_datetime.utcnow.return_value.strftime.return_value = "20260611-120000"

            event = {
                "arguments": {"input": {"testSetId": "ts1"}},
                "identity": _ADMIN_IDENTITY,
            }
            test_runner_index.handler(event, {})

            args, kwargs = mock_store_metadata.call_args
            assert args[-1] is None, "no version may be pinned unless asked for"
            assert kwargs["test_set_draft_version"] == 3
            body = json.loads(mock_send.call_args.kwargs["MessageBody"])
            # The copier reads live baselines when no version is in the message.
            assert "testSetVersion" not in body

    @patch.dict(
        os.environ,
        {
            "TRACKING_TABLE": "test-table",
            "CONFIG_TABLE": "test-config-table",
            "FILE_COPY_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue",
        },
    )
    def test_pins_a_requested_version_into_the_run_and_the_copy_job(self):
        with (
            patch.object(test_runner_index, "_get_test_set") as mock_get_test_set,
            patch.object(test_runner_index, "_capture_config") as mock_capture_config,
            patch.object(
                test_runner_index, "_active_config_version", return_value="v1"
            ),
            patch.object(
                test_runner_index, "_store_test_run_metadata"
            ) as mock_store_metadata,
            patch.object(test_runner_index.sqs, "send_message") as mock_send,
            patch("datetime.datetime") as mock_datetime,
        ):
            mock_get_test_set.return_value = {
                "name": "Test-Set",
                "fileCount": 3,
                "activeReference": 2,
                "latestVersion": 2,
                "draftVersion": 3,
            }
            mock_capture_config.return_value = {"Config": {}}
            mock_datetime.utcnow.return_value.strftime.return_value = "20260611-120000"

            event = {
                "arguments": {"input": {"testSetId": "ts1", "testSetVersion": 1}},
                "identity": _ADMIN_IDENTITY,
            }
            test_runner_index.handler(event, {})

            args, kwargs = mock_store_metadata.call_args
            assert args[-1] == 1
            # Pinned means pinned: the draft is not what was scored.
            assert kwargs["test_set_draft_version"] is None
            body = json.loads(mock_send.call_args.kwargs["MessageBody"])
            assert body["testSetVersion"] == 1

    @patch.dict(
        os.environ,
        {
            "TRACKING_TABLE": "test-table",
            "CONFIG_TABLE": "test-config-table",
            "FILE_COPY_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue",
        },
    )
    def test_refuses_a_version_the_set_does_not_have(self):
        # Otherwise the copier finds no snapshot and falls back to live baselines,
        # and the run would claim to have scored a version that does not exist.
        with (
            patch.object(test_runner_index, "_get_test_set") as mock_get_test_set,
            patch.object(test_runner_index, "_capture_config"),
            patch.object(
                test_runner_index, "_active_config_version", return_value="v1"
            ),
            patch.object(test_runner_index, "_store_test_run_metadata") as store,
            patch.object(test_runner_index.sqs, "send_message") as mock_send,
        ):
            mock_get_test_set.return_value = {
                "name": "Test-Set",
                "fileCount": 3,
                "latestVersion": 2,
            }
            event = {
                "arguments": {"input": {"testSetId": "ts1", "testSetVersion": 5}},
                "identity": _ADMIN_IDENTITY,
            }
            with pytest.raises(Exception, match="does not exist"):
                test_runner_index.handler(event, {})
            store.assert_not_called()
            mock_send.assert_not_called()

    @patch.dict(
        os.environ,
        {
            "TRACKING_TABLE": "test-table",
            "CONFIG_TABLE": "test-config-table",
            "FILE_COPY_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue",
        },
    )
    def test_unpublished_test_set_pins_no_version(self):
        """A test set never published pins test_set_version=None."""
        with (
            patch.object(test_runner_index, "_get_test_set") as mock_get_test_set,
            patch.object(test_runner_index, "_capture_config") as mock_capture_config,
            patch.object(
                test_runner_index, "_active_config_version", return_value="v1"
            ),
            patch.object(
                test_runner_index, "_store_test_run_metadata"
            ) as mock_store_metadata,
            patch.object(test_runner_index.sqs, "send_message"),
            patch("datetime.datetime") as mock_datetime,
        ):
            mock_get_test_set.return_value = {"name": "Test-Set", "fileCount": 3}
            mock_capture_config.return_value = {"Config": {}}
            mock_datetime.utcnow.return_value.strftime.return_value = "20260611-120000"

            event = {
                "arguments": {"input": {"testSetId": "ts1"}},
                "identity": _ADMIN_IDENTITY,
            }
            test_runner_index.handler(event, {})

            args, _ = mock_store_metadata.call_args
            assert args[-1] is None

    # -- send_test_run_to_review (on-demand HITL trigger) -----------------

    @patch.dict(os.environ, {"TRACKING_TABLE": "test-table"})
    def test_send_test_run_to_review_queues_docs_with_alerts(self):
        """Docs with confidence alerts are flipped to PendingReview; clean ones skipped."""
        from unittest.mock import MagicMock

        table = MagicMock()
        # run metadata with two files
        run_item = {"TestSetId": "ts1", "Files": ["a.pdf", "b.pdf"]}
        doc_a = {"ConfidenceAlertCount": 3, "HITLStatus": ""}  # should queue
        doc_b = {"ConfidenceAlertCount": 0, "HITLStatus": ""}  # no alerts -> skip

        def _get_item(Key):
            sk_pk = Key["PK"]
            if sk_pk == "testrun#run-1":
                return {"Item": run_item}
            if sk_pk == "doc#run-1/a.pdf":
                return {"Item": doc_a}
            if sk_pk == "doc#run-1/b.pdf":
                return {"Item": doc_b}
            return {}

        table.get_item.side_effect = _get_item
        with patch.object(test_runner_index.dynamodb, "Table", return_value=table):
            result = test_runner_index.send_test_run_to_review({"testRunId": "run-1"})

        assert result["queuedCount"] == 1
        assert result["skippedCount"] == 1
        assert result["testSetId"] == "ts1"
        # exactly one doc updated, and it's a.pdf -> PendingReview + TestSetId
        assert table.update_item.call_count == 1
        upd = table.update_item.call_args.kwargs
        assert upd["Key"]["PK"] == "doc#run-1/a.pdf"
        assert upd["ExpressionAttributeValues"][":s"] == "PendingReview"
        assert upd["ExpressionAttributeValues"][":tsid"] == "ts1"

    @patch.dict(os.environ, {"TRACKING_TABLE": "test-table"})
    def test_send_test_run_to_review_skips_completed(self):
        """Already-reviewed docs are not re-queued."""
        from unittest.mock import MagicMock

        table = MagicMock()
        run_item = {"TestSetId": "ts1", "Files": ["done.pdf"]}
        doc = {"ConfidenceAlertCount": 5, "HITLStatus": "Review Completed"}

        def _get_item(Key):
            if Key["PK"] == "testrun#run-2":
                return {"Item": run_item}
            return {"Item": doc}

        table.get_item.side_effect = _get_item
        with patch.object(test_runner_index.dynamodb, "Table", return_value=table):
            result = test_runner_index.send_test_run_to_review({"testRunId": "run-2"})

        assert result["queuedCount"] == 0
        assert result["skippedCount"] == 1
        assert table.update_item.call_count == 0

    @patch.dict(os.environ, {"TRACKING_TABLE": "test-table"})
    def test_send_test_run_to_review_missing_run_raises(self):
        from unittest.mock import MagicMock

        table = MagicMock()
        table.get_item.return_value = {}
        with patch.object(test_runner_index.dynamodb, "Table", return_value=table):
            with pytest.raises(ValueError, match="Test run 'ghost' not found"):
                test_runner_index.send_test_run_to_review({"testRunId": "ghost"})
