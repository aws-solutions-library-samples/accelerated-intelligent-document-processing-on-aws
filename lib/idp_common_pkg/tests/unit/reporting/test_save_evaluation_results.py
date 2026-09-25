# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for `SaveReportingData.save_evaluation_results`.

This writes the three Parquet tables that back the evaluation dashboard and every
accuracy figure quoted from Athena: document-level metrics, per-section metrics, and
one row per compared attribute. Nothing downstream re-derives any of it, so a wrong
value here becomes a wrong published number with no second source to disagree with.

Four properties are asserted deliberately, because each has a silent failure mode.

**`None` must stay `None` and not become `0.0`.** `weighted_overall_score` is
deliberately absent for documents and sections that were excluded from scoring — a
section with no extractable schema is a no-op, not a section that scored zero. Parquet
and Athena treat the absence as SQL NULL, so `AVG()` skips it; substituting `0.0` drags
every aggregate down and looks like a real regression. The distinction is invisible in
the happy path, so it is asserted directly. `doc_split_metrics` has the same shape for
documents processed before those metrics existed.

**The S3 key carries the partition, and the partition comes from the document's own
event time.** Athena reads `date=` as a partition column, so a record written under
today's date for a document ingested last week is silently misfiled and drops out of
any time-bounded query. The fallback to wall-clock time when the timestamp is missing or
unparseable is correct but must be *reached* rather than hit by accident, so all three
paths are covered.

**The timestamp in the filename is what stops a re-run overwriting the first run.** Two
evaluations of the same document on the same day would otherwise collide on one key, and
the earlier result would vanish rather than being superseded visibly.

**A document id containing a slash would create a nested prefix**, so it is escaped.
Document ids here are S3 input keys and almost always contain slashes, making this the
normal case rather than an edge one.

`_save_records_as_parquet` is patched throughout: these tests are about which records
and which keys are produced, not about pyarrow serialisation, which
`test_save_reporting_data.py` covers.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from idp_common.models import Document
from idp_common.reporting.save_reporting_data import SaveReportingData

MODULE = "idp_common.reporting.save_reporting_data"


@pytest.fixture
def saver():
    """A SaveReportingData with a mocked S3 client and a stubbed parquet writer."""
    with patch("boto3.client", return_value=MagicMock()):
        instance = SaveReportingData(reporting_bucket="reporting-bucket")
    instance._save_records_as_parquet = MagicMock()
    return instance


def _document(**overrides) -> Document:
    doc = Document(id="batch-1/doc.pdf", input_key="batch-1/doc.pdf")
    doc.evaluation_results_uri = "s3://b/batch-1/doc.pdf/evaluation/results.json"
    doc.initial_event_time = "2025-09-10T12:03:27.256164+00:00"
    for key, value in overrides.items():
        setattr(doc, key, value)
    return doc


def _eval_result(**overrides) -> dict:
    result = {
        "overall_metrics": {
            "accuracy": 0.85,
            "precision": 0.9,
            "recall": 0.8,
            "f1_score": 0.85,
            "false_alarm_rate": 0.1,
            "false_discovery_rate": 0.05,
            "weighted_overall_score": 0.87,
        },
        "execution_time": 1.5,
        "section_results": [],
    }
    result.update(overrides)
    return result


def _writes(saver) -> dict[str, tuple[list, str]]:
    """The parquet writes, keyed by the table name in their S3 prefix."""
    out = {}
    for call in saver._save_records_as_parquet.call_args_list:
        records, key = call.args[0], call.args[1]
        table = key.split("/")[1]
        out[table] = (records, key)
    return out


@pytest.mark.unit
class TestPreconditions:
    """The two cases that produce no write at all, and the one that reports an error."""

    def test_a_document_with_no_evaluation_uri_returns_none(self, saver):
        doc = _document()
        doc.evaluation_results_uri = None
        assert saver.save_evaluation_results(doc) is None
        saver._save_records_as_parquet.assert_not_called()

    def test_empty_evaluation_results_return_none(self, saver):
        # An empty dict is distinct from a load failure: nothing was wrong, there is
        # just nothing to report, so this is not an error status.
        with patch(f"{MODULE}.get_json_content", return_value={}):
            assert saver.save_evaluation_results(_document()) is None
        saver._save_records_as_parquet.assert_not_called()

    def test_a_load_failure_returns_a_500_rather_than_raising(self, saver):
        # The caller is a Lambda handling a completed document; raising here would fail
        # the whole document for a reporting problem after processing already succeeded.
        with patch(f"{MODULE}.get_json_content", side_effect=RuntimeError("denied")):
            result = saver.save_evaluation_results(_document())
        assert result["statusCode"] == 500
        assert "Error loading evaluation results" in result["body"]
        saver._save_records_as_parquet.assert_not_called()

    def test_a_successful_save_reports_200(self, saver):
        with patch(f"{MODULE}.get_json_content", return_value=_eval_result()):
            result = saver.save_evaluation_results(_document())
        assert result == {
            "statusCode": 200,
            "body": "Successfully saved evaluation results to reporting bucket",
        }


@pytest.mark.unit
class TestDocumentRecord:
    """The document-level row: the numbers the dashboard's headline figures come from."""

    def _record(self, saver, eval_result=None, doc=None):
        with patch(
            f"{MODULE}.get_json_content", return_value=eval_result or _eval_result()
        ):
            saver.save_evaluation_results(doc or _document())
        return _writes(saver)["document_metrics"][0][0]

    def test_the_overall_metrics_are_carried_across(self, saver):
        record = self._record(saver)
        assert record["accuracy"] == 0.85
        assert record["precision"] == 0.9
        assert record["recall"] == 0.8
        assert record["f1_score"] == 0.85
        assert record["false_alarm_rate"] == 0.1
        assert record["false_discovery_rate"] == 0.05
        assert record["weighted_overall_score"] == 0.87
        assert record["execution_time"] == 1.5

    def test_missing_metrics_default_to_zero(self, saver):
        # A truncated evaluation result must still produce a row; the alternative is a
        # document missing from the dashboard entirely, which reads as "not processed".
        record = self._record(saver, _eval_result(overall_metrics={}))
        assert record["accuracy"] == 0.0
        assert record["execution_time"] == 1.5

    def test_an_absent_weighted_score_stays_null_rather_than_becoming_zero(self, saver):
        """The one metric that must NOT default to 0.0.

        It is absent for documents whose sections were all no-ops (no extractable
        schema). Athena treats NULL as "skip this row" in `AVG()`; 0.0 would be averaged
        in and would look like a genuine accuracy drop across the whole corpus.
        """
        record = self._record(saver, _eval_result(overall_metrics={"accuracy": 1.0}))
        assert record["weighted_overall_score"] is None, (
            "an excluded document scored 0.0 instead of NULL, which skews every average"
        )

    def test_doc_split_metrics_are_carried_when_present(self, saver):
        record = self._record(
            saver,
            _eval_result(
                doc_split_metrics={
                    "page_level_accuracy": 0.95,
                    "split_accuracy_without_order": 0.9,
                    "split_accuracy_with_order": 0.85,
                    "total_pages": 10,
                    "total_splits": 3,
                    "correctly_classified_pages": 9,
                    "correctly_split_without_order": 2,
                    "correctly_split_with_order": 2,
                }
            ),
        )
        assert record["page_level_accuracy"] == 0.95
        assert record["total_pages"] == 10
        assert record["correctly_split_with_order"] == 2

    @pytest.mark.parametrize("absent", [{}, None], ids=["empty-dict", "absent"])
    def test_doc_split_metrics_are_null_when_absent(self, saver, absent):
        # Backward compatibility: documents evaluated before these metrics existed have
        # no value, and 0 would read as "nothing was classified correctly".
        record = self._record(saver, _eval_result(doc_split_metrics=absent))
        for field in (
            "page_level_accuracy",
            "split_accuracy_without_order",
            "split_accuracy_with_order",
            "total_pages",
            "total_splits",
            "correctly_classified_pages",
            "correctly_split_without_order",
            "correctly_split_with_order",
        ):
            assert record[field] is None, f"{field} defaulted instead of staying NULL"

    def test_the_config_version_defaults_to_the_literal_default(self, saver):
        # The reporting tables join on this, so an empty string would create a second
        # apparent config version that no query knows to look for.
        doc = _document()
        doc.config_version = None
        assert self._record(saver, doc=doc)["config_version"] == "default"

    def test_an_explicit_config_version_is_used(self, saver):
        doc = _document()
        doc.config_version = "v3"
        assert self._record(saver, doc=doc)["config_version"] == "v3"

    def test_the_input_key_is_recorded_alongside_the_id(self, saver):
        record = self._record(saver)
        assert record["document_id"] == "batch-1/doc.pdf"
        assert record["input_key"] == "batch-1/doc.pdf"


@pytest.mark.unit
class TestPartitioningAndKeys:
    """Where the records land, which decides whether Athena can find them."""

    def _keys(self, saver, doc=None, eval_result=None):
        with patch(
            f"{MODULE}.get_json_content", return_value=eval_result or _eval_result()
        ):
            saver.save_evaluation_results(doc or _document())
        return {table: key for table, (_, key) in _writes(saver).items()}

    def test_the_partition_comes_from_the_documents_event_time(self, saver):
        # Not from now(): a backfill run today for a document ingested in September must
        # land in September's partition or every date-bounded query misses it.
        key = self._keys(saver)["document_metrics"]
        assert "date=2025-09-10/" in key

    def test_a_trailing_Z_timestamp_is_parsed(self, saver):
        # fromisoformat rejects a bare "Z" before Python 3.11, and the code replaces it
        # for that reason; this is the shape S3 event times actually arrive in.
        doc = _document(initial_event_time="2025-01-05T08:00:00Z")
        assert "date=2025-01-05/" in self._keys(saver, doc)["document_metrics"]

    def test_an_unparseable_event_time_falls_back_to_now(self, saver):
        doc = _document(initial_event_time="not a timestamp")
        key = self._keys(saver, doc)["document_metrics"]
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        assert f"date={today}/" in key

    def test_a_missing_event_time_falls_back_to_now(self, saver):
        doc = _document(initial_event_time=None)
        key = self._keys(saver, doc)["document_metrics"]
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        assert f"date={today}/" in key

    def test_slashes_in_the_document_id_are_escaped(self, saver):
        # Document ids are S3 input keys, so they almost always contain slashes. Left
        # unescaped they would create a nested prefix under the partition and break the
        # flat layout the Glue table expects.
        key = self._keys(saver)["document_metrics"]
        filename = key.split("/")[-1]
        assert filename.startswith("batch-1_doc.pdf_")
        assert key.count("/") == 3, f"the id created extra prefix levels: {key}"

    def test_backslashes_are_escaped_too(self, saver):
        doc = _document(id="win\\path\\doc.pdf")
        key = self._keys(saver, doc)["document_metrics"]
        assert "win_path_doc.pdf" in key

    def test_the_filename_carries_a_millisecond_timestamp(self, saver):
        # This is what keeps a re-evaluation from overwriting the first run's row: both
        # would otherwise write the same key on the same day and the earlier result
        # would disappear rather than being visibly superseded.
        key = self._keys(saver)["document_metrics"]
        stamp = key.split("batch-1_doc.pdf_")[1].removesuffix("_results.parquet")
        assert len(stamp) == len("20250910_120327_256"), stamp
        assert stamp.startswith("20250910_120327")

    def test_each_table_gets_its_own_prefix(self, saver):
        keys = self._keys(
            saver,
            eval_result=_eval_result(
                section_results=[
                    {
                        "section_id": "s1",
                        "document_class": "invoice",
                        "metrics": {},
                        "attributes": [{"name": "total"}],
                    }
                ]
            ),
        )
        assert keys["document_metrics"].startswith(
            "evaluation_metrics/document_metrics/"
        )
        assert keys["section_metrics"].startswith("evaluation_metrics/section_metrics/")
        assert keys["attribute_metrics"].startswith(
            "evaluation_metrics/attribute_metrics/"
        )

    def test_the_id_falls_back_to_the_input_key_then_to_unknown(self, saver):
        doc = _document()
        doc.id = None
        assert "batch-1_doc.pdf" in self._keys(saver, doc)["document_metrics"]
        saver._save_records_as_parquet.reset_mock()
        doc2 = _document()
        doc2.id = None
        doc2.input_key = None
        assert "unknown" in self._keys(saver, doc2)["document_metrics"]


@pytest.mark.unit
class TestSectionAndAttributeRecords:
    """The per-section and per-attribute rows."""

    SECTIONS = [
        {
            "section_id": "s1",
            "document_class": "invoice",
            "metrics": {
                "accuracy": 0.9,
                "precision": 0.8,
                "recall": 0.7,
                "f1_score": 0.75,
                "false_alarm_rate": 0.2,
                "false_discovery_rate": 0.1,
                "weighted_overall_score": 0.82,
            },
            "attributes": [
                {
                    "name": "total",
                    "expected": "100.00",
                    "actual": "100.00",
                    "matched": True,
                    "score": 1.0,
                    "reason": "exact",
                    "evaluation_method": "EXACT",
                    "confidence": 0.99,
                    "confidence_threshold": 0.9,
                    "weight": 2.0,
                },
                {"name": "date"},
            ],
        },
        {
            "section_id": "s2",
            "document_class": "receipt",
            "metrics": {},
            "attributes": [],
        },
    ]

    def _records(self, saver, sections=None):
        with patch(
            f"{MODULE}.get_json_content",
            return_value=_eval_result(
                section_results=sections if sections is not None else self.SECTIONS
            ),
        ):
            saver.save_evaluation_results(_document())
        return _writes(saver)

    def test_one_row_per_section(self, saver):
        records = self._records(saver)["section_metrics"][0]
        assert [r["section_id"] for r in records] == ["s1", "s2"]
        assert records[0]["section_type"] == "invoice"
        assert records[0]["accuracy"] == 0.9
        assert records[0]["weighted_overall_score"] == 0.82

    def test_an_excluded_sections_weighted_score_stays_null(self, saver):
        # Same reasoning as the document-level case, one level down: a no-op section
        # must not average in as a zero across a per-section rollup.
        records = self._records(saver)["section_metrics"][0]
        assert records[1]["weighted_overall_score"] is None
        assert records[1]["accuracy"] == 0.0, "the other metrics still default"

    def test_one_row_per_attribute_across_all_sections(self, saver):
        records = self._records(saver)["attribute_metrics"][0]
        assert [r["attribute_name"] for r in records] == ["total", "date"]
        assert all(r["document_id"] == "batch-1/doc.pdf" for r in records)

    def test_an_attribute_carries_its_sections_identity(self, saver):
        # The attribute table is queried on its own, so without these it could not be
        # attributed to a section or a document class.
        record = self._records(saver)["attribute_metrics"][0][0]
        assert record["section_id"] == "s1"
        assert record["section_type"] == "invoice"

    def test_attribute_fields_are_carried_across(self, saver):
        record = self._records(saver)["attribute_metrics"][0][0]
        assert record["expected"] == "100.00"
        assert record["actual"] == "100.00"
        assert record["matched"] is True
        assert record["score"] == 1.0
        assert record["reason"] == "exact"
        assert record["evaluation_method"] == "EXACT"
        assert record["weight"] == 2.0

    def test_an_absent_weight_defaults_to_one(self, saver):
        # Weight multiplies into the weighted score, so a missing weight defaulting to
        # 0.0 would silently remove the attribute from scoring altogether.
        record = self._records(saver)["attribute_metrics"][0][1]
        assert record["weight"] == 1.0

    def test_an_explicitly_null_weight_also_defaults_to_one(self, saver):
        # `attr.get("weight")` returns None for both absent and null, and the code tests
        # for None rather than falsiness -- so a real 0.0 weight is preserved. Both
        # branches are covered here and below.
        records = self._records(
            saver,
            [
                {
                    "section_id": "s",
                    "metrics": {},
                    "attributes": [{"name": "a", "weight": None}],
                }
            ],
        )
        assert records["attribute_metrics"][0][0]["weight"] == 1.0

    def test_a_genuine_zero_weight_is_preserved(self, saver):
        records = self._records(
            saver,
            [
                {
                    "section_id": "s",
                    "metrics": {},
                    "attributes": [{"name": "a", "weight": 0.0}],
                }
            ],
        )
        assert records["attribute_metrics"][0][0]["weight"] == 0.0, (
            "a deliberate zero weight was replaced with 1.0"
        )

    def test_confidence_values_are_serialised_to_strings(self, saver):
        # The column is declared pa.string(), so a float reaching it unserialised would
        # fail the parquet write for the whole document.
        record = self._records(saver)["attribute_metrics"][0][0]
        assert record["confidence"] == "0.99"
        assert record["confidence_threshold"] == "0.9"

    def test_no_sections_writes_only_the_document_table(self, saver):
        # A document whose evaluation produced no sections still belongs in the
        # dashboard; writing an empty parquet file for the other two would create rows
        # with no data rather than no rows.
        written = self._records(saver, [])
        assert set(written) == {"document_metrics"}

    def test_sections_with_no_attributes_write_no_attribute_table(self, saver):
        written = self._records(
            saver, [{"section_id": "s", "metrics": {}, "attributes": []}]
        )
        assert set(written) == {"document_metrics", "section_metrics"}


@pytest.mark.unit
class TestSaveDispatch:
    """save(): which data types route to which method."""

    @pytest.mark.parametrize(
        "requested, expected_method",
        [
            ("evaluation_results", "save_evaluation_results"),
            ("metering", "save_metering_data"),
            ("sections", "save_document_sections"),
            ("rule_validation_results", "save_rule_validation_results"),
        ],
    )
    def test_each_data_type_calls_its_own_method(
        self, saver, requested, expected_method
    ):
        with patch.object(
            saver, expected_method, return_value={"statusCode": 200}
        ) as m:
            results = saver.save(_document(), [requested])
        m.assert_called_once()
        assert results == [{"statusCode": 200}]

    def test_an_unknown_data_type_is_ignored(self, saver):
        # Silently, and that is the intended behaviour: the list comes from
        # configuration, so an unrecognised entry should not fail a completed document.
        assert saver.save(_document(), ["not_a_real_type"]) == []

    def test_a_method_returning_none_contributes_no_result(self, saver):
        with patch.object(saver, "save_evaluation_results", return_value=None):
            assert saver.save(_document(), ["evaluation_results"]) == []

    def test_several_data_types_are_all_processed(self, saver):
        with (
            patch.object(saver, "save_evaluation_results", return_value={"a": 1}),
            patch.object(saver, "save_metering_data", return_value={"b": 2}),
        ):
            results = saver.save(_document(), ["evaluation_results", "metering"])
        assert results == [{"a": 1}, {"b": 2}]
