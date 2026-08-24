# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""How the markdown report describes a section that failed to evaluate.

The failure block used to assert, for *every* failure, that no configuration
was found for the document class — and printed the matching "how to fix" list.
So an extraction-parsing failure or a baseline type mismatch was reported as a
missing configuration, pointing users at something that was not wrong.
"""

import pytest

from idp_common.evaluation.models import (
    AttributeEvaluationResult,
    DocumentEvaluationResult,
    SectionEvaluationResult,
)

FAILED_METRICS = {
    "precision": 0.0,
    "recall": 0.0,
    "f1_score": 0.0,
    "accuracy": 0.0,
    "false_alarm_rate": 0.0,
    "false_discovery_rate": 0.0,
    "weighted_overall_score": 0.0,
    "evaluation_failed": True,
}


def _report(reason: str, failure_type: str | None) -> str:
    metrics = dict(FAILED_METRICS)
    if failure_type is not None:
        metrics["failure_type"] = failure_type
    return DocumentEvaluationResult(
        document_id="doc-1",
        section_results=[
            SectionEvaluationResult(
                section_id="1",
                document_class="CoverSheet",
                attributes=[
                    AttributeEvaluationResult(
                        name="__EVALUATION_FAILURE__",
                        expected=None,
                        actual=None,
                        matched=False,
                        score=0.0,
                        reason=reason,
                        evaluation_method="N/A",
                    )
                ],
                metrics=metrics,
            )
        ],
        overall_metrics={},
    ).to_markdown()


@pytest.mark.unit
def test_missing_configuration_keeps_its_config_remediation():
    md = _report(
        "No schema configuration found for document class: CoverSheet. "
        "Cannot evaluate without configuration or baseline data.",
        "missing_schema_configuration",
    )

    assert "EVALUATION FAILED" in md
    assert "No schema configuration found for document class: CoverSheet" in md
    assert "**How to fix:**" in md
    assert "in your `evaluation` config YAML" in md


@pytest.mark.unit
def test_parsing_failure_is_not_described_as_a_missing_configuration():
    md = _report(
        "Extraction output for this section could not be parsed as JSON.",
        "extraction_parsing_failed",
    )

    assert "could not be parsed as JSON" in md
    # The wrong diagnosis and its remediation must both be absent.
    assert "no configuration was found" not in md
    assert "No schema configuration exists for this document class" not in md
    assert "in your `evaluation` config YAML" not in md
    # ...replaced by advice that fits the actual cause.
    assert "was truncated" in md


@pytest.mark.unit
def test_baseline_validation_failure_points_at_the_baseline():
    md = _report(
        "Data validation error: The baseline data format doesn't match the schema.",
        "baseline_data_validation_error",
    )

    assert "baseline (expected) values against the class schema" in md
    assert "in your `evaluation` config YAML" not in md


@pytest.mark.unit
def test_untyped_failure_states_the_reason_and_offers_no_guessed_remediation():
    """Results written before ``failure_type`` existed must still read
    correctly — reason shown, no invented advice."""
    md = _report("Unexpected error during evaluation: boom", None)

    assert "Unexpected error during evaluation: boom" in md
    assert "**How to fix:**" not in md
    assert "no configuration was found" not in md


@pytest.mark.unit
def test_failure_type_is_not_rendered_as_a_metric_row():
    md = _report("...", "missing_schema_configuration")

    assert "| failure_type |" not in md
    # The real metrics still appear.
    assert "| precision |" in md


@pytest.mark.unit
def test_zero_metrics_are_labelled_not_scored():
    """The zeros read as a scored-zero extraction next to a healthy
    document-level score; say what they actually mean."""
    md = _report("...", "missing_schema_configuration")

    assert "Metrics (Failure State)" in md
    assert "mean *not scored*, not *scored zero*" in md


# ---------------------------------------------------------------------------
# The report's accuracy depends on the service labelling the cause, so pin the
# producer side too — the two halves are useless apart.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "raised,expected_type",
    [
        (
            ValueError("No schema configuration found for class 'X'"),
            "missing_schema_configuration",
        ),
        (
            ValueError(
                "Error in field 'vendor': field_definitions must contain at least one field"
            ),
            "empty_nested_object",
        ),
        (ValueError("something else about the schema"), "schema_configuration_error"),
        (RuntimeError("boom"), "unexpected_error"),
    ],
)
def test_service_tags_each_failure_with_its_cause(raised, expected_type):
    """Every failure path must name its cause; the report's remediation is
    keyed on this and silently falls back to none if it is missing."""
    from unittest.mock import patch

    from idp_common.evaluation.service import EvaluationService
    from idp_common.models import Section

    config = {
        "classes": [
            {
                "$id": "invoice",
                "x-aws-idp-document-type": "Invoice",
                "type": "object",
                "properties": {"invoice_number": {"type": "string"}},
            }
        ]
    }
    svc = EvaluationService(region="us-east-1", config=config, max_workers=1)

    with patch.object(svc, "_get_stickler_model", side_effect=raised):
        result = svc.evaluate_section(
            section=Section(section_id="1", classification="Invoice", page_ids=["1"]),
            expected_results={"invoice_number": "A-1"},
            actual_results={"invoice_number": "A-1"},
        )

    assert result.metrics.get("evaluation_failed") is True
    assert result.metrics.get("failure_type") == expected_type
