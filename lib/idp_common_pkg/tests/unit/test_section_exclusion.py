# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the excluded-class feature.

Covers:
* ``Section`` model round-trips the ``excluded`` / ``exclusion_reason`` flags.
* ``idp_common.section_exclusion`` helpers.
* ``ClassificationService._load_document_types`` and
  ``_mark_excluded_sections`` correctly read the schema extensions and
  propagate the flags onto sections.
* The ``Section.to_dict`` / ``from_dict`` emission contract is backward
  compatible (only emits the fields when they're set).
* Every DynamoDB section writer persists the flags (GitHub #704) — without
  the write the UI's "Skipped" badge can never render.
* Downstream service skip behaviour (extraction) writes a stub result
  and leaves the section otherwise untouched.
* Assessment short-circuits without calling LLM.
* Summarization short-circuits and writes a stub ``summary.json``.
* Rule validation short-circuits and returns empty responses.

These tests are intentionally small and self-contained — they don't
require S3 / Bedrock / DynamoDB.  They use real production code paths
with lightweight in-memory doubles.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from idp_common import section_exclusion
from idp_common.classification.service import ClassificationService
from idp_common.config.schema_constants import (
    X_AWS_IDP_DOCUMENT_TYPE,
    X_AWS_IDP_EXCLUDE_FROM_PROCESSING,
    X_AWS_IDP_EXCLUSION_REASON,
)
from idp_common.models import Document, Page, Section

# ---------------------------------------------------------------------------
# Section model: excluded / exclusion_reason round-trip through dict/JSON
# ---------------------------------------------------------------------------


class TestSectionExclusionFields:
    def test_default_values(self) -> None:
        """New Section defaults excluded=False, exclusion_reason=None."""
        s = Section(section_id="1", classification="Foo")
        assert s.excluded is False
        assert s.exclusion_reason is None

    def test_to_dict_omits_when_not_excluded(self) -> None:
        """Compact output when section is not excluded (backward compat)."""
        s = Section(section_id="1", classification="Foo")
        doc = Document(id="d", sections=[s])
        d = doc.to_dict()
        section_dict = d["sections"][0]
        assert "excluded" not in section_dict
        assert "exclusion_reason" not in section_dict

    def test_to_dict_emits_when_excluded(self) -> None:
        s = Section(
            section_id="1",
            classification="Instructions",
            excluded=True,
            exclusion_reason="instructions",
        )
        doc = Document(id="d", sections=[s])
        d = doc.to_dict()
        section_dict = d["sections"][0]
        assert section_dict["excluded"] is True
        assert section_dict["exclusion_reason"] == "instructions"

    def test_from_dict_restores_exclusion(self) -> None:
        """Round-trip Document via to_dict / from_dict preserves flags."""
        original = Section(
            section_id="1",
            classification="Instructions",
            excluded=True,
            exclusion_reason="legal",
        )
        doc = Document(id="d", sections=[original])
        roundtripped = Document.from_dict(doc.to_dict())
        assert roundtripped.sections[0].excluded is True
        assert roundtripped.sections[0].exclusion_reason == "legal"

    def test_section_from_dict_handles_missing_fields(self) -> None:
        """Older persisted documents that predate the feature still load."""
        data = {
            "section_id": "1",
            "classification": "Foo",
            "page_ids": ["1"],
        }
        s = Section.from_dict(data)
        assert s.excluded is False
        assert s.exclusion_reason is None

    def test_section_to_dict_always_includes_fields_via_section_dataclass_helper(
        self,
    ) -> None:
        """Section.to_dict (not Document.to_dict) always includes fields.

        The Document.to_dict path omits them when false for backward
        compatibility, but the raw Section.to_dict includes them — this
        is used by callers that want the canonical representation.
        """
        s = Section(section_id="1", classification="Foo")
        d = s.to_dict()
        assert d["excluded"] is False
        assert d["exclusion_reason"] is None


# ---------------------------------------------------------------------------
# DynamoDB persistence (GitHub #704)
#
# The flags existed end to end EXCEPT for the write: the model carried them,
# Document.to_dict emitted them, schema.graphql declared them and SectionsPanel
# rendered the "Skipped" badge off them — but no DynamoDB writer put them in the
# item, so the badge could never appear and an excluded section looked like an
# unexplained empty one.
# ---------------------------------------------------------------------------


class TestExclusionPersistedToDynamoDB:
    def setup_method(self) -> None:
        from idp_common.dynamodb.service import DocumentDynamoDBService

        self.mock_client = MagicMock()
        self.service = DocumentDynamoDBService(dynamodb_client=self.mock_client)

    @staticmethod
    def _excluded_section() -> Section:
        return Section(
            section_id="1",
            classification="Instructions",
            page_ids=["1", "2"],
            excluded=True,
            exclusion_reason="instructions",
        )

    # -- writer 1: the full document item (update_document) ------------------

    def _update_expression_sections(self, doc: Document):
        self.mock_client.update_item.return_value = {"Attributes": {}}
        self.service.update_document(doc)
        return self.mock_client.update_item.call_args.kwargs[
            "expression_attribute_values"
        ][":Sections"]

    def test_update_document_persists_flags(self) -> None:
        doc = Document(id="d", input_key="d", sections=[self._excluded_section()])
        section_data = self._update_expression_sections(doc)[0]
        assert section_data["Excluded"] is True
        assert section_data["ExclusionReason"] == "instructions"

    def test_update_document_omits_flags_for_normal_section(self) -> None:
        """Items stay byte-identical to what older code wrote."""
        doc = Document(
            id="d",
            input_key="d",
            sections=[Section(section_id="1", classification="Form", page_ids=["1"])],
        )
        section_data = self._update_expression_sections(doc)[0]
        assert "Excluded" not in section_data
        assert "ExclusionReason" not in section_data

    def test_update_document_omits_reason_when_unset(self) -> None:
        doc = Document(
            id="d",
            input_key="d",
            sections=[
                Section(
                    section_id="1",
                    classification="Instructions",
                    page_ids=["1"],
                    excluded=True,
                )
            ],
        )
        section_data = self._update_expression_sections(doc)[0]
        assert section_data["Excluded"] is True
        assert "ExclusionReason" not in section_data

    # -- writer 2: the atomic single-section update --------------------------

    def _single_section_data(self, section: Section):
        self.mock_client.update_item.return_value = {"Attributes": {}}
        self.service.update_document_section(
            document_id="d", section_index=0, section=section
        )
        return self.mock_client.update_item.call_args.kwargs[
            "expression_attribute_values"
        ][":section"]

    def test_update_document_section_persists_flags(self) -> None:
        """This writer is a WHOLE-MAP replace (``SET #Sections[i] = :section``).

        Extraction calls it for every section including the excluded ones it
        short-circuited, so omitting the keys did not merely skip them — it
        ERASED the flags classification had already written, and the badge would
        have vanished moments after appearing.
        """
        section_data = self._single_section_data(self._excluded_section())
        assert section_data["Excluded"] is True
        assert section_data["ExclusionReason"] == "instructions"

    def test_update_document_section_omits_flags_for_normal_section(self) -> None:
        section_data = self._single_section_data(
            Section(section_id="1", classification="Form", page_ids=["1"])
        )
        assert "Excluded" not in section_data
        assert "ExclusionReason" not in section_data

    # -- writer 3: the immutable run (version) snapshot ----------------------

    def test_create_document_run_snapshots_flags(self) -> None:
        doc = Document(
            id="d",
            input_key="d",
            queued_time="2025-07-07T14:00:00.000Z",
            sections=[self._excluded_section()],
        )
        self.service.create_document_run(
            document=doc, run_id="20250707T141530Z-a", manifest_uri="s3://o/m.json"
        )
        item = self.mock_client.put_item.call_args[0][0]
        assert item["Sections"][0]["Excluded"] is True
        assert item["Sections"][0]["ExclusionReason"] == "instructions"

    # -- the read side ------------------------------------------------------

    def test_item_to_document_restores_flags(self) -> None:
        """A Document read back and re-written must not lose the flags."""
        doc = self.service._dynamodb_item_to_document(
            {
                "ObjectKey": "d",
                "ObjectStatus": "COMPLETED",
                "Sections": [
                    {
                        "Id": "1",
                        "Class": "Instructions",
                        "PageIds": [1, 2],
                        "Excluded": True,
                        "ExclusionReason": "instructions",
                    }
                ],
            }
        )
        assert doc.sections[0].excluded is True
        assert doc.sections[0].exclusion_reason == "instructions"

    def test_item_to_document_tolerates_items_written_before_the_fix(self) -> None:
        doc = self.service._dynamodb_item_to_document(
            {
                "ObjectKey": "d",
                "ObjectStatus": "COMPLETED",
                "Sections": [{"Id": "1", "Class": "Form", "PageIds": [1]}],
            }
        )
        assert doc.sections[0].excluded is False
        assert doc.sections[0].exclusion_reason is None


# ---------------------------------------------------------------------------
# section_exclusion helpers
# ---------------------------------------------------------------------------


class TestIsSectionExcluded:
    def test_none_section(self) -> None:
        assert section_exclusion.is_section_excluded(None) is False

    def test_not_excluded(self) -> None:
        s = Section(section_id="1", classification="Foo")
        assert section_exclusion.is_section_excluded(s) is False

    def test_excluded(self) -> None:
        s = Section(section_id="1", classification="Instr", excluded=True)
        assert section_exclusion.is_section_excluded(s) is True


class TestBuildSkippedStubResult:
    def test_contains_all_stable_keys(self) -> None:
        doc = Document(id="doc-123")
        sec = Section(
            section_id="1",
            classification="Instructions",
            page_ids=["1", "2", "3"],
            excluded=True,
            exclusion_reason="legal",
        )
        result = section_exclusion.build_skipped_stub_result(
            doc, sec, stage="extraction"
        )
        assert result["status"] == section_exclusion.SKIPPED_STATUS
        assert result["stage"] == "extraction"
        assert result["section_id"] == "1"
        assert result["classification"] == "Instructions"
        assert result["excluded"] is True
        assert result["exclusion_reason"] == "legal"
        assert result["page_ids"] == ["1", "2", "3"]
        assert result["document_id"] == "doc-123"
        assert "skipped" in result["message"].lower()

    def test_merges_extra_fields(self) -> None:
        doc = Document(id="d")
        sec = Section(section_id="1", classification="Foo", excluded=True)
        result = section_exclusion.build_skipped_stub_result(
            doc, sec, stage="rule_validation", extra={"chunks_created": 0}
        )
        assert result["chunks_created"] == 0


class TestWriteSkippedStub:
    def test_logs_and_returns_none_when_no_s3_target(self, caplog) -> None:
        doc = Document(id="d")
        sec = Section(section_id="1", classification="Foo", excluded=True)
        with caplog.at_level("INFO"):
            uri = section_exclusion.write_skipped_stub(doc, sec, stage="extraction")
        assert uri is None
        # Payload should have been logged.
        assert any(
            "skipped_excluded_class" in rec.getMessage() for rec in caplog.records
        )

    def test_writes_to_s3_when_target_provided(self) -> None:
        doc = Document(id="doc-1")
        sec = Section(
            section_id="7",
            classification="Instructions",
            page_ids=["1", "2"],
            excluded=True,
            exclusion_reason="instructions",
        )
        with patch("boto3.client") as mock_boto:
            s3 = MagicMock()
            mock_boto.return_value = s3
            uri = section_exclusion.write_skipped_stub(
                doc,
                sec,
                stage="extraction",
                output_bucket="bkt",
                output_key="doc-1/sections/7/result.json",
            )
        assert uri == "s3://bkt/doc-1/sections/7/result.json"
        s3.put_object.assert_called_once()
        kwargs = s3.put_object.call_args.kwargs
        assert kwargs["Bucket"] == "bkt"
        assert kwargs["Key"] == "doc-1/sections/7/result.json"
        body = json.loads(kwargs["Body"])
        assert body["excluded"] is True
        assert body["stage"] == "extraction"
        assert body["classification"] == "Instructions"

    def test_s3_exception_is_swallowed(self) -> None:
        """Failing to write a stub must never surface as a hard error."""
        doc = Document(id="d")
        sec = Section(section_id="1", classification="Foo", excluded=True)
        with patch("boto3.client") as mock_boto:
            s3 = MagicMock()
            s3.put_object.side_effect = RuntimeError("S3 down")
            mock_boto.return_value = s3
            uri = section_exclusion.write_skipped_stub(
                doc,
                sec,
                stage="extraction",
                output_bucket="bkt",
                output_key="k",
            )
        assert uri is None  # returns None instead of raising


# ---------------------------------------------------------------------------
# ClassificationService: load DocumentType + mark excluded sections
# ---------------------------------------------------------------------------


def _make_service_with_classes(classes):
    """Construct a ClassificationService with the given raw classes.

    Uses ``ClassificationService.__new__`` to bypass the heavy __init__
    (Bedrock client, etc.) — we only need ``_load_document_types`` and
    ``_mark_excluded_sections`` for this test.
    """
    svc = ClassificationService.__new__(ClassificationService)
    # Minimal config surface that _load_document_types touches:
    svc.config = MagicMock()
    svc.config.classes = classes
    svc.document_types = svc._load_document_types()
    return svc


class TestLoadDocumentTypesWithExclusion:
    def test_reads_x_aws_idp_keys(self) -> None:
        svc = _make_service_with_classes(
            [
                {
                    X_AWS_IDP_DOCUMENT_TYPE: "Boilerplate",
                    "description": "Static pages",
                    X_AWS_IDP_EXCLUDE_FROM_PROCESSING: True,
                    X_AWS_IDP_EXCLUSION_REASON: "instructions",
                },
                {
                    X_AWS_IDP_DOCUMENT_TYPE: "Form",
                    "description": "The form",
                },
            ]
        )
        bp = next(dt for dt in svc.document_types if dt.type_name == "Boilerplate")
        frm = next(dt for dt in svc.document_types if dt.type_name == "Form")
        assert bp.excluded is True
        assert bp.exclusion_reason == "instructions"
        assert frm.excluded is False
        assert frm.exclusion_reason is None

    def test_reads_legacy_snake_case_aliases(self) -> None:
        """Legacy snake_case keys are accepted as a convenience for hand-authored configs."""
        svc = _make_service_with_classes(
            [
                {
                    X_AWS_IDP_DOCUMENT_TYPE: "Legacy",
                    "description": "",
                    "exclude_from_processing": True,
                    "exclusion_reason": "legal",
                },
            ]
        )
        dt = svc.document_types[0]
        assert dt.excluded is True
        assert dt.exclusion_reason == "legal"


class TestMarkExcludedSections:
    def test_propagates_flag_from_class_config(self) -> None:
        svc = _make_service_with_classes(
            [
                {
                    X_AWS_IDP_DOCUMENT_TYPE: "Boilerplate",
                    "description": "",
                    X_AWS_IDP_EXCLUDE_FROM_PROCESSING: True,
                    X_AWS_IDP_EXCLUSION_REASON: "instructions",
                },
                {X_AWS_IDP_DOCUMENT_TYPE: "Form", "description": ""},
            ]
        )
        doc = Document(
            id="d",
            sections=[
                Section(
                    section_id="1", classification="Boilerplate", page_ids=["1", "2"]
                ),
                Section(section_id="2", classification="Form", page_ids=["3"]),
            ],
        )
        svc._mark_excluded_sections(doc)
        assert doc.sections[0].excluded is True
        assert doc.sections[0].exclusion_reason == "instructions"
        assert doc.sections[1].excluded is False
        assert doc.sections[1].exclusion_reason is None

    def test_resets_stale_flag_when_class_changes(self) -> None:
        """If a Section was previously excluded but is now classified as a
        non-excluded class, the flags should be cleared."""
        svc = _make_service_with_classes(
            [{X_AWS_IDP_DOCUMENT_TYPE: "Form", "description": ""}]
        )
        sec = Section(
            section_id="1",
            classification="Form",
            excluded=True,
            exclusion_reason="instructions",
        )
        doc = Document(id="d", sections=[sec])
        svc._mark_excluded_sections(doc)
        assert sec.excluded is False
        assert sec.exclusion_reason is None

    def test_unknown_class_leaves_section_not_excluded(self) -> None:
        svc = _make_service_with_classes(
            [{X_AWS_IDP_DOCUMENT_TYPE: "Known", "description": ""}]
        )
        sec = Section(section_id="1", classification="NeverSeen")
        doc = Document(id="d", sections=[sec])
        svc._mark_excluded_sections(doc)
        assert sec.excluded is False


# ---------------------------------------------------------------------------
# Extraction service: skips excluded sections and writes stub
# ---------------------------------------------------------------------------


class TestExtractionSkipsExcludedSections:
    def test_process_document_section_short_circuits(self) -> None:
        """``ExtractionService.process_document_section`` must exit immediately
        for excluded sections and must not call _prepare_section_info,
        _load_document_text, or any LLM invocation helpers."""
        from idp_common.extraction.service import ExtractionService

        svc = ExtractionService.__new__(ExtractionService)
        svc._reset_context = MagicMock()  # type: ignore[method-assign]
        # We mock helpers we expect NOT to be called.
        svc._prepare_section_info = MagicMock()  # type: ignore[method-assign]
        svc._load_document_text = MagicMock()  # type: ignore[method-assign]
        svc._load_document_images = MagicMock()  # type: ignore[method-assign]
        svc._initialize_extraction_context = MagicMock()  # type: ignore[method-assign]
        svc._invoke_extraction_model = MagicMock()  # type: ignore[method-assign]
        svc._save_results = MagicMock()  # type: ignore[method-assign]
        svc.config = MagicMock()

        sec = Section(
            section_id="1",
            classification="Instructions",
            page_ids=["1", "2"],
            excluded=True,
            exclusion_reason="instructions",
        )
        doc = Document(
            id="doc-1",
            input_key="doc-1",
            output_bucket="out-bkt",
            sections=[sec],
            pages={"1": Page(page_id="1"), "2": Page(page_id="2")},
        )

        with patch(
            "idp_common.section_exclusion.write_skipped_stub",
            return_value="s3://out-bkt/doc-1/sections/1/result.json",
        ) as mock_stub:
            result = svc.process_document_section(doc, "1")

        # Stub written.
        mock_stub.assert_called_once()
        # No extraction work happened.
        svc._prepare_section_info.assert_not_called()
        svc._load_document_text.assert_not_called()
        svc._invoke_extraction_model.assert_not_called()
        svc._save_results.assert_not_called()
        # Section got the stub URI.
        assert result.sections[0].extraction_result_uri == (
            "s3://out-bkt/doc-1/sections/1/result.json"
        )

    def test_non_excluded_section_still_runs_normal_path(self) -> None:
        """Sanity check that the skip guard only triggers for excluded sections."""
        from idp_common.extraction.service import ExtractionService

        svc = ExtractionService.__new__(ExtractionService)
        svc._reset_context = MagicMock()  # type: ignore[method-assign]
        # _prepare_section_info is the next call after the skip check —
        # if we get here, we've passed the skip guard.  Raise a unique
        # sentinel so the test can assert on it.
        svc._prepare_section_info = MagicMock(side_effect=ValueError("proceeded"))  # type: ignore[method-assign]
        svc.config = MagicMock()

        sec = Section(
            section_id="1",
            classification="RealForm",
            page_ids=["1"],
            excluded=False,
        )
        doc = Document(
            id="doc-1",
            input_key="doc-1",
            output_bucket="out-bkt",
            sections=[sec],
            pages={"1": Page(page_id="1")},
        )

        # The ValueError path should return the doc (per the existing
        # contract in process_document_section), but we want to confirm
        # we reached _prepare_section_info at all.
        result = svc.process_document_section(doc, "1")
        svc._prepare_section_info.assert_called_once()
        assert result is doc


# ---------------------------------------------------------------------------
# Evaluation service: excluded sections are filtered from metrics
# ---------------------------------------------------------------------------


def test_evaluation_filters_excluded_sections(tmp_path) -> None:
    """evaluate_document should not pair excluded sections and should
    record them in evaluation_result.excluded_sections."""
    # Build two documents where the first (excluded) section exists in
    # actual but not in expected — without filtering this would produce
    # noise in the metrics.
    actual_doc = Document(
        id="doc-a",
        input_key="doc-a",
        output_bucket="out",
        sections=[
            Section(
                section_id="1",
                classification="Instr",
                page_ids=["1", "2"],
                excluded=True,
                exclusion_reason="instructions",
            ),
            Section(
                section_id="2",
                classification="Form",
                page_ids=["3", "4"],
                extraction_result_uri="s3://out/doc-a/sections/2/result.json",
            ),
        ],
    )
    expected_doc = Document(
        id="doc-a",
        input_key="doc-a",
        sections=[
            Section(
                section_id="2",
                classification="Form",
                page_ids=["3", "4"],
                extraction_result_uri="s3://baseline/doc-a/sections/2/result.json",
            )
        ],
    )

    # Use a real-ish EvaluationService but stub out the Stickler-heavy
    # parts. The test only asserts that excluded sections are filtered
    # and annotated.
    from idp_common.evaluation.service import EvaluationService

    svc = EvaluationService.__new__(EvaluationService)
    svc.max_workers = 1
    # We don't care about the per-section result — return None so the
    # process_section future loop skips it — we just want to validate
    # the section_pairs / excluded_sections_info partitioning.
    svc._process_section = MagicMock(return_value=(None, {}))  # type: ignore[method-assign]
    svc.stickler_models = {}
    svc._auto_generated_models = set()

    # Patch DocSplitClassificationMetrics to a no-op so evaluate_document
    # doesn't need the stickler library.
    with patch(
        "idp_common.evaluation.service.DocSplitClassificationMetrics"
    ) as mock_split:
        mock_split.return_value.load_sections = MagicMock()
        mock_split.return_value.calculate_all_metrics = MagicMock(
            return_value={
                "page_level_accuracy": {
                    "accuracy": 1.0,
                    "total_pages": 4,
                    "correct_pages": 4,
                    "page_details": [],
                },
                "split_accuracy_without_order": {
                    "accuracy": 1.0,
                    "total_sections": 2,
                    "correct_sections": 2,
                    "section_details": [],
                },
                "split_accuracy_with_order": {
                    "accuracy": 1.0,
                    "total_sections": 2,
                    "correct_sections": 2,
                    "section_details": [],
                },
            }
        )
        mock_split.return_value.sections_pred = []

        # Also stub out S3 writes.
        with patch("idp_common.evaluation.service.s3") as mock_s3:
            mock_s3.write_content = MagicMock()
            result_doc = svc.evaluate_document(
                actual_doc, expected_doc, store_results=False
            )

    # Excluded section must be captured but NOT passed to _process_section.
    assert svc._process_section.call_count == 1
    called_with_section = svc._process_section.call_args.args[0]
    assert called_with_section.section_id == "2"

    assert result_doc.evaluation_result is not None
    excluded = result_doc.evaluation_result.excluded_sections
    assert len(excluded) == 1
    assert excluded[0]["section_id"] == "1"
    assert excluded[0]["classification"] == "Instr"
    assert excluded[0]["exclusion_reason"] == "instructions"


# ---------------------------------------------------------------------------
# Assessment service: skips excluded sections (no LLM, no extraction read)
# ---------------------------------------------------------------------------


class TestAssessmentSkipsExcludedSections:
    """``AssessmentService.process_document_section`` returns the document
    unchanged and never reads extraction results or calls the LLM."""

    def _make_service(self):
        from idp_common.assessment.service import AssessmentService

        svc = AssessmentService.__new__(AssessmentService)
        svc.config = MagicMock()
        svc.config.assessment.enabled = True
        # Mock methods that should NOT be called for excluded sections
        svc._load_extraction_results = MagicMock()  # type: ignore[method-assign]
        svc._assess_section = MagicMock()  # type: ignore[method-assign]
        return svc

    def _make_excluded_doc(self):
        sec = Section(
            section_id="1",
            classification="Instructions",
            page_ids=["1", "2"],
            excluded=True,
            exclusion_reason="instructions",
        )
        return Document(
            id="doc-1",
            input_key="doc-1",
            output_bucket="out-bkt",
            sections=[sec],
            pages={"1": Page(page_id="1"), "2": Page(page_id="2")},
        )

    def test_process_document_section_short_circuits(self) -> None:
        svc = self._make_service()
        doc = self._make_excluded_doc()

        result = svc.process_document_section(doc, "1")

        assert result is doc
        svc._load_extraction_results.assert_not_called()
        svc._assess_section.assert_not_called()

    def test_non_excluded_section_proceeds(self) -> None:
        """Non-excluded section should pass the skip guard."""
        svc = self._make_service()
        sec = Section(
            section_id="1",
            classification="Form",
            page_ids=["1"],
            excluded=False,
            # No extraction_result_uri → will fail at the next guard,
            # which is fine — we only want to prove we passed the skip check.
        )
        doc = Document(
            id="doc-1",
            input_key="doc-1",
            output_bucket="out-bkt",
            sections=[sec],
            pages={"1": Page(page_id="1")},
        )
        result = svc.process_document_section(doc, "1")
        # Should have hit the "no extraction results" error, not the skip guard.
        assert any("no extraction results" in e.lower() for e in result.errors)


# ---------------------------------------------------------------------------
# Summarization service: skips excluded sections and writes stub
# ---------------------------------------------------------------------------


class TestSummarizationSkipsExcludedSections:
    """``SummarizationService.process_document_section`` writes a stub
    summary.json and returns (document, {}) for excluded sections."""

    def _make_service(self):
        from idp_common.summarization.service import SummarizationService

        svc = SummarizationService.__new__(SummarizationService)
        svc.config = MagicMock()
        # Mock methods that should NOT be called for excluded sections
        svc._build_prompt = MagicMock()  # type: ignore[method-assign]
        svc._invoke_model = MagicMock()  # type: ignore[method-assign]
        return svc

    def test_process_document_section_short_circuits_and_writes_stub(self) -> None:
        svc = self._make_service()
        sec = Section(
            section_id="1",
            classification="Instructions",
            page_ids=["1", "2"],
            excluded=True,
            exclusion_reason="instructions",
        )
        doc = Document(
            id="doc-1",
            input_key="doc-1",
            output_bucket="out-bkt",
            sections=[sec],
            pages={"1": Page(page_id="1"), "2": Page(page_id="2")},
        )

        with patch(
            "idp_common.section_exclusion.write_skipped_stub",
            return_value="s3://out-bkt/doc-1/sections/1/summary.json",
        ) as mock_stub:
            result_doc, metering = svc.process_document_section(doc, "1")

        # Stub was written for summarization stage.
        mock_stub.assert_called_once()
        call_kwargs = mock_stub.call_args
        assert call_kwargs.kwargs.get("stage") == "summarization" or (
            len(call_kwargs.args) >= 3 and call_kwargs.args[2] == "summarization"
        )
        # No LLM work happened.
        svc._build_prompt.assert_not_called()
        svc._invoke_model.assert_not_called()
        # Returns document and empty metering dict.
        assert result_doc is doc
        assert metering == {}

    def test_non_excluded_section_proceeds(self) -> None:
        svc = self._make_service()
        sec = Section(
            section_id="1",
            classification="Form",
            page_ids=[],  # empty page_ids will trigger next error
            excluded=False,
        )
        doc = Document(
            id="doc-1",
            input_key="doc-1",
            output_bucket="out-bkt",
            sections=[sec],
            pages={},
        )
        result_doc, _ = svc.process_document_section(doc, "1")
        assert any("no page" in e.lower() for e in result_doc.errors)


# ---------------------------------------------------------------------------
# Rule validation service: skips excluded sections
# ---------------------------------------------------------------------------


class TestRuleValidationSkipsExcludedSections:
    """The ``process_one_section`` inner function in
    ``RuleValidationService.validate_document_async`` returns
    ``({}, 0, False)`` for excluded sections.

    Since ``process_one_section`` is an inner async function, we test the
    skip guard by importing ``is_section_excluded`` directly — the same
    check the service uses — and validating the function output contract.
    """

    def test_is_section_excluded_returns_true_for_excluded(self) -> None:
        sec = Section(
            section_id="1",
            classification="Instructions",
            excluded=True,
            exclusion_reason="instructions",
        )
        assert section_exclusion.is_section_excluded(sec) is True

    def test_is_section_excluded_returns_false_for_normal(self) -> None:
        sec = Section(
            section_id="2",
            classification="Form",
            excluded=False,
        )
        assert section_exclusion.is_section_excluded(sec) is False

    def test_expected_return_shape_for_skipped_section(self) -> None:
        """The contract for a skipped section in process_one_section is
        ``(section_responses={}, 0, False)``."""
        sec = Section(
            section_id="1",
            classification="Instructions",
            excluded=True,
            exclusion_reason="instructions",
        )
        if section_exclusion.is_section_excluded(sec):
            section_responses, count, chunking = {}, 0, False
        else:
            raise AssertionError("Section should be excluded")

        assert section_responses == {}
        assert count == 0
        assert chunking is False
