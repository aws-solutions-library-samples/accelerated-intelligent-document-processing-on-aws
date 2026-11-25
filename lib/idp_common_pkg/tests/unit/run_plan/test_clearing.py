# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What is cleared before a run starts at each step, on the document and in S3."""

import boto3
import pytest
from moto import mock_aws

from idp_common.models import Document, Page, RuleValidationResult, Section
from idp_common.run_plan import clearing
from idp_common.run_plan.clearing import (
    ROW_EVALUATION,
    ROW_POLICY_RULE_VALIDATION,
    ROW_SECTIONS,
    ROW_SUMMARY,
    clear_after_review,
    clear_for_start,
    clear_policy_rule_validation,
    recount,
)
from idp_common.run_plan.steps import UnknownStep, step_ids

pytestmark = pytest.mark.unit

BUCKET = "clearing-output"
KEY = "packet.pdf"


def _document() -> Document:
    """A fully processed two-section document."""
    document = Document(id=KEY, input_key=KEY, output_bucket=BUCKET, num_pages=2)
    for page_id in ("1", "2"):
        document.pages[page_id] = Page(
            page_id=page_id,
            image_uri=f"s3://{BUCKET}/{KEY}/pages/{page_id}/image.jpg",
            raw_text_uri=f"s3://{BUCKET}/{KEY}/pages/{page_id}/rawText.json",
            parsed_text_uri=f"s3://{BUCKET}/{KEY}/pages/{page_id}/result.json",
            classification="Invoice",
            confidence=0.9,
            classification_reason="looks like one",
            classification_candidates=[{"class": "Invoice", "probability": 0.9}],
            document_boundary="start",
        )
    for section_id in ("1", "2"):
        document.sections.append(
            Section(
                section_id=section_id,
                classification="Invoice",
                page_ids=[section_id],
                extraction_result_uri=(
                    f"s3://{BUCKET}/{KEY}/sections/{section_id}/result.json"
                ),
                attributes={"Total": 1},
                confidence_threshold_alerts=[
                    {"attribute_name": "Total", "confidence": 0.2}
                ],
                confidence_alert_total=40,
                missing_required_fields=[{"attribute_name": "Vendor"}],
            )
        )
    document.confidence_alert_count = 80
    document.missing_required_field_count = 2
    document.rule_validation_result = RuleValidationResult(
        request_id="r",
        output_uri=f"s3://{BUCKET}/{KEY}/rule_validation/consolidated/"
        "consolidated_summary.md",
    )
    document.summary_report_uri = f"s3://{BUCKET}/{KEY}/summary/summary.md"
    document.summarization_result = object()
    document.evaluation_report_uri = f"s3://{BUCKET}/{KEY}/evaluation/report.md"
    document.evaluation_results_uri = f"s3://{BUCKET}/{KEY}/evaluation/results.json"
    document.evaluation_status = "COMPLETED"
    document.evaluation_result = object()
    return document


def _state(document: Document) -> dict:
    """What a run could find already done, stage by stage."""
    return {
        "pages": bool(document.pages),
        "classes": bool(document.pages)
        and all(p.classification for p in document.pages.values()),
        "sections": [s.section_id for s in document.sections],
        "results": [s.section_id for s in document.sections if s.extraction_result_uri],
        "alerts": [
            s.section_id
            for s in document.sections
            if s.confidence_threshold_alerts
            or s.confidence_alert_total
            or s.missing_required_fields
        ],
        "policy": document.rule_validation_result is not None,
        "summary": document.summary_report_uri is not None,
        "evaluation": document.evaluation_report_uri is not None
        or document.evaluation_results_uri is not None
        or document.evaluation_status is not None,
    }


_FULL = {
    "pages": True,
    "classes": True,
    "sections": ["1", "2"],
    "results": ["1", "2"],
    "alerts": ["1", "2"],
    "policy": True,
    "summary": True,
    "evaluation": True,
}

#: Start -> what is left afterwards (everything else is cleared).
_AFTER = {
    "beginning": {
        **_FULL,
        "pages": False,
        "classes": False,
        "sections": [],
        "results": [],
        "alerts": [],
        "policy": False,
        "summary": False,
        "evaluation": False,
    },
    "classification": {
        **_FULL,
        "classes": False,
        "sections": [],
        "results": [],
        "alerts": [],
        "policy": False,
        "summary": False,
        "evaluation": False,
    },
    "extraction": {
        **_FULL,
        "results": [],
        "alerts": [],
        "policy": False,
        "summary": False,
        "evaluation": False,
    },
    "assessment": {
        **_FULL,
        "alerts": [],
        "policy": False,
        "summary": False,
        "evaluation": False,
    },
    "policy_rule_validation": {
        **_FULL,
        "policy": False,
        "summary": False,
        "evaluation": False,
    },
    "summarization": {**_FULL, "summary": False, "evaluation": False},
    "evaluation": {**_FULL, "evaluation": False},
}


@pytest.fixture
def bucket(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        for key in (
            f"{KEY}/pages/1/result.json",
            f"{KEY}/sections/1/result.json",
            f"{KEY}/sections/2/result.json",
            f"{KEY}/rule_validation/sections/section_1_responses.json",
            f"{KEY}/rule_validation/consolidated/consolidated_summary.json",
            f"{KEY}/rule_validation/consolidated/consolidated_summary.md",
            f"{KEY}/summary/summary.md",
            f"{KEY}/runs/2026/manifest.json",
            # Another document whose key starts with this one's.
            f"{KEY}.copy/rule_validation/consolidated/consolidated_summary.json",
        ):
            client.put_object(Bucket=BUCKET, Key=key, Body=b"{}")
        yield client


def _keys(client) -> set[str]:
    listed = client.list_objects_v2(Bucket=BUCKET).get("Contents", [])
    return {item["Key"] for item in listed}


class TestTheDocument:
    @pytest.mark.parametrize("start", step_ids())
    def test_each_start_clears_itself_and_everything_below(self, bucket, start):
        document = _document()
        assert _state(document) == _FULL

        clear_for_start(document, start, s3=bucket)

        assert _state(document) == _AFTER[start]

    def test_the_table_covers_every_step(self):
        assert set(_AFTER) == set(step_ids())

    def test_ocr_is_the_beginning(self, bucket):
        document = _document()

        clear_for_start(document, "ocr", s3=bucket)

        assert _state(document) == _AFTER["beginning"]

    def test_an_unknown_start_is_refused(self):
        with pytest.raises(UnknownStep):
            clear_for_start(_document(), "nope")

    def test_classification_clears_every_class_signal_on_a_page(self, bucket):
        document = _document()

        clear_for_start(document, "classification", s3=bucket)

        page = document.pages["1"]
        assert (
            page.classification,
            page.confidence,
            page.classification_reason,
            page.classification_candidates,
            page.document_boundary,
        ) == (None, None, None, None, None)
        # OCR's output is kept.
        assert page.image_uri and page.parsed_text_uri and page.raw_text_uri

    def test_extraction_clears_the_section_fields(self, bucket):
        document = _document()

        clear_for_start(document, "extraction", s3=bucket)

        section = document.sections[0]
        assert section.extraction_result_uri is None
        assert section.attributes is None
        assert section.confidence_threshold_alerts == []
        assert section.confidence_alert_total is None
        assert section.missing_required_fields == []
        # The class and its pages are kept.
        assert (section.classification, section.page_ids) == ("Invoice", ["1"])

    def test_assessment_keeps_the_extraction_result(self, bucket):
        document = _document()

        clear_for_start(document, "assessment", s3=bucket)

        section = document.sections[0]
        assert section.extraction_result_uri.endswith("sections/1/result.json")
        assert section.attributes == {"Total": 1}
        assert section.confidence_alert_total is None

    def test_the_document_counts_follow_the_sections(self, bucket):
        document = _document()

        clear_for_start(document, "assessment", sections=["2"], s3=bucket)

        # Section 1 keeps its total of 40; section 2 was cleared.
        assert document.confidence_alert_count == 40
        assert document.missing_required_field_count == 1

    def test_a_later_start_leaves_the_counts_alone(self, bucket):
        document = _document()

        clear_for_start(document, "policy_rule_validation", s3=bucket)

        assert document.confidence_alert_count == 80
        assert document.missing_required_field_count == 2


class TestSections:
    def test_extraction_with_sections_clears_only_those(self, bucket):
        document = _document()

        clear_for_start(document, "extraction", sections=["2"], s3=bucket)

        state = _state(document)
        assert state["results"] == ["1"]
        assert state["alerts"] == ["1"]
        assert state["policy"] is False
        assert f"{KEY}/sections/1/result.json" in _keys(bucket)
        assert f"{KEY}/sections/2/result.json" not in _keys(bucket)

    def test_a_section_id_the_document_does_not_have_is_ignored(self, bucket):
        document = _document()

        clear_for_start(document, "extraction", sections=["9"], s3=bucket)

        assert _state(document)["results"] == ["1", "2"]


class TestS3:
    def test_extraction_deletes_the_section_results(self, bucket):
        clear_for_start(_document(), "extraction", s3=bucket)

        keys = _keys(bucket)
        assert f"{KEY}/sections/1/result.json" not in keys
        assert f"{KEY}/sections/2/result.json" not in keys
        assert f"{KEY}/pages/1/result.json" in keys

    def test_classification_deletes_the_section_results(self, bucket):
        clear_for_start(_document(), "classification", s3=bucket)

        assert not [k for k in _keys(bucket) if "/sections/" in k and "rule_" not in k]
        assert f"{KEY}/pages/1/result.json" in _keys(bucket)

    @pytest.mark.parametrize(
        "start",
        ["classification", "extraction", "assessment", "policy_rule_validation"],
    )
    def test_the_policy_rule_validation_prefix_is_deleted(self, bucket, start):
        clear_for_start(_document(), start, s3=bucket)

        keys = _keys(bucket)
        assert not [k for k in keys if k.startswith(f"{KEY}/rule_validation/")]
        # The run manifests and another document's files are untouched.
        assert f"{KEY}/runs/2026/manifest.json" in keys
        assert (
            f"{KEY}.copy/rule_validation/consolidated/consolidated_summary.json" in keys
        )

    @pytest.mark.parametrize("start", ["summarization", "evaluation"])
    def test_a_start_after_policy_rule_validation_keeps_its_files(self, bucket, start):
        before = _keys(bucket)

        clear_for_start(_document(), start, s3=bucket)

        assert _keys(bucket) == before

    def test_the_beginning_deletes_nothing_itself(self, bucket):
        """The caller purges the whole output prefix for a start at the beginning."""
        before = _keys(bucket)

        clear_for_start(_document(), "beginning", s3=bucket)

        assert _keys(bucket) == before

    def test_a_missing_object_is_not_an_error(self, bucket):
        document = _document()
        document.sections[
            0
        ].extraction_result_uri = (
            f"s3://{BUCKET}/{KEY}/sections/never-written/result.json"
        )

        clear_for_start(document, "extraction", s3=bucket)

        assert _state(document)["results"] == []

    def test_a_failed_delete_does_not_stop_the_clearing(self):
        class _Denied:
            def delete_object(self, **kwargs):
                raise RuntimeError("AccessDenied")

            def get_paginator(self, name):
                raise RuntimeError("AccessDenied")

        document = _document()

        clear_for_start(document, "extraction", s3=_Denied())

        assert _state(document) == _AFTER["extraction"]


class _MustNotBeUsed:
    """An S3 client that fails the test when anything is asked of it.

    ``pytest.fail`` raises outside ``Exception``, so the clearing code, which logs
    a failed delete and carries on, cannot swallow it.
    """

    def __init__(self):
        self.used = []

    def __getattr__(self, name):
        self.used.append(name)
        pytest.fail(f"keep_objects must not use S3, and {name} was asked for")


def _fields(document: Document) -> dict:
    """Every field a clearing can change."""
    return {
        "state": _state(document),
        "num_pages": document.num_pages,
        "counts": (
            document.confidence_alert_count,
            document.missing_required_field_count,
        ),
        "pages": [
            (
                page.page_id,
                page.classification,
                page.confidence,
                page.classification_reason,
                page.classification_candidates,
                page.document_boundary,
            )
            for page in document.pages.values()
        ],
        "sections": [
            (
                section.section_id,
                section.extraction_result_uri,
                section.attributes,
                section.confidence_threshold_alerts,
                section.confidence_alert_total,
                section.missing_required_fields,
            )
            for section in document.sections
        ],
        "summary": (
            document.summary_report_uri,
            document.summarization_result is None,
        ),
        "evaluation": (
            document.evaluation_report_uri,
            document.evaluation_results_uri,
            document.evaluation_status,
            document.evaluation_result is None,
        ),
    }


class TestKeepObjects:
    @pytest.mark.parametrize("start", step_ids())
    def test_keep_objects_deletes_nothing_and_clears_the_same_fields(
        self, bucket, start
    ):
        s3 = _MustNotBeUsed()
        kept, cleared = _document(), _document()

        kept_rows = clear_for_start(kept, start, s3=s3, keep_objects=True)

        assert s3.used == []
        # The same fields, and the same row attributes, as a clearing that deletes.
        cleared_rows = clear_for_start(cleared, start, s3=bucket)
        assert _fields(kept) == _fields(cleared)
        assert _state(kept) == _AFTER[start]
        assert kept_rows == cleared_rows

    @pytest.mark.parametrize("start", step_ids())
    def test_keep_objects_leaves_every_object_in_the_bucket(self, bucket, start):
        before = _keys(bucket)

        clear_for_start(_document(), start, s3=bucket, keep_objects=True)

        assert _keys(bucket) == before

    def test_keep_objects_with_sections_clears_only_those_fields(self):
        s3 = _MustNotBeUsed()
        document = _document()

        clear_for_start(
            document, "extraction", sections=["2"], s3=s3, keep_objects=True
        )

        assert s3.used == []
        state = _state(document)
        assert state["results"] == ["1"]
        assert state["alerts"] == ["1"]
        assert state["policy"] is False
        assert document.confidence_alert_count == 40

    def test_without_keep_objects_the_same_client_is_used(self):
        # Non-vacuous: the stub does stop a clearing that deletes.
        with pytest.raises(pytest.fail.Exception, match="delete_object"):
            clear_for_start(_document(), "extraction", s3=_MustNotBeUsed())


class TestRecount:
    def test_the_counts_are_read_from_the_sections(self):
        document = _document()
        section = document.sections[1]
        section.confidence_threshold_alerts = []
        section.confidence_alert_total = None
        section.missing_required_fields = []

        recount(document)

        # Section 1 keeps its total of 40 and its one missing field.
        assert document.confidence_alert_count == 40
        assert document.missing_required_field_count == 1

    def test_a_document_without_sections_counts_nothing(self):
        document = _document()
        document.sections = []

        recount(document)

        assert document.confidence_alert_count == 0
        assert document.missing_required_field_count == 0

    def test_the_private_name_is_gone(self):
        assert not hasattr(clearing, "_recount")


class TestRowAttributes:
    @pytest.mark.parametrize(
        ("start", "expected"),
        [
            ("beginning", ROW_POLICY_RULE_VALIDATION + ROW_SUMMARY + ROW_EVALUATION),
            (
                "classification",
                ROW_SECTIONS
                + ROW_POLICY_RULE_VALIDATION
                + ROW_SUMMARY
                + ROW_EVALUATION,
            ),
            ("extraction", ROW_POLICY_RULE_VALIDATION + ROW_SUMMARY + ROW_EVALUATION),
            ("assessment", ROW_POLICY_RULE_VALIDATION + ROW_SUMMARY + ROW_EVALUATION),
            (
                "policy_rule_validation",
                ROW_POLICY_RULE_VALIDATION + ROW_SUMMARY + ROW_EVALUATION,
            ),
            ("summarization", ROW_SUMMARY + ROW_EVALUATION),
            ("evaluation", ROW_EVALUATION),
        ],
    )
    def test_the_row_attributes_of_what_was_cleared_are_returned(
        self, bucket, start, expected
    ):
        assert clear_for_start(_document(), start, s3=bucket) == expected


class TestTheOtherTwo:
    def test_clear_policy_rule_validation(self, bucket):
        document = _document()

        clear_policy_rule_validation(document, s3=bucket)

        assert document.rule_validation_result is None
        assert not [k for k in _keys(bucket) if k.startswith(f"{KEY}/rule_validation/")]
        assert _state(document) == {**_FULL, "policy": False}

    def test_clear_policy_rule_validation_without_a_bucket_clears_the_field(self):
        document = _document()
        document.output_bucket = None

        clear_policy_rule_validation(document)

        assert document.rule_validation_result is None

    def test_clear_after_review_clears_fields_only(self, bucket):
        document = _document()
        before = _keys(bucket)

        clear_after_review(document)

        assert _state(document) == _AFTER["policy_rule_validation"]
        assert _keys(bucket) == before
