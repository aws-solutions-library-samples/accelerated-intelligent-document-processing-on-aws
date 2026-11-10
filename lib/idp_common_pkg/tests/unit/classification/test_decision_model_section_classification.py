# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``section_classification.method: decision_model`` (GitHub #1656).

Driven through the real ``split_then_classify_document`` with the splitting call
answered by a fake Bedrock and the section call answered by a fake decision-model
Lambda. Four properties are the ones a refactor could lose quietly:

- **The request is the one the method promises**: one ``choice`` question whose
  options are the configured classes with their descriptions, the section's first
  ``max_pages`` pages as images (the default input), and no Bedrock section call.
- **The confidence is the chosen class's probability**, not the decider's
  normalised ``confidence`` field, which is a different number.
- **A failed call fails its section** and never becomes an LLM call.
- **``low_confidence`` routes only answers below the threshold**, and only when
  it is set.
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from idp_common.classification.service import ClassificationService
from idp_common.config.schema_constants import UNCLASSIFIED_CLASS
from tests.unit.classification.test_split_then_classify import (
    _classes,
    _document,
    _png,
    _reply,
)

pytestmark = pytest.mark.unit

ARN = "arn:aws:lambda:us-west-2:123456789012:function:GENAIIDP-decider"


class FakeDecider:
    """A decision-model Lambda that answers each section with the next distribution."""

    def __init__(self, distributions: List[Dict[str, float]], error: Any = None):
        self.distributions = list(distributions)
        self.error = error
        self.requests: List[Dict[str, Any]] = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.requests.append(json.loads(Payload))
        if self.error is not None:
            return {
                "FunctionError": "Unhandled",
                "Payload": io.BytesIO(json.dumps(self.error).encode()),
            }
        probs = self.distributions.pop(0)
        choice = max(probs, key=lambda name: probs[name])
        body = {
            "model": "strands-decider-test",
            "answers": {
                "doc_class": {
                    "type": "choice",
                    "choice": choice,
                    "probabilities": probs,
                    # Deliberately unlike probs[choice], so a test can tell them apart.
                    "confidence": 0.123,
                }
            },
            "usage": {"input_tokens": 200, "output_tokens": 1},
        }
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(body).encode())}


def _service(decision_model=None, low_confidence=None, classes=("invoice", "receipt")):
    section = {
        "method": "decision_model",
        "pages": "all",
        "decision_model": {"endpoint": ARN, **(decision_model or {})},
    }
    if low_confidence:
        section["low_confidence"] = low_confidence
    config = {
        "classes": _classes(*classes),
        "classification": {
            "strategy": "split_then_classify",
            "split_then_classify": {
                "splitting": {"llm": {"granularity": "packet"}},
                "section_classification": section,
            },
        },
    }
    return ClassificationService(region="us-west-2", config=config)


def _split(*ranges):
    segments = [{"ordinal_start_page": a, "ordinal_end_page": b} for a, b in ranges]
    calls: List[str] = []

    def bedrock(content, config):
        calls.append(config.get("stage"))
        if config.get("stage") == ClassificationService.STAGE_SPLITTING:
            return _reply({"segments": segments}, "Splitting")
        return _reply({"class": "receipt"}, "SectionClassification")

    return bedrock, calls


def _run(service, document, decider, bedrock):
    with (
        patch("idp_common.s3.get_text_content", return_value="page text"),
        patch("idp_common.image.prepare_image", return_value=_png()),
        patch.object(service, "_invoke_bedrock_model", side_effect=bedrock),
        patch("idp_common.decision_model.client.boto3.client", return_value=decider),
    ):
        return service.split_then_classify_document(document)


def test_each_section_is_one_choice_question_over_the_classes():
    service = _service()
    decider = FakeDecider(
        [{"invoice": 0.9, "receipt": 0.1}, {"invoice": 0.2, "receipt": 0.8}]
    )
    bedrock, calls = _split((1, 2), (3, 3))
    result = _run(service, _document(3), decider, bedrock)

    assert [s.classification for s in result.sections] == ["invoice", "receipt"]
    assert calls == [ClassificationService.STAGE_SPLITTING]  # no LLM section call
    first = decider.requests[0]
    question = first["questions"]["doc_class"]
    assert question["type"] == "choice"
    assert set(question["criteria"]) == {"invoice", "receipt"}
    assert "distinctive layout" in question["criteria"]["invoice"]
    # Default input is images, at the default max_pages of 1.
    assert len(first["images"]) == 1 and first["state"] == ""


def test_confidence_is_the_chosen_class_probability():
    service = _service()
    decider = FakeDecider([{"invoice": 0.9, "receipt": 0.1}])
    bedrock, _ = _split((1, 1))
    result = _run(service, _document(1), decider, bedrock)

    section = result.sections[0]
    assert section.confidence == pytest.approx(0.9)
    page = result.pages["1"]
    assert page.classification_candidates[0] == {"class": "invoice", "probability": 0.9}
    assert "Decision model strands-decider-test" in (page.classification_reason or "")
    assert "SectionClassification/decision_model/GENAIIDP-decider" in result.metering


def test_max_pages_bounds_the_images_and_text_input_sends_text():
    service = _service(decision_model={"max_pages": 2, "input": "both"})
    decider = FakeDecider([{"invoice": 0.6, "receipt": 0.4}])
    bedrock, _ = _split((1, 4))
    _run(service, _document(4), decider, bedrock)

    request = decider.requests[0]
    assert len(request["images"]) == 2
    assert request["state"].count("page text") == 2


def test_a_refusal_fails_the_section_and_calls_no_llm():
    service = _service()
    decider = FakeDecider(
        [],
        error={
            "errorType": "ValueError",
            "errorMessage": "prompt of 5000 tokens exceeds the context window of 4096",
        },
    )
    bedrock, calls = _split((1, 1))
    result = _run(service, _document(1), decider, bedrock)

    assert result.sections[0].classification == UNCLASSIFIED_CLASS
    assert calls == [ClassificationService.STAGE_SPLITTING]
    assert any("4096" in e for e in result.errors)
    assert len(decider.requests) == 1  # refusals are never retried


def test_low_confidence_llm_relabels_only_answers_below_the_threshold():
    service = _service(
        low_confidence={"min_confidence": 0.7, "on_low_confidence": "llm"}
    )
    decider = FakeDecider(
        [{"invoice": 0.95, "receipt": 0.05}, {"invoice": 0.55, "receipt": 0.45}]
    )
    bedrock, calls = _split((1, 1), (2, 2))
    result = _run(service, _document(2), decider, bedrock)

    # The confident section keeps the decider's answer; the other is relabeled.
    assert [s.classification for s in result.sections] == ["invoice", "receipt"]
    assert calls.count(ClassificationService.STAGE_SECTION_CLASSIFICATION) == 1


def test_low_confidence_fallback_class_assigns_the_fallback():
    service = _service(low_confidence={"min_confidence": 0.7})
    decider = FakeDecider([{"invoice": 0.55, "receipt": 0.45}])
    bedrock, calls = _split((1, 1))
    result = _run(service, _document(1), decider, bedrock)

    assert result.sections[0].classification == service._fallback_class()
    assert calls == [ClassificationService.STAGE_SPLITTING]


def test_label_map_maps_a_model_label_onto_a_class():
    service = _service(decision_model={"label_map": {"bill": "invoice"}})
    decider = FakeDecider([{"bill": 0.8, "receipt": 0.2}])
    bedrock, _ = _split((1, 1))
    result = _run(service, _document(1), decider, bedrock)
    assert result.sections[0].classification == "invoice"


def test_the_method_and_its_bounds_are_validated():
    from idp_common.config.models import IDPConfig

    def cfg(section):
        return IDPConfig.model_validate(
            {
                "classification": {
                    "strategy": "split_then_classify",
                    "split_then_classify": {"section_classification": section},
                }
            }
        )

    assert (
        cfg(
            {"method": "decision_model"}
        ).classification.split_then_classify.section_classification.method
        == "decision_model"
    )
    with pytest.raises(Exception, match="1329"):
        cfg({"method": "embeddings"})
    with pytest.raises(Exception):
        cfg({"decision_model": {"max_pages": 5}})
    with pytest.raises(Exception, match="input must be"):
        cfg({"decision_model": {"input": "audio"}})
    with pytest.raises(Exception, match="score_calibration"):
        cfg({"decision_model": {"score_calibration": "softmax"}})
    with pytest.raises(Exception, match="on_low_confidence"):
        cfg({"low_confidence": {"on_low_confidence": "retry"}})
