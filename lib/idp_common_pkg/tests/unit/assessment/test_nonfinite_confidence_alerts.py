# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A NaN or infinite confidence alerts, in every alert builder.

``json.loads`` accepts ``NaN``, ``Infinity`` and ``-Infinity``, and
``nan < threshold`` is False, so a NaN score compared directly raised no alert
and never routed to review. Every fixture here is produced by the reader the
production path uses on that input — ``json.loads`` of the model's response in
Pipeline mode, ``download_json`` of the BDA result in BDA mode — rather than by
writing ``float("nan")`` into a dict, so the tests also pin that the value
actually survives the reader.
"""

from __future__ import annotations

import copy
import io
import json
import math
from typing import Any
from unittest.mock import patch

import pytest

from idp_common.assessment.batching import enrich_assessment_with_thresholds
from idp_common.assessment.service import AssessmentService, _safe_float_conversion
from idp_common.review_queues import routing
from idp_common.utils import extract_json_from_text, finite_float
from tests.unit.assessment.test_bda_lambda_functions import bda_index

pytestmark = pytest.mark.unit

NONFINITE_TOKENS = ["NaN", "Infinity", "-Infinity"]


def _model_response(token: str) -> dict:
    """What the separate-mode parser makes of a response carrying ``token``."""
    text = (
        "```json\n"
        f'{{"InvoiceTotal": {{"confidence": {token}, "confidence_reason": "r"}},'
        ' "InvoiceDate": {"confidence": 0.95, "confidence_reason": "r"}}\n'
        "```"
    )
    return json.loads(extract_json_from_text(text))


class TestFiniteFloat:
    @pytest.mark.parametrize("token", NONFINITE_TOKENS)
    def test_the_reader_really_yields_a_non_finite_float(self, token):
        value = _model_response(token)["InvoiceTotal"]["confidence"]
        assert isinstance(value, float) and not math.isfinite(value)
        assert finite_float(value) is None

    @pytest.mark.parametrize("value", [None, "", "  ", "n/a", [], {}, "nan", "inf"])
    def test_unusable_values_read_as_none(self, value):
        assert finite_float(value) is None

    def test_an_integer_too_large_for_a_float_reads_as_none(self):
        """``json.loads`` keeps a long integer literal as an ``int``, and
        ``float()`` of it raises OverflowError rather than returning inf."""
        value = json.loads('{"confidence": 1' + "0" * 400 + "}")["confidence"]
        assert isinstance(value, int)
        assert finite_float(value) is None
        assert routing.bda_segment_triggers([], value, 0.8, hitl_enabled=True)
        assert _safe_float_conversion(value, 0.0) == 0.0

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0.5, 0.5), ("0.5", 0.5), (1, 1.0), (0, 0.0), (1.7, 1.7), (-0.2, -0.2)],
    )
    def test_finite_values_are_read_unclamped(self, value, expected):
        assert finite_float(value) == expected


_INVOICE_CONFIG = {
    "classes": [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "invoice",
            "x-aws-idp-document-type": "invoice",
            "type": "object",
            "properties": {
                "InvoiceTotal": {
                    "type": "string",
                    "x-aws-idp-confidence-threshold": 0.8,
                },
                "InvoiceDate": {
                    "type": "string",
                    "x-aws-idp-confidence-threshold": 0.8,
                },
            },
        }
    ],
    "assessment": {
        "model": "anthropic.claude-3-sonnet-20240229-v1:0",
        "system_prompt": "You are a document assessment assistant.",
        "task_prompt": (
            "Assess {DOCUMENT_CLASS}: {ATTRIBUTE_NAMES_AND_DESCRIPTIONS} "
            "{EXTRACTION_RESULTS} {DOCUMENT_TEXT}"
        ),
    },
}


def _separate_step(token: str, threshold: Any = 0.8) -> tuple[dict, list]:
    """Run the separate confidence step the way ``process_document_section``
    does — ``assess_results_batched`` with the class schema and HITL default —
    with only the Bedrock call stubbed. Returns ``(stored alerts, alerts built
    by assess_results itself)``; production stores the first and discards the
    second."""
    from idp_common.assessment.batching import assess_results_batched

    text = (
        f'{{"InvoiceTotal": {{"confidence": {token}, "confidence_reason": "r"}},'
        ' "InvoiceDate": {"confidence": 0.95, "confidence_reason": "r"}}'
    )
    response = {
        "response": {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"text": text}]}},
        },
        "metering": {},
    }
    config = copy.deepcopy(_INVOICE_CONFIG)
    props = config["classes"][0]["properties"]
    props["InvoiceTotal"]["x-aws-idp-confidence-threshold"] = threshold
    svc = AssessmentService(region="us-west-2", config=config)
    results = {"InvoiceTotal": "12.00", "InvoiceDate": "2024-01-01"}
    with (
        patch(
            "idp_common.assessment.service.bedrock.invoke_model",
            return_value=response,
        ),
        patch(
            "idp_common.assessment.service.bedrock.extract_text_from_response",
            return_value=text,
        ),
    ):
        core = svc.assess_results(
            class_label="invoice",
            extraction_results=results,
            document_text="t",
            page_images=[],
        )
        stored = assess_results_batched(
            svc,
            class_label="invoice",
            extraction_results=results,
            document_text="t",
            page_images=[],
            batch_size=50,
            geometry_mode="off",
            class_schema=svc._get_class_schema("invoice"),
            default_confidence_threshold=svc.config.hitl.confidence_threshold,
        )
    return stored, core.confidence_threshold_alerts


def _separate_step_alerts(token: str) -> tuple[list, list]:
    stored, core = _separate_step(token)
    return stored["alerts"], core


_ZERO_ALERT = [
    {"attribute_name": "InvoiceTotal", "confidence": 0.0, "confidence_threshold": 0.8}
]


class TestSeparateModeAlerts:
    """The separate confidence step stores the alerts that
    ``enrich_assessment_with_thresholds`` regenerates, not the ones
    ``assess_results`` builds through ``_safe_float_conversion``."""

    @pytest.mark.parametrize("token", NONFINITE_TOKENS)
    def test_the_stored_alert_reads_a_non_finite_score_as_zero(self, token):
        stored, _ = _separate_step_alerts(token)
        assert stored == _ZERO_ALERT

    @pytest.mark.parametrize("token", ["null", '"n/a"'])
    def test_a_null_or_non_numeric_score_stores_no_alert(self, token):
        """Unchanged: the stored alerts skip an unscored leaf."""
        stored, _ = _separate_step_alerts(token)
        assert stored == []

    def test_an_integer_too_large_for_a_float_does_not_fail_the_step(self):
        """It read as unusable, like a non-numeric score, rather than raising
        OverflowError out of ``enrich_assessment_with_thresholds``."""
        stored, _ = _separate_step_alerts("1" + "0" * 400)
        assert stored == []

    @pytest.mark.parametrize("threshold", [math.nan, math.inf, -math.inf])
    def test_a_non_finite_schema_threshold_falls_back_in_leaf_and_alert(
        self, threshold
    ):
        """The stored leaf's threshold and the stored alert's agree: both fall
        back to the HITL default rather than one using the default and the
        other the malformed value."""
        stored, _ = _separate_step("0.1", threshold=threshold)
        leaf = stored["assessment"]["InvoiceTotal"]["confidence_threshold"]
        assert math.isfinite(leaf)
        assert [
            (a["attribute_name"], a["confidence"], a["confidence_threshold"])
            for a in stored["alerts"]
        ] == [("InvoiceTotal", 0.1, leaf)]

    def test_a_low_numeric_score_still_alerts_at_its_value(self):
        stored, _ = _separate_step_alerts("0.1")
        assert [(a["attribute_name"], a["confidence"]) for a in stored] == [
            ("InvoiceTotal", 0.1)
        ]

    @pytest.mark.parametrize("token", NONFINITE_TOKENS)
    def test_conversion_reads_non_finite_as_the_default(self, token):
        """``_safe_float_conversion`` also reads the thresholds the separate
        step stores on each leaf, so it applies the same rule."""
        value = _model_response(token)["InvoiceTotal"]["confidence"]
        assert _safe_float_conversion(value, 0.0) == 0.0
        assert _safe_float_conversion(value, 0.9) == 0.9
        _, core = _separate_step_alerts(token)
        assert core == _ZERO_ALERT


class TestIntegratedModeAlerts:
    """``enrich_assessment_with_thresholds`` builds the alerts for integrated
    confidence and for the batched list flow. The fixture comes from the
    separate step's parser; the integrated path receives the same JSON values
    from its own ``json.loads`` of the response or tool input."""

    @pytest.mark.parametrize("token", NONFINITE_TOKENS)
    def test_a_non_finite_field_alerts_at_zero(self, token):
        enriched, alerts = enrich_assessment_with_thresholds(
            _model_response(token), {"properties": {}}, 0.8
        )
        assert alerts == [
            {
                "attribute_name": "InvoiceTotal",
                "confidence": 0.0,
                "confidence_threshold": 0.8,
            }
        ]
        # The stored leaf is left as the model wrote it; only the alert reads 0.0.
        assert not math.isfinite(enriched["InvoiceTotal"]["confidence"])

    def test_null_and_non_numeric_still_raise_no_alert_here(self):
        """Unchanged on purpose: in this builder a null leaf is an unscored
        placeholder, reported by the coverage audit rather than as an alert."""
        _, alerts = enrich_assessment_with_thresholds(
            {"A": {"confidence": None}, "B": {"confidence": "n/a"}},
            {"properties": {}},
            0.8,
        )
        assert alerts == []


class TestBDAAlerts:
    @pytest.mark.parametrize("token", NONFINITE_TOKENS)
    def test_a_non_finite_blueprint_confidence_triggers_review(self, token):
        value = json.loads(f'{{"confidence": {token}}}')["confidence"]
        assert routing.bda_segment_triggers([], value, 0.8, hitl_enabled=True)
        assert routing.bda_segment_triggers([], value, 0.8, hitl_enabled=False) is None

    @pytest.mark.parametrize("where", ["field", "blueprint"])
    @pytest.mark.parametrize("token", NONFINITE_TOKENS)
    def test_a_non_finite_score_reaches_review_through_the_real_reader(
        self, token, where
    ):
        """Three segments read by the Lambda's own ``download_json``; the middle
        one carries a non-finite field confidence or a non-finite blueprint
        confidence, and either one alone marks it for review."""
        from idp_common.models import Document, Section

        def body(page_index, field_conf, bp_conf):
            return (
                "{"
                f'"matched_blueprint": {{"name": "invoice-bp", "confidence": {bp_conf}}},'
                f'"split_document": {{"page_indices": [{page_index}]}},'
                '"document_class": {"type": "invoice"},'
                '"explainability_info": [{"InvoiceTotal": {"value": "12.00",'
                f' "confidence": {field_conf}, "geometry": [{{"page": {page_index + 1}}}]}}}}]'
                "}"
            ).encode()

        bodies = {
            "out/1.json": body(0, "0.95", "0.99"),
            "out/2.json": body(
                1,
                token if where == "field" else "0.95",
                token if where == "blueprint" else "0.99",
            ),
            "out/3.json": body(2, "0.95", "0.99"),
        }

        def get_object(Bucket, Key):  # noqa: N803 - boto3 keyword names
            return {"Body": io.BytesIO(bodies[Key])}

        doc = Document(id="pkg.pdf", input_key="pkg.pdf")
        doc.sections = [
            Section(section_id=str(n), classification="invoice", page_ids=[str(n)])
            for n in (1, 2, 3)
        ]
        segments = [
            {"custom_output_status": "MATCH", "custom_output_path": f"s3://bda/{k}"}
            for k in bodies
        ]
        with (
            patch.object(bda_index.s3_client, "get_object", side_effect=get_object),
            patch.object(bda_index, "legacy_hitl_enabled", return_value=True),
            patch.object(bda_index, "resolve_class_schema", return_value=None),
            patch.dict("os.environ", {"DB_NAME": ""}, clear=False),
            patch.object(bda_index, "boto3"),
            patch.object(bda_index, "metrics"),
        ):
            result, hitl = bda_index.process_segments(
                input_bucket="in",
                output_bucket="out",
                object_key="pkg.pdf",
                segment_metadata=segments,
                confidence_threshold=0.8,
                execution_id="exec-1",
                document=doc,
            )

        field_alert = {
            "attribute_name": "InvoiceTotal",
            "confidence": 0.0,
            "confidence_threshold": 0.8,
        }
        assert [s.confidence_threshold_alerts for s in result.sections] == [
            [],
            [field_alert] if where == "field" else [],
            [],
        ]
        # parse_confidence already read a non-finite blueprint score as unscored.
        middle = None if where == "blueprint" else 0.99
        assert [s.confidence for s in result.sections] == [0.99, middle, 0.99]
        assert [m.hitl_triggered for m in result.hitl_metadata] == [False, True, False]
        assert hitl is True
