# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``classification.modality`` — text, images, or both, at either granularity (#1328).

Classification used to tie two independent choices together: how many calls are
made, and what each call sees. Modality is the second one, and it is genuinely
orthogonal — the same two flags apply to a per-page call and a per-packet call,
because the parser does not care which modality produced the answer.

**Both flags are tri-state and default to unset**, meaning *infer from the
prompt's placeholders*. That is not a compatibility shim beside the real
behaviour; it IS the behaviour the service already had, where ``{DOCUMENT_TEXT}``
present meant text was sent and deleting ``{DOCUMENT_IMAGE}`` was the documented
way to get text-only classification. The tests below pin both halves: an
untouched configuration must produce a byte-identical prompt, and an explicit
flag must take over.

Also covers the two defects on the ``contextPagesCount`` path, which was a
separate content builder and had diverged from the main one in two ways that
were both silent: ``{FEW_SHOT_EXAMPLES}`` was never substituted, so every
configured example was dropped and the placeholder reached the model as a
literal string; and page images were attached whether or not the prompt asked
for them, so a text-only prompt still paid for and sent them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from idp_common.classification.service import ClassificationService
from idp_common.config.models import IDPConfig
from idp_common.models import Document, Page

PAGE_PROMPT = (
    "<document-types>\n{CLASS_NAMES_AND_DESCRIPTIONS}\n</document-types>\n"
    "<few-shot-examples>\n{FEW_SHOT_EXAMPLES}\n</few-shot-examples>\n"
    "<document-ocr-data>\n{DOCUMENT_TEXT}\n</document-ocr-data>\n"
    "<document-image>\n{DOCUMENT_IMAGE}\n</document-image>\n"
    "<final-instructions>\nAnswer in JSON.\n</final-instructions>\n"
)


def _classes(*names: str) -> List[Dict[str, Any]]:
    return [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": name,
            "x-aws-idp-document-type": name,
            "type": "object",
            "description": f"A {name}",
            "properties": {},
        }
        for name in names
    ]


def _service(
    *,
    task_prompt: str = PAGE_PROMPT,
    modality: Optional[Dict[str, Any]] = None,
    classes: Optional[List[Dict[str, Any]]] = None,
    **classification: Any,
) -> ClassificationService:
    cfg: Dict[str, Any] = {
        "model": "us.amazon.nova-2-lite-v1:0",
        "system_prompt": "You classify documents.",
        "task_prompt": task_prompt,
        "confidence": {"mode": "off"},
    }
    if modality is not None:
        cfg["modality"] = modality
    cfg.update(classification)
    return ClassificationService(
        region="us-west-2",
        config={
            "classes": classes if classes is not None else _classes("invoice", "receipt"),
            "classification": cfg,
        },
    )


def _png(colour: str = "white") -> bytes:
    """A real 1x1 PNG. ``prepare_bedrock_image_attachment`` sniffs the format
    with Pillow, so placeholder bytes are rejected before the content array is
    built and every assertion about image blocks would be untestable."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _text_of(content: List[Dict[str, Any]]) -> str:
    return "".join(block["text"] for block in content if "text" in block)


def _image_count(content: List[Dict[str, Any]]) -> int:
    return sum(1 for block in content if "image" in block)


# --------------------------------------------------------------------------
# Tri-state parsing
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestTristateParsing:
    @pytest.mark.parametrize("value", ["", None, "   "])
    def test_an_empty_value_means_unset_not_false(self, value):
        """Stored configurations are string-typed, so "" has to mean "not
        configured". Reading it as False would silently switch a modality off."""
        config = IDPConfig(
            **{"classification": {"modality": {"text": value, "images": value}}}
        )
        assert config.classification.modality.text is None
        assert config.classification.modality.images is None

    @pytest.mark.parametrize("value", [True, "true", "True", "yes", "on", "1", 1])
    def test_truthy_spellings_parse_to_true(self, value):
        config = IDPConfig(**{"classification": {"modality": {"images": value}}})
        assert config.classification.modality.images is True

    @pytest.mark.parametrize("value", [False, "false", "False", "no", "off", "0", 0])
    def test_falsy_spellings_parse_to_false(self, value):
        config = IDPConfig(**{"classification": {"modality": {"images": value}}})
        assert config.classification.modality.images is False

    def test_an_unreadable_value_raises_rather_than_defaulting(self):
        """Either default would be wrong and silent: 'infer' and 'send nothing'
        are different behaviours and the operator typed neither."""
        with pytest.raises(Exception, match="infer from the prompt"):
            IDPConfig(**{"classification": {"modality": {"images": "maybe"}}})

    def test_the_default_config_leaves_both_unset(self):
        assert IDPConfig().classification.modality.text is None
        assert IDPConfig().classification.modality.images is None


# --------------------------------------------------------------------------
# Refused combinations
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestRefusedCombinations:
    def test_both_modalities_off_is_refused(self):
        with pytest.raises(Exception, match="nothing to classify"):
            IDPConfig(
                **{
                    "classification": {
                        "modality": {"text": False, "images": False},
                    }
                }
            )

    def test_only_the_explicit_pair_is_refused(self):
        """A prompt with neither placeholder is a long-standing, deliberate way
        to write a classifier that works from the class descriptions alone, so
        the inferred case must not be refused."""
        config = IDPConfig(
            **{"classification": {"task_prompt": "Pick a class.", "modality": {}}}
        )
        assert config.classification.modality.text is None

    def test_ocr_none_with_images_off_is_refused(self):
        """No OCR text is produced and no images are sent, so classification
        would be working from the literal string '[No text content for page N]'."""
        with pytest.raises(Exception, match="no text and no images"):
            IDPConfig(
                **{
                    "ocr": {"backend": "none"},
                    "classification": {"modality": {"images": False}},
                }
            )

    def test_ocr_none_with_images_on_is_allowed(self):
        config = IDPConfig(
            **{
                "ocr": {"backend": "none"},
                "classification": {"modality": {"images": True}},
            }
        )
        assert config.classification.modality.images is True

    def test_a_real_ocr_backend_with_images_off_is_allowed(self):
        config = IDPConfig(
            **{
                "ocr": {"backend": "textract"},
                "classification": {"modality": {"images": False}},
            }
        )
        assert config.classification.modality.images is False


# --------------------------------------------------------------------------
# Resolution and prompt rewriting
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveModality:
    def test_unset_flags_are_inferred_from_the_placeholders(self):
        assert _service()._resolve_modality() == (True, True)

    def test_a_prompt_without_the_image_placeholder_infers_text_only(self):
        service = _service(task_prompt="{DOCUMENT_TEXT}")
        assert service._resolve_modality() == (True, False)

    def test_a_prompt_without_the_text_placeholder_infers_images_only(self):
        service = _service(task_prompt="{DOCUMENT_IMAGE}")
        assert service._resolve_modality() == (False, True)

    def test_an_explicit_flag_overrides_the_inference(self):
        service = _service(modality={"images": False})
        assert service._resolve_modality() == (True, False)

    def test_an_explicit_true_overrides_a_prompt_with_no_placeholder(self):
        service = _service(task_prompt="{DOCUMENT_TEXT}", modality={"images": True})
        assert service._resolve_modality() == (True, True)


@pytest.mark.unit
class TestApplyModalityToPrompt:
    def test_an_unset_modality_returns_the_prompt_untouched(self):
        """The overwhelmingly common path: every configuration written before
        this setting existed must produce a byte-identical prompt."""
        service = _service()
        assert service._apply_modality_to_prompt(PAGE_PROMPT) == PAGE_PROMPT

    def test_an_explicit_flag_that_agrees_with_the_prompt_changes_nothing(self):
        service = _service(modality={"text": True, "images": True})
        assert service._apply_modality_to_prompt(PAGE_PROMPT) == PAGE_PROMPT

    def test_turning_images_off_removes_the_placeholder_and_its_wrapper(self):
        """An empty element announcing a modality that is switched off is a
        misleading thing to leave in a prompt."""
        service = _service(modality={"images": False})
        result = service._apply_modality_to_prompt(PAGE_PROMPT)
        assert "{DOCUMENT_IMAGE}" not in result
        assert "<document-image>" not in result
        # Everything else survives.
        assert "{DOCUMENT_TEXT}" in result
        assert "<final-instructions>" in result

    def test_turning_text_off_removes_the_placeholder_and_its_wrapper(self):
        service = _service(modality={"text": False})
        result = service._apply_modality_to_prompt(PAGE_PROMPT)
        assert "{DOCUMENT_TEXT}" not in result
        assert "<document-ocr-data>" not in result
        assert "{DOCUMENT_IMAGE}" in result

    def test_an_unwrapped_placeholder_is_simply_deleted(self):
        service = _service(
            task_prompt="before {DOCUMENT_IMAGE} after", modality={"images": False}
        )
        assert (
            service._apply_modality_to_prompt("before {DOCUMENT_IMAGE} after")
            == "before  after"
        )

    def test_turning_images_on_appends_a_placeholder_when_there_is_none(self):
        service = _service(task_prompt="{DOCUMENT_TEXT}", modality={"images": True})
        result = service._apply_modality_to_prompt("{DOCUMENT_TEXT}")
        assert "{DOCUMENT_IMAGE}" in result
        assert result.index("{DOCUMENT_TEXT}") < result.index("{DOCUMENT_IMAGE}")

    def test_turning_text_on_appends_a_placeholder_when_there_is_none(self):
        service = _service(task_prompt="{DOCUMENT_IMAGE}", modality={"text": True})
        result = service._apply_modality_to_prompt("{DOCUMENT_IMAGE}")
        assert "{DOCUMENT_TEXT}" in result


# --------------------------------------------------------------------------
# What actually reaches the request
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestRequestContent:
    def test_images_off_sends_no_image_block(self):
        service = _service(modality={"images": False})
        config = service._get_classification_config()
        content = service._build_content(
            config["task_prompt"], "page text", "invoice: An invoice", _png()
        )
        assert _image_count(content) == 0
        assert "page text" in _text_of(content)

    def test_text_off_sends_no_page_text(self):
        service = _service(modality={"text": False})
        config = service._get_classification_config()
        content = service._build_content(
            config["task_prompt"], "page text", "invoice: An invoice", _png()
        )
        assert "page text" not in _text_of(content)
        assert _image_count(content) == 1

    def test_both_on_sends_both(self):
        service = _service()
        config = service._get_classification_config()
        content = service._build_content(
            config["task_prompt"], "page text", "invoice: An invoice", _png()
        )
        assert "page text" in _text_of(content)
        assert _image_count(content) == 1

    def test_the_image_is_not_even_loaded_when_images_are_off(self):
        """Not just discarded — loading one costs an S3 GET and a Pillow resize
        per page, which the content builder would then throw away."""
        from unittest.mock import patch

        service = _service(modality={"images": False})
        with (
            patch("idp_common.s3.get_text_content", return_value="page text"),
            patch("idp_common.image.prepare_image") as prepare,
            patch.object(service, "_invoke_bedrock_model") as invoke,
        ):
            invoke.return_value = {
                "response": {
                    "output": {
                        "message": {
                            "content": [
                                {
                                    "text": '{"class": "invoice", '
                                    '"document_boundary": "start"}'
                                }
                            ]
                        }
                    }
                },
                "metering": {},
            }
            service.classify_page_bedrock(
                page_id="1", text_uri="s3://b/1.txt", image_uri="s3://b/1.jpg"
            )
        prepare.assert_not_called()

    def test_context_pages_do_not_load_images_when_images_are_off(self):
        from unittest.mock import patch

        service = _service(modality={"images": False}, contextPagesCount=1)
        document = Document(
            id="d",
            input_key="d.pdf",
            pages={
                str(i): Page(
                    page_id=str(i),
                    image_uri=f"s3://b/{i}.jpg",
                    parsed_text_uri=f"s3://b/{i}.txt",
                )
                for i in (1, 2, 3)
            },
        )
        with (
            patch("idp_common.s3.get_text_content", return_value="t"),
            patch("idp_common.image.prepare_image") as prepare,
        ):
            cache = service._preload_page_content(document, 1)
        prepare.assert_not_called()
        assert all(entry.image_content is None for entry in cache.values())
        assert all(entry.text_content == "t" for entry in cache.values())


# --------------------------------------------------------------------------
# The two context-path defects
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestContextPathDefects:
    """``contextPagesCount > 0`` took a separate content builder which had
    diverged from the main one in two silent ways."""

    EXAMPLES_CLASSES = [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "invoice",
            "x-aws-idp-document-type": "invoice",
            "type": "object",
            "description": "An invoice",
            "properties": {},
            "x-aws-idp-examples": [
                {
                    "x-aws-idp-name": "example-1",
                    "x-aws-idp-class-prompt": "This is what an invoice looks like.",
                }
            ],
        },
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "receipt",
            "x-aws-idp-document-type": "receipt",
            "type": "object",
            "description": "A receipt",
            "properties": {},
        },
    ]

    def test_the_context_path_substitutes_few_shot_examples(self):
        """The placeholder used to reach the model as the literal eight-word
        string, with every configured example dropped — the exact failure the
        placeholder was put in the default prompt to prevent."""
        service = _service(classes=self.EXAMPLES_CLASSES, contextPagesCount=1)
        content = service._build_context_content(
            task_prompt_template=service.config.classification.task_prompt,
            document_text="current page text",
            image_content=_png(),
            before_images=[_png("red")],
            after_images=[_png("blue")],
        )
        text = _text_of(content)
        assert "{FEW_SHOT_EXAMPLES}" not in text
        assert "This is what an invoice looks like." in text

    def test_the_context_path_omits_images_when_the_prompt_has_no_placeholder(self):
        """A deliberately text-only prompt used to receive the current page's
        image and every context page's image anyway."""
        text_only = PAGE_PROMPT.replace(
            "<document-image>\n{DOCUMENT_IMAGE}\n</document-image>\n", ""
        )
        service = _service(task_prompt=text_only, contextPagesCount=1)
        content = service._build_context_content(
            task_prompt_template=text_only,
            document_text="current page text",
            image_content=_png(),
            before_images=[_png("red"), _png("blue")],
            after_images=[_png("blue")],
        )
        assert _image_count(content) == 0
        assert "current page text" in _text_of(content)

    def test_the_context_path_still_sends_images_when_the_prompt_asks(self):
        service = _service(contextPagesCount=1)
        content = service._build_context_content(
            task_prompt_template=service.config.classification.task_prompt,
            document_text="current page text",
            image_content=_png(),
            before_images=[_png("red")],
            after_images=[_png("blue")],
        )
        # One context page either side plus the page itself.
        assert _image_count(content) == 3

    def test_no_empty_text_block_is_emitted(self):
        """Bedrock rejects an empty text block, and a prompt segment is
        legitimately blank when a placeholder sits at one end of the template."""
        service = _service(task_prompt="{FEW_SHOT_EXAMPLES}{DOCUMENT_TEXT}")
        content = service._build_context_content(
            task_prompt_template="{FEW_SHOT_EXAMPLES}{DOCUMENT_TEXT}",
            document_text="t",
            image_content=None,
            before_images=[],
            after_images=[],
        )
        assert all(block.get("text", "x").strip() for block in content)

    def test_a_repeated_image_placeholder_appends_images_after_the_text(self):
        service = _service(task_prompt="{DOCUMENT_IMAGE} a {DOCUMENT_IMAGE}")
        content = service._build_context_content(
            task_prompt_template="{DOCUMENT_IMAGE} a {DOCUMENT_IMAGE}",
            document_text="t",
            image_content=_png(),
            before_images=[],
            after_images=[],
        )
        assert _image_count(content) == 1
        assert "{DOCUMENT_IMAGE}" not in _text_of(content)
