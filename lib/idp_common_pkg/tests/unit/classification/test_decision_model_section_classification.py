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
- **The section's text is cut from the end to fit ``max_input_tokens``** (#2032),
  so the first page stays whole when it fits; a request with no text is unchanged.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from idp_common.classification.service import ClassificationService
from idp_common.config.models import DECISION_MODEL_MAX_SECTION_PAGES
from idp_common.config.schema_constants import UNCLASSIFIED_CLASS
from tests.unit.classification.test_split_then_classify import (
    _classes,
    _document,
    _png,
    _reply,
)
from tests.unit.decision_model.window_contract import (  # noqa: F401 - autouse
    DENSE_STYLES,
    WINDOW_INPUT_TOKENS,
    dense_page,
    no_oversize_requests,
    request_tokens,
    window_refusal,
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
        refusal = window_refusal(Payload)
        if refusal:
            return refusal
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
        patch("idp_common.decision_model.invoker.boto3.client", return_value=decider),
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

    selected = cfg({"method": "decision_model", "decision_model": {"endpoint": ARN}})
    section = selected.classification.split_then_classify.section_classification
    assert section.method == "decision_model"
    with pytest.raises(Exception, match="endpoint is empty"):
        cfg({"method": "decision_model"})
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


def test_an_empty_question_is_the_shipped_one():
    from idp_common.config.models import IDPConfig

    cfg = IDPConfig.model_validate(
        {
            "classification": {
                "split_then_classify": {
                    "section_classification": {"decision_model": {"instructions": " "}}
                }
            }
        }
    )
    dm = cfg.classification.split_then_classify.section_classification.decision_model
    assert dm.instructions == "Which type of document is this?"


# --- The section's text is cut to fit max_input_tokens (#2032) -----------------


def _run_with_texts(service, document, decider, bedrock, texts: List[str]):
    """``_run`` with each page's own OCR text instead of one shared string."""
    by_uri = {f"s3://b/{n}.txt": text for n, text in enumerate(texts, start=1)}
    with (
        patch("idp_common.s3.get_text_content", side_effect=by_uri.__getitem__),
        patch("idp_common.image.prepare_image", return_value=_png()),
        patch.object(service, "_invoke_bedrock_model", side_effect=bedrock),
        patch("idp_common.decision_model.invoker.boto3.client", return_value=decider),
    ):
        return service.split_then_classify_document(document)


def _request_tokens(request: Dict[str, Any]) -> float:
    """What the request holds, by the estimate the labeler cuts against."""
    from idp_common.decision_model import IMAGE_TOKENS, estimate_tokens

    return (
        estimate_tokens(request["state"])
        + estimate_tokens(json.dumps(request["questions"]["doc_class"]))
        + IMAGE_TOKENS * len(request["images"])
    )


def test_the_first_page_stays_whole_when_the_section_text_is_cut(caplog):
    from idp_common.decision_model import DEFAULT_MAX_INPUT_TOKENS

    first = "INVOICE 7\nBill to: Acme Corp"
    second = "\n".join(f"Line {i:04d} widget {i:03d}.00 USD" for i in range(800))
    third = "Thank you for your business"
    service = _service(decision_model={"max_pages": 3, "input": "text"})
    decider = FakeDecider([{"invoice": 0.7, "receipt": 0.3}])
    bedrock, _ = _split((1, 3))
    with caplog.at_level(logging.INFO):
        _run_with_texts(service, _document(3), decider, bedrock, [first, second, third])

    (request,) = decider.requests
    state = request["state"]
    assert state.startswith(f"Page 1:\n{first}\n\nPage 2:\n")
    assert third not in state
    assert _request_tokens(request) <= DEFAULT_MAX_INPUT_TOKENS
    dropped = [r.getMessage() for r in caplog.records if "Dropped" in r.getMessage()]
    assert len(dropped) == 1 and "pages 1, 2, 3" in dropped[0]


def test_a_lowered_max_input_tokens_is_honoured_for_a_section():
    texts = ["\n".join(f"Row {i} of page {p}" for i in range(60)) for p in (1, 2)]
    settings = {"max_pages": 2, "input": "both"}
    lowered = FakeDecider([{"invoice": 0.7, "receipt": 0.3}])
    default = FakeDecider([{"invoice": 0.7, "receipt": 0.3}])
    for decider, extra in ((lowered, {"max_input_tokens": 900}), (default, {})):
        bedrock, _ = _split((1, 2))
        service = _service(decision_model={**settings, **extra})
        _run_with_texts(service, _document(2), decider, bedrock, texts)

    (cut,) = lowered.requests
    (whole,) = default.requests
    assert _request_tokens(whole) > 900
    assert _request_tokens(cut) <= 900
    assert whole["state"].startswith(cut["state"]) and cut["state"] != whole["state"]
    assert whole["state"] == f"Page 1:\n{texts[0]}\n\nPage 2:\n{texts[1]}"


def test_a_section_request_with_no_text_or_text_that_fits_is_untouched(caplog):
    images_only = FakeDecider([{"invoice": 0.7, "receipt": 0.3}])
    short_text = FakeDecider([{"invoice": 0.7, "receipt": 0.3}])
    with caplog.at_level(logging.INFO):
        bedrock, _ = _split((1, 1))
        _run_with_texts(
            _service(decision_model={"max_input_tokens": 512}),
            _document(1),
            images_only,
            bedrock,
            ["A short page.\n"],
        )
        bedrock, _ = _split((1, 1))
        _run_with_texts(
            _service(decision_model={"input": "text"}),
            _document(1),
            short_text,
            bedrock,
            ["A short page.\n"],
        )
    assert images_only.requests[0]["state"] == ""
    assert len(images_only.requests[0]["images"]) == 1
    assert short_text.requests[0]["state"] == "Page 1:\nA short page.\n"
    assert not any("Dropped" in r.getMessage() for r in caplog.records)


def test_a_section_whose_text_has_no_room_is_failed_rather_than_sent_empty():
    """Twelve class descriptions fill max_input_tokens, so the section's text
    would be cut to nothing and a class picked from no evidence. The request is
    not sent, and the section fails as a refused call does."""
    classes = tuple(f"class{i:02d}" for i in range(12))
    service = _service(
        decision_model={"input": "text", "max_input_tokens": 256}, classes=classes
    )
    decider = FakeDecider([])
    bedrock, _ = _split((1, 1))
    result = _run_with_texts(
        service, _document(1), decider, bedrock, ["INVOICE 7, Bill to: Acme"]
    )

    assert decider.requests == []
    assert result.sections[0].classification == UNCLASSIFIED_CLASS


def test_a_limit_with_no_room_for_section_text_is_refused_at_load():
    from idp_common.config.models import IDPConfig

    def section(**decision_model):
        return IDPConfig.model_validate(
            {
                "classification": {
                    "split_then_classify": {
                        "section_classification": {"decision_model": decision_model}
                    }
                }
            }
        )

    # 'both' at two pages carries two images, 512 tokens before any text.
    with pytest.raises(Exception, match="max_input_tokens"):
        section(input="both", max_pages=2, max_input_tokens=512)
    assert section(input="both", max_pages=1, max_input_tokens=512)
    assert section(input="images", max_pages=2, max_input_tokens=256)


def test_every_section_request_fits_the_window_at_the_most_pages():
    """#2051: a section of dense pages at the largest ``max_pages``, in ``text``,
    ``both`` and ``images``, with two classes and with eight, at the default
    ``max_input_tokens``. Every request fits the window; with text, the section's
    text had to be cut to make it fit."""
    pages = DECISION_MODEL_MAX_SECTION_PAGES
    classes_cases = (("invoice", "receipt"), tuple(f"class{i}" for i in range(8)))
    for mode in ("text", "both", "images"):
        for classes in classes_cases:
            service = _service(
                decision_model={"input": mode, "max_pages": pages}, classes=classes
            )
            cfg = service.config.classification.split_then_classify
            limit = cfg.section_classification.decision_model.max_input_tokens
            assert limit == WINDOW_INPUT_TOKENS
            for style in DENSE_STYLES if mode != "images" else DENSE_STYLES[:1]:
                case = f"input {mode}, {len(classes)} classes, {style} text"
                texts = [dense_page(style, p) for p in range(1, pages + 1)]
                decider = FakeDecider([{name: 1 / len(classes) for name in classes}])
                bedrock, _ = _split((1, pages))
                result = _run_with_texts(
                    service, _document(pages), decider, bedrock, texts
                )

                assert result.sections[0].classification in classes, (
                    f"{case}: {result.errors}"
                )
                (request,) = decider.requests
                assert len(request["images"]) == (pages if mode != "text" else 0)
                if mode != "images":
                    # The first page alone is over the window, so it was cut.
                    assert request["state"].startswith(f"Page 1:\n{texts[0][:20]}")
                    assert "Page 2:" not in request["state"], case
                tokens = request_tokens(request)
                assert tokens <= WINDOW_INPUT_TOKENS, (
                    f"{case}: the request was about {tokens:.0f} tokens, over the "
                    f"{WINDOW_INPUT_TOKENS} the window holds"
                )


# --- A cut never keeps only the "Page 1:" line (#2111) -------------------------


@pytest.mark.parametrize("mode", ["text", "both"])
@pytest.mark.parametrize("style", DENSE_STYLES)
def test_a_first_page_with_no_line_breaks_is_cut_mid_line_not_to_its_header(
    mode, style
):
    """Dense text on one line (CJK, figures, words): the cut used to back up to the
    line break after "Page 1:" and classify the section from no page text."""
    from idp_common.decision_model import (
        DEFAULT_MAX_INPUT_TOKENS,
        IMAGE_TOKENS,
        estimate_tokens,
    )
    from idp_common.decision_model.budget import LINE_BREAK_BACKOFF_SHARE

    page = dense_page(style, 1).replace("\n", " ")
    decider = FakeDecider([{"invoice": 0.7, "receipt": 0.3}])
    bedrock, _ = _split((1, 1))
    _run_with_texts(
        _service(decision_model={"input": mode}), _document(1), decider, bedrock, [page]
    )

    (request,) = decider.requests
    state = request["state"]
    assert state.startswith("Page 1:\n") and f"Page 1:\n{page}".startswith(state)
    budget = (
        DEFAULT_MAX_INPUT_TOKENS
        - estimate_tokens(json.dumps(request["questions"]["doc_class"]))
        - IMAGE_TOKENS * len(request["images"])
    )
    assert estimate_tokens(state) >= budget * (1 - LINE_BREAK_BACKOFF_SHARE) - 2


@pytest.mark.parametrize(
    "first_page, kept_head",
    # The empty-first-page budget holds about five characters of page 2: under
    # half the budget once both header lines are discounted, over half if the
    # second one counted as evidence.
    [(None, "Page 1:\nab"), ("", "Page 1:\n\n\nPage 2:\n" + "第" * 4)],
    ids=["one-page", "empty-first-page"],
)
def test_a_section_whose_cut_would_keep_only_its_headers_is_failed(
    first_page, kept_head
):
    """A limit that leaves room for the page header lines and a few characters:
    the section is refused, not classified from its header lines."""
    import math

    from idp_common.classification.decision_model_labeler import (
        DecisionModelSectionLabeler,
    )
    from idp_common.decision_model import estimate_tokens

    classes = tuple(f"class{i:02d}" for i in range(12))
    pages = 1 if first_page is None else 2

    def labeler(limit=None):
        extra = {} if limit is None else {"max_input_tokens": limit}
        service = _service(
            decision_model={"input": "text", "max_pages": pages, **extra},
            classes=classes,
        )
        section_cfg = (
            service.config.classification.split_then_classify.section_classification
        )
        return service, DecisionModelSectionLabeler(service, section_cfg)

    # The question as the request builder counts it, from a request it built.
    _, probe = labeler()
    built = probe.build_request(None, {"p1": "x"}, ["p1"])
    question = estimate_tokens(
        json.dumps(built.questions["doc_class"].model_dump(mode="json"))
    )
    limit = math.ceil(question + estimate_tokens(kept_head))
    assert limit >= 256  # the least max_input_tokens accepted

    service, _ = labeler(limit)
    decider = FakeDecider([])
    bedrock, _ = _split((1, pages))
    page = dense_page("cjk", 1).replace("\n", " ")
    texts = [page] if first_page is None else [first_page, page]
    result = _run_with_texts(service, _document(pages), decider, bedrock, texts)

    assert decider.requests == []
    assert result.sections[0].classification == UNCLASSIFIED_CLASS


def test_an_images_request_over_the_limit_is_refused_locally_not_sent():
    """Long class descriptions and four page images: the request is refused before
    it is sent, with the reason, instead of going out to be refused."""
    from idp_common.decision_model import DEFAULT_MAX_INPUT_TOKENS, IMAGE_TOKENS

    schemas = _classes("invoice", "receipt")
    for schema in schemas:
        schema["description"] = f"A {schema['$id']} " + "with a long layout note " * 120
    room = DEFAULT_MAX_INPUT_TOKENS - 4 * IMAGE_TOKENS
    from idp_common.decision_model import estimate_tokens

    assert sum(estimate_tokens(s["description"]) for s in schemas) > room

    config = {
        "classes": schemas,
        "classification": {
            "strategy": "split_then_classify",
            "split_then_classify": {
                "splitting": {"llm": {"granularity": "packet"}},
                "section_classification": {
                    "method": "decision_model",
                    "pages": "all",
                    "decision_model": {
                        "endpoint": ARN,
                        "input": "images",
                        "max_pages": 4,
                    },
                },
            },
        },
    }
    service = ClassificationService(region="us-west-2", config=config)
    decider = FakeDecider([])
    bedrock, _ = _split((1, 4))
    result = _run_with_texts(
        service, _document(4), decider, bedrock, ["a", "b", "c", "d"]
    )

    assert decider.requests == []
    assert result.sections[0].classification == UNCLASSIFIED_CLASS


def test_max_input_tokens_above_the_window_less_the_reserve_is_capped(caplog):
    from idp_common.config.models import IDPConfig
    from idp_common.decision_model import DEFAULT_MAX_INPUT_TOKENS

    def section(limit):
        return IDPConfig.model_validate(
            {
                "classification": {
                    "split_then_classify": {
                        "section_classification": {
                            "decision_model": {"max_input_tokens": limit}
                        }
                    }
                }
            }
        )

    def limit(cfg):
        sc = cfg.classification.split_then_classify.section_classification
        return sc.decision_model.max_input_tokens

    with caplog.at_level(logging.WARNING):
        assert limit(section(DEFAULT_MAX_INPUT_TOKENS)) == DEFAULT_MAX_INPUT_TOKENS
        assert caplog.records == []
        # Capped, not refused, so a configuration saved with a larger value loads.
        assert limit(section(DEFAULT_MAX_INPUT_TOKENS + 1)) == DEFAULT_MAX_INPUT_TOKENS
    (record,) = caplog.records
    assert "max_input_tokens is 3585" in record.getMessage()
