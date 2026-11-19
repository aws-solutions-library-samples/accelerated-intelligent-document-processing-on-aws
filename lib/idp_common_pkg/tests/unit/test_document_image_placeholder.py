# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A prompt that repeats ``{DOCUMENT_IMAGE}`` is refused everywhere it is used.

The page images go in at the placeholder, so it has to appear exactly once. The
separate confidence pass already refused a repeat. Extraction and classification
logged a warning and carried on: extraction and classification's page prompt then
sent the text without any image, and classification's context-pages and packet
prompts sent the images after the text. Both now refuse the prompt with the
confidence pass's message, through one shared rule
(``idp_common.document_image_placeholder``), and ``validate_config`` refuses the
same prompts before they are used.

The runtime tests go through the public entry points (``process_document_section``,
``classify_document``) with only S3, image loading and the model call patched, so
they cover every builder and fallback between the entry point and the request.
Each refusal case has a single-placeholder control that must reach the model with
its images, so a refusal cannot pass by refusing everything.
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from idp_common.classification.service import ClassificationService
from idp_common.config.merge_utils import validate_config
from idp_common.document_image_placeholder import (
    DocumentImagePlaceholderError,
    check_document_image_placeholder,
)
from idp_common.extraction.service import ExtractionService
from idp_common.models import Document, Page, Section, Status

_IMG = "{DOCUMENT_IMAGE}"

# name: (prompt template, refused). The text and class placeholders are present in
# every case so the only difference between a refused case and its control is the
# image placeholder count.
_EXTRACTION_PROMPTS = {
    "once": (
        "Fields {ATTRIBUTE_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT} " + _IMG,
        False,
    ),
    "twice": (
        "Fields {ATTRIBUTE_NAMES_AND_DESCRIPTIONS} "
        + _IMG
        + " {DOCUMENT_TEXT} "
        + _IMG,
        True,
    ),
    "three times": (_IMG * 3 + " {DOCUMENT_TEXT}", True),
    "once either side of the examples": (
        _IMG + " {FEW_SHOT_EXAMPLES} {DOCUMENT_TEXT} " + _IMG,
        True,
    ),
    "once, after the examples": ("{FEW_SHOT_EXAMPLES} {DOCUMENT_TEXT} " + _IMG, False),
}


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _class(name: str, **extra: Any) -> Dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": name,
        "x-aws-idp-document-type": name,
        "type": "object",
        "description": f"A {name}",
        "properties": {"number": {"type": "string", "description": "The number"}},
        **extra,
    }


def _image_count(content: List[Dict[str, Any]]) -> int:
    return sum(1 for block in content if "image" in block)


@pytest.mark.unit
class TestRule:
    @pytest.mark.parametrize("prompt", ["", "no image here", "a " + _IMG + " b"])
    def test_none_or_one_passes(self, prompt):
        check_document_image_placeholder(prompt, "p")

    @pytest.mark.parametrize("count", [2, 3])
    def test_a_repeat_is_refused_naming_the_prompt_and_the_count(self, count):
        with pytest.raises(DocumentImagePlaceholderError) as raised:
            check_document_image_placeholder(_IMG * count, "the prompt")
        assert str(raised.value).startswith("the prompt: ")
        assert f"found {count} occurrences" in str(raised.value)

    def test_it_is_a_value_error(self):
        """Callers that already handle a prompt ValueError keep handling it."""
        assert issubclass(DocumentImagePlaceholderError, ValueError)


def _run_extraction(task_prompt: str, class_prompt: Optional[str] = None):
    """``process_document_section`` on a one-page section; returns (outcome, invoke)."""
    extra = {"x-aws-idp-extraction-task-prompt": class_prompt} if class_prompt else {}
    service = ExtractionService(
        region="us-west-2",
        config={
            "classes": [_class("invoice", **extra)],
            "extraction": {
                "model": "us.amazon.nova-lite-v1:0",
                "system_prompt": "You extract fields.",
                "task_prompt": task_prompt,
            },
        },
    )
    document = Document(
        id="d",
        input_key="d.pdf",
        input_bucket="in",
        output_bucket="out",
        status=Status.EXTRACTING,
        pages={
            "1": Page(
                page_id="1", image_uri="s3://in/1.jpg", parsed_text_uri="s3://in/1.txt"
            )
        },
    )
    document.sections.append(
        Section(section_id="1", classification="invoice", page_ids=["1"])
    )
    with (
        patch("idp_common.s3.get_text_content", return_value="page text"),
        patch("idp_common.image.prepare_image", return_value=_png()),
        patch("idp_common.bedrock.invoke_model") as invoke,
        patch("idp_common.s3.write_content"),
        patch("idp_common.metrics.put_metric"),
    ):
        invoke.return_value = {
            "response": {
                "output": {"message": {"content": [{"text": '{"number": "1"}'}]}}
            },
            "metering": {},
        }
        try:
            outcome: Any = service.process_document_section(document, "1")
        except DocumentImagePlaceholderError as e:
            outcome = e
    return outcome, invoke


@pytest.mark.unit
class TestExtractionRefuses:
    @pytest.mark.parametrize(
        "prompt,refused",
        list(_EXTRACTION_PROMPTS.values()),
        ids=list(_EXTRACTION_PROMPTS),
    )
    def test_task_prompt(self, prompt, refused):
        outcome, invoke = _run_extraction(prompt)
        if refused:
            assert isinstance(outcome, DocumentImagePlaceholderError)
            assert "extraction task prompt" in str(outcome)
            invoke.assert_not_called()
        else:
            assert not isinstance(outcome, Exception)
            invoke.assert_called_once()
            assert _image_count(invoke.call_args.kwargs["content"]) == 1

    def test_a_class_prompt_override(self):
        """The per-class prompt replaces the configured one, so it is the one judged."""
        good = _EXTRACTION_PROMPTS["once"][0]
        outcome, invoke = _run_extraction(
            good, class_prompt=_EXTRACTION_PROMPTS["twice"][0]
        )
        assert isinstance(outcome, DocumentImagePlaceholderError)
        invoke.assert_not_called()

    def test_the_default_prompt_is_not_sent_instead(self):
        """``_build_extraction_content`` answers an ordinary formatting ValueError by
        sending the default prompt; a repeated placeholder must not take that route."""
        outcome, invoke = _run_extraction(_EXTRACTION_PROMPTS["twice"][0])
        assert isinstance(outcome, DocumentImagePlaceholderError)
        invoke.assert_not_called()


_PAGE = (
    "<types>{CLASS_NAMES_AND_DESCRIPTIONS}</types>\n<examples>{FEW_SHOT_EXAMPLES}</examples>\n"
    "<text>{DOCUMENT_TEXT}</text>\n"
)
_PAGE_REPLY = '{"class": "invoice", "document_boundary": "start"}'
_PACKET_REPLY = json.dumps(
    {"segments": [{"ordinal_start_page": 1, "ordinal_end_page": 2, "type": "invoice"}]}
)

# name: (prompt, refused)
_CLASSIFICATION_PROMPTS = {
    "once": (_PAGE + _IMG, False),
    "twice": (_PAGE + _IMG + "\nagain " + _IMG, True),
    "once either side of the examples": (
        _IMG + _PAGE.replace("{FEW_SHOT_EXAMPLES}", "{FEW_SHOT_EXAMPLES} " + _IMG),
        True,
    ),
}

# name: (classification settings beyond the prompt, model reply, images in a
# successful request)
_CLASSIFICATION_PATHS = {
    "page": ({}, _PAGE_REPLY, 1),
    "context pages": ({"contextPagesCount": 1}, _PAGE_REPLY, 2),
    "packet": ({"granularity": "packet"}, _PACKET_REPLY, 2),
}


def _run_classification(prompt: str, settings: Dict[str, Any], reply: str):
    cfg: Dict[str, Any] = {
        "model": "us.amazon.nova-2-lite-v1:0",
        "system_prompt": "You classify documents.",
        "confidence": {"mode": "off"},
        **settings,
    }
    if settings.get("granularity") == "packet":
        cfg["packet"] = {
            "system_prompt": "You split packets.",
            "task_prompt": prompt.replace("<types>", "Return segments. <types>"),
            "window_pages": "0",
        }
    else:
        cfg["task_prompt"] = prompt
    service = ClassificationService(
        region="us-west-2",
        config={
            "classes": [_class("invoice"), _class("receipt")],
            "classification": cfg,
        },
    )
    document = Document(
        id="d",
        input_key="d.pdf",
        pages={
            str(i): Page(
                page_id=str(i),
                image_uri=f"s3://b/{i}.jpg",
                parsed_text_uri=f"s3://b/{i}.txt",
            )
            for i in (1, 2)
        },
    )
    with (
        patch("idp_common.s3.get_text_content", return_value="page text"),
        patch("idp_common.image.prepare_image", return_value=_png()),
        patch.object(service, "_invoke_bedrock_model") as invoke,
    ):
        invoke.return_value = {
            "response": {"output": {"message": {"content": [{"text": reply}]}}},
            "metering": {},
        }
        outcome = service.classify_document(document)
    return outcome, invoke


@pytest.mark.unit
class TestClassificationRefuses:
    @pytest.mark.parametrize("path", list(_CLASSIFICATION_PATHS))
    @pytest.mark.parametrize(
        "prompt,refused",
        list(_CLASSIFICATION_PROMPTS.values()),
        ids=list(_CLASSIFICATION_PROMPTS),
    )
    def test_task_prompt(self, path, prompt, refused):
        settings, reply, images = _CLASSIFICATION_PATHS[path]
        outcome, invoke = _run_classification(prompt, settings, reply)
        if not refused:
            assert isinstance(outcome, Document)
            assert invoke.called
            for call in invoke.call_args_list:
                assert _image_count(call.kwargs["content"]) == images
            return
        invoke.assert_not_called()
        # The classification Lambda fails the document when it is FAILED (the
        # packet path) or a page failed (the page paths, which record the
        # exception it then re-raises).
        failed_pages = outcome.metadata.get("failed_page_exceptions") or {}
        if path == "packet":
            assert outcome.status == Status.FAILED
        else:
            assert set(failed_pages) == {"1", "2"}
            assert isinstance(
                outcome.metadata.get("primary_exception"),
                DocumentImagePlaceholderError,
            )
        message = " ".join(outcome.errors)
        assert "classification" in message and "found " in message


def _validate(config: Dict[str, Any]) -> List[str]:
    result = validate_config(config)
    errors = [e for e in result["errors"] if "DOCUMENT_IMAGE placeholder" in e]
    if errors:
        assert result["valid"] is False
    return errors


@pytest.mark.unit
class TestValidateConfigRefusesTheSamePrompts:
    """``validate_config`` refuses exactly what the services refuse.

    The cases use the same prompt tables as the runtime tests above, so an
    expectation here is one the runtime tests pin for the services.
    """

    @pytest.mark.parametrize(
        "prompt,refused",
        list(_EXTRACTION_PROMPTS.values()),
        ids=list(_EXTRACTION_PROMPTS),
    )
    def test_extraction_task_prompt(self, prompt, refused):
        errors = _validate(
            {"classes": [_class("invoice")], "extraction": {"task_prompt": prompt}}
        )
        assert bool(errors) is refused, errors
        if refused:
            assert errors[0].startswith("extraction.task_prompt: ")

    def test_a_class_prompt_override(self):
        cls = _class(
            "invoice",
            **{"x-aws-idp-extraction-task-prompt": _EXTRACTION_PROMPTS["twice"][0]},
        )
        errors = _validate({"classes": [cls]})
        assert len(errors) == 1 and "class invoice" in errors[0], errors

    def test_the_integrated_confidence_prompt(self):
        """Simple + integrated confidence sends the 1S-TopK prompt, not task_prompt."""
        errors = _validate(
            {
                "classes": [_class("invoice")],
                "extraction": {
                    "confidence": {"mode": "integrated"},
                    "task_prompt_extraction_with_confidence_topk": _EXTRACTION_PROMPTS[
                        "twice"
                    ][0],
                },
            }
        )
        assert len(errors) == 1 and "integrated-confidence" in errors[0], errors

    @pytest.mark.parametrize(
        "prompt,refused",
        list(_CLASSIFICATION_PROMPTS.values()),
        ids=list(_CLASSIFICATION_PROMPTS),
    )
    def test_classification_page_prompt(self, prompt, refused):
        errors = _validate(
            {
                "classes": [_class("invoice")],
                "classification": {"combined": {"page": {"task_prompt": prompt}}},
            }
        )
        assert bool(errors) is refused, errors
        if refused:
            assert errors[0].startswith("classification.combined.page.task_prompt: ")

    def test_classification_with_images_off_is_not_refused(self):
        """``modality.images: false`` removes every placeholder before the prompt is
        built, so the service sends it; validation must not refuse it either."""
        prompt = _CLASSIFICATION_PROMPTS["twice"][0]
        errors = _validate(
            {
                "classes": [_class("invoice")],
                "classification": {
                    "combined": {
                        "page": {"task_prompt": prompt},
                        "llm": {"modality": {"images": False}},
                    }
                },
            }
        )
        assert errors == []
        outcome, invoke = _run_classification(
            prompt, {"modality": {"images": False}}, _PAGE_REPLY
        )
        assert isinstance(outcome, Document) and invoke.called

    def test_a_repeat_inside_a_comment_is_not_counted(self):
        prompt = _CLASSIFICATION_PROMPTS["once"][0] + "\n{# " + _IMG + " #}"
        errors = _validate(
            {
                "classes": [_class("invoice")],
                "classification": {"combined": {"page": {"task_prompt": prompt}}},
            }
        )
        assert errors == []

    def test_the_shipped_defaults_pass(self):
        assert _validate({"classes": [_class("invoice")]}) == []


def _defaults_cls() -> Dict[str, Any]:
    from idp_common.config.merge_utils import merge_config_with_defaults

    return merge_config_with_defaults({})["classification"]


def _stc_user(splitting_prompt=None, section_prompt=None, splitting_modality=None):
    stc: Dict[str, Any] = {}
    if splitting_prompt is not None:
        stc["splitting"] = {"llm": {"page": {"task_prompt": splitting_prompt}}}
        if splitting_modality is not None:
            stc["splitting"]["llm"]["modality"] = splitting_modality
    if section_prompt is not None:
        stc["section_classification"] = {"llm": {"task_prompt": section_prompt}}
    return {
        "classes": [_class("invoice"), _class("receipt")],
        "classification": {
            "strategy": "split_then_classify",
            "split_then_classify": stc,
        },
    }


def _run_merged_classification(user: Dict[str, Any]):
    """``classify_document`` on the merged configuration, answering each call by stage."""
    from idp_common.config.merge_utils import merge_config_with_defaults

    service = ClassificationService(
        region="us-west-2", config=merge_config_with_defaults(user)
    )
    document = Document(
        id="d",
        input_key="d.pdf",
        pages={
            str(i): Page(
                page_id=str(i),
                image_uri=f"s3://b/{i}.jpg",
                parsed_text_uri=f"s3://b/{i}.txt",
            )
            for i in (1, 2)
        },
    )
    stages: List[str] = []

    def answer(content, config):
        stages.append(config.get("stage"))
        reply = {
            "class": "invoice",
            "document_boundary": "start",
            "segments": [{"ordinal_start_page": 1, "ordinal_end_page": 2}],
        }
        return {
            "response": {
                "output": {"message": {"content": [{"text": json.dumps(reply)}]}}
            },
            "metering": {},
        }

    with (
        patch("idp_common.s3.get_text_content", return_value="page text"),
        patch("idp_common.image.prepare_image", return_value=_png()),
        patch.object(service, "_invoke_bedrock_model", side_effect=answer),
    ):
        try:
            outcome: Any = service.classify_document(document)
        except DocumentImagePlaceholderError as e:
            outcome = e
    return outcome, document, stages


@pytest.mark.unit
class TestSplitThenClassify:
    def test_the_shipped_prompts_classify(self):
        """Control: both calls run and every section gets a class."""
        outcome, _, stages = _run_merged_classification(_stc_user())
        assert isinstance(outcome, Document)
        assert "section_classification" in stages
        assert {s.classification for s in outcome.sections} == {"invoice"}

    def test_a_repeat_in_the_section_classification_prompt_fails_the_document(self):
        """Refused once before the per-segment calls. Inside them the refusal was
        recorded as each segment's failure, and the document carried on with every
        section ``unclassified``."""
        prompt = _defaults_cls()["split_then_classify"]["section_classification"][
            "llm"
        ]["task_prompt"]
        outcome, document, stages = _run_merged_classification(
            _stc_user(section_prompt=prompt + _IMG)
        )
        assert isinstance(outcome, DocumentImagePlaceholderError)
        assert "section classification task prompt" in str(outcome)
        assert document.status == Status.FAILED
        assert "section_classification" not in stages
        assert _validate(_stc_user(section_prompt=prompt + _IMG))

    def test_a_repeat_in_the_splitting_prompt_fails_the_document(self):
        prompt = _defaults_cls()["split_then_classify"]["splitting"]["llm"]["page"][
            "task_prompt"
        ]
        user = _stc_user(splitting_prompt=prompt + _IMG)
        outcome, document, stages = _run_merged_classification(user)
        assert document.status == Status.FAILED
        assert stages == []
        assert "splitting task prompt" in " ".join(document.errors)
        assert _validate(user)


@pytest.mark.unit
class TestImagesOffJudgesThePromptAsSent:
    """``modality.images: false`` removes the WRAPPED placeholder only, when there is
    one, so bare repeats beside it are still sent and still refused. The validator
    applies the same removal (``strip_placeholder``)."""

    def test_bare_repeats_beside_a_wrapped_placeholder_are_refused_both_sides(self):
        prompt = _defaults_cls()["split_then_classify"]["splitting"]["llm"]["page"][
            "task_prompt"
        ]
        assert "<document-image>" in prompt  # the shape this case is about
        user = _stc_user(
            splitting_prompt=prompt + _IMG + _IMG, splitting_modality={"images": False}
        )
        _, document, stages = _run_merged_classification(user)
        assert document.status == Status.FAILED and stages == []
        assert _validate(user)

    def test_a_wrapped_placeholder_alone_is_removed_and_accepted_both_sides(self):
        prompt = _defaults_cls()["split_then_classify"]["splitting"]["llm"]["page"][
            "task_prompt"
        ]
        user = _stc_user(splitting_prompt=prompt, splitting_modality={"images": False})
        outcome, _, stages = _run_merged_classification(user)
        assert isinstance(outcome, Document) and "splitting" in stages
        assert _validate(user) == []


_DM = {"endpoint": "arn:aws:lambda:us-west-2:111122223333:function:GENAIIDP-dm"}


@pytest.mark.unit
class TestSectionPromptOnlyJudgedWhenSent:
    """Under ``method: decision_model`` the LLM section prompt is sent only for
    ``on_low_confidence: llm``; otherwise neither side refuses it."""

    @staticmethod
    def _user(low_confidence=None):
        prompt = _defaults_cls()["split_then_classify"]["section_classification"][
            "llm"
        ]["task_prompt"]
        section: Dict[str, Any] = {
            "method": "decision_model",
            "decision_model": _DM,
            "llm": {"task_prompt": prompt + _IMG},
        }
        if low_confidence:
            section["low_confidence"] = low_confidence
        user = _stc_user()
        user["classification"]["split_then_classify"]["section_classification"] = (
            section
        )
        return user

    @staticmethod
    def _run(user):
        from idp_common.classification import decision_model_labeler

        def confident(self, document, pages_content, page_ids, label, llm_label):
            return "invoice", 0.99, [], "dm", {}, None

        with patch.object(
            decision_model_labeler.DecisionModelSectionLabeler, "label", confident
        ):
            return _run_merged_classification(user)

    def test_fallback_class_never_sends_it_so_nothing_refuses(self):
        user = self._user()
        outcome, _, _ = self._run(user)
        assert isinstance(outcome, Document)
        assert {s.classification for s in outcome.sections} == {"invoice"}
        assert _validate(user) == []

    def test_llm_fallback_can_send_it_so_both_refuse(self):
        user = self._user({"on_low_confidence": "llm", "min_confidence": 0.5})
        outcome, _, _ = self._run(user)
        assert isinstance(outcome, DocumentImagePlaceholderError)
        assert _validate(user)

    def test_images_off_section_prompt_is_judged_after_the_removal(self):
        """The wrapped placeholder is removed under ``modality.images: false``, so
        the one bare placeholder left is a single one and is sent."""
        prompt = _defaults_cls()["split_then_classify"]["section_classification"][
            "llm"
        ]["task_prompt"]
        assert "<document-image>" in prompt
        user = _stc_user(section_prompt=prompt + _IMG)
        user["classification"]["split_then_classify"]["section_classification"]["llm"][
            "modality"
        ] = {"images": False}
        outcome, _, stages = _run_merged_classification(user)
        assert isinstance(outcome, Document) and "section_classification" in stages
        assert _validate(user) == []
