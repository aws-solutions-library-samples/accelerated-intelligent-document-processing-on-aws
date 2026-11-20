# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Class confidence must be in the output-format example, and its absence reported (#2039).

With packet granularity and ``confidence.mode: topk``, Claude Sonnet 5.5 returned
every segment with exactly ``ordinal_start_page``, ``ordinal_end_page``, ``type``
and ``reason`` — no ``candidates`` — so every section was stored unscored. The
``<class-confidence>`` block asks for the key in prose, but the shipped prompt
also carries a JSON example in ``<output-format>`` and ends by asking for "the
exact JSON format specified in <output-format>", and that model followed the
example. Two halves:

- a SHIPPED output-format example gains the key the configured mode asks for,
  in every prompt the class-confidence block is spliced into, and only then;
- a reply that still carries no usable confidence when one was requested is
  recorded as one warning-level processing issue listing the sections, rather
  than leaving the gap silent.

The replies below are shaped like the captured Sonnet 5.5 reply (segments with
those four keys and nothing else) and like a compliant one.
"""

from __future__ import annotations

import io
import json
import re
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from idp_common.classification.class_confidence import (
    CONFIDENCE_KEY_EXAMPLES,
    add_confidence_key_to_output_format,
)
from idp_common.classification.service import (
    STAGE_CLASSIFICATION,
    STAGE_SECTION_CLASSIFICATION,
    ClassificationService,
    classification_task_prompt_as_sent,
    shipped_classification_prompt,
)
from idp_common.config.merge_utils import load_system_defaults
from idp_common.config.models import IDPConfig
from idp_common.models import Document, Page

CODE = "classification_confidence_missing"
CLASS_NAMES = ["Payslip", "W2", "Bank-checks"]


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _classes() -> List[Dict[str, Any]]:
    return [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": name,
            "x-aws-idp-document-type": name,
            "type": "object",
            "description": f"A {name}",
            "properties": {},
        }
        for name in CLASS_NAMES
    ]


def _config(
    *,
    granularity: str = "packet",
    strategy: str = "combined",
    mode: str = "topk",
) -> Dict[str, Any]:
    """The shipped defaults, so the prompts are the ones a deployment sends."""
    config = load_system_defaults()
    config["classes"] = _classes()
    classification = config["classification"]
    classification["strategy"] = strategy
    classification["combined"]["granularity"] = granularity
    classification["combined"]["packet"]["window_pages"] = "0"
    classification["combined"]["llm"]["confidence"]["mode"] = mode
    stc = classification["split_then_classify"]
    stc["splitting"]["llm"]["granularity"] = "packet"
    stc["section_classification"]["llm"]["confidence"]["mode"] = mode
    return config


def _service(**kwargs: Any) -> ClassificationService:
    return ClassificationService(region="us-west-2", config=_config(**kwargs))


def _document(page_count: int) -> Document:
    return Document(
        id="doc-1",
        input_key="doc.pdf",
        pages={
            str(i): Page(
                page_id=str(i),
                image_uri=f"s3://b/{i}.jpg",
                parsed_text_uri=f"s3://b/{i}.txt",
            )
            for i in range(1, page_count + 1)
        },
    )


def _reply(payload: Any) -> Dict[str, Any]:
    return {
        "response": {
            "output": {"message": {"content": [{"text": json.dumps(payload)}]}}
        },
        "metering": {"Classification/bedrock/model": {"inputTokens": 100}},
    }


def _segment(page: int, doc_type: str, scored: bool) -> Dict[str, Any]:
    """One segment: the captured Sonnet 5.5 shape, or that plus candidates."""
    segment: Dict[str, Any] = {
        "ordinal_start_page": page,
        "ordinal_end_page": page,
        "type": doc_type,
        "reason": f"Page {page} carries the opening block of a {doc_type}.",
    }
    if scored:
        segment["candidates"] = [
            {"class": doc_type, "probability": 0.9},
            {
                "class": next(c for c in CLASS_NAMES if c != doc_type),
                "probability": 0.05,
            },
        ]
    return segment


def _issues(document: Document) -> List[Any]:
    return [
        pi
        for section in document.sections
        for pi in (section.processing_issues or [])
        if pi.code == CODE
    ]


def _output_format(prompt: str) -> str:
    match = re.search(r"<output-format>(.*?)</output-format>", prompt, re.S)
    assert match, "the prompt has no <output-format> block"
    return match.group(1)


def _sent_prompt(content: List[Dict[str, Any]]) -> str:
    return "".join(
        part["text"]
        for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------

PROMPT_CASES = [
    # (granularity, stage, shipped prompt path, key goes inside a segment)
    ("packet", STAGE_CLASSIFICATION, ("combined", "packet", "task_prompt"), True),
    ("page", STAGE_CLASSIFICATION, ("combined", "page", "task_prompt"), False),
    (
        "page",
        STAGE_SECTION_CLASSIFICATION,
        ("split_then_classify", "section_classification", "llm", "task_prompt"),
        False,
    ),
]


def _as_sent(granularity: str, stage: str, path: tuple, mode: str) -> str:
    classification = IDPConfig.model_validate(
        _config(granularity=granularity, mode=mode)
    ).classification
    return classification_task_prompt_as_sent(
        shipped_classification_prompt(*path), classification, stage, len(CLASS_NAMES)
    )


@pytest.mark.unit
class TestTheOutputFormatExampleCarriesTheKey:
    @pytest.mark.parametrize("granularity,stage,path,per_segment", PROMPT_CASES)
    @pytest.mark.parametrize(
        "mode,key", [("topk", '"candidates"'), ("verbalized", '"confidence"')]
    )
    def test_the_shipped_example_names_the_key_when_confidence_is_on(
        self, granularity, stage, path, per_segment, mode, key
    ):
        body = _output_format(_as_sent(granularity, stage, path, mode))
        assert body.count(key) == 1
        assert CONFIDENCE_KEY_EXAMPLES[mode] in body
        if per_segment:
            # Inside the segment object, not beside the segments array: the
            # parser reads it per segment.
            segment_object = body[body.index("[") : body.rindex("]")]
            assert key in segment_object
        # Still valid JSON-shaped text: the key follows a comma after the
        # previous value, and the object still closes.
        assert re.search(r'",\s*\n\s*' + re.escape(key), body)

    @pytest.mark.parametrize("granularity,stage,path,per_segment", PROMPT_CASES)
    def test_the_example_is_unchanged_when_confidence_is_off(
        self, granularity, stage, path, per_segment
    ):
        sent = _as_sent(granularity, stage, path, "off")
        shipped = shipped_classification_prompt(*path)
        assert _output_format(sent) == _output_format(shipped)
        assert '"candidates"' not in _output_format(sent)
        assert '"confidence"' not in _output_format(sent)

    def test_the_splitting_call_is_never_given_the_key(self):
        """It reports no class, so there is nothing for a score to describe."""
        from idp_common.classification.service import STAGE_SPLITTING

        classification = IDPConfig.model_validate(
            _config(strategy="split_then_classify")
        ).classification
        path = ("split_then_classify", "splitting", "llm", "packet", "task_prompt")
        shipped = shipped_classification_prompt(*path)
        assert shipped, "the shipped splitting prompt moved; update the path"
        sent = classification_task_prompt_as_sent(
            shipped, classification, STAGE_SPLITTING, len(CLASS_NAMES)
        )
        assert '"candidates"' not in sent

    def test_a_custom_prompt_that_kept_the_shipped_block_gets_the_key(self):
        shipped = shipped_classification_prompt("combined", "packet", "task_prompt")
        block = shipped[shipped.index("<output-format>") :]
        block = block[: block.index("</output-format>") + len("</output-format>")]
        # Reflowed whitespace is still the shipped block.
        custom = (
            "<task-description> My own packet task. </task-description>\n"
            + re.sub(r"\n\s*", "\n", block)
            + "\n<document-text> {DOCUMENT_TEXT} </document-text>"
        )
        result = add_confidence_key_to_output_format(
            custom, shipped, "topk", per_segment=True
        )
        assert '"candidates"' in _output_format(result)
        assert result.startswith("<task-description> My own packet task.")

    def test_an_output_format_the_operator_wrote_is_left_alone(self):
        shipped = shipped_classification_prompt("combined", "packet", "task_prompt")
        custom = (
            '<output-format> {"segments": [{"ordinal_start_page": 1, '
            '"ordinal_end_page": 2, "type": "X", "why": "..."}]} </output-format>'
        )
        assert (
            add_confidence_key_to_output_format(custom, shipped, "topk", True) == custom
        )

    def test_adding_the_key_is_idempotent(self):
        shipped = shipped_classification_prompt("combined", "packet", "task_prompt")
        once = add_confidence_key_to_output_format(shipped, shipped, "topk", True)
        assert once != shipped
        assert add_confidence_key_to_output_format(once, shipped, "topk", True) == once

    def test_the_service_sends_the_key_in_the_packet_call(self):
        """End to end: the content the model receives carries it."""
        service = _service()
        sent: List[str] = []

        def invoke(content, config):
            sent.append(_sent_prompt(content))
            return _reply({"segments": [_segment(1, "Payslip", scored=True)]})

        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=invoke),
        ):
            service.classify_document(_document(1))
        (prompt,) = sent
        assert CONFIDENCE_KEY_EXAMPLES["topk"] in _output_format(prompt)


# --------------------------------------------------------------------------
# A reply with no confidence is reported
# --------------------------------------------------------------------------


def _classify(
    service: ClassificationService, document: Document, replies: List[Any]
) -> Document:
    with (
        patch("idp_common.s3.get_text_content", return_value="page text"),
        patch("idp_common.image.prepare_image", return_value=_png()),
        patch.object(service, "_invoke_bedrock_model", side_effect=replies),
    ):
        return service.classify_document(document)


PACKET_TYPES = ["Payslip", "W2", "Bank-checks"]


@pytest.mark.unit
class TestPacketRepliesWithoutConfidence:
    def test_the_captured_shape_records_one_warning_listing_every_section(self):
        replies = [
            _reply(
                {
                    "segments": [
                        _segment(i, t, scored=False)
                        for i, t in enumerate(PACKET_TYPES, start=1)
                    ]
                }
            )
        ]
        result = _classify(_service(), _document(3), replies)

        assert [s.confidence for s in result.sections] == [None, None, None]
        (issue,) = _issues(result)
        assert issue.severity == "warning"
        assert issue.stage == "classification"
        assert issue.section_id == result.sections[0].section_id
        assert issue.details == {
            "section_ids": [s.section_id for s in result.sections],
            "section_count": 3,
            "page_ids": ["1", "2", "3"],
            "page_count": 3,
        }
        for section in result.sections:
            assert f"{section.section_id} ({section.classification})" in issue.message

    def test_a_compliant_reply_records_nothing(self):
        replies = [
            _reply(
                {
                    "segments": [
                        _segment(i, t, scored=True)
                        for i, t in enumerate(PACKET_TYPES, start=1)
                    ]
                }
            )
        ]
        result = _classify(_service(), _document(3), replies)
        assert [s.confidence for s in result.sections] == [0.9, 0.9, 0.9]
        assert _issues(result) == []

    def test_only_the_unscored_sections_are_listed(self):
        replies = [
            _reply(
                {
                    "segments": [
                        _segment(1, "Payslip", scored=True),
                        _segment(2, "W2", scored=False),
                        _segment(3, "Bank-checks", scored=True),
                    ]
                }
            )
        ]
        result = _classify(_service(), _document(3), replies)
        (issue,) = _issues(result)
        assert issue.section_id == result.sections[1].section_id
        assert issue.details["section_ids"] == [result.sections[1].section_id]
        assert issue.details["page_ids"] == ["2"]

    def test_nothing_is_recorded_when_confidence_was_not_requested(self):
        replies = [_reply({"segments": [_segment(1, "Payslip", scored=False)]})]
        result = _classify(_service(mode="off"), _document(1), replies)
        assert result.sections[0].confidence is None
        assert _issues(result) == []

    def test_a_fallback_class_is_not_reported_as_the_models_unscored_answer(self):
        """The fallback is ours, and is unscored on purpose."""
        bad = _reply({"segments": [_segment(1, "NotAClass", scored=False)]})
        config = _config()
        config["classification"]["fallback_class"] = "Bank-checks"
        service = ClassificationService(region="us-west-2", config=config)
        result = _classify(service, _document(1), [bad, bad, bad])
        assert result.sections[0].classification == "Bank-checks"
        assert result.sections[0].classification != "NotAClass"
        assert _issues(result) == []

    def test_reclassifying_replaces_rather_than_duplicates_the_issue(self):
        service = _service()
        document = _document(1)
        reply = _reply({"segments": [_segment(1, "Payslip", scored=False)]})
        first = _classify(service, document, [reply])
        assert len(_issues(first)) == 1
        ClassificationService._record_missing_confidence_issue(first, {"1": "Payslip"})
        assert len(_issues(first)) == 1


@pytest.mark.unit
class TestPageRepliesWithoutConfidence:
    @staticmethod
    def _page_reply(doc_type: str, boundary: str, scored: bool) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "classification_reason": "evidence",
            "class": doc_type,
            "document_boundary": boundary,
        }
        if scored:
            payload["candidates"] = [{"class": doc_type, "probability": 0.8}]
        return _reply(payload)

    def _run(self, scored: List[bool], mode: str = "topk") -> Document:
        service = _service(granularity="page", mode=mode)
        service.max_workers = 1  # replies are consumed in page order
        replies = [
            self._page_reply(t, "start", s)
            for t, s in zip(["Payslip", "W2"], scored, strict=True)
        ]
        return _classify(service, _document(2), replies)

    def test_an_unscored_page_is_reported_on_its_section(self):
        result = self._run([True, False])
        assert [s.confidence for s in result.sections] == [0.8, None]
        (issue,) = _issues(result)
        assert issue.section_id == result.sections[1].section_id
        assert issue.details["page_ids"] == ["2"]

    def test_scored_pages_record_nothing(self):
        assert _issues(self._run([True, True])) == []

    def test_nothing_is_recorded_with_confidence_off(self):
        assert _issues(self._run([False, False], mode="off")) == []


@pytest.mark.unit
class TestSectionClassificationRepliesWithoutConfidence:
    def _run(self, scored: bool) -> Document:
        service = _service(strategy="split_then_classify")
        payload: Dict[str, Any] = {"classification_reason": "x", "class": "W2"}
        if scored:
            payload["candidates"] = [{"class": "W2", "probability": 0.7}]
        replies = [
            _reply({"segments": [{"ordinal_start_page": 1, "ordinal_end_page": 2}]}),
            _reply(payload),
        ]
        return _classify(service, _document(2), replies)

    def test_an_unscored_label_is_reported(self):
        result = self._run(scored=False)
        assert result.sections[0].classification == "W2"
        (issue,) = _issues(result)
        assert issue.details["page_ids"] == ["1", "2"]

    def test_a_scored_label_records_nothing(self):
        result = self._run(scored=True)
        assert result.sections[0].confidence == 0.7
        assert _issues(result) == []


@pytest.mark.unit
class TestUnscoredOnPurposeIsNotReported:
    """A class that is not the model's answer is unscored for its own reason."""

    @staticmethod
    def _regex_service(granularity: str) -> ClassificationService:
        config = _config(granularity=granularity)
        # The document's name asserts the class; the model still runs for the
        # boundaries because the document has more than one page.
        config["classes"][1]["x-aws-idp-document-name-regex"] = r"^doc-"
        return ClassificationService(region="us-west-2", config=config)

    def test_a_document_name_regex_class_on_the_packet_path(self):
        service = self._regex_service("packet")
        reply = _reply(
            {
                "segments": [
                    _segment(1, "Payslip", scored=False),
                    _segment(2, "Payslip", scored=False),
                ]
            }
        )
        result = _classify(service, _document(2), [reply])
        assert {s.classification for s in result.sections} == {"W2"}
        assert _issues(result) == []

    def test_a_document_name_regex_class_on_the_page_path(self):
        """The pinned page is scored 1.0, so its section is not unscored."""
        service = self._regex_service("page")
        service.max_workers = 1
        replies = [
            _reply({"class": "Payslip", "document_boundary": b})
            for b in ("start", "continue")
        ]
        result = _classify(service, _document(2), replies)
        assert {s.classification for s in result.sections} == {"W2"}
        assert _issues(result) == []

    def test_a_label_reply_with_no_class_is_reported_as_failed_only(self):
        service = _service(strategy="split_then_classify")
        replies = [
            _reply({"segments": [{"ordinal_start_page": 1, "ordinal_end_page": 1}]}),
            _reply({"classification_reason": "no idea"}),
        ]
        # With validation off an empty class is kept, so the reply is "answered"
        # with no class rather than retried into the fallback.
        service.config.classification.split_then_classify.section_classification.llm.validation.enforceValidClasses = False
        result = _classify(service, _document(1), replies)
        codes = [pi.code for pi in result.sections[0].processing_issues]
        assert "classification_failed" in codes
        assert CODE not in codes

    def test_a_failed_label_after_page_level_splitting_is_not_reported(self):
        """Page-level splitting builds its segments without a parsed reply, so
        nothing about them says the model left a score out."""
        config = _config(strategy="split_then_classify")
        config["classification"]["split_then_classify"]["splitting"]["llm"][
            "granularity"
        ] = "page"
        service = ClassificationService(region="us-west-2", config=config)
        replies = [
            _reply({"class": "W2", "document_boundary": "start"}),
            ValueError("the labelling call failed"),
        ]
        result = _classify(service, _document(1), replies)
        codes = [pi.code for pi in result.sections[0].processing_issues]
        assert "classification_failed" in codes
        assert CODE not in codes


@pytest.mark.unit
def test_a_segment_records_no_unscored_page_unless_a_reply_said_so():
    from idp_common.classification.models import Segment

    assert Segment(page_ids=["1"]).unscored_reply_pages == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    "prompt,mode,per_segment",
    [
        pytest.param("<output-format> {} </output-format>", "off", False, id="off"),
        pytest.param("", "topk", False, id="empty-prompt"),
        pytest.param("no block here", "topk", False, id="no-block"),
        pytest.param("<output-format> { unclosed", "topk", False, id="no-close-tag"),
        pytest.param(
            '<output-format> {"a": "x", "candidates": []} </output-format>',
            "topk",
            False,
            id="key-already-there",
        ),
        pytest.param(
            '<output-format> {"a": "x"} </output-format>',
            "topk",
            True,
            id="no-segments-array",
        ),
        pytest.param(
            '<output-format> "a": "x" </output-format>', "topk", False, id="no-object"
        ),
        pytest.param("<output-format> } </output-format>", "topk", False, id="empty"),
    ],
)
def test_a_block_the_key_cannot_be_placed_in_is_returned_unchanged(
    prompt, mode, per_segment
):
    """Each is its own shipped block, so only the shape stops the rewrite."""
    assert (
        add_confidence_key_to_output_format(prompt, prompt, mode, per_segment) == prompt
    )


# --------------------------------------------------------------------------
# Unscored by this service's own logic, not by the reply
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestOwnLogicIsNotBlamedOnTheModel:
    def test_a_page_reply_with_no_class_is_not_reported(self):
        """With validation off an empty class is kept as 'unclassified'."""
        config = _config(granularity="page")
        config["classification"]["combined"]["llm"]["validation"][
            "enforceValidClasses"
        ] = False
        service = ClassificationService(region="us-west-2", config=config)
        result = _classify(
            service,
            _document(1),
            [_reply({"classification_reason": "x", "document_boundary": "start"})],
        )
        assert result.sections[0].classification == "unclassified"
        assert _issues(result) == []

    def test_a_page_fallback_class_is_not_reported(self):
        bad = _reply({"class": "NotAClass", "document_boundary": "start"})
        # A configured class as the fallback, so only the fallback rule (not
        # the 'unclassified' one) keeps it out.
        config = _config(granularity="page")
        config["classification"]["fallback_class"] = "Bank-checks"
        service = ClassificationService(region="us-west-2", config=config)
        result = _classify(service, _document(1), [bad, bad, bad])
        assert result.sections[0].classification == "Bank-checks"
        assert _issues(result) == []

    @staticmethod
    def _disabled(granularity: str = "packet", **shape: Any) -> ClassificationService:
        config = _config(granularity=granularity)
        config["classification"]["document_shape"]["sectionSplitting"] = "disabled"
        config["classification"]["document_shape"].update(shape)
        return ClassificationService(region="us-west-2", config=config)

    def test_a_page_relabelled_by_the_page_vote_is_not_reported(self):
        """The model gave page 1 Payslip; the one section is W2 by vote."""
        reply = _reply(
            {
                "segments": [
                    _segment(1, "Payslip", scored=False),
                    {**_segment(2, "W2", scored=True), "ordinal_end_page": 3},
                ]
            }
        )
        result = _classify(self._disabled(), _document(3), [reply])
        assert [s.classification for s in result.sections] == ["W2"]
        assert _issues(result) == []

    def test_an_unscored_page_of_the_winning_class_is_still_reported(self):
        reply = _reply(
            {
                "segments": [
                    _segment(1, "W2", scored=False),
                    {**_segment(2, "W2", scored=True), "ordinal_end_page": 3},
                ]
            }
        )
        result = _classify(self._disabled(), _document(3), [reply])
        (issue,) = _issues(result)
        assert issue.details["page_ids"] == ["1"]

    @pytest.mark.parametrize("granularity", ["page", "packet"])
    def test_a_page_limited_sample_is_not_reported(self, granularity):
        """Every page is unscored by design when the class is extrapolated."""
        service = self._disabled(granularity, maxPagesForClassification="1")
        reply = (
            _reply({"segments": [_segment(1, "W2", scored=False)]})
            if granularity == "packet"
            else _reply({"class": "W2", "document_boundary": "start"})
        )
        result = _classify(service, _document(3), [reply])
        assert len(result.sections[0].page_ids) == 3
        assert _issues(result) == []


@pytest.mark.unit
class TestWhatTheIssueNames:
    def test_a_page_a_later_window_scored_is_not_named(self):
        config = _config()
        config["classification"]["combined"]["packet"].update(
            {"window_pages": "3", "overlap_pages": "1"}
        )
        service = ClassificationService(region="us-west-2", config=config)
        window_1 = _reply(
            {"segments": [{**_segment(1, "W2", scored=False), "ordinal_end_page": 3}]}
        )
        window_2 = _reply(
            {"segments": [{**_segment(1, "W2", scored=True), "ordinal_end_page": 2}]}
        )
        service.max_workers = 1
        result = _classify(service, _document(4), [window_1, window_2])
        assert [s.page_ids for s in result.sections] == [["1", "2", "3", "4"]]
        (issue,) = _issues(result)
        assert issue.details["page_ids"] == ["1", "2", "3"]

    def test_the_issue_bounds_what_it_names(self):
        count = 30
        types = [CLASS_NAMES[i % 2] for i in range(count)]
        reply = _reply(
            {
                "segments": [
                    _segment(i, t, scored=False) for i, t in enumerate(types, start=1)
                ]
            }
        )
        result = _classify(_service(), _document(count), [reply])
        (issue,) = _issues(result)
        assert issue.details["section_count"] == count
        assert len(issue.details["section_ids"]) == count
        assert f"and {count - 20} more" in issue.message
        assert issue.message.count("(") == 21  # 20 named plus "section(s)"


@pytest.mark.unit
class TestPerStageModeAndSplitting:
    def test_section_classification_off_reports_nothing(self):
        """The combined call's mode does not decide for section classification."""
        config = _config(strategy="split_then_classify")
        config["classification"]["split_then_classify"]["section_classification"][
            "llm"
        ]["confidence"]["mode"] = "off"
        service = ClassificationService(region="us-west-2", config=config)
        replies = [
            _reply({"segments": [{"ordinal_start_page": 1, "ordinal_end_page": 1}]}),
            _reply({"classification_reason": "x", "class": "W2"}),
        ]
        assert _issues(_classify(service, _document(1), replies)) == []

    def test_a_failed_label_after_packet_splitting_is_not_reported(self):
        service = _service(strategy="split_then_classify")
        replies = [
            _reply({"segments": [{"ordinal_start_page": 1, "ordinal_end_page": 1}]}),
            ValueError("the labelling call failed"),
        ]
        result = _classify(service, _document(1), replies)
        codes = [pi.code for pi in result.sections[0].processing_issues]
        assert "classification_failed" in codes
        assert CODE not in codes


@pytest.mark.unit
def test_an_operators_own_confidence_block_keeps_the_example_unchanged():
    classification = IDPConfig.model_validate(_config()).classification
    shipped = shipped_classification_prompt("combined", "packet", "task_prompt")
    own = shipped.replace(
        "<<CACHEPOINT>>",
        '<class-confidence> Add a "score" key per segment. </class-confidence>\n'
        "<<CACHEPOINT>>",
        1,
    )
    sent = classification_task_prompt_as_sent(
        own, classification, STAGE_CLASSIFICATION, len(CLASS_NAMES)
    )
    assert _output_format(sent) == _output_format(shipped)


@pytest.mark.unit
class TestRoundTwoGuards:
    def test_a_section_unscored_only_by_a_relabel_is_not_reported(self):
        """Page 1 Payslip (scored), page 2 W2 (unscored), pages 3-4 W2 scored:
        the vote relabels page 1, which alone leaves the one section unscored,
        so a score on page 2 would not have changed the outcome."""
        config = _config()
        config["classification"]["document_shape"]["sectionSplitting"] = "disabled"
        service = ClassificationService(region="us-west-2", config=config)
        reply = _reply(
            {
                "segments": [
                    _segment(1, "Payslip", scored=True),
                    _segment(2, "W2", scored=False),
                    {**_segment(3, "W2", scored=True), "ordinal_end_page": 4},
                ]
            }
        )
        result = _classify(service, _document(4), [reply])
        assert [(s.classification, s.confidence) for s in result.sections] == [
            ("W2", None)
        ]
        assert _issues(result) == []

    def test_a_pinned_class_the_model_also_gave_unscored_is_not_reported(self):
        """Document-name regex pins W2; the model said W2 with no score; the
        pinned page is scored 1.0, so there is nothing to report."""
        config = _config(granularity="page")
        config["classes"][1]["x-aws-idp-document-name-regex"] = r"^doc-"
        service = ClassificationService(region="us-west-2", config=config)
        service.max_workers = 1
        replies = [
            _reply({"class": "W2", "document_boundary": b})
            for b in ("start", "continue")
        ]
        result = _classify(service, _document(2), replies)
        assert [s.confidence for s in result.sections] == [1.0]
        assert _issues(result) == []

    def test_an_unclassified_label_with_validation_off_is_not_reported(self):
        config = _config(strategy="split_then_classify")
        config["classification"]["split_then_classify"]["section_classification"][
            "llm"
        ]["validation"]["enforceValidClasses"] = False
        service = ClassificationService(region="us-west-2", config=config)
        replies = [
            _reply({"segments": [{"ordinal_start_page": 1, "ordinal_end_page": 1}]}),
            _reply({"classification_reason": "x", "class": "unclassified"}),
        ]
        result = _classify(service, _document(1), replies)
        assert result.sections[0].classification == "unclassified"
        assert _issues(result) == []

    def test_the_issue_is_on_the_first_listed_sections_own_list(self):
        reply = _reply(
            {
                "segments": [
                    _segment(1, "Payslip", scored=True),
                    _segment(2, "W2", scored=False),
                    _segment(3, "Bank-checks", scored=False),
                ]
            }
        )
        result = _classify(_service(), _document(3), [reply])
        holders = [
            s.section_id
            for s in result.sections
            if any(pi.code == CODE for pi in s.processing_issues)
        ]
        assert holders == [result.sections[1].section_id]
        (issue,) = _issues(result)
        assert issue.section_id == result.sections[1].section_id

    def test_details_and_root_cause_are_capped(self):
        count = 120
        reply = _reply(
            {
                "segments": [
                    _segment(i, CLASS_NAMES[i % 2], scored=False)
                    for i in range(1, count + 1)
                ]
            }
        )
        result = _classify(_service(), _document(count), [reply])
        (issue,) = _issues(result)
        assert issue.details["section_count"] == count
        assert issue.details["page_count"] == count
        assert len(issue.details["section_ids"]) == 100
        assert len(issue.details["page_ids"]) == 100
        assert f"and {count - 20} more" in issue.root_cause
        named_pages = issue.root_cause.split("pages ", 1)[1].split(", and")[0]
        assert len(named_pages.split(", ")) == 20
