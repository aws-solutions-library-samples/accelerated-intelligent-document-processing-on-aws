"""Unit tests for job_tracker Lambda function."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add idp_common to path and import real Status
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../lib/idp_common_pkg"))
from idp_common.models import Status

# Mock idp_common before importing index
mock_job_service = MagicMock()
mock_job_service_module = MagicMock()
mock_job_service_module.create_job_service = MagicMock(return_value=mock_job_service)
mock_models_module = MagicMock()
mock_models_module.Status = Status

sys.modules["idp_common"] = MagicMock()
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

    def test_invalid_job_path(self, mock_s3, job_svc):
        """Test handler returns 400 for invalid job path format."""
        from index import handler

        event = make_event("jobs/only-uuid")
        response = handler(event, None)

        assert response["statusCode"] == 400


class TestCreateResultsZip:
    """Tests for create_results_zip function."""

    def test_creates_zip_from_output_files(self, mock_s3):
        """Test ZIP is created from all files in job prefix."""
        from index import create_results_zip

        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "jobs/uuid/file1.json"},
                {"Key": "jobs/uuid/file2.json"},
            ]
        }
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"content")}

        create_results_zip("uuid")

        mock_s3.put_object.assert_called_once()
        call_args = mock_s3.put_object.call_args
        assert call_args[1]["Key"] == "jobs/uuid/results.zip"
        assert call_args[1]["Bucket"] == "test-output-bucket"

    def test_handles_empty_output_files(self, mock_s3):
        """Test ZIP creation handles case with no output files."""
        from index import create_results_zip

        mock_s3.list_objects_v2.return_value = {}

        create_results_zip("uuid")

        mock_s3.put_object.assert_not_called()
