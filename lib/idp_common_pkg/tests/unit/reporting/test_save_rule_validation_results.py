# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the rule-validation half of `SaveReportingData`:
`save_rule_validation_results` and `_create_or_update_rule_validation_glue_table`.

These write the two Parquet tables behind compliance reporting — one summary row per
document and one row per evaluated rule. A compliance answer is the output a reader acts
on, so a miscounted `fail_count` or a rule attributed to the wrong policy type is worse
than a missing row: it is a confident wrong answer.

Four things shape these tests.

**The URI is derived, not given.** The consolidated summary is written as Markdown and
the JSON sibling is found by replacing `.md` with `.json`. `str.replace` is unanchored, so
this is asserted on the resulting URI rather than assumed — and the fallback to
`output_uri` when no `summary` attribute exists is a separate path, covered separately.

**Counts come from nested `.get()` chains with zero defaults.** A payload missing
`overall_statistics`, or with `recommendation_counts` absent, must yield zeros rather than
raising — a document that validated fine should not be dropped from the dashboard because
its summary shape changed. But `information_not_found_count` reads a key with a *space* in
it (`"Information Not Found"`), which is the kind of literal that breaks silently, so it
is asserted by value.

**The timestamp is computed in UTC and then stripped to naive**, deliberately: the Parquet
schema declares naive `timestamp("ms")`, and making it timezone-aware would change the
Glue column type to `timestamp with time zone` and break the already-created tables. So
the tests assert both that an offset input is *converted* to UTC rather than merely
truncated, and that what lands in the record is naive.

**`document_id` is coalesced explicitly rather than with `dict.get`'s default**, because
`get` returns `None` for a key that is present and null. Both shapes appear in real
payloads and both reach `re.sub`, which raises on `None`.

`_save_records_as_parquet` is patched: these tests are about which records and keys are
produced, not pyarrow serialisation.
"""

from __future__ import annotations

import datetime
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pytest

from idp_common.models import Document
from idp_common.reporting.save_reporting_data import SaveReportingData

MODULE = "idp_common.reporting.save_reporting_data"


@pytest.fixture
def saver():
    with patch("boto3.client", return_value=MagicMock()):
        instance = SaveReportingData(reporting_bucket="reporting-bucket")
    instance._save_records_as_parquet = MagicMock()
    instance._create_or_update_rule_validation_glue_table = MagicMock(return_value=True)
    return instance


def _document(*, summary_uri=".../consolidated.md", output_uri=None, **overrides):
    doc = Document(id="batch-1/doc.pdf", input_key="batch-1/doc.pdf")
    doc.initial_event_time = "2025-09-10T12:03:27.256164+00:00"
    if summary_uri is not None:
        result = SimpleNamespace(
            summary=SimpleNamespace(consolidated_summary_uri=summary_uri),
            output_uri=output_uri,
        )
    else:
        result = SimpleNamespace(output_uri=output_uri)
    doc.rule_validation_result = result
    for key, value in overrides.items():
        setattr(doc, key, value)
    return doc


def _payload(**overrides) -> dict:
    payload = {
        "document_id": "batch-1/doc.pdf",
        "overall_status": "FAIL",
        "total_policy_types": 2,
        "overall_statistics": {
            "total_rules": 5,
            "recommendation_counts": {
                "Pass": 3,  # nosec B105 - a verdict tally key, not a credential
                "Fail": 1,
                "Information Not Found": 1,
            },
        },
        "rule_details": {},
    }
    payload.update(overrides)
    return payload


def _writes(saver) -> dict[str, tuple[list, str]]:
    out = {}
    for call in saver._save_records_as_parquet.call_args_list:
        records, key = call.args[0], call.args[1]
        out[key.split("/")[0]] = (records, key)
    return out


@pytest.mark.unit
class TestPreconditions:
    """The cases that write nothing."""

    def test_a_document_with_no_rule_validation_result_returns_none(self, saver):
        doc = Document(id="d", input_key="d")
        assert saver.save_rule_validation_results(doc) is None
        saver._save_records_as_parquet.assert_not_called()

    def test_a_falsy_rule_validation_result_returns_none(self, saver):
        doc = Document(id="d", input_key="d")
        doc.rule_validation_result = None
        assert saver.save_rule_validation_results(doc) is None

    def test_neither_a_summary_nor_an_output_uri_returns_none(self, saver):
        doc = _document(summary_uri=None, output_uri=None)
        assert saver.save_rule_validation_results(doc) is None
        saver._save_records_as_parquet.assert_not_called()

    def test_an_empty_payload_returns_none(self, saver):
        with patch(f"{MODULE}.get_json_content", return_value={}):
            assert saver.save_rule_validation_results(_document()) is None

    def test_a_load_failure_returns_a_500_rather_than_raising(self, saver):
        # Reporting runs after the document has already been validated; raising would
        # fail a document whose compliance answer was computed correctly.
        with patch(f"{MODULE}.get_json_content", side_effect=RuntimeError("denied")):
            result = saver.save_rule_validation_results(_document())
        assert result["statusCode"] == 500
        assert "Error loading rule validation results" in result["body"]


@pytest.mark.unit
class TestUriDerivation:
    """Which URI is read, and how the .json sibling is found."""

    def test_the_summary_uri_has_its_extension_swapped(self, saver):
        doc = _document(summary_uri="s3://b/doc/consolidated_summary.md")
        with patch(f"{MODULE}.get_json_content", return_value=_payload()) as get:
            saver.save_rule_validation_results(doc)
        assert get.call_args.args[0] == "s3://b/doc/consolidated_summary.json"

    def test_the_output_uri_is_the_fallback_when_there_is_no_summary(self, saver):
        doc = _document(summary_uri=None, output_uri="s3://b/doc/report.md")
        with patch(f"{MODULE}.get_json_content", return_value=_payload()) as get:
            saver.save_rule_validation_results(doc)
        assert get.call_args.args[0] == "s3://b/doc/report.json"

    def test_the_summary_uri_wins_over_the_output_uri(self, saver):
        doc = _document(summary_uri="s3://b/summary.md", output_uri="s3://b/other.md")
        with patch(f"{MODULE}.get_json_content", return_value=_payload()) as get:
            saver.save_rule_validation_results(doc)
        assert get.call_args.args[0] == "s3://b/summary.json"

    def test_an_md_substring_elsewhere_in_the_path_is_also_replaced(self, saver):
        """`str.replace` is unanchored, so the FIRST `.md` anywhere is rewritten.

        A bucket or prefix containing `.md` — `s3://b/docs.md.archive/x.md` — has its
        prefix rewritten instead of its extension, and the read then targets a key that
        does not exist, which surfaces as the 500 above rather than as a wrong file.
        Pinned in the direction that is true so a switch to an anchored replacement is a
        visible change; no caller in this repository produces such a URI today.
        """
        doc = _document(summary_uri="s3://b/docs.md.archive/report.md")
        with patch(f"{MODULE}.get_json_content", return_value=_payload()) as get:
            saver.save_rule_validation_results(doc)
        assert get.call_args.args[0] == "s3://b/docs.json.archive/report.json"


@pytest.mark.unit
class TestDocumentSummaryRecord:
    """The one-row-per-document compliance summary."""

    def _record(self, saver, payload=None, doc=None):
        with patch(f"{MODULE}.get_json_content", return_value=payload or _payload()):
            saver.save_rule_validation_results(doc or _document())
        return _writes(saver)["rule_validation_summary"][0][0]

    def test_the_counts_are_carried_across(self, saver):
        record = self._record(saver)
        assert record["overall_status"] == "FAIL"
        assert record["total_policy_types"] == 2
        assert record["total_rules"] == 5
        assert record["pass_count"] == 3
        assert record["fail_count"] == 1
        assert record["information_not_found_count"] == 1

    def test_the_information_not_found_key_has_spaces_in_it(self, saver):
        # `recommendation_counts["Information Not Found"]` -- a spaced literal that a
        # rename on either side would break silently, leaving the count permanently 0
        # and making unanswered rules look answered.
        record = self._record(
            saver,
            _payload(
                overall_statistics={
                    "total_rules": 1,
                    "recommendation_counts": {"Information Not Found": 7},
                }
            ),
        )
        assert record["information_not_found_count"] == 7

    def test_a_missing_statistics_block_yields_zeros_not_an_error(self, saver):
        record = self._record(saver, _payload(overall_statistics={}))
        assert record["total_rules"] == 0
        assert record["pass_count"] == 0
        assert record["fail_count"] == 0
        assert record["information_not_found_count"] == 0

    def test_an_absent_status_becomes_UNKNOWN(self, saver):
        # Not an empty string: the column is queried and grouped on, and "" would form a
        # silent extra category alongside PASS and FAIL.
        payload = _payload()
        del payload["overall_status"]
        assert self._record(saver, payload)["overall_status"] == "UNKNOWN"

    def test_the_input_key_comes_from_the_document_not_the_payload(self, saver):
        record = self._record(saver)
        assert record["input_key"] == "batch-1/doc.pdf"


@pytest.mark.unit
class TestDocumentIdCoalescing:
    """document_id: three sources, and why `dict.get` alone is not enough."""

    def _key(self, saver, payload, doc):
        with patch(f"{MODULE}.get_json_content", return_value=payload):
            saver.save_rule_validation_results(doc)
        return _writes(saver)["rule_validation_summary"][1]

    def test_the_payloads_id_is_preferred(self, saver):
        key = self._key(saver, _payload(document_id="from-payload"), _document())
        assert "from-payload_summary.parquet" in key

    def test_an_explicitly_null_id_in_the_payload_falls_back_to_the_document(
        self, saver
    ):
        # `.get("document_id", default)` returns None for a present-but-null key, so a
        # default alone would put None into re.sub() and raise, aborting the save for a
        # document whose validation succeeded.
        key = self._key(saver, _payload(document_id=None), _document())
        assert "batch-1_doc.pdf_summary.parquet" in key

    def test_both_being_absent_falls_back_to_unknown(self, saver):
        doc = _document()
        doc.id = None
        key = self._key(saver, _payload(document_id=None), doc)
        assert "unknown_summary.parquet" in key

    def test_slashes_and_backslashes_are_escaped(self, saver):
        key = self._key(saver, _payload(document_id="a/b\\c.pdf"), _document())
        assert "a_b_c.pdf_summary.parquet" in key
        assert key.count("/") == 2, f"the id created extra prefix levels: {key}"


@pytest.mark.unit
class TestTimestampHandling:
    """UTC-then-strip, and the partition it produces."""

    def _record_and_key(self, saver, doc):
        with patch(f"{MODULE}.get_json_content", return_value=_payload()):
            saver.save_rule_validation_results(doc)
        records, key = _writes(saver)["rule_validation_summary"]
        return records[0], key

    def test_the_stored_timestamp_is_naive(self, saver):
        # The parquet schema declares a naive timestamp("ms"). A tz-aware value would map
        # to a different Glue column type and break the existing tables, so this is a
        # compatibility constraint rather than a style choice.
        record, _ = self._record_and_key(saver, _document())
        assert record["validation_date"].tzinfo is None

    def test_an_offset_timestamp_is_CONVERTED_to_utc_not_truncated(self, saver):
        # 09:30 at +05:30 is 04:00 UTC. Dropping the offset instead of converting would
        # store 09:30 and misfile anything near a date boundary by a whole day.
        doc = _document(initial_event_time="2025-09-10T09:30:00+05:30")
        record, key = self._record_and_key(saver, doc)
        assert record["validation_date"] == datetime.datetime(2025, 9, 10, 4, 0)
        assert "date=2025-09-10/" in key

    def test_a_conversion_across_midnight_moves_the_partition(self, saver):
        # 01:00 at +05:30 is 19:30 the PREVIOUS day in UTC, so the partition must move.
        doc = _document(initial_event_time="2025-09-10T01:00:00+05:30")
        record, key = self._record_and_key(saver, doc)
        assert record["validation_date"] == datetime.datetime(2025, 9, 9, 19, 30)
        assert "date=2025-09-09/" in key

    def test_a_trailing_Z_is_accepted(self, saver):
        doc = _document(initial_event_time="2025-01-05T08:00:00Z")
        record, key = self._record_and_key(saver, doc)
        assert record["validation_date"] == datetime.datetime(2025, 1, 5, 8, 0)
        assert "date=2025-01-05/" in key

    def test_a_naive_timestamp_is_used_as_given(self, saver):
        doc = _document(initial_event_time="2025-03-04T05:06:07")
        record, _ = self._record_and_key(saver, doc)
        assert record["validation_date"] == datetime.datetime(2025, 3, 4, 5, 6, 7)

    @pytest.mark.parametrize("value", ["not a timestamp", None])
    def test_an_unusable_event_time_falls_back_to_utc_now(self, saver, value):
        doc = _document(initial_event_time=value)
        record, key = self._record_and_key(saver, doc)
        assert record["validation_date"].tzinfo is None
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        assert f"date={today}/" in key


@pytest.mark.unit
class TestRuleDetailRecords:
    """One row per rule, flattened out of the per-policy-type mapping."""

    DETAILS = {
        "Lending": {
            "rules": [
                {
                    "rule": "LTV must not exceed 80%",
                    "recommendation": "Fail",
                    "reasoning": "LTV was 85%",
                    "supporting_pages": [2, 3],
                },
                {"rule": "Income documented"},
            ]
        },
        "Insurance": {
            "rules": [{"rule": "Coverage present", "recommendation": "Pass"}]
        },
    }

    def _records(self, saver, details=None):
        with patch(
            f"{MODULE}.get_json_content",
            return_value=_payload(
                rule_details=details if details is not None else self.DETAILS
            ),
        ):
            saver.save_rule_validation_results(_document())
        written = _writes(saver)
        return written.get("rule_validation_details", ([], None))

    def test_rules_from_every_policy_type_are_flattened(self, saver):
        records, _ = self._records(saver)
        assert [r["rule"] for r in records] == [
            "LTV must not exceed 80%",
            "Income documented",
            "Coverage present",
        ]

    def test_each_rule_keeps_its_own_policy_type(self, saver):
        # The rule text alone is ambiguous across policy types, and a compliance report
        # attributing a lending rule to insurance is a wrong answer rather than a gap.
        records, _ = self._records(saver)
        assert [r["policy_type"] for r in records] == [
            "Lending",
            "Lending",
            "Insurance",
        ]

    def test_rule_fields_are_carried_across(self, saver):
        record = self._records(saver)[0][0]
        assert record["recommendation"] == "Fail"
        assert record["reasoning"] == "LTV was 85%"

    def test_supporting_pages_are_serialised_as_json(self, saver):
        # The column is pa.string(), so the list has to be encoded; the UI parses it back
        # to render page citations, and a citation pointing at the wrong page is worse
        # than none.
        record = self._records(saver)[0][0]
        assert record["supporting_pages"] == json.dumps([2, 3])
        assert json.loads(record["supporting_pages"]) == [2, 3]

    def test_absent_supporting_pages_serialise_to_an_empty_list(self, saver):
        record = self._records(saver)[0][1]
        assert record["supporting_pages"] == "[]"

    def test_absent_rule_fields_get_explicit_placeholders(self, saver):
        record = self._records(saver)[0][1]
        assert record["recommendation"] == "Unknown"
        assert record["reasoning"] == ""

    def test_no_rule_details_writes_only_the_summary(self, saver):
        # Writing an empty details file would add a file Athena reads as zero rows, which
        # is the same outcome with extra objects to pay for and scan.
        self._records(saver, {})
        assert set(_writes(saver)) == {"rule_validation_summary"}

    def test_a_policy_type_with_an_empty_rule_list_contributes_nothing(self, saver):
        records, _ = self._records(saver, {"Lending": {"rules": []}})
        assert records == []

    def test_a_policy_type_with_no_rules_key_is_tolerated(self, saver):
        records, _ = self._records(saver, {"Lending": {}})
        assert records == []

    def test_the_status_line_counts_the_rules_written(self, saver):
        with patch(
            f"{MODULE}.get_json_content",
            return_value=_payload(rule_details=self.DETAILS),
        ):
            result = saver.save_rule_validation_results(_document())
        assert result["statusCode"] == 200
        assert "1 summary + 3 rule details" in result["body"]

    def test_the_summary_key_carries_no_timestamp_so_a_rerun_overwrites(self, saver):
        """Unlike the evaluation tables, these keys are `<doc_id>_summary.parquet`.

        The evaluation writer deliberately embeds a millisecond timestamp so a
        re-processed document does not overwrite its earlier row. These two do not, so
        re-validating a document replaces its previous compliance summary and rule
        details rather than adding to them.

        Asserted rather than judged: latest-wins is a defensible choice for a compliance
        answer, where the current verdict is usually the one that matters. It is pinned
        because the asymmetry with the sibling tables is undocumented, so anyone relying
        on rule-validation history being retained would be relying on something that is
        not true.
        """
        with patch(f"{MODULE}.get_json_content", return_value=_payload()):
            saver.save_rule_validation_results(_document())
        key = _writes(saver)["rule_validation_summary"][1]
        assert (
            key
            == "rule_validation_summary/date=2025-09-10/batch-1_doc.pdf_summary.parquet"
        )


@pytest.mark.unit
class TestRuleValidationGlueTable:
    """_create_or_update_rule_validation_glue_table."""

    SCHEMA = pa.schema([("document_id", pa.string()), ("total_rules", pa.int32())])

    def _saver(self, *, glue=True, database="idp_db"):
        with patch("boto3.client", return_value=MagicMock()):
            instance = SaveReportingData(reporting_bucket="reporting-bucket")
        instance.glue_client = MagicMock() if glue else None
        instance.database_name = database
        return instance

    @pytest.mark.parametrize(
        "glue, database", [(False, "idp_db"), (True, None), (True, "")]
    )
    def test_it_is_skipped_when_glue_is_not_configured(self, glue, database):
        # Reporting to S3 still works without Glue; the tables just are not queryable.
        # Skipping must not raise, or a deployment without analytics would fail every
        # document's reporting step.
        saver = self._saver(glue=glue, database=database)
        assert (
            saver._create_or_update_rule_validation_glue_table("t", self.SCHEMA)
            is False
        )

    def test_an_existing_table_with_all_the_columns_is_left_alone(self):
        # Calling update_table on every document would be a needless write per document.
        saver = self._saver()
        saver.glue_client.get_table.return_value = {
            "Table": {
                "StorageDescriptor": {
                    "Columns": [{"Name": "document_id"}, {"Name": "total_rules"}]
                }
            }
        }
        assert (
            saver._create_or_update_rule_validation_glue_table(
                "rule_validation_summary", self.SCHEMA
            )
            is True
        )
        saver.glue_client.update_table.assert_not_called()
        saver.glue_client.create_table.assert_not_called()

    def test_a_table_missing_a_column_is_updated(self):
        # A new field in the schema has to reach Glue or Athena cannot select it, and the
        # parquet files will already contain it.
        saver = self._saver()
        saver.glue_client.get_table.return_value = {
            "Table": {"StorageDescriptor": {"Columns": [{"Name": "document_id"}]}}
        }
        assert (
            saver._create_or_update_rule_validation_glue_table("t", self.SCHEMA) is True
        )
        saver.glue_client.update_table.assert_called_once()

    def test_a_missing_table_is_created(self):
        saver = self._saver()
        saver.glue_client.get_table.side_effect = Exception("EntityNotFoundException")
        assert (
            saver._create_or_update_rule_validation_glue_table("t", self.SCHEMA) is True
        )
        saver.glue_client.create_table.assert_called_once()

    def test_the_created_table_uses_partition_projection(self):
        # Without projection, Athena returns nothing until a crawler or MSCK runs, so a
        # freshly written partition would be invisible.
        saver = self._saver()
        saver.glue_client.get_table.side_effect = Exception("EntityNotFoundException")
        saver._create_or_update_rule_validation_glue_table("t", self.SCHEMA)
        table_input = saver.glue_client.create_table.call_args.kwargs["TableInput"]
        assert table_input["Parameters"]["projection.enabled"] == "true"
        assert table_input["Parameters"]["projection.date.type"] == "date"
        assert table_input["PartitionKeys"] == [{"Name": "date", "Type": "string"}]

    def test_the_location_and_template_point_at_the_reporting_bucket(self):
        saver = self._saver()
        saver.glue_client.get_table.side_effect = Exception("EntityNotFoundException")
        saver._create_or_update_rule_validation_glue_table("t", self.SCHEMA)
        table_input = saver.glue_client.create_table.call_args.kwargs["TableInput"]
        assert table_input["StorageDescriptor"]["Location"] == (
            "s3://reporting-bucket/t/"
        )
        assert table_input["Parameters"]["storage.location.template"] == (
            "s3://reporting-bucket/t/date=${date}/"
        )

    def test_a_concurrent_create_is_not_logged_as_an_error_but_still_returns_false(
        self,
    ):
        """Losing a create race is benign, and the return value says otherwise.

        Two documents completing together both find the table missing and both call
        `create_table`; the loser gets `AlreadyExistsException`. The code deliberately
        suppresses the error log for that case -- the table exists, which is the desired
        end state -- but still returns False, the same value it returns for a real
        failure. No caller checks the result, so nothing misbehaves today; pinned so that
        a caller which starts checking does not read a benign race as a failure.
        """
        saver = self._saver()
        saver.glue_client.get_table.side_effect = Exception("EntityNotFoundException")
        saver.glue_client.create_table.side_effect = Exception("AlreadyExistsException")
        assert (
            saver._create_or_update_rule_validation_glue_table("t", self.SCHEMA)
            is False
        )

    def test_a_genuine_create_failure_returns_false(self, caplog):
        saver = self._saver()
        saver.glue_client.get_table.side_effect = Exception("EntityNotFoundException")
        saver.glue_client.create_table.side_effect = Exception("AccessDenied")
        with caplog.at_level("ERROR"):
            assert (
                saver._create_or_update_rule_validation_glue_table("t", self.SCHEMA)
                is False
            )
        assert any("Error creating table" in r.message for r in caplog.records)

    def test_an_unexpected_get_failure_returns_false_without_creating(self):
        # Throttling or a permissions error must not be read as "the table is missing",
        # which would attempt a create that also fails and obscure the real cause.
        saver = self._saver()
        saver.glue_client.get_table.side_effect = Exception("ThrottlingException")
        assert (
            saver._create_or_update_rule_validation_glue_table("t", self.SCHEMA)
            is False
        )
        saver.glue_client.create_table.assert_not_called()


@pytest.mark.unit
class TestGlueTablesAreRegistered:
    """The save path must register both tables, or the data is unqueryable."""

    def test_the_summary_table_is_registered(self, saver):
        with patch(f"{MODULE}.get_json_content", return_value=_payload()):
            saver.save_rule_validation_results(_document())
        names = [
            c.args[0]
            for c in saver._create_or_update_rule_validation_glue_table.call_args_list
        ]
        assert "rule_validation_summary" in names

    def test_the_details_table_is_registered_only_when_there_are_details(self, saver):
        with patch(f"{MODULE}.get_json_content", return_value=_payload()):
            saver.save_rule_validation_results(_document())
        names = [
            c.args[0]
            for c in saver._create_or_update_rule_validation_glue_table.call_args_list
        ]
        assert "rule_validation_details" not in names

        saver._create_or_update_rule_validation_glue_table.reset_mock()
        with patch(
            f"{MODULE}.get_json_content",
            return_value=_payload(rule_details={"P": {"rules": [{"rule": "r"}]}}),
        ):
            saver.save_rule_validation_results(_document())
        names = [
            c.args[0]
            for c in saver._create_or_update_rule_validation_glue_table.call_args_list
        ]
        assert "rule_validation_details" in names
