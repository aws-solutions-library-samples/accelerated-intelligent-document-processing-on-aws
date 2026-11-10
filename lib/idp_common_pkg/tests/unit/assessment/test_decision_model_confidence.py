# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``extraction.confidence.method: decision_model`` (GitHub #1656).

Driven through the real ``AssessmentService.assess_results`` and the real batcher
with a fake decision-model Lambda. What is pinned:

- **One request per page, one noul question per value**, and a value's confidence
  is its **highest** P(yes) across the pages.
- **The output is the LLM path's shape**, so thresholds and alerts apply unchanged:
  a value below its threshold raises an alert.
- **No Bedrock call is made**, and a refusal propagates rather than becoming one.
- **max_pages and max_questions_per_request bound the requests.**
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from idp_common.assessment.service import AssessmentService
from idp_common.decision_model import DecisionModelRefused

pytestmark = pytest.mark.unit

ARN = "arn:aws:lambda:us-west-2:123456789012:function:GENAIIDP-decider"

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "invoice",
    "x-aws-idp-document-type": "invoice",
    "type": "object",
    "properties": {
        "Total": {"type": "string", "description": "The invoice total"},
        "Vendor": {
            "type": "object",
            "properties": {"Name": {"type": "string", "description": "Vendor name"}},
        },
        "Lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "Amount": {"type": "string", "description": "Line amount"}
                },
            },
        },
    },
}

EXTRACTED = {
    "Total": "100.00",
    "Vendor": {"Name": "Acme"},
    "Lines": [{"Amount": "60.00"}, {"Amount": "40.00"}],
}


class FakeDecider:
    """Answers each question by looking up P(yes) for (page, field text)."""

    def __init__(self, p_by_page: List[Dict[str, float]], error: Any = None):
        self.p_by_page = p_by_page
        self.error = error
        self.requests: List[Dict[str, Any]] = []

    def invoke(self, FunctionName, InvocationType, Payload):
        request = json.loads(Payload)
        self.requests.append(request)
        if self.error is not None:
            return {
                "FunctionError": "Unhandled",
                "Payload": io.BytesIO(json.dumps(self.error).encode()),
            }
        # Requests carry one image each; the image bytes encode the page index.
        page = int(__import__("base64").b64decode(request["images"][0])[-1:].decode())
        table = self.p_by_page[page]
        answers = {}
        for name, question in request["questions"].items():
            p = next(
                (v for k, v in table.items() if f'"{k}"' in question["instructions"]),
                0.5,
            )
            answers[name] = {"type": "noul", "noul": p}
        body = {"model": "strands-decider-test", "answers": answers, "usage": {}}
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(body).encode())}


def _service(**decision_model):
    return AssessmentService(
        region="us-west-2",
        config={
            "classes": [SCHEMA],
            "hitl": {"confidence_threshold": 0.8},
            "extraction": {
                "confidence": {
                    "mode": "separate",
                    "method": "decision_model",
                    "decision_model": {"endpoint": ARN, **decision_model},
                }
            },
        },
    )


def _pages(n: int) -> List[bytes]:
    # Not real images: encode_image is patched to pass bytes through, and the fake
    # reads the trailing digit as the page index.
    return [f"PAGE-{i}".encode() for i in range(n)]


def _assess(service, decider, pages):
    with (
        patch("idp_common.decision_model.invoker.boto3.client", return_value=decider),
        patch(
            "idp_common.assessment.decision_model_confidence.encode_image",
            side_effect=lambda b: __import__("base64").b64encode(b).decode(),
        ),
        patch(
            "idp_common.bedrock.invoke_model",
            side_effect=AssertionError("Bedrock was called"),
        ),
    ):
        return service.assess_results(
            class_label="invoice",
            extraction_results=EXTRACTED,
            document_text="ocr text",
            page_images=pages,
        )


def test_one_request_per_page_and_the_best_page_wins():
    decider = FakeDecider(
        [
            {"Total": 0.30, "Vendor.Name": 0.95, "Lines[0].Amount": 0.9},
            {"Total": 0.92, "Vendor.Name": 0.10, "Lines[1].Amount": 0.85},
        ]
    )
    core = _assess(_service(), decider, _pages(2))

    assert len(decider.requests) == 2
    assert len(decider.requests[0]["questions"]) == 4  # Total, Name, 2 amounts
    assert all(q["type"] == "noul" for q in decider.requests[0]["questions"].values())
    a = core.enhanced_assessment
    assert a["Total"]["confidence"] == pytest.approx(0.92)
    assert "page 2" in a["Total"]["confidence_reason"]
    assert a["Vendor"]["Name"]["confidence"] == pytest.approx(0.95)
    assert a["Lines"][0]["Amount"]["confidence"] == pytest.approx(0.9)
    assert a["Total"]["confidence_source"] == "decision_model"
    assert "Assessment/decision_model/GENAIIDP-decider" in core.metering


def test_values_below_threshold_raise_alerts():
    decider = FakeDecider(
        [{"Total": 0.95, "Vendor.Name": 0.95, "Lines[0].Amount": 0.4}]
    )
    core = _assess(_service(), decider, _pages(1))
    alerted = {a["attribute_name"] for a in core.confidence_threshold_alerts}
    assert any(name.startswith("Lines[0]") for name in alerted)
    assert not any(name.startswith("Total") for name in alerted)


def test_the_question_names_field_description_and_value():
    decider = FakeDecider([{}])
    _assess(_service(), decider, _pages(1))
    texts = [q["instructions"] for q in decider.requests[0]["questions"].values()]
    assert any(
        '"Total"' in t and "The invoice total" in t and '"100.00"' in t for t in texts
    )


def test_max_pages_and_chunking_bound_the_requests():
    decider = FakeDecider([{}, {}, {}])
    _assess(_service(max_pages=2, max_questions_per_request=3), decider, _pages(3))
    # Two pages read, four questions in chunks of three: 2 x 2 requests.
    assert len(decider.requests) == 4
    assert sorted(len(r["questions"]) for r in decider.requests) == [1, 1, 3, 3]


def test_a_refusal_propagates_and_calls_no_llm():
    decider = FakeDecider(
        [], error={"errorType": "ValueError", "errorMessage": "over the window"}
    )
    with pytest.raises(DecisionModelRefused, match="over the window"):
        _assess(_service(), decider, _pages(1))


def test_through_the_batcher_every_row_is_scored_once():
    from idp_common.assessment.batching import assess_results_batched

    service = _service()
    decider = FakeDecider([{"Total": 0.9, "Vendor.Name": 0.9}])
    with (
        patch("idp_common.decision_model.invoker.boto3.client", return_value=decider),
        patch(
            "idp_common.assessment.decision_model_confidence.encode_image",
            side_effect=lambda b: __import__("base64").b64encode(b).decode(),
        ),
        patch(
            "idp_common.bedrock.invoke_model",
            side_effect=AssertionError("Bedrock was called"),
        ),
    ):
        out = assess_results_batched(
            service,
            class_label="invoice",
            extraction_results=EXTRACTED,
            document_text="ocr text",
            page_images=_pages(1),
            batch_size=25,
            class_schema=service._get_class_schema("invoice"),
            default_confidence_threshold=0.8,
        )
    assert len(out["assessment"]["Lines"]) == 2
    assert out["parsing_succeeded"] is True


def test_integrated_mode_is_refused_with_the_decision_model():
    from idp_common.config.models import IDPConfig

    with pytest.raises(Exception, match="needs mode"):
        IDPConfig.model_validate(
            {
                "extraction": {
                    "confidence": {"method": "decision_model", "mode": "integrated"}
                }
            }
        )
    with pytest.raises(Exception, match="method must be"):
        IDPConfig.model_validate({"extraction": {"confidence": {"method": "vote"}}})
    with pytest.raises(Exception):
        IDPConfig.model_validate(
            {"extraction": {"confidence": {"decision_model": {"max_pages": 51}}}}
        )


def test_an_empty_question_and_a_price_per_request_are_handled_at_load():
    from idp_common.config.models import IDPConfig

    cfg = IDPConfig.model_validate(
        {"extraction": {"confidence": {"decision_model": {"instructions": ""}}}}
    )
    assert "{VALUE}" in cfg.extraction.confidence.decision_model.instructions
    with pytest.raises(Exception, match="price_per_request is not read"):
        IDPConfig.model_validate(
            {
                "extraction": {
                    "confidence": {"decision_model": {"price_per_request": 0.01}}
                }
            }
        )


def test_a_bedrock_override_is_refused_rather_than_ignored():
    service = _service()
    with pytest.raises(ValueError, match="cannot run the Bedrock model override"):
        service.assess_results(
            class_label="invoice",
            extraction_results=EXTRACTED,
            document_text="",
            page_images=_pages(1),
            model_id_override="us.anthropic.claude-sonnet-5",
        )


def test_a_page_without_an_image_keeps_the_later_page_numbers():
    decider = FakeDecider([{}, {"Total": 0.99}])
    pages: List[Any] = [None, *_pages(2)]  # page 1 has no image
    # The fake reads the page index from the image bytes: shift them to match.
    pages[2] = b"PAGE-1"
    pages[1] = b"PAGE-0"
    core = _assess(_service(), decider, pages)
    assert "page 3" in core.enhanced_assessment["Total"]["confidence_reason"]


def test_the_method_needs_an_endpoint_and_ocr_geometry():
    from idp_common.config.models import IDPConfig

    def cfg(confidence, geometry=None):
        extraction = {"confidence": {"mode": "separate", **confidence}}
        if geometry:
            extraction["geometry"] = {"mode": geometry}
        return IDPConfig.model_validate({"extraction": extraction})

    with pytest.raises(Exception, match="endpoint is empty"):
        cfg({"method": "decision_model"})
    ok = {"method": "decision_model", "decision_model": {"endpoint": ARN}}
    assert cfg(ok).extraction.geometry.mode == "ocr_only"
    for mode in ("llm", "llm_grounded"):
        with pytest.raises(Exception, match="returns no bounding boxes"):
            cfg(ok, geometry=mode)
    # Off scores nothing, so neither rule applies.
    cfg({"method": "decision_model", "mode": "off"}, geometry="llm")
