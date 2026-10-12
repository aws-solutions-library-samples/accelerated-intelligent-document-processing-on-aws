# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for :mod:`idp_common.job_completion`.

This module exists because a /Jobs API batch can now be finished by two
different writers — ``job_tracker`` when a workflow execution ends, and the
ingest malware gate in ``queue_sender`` when a document is refused and so never
runs one. The tests that matter most here are the ones about a job *not hanging*:
before this module, a refused file left its batch reporting ``IN_PROGRESS``
forever with no error anywhere.
"""

import zipfile
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from idp_common.job_completion import (
    TERMINAL_FILE_STATUSES,
    create_results_zip,
    finalize_job_file,
    parse_job_document_id,
)
from idp_common.models import Status


@pytest.mark.unit
class TestParseJobDocumentId:
    """Telling a /Jobs document from an ordinary upload."""

    def test_a_job_key_splits_into_job_and_filename(self):
        assert parse_job_document_id("jobs/abc-123/report.pdf") == (
            "abc-123",
            "report.pdf",
        )

    def test_a_filename_containing_slashes_survives_intact(self):
        """maxsplit=2: a job file may itself be nested, and truncating its name
        would address the wrong entry in the Files map — or none at all."""
        assert parse_job_document_id("jobs/abc-123/sub/dir/report.pdf") == (
            "abc-123",
            "sub/dir/report.pdf",
        )

    @pytest.mark.parametrize(
        "document_id",
        [
            "",
            "report.pdf",
            "uploads/report.pdf",
            # A jobs/ key that names no file: there is nothing in the Files map
            # to finalize, so it must not be mistaken for one.
            "jobs/abc-123",
            "jobs/abc-123/",
            "jobs//report.pdf",
            # Near-miss prefixes. "jobs" without the slash is an ordinary key.
            "jobsfoo/abc/report.pdf",
        ],
    )
    def test_everything_else_is_not_a_job_document(self, document_id):
        assert parse_job_document_id(document_id) is None


@pytest.mark.unit
class TestFinalizeJobFile:
    def _job_service(self, files):
        svc = MagicMock()
        svc.update_file_status.return_value = files
        return svc

    def _s3(self, keys=("jobs/j1/a.pdf",)):
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {"Contents": [{"Key": k} for k in keys]}
        s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"bytes")}
        return s3

    def test_a_non_terminal_status_is_refused(self):
        """Storing one would be the exact defect this module exists to fix, and
        it would stay invisible until a customer reported a stuck job."""
        svc = self._job_service({"a.pdf": "COMPLETED"})
        with pytest.raises(ValueError, match="terminal status"):
            finalize_job_file(
                job_id="j1",
                filename="a.pdf",
                status=Status.MALWARE_SCANNING,
                job_service=svc,
                s3_client=self._s3(),
                output_bucket="out",
            )
        svc.update_file_status.assert_not_called()

    def test_quarantined_is_refused_as_a_files_map_value(self):
        """The document-level malware statuses deliberately never reach the Files
        map: three separate copies of the terminal-status set read it, and none
        of them knows these values."""
        for status in (Status.QUARANTINED, Status.MALWARE_SCAN_FAILED):
            assert status.value not in TERMINAL_FILE_STATUSES
            with pytest.raises(ValueError, match="terminal status"):
                finalize_job_file(
                    job_id="j1",
                    filename="a.pdf",
                    status=status,
                    job_service=self._job_service({}),
                    s3_client=self._s3(),
                    output_bucket="out",
                )

    def test_a_missing_job_is_reported_not_raised(self):
        """A TTL-expired or deleted job row is not an error for the caller: the
        document's own status has already been recorded."""
        svc = self._job_service(None)
        result = finalize_job_file(
            job_id="gone",
            filename="a.pdf",
            status=Status.FAILED,
            job_service=svc,
            s3_client=self._s3(),
            output_bucket="out",
        )
        assert result["found"] is False
        assert result["job_complete"] is False

    def test_a_job_with_work_outstanding_does_not_build_the_zip(self):
        s3 = self._s3()
        result = finalize_job_file(
            job_id="j1",
            filename="a.pdf",
            status=Status.FAILED,
            job_service=self._job_service({"a.pdf": "FAILED", "b.pdf": "EXTRACTING"}),
            s3_client=s3,
            output_bucket="out",
        )
        assert result["job_complete"] is False
        s3.put_object.assert_not_called()

    def test_the_last_file_builds_the_zip_and_marks_results_ready(self):
        svc = self._job_service({"a.pdf": "FAILED", "b.pdf": "COMPLETED"})
        s3 = self._s3()
        result = finalize_job_file(
            job_id="j1",
            filename="a.pdf",
            status=Status.FAILED,
            job_service=svc,
            s3_client=s3,
            output_bucket="out",
        )
        assert result["job_complete"] is True
        assert result["results_ready"] is True
        s3.put_object.assert_called_once()
        svc.mark_results_ready.assert_called_once_with("j1", True)

    def test_results_ready_is_not_marked_before_the_zip_exists(self):
        """The API gates SUCCEEDED on that flag, so setting it first would send a
        client to a 404."""
        svc = self._job_service({"a.pdf": "FAILED"})
        s3 = self._s3()
        s3.put_object.side_effect = RuntimeError("S3 unavailable")
        with pytest.raises(RuntimeError, match="S3 unavailable"):
            finalize_job_file(
                job_id="j1",
                filename="a.pdf",
                status=Status.FAILED,
                job_service=svc,
                s3_client=s3,
                output_bucket="out",
            )
        svc.mark_results_ready.assert_not_called()

    def test_a_job_whose_every_file_was_refused_completes_without_a_zip(self):
        """There is no output to zip, which is the normal shape of a batch the
        malware gate refused outright. The job must still complete."""
        svc = self._job_service({"a.pdf": "FAILED"})
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {}
        result = finalize_job_file(
            job_id="j1",
            filename="a.pdf",
            status=Status.FAILED,
            job_service=svc,
            s3_client=s3,
            output_bucket="out",
        )
        assert result["job_complete"] is True
        assert result["results_ready"] is False
        s3.put_object.assert_not_called()
        # False, not True: `api_handler` holds a job at IN_PROGRESS while it is
        # SUCCEEDED/PARTIALLY_SUCCEEDED and not ResultsReady, so claiming ready
        # with no zip would hand a client a results URL that 404s. The job still
        # reports terminal, because every-file-refused scores FAILED and that
        # status is not gated on the flag.
        svc.mark_results_ready.assert_called_once_with("j1", False)

    def test_it_is_idempotent_under_a_repeated_call(self):
        """The malware gate re-attempts this on a duplicate GuardDuty verdict,
        which is its only recovery route when the first delivery failed partway
        through. A second call must be harmless."""
        svc = self._job_service({"a.pdf": "FAILED"})
        s3 = self._s3()
        first = finalize_job_file(
            job_id="j1",
            filename="a.pdf",
            status=Status.FAILED,
            job_service=svc,
            s3_client=s3,
            output_bucket="out",
        )
        second = finalize_job_file(
            job_id="j1",
            filename="a.pdf",
            status=Status.FAILED,
            job_service=svc,
            s3_client=s3,
            output_bucket="out",
        )
        assert first == second


@pytest.mark.unit
class TestCreateResultsZip:
    def test_it_zips_every_output_under_the_job_prefix(self):
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "jobs/j1/a.json"}, {"Key": "jobs/j1/b.json"}]
        }
        s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"payload")}

        assert create_results_zip("j1", s3, "out") is True

        body = s3.put_object.call_args.kwargs["Body"]
        names = zipfile.ZipFile(BytesIO(body)).namelist()
        assert names == ["jobs/j1/a.json", "jobs/j1/b.json"]
        assert s3.put_object.call_args.kwargs["Key"] == "jobs/j1/results.zip"

    def test_a_previous_results_zip_is_not_zipped_into_the_new_one(self):
        """Re-finalizing a job would otherwise nest the old archive inside the
        new one, doubling its size on every pass."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "jobs/j1/a.json"},
                {"Key": "jobs/j1/results.zip"},
            ]
        }
        s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"payload")}

        create_results_zip("j1", s3, "out")

        body = s3.put_object.call_args.kwargs["Body"]
        assert zipfile.ZipFile(BytesIO(body)).namelist() == ["jobs/j1/a.json"]

    def test_it_follows_the_continuation_token_across_pages(self):
        """list_objects_v2 caps at 1000 keys, and a batch of a few hundred
        multi-page documents exceeds that — a single call would silently zip a
        prefix of the results and report success."""
        s3 = MagicMock()
        s3.list_objects_v2.side_effect = [
            {
                "Contents": [{"Key": "jobs/j1/a.json"}],
                "IsTruncated": True,
                "NextContinuationToken": "page2",
            },
            {"Contents": [{"Key": "jobs/j1/b.json"}]},
        ]
        s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"payload")}

        create_results_zip("j1", s3, "out")

        assert s3.list_objects_v2.call_count == 2
        assert (
            s3.list_objects_v2.call_args_list[1].kwargs["ContinuationToken"] == "page2"
        )
        body = s3.put_object.call_args.kwargs["Body"]
        assert zipfile.ZipFile(BytesIO(body)).namelist() == [
            "jobs/j1/a.json",
            "jobs/j1/b.json",
        ]

    def test_pagination_stops_on_a_missing_token_even_if_truncated_is_truthy(self):
        """The loop advances on the token, never on ``IsTruncated``. Those look
        interchangeable and are not: a truthy non-boolean ``IsTruncated`` with no
        token spins forever, and that is precisely what an unconfigured
        ``MagicMock`` returns — so the first version of this hung the job_tracker
        suite instead of failing it.
        """
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "jobs/j1/a.json"}],
            # Truthy, and deliberately not a bool, with no token beside it.
            "IsTruncated": "yes",
        }
        s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"payload")}

        create_results_zip("j1", s3, "out")

        assert s3.list_objects_v2.call_count == 1

    def test_an_entirely_unconfigured_client_terminates(self):
        """The MagicMock shape itself, as a regression guard: ``.get()`` on a
        bare MagicMock answers a truthy MagicMock for every key."""
        s3 = MagicMock()
        assert create_results_zip("j1", s3, "out") is False
        assert s3.list_objects_v2.call_count == 1
