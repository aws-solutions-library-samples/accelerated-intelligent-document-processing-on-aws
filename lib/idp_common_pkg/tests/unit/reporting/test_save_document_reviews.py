# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review-queue reporting (IDP-0002): ``document_reviews`` rows and section columns.

A document routed to a review queue gets one ``document_reviews`` row, at a
deterministic key so the finalize run overwrites the phase-1 row, and its
``document_sections`` rows carry four review columns. Every other document's
rows are unchanged, which is what keeps the change add-only for Athena.
"""

import datetime
import io
from unittest.mock import MagicMock, patch

import pyarrow.parquet as pq
import pytest

from idp_common.models import Document, Section
from idp_common.reporting.save_reporting_data import SaveReportingData
from idp_common.review_queues.models import ReviewReportingInfo

_REVIEW_COLUMNS = {"review_status", "reviewed_by", "reviewed_at", "review_queue"}


def _review(**overrides) -> ReviewReportingInfo:
    fields = {
        "cycle_id": "c-1",
        "review_status": "Completed",
        "disposition": "approved",
        "profile": "claims",
        "queue": "default",
        "reviewer": "alice@example.com",
        "reviewed_at": "2025-07-07T15:00:00Z",
        "enqueued_at": "2025-07-07T14:30:00+02:00",
        "reason_count": 2,
        "top_reason_kind": "low_confidence",
        "escalated": True,
        "machine_run_id": "run-1",
        "final_run_id": "run-2",
    }
    fields.update(overrides)
    return ReviewReportingInfo(**fields)


@pytest.fixture
def s3():
    with patch("boto3.client") as client:
        mock_s3 = MagicMock()
        client.return_value = mock_s3
        yield mock_s3


def _written(s3, index: int = -1):
    call = s3.put_object.call_args_list[index].kwargs
    return call["Key"], pq.read_table(io.BytesIO(call["Body"]))


@pytest.mark.unit
class TestSaveDocumentReviews:
    def test_the_row_carries_every_review_field(self, s3):
        doc = Document(
            id="batch/a.pdf",
            input_key="batch/a.pdf",
            initial_event_time="2025-07-07T13:59:00Z",
        )
        result = SaveReportingData("rep").save_document_reviews(doc, _review())

        key, table = _written(s3)
        assert key == "document_reviews/date=2025-07-07/batch_a.pdf.parquet"
        assert result == {
            "statusCode": 200,
            "body": f"Saved document review row to s3://rep/{key}",
        }
        (row,) = table.to_pylist()
        assert row["document_id"] == "batch/a.pdf"
        assert row["cycle_id"] == "c-1"
        assert row["review_status"] == "Completed"
        assert row["disposition"] == "approved"
        assert row["profile"] == "claims"
        assert row["queue"] == "default"
        assert row["reviewer"] == "alice@example.com"
        assert row["reason_count"] == 2
        assert row["top_reason_kind"] == "low_confidence"
        assert row["reject_reason"] is None
        assert row["escalated"] is True
        assert row["machine_run_id"] == "run-1"
        assert row["final_run_id"] == "run-2"
        # Timestamps are normalised to naive UTC, whatever offset they were written in.
        assert row["reviewed_at"] == datetime.datetime(2025, 7, 7, 15, 0)
        assert row["enqueued_at"] == datetime.datetime(2025, 7, 7, 12, 30)
        assert isinstance(row["timestamp"], datetime.datetime)

    def test_the_key_is_deterministic_so_the_final_row_overwrites_phase_one(self, s3):
        doc = Document(
            id="a.pdf", input_key="a.pdf", initial_event_time="2025-07-07T23:30:00Z"
        )
        reporter = SaveReportingData("rep")
        reporter.save_document_reviews(doc, _review(review_status="Pending"))
        reporter.save_document_reviews(doc, _review(review_status="Completed"))
        first, _ = _written(s3, 0)
        second, table = _written(s3, 1)
        assert first == second
        assert table.column("review_status").to_pylist() == ["Completed"]

    def test_the_date_partition_is_the_utc_day_of_the_initial_event(self, s3):
        # 01:30 at +05:00 is 20:30 the previous day in UTC.
        doc = Document(
            id="a.pdf",
            input_key="a.pdf",
            initial_event_time="2025-07-08T01:30:00+05:00",
        )
        SaveReportingData("rep").save_document_reviews(doc, _review())
        key, _ = _written(s3)
        assert key == "document_reviews/date=2025-07-07/a.pdf.parquet"

    @pytest.mark.parametrize("event_time", [None, "not-a-time"])
    def test_a_missing_or_bad_event_time_partitions_by_today(self, s3, event_time):
        doc = Document(id="a.pdf", input_key="a.pdf", initial_event_time=event_time)
        SaveReportingData("rep").save_document_reviews(doc, _review())
        key, _ = _written(s3)
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        yesterday = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        ).strftime("%Y-%m-%d")
        assert key in {
            f"document_reviews/date={today}/a.pdf.parquet",
            f"document_reviews/date={yesterday}/a.pdf.parquet",
        }

    def test_unset_or_unparseable_review_times_are_null(self, s3):
        doc = Document(id="a.pdf", input_key="a.pdf")
        review = _review(reviewed_at=None, enqueued_at="yesterday", reason_count=0)
        SaveReportingData("rep").save_document_reviews(doc, review)
        (row,) = _written(s3)[1].to_pylist()
        assert row["reviewed_at"] is None
        assert row["enqueued_at"] is None
        assert row["reason_count"] == 0

    def test_a_naive_event_time_is_used_as_is(self, s3):
        doc = Document(
            id="x\\y.pdf",
            input_key="x\\y.pdf",
            initial_event_time="2025-01-02T03:04:05",
        )
        SaveReportingData("rep").save_document_reviews(doc, _review())
        key, _ = _written(s3)
        assert key == "document_reviews/date=2025-01-02/x_y.pdf.parquet"


@pytest.mark.unit
class TestSaveDispatch:
    def test_document_reviews_is_written_only_with_a_review(self, s3):
        doc = Document(id="a.pdf", input_key="a.pdf")
        reporter = SaveReportingData("rep")
        assert reporter.save(doc, ["document_reviews"]) == []
        s3.put_object.assert_not_called()

        results = reporter.save(doc, ["document_reviews"], review=_review())
        assert len(results) == 1 and results[0]["statusCode"] == 200
        assert s3.put_object.call_args.kwargs["Key"].startswith("document_reviews/")

    def test_a_review_without_the_data_type_writes_no_review_row(self, s3):
        doc = Document(id="a.pdf", input_key="a.pdf")
        assert SaveReportingData("rep").save(doc, [], review=_review()) == []
        s3.put_object.assert_not_called()


@pytest.mark.unit
class TestSectionReviewColumns:
    @staticmethod
    def _document():
        return Document(
            id="a.pdf",
            input_key="a.pdf",
            initial_event_time="2025-07-07T13:59:00Z",
            sections=[
                Section(
                    section_id="1",
                    classification="invoice",
                    confidence=0.9,
                    page_ids=["1"],
                    extraction_result_uri="s3://out/a.pdf/sections/1/result.json",
                )
            ],
        )

    @staticmethod
    def _section_table(s3):
        keys = [c.kwargs["Key"] for c in s3.put_object.call_args_list]
        index = next(
            i for i, k in enumerate(keys) if k.startswith("document_sections/")
        )
        return _written(s3, index)[1]

    @patch(
        "idp_common.reporting.save_reporting_data.get_json_content",
        return_value={"inference_result": {"total": 10}},
    )
    def test_rows_of_a_reviewed_document_carry_the_review_columns(self, _json, s3):
        SaveReportingData("rep").save(
            self._document(), ["sections"], review=_review(queue="claims")
        )
        (row,) = self._section_table(s3).to_pylist()
        assert row["review_status"] == "Completed"
        assert row["reviewed_by"] == "alice@example.com"
        assert row["reviewed_at"] == "2025-07-07T15:00:00Z"
        assert row["review_queue"] == "claims"

    @patch(
        "idp_common.reporting.save_reporting_data.get_json_content",
        return_value={"inference_result": {"total": 10}},
    )
    def test_rows_of_any_other_document_are_unchanged(self, _json, s3):
        reporter = SaveReportingData("rep")
        reporter.save(self._document(), ["sections"], review=_review())
        s3.put_object.reset_mock()
        # The review from the previous call must not leak into the next document.
        reporter.save(self._document(), ["sections"])
        columns = set(self._section_table(s3).column_names)
        assert not columns & _REVIEW_COLUMNS
