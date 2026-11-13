#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for publish.py IDPPublisher class
Tests only methods that exist in the current implementation
"""

import os
import sys
import tempfile
from unittest.mock import Mock, patch

import pytest

# Add the project root to the path so we can import publish
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../.."))

import publish as publish_script
from publish import IDPPublisher


class TestIDPPublisherInitialization:
    """Test IDPPublisher initialization"""

    def test_init_default_values(self):
        """Test that IDPPublisher initializes with correct default values"""
        publisher = IDPPublisher()

        assert publisher.verbose is False
        assert publisher.bucket_basename is None
        assert publisher.prefix is None
        assert publisher.region is None
        assert publisher.acl is None
        assert publisher.bucket is None
        assert publisher.prefix_and_version is None
        assert publisher.version is None
        assert publisher.build_errors == []

    def test_init_verbose_mode(self):
        """Test that IDPPublisher initializes correctly with verbose mode enabled"""
        publisher = IDPPublisher(verbose=True)

        assert publisher.verbose is True


class TestIDPPublisherParameterValidation:
    """Test parameter validation and parsing"""

    def test_check_parameters_missing_required(self):
        """Test that missing required parameters cause exit"""
        publisher = IDPPublisher()

        with patch.object(publisher, "print_usage") as mock_usage:
            with pytest.raises(SystemExit) as exc_info:
                publisher.check_parameters(["bucket"])

            assert exc_info.value.code == 1
            mock_usage.assert_called_once()

    def test_check_parameters_valid_minimal(self):
        """Test valid minimal parameters"""
        publisher = IDPPublisher()

        publisher.check_parameters(["test-bucket", "test-prefix", "us-east-1"])

        assert publisher.bucket_basename == "test-bucket"
        assert publisher.prefix == "test-prefix"
        assert publisher.region == "us-east-1"
        assert publisher.public is False
        assert publisher.acl == "bucket-owner-full-control"
        assert publisher.max_workers is None

    def test_check_parameters_with_public_flag(self):
        """Test parameters with public flag"""
        publisher = IDPPublisher()

        with patch.object(publisher.console, "print") as mock_print:
            publisher.check_parameters(
                ["test-bucket", "test-prefix", "us-east-1", "public"]
            )

        assert publisher.public is True
        assert publisher.acl == "public-read"
        mock_print.assert_any_call(
            "[green]Published S3 artifacts will be accessible by public.[/green]"
        )

    def test_check_parameters_with_max_workers(self):
        """Test parameters with max-workers option"""
        publisher = IDPPublisher()

        with patch.object(publisher.console, "print") as mock_print:
            publisher.check_parameters(
                ["test-bucket", "test-prefix", "us-east-1", "--max-workers", "4"]
            )

        assert publisher.max_workers == 4
        mock_print.assert_any_call("[green]Using 4 concurrent workers[/green]")

    def test_check_parameters_strip_trailing_slash(self):
        """Test that trailing slash is stripped from prefix"""
        publisher = IDPPublisher()

        publisher.check_parameters(["test-bucket", "test-prefix/", "us-east-1"])

        assert publisher.prefix == "test-prefix"


class TestIDPPublisherEnvironmentSetup:
    """Test environment setup functionality"""

    @patch("boto3.client")
    def test_setup_environment_reads_version_and_derived_values(self, mock_boto_client):
        """setup_environment reads VERSION and derives bucket / prefix values."""
        mock_boto_client.return_value = Mock()

        publisher = IDPPublisher()
        publisher.region = "us-east-1"

        with patch("builtins.open", mock_open_version_file("1.0.0")):
            publisher.setup_environment()

            assert publisher.version == "1.0.0"
            assert publisher.prefix_and_version.endswith("/1.0.0")
            assert publisher.bucket.endswith("-us-east-1")

    @patch("boto3.client")
    def test_setup_environment_us_east_1_udop_model(self, mock_boto_client):
        """Test UDOP model path for us-east-1"""
        mock_boto_client.return_value = Mock()

        publisher = IDPPublisher()
        publisher.region = "us-east-1"

        with patch("builtins.open", mock_open_version_file("1.0.0")):
            publisher.setup_environment()

            expected_model = "s3://aws-ml-blog-us-east-1/artifacts/genai-idp/udop-finetuning/rvl-cdip/model.tar.gz"
            assert publisher.public_sample_udop_model == expected_model

    @patch("boto3.client")
    def test_setup_environment_other_region_udop_model(self, mock_boto_client):
        """Test UDOP model path for other regions (fallback to us-east-1)"""
        mock_boto_client.return_value = Mock()

        publisher = IDPPublisher()
        publisher.region = "eu-west-1"

        with patch("builtins.open", mock_open_version_file("1.0.0")):
            publisher.setup_environment()

            # Uses the actual region (no longer falls back to us-east-1)
            expected_model = "s3://aws-ml-blog-eu-west-1/artifacts/genai-idp/udop-finetuning/rvl-cdip/model.tar.gz"
            assert publisher.public_sample_udop_model == expected_model


class TestIDPPublisherVersionComparison:
    """Test version comparison functionality"""

    def test_version_compare_equal(self):
        """Test version comparison for equal versions"""
        publisher = IDPPublisher()
        assert publisher.version_compare("1.2.3", "1.2.3") == 0

    def test_version_compare_less_than(self):
        """Test version comparison for less than"""
        publisher = IDPPublisher()
        assert publisher.version_compare("1.2.2", "1.2.3") == -1
        assert publisher.version_compare("1.2.3", "2.0.0") == -1

    def test_version_compare_greater_than(self):
        """Test version comparison for greater than"""
        publisher = IDPPublisher()
        assert publisher.version_compare("1.2.4", "1.2.3") == 1
        assert publisher.version_compare("2.0.0", "1.2.3") == 1

    def test_version_compare_different_lengths(self):
        """Test version comparison with different length versions"""
        publisher = IDPPublisher()
        assert publisher.version_compare("1.2", "1.2.0") == 0
        assert publisher.version_compare("1.2.1", "1.2") == 1
        assert publisher.version_compare("1.2", "1.2.1") == -1


class TestIDPPublisherChecksumOperations:
    """Test checksum calculation and management"""

    def test_get_file_checksum_existing_file(self):
        """Test get_file_checksum with existing file"""
        publisher = IDPPublisher()

        with tempfile.NamedTemporaryFile(mode="w", delete=False) as temp_file:
            temp_file.write("test content")
            temp_file_path = temp_file.name

        try:
            checksum = publisher.get_file_checksum(temp_file_path)
            assert len(checksum) == 64  # SHA256 hex digest length
            assert checksum != ""
        finally:
            os.unlink(temp_file_path)

    def test_get_file_checksum_nonexistent_file(self):
        """Test get_file_checksum with non-existent file"""
        publisher = IDPPublisher()
        checksum = publisher.get_file_checksum("/nonexistent/file.txt")
        assert checksum == ""

    def test_get_directory_checksum_existing_directory(self):
        """Test get_directory_checksum with existing directory"""
        publisher = IDPPublisher()

        with tempfile.TemporaryDirectory() as temp_dir:
            # Create some files
            file1_path = os.path.join(temp_dir, "file1.txt")
            file2_path = os.path.join(temp_dir, "file2.txt")

            with open(file1_path, "w") as f:
                f.write("content1")
            with open(file2_path, "w") as f:
                f.write("content2")

            checksum = publisher.get_directory_checksum(temp_dir)
            assert len(checksum) == 64
            assert checksum != ""

    def test_get_directory_checksum_nonexistent_directory(self):
        """Test get_directory_checksum with non-existent directory"""
        publisher = IDPPublisher()
        checksum = publisher.get_directory_checksum("/nonexistent/directory")
        assert checksum == ""


def mock_open_version_file(version_content):
    """Helper function to mock opening VERSION file"""
    from unittest.mock import mock_open

    return mock_open(read_data=version_content)


class _FakePublisher:
    """Stands in for IDPPublisher: records the arguments ``run`` received."""

    region = "us-east-1"
    bucket = "artifacts-us-east-1"
    prefix = "idp"
    version = "0.0.0"
    public = False

    def __init__(self):
        self.args = None

    def run(self, args):
        self.args = args
        _FakePublisher.last = self


class _FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def __call__(self, region=None):
        client = Mock()

        def publish_terraform(**kwargs):
            self.calls.append(kwargs)
            if self.error:
                raise self.error
            return {"moduleUrl": "s3://m", "sha256": "0" * 64, "pointerUrl": "s3://p"}

        client.publish.publish_terraform = publish_terraform
        return client


@pytest.mark.unit
class TestPublishScriptTerraformStep:
    """A direct ``publish.py`` run publishes the Terraform module after the
    CloudFormation artifacts, unless given ``--no-terraform``."""

    def _run(self, monkeypatch, argv, client):
        from rich.console import Console

        import idp_sdk

        monkeypatch.setattr(idp_sdk, "IDPClient", client)
        console = Console(record=True, width=400)
        rc = publish_script.run_publish(argv, _FakePublisher, console)
        return rc, console.export_text()

    def test_no_terraform_skips_the_module_and_is_not_passed_on(self, monkeypatch):
        client = _FakeClient()
        rc, _ = self._run(
            monkeypatch, ["b", "idp", "us-east-1", "--no-terraform"], client
        )
        assert rc == 0
        assert client.calls == []
        assert _FakePublisher.last.args == ["b", "idp", "us-east-1"]

    def test_the_module_is_published_by_default(self, monkeypatch):
        client = _FakeClient()
        rc, out = self._run(monkeypatch, ["b", "idp", "us-east-1"], client)
        assert rc == 0
        assert len(client.calls) == 1
        assert client.calls[0]["bucket"] == "artifacts-us-east-1"
        assert "Terraform module: s3://m" in out

    def test_a_terraform_failure_says_the_cloudformation_artifacts_are_published(
        self, monkeypatch
    ):
        client = _FakeClient(error=RuntimeError("translator exploded"))
        rc, out = self._run(monkeypatch, ["b", "idp", "us-east-1"], client)
        assert rc == 1
        assert "translator exploded" in out
        assert "CloudFormation artifacts were published" in out
        assert "s3://artifacts-us-east-1/idp" in out
        assert "--no-terraform" in out

    def test_no_environment_variable_turns_the_module_off(self, monkeypatch):
        """Only the flag does: the variable that once did was set by nothing."""
        monkeypatch.setenv("IDP_SDK_PUBLISH_SUBPROCESS", "1")
        client = _FakeClient()
        rc, _ = self._run(monkeypatch, ["b", "idp", "us-east-1"], client)
        assert rc == 0 and len(client.calls) == 1


if __name__ == "__main__":
    pytest.main([__file__])
