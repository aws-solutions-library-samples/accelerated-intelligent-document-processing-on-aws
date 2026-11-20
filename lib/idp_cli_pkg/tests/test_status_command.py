# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the enhanced status command with search functionality.
"""

from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from idp_cli.cli import cli


@pytest.fixture
def runner():
    """Create a Click CLI test runner."""
    return CliRunner()


@pytest.fixture
def mock_batch_processor():
    """Mock IDPClient (replaces BatchProcessor — cli.py now uses IDPClient)."""
    with patch("idp_sdk.IDPClient") as mock:
        mock_instance = Mock()
        mock_instance.resources = {
            "DocumentsTable": "test-table",
            "InputBucket": "test-input-bucket",
        }
        # batch.get_status() must return a BatchStatus-like object so that
        # _batch_status_to_display_dicts() in cli.py doesn't blow up.
        mock_batch_status = Mock()
        mock_batch_status.documents = []
        mock_batch_status.total = 0
        mock_batch_status.completed = 0
        mock_batch_status.failed = 0
        mock_batch_status.in_progress = 0
        mock_batch_status.queued = 0
        mock_batch_status.success_rate = 0.0
        mock_batch_status.all_complete = True
        mock_instance.batch.get_status.return_value = mock_batch_status
        mock.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def mock_tracking_searcher():
    """Mock TrackingTableSearcher."""
    with patch("idp_cli.search_tracking_table.TrackingTableSearcher") as mock:
        mock_instance = Mock()
        mock.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def mock_progress_monitor():
    """Dummy fixture — ProgressMonitor is no longer used directly in cli.py status command.
    IDPClient.batch.get_status() is used instead. Kept so test signatures don't need changing."""
    yield Mock()


class TestStatusCommandBasic:
    """Test basic status command functionality (backward compatibility)."""

    def test_status_requires_identifier(self, runner):
        """Test that status command requires either batch-id or document-id."""
        result = runner.invoke(cli, ["status", "--stack-name", "test-stack"])

        assert result.exit_code != 0
        assert "Must specify either --batch-id or --document-id" in result.output

    def test_status_rejects_both_identifiers(self, runner):
        """Test that status command rejects both batch-id and document-id."""
        result = runner.invoke(
            cli,
            [
                "status",
                "--stack-name",
                "test-stack",
                "--batch-id",
                "batch-123",
                "--document-id",
                "doc.pdf",
            ],
        )

        assert result.exit_code != 0
        assert "Cannot specify both --batch-id and --document-id" in result.output


class TestStatusCommandSearch:
    """Test status command with search functionality."""

    def test_status_with_batch_id_searches_tracking_table(
        self,
        runner,
        mock_batch_processor,
        mock_tracking_searcher,
        mock_progress_monitor,
    ):
        """Test that batch-id triggers tracking table search."""
        # Mock search results
        mock_tracking_searcher.search_by_pk_and_status.return_value = {
            "success": True,
            "count": 2,
            "items": [
                {
                    "ObjectKey": {"S": "batch-123/doc1.pdf"},
                    "ObjectStatus": {"S": "COMPLETED"},
                },
                {
                    "ObjectKey": {"S": "batch-123/doc2.pdf"},
                    "ObjectStatus": {"S": "COMPLETED"},
                },
            ],
        }

        # Mock progress monitor
        mock_progress_monitor.get_batch_status.return_value = []
        mock_progress_monitor.calculate_statistics.return_value = {}

        with patch("idp_cli.cli.display") as mock_display:
            mock_display.show_final_status_summary.return_value = 0

            runner.invoke(
                cli,
                [
                    "status",
                    "--stack-name",
                    "test-stack",
                    "--batch-id",
                    "batch-123",
                    "--object-status",
                    "COMPLETED",
                ],
            )

            # Verify search was called
            mock_tracking_searcher.search_by_pk_and_status.assert_called_once_with(
                pk="batch-123", object_status="COMPLETED"
            )

    def test_status_with_batch_id_no_status_searches_all(
        self,
        runner,
        mock_batch_processor,
        mock_tracking_searcher,
        mock_progress_monitor,
    ):
        """Test that batch-id without status searches all statuses."""
        # Mock search results for each status
        mock_tracking_searcher.search_by_pk_and_status.return_value = {
            "success": True,
            "count": 1,
            "items": [
                {
                    "ObjectKey": {"S": "batch-123/doc1.pdf"},
                    "ObjectStatus": {"S": "COMPLETED"},
                }
            ],
        }

        # Mock progress monitor
        mock_progress_monitor.get_batch_status.return_value = []
        mock_progress_monitor.calculate_statistics.return_value = {}

        with patch("idp_cli.cli.display") as mock_display:
            mock_display.show_final_status_summary.return_value = 0

            runner.invoke(
                cli,
                ["status", "--stack-name", "test-stack", "--batch-id", "batch-123"],
            )

            # One search over every status (#2195): a per-status list missed every
            # step status, so a document mid-pipeline was not found.
            mock_tracking_searcher.search_by_pk_and_status.assert_called_once_with(
                pk="batch-123", object_status=None
            )

    def test_status_with_batch_id_no_results(
        self, runner, mock_batch_processor, mock_tracking_searcher
    ):
        """Test status command when search returns no results."""
        # Mock empty search results
        mock_tracking_searcher.search_by_pk_and_status.return_value = {
            "success": True,
            "count": 0,
            "items": [],
        }

        result = runner.invoke(
            cli,
            [
                "status",
                "--stack-name",
                "test-stack",
                "--batch-id",
                "nonexistent",
                "--object-status",
                "COMPLETED",
            ],
        )

        assert result.exit_code != 0
        assert "No documents found" in result.output

    def test_status_with_batch_id_search_error(
        self, runner, mock_batch_processor, mock_tracking_searcher
    ):
        """Test status command when search fails."""
        # Mock search error
        mock_tracking_searcher.search_by_pk_and_status.return_value = {
            "success": False,
            "error": "DynamoDB error",
        }

        result = runner.invoke(
            cli,
            [
                "status",
                "--stack-name",
                "test-stack",
                "--batch-id",
                "batch-123",
                "--object-status",
                "COMPLETED",
            ],
        )

        assert result.exit_code != 0
        assert "Search failed" in result.output


class TestStatusCommandTiming:
    """Test status command with timing statistics."""

    def test_status_with_get_time_flag(
        self, runner, mock_batch_processor, mock_tracking_searcher
    ):
        """Test status command with --get-time flag."""
        # Mock search results
        search_results = {
            "success": True,
            "count": 1,
            "items": [
                {
                    "ObjectKey": {"S": "batch-123/doc1.pdf"},
                    "ObjectStatus": {"S": "COMPLETED"},
                }
            ],
        }
        mock_tracking_searcher.search_by_pk_and_status.return_value = search_results

        # Mock timing statistics
        mock_tracking_searcher.calculate_timing_statistics.return_value = {
            "success": True,
            "valid_count": 1,
            "processing_time": {
                "average": 45.5,
                "median": 45.5,
                "min": 45.5,
                "max": 45.5,
            },
        }

        runner.invoke(
            cli,
            [
                "status",
                "--stack-name",
                "test-stack",
                "--batch-id",
                "batch-123",
                "--object-status",
                "COMPLETED",
                "--get-time",
            ],
        )

        # Verify timing calculation was called
        mock_tracking_searcher.calculate_timing_statistics.assert_called_once_with(
            search_results, include_metering=False
        )

        # Verify display was called
        mock_tracking_searcher.display_timing_statistics.assert_called_once()

    def test_status_with_get_time_and_metering(
        self, runner, mock_batch_processor, mock_tracking_searcher
    ):
        """Test status command with --get-time and --include-metering flags."""
        # Mock search results
        search_results = {
            "success": True,
            "count": 1,
            "items": [
                {
                    "ObjectKey": {"S": "batch-123/doc1.pdf"},
                    "ObjectStatus": {"S": "COMPLETED"},
                }
            ],
        }
        mock_tracking_searcher.search_by_pk_and_status.return_value = search_results

        # Mock timing statistics with metering
        mock_tracking_searcher.calculate_timing_statistics.return_value = {
            "success": True,
            "valid_count": 1,
            "metering_count": 1,
            "metering": {
                "Assessment": {"average": 1.5, "total": 1.5},
                "OCR": {"average": 2.5, "total": 2.5},
            },
        }

        runner.invoke(
            cli,
            [
                "status",
                "--stack-name",
                "test-stack",
                "--batch-id",
                "batch-123",
                "--object-status",
                "COMPLETED",
                "--get-time",
                "--include-metering",
            ],
        )

        # Verify timing calculation was called with metering enabled
        mock_tracking_searcher.calculate_timing_statistics.assert_called_once_with(
            search_results, include_metering=True
        )


class TestStatusCommandDetails:
    """Test status command with detailed display."""

    def test_status_with_show_details_flag(
        self,
        runner,
        mock_batch_processor,
        mock_tracking_searcher,
        mock_progress_monitor,
    ):
        """Test status command with --show-details flag."""
        # Mock search results
        search_results = {
            "success": True,
            "count": 2,
            "items": [
                {
                    "ObjectKey": {"S": "batch-123/doc1.pdf"},
                    "ObjectStatus": {"S": "COMPLETED"},
                    "PK": {"S": "doc#batch-123/doc1.pdf"},
                },
                {
                    "ObjectKey": {"S": "batch-123/doc2.pdf"},
                    "ObjectStatus": {"S": "COMPLETED"},
                    "PK": {"S": "doc#batch-123/doc2.pdf"},
                },
            ],
        }
        mock_tracking_searcher.search_by_pk_and_status.return_value = search_results

        # Mock progress monitor
        mock_progress_monitor.get_batch_status.return_value = []
        mock_progress_monitor.calculate_statistics.return_value = {}

        with patch("idp_cli.cli.display") as mock_display:
            mock_display.show_final_status_summary.return_value = 0

            runner.invoke(
                cli,
                [
                    "status",
                    "--stack-name",
                    "test-stack",
                    "--batch-id",
                    "batch-123",
                    "--object-status",
                    "COMPLETED",
                    "--show-details",
                ],
            )

            # Verify display_results was called with show_details=True
            mock_tracking_searcher.display_results.assert_called_once_with(
                search_results, show_details=True
            )


class TestStatusCommandDocumentId:
    """Test status command with document-id (backward compatibility)."""

    def test_status_with_document_id(
        self, runner, mock_batch_processor, mock_progress_monitor
    ):
        """Test status command with document-id uses IDPClient.batch.get_status()."""
        with patch("idp_cli.cli.display") as mock_display:
            mock_display.show_final_status_summary.return_value = 0

            runner.invoke(
                cli,
                [
                    "status",
                    "--stack-name",
                    "test-stack",
                    "--document-id",
                    "batch-123/doc1.pdf",
                ],
            )

            # Verify IDPClient.batch.get_status() was called with the document id
            mock_batch_processor.batch.get_status.assert_called_once_with(
                "batch-123/doc1.pdf"
            )


class TestStatusCommandOptions:
    """Test various option combinations."""

    def test_status_help_shows_new_options(self, runner):
        """Test that help text includes new options."""
        result = runner.invoke(cli, ["status", "--help"])

        assert result.exit_code == 0
        assert "--object-status" in result.output
        assert "--show-details" in result.output
        assert "--get-time" in result.output
        assert "--include-metering" in result.output

    def test_status_json_format_with_search(
        self,
        runner,
        mock_batch_processor,
        mock_tracking_searcher,
        mock_progress_monitor,
    ):
        """Test status command with JSON format and search."""
        # Mock search results
        mock_tracking_searcher.search_by_pk_and_status.return_value = {
            "success": True,
            "count": 1,
            "items": [
                {
                    "ObjectKey": {"S": "batch-123/doc1.pdf"},
                    "ObjectStatus": {"S": "COMPLETED"},
                }
            ],
        }

        # Mock progress monitor
        mock_progress_monitor.get_batch_status.return_value = [
            {
                "document_id": "batch-123/doc1.pdf",
                "status": "COMPLETED",
            }
        ]
        mock_progress_monitor.calculate_statistics.return_value = {}

        with patch("idp_cli.cli.display") as mock_display:
            mock_display.format_status_json.return_value = '{"status": "COMPLETED"}'

            runner.invoke(
                cli,
                [
                    "status",
                    "--stack-name",
                    "test-stack",
                    "--batch-id",
                    "batch-123",
                    "--object-status",
                    "COMPLETED",
                    "--format",
                    "json",
                ],
            )

            # Verify JSON formatting was used
            mock_display.format_status_json.assert_called_once()


class TestStatusCommandIntegration:
    """Integration-style tests for complete workflows."""

    def test_complete_search_workflow(
        self,
        runner,
        mock_batch_processor,
        mock_tracking_searcher,
        mock_progress_monitor,
    ):
        """Test complete workflow: search → display → timing."""
        # Mock search results
        search_results = {
            "success": True,
            "count": 2,
            "items": [
                {
                    "ObjectKey": {"S": "batch-123/doc1.pdf"},
                    "ObjectStatus": {"S": "COMPLETED"},
                },
                {
                    "ObjectKey": {"S": "batch-123/doc2.pdf"},
                    "ObjectStatus": {"S": "COMPLETED"},
                },
            ],
        }
        mock_tracking_searcher.search_by_pk_and_status.return_value = search_results

        # Mock timing statistics
        mock_tracking_searcher.calculate_timing_statistics.return_value = {
            "success": True,
            "valid_count": 2,
        }

        runner.invoke(
            cli,
            [
                "status",
                "--stack-name",
                "test-stack",
                "--batch-id",
                "batch-123",
                "--object-status",
                "COMPLETED",
                "--get-time",
                "--include-metering",
            ],
        )

        # Verify search and timing steps were called
        mock_tracking_searcher.search_by_pk_and_status.assert_called_once()
        mock_tracking_searcher.calculate_timing_statistics.assert_called_once()
        mock_tracking_searcher.display_timing_statistics.assert_called_once()


class TestStatusJsonStdoutIsPureJson:
    """`status --batch-id ... --format json` puts nothing but the payload on stdout.

    The searcher is real here and only its DynamoDB client is stubbed, because
    the lines that broke `| jq` were printed by the searcher itself; a test that
    mocks the searcher cannot see them (#2199).
    """

    def test_the_searchers_progress_lines_go_to_stderr(
        self, runner, mock_batch_processor
    ):
        import json

        from idp_cli.search_tracking_table import TrackingTableSearcher

        dynamodb = Mock()
        dynamodb.scan.return_value = {
            "Items": [
                {
                    "PK": {"S": "doc#batch-123/doc1.pdf"},
                    "ObjectKey": {"S": "batch-123/doc1.pdf"},
                    "ObjectStatus": {"S": "COMPLETED"},
                }
            ]
        }

        def offline_init(self, stack_name, region=None):
            self.stack_name = stack_name
            self.region = region
            self.dynamodb = dynamodb
            self.table_name = "test-table"

        with patch.object(TrackingTableSearcher, "__init__", offline_init):
            result = runner.invoke(
                cli,
                [
                    "status",
                    "--stack-name",
                    "test-stack",
                    "--batch-id",
                    "batch-123",
                    "--format",
                    "json",
                ],
            )

        dynamodb.scan.assert_called_once()
        payload = json.loads(result.stdout)
        assert isinstance(payload, dict)
        assert "Searching tracking table" in result.stderr
        assert "Found 1 matching documents" in result.stderr
