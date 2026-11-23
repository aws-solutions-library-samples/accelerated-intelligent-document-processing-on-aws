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
- **Text and both modes are per page too** (#2032): each request carries one page's
  OCR text (with that page's image in ``both``), never the whole section's, cut from
  the end so the request fits ``max_input_tokens``. Text past the cut is not sent.
- **A refusal names its page and section** in a warning and in the error.
- **An empty value is asked whether the page shows a value** (#2033), and scores
  1 - the highest P(yes), so a value the extraction missed scores low.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import patch

import pytest

from idp_common.assessment import decision_model_confidence as dmc
from idp_common.assessment.service import AssessmentService
from idp_common.config.models import DECISION_MODEL_MAX_CONFIDENCE_PAGES
from idp_common.decision_model import DecisionModelRefused
from idp_common.models import Document, Page, Section, Status
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
        refusal = window_refusal(request)
        if refusal:
            return refusal
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


# --- Per-page text, truncation, refusals and empty values ----------------------


class ScriptedDecider:
    """A fake decision-model Lambda that answers ``answer(request, instructions)``.

    ``refuse(request)`` returning a message refuses that request the way the
    decider refuses a prompt over its window.
    """

    def __init__(
        self,
        answer: Callable[[Dict[str, Any], str], float] = lambda request, q: 0.5,
        refuse: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
    ):
        self.answer = answer
        self.refuse = refuse
        self.requests: List[Dict[str, Any]] = []

    def invoke(self, FunctionName, InvocationType, Payload):
        request = json.loads(Payload)
        self.requests.append(request)
        refusal = window_refusal(request)
        if refusal:
            return refusal
        message = self.refuse(request) if self.refuse else None
        if message:
            error = {"errorType": "ValueError", "errorMessage": message}
            return {
                "FunctionError": "Unhandled",
                "Payload": io.BytesIO(json.dumps(error).encode()),
            }
        answers = {
            name: {"type": "noul", "noul": self.answer(request, q["instructions"])}
            for name, q in request["questions"].items()
        }
        body = {"model": "strands-decider-test", "answers": answers, "usage": {}}
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(body).encode())}


def _patched(decider):
    """The fake Lambda, image encoding as plain base64, and Bedrock refused."""
    return (
        patch("idp_common.decision_model.invoker.boto3.client", return_value=decider),
        patch(
            "idp_common.assessment.decision_model_confidence.encode_image",
            side_effect=lambda b: base64.b64encode(b).decode(),
        ),
        patch(
            "idp_common.bedrock.invoke_model",
            side_effect=AssertionError("Bedrock was called"),
        ),
    )


def _score(service, decider, *, page_texts, page_images=(), extracted=EXTRACTED):
    lambda_client, encoder, bedrock = _patched(decider)
    with lambda_client, encoder, bedrock:
        return service.assess_results(
            class_label="invoice",
            extraction_results=extracted,
            document_text="\n".join(t or "" for t in page_texts),
            page_images=list(page_images),
            page_texts=page_texts,
        )


def _image_of(request: Dict[str, Any]) -> str:
    return base64.b64decode(request["images"][0]).decode()


def test_text_mode_asks_each_page_with_its_own_text():
    texts = ["Invoice from Acme", "", "Total due 100.00"]
    decider = ScriptedDecider(
        lambda r, q: 0.9 if '"Total"' in q and "100.00" in r["state"] else 0.2
    )
    core = _score(_service(input="text", max_concurrency=1), decider, page_texts=texts)

    # One request per page with text, each holding only that page; the blank page
    # is skipped without renumbering the page after it.
    assert [r["state"] for r in decider.requests] == [texts[0], texts[2]]
    assert all(r["images"] == [] for r in decider.requests)
    total = core.enhanced_assessment["Total"]
    assert total["confidence"] == pytest.approx(0.9)
    assert "highest on page 3 (2 pages read)" in total["confidence_reason"]


def test_both_mode_pairs_each_image_with_its_own_page_text():
    texts = ["text of page one", "text of page two"]
    decider = ScriptedDecider()
    _score(
        _service(input="both", max_concurrency=1),
        decider,
        page_texts=texts,
        page_images=_pages(2),
    )
    pairs = sorted((_image_of(r), r["state"]) for r in decider.requests)
    assert pairs == [("PAGE-0", texts[0]), ("PAGE-1", texts[1])]


def test_both_mode_without_per_page_text_is_refused_rather_than_sent_whole():
    service = _service(input="both")
    lambda_client, encoder, bedrock = _patched(ScriptedDecider())
    with lambda_client, encoder, bedrock, pytest.raises(ValueError, match="per-page"):
        service.assess_results(
            class_label="invoice",
            extraction_results=EXTRACTED,
            document_text="the whole section",
            page_images=_pages(2),
        )


def _longest_question(request: Dict[str, Any]) -> float:
    from idp_common.decision_model import estimate_tokens

    return max(
        estimate_tokens(q["instructions"]) for q in request["questions"].values()
    )


def test_a_long_page_is_cut_from_the_end_to_fit_max_input_tokens(caplog):
    from idp_common.decision_model import DEFAULT_MAX_INPUT_TOKENS, estimate_tokens

    # Lines of varying length, so a cut that ignored line breaks would land mid-line.
    lines = [
        f"Line {i:04d} {'widget ' * (1 + i % 5)}{i * 7 % 1000:03d}.00 USD"
        for i in range(600)
    ]
    page = "\n".join([*lines, "Total due 100.00"])
    assert estimate_tokens(page) > DEFAULT_MAX_INPUT_TOKENS
    decider = ScriptedDecider(
        lambda r, q: (
            0.95 if '"Total"' in q and "Total due 100.00" in r["state"] else 0.1
        )
    )
    with caplog.at_level(logging.INFO):
        core = _score(
            _service(input="text", max_concurrency=1), decider, page_texts=[page]
        )

    # Still one request for the page, its text cut at a line break from the end.
    (request,) = decider.requests
    state = request["state"]
    assert page.startswith(state) and state.endswith("\n")
    assert estimate_tokens(state) + _longest_question(request) <= (
        DEFAULT_MAX_INPUT_TOKENS
    )
    # The total is only in the cut part, so the decision model never saw it.
    assert "Total due 100.00" not in state
    assert core.enhanced_assessment["Total"]["confidence"] == pytest.approx(0.1)
    dropped = [r.getMessage() for r in caplog.records if "Dropped" in r.getMessage()]
    assert len(dropped) == 1
    assert f"Dropped {len(page) - len(state)} of {len(page)}" in dropped[0]
    assert "page 1" in dropped[0]


def test_a_lowered_max_input_tokens_is_honoured_beside_the_image():
    from idp_common.decision_model import IMAGE_TOKENS, estimate_tokens

    page = "\n".join(f"Row {i} of the statement, carried forward" for i in range(120))
    budget = 1000
    lowered, default = ScriptedDecider(), ScriptedDecider()
    _score(
        _service(input="both", max_input_tokens=budget),
        lowered,
        page_texts=[page],
        page_images=_pages(1),
    )
    _score(_service(input="both"), default, page_texts=[page], page_images=_pages(1))

    (cut,) = lowered.requests
    assert estimate_tokens(page) + _longest_question(cut) + IMAGE_TOKENS > budget
    assert (
        estimate_tokens(cut["state"]) + _longest_question(cut) + IMAGE_TOKENS <= budget
    )
    assert page.startswith(cut["state"]) and len(cut["state"]) < len(page)
    assert default.requests[0]["state"] == page


def test_a_request_with_no_text_or_text_that_fits_is_untouched(caplog):
    decider = ScriptedDecider()
    with caplog.at_level(logging.INFO):
        # Images only: there is no text to cut.
        _score(_service(), decider, page_texts=["", ""], page_images=_pages(2))
        # Pages without text stay empty even when the limit leaves no room at all.
        _score(
            _service(input="both", max_input_tokens=320),
            decider,
            page_texts=["", ""],
            page_images=_pages(2),
        )
        # Text that fits is sent exactly as it is, trailing line break included.
        _score(
            _service(input="both"),
            decider,
            page_texts=["", "A short page.\n"],
            page_images=_pages(2),
        )
    sent = sorted((_image_of(r), r["state"]) for r in decider.requests)
    assert sent == [
        ("PAGE-0", ""),
        ("PAGE-0", ""),
        ("PAGE-0", ""),
        ("PAGE-1", ""),
        ("PAGE-1", ""),
        ("PAGE-1", "A short page.\n"),
    ]
    assert all(len(r["images"]) == 1 for r in decider.requests)
    assert not any("Dropped" in r.getMessage() for r in caplog.records)


def test_truncation_cuts_the_tail_at_a_line_break_when_it_can():
    from idp_common.decision_model import estimate_tokens, truncate_to_tokens

    # A line break in the last tenth of the prefix that fits: the cut ends there.
    first = "a" * 39 + "\n"
    text = first + "b" * 39 + "\n" + "c" * 39 + "\n"
    kept, dropped = truncate_to_tokens(text, estimate_tokens(first + "bb"))
    assert (kept, dropped) == (first, len(text) - len(first))
    # One line of word letters with no break: cut where the budget runs out.
    assert truncate_to_tokens("ab" * 50, 10) == ("ab" * 10, 80)
    assert truncate_to_tokens(text, 1000) == (text, 0)
    assert truncate_to_tokens("", 0) == ("", 0)


def test_max_input_tokens_defaults_to_the_window_less_the_prompt_reserve():
    from idp_common.config.models import IDPConfig
    from idp_common.decision_model import (
        CONTEXT_WINDOW_TOKENS,
        DEFAULT_MAX_INPUT_TOKENS,
        PROMPT_RESERVE_TOKENS,
    )

    assert DEFAULT_MAX_INPUT_TOKENS == CONTEXT_WINDOW_TOKENS - PROMPT_RESERVE_TOKENS
    cfg = IDPConfig.model_validate({})
    section = cfg.classification.split_then_classify.section_classification
    assert cfg.extraction.confidence.decision_model.max_input_tokens == (
        DEFAULT_MAX_INPUT_TOKENS
    )
    assert section.decision_model.max_input_tokens == DEFAULT_MAX_INPUT_TOKENS
    lowered = IDPConfig.model_validate(
        {"extraction": {"confidence": {"decision_model": {"max_input_tokens": "900"}}}}
    )
    assert lowered.extraction.confidence.decision_model.max_input_tokens == 900
    with pytest.raises(Exception, match="max_input_tokens"):
        IDPConfig.model_validate(
            {"extraction": {"confidence": {"decision_model": {"max_input_tokens": 9}}}}
        )


def test_max_pages_bounds_the_text_pages_read(caplog):
    texts = ["page one", "page two", "page three"]
    decider = ScriptedDecider()
    with caplog.at_level(logging.WARNING, logger=dmc.__name__):
        _score(
            _service(input="text", max_pages=2, max_concurrency=1),
            decider,
            page_texts=texts,
        )
    assert [r["state"] for r in decider.requests] == texts[:2]
    assert any("reads the first 2" in r.getMessage() for r in caplog.records)


def test_a_refused_page_is_logged_and_named_in_the_error(caplog):
    decider = ScriptedDecider(
        refuse=lambda r: (
            "prompt of 5000 tokens exceeds 4096" if "second" in r["state"] else None
        )
    )
    with (
        caplog.at_level(logging.WARNING, logger=dmc.__name__),
        pytest.raises(DecisionModelRefused) as raised,
    ):
        _score(
            _service(input="text", max_concurrency=1),
            decider,
            page_texts=["first page", "second page"],
        )
    message = str(raised.value)
    assert "for page 2," in message and "5000 tokens" in message
    assert "1 of 2" in message
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "refused" in r.getMessage()
    ]
    assert len(warnings) == 1 and "page 2" in warnings[0]
    # Every request was sent: one refusal does not abandon the others mid-flight.
    assert len(decider.requests) == 2


def test_an_empty_value_is_asked_whether_a_page_shows_one_and_scores_the_complement():
    extracted = {"Total": None, "Vendor": {"Name": ""}, "Lines": [{"Amount": "60.00"}]}

    def answer(request, q):
        page = _image_of(request)
        if '"Total"' in q:  # missed by the extraction: page 1 shows it
            return 0.9 if page == "PAGE-0" else 0.2
        if '"Vendor.Name"' in q:  # truly absent: no page shows one
            return 0.05
        return 0.7

    decider = ScriptedDecider(answer)
    core = _score(
        _service(),
        decider,
        page_texts=["", ""],
        page_images=_pages(2),
        extracted=extracted,
    )
    a = core.enhanced_assessment
    # A value the extraction missed is the least confident, not the most.
    assert a["Total"]["confidence"] == pytest.approx(0.1)
    assert "no value was extracted" in a["Total"]["confidence_reason"]
    assert "page 1" in a["Total"]["confidence_reason"]
    assert a["Vendor"]["Name"]["confidence"] == pytest.approx(0.95)
    # A found value is scored as before.
    assert a["Lines"][0]["Amount"]["confidence"] == pytest.approx(0.7)

    asked = [
        q["instructions"] for r in decider.requests for q in r["questions"].values()
    ]
    total_q = next(t for t in asked if '"Total"' in t)
    assert total_q == (
        'The system found no value for the field "Total" (The invoice total). '
        "Does the page show a value for it?"
    )
    assert not any("(empty" in t for t in asked)
    assert any('"60.00"' in t and "Is that the correct value?" in t for t in asked)


def _section_document(page_count: int) -> Document:
    document = Document(
        id="d",
        input_key="d.pdf",
        input_bucket="in",
        output_bucket="out",
        status=Status.ASSESSING,
    )
    for n in range(1, page_count + 1):
        document.pages[str(n)] = Page(
            page_id=str(n),
            image_uri=f"s3://in/d.pdf/pages/{n}/image.jpg",
            parsed_text_uri=f"s3://in/d.pdf/pages/{n}/parsed.txt",
        )
    document.sections.append(
        Section(
            section_id="1",
            classification="invoice",
            page_ids=[str(n) for n in range(1, page_count + 1)],
            extraction_result_uri="s3://out/d.pdf/sections/1/result.json",
        )
    )
    return document


def _run_section(service, decider, page_texts: List[str]):
    """``process_document_section``: the caller that reads each page's text."""
    extraction = {
        "document_class": {"type": "invoice"},
        "inference_result": EXTRACTED,
        "metadata": {"parsing_succeeded": True},
    }
    by_uri = {
        f"s3://in/d.pdf/pages/{n}/parsed.txt": text
        for n, text in enumerate(page_texts, start=1)
    }
    lambda_client, encoder, bedrock = _patched(decider)
    with (
        lambda_client,
        encoder,
        bedrock,
        patch("idp_common.s3.get_json_content", return_value=extraction),
        patch("idp_common.s3.get_text_content", side_effect=by_uri.__getitem__),
        patch("idp_common.image.prepare_image", return_value=b"img"),
        patch("idp_common.s3.write_content"),
        patch("idp_common.metrics.put_metric"),
    ):
        return service.process_document_section(_section_document(len(page_texts)), "1")


def test_the_assessment_step_sends_each_page_its_own_text():
    texts = ["Acme Corp, invoice 7", "Lines 60.00 and 40.00", "Total due 100.00"]
    decider = ScriptedDecider()
    _run_section(_service(input="text", max_concurrency=1), decider, texts)
    assert [r["state"] for r in decider.requests] == texts


def test_the_assessment_step_names_the_section_when_a_page_is_refused(caplog):
    decider = ScriptedDecider(
        refuse=lambda r: (
            "prompt of 5000 tokens exceeds 4096" if "Total" in r["state"] else None
        )
    )
    with caplog.at_level(logging.WARNING), pytest.raises(DecisionModelRefused):
        _run_section(
            _service(input="text", max_concurrency=1),
            decider,
            ["Acme Corp, invoice 7", "Total due 100.00"],
        )
    section_warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "section 1" in r.getMessage()
    ]
    assert len(section_warnings) == 1
    assert "page 2" in section_warnings[0]
    assert "no confidence scores" in section_warnings[0]


# --- Review follow-ups on PR #2044 ---------------------------------------------


def test_a_page_whose_text_has_no_room_is_refused_rather_than_sent_empty(caplog):
    """When the question alone fills max_input_tokens, the page text would be cut
    to nothing and an empty field scored about 1.0 from no evidence. The request is
    not sent; the pass fails like a refusal, so the section degrades. The question
    alone overflows the limit here, so the refusal says so (#2111)."""
    extracted = {"Total": None, "Notes": "remark " * 120}
    decider = ScriptedDecider()
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(DecisionModelRefused, match="question and 0 image") as raised,
    ):
        _score(
            _service(input="text", max_input_tokens=256),
            decider,
            page_texts=["Total due 100.00"],
            extracted=extracted,
        )
    assert decider.requests == []
    assert "max_input_tokens of 256" in str(raised.value)
    assert any(
        r.levelno == logging.WARNING and "max_input_tokens" in r.getMessage()
        for r in caplog.records
    )


def test_a_limit_with_no_room_for_text_is_refused_when_the_configuration_loads():
    from idp_common.config.models import IDPConfig

    def confidence(**decision_model):
        return IDPConfig.model_validate(
            {"extraction": {"confidence": {"decision_model": decision_model}}}
        )

    # In 'both' the page image alone takes 256, so 256 leaves no room for text.
    with pytest.raises(Exception, match="max_input_tokens"):
        confidence(input="both", max_input_tokens=256)
    # 'images' sends no text, so the same limit is accepted.
    assert confidence(input="images", max_input_tokens=256)
    assert confidence(input="text", max_input_tokens=256)


def test_the_empty_value_question_is_a_setting():
    from idp_common.config.models import IDPConfig

    question = 'Is a value for "{FIELD}" ({DESCRIPTION}) printed on this page?'
    decider = ScriptedDecider()
    _score(
        _service(empty_value_instructions=question),
        decider,
        page_texts=[""],
        page_images=_pages(1),
        extracted={"Total": None},
    )
    (asked,) = [
        q["instructions"] for r in decider.requests for q in r["questions"].values()
    ]
    assert asked == 'Is a value for "Total" (The invoice total) printed on this page?'
    # Empty is the shipped question, as for `instructions`.
    shipped = IDPConfig.model_validate(
        {
            "extraction": {
                "confidence": {"decision_model": {"empty_value_instructions": ""}}
            }
        }
    ).extraction.confidence.decision_model.empty_value_instructions
    assert shipped == (
        'The system found no value for the field "{FIELD}" ({DESCRIPTION}). '
        "Does the page show a value for it?"
    )


def test_every_request_fits_the_window_at_the_largest_section():
    """#2051: dense text on every page of a section at the largest ``max_pages``,
    in ``text`` and ``both``, at the default ``max_input_tokens``. Every request,
    one per page, fits the window, and each carries text that had to be cut."""
    pages = DECISION_MODEL_MAX_CONFIDENCE_PAGES
    images = _pages(pages)
    for mode in ("text", "both"):
        service = _service(input=mode, max_pages=pages)
        cfg = service.config.extraction.confidence.decision_model
        assert cfg.max_input_tokens == WINDOW_INPUT_TOKENS
        for style in DENSE_STYLES:
            texts = [dense_page(style, p) for p in range(1, pages + 1)]
            decider = ScriptedDecider()
            _score(service, decider, page_texts=texts, page_images=images)

            case = f"input {mode}, {style} text"
            # One request per page, each with a different page's text, cut.
            states = [request["state"] for request in decider.requests]
            assert len(set(states)) == pages, case
            for state in states:
                source = next(t for t in texts if t.startswith(state))
                assert 0 < len(state) < len(source), (
                    f"{case}: a page was not cut, so the case proves nothing"
                )
            for request in decider.requests:
                assert len(request["images"]) == (mode == "both"), case
                tokens = request_tokens(request)
                assert tokens <= WINDOW_INPUT_TOKENS, (
                    f"{case}: a request of about {tokens:.0f} tokens was sent, "
                    f"over the {WINDOW_INPUT_TOKENS} the window holds"
                )


# --- A page with a short first line and no other line break (#2111) -----------


@pytest.mark.parametrize("mode", ["text", "both"])
@pytest.mark.parametrize("style", DENSE_STYLES)
def test_a_page_with_a_short_first_line_and_no_other_break_is_cut_mid_line(mode, style):
    """The cut used to back up to the line break after "INVOICE" and score every
    value from that one word."""
    from idp_common.decision_model import (
        DEFAULT_MAX_INPUT_TOKENS,
        IMAGE_TOKENS,
        estimate_tokens,
    )
    from idp_common.decision_model.budget import LINE_BREAK_BACKOFF_SHARE

    page = "INVOICE\n" + dense_page(style, 1).replace("\n", " ")
    decider = ScriptedDecider()
    _score(
        _service(input=mode, max_concurrency=1),
        decider,
        page_texts=[page],
        page_images=_pages(1) if mode == "both" else (),
    )

    (request,) = decider.requests
    state = request["state"]
    assert page.startswith(state)
    budget = (
        DEFAULT_MAX_INPUT_TOKENS
        - _longest_question(request)
        - IMAGE_TOKENS * len(request["images"])
    )
    assert estimate_tokens(state) >= budget * (1 - LINE_BREAK_BACKOFF_SHARE) - 2


def test_an_images_request_whose_question_overflows_is_refused_locally():
    """input images cuts no text, so nothing else checks the request's size before
    it is sent: a question longer than the limit is refused here, not sent."""
    from idp_common.decision_model import (
        DEFAULT_MAX_INPUT_TOKENS,
        IMAGE_TOKENS,
        estimate_tokens,
    )

    notes = "remark " * 1000
    assert estimate_tokens(notes) > DEFAULT_MAX_INPUT_TOKENS - IMAGE_TOKENS
    decider = ScriptedDecider()
    with pytest.raises(DecisionModelRefused, match="question and 1 image"):
        _score(
            _service(input="images"),
            decider,
            page_texts=["Total due 100.00"],
            page_images=_pages(1),
            extracted={"Total": "100.00", "Notes": notes},
        )
    assert decider.requests == []


def test_max_input_tokens_is_capped_at_the_window_less_the_reserve(caplog):
    from idp_common.config.models import IDPConfig
    from idp_common.decision_model import DEFAULT_MAX_INPUT_TOKENS

    def confidence(limit):
        return IDPConfig.model_validate(
            {
                "extraction": {
                    "confidence": {"decision_model": {"max_input_tokens": limit}}
                }
            }
        )

    def limit(cfg):
        return cfg.extraction.confidence.decision_model.max_input_tokens

    with caplog.at_level(logging.WARNING):
        assert limit(confidence(DEFAULT_MAX_INPUT_TOKENS)) == DEFAULT_MAX_INPUT_TOKENS
        assert caplog.records == []
        # Capped, not refused, so a configuration saved with a larger value loads.
        assert limit(confidence(DEFAULT_MAX_INPUT_TOKENS + 1)) == (
            DEFAULT_MAX_INPUT_TOKENS
        )
    (record,) = caplog.records
    assert "max_input_tokens is 3585" in record.getMessage()
