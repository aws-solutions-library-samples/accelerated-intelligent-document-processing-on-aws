"""Unit tests for job_tracker Lambda function."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add idp_common to path and import real Status
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../lib/idp_common_pkg"))
from idp_common.models import Status, document_wrapper_status, terminal_document_status

# Mock idp_common before importing index. `job_completion` is deliberately NOT
# mocked: it holds the all-files-terminal check and the zip build that this
# handler's behaviour is almost entirely made of, so stubbing it would leave
# these tests asserting against a MagicMock rather than the logic.
mock_job_service = MagicMock()
mock_job_service_module = MagicMock()
mock_job_service_module.create_job_service = MagicMock(return_value=mock_job_service)
mock_models_module = MagicMock()
mock_models_module.Status = Status
mock_models_module.terminal_document_status = terminal_document_status
mock_models_module.document_wrapper_status = document_wrapper_status

sys.modules["idp_common.job_service"] = mock_job_service_module
sys.modules["idp_common.models"] = mock_models_module


@pytest.fixture(autouse=True)
def mock_env():
    """Set up environment variables for tests."""
    env_vars = {
        "OUTPUT_BUCKET": "test-output-bucket",
        "TRACKING_TABLE": "test-table",
        "LOG_LEVEL": "INFO",
    }
    with patch.dict(os.environ, env_vars):
        yield


@pytest.fixture
def mock_s3():
    """Mock S3 client."""
    with patch("index.s3") as mock:
        yield mock


@pytest.fixture
def job_svc():
    """Reset and return mock job service."""
    mock_job_service.reset_mock()
    with patch("index.job_service", mock_job_service):
        yield mock_job_service


def make_event(doc_id: str, status: str = "SUCCEEDED") -> dict:
    """Create a Step Functions completion event."""
    return {
        "detail": {
            "input": json.dumps({"document": {"document_id": doc_id}}),
            "status": status,
        }
    }


class TestHandler:
    """Tests for handler function."""

    def test_skips_non_job_document(self, mock_s3, job_svc):
        """Test handler skips documents not in jobs/ prefix."""
        from index import handler

        event = make_event("regular/document.pdf")
        response = handler(event, None)

        assert response["statusCode"] == 200
        assert "Not a job document" in response["body"]
        job_svc.update_file_status.assert_not_called()

    def test_updates_file_status_succeeded(self, mock_s3, job_svc):
        """Test handler updates file status to COMPLETED on SUCCEEDED."""
        from index import handler

        job_svc.update_file_status.return_value = {"doc.pdf": "COMPLETED", "other.pdf": "EXTRACTING"}

        event = make_event("jobs/test-uuid/doc.pdf", "SUCCEEDED")
        response = handler(event, None)

        assert response["statusCode"] == 200
        job_svc.update_file_status.assert_called_once_with("test-uuid", "doc.pdf", Status.COMPLETED)

    def test_updates_file_status_failed(self, mock_s3, job_svc):
        """Test handler updates file status to FAILED on FAILED."""
        from index import handler

        job_svc.update_file_status.return_value = {"doc.pdf": "FAILED"}

        event = make_event("jobs/test-uuid/doc.pdf", "FAILED")
        handler(event, None)

        job_svc.update_file_status.assert_called_once_with("test-uuid", "doc.pdf", Status.FAILED)

    def test_updates_file_status_aborted(self, mock_s3, job_svc):
        """Test handler updates file status to ABORTED on ABORTED."""
        from index import handler

        job_svc.update_file_status.return_value = {"doc.pdf": "ABORTED"}

        event = make_event("jobs/test-uuid/doc.pdf", "ABORTED")
        handler(event, None)

        job_svc.update_file_status.assert_called_once_with("test-uuid", "doc.pdf", Status.ABORTED)

    def test_job_not_found(self, mock_s3, job_svc):
        """Test handler returns 404 when job not found."""
        from index import handler

        job_svc.update_file_status.return_value = None

        event = make_event("jobs/nonexistent/doc.pdf")
        response = handler(event, None)

        assert response["statusCode"] == 404

    def test_job_still_processing(self, mock_s3, job_svc):
        """Test handler does not create ZIP when files still processing."""
        from index import handler

        job_svc.update_file_status.return_value = {"a.pdf": "COMPLETED", "b.pdf": "EXTRACTING"}

        event = make_event("jobs/test-uuid/a.pdf")
        response = handler(event, None)

        assert response["statusCode"] == 200
        assert "still processing" in response["body"]
        mock_s3.list_objects_v2.assert_not_called()

    def test_creates_zip_when_all_complete(self, mock_s3, job_svc):
        """Test handler creates ZIP when all files reach terminal state."""
        from index import handler

        job_svc.update_file_status.return_value = {"a.pdf": "COMPLETED", "b.pdf": "COMPLETED"}
        mock_s3.list_objects_v2.return_value = {"Contents": [{"Key": "jobs/test-uuid/a.pdf"}]}
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"content")}

        event = make_event("jobs/test-uuid/b.pdf")
        response = handler(event, None)

        assert response["statusCode"] == 200
        assert "ZIP created" in response["body"]
        mock_s3.put_object.assert_called_once()

    def test_creates_zip_with_mixed_terminal_states(self, mock_s3, job_svc):
        """Test handler creates ZIP when all files terminal (mixed COMPLETED/FAILED)."""
        from index import handler

        job_svc.update_file_status.return_value = {"a.pdf": "COMPLETED", "b.pdf": "FAILED"}
        mock_s3.list_objects_v2.return_value = {"Contents": [{"Key": "jobs/test-uuid/a.pdf"}]}
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"content")}

        event = make_event("jobs/test-uuid/b.pdf", "FAILED")
        response = handler(event, None)

        assert "ZIP created" in response["body"]

    def test_creates_zip_when_a_file_was_rejected(self, mock_s3, job_svc):
        """REJECTED (a reviewer rejected the document) is terminal: the ZIP is built."""
        from index import handler

        job_svc.update_file_status.return_value = {"a.pdf": "COMPLETED", "b.pdf": "REJECTED"}
        mock_s3.list_objects_v2.return_value = {"Contents": [{"Key": "jobs/test-uuid/a.pdf"}]}
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"content")}

        response = handler(make_event("jobs/test-uuid/b.pdf"), None)

        assert "ZIP created" in response["body"]

    def test_wrapper_rejected_status_is_recorded_as_rejected(self, mock_s3, job_svc):
        """A SUCCEEDED finalize run whose wrapper says REJECTED records REJECTED."""
        from index import handler

        job_svc.update_file_status.return_value = {"b.pdf": "REJECTED"}
        mock_s3.list_objects_v2.return_value = {"Contents": []}
        event = make_event("jobs/test-uuid/b.pdf")
        event["detail"]["output"] = json.dumps({"document": {"status": "REJECTED"}})

        handler(event, None)

        job_svc.update_file_status.assert_called_once_with(
            "test-uuid", "b.pdf", Status.REJECTED
        )

    def test_review_hold_is_recorded_without_finalizing_the_job(
        self, mock_s3, job_svc
    ):
        """HITL_IN_PROGRESS is not terminal: record it, keep the job open, no ZIP."""
        from index import handler

        job_svc.update_file_status.return_value = {"b.pdf": "HITL_IN_PROGRESS"}
        event = make_event("jobs/test-uuid/b.pdf")
        event["detail"]["output"] = json.dumps(
            {"document": {"status": "HITL_IN_PROGRESS"}}
        )

        response = handler(event, None)

        assert response == {"statusCode": 200, "body": "Job still processing"}
        job_svc.update_file_status.assert_called_once_with(
            "test-uuid", "b.pdf", Status.HITL_IN_PROGRESS
        )
        mock_s3.put_object.assert_not_called()

    def test_review_hold_for_a_missing_job_is_not_found(self, mock_s3, job_svc):
        """A review hold on a job that no longer exists answers 404, not 200."""
        from index import handler

        job_svc.update_file_status.return_value = None
        event = make_event("jobs/gone-uuid/b.pdf")
        event["detail"]["output"] = json.dumps(
            {"document": {"status": "HITL_IN_PROGRESS"}}
        )

        response = handler(event, None)

        assert response == {"statusCode": 404, "body": "Job not found"}
        job_svc.update_file_status.assert_called_once_with(
            "gone-uuid", "b.pdf", Status.HITL_IN_PROGRESS
        )
        mock_s3.put_object.assert_not_called()

    def test_invalid_job_path(self, mock_s3, job_svc):
        """Test handler returns 400 for invalid job path format."""
        from index import handler

        event = make_event("jobs/only-uuid")
        response = handler(event, None)

        assert response["statusCode"] == 400


class TestCreateResultsZip:
    """Tests for the shared zip builder, exercised through this handler.

    The function itself now lives in ``idp_common.job_completion`` so the ingest
    malware gate can finalize a job file for a document that never ran a workflow.
    Its own unit tests are in ``lib/idp_common_pkg/tests/unit/test_job_completion.py``;
    these two stay here because they pin the wiring — the bucket this handler
    passes and the key it writes.
    """

    def test_creates_zip_from_output_files(self, mock_s3):
        """Test ZIP is created from all files in job prefix."""
        import index

        from idp_common.job_completion import create_results_zip

        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "jobs/uuid/file1.json"},
                {"Key": "jobs/uuid/file2.json"},
            ]
        }
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"content")}

        assert create_results_zip("uuid", mock_s3, index.OUTPUT_BUCKET) is True

        mock_s3.put_object.assert_called_once()
        call_args = mock_s3.put_object.call_args
        assert call_args[1]["Key"] == "jobs/uuid/results.zip"
        assert call_args[1]["Bucket"] == "test-output-bucket"

    def test_handles_empty_output_files(self, mock_s3):
        """Test ZIP creation handles case with no output files."""
        import index

        from idp_common.job_completion import create_results_zip

        mock_s3.list_objects_v2.return_value = {}

        assert create_results_zip("uuid", mock_s3, index.OUTPUT_BUCKET) is False

        mock_s3.put_object.assert_not_called()


class TestTheDelegatedCompletionEvent:
    """The second event shape this handler answers.

    The ingest malware gate refuses a document without starting an execution, so
    when its refusal is the LAST file in a /Jobs batch to go terminal no
    execution-status event follows and nothing would ever build the zip. The gate
    marks the file terminal, then sends this event here — the one function in the
    stack with the memory, timeout, ephemeral storage and output-bucket grants
    that zipping needs.
    """

    def _completion_event(self, job_id="test-uuid"):
        return {"idpJobCompletion": {"jobId": job_id}}

    def test_it_builds_the_zip_and_marks_the_job_ready(self, mock_s3, job_svc):
        from index import handler

        mock_s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "jobs/test-uuid/doc.json"}]
        }
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"{}")}

        response = handler(self._completion_event(), None)

        assert response["statusCode"] == 200
        assert mock_s3.put_object.call_args[1]["Key"] == "jobs/test-uuid/results.zip"
        job_svc.mark_results_ready.assert_called_once_with("test-uuid", True)

    def test_a_job_with_no_outputs_is_marked_not_ready(self, mock_s3, job_svc):
        """Every file in the batch was refused. `compute_job_status` scores that
        FAILED, which the ResultsReady gate does not apply to, so the job still
        reports terminal — but claiming a zip exists would send a client to a
        404."""
        from index import handler

        mock_s3.list_objects_v2.return_value = {}

        handler(self._completion_event(), None)

        mock_s3.put_object.assert_not_called()
        job_svc.mark_results_ready.assert_called_once_with("test-uuid", False)

    def test_it_does_not_touch_the_files_map(self, mock_s3, job_svc):
        """The delegating caller already wrote the file's terminal status. Writing
        one here would need a filename this event does not carry, and re-deriving
        it would be guessing."""
        from index import handler

        mock_s3.list_objects_v2.return_value = {}

        handler(self._completion_event(), None)

        job_svc.update_file_status.assert_not_called()

    def test_a_completion_event_without_a_job_id_is_a_client_error(
        self, mock_s3, job_svc
    ):
        """Reported rather than guessed at: there is no safe default job to
        complete, and silently returning 200 would make a malformed delegation
        indistinguishable from a successful one."""
        from index import handler

        response = handler({"idpJobCompletion": {}}, None)

        assert response["statusCode"] == 400
        job_svc.mark_results_ready.assert_not_called()
        mock_s3.put_object.assert_not_called()

    def test_an_ordinary_step_functions_event_is_unaffected(self, mock_s3, job_svc):
        """The branch is keyed on a field no Step Functions event carries, so the
        path that handles every other job file is untouched."""
        from index import handler

        job_svc.update_file_status.return_value = {
            "doc.pdf": "COMPLETED",
            "other.pdf": "EXTRACTING",
        }

        response = handler(make_event("jobs/test-uuid/doc.pdf", "SUCCEEDED"), None)

        assert response["statusCode"] == 200
        job_svc.update_file_status.assert_called_once()
