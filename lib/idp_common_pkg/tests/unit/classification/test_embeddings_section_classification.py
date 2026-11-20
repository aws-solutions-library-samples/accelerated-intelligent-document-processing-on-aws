# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``section_classification.method: embeddings`` (GitHub #1657).

Driven through the real ``split_then_classify_document`` with the splitting call
answered by a fake ``_invoke_bedrock_model`` and the embedding calls answered by a
fake ``bedrock-runtime`` client underneath the real ``BedrockClient``, so the request
body, the response parsing and the token header are the product's own code. The
fake maps an image's colour, or a text's class name, to a fixed vector, so every
similarity in these tests is computed rather than asserted.

The properties a refactor could lose quietly:

- **The class is the best-matching reference**, its confidence the softmax
  probability, and no LLM section call is made when that is above the thresholds.
- **Below either threshold the LLM labels the section**, the reason says so, and
  both calls are metered.
- **Embedding calls are metered under their own key** with the token count Bedrock
  reports.
- **A failed embedding call fails its section** and never becomes an LLM call; a
  transient one retries the step.
- **Reference vectors are embedded once per process**, not once per document.
"""

from __future__ import annotations

import base64
import io
import json
import math
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from idp_common.classification import embeddings_labeler as el
from idp_common.classification.service import ClassificationService
from idp_common.config.models import IDPConfig
from idp_common.config.schema_constants import UNCLASSIFIED_CLASS
from idp_common.utils.transient_errors import TransientError
from tests.unit.classification.test_split_then_classify import _classes, _reply

pytestmark = pytest.mark.unit

MODEL = "us.cohere.embed-v4:0"  # the us-west-2 regional default
TOKENS = 123


def _png(colour: Tuple[int, int, int], size: Tuple[int, int] = (4, 4)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


RED, BLUE, PURPLE, GREY = (255, 0, 0), (0, 0, 255), (128, 0, 128), (90, 90, 90)


def _unit(*xs: float) -> List[float]:
    norm = math.sqrt(sum(x * x for x in xs))
    return [x / norm for x in xs]


#: Colour → vector. RED and BLUE are orthogonal; PURPLE sits between them, so its
#: two similarities tie; GREY is nearly orthogonal to both.
VECTORS = {
    RED: _unit(1, 0, 0),
    BLUE: _unit(0, 1, 0),
    PURPLE: _unit(1, 1, 0.1),
    GREY: _unit(0.05, 0.05, 1),
}
TEXT_VECTORS = {"invoice": _unit(1, 0, 0.2), "receipt": _unit(0, 1, 0.2)}


#: The largest image side the fake was sent, per request, in pixels.
SIZES: List[int] = []


class FakeRuntime:
    """``bedrock-runtime`` answering ``invoke_model`` with the vector for its input."""

    def __init__(
        self,
        error: Optional[str] = None,
        fail_on: Optional[str] = None,
        fail_colour: Optional[Tuple[int, int, int]] = None,
    ):
        self.bodies: List[Dict[str, Any]] = []
        self.models: List[str] = []
        self.error = error
        # With ``fail_on`` set, only requests of that input type fail.
        self.fail_on = fail_on
        # With ``fail_colour`` set, only images of that colour fail.
        self.fail_colour = fail_colour

    def invoke_model(self, modelId, body, contentType, accept):
        request = json.loads(body)
        self.bodies.append(request)
        self.models.append(modelId)
        if self.error and self.fail_on in (None, request.get("input_type")):
            raise ClientError(
                {"Error": {"Code": self.error, "Message": "no"}}, "InvokeModel"
            )
        if "images" in request:
            from PIL import Image

            data = base64.b64decode(request["images"][0].split(",", 1)[1])
            opened = Image.open(io.BytesIO(data))
            SIZES.append(max(opened.size))
            colour = opened.convert("RGB").getpixel((0, 0))
            if colour == self.fail_colour:
                raise ClientError(
                    {"Error": {"Code": "ValidationException", "Message": "no"}},
                    "InvokeModel",
                )
            vector = VECTORS[colour]
        else:
            text = request["texts"][0]
            vector = next(v for k, v in TEXT_VECTORS.items() if text.startswith(k))
        payload = {"embeddings": {"float": [vector]}}
        return {
            "body": io.BytesIO(json.dumps(payload).encode()),
            "ResponseMetadata": {
                "HTTPHeaders": {"x-amzn-bedrock-input-token-count": str(TOKENS)}
            },
        }


class _Session:
    def __init__(self, runtime):
        self.runtime = runtime

    def client(self, name, **_):
        assert name == "bedrock-runtime"
        return self.runtime


@pytest.fixture(autouse=True)
def _fresh_cache():
    el.clear_reference_cache()
    yield
    el.clear_reference_cache()


def _examples_classes(colours: Dict[str, Tuple[int, int, int]]):
    classes = _classes(*colours)
    for schema in classes:
        name = schema["x-aws-idp-document-type"]
        schema["x-aws-idp-examples"] = [
            {"name": f"{name}-1", "x-aws-idp-image-path": f"s3://ex/{name}.png"}
        ]
    return classes


def _service(
    *,
    embeddings: Optional[Dict[str, Any]] = None,
    low_confidence: Optional[Dict[str, Any]] = None,
    classes=None,
):
    section = {
        "method": "embeddings",
        "pages": "all",
        "embeddings": {"temperature": 0.05, **(embeddings or {})},
        "low_confidence": {
            "min_confidence": 0.6,
            "on_low_confidence": "llm",
            **(low_confidence or {}),
        },
    }
    config = {
        "classes": classes
        if classes is not None
        else _examples_classes({"invoice": RED, "receipt": BLUE}),
        "classification": {
            "strategy": "split_then_classify",
            "split_then_classify": {
                "splitting": {"llm": {"granularity": "packet"}},
                "section_classification": section,
            },
        },
    }
    return ClassificationService(region="us-west-2", config=config)


def _document(colours: List[Tuple[int, int, int]]):
    from idp_common.models import Document, Page

    return Document(
        id="doc-1",
        input_key="doc.pdf",
        pages={
            str(i): Page(
                page_id=str(i),
                image_uri=f"s3://b/{i}.png",
                parsed_text_uri=f"s3://b/{i}.txt",
            )
            for i in range(1, len(colours) + 1)
        },
    )


def _run(
    service,
    colours,
    ranges,
    runtime,
    llm_class="receipt",
    examples=None,
    size=(4, 4),
    llm_error=None,
):
    segments = [{"ordinal_start_page": a, "ordinal_end_page": b} for a, b in ranges]
    calls: List[str] = []

    def bedrock(content, config):
        calls.append(config.get("stage"))
        if config.get("stage") == ClassificationService.STAGE_SPLITTING:
            return _reply({"segments": segments}, "Splitting")
        if llm_error is not None:
            raise llm_error
        return _reply(
            {"class": llm_class, "classification_reason": "llm says so"},
            "SectionClassification",
        )

    pages = {f"s3://b/{i}.png": _png(c, size) for i, c in enumerate(colours, start=1)}
    examples = examples or {
        "s3://ex/invoice.png": _png(RED),
        "s3://ex/receipt.png": _png(BLUE),
    }
    with (
        patch("idp_common.s3.get_text_content", return_value="page text"),
        patch("idp_common.image.prepare_image", side_effect=lambda uri, *a: pages[uri]),
        patch("idp_common.s3.get_binary_content", side_effect=examples.__getitem__),
        patch.object(service, "_invoke_bedrock_model", side_effect=bedrock),
        patch(
            "idp_common.bedrock.client.get_bedrock_session",
            return_value=_Session(runtime),
        ),
        patch("idp_common.bedrock.client.time.sleep"),
    ):
        result = service.split_then_classify_document(_document(colours))
    return result, calls


def test_each_section_takes_its_best_matching_reference_without_an_llm_call():
    runtime = FakeRuntime()
    result, calls = _run(_service(), [RED, RED, BLUE], [(1, 2), (3, 3)], runtime)

    assert [s.classification for s in result.sections] == ["invoice", "receipt"]
    assert calls == [ClassificationService.STAGE_SPLITTING]
    page = result.pages["1"]
    # Similarities 1.0 vs 0.0 at temperature 0.05: e^20 / (e^20 + 1).
    expected = 1 / (1 + math.exp(-1 / 0.05))
    assert result.sections[0].confidence == pytest.approx(expected)
    assert page.classification_candidates[0]["class"] == "invoice"
    assert page.classification_candidates[0]["probability"] == pytest.approx(
        expected, abs=1e-6
    )
    assert (page.classification_reason or "").startswith(
        f"Embeddings ({MODEL}): invoice"
    )
    # Two references, then pages 1, 2 and 3: five calls, each metered.
    assert len(runtime.bodies) == 5
    key = f"SectionClassification/bedrock/{MODEL}"
    assert result.metering[key]["inputTokens"] == 5 * TOKENS
    assert result.metering[key]["requests"] == 5


def test_references_are_documents_and_sections_are_queries():
    runtime = FakeRuntime()
    _run(_service(), [RED], [(1, 1)], runtime)
    kinds = [b["input_type"] for b in runtime.bodies]
    assert kinds == ["search_document", "search_document", "search_query"]


def test_a_section_below_min_confidence_is_labeled_by_the_llm():
    runtime = FakeRuntime()
    result, calls = _run(_service(), [RED, PURPLE], [(1, 1), (2, 2)], runtime)

    assert [s.classification for s in result.sections] == ["invoice", "receipt"]
    assert calls.count(ClassificationService.STAGE_SECTION_CLASSIFICATION) == 1
    reason = result.pages["2"].classification_reason or ""
    assert reason.startswith(f"Embeddings ({MODEL}):")
    assert "below min_confidence 0.600, so labeled by the LLM: llm says so" in reason
    assert "SectionClassification/bedrock/nova" in result.metering
    assert f"SectionClassification/bedrock/{MODEL}" in result.metering


def test_a_section_below_min_similarity_is_labeled_by_the_llm():
    service = _service(
        embeddings={"min_similarity": 0.5},
        low_confidence={"min_confidence": 0.0},
    )
    # GREY is closest to neither reference, though one of them still wins the softmax.
    result, calls = _run(service, [GREY], [(1, 1)], FakeRuntime(), llm_class="invoice")
    assert result.sections[0].classification == "invoice"
    assert calls.count(ClassificationService.STAGE_SECTION_CLASSIFICATION) == 1
    assert "below min_similarity 0.500" in (
        result.pages["1"].classification_reason or ""
    )


def test_descriptions_mode_embeds_each_class_name_and_description():
    service = _service(
        embeddings={"references": "descriptions"},
        classes=_classes("invoice", "receipt"),
    )
    runtime = FakeRuntime()
    result, _ = _run(service, [RED], [(1, 1)], runtime)

    texts = [b["texts"][0] for b in runtime.bodies if "texts" in b]
    assert texts == [
        "invoice: A invoice document with distinctive layout",
        "receipt: A receipt document with distinctive layout",
    ]
    assert result.sections[0].classification == "invoice"
    # Two description calls and the page, all metered.
    assert result.metering[f"SectionClassification/bedrock/{MODEL}"]["requests"] == 3


def test_a_failed_embedding_call_fails_the_section_and_calls_no_llm():
    runtime = FakeRuntime(error="AccessDeniedException")
    result, calls = _run(_service(), [RED], [(1, 1)], runtime)

    assert result.sections[0].classification == UNCLASSIFIED_CLASS
    assert calls == [ClassificationService.STAGE_SPLITTING]
    assert any("AccessDeniedException" in e for e in result.errors)


def test_a_throttle_the_client_cannot_absorb_retries_the_step():
    runtime = FakeRuntime(error="ThrottlingException")
    with pytest.raises(TransientError):
        _run(_service(), [RED], [(1, 1)], runtime)


def test_references_are_embedded_once_per_process():
    runtime = FakeRuntime()
    _run(_service(), [RED], [(1, 1)], runtime)
    first = len(runtime.bodies)
    result, _ = _run(_service(), [BLUE], [(1, 1)], runtime)
    assert len(runtime.bodies) - first == 1  # the section page only
    # The second document is not charged for references it did not embed.
    assert (
        result.metering[f"SectionClassification/bedrock/{MODEL}"]["inputTokens"]
        == TOKENS
    )


def test_a_class_without_an_example_image_is_never_chosen_by_embeddings():
    classes = _examples_classes({"invoice": RED, "receipt": BLUE})
    classes += _classes("memo")
    result, _ = _run(_service(classes=classes), [RED], [(1, 1)], FakeRuntime())
    names = [c["class"] for c in result.pages["1"].classification_candidates or []]
    assert names == ["invoice", "receipt"]


def test_fewer_than_two_classes_with_references_fails_the_sections():
    classes = _examples_classes({"invoice": RED}) + _classes("receipt")
    result, calls = _run(_service(classes=classes), [RED], [(1, 1)], FakeRuntime())
    assert result.sections[0].classification == UNCLASSIFIED_CLASS
    assert any("at least two classes" in e for e in result.errors)
    assert calls == [ClassificationService.STAGE_SPLITTING]


# --- the scoring functions ----------------------------------------------------


def test_softmax_and_margin():
    probs = el.softmax_probabilities({"a": 0.86, "b": 0.85}, 0.02)
    assert probs["a"] == pytest.approx(1 / (1 + math.exp(-0.5)))
    score = el.SectionScore({"a": 0.86, "b": 0.85}, probs)
    assert score.top_class == "a" and score.margin == pytest.approx(0.01)
    with pytest.raises(ValueError):
        el.softmax_probabilities({"a": 1.0}, 0.0)


def test_vectors_of_different_dimensions_are_refused():
    with pytest.raises(ValueError, match="different models"):
        el.class_similarities([1.0, 0.0], {"a": [[1.0, 0.0, 0.0]]})
    with pytest.raises(ValueError):
        el.mean_unit_vector([[1.0, 0.0], [1.0, 0.0, 0.0]])
    assert el.mean_unit_vector([[2.0, 0.0], [0.0, 3.0]]) == pytest.approx(_unit(1, 1))


# --- configuration ------------------------------------------------------------


def _section(section: Dict[str, Any]):
    return IDPConfig.model_validate(
        {
            "classification": {
                "strategy": "split_then_classify",
                "split_then_classify": {"section_classification": section},
            }
        }
    ).classification.split_then_classify.section_classification


def test_embeddings_need_the_llm_fallback_and_a_threshold():
    with pytest.raises(Exception, match="on_low_confidence 'llm'"):
        _section({"method": "embeddings", "low_confidence": {"min_confidence": 0.5}})
    with pytest.raises(Exception, match="neither"):
        _section(
            {"method": "embeddings", "low_confidence": {"on_low_confidence": "llm"}}
        )
    ok = _section(
        {
            "method": "embeddings",
            "embeddings": {"min_similarity": 0.3},
            "low_confidence": {"on_low_confidence": "llm"},
        }
    )
    assert ok.method == "embeddings"
    with pytest.raises(Exception, match="references must be one of"):
        _section({"embeddings": {"references": "testset"}})


def test_the_new_keys_are_inert_without_the_method():
    # A configuration that does not select embeddings is not validated against them.
    section = _section({"low_confidence": {"min_confidence": 0.0}})
    assert section.method == "llm"
    assert section.embeddings.model_dump() == {
        "model": "",
        "min_similarity": 0.0,
        "references": "examples",
        "temperature": el_default("temperature"),
        "max_image_dimension": el_default("max_image_dimension"),
    }


def el_default(name: str):
    from idp_common.config.models import EmbeddingsConfig

    return EmbeddingsConfig.model_fields[name].default


def test_section_prompt_is_sent_under_embeddings():
    from idp_common.document_image_placeholder import section_prompt_can_be_sent

    assert section_prompt_can_be_sent("llm", "fallback_class")
    assert section_prompt_can_be_sent("embeddings", "llm")
    assert section_prompt_can_be_sent("decision_model", "llm")
    assert not section_prompt_can_be_sent("decision_model", "fallback_class")
    assert not section_prompt_can_be_sent("embeddings", "fallback_class")


def test_a_class_scores_its_best_reference_not_its_first():
    classes = _examples_classes({"invoice": RED, "receipt": BLUE})
    # invoice's first example is grey; its second, red, is the one a red page matches.
    classes[0]["x-aws-idp-examples"] = [
        {"name": "i1", "x-aws-idp-image-path": "s3://ex/grey.png"},
        {"name": "i2", "x-aws-idp-image-path": "s3://ex/invoice.png"},
    ]
    examples = {
        "s3://ex/grey.png": _png(GREY),
        "s3://ex/invoice.png": _png(RED),
        "s3://ex/receipt.png": _png(BLUE),
    }
    result, calls = _run(
        _service(classes=classes), [RED], [(1, 1)], FakeRuntime(), examples=examples
    )
    assert result.sections[0].classification == "invoice"
    assert "similarity 1.000" in (result.pages["1"].classification_reason or "")
    assert calls == [ClassificationService.STAGE_SPLITTING]


def test_page_images_are_downscaled_before_they_are_embedded():
    SIZES.clear()
    service = _service(embeddings={"max_image_dimension": 300})
    _run(service, [RED], [(1, 1)], FakeRuntime(), size=(600, 900))
    assert SIZES[-1] == 300  # the section page, its longer side fitted to the limit


def test_a_section_with_no_loadable_image_is_labeled_by_the_llm():
    service = _service()
    document_pages = _document([RED])
    document_pages.pages["1"].image_uri = None
    runtime = FakeRuntime()
    with patch(
        "tests.unit.classification.test_embeddings_section_classification._document",
        return_value=document_pages,
    ):
        result, calls = _run(service, [RED], [(1, 1)], runtime)
    assert result.sections[0].classification == "receipt"  # the LLM's answer
    assert calls.count(ClassificationService.STAGE_SECTION_CLASSIFICATION) == 1
    assert "no image could be loaded for pages 1, so labeled by the LLM" in (
        result.pages["1"].classification_reason or ""
    )


def test_a_zero_vector_fails_the_section():
    VECTORS[(0, 0, 0)] = [0.0, 0.0, 0.0]
    try:
        result, calls = _run(_service(), [(0, 0, 0)], [(1, 1)], FakeRuntime())
    finally:
        del VECTORS[(0, 0, 0)]
    assert result.sections[0].classification == UNCLASSIFIED_CLASS
    assert any("zero vector" in e for e in result.errors)
    assert calls == [ClassificationService.STAGE_SPLITTING]


def _cohere(result) -> Dict[str, Any]:
    return result.metering.get(f"SectionClassification/bedrock/{MODEL}") or {}


def test_the_confidence_is_the_probability_not_the_similarity():
    # At temperature 0.5 a similarity of 1.0 against 0.0 is a probability of
    # 1 / (1 + e^-2), far enough from 1.0 that the two cannot be confused.
    service = _service(embeddings={"temperature": 0.5})
    result, _ = _run(service, [RED], [(1, 1)], FakeRuntime())
    assert result.sections[0].confidence == pytest.approx(1 / (1 + math.exp(-2)))


def test_a_fallback_section_still_meters_its_embedding_calls():
    result, _ = _run(_service(), [PURPLE], [(1, 1)], FakeRuntime())
    # Two references and the section's one page, although the LLM labelled it.
    assert _cohere(result)["requests"] == 3
    assert _cohere(result)["inputTokens"] == 3 * TOKENS


def test_min_similarity_reads_the_top_similarity_not_the_margin():
    # PURPLE ties between the two classes: margin 0, top similarity about 0.705.
    service = _service(
        embeddings={"min_similarity": 0.5}, low_confidence={"min_confidence": 0.0}
    )
    _, calls = _run(service, [PURPLE], [(1, 1)], FakeRuntime())
    assert calls == [ClassificationService.STAGE_SPLITTING]


def test_a_section_is_the_mean_of_its_pages():
    # RED then BLUE average to a vector equally like both classes, so the
    # section is unsure and goes to the LLM; its first page alone would not.
    result, calls = _run(_service(), [RED, BLUE], [(1, 2)], FakeRuntime())
    assert calls.count(ClassificationService.STAGE_SECTION_CLASSIFICATION) == 1
    assert "similarity 0.707" in (result.pages["1"].classification_reason or "")


def test_the_cache_does_not_serve_one_models_vectors_to_another():
    runtime = FakeRuntime()
    other = "us.cohere.embed-v4:0-other"
    _run(_service(), [RED], [(1, 1)], runtime)
    _run(_service(embeddings={"model": other}), [RED], [(1, 1)], runtime)
    assert runtime.models.count(other) == 3  # both references and the page


def test_reference_images_are_downscaled_too():
    SIZES.clear()
    examples = {
        "s3://ex/invoice.png": _png(RED, (600, 900)),
        "s3://ex/receipt.png": _png(BLUE, (900, 600)),
    }
    service = _service(embeddings={"max_image_dimension": 300})
    _run(service, [RED], [(1, 1)], FakeRuntime(), examples=examples)
    assert SIZES[:2] == [300, 300]


def test_a_single_class_needs_no_embedding_call():
    runtime = FakeRuntime()
    service = _service(classes=_examples_classes({"invoice": RED}))
    result, _ = _run(service, [BLUE], [(1, 1)], runtime)
    assert result.sections[0].classification == "invoice"
    assert runtime.bodies == []


# --- reference metering survives a failed or retried section --------------------


def test_reference_calls_are_metered_when_the_section_call_fails():
    runtime = FakeRuntime(error="ValidationException", fail_on="search_query")
    result, _ = _run(_service(), [RED], [(1, 1)], runtime)
    assert result.sections[0].classification == UNCLASSIFIED_CLASS
    assert _cohere(result)["requests"] == 2  # both references; the page failed


def test_reference_calls_are_metered_when_the_llm_fallback_fails():
    result, _ = _run(
        _service(), [PURPLE], [(1, 1)], FakeRuntime(), llm_error=ValueError("boom")
    )
    assert result.sections[0].classification == UNCLASSIFIED_CLASS
    assert _cohere(result)["requests"] == 3


def test_reference_calls_are_on_the_retried_attempts_failure_metering():
    from idp_common import utils

    runtime = FakeRuntime(error="ThrottlingException", fail_on="search_query")
    with pytest.raises(TransientError) as raised:
        _run(_service(), [RED], [(1, 1)], runtime)
    metering = utils.failure_metering(raised.value)
    assert metering[f"SectionClassification/bedrock/{MODEL}"]["requests"] == 2


def test_the_pages_embedded_before_a_failing_page_are_metered():
    runtime = FakeRuntime(fail_colour=GREY)
    result, _ = _run(_service(), [RED, GREY], [(1, 2)], runtime)
    assert result.sections[0].classification == UNCLASSIFIED_CLASS
    assert _cohere(result)["requests"] == 3  # both references and page 1


def test_label_with_a_single_class_returns_it_without_a_call():
    from idp_common.classification.embeddings_labeler import EmbeddingSectionLabeler

    service = _service(classes=_examples_classes({"invoice": RED}))
    section = service.config.classification.split_then_classify.section_classification
    labeler = EmbeddingSectionLabeler(service, section)
    result = labeler.label(_document([RED]), {}, ["1"], "s", lambda: pytest.fail())
    assert result[0] == "invoice" and result[1] is None
