# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for {FEW_SHOT_EXAMPLES} handling in extraction prompts.

The shipped extraction prompts carry the placeholder so a class that defines
``x-aws-idp-examples`` gets them without also editing the prompt. That makes two
properties load-bearing:

1. With no examples the placeholder is a no-op (nothing added, no error).
2. An example whose ``imagePath`` cannot be read degrades to a text-only example
   instead of failing the document.
"""

from unittest.mock import patch

import pytest

from idp_common.config.merge_utils import load_system_defaults
from idp_common.extraction.service import ExtractionService
from idp_common.utils.few_shot_example_builder import (
    build_few_shot_extraction_examples_content,
)

CLASS_WITHOUT_EXAMPLES = {
    "$id": "invoice",
    "x-aws-idp-document-type": "invoice",
    "type": "object",
    "properties": {"invoice_number": {"type": "string", "description": "Number"}},
}

CLASS_WITH_EXAMPLES = {
    **CLASS_WITHOUT_EXAMPLES,
    "x-aws-idp-examples": [
        {
            "name": "signature-absent",
            "x-aws-idp-attributes-prompt": (
                'expected attributes are: {"SignaturePresent": false}\n'
                "NOTE: small marks/artifacts are NOT signatures."
            ),
        }
    ],
}


def _service_for(config_with, class_schema):
    """An ExtractionService with the per-section prompt context already set."""
    service = ExtractionService(region="us-west-2", config=config_with(class_schema))
    service._document_text = "some text"
    service._class_label = "invoice"
    service._attribute_descriptions = "invoice_number"
    service._class_schema = class_schema
    service._page_images = []
    return service


@pytest.fixture
def config_with():
    """Build a minimal extraction config around the given class schema."""

    def _make(class_schema):
        return {
            "classes": [class_schema],
            "extraction": {
                "model": "us.amazon.nova-lite-v1:0",
                "system_prompt": "You are a document assistant.",
                "task_prompt": (
                    "<attributes> {ATTRIBUTE_NAMES_AND_DESCRIPTIONS} </attributes>\n"
                    "<few-shot-examples> {FEW_SHOT_EXAMPLES} </few-shot-examples>\n"
                    "<document-text> {DOCUMENT_TEXT} </document-text>"
                ),
            },
        }

    return _make


@pytest.mark.unit
class TestShippedPromptsCarryPlaceholder:
    """Every shipped extraction prompt variant offers the placeholder."""

    @pytest.mark.parametrize(
        "prompt_key",
        [
            "task_prompt",
            "task_prompt_extraction_with_confidence",
            "task_prompt_extraction_with_confidence_topk",
        ],
    )
    def test_default_extraction_prompts_include_few_shot_placeholder(self, prompt_key):
        defaults = load_system_defaults("pattern-2")
        prompt = defaults["extraction"][prompt_key]

        assert "{FEW_SHOT_EXAMPLES}" in prompt, prompt_key
        # Exactly one occurrence — the builder splits on it and silently ignores
        # the placeholder entirely when there are two or more.
        assert prompt.count("{FEW_SHOT_EXAMPLES}") == 1, prompt_key
        # Examples are static per class, so they belong in the cacheable prefix.
        assert prompt.index("{FEW_SHOT_EXAMPLES}") < prompt.index("<<CACHEPOINT>>"), (
            prompt_key
        )


@pytest.mark.unit
class TestPlaceholderIsNoOpWithoutExamples:
    def test_no_examples_adds_no_content(self):
        assert build_few_shot_extraction_examples_content(CLASS_WITHOUT_EXAMPLES) == []

    def test_prompt_renders_without_examples(self, config_with):
        service = _service_for(config_with, CLASS_WITHOUT_EXAMPLES)

        content = service._build_prompt_content(
            service.config.extraction.task_prompt, image_content=None
        )

        rendered = "".join(item.get("text", "") for item in content)
        assert "{FEW_SHOT_EXAMPLES}" not in rendered  # placeholder consumed
        assert "some text" in rendered  # text after the placeholder still rendered
        assert not any("image" in item for item in content)

    def test_prompt_includes_examples_when_defined(self, config_with):
        service = _service_for(config_with, CLASS_WITH_EXAMPLES)

        content = service._build_prompt_content(
            service.config.extraction.task_prompt, image_content=None
        )

        rendered = "".join(item.get("text", "") for item in content)
        assert "small marks/artifacts are NOT signatures" in rendered
        assert "some text" in rendered

    def test_prompt_includes_examples_in_legacy_key_form(self, config_with):
        """The UI's schema editor writes camelCase keys; those must work too."""
        legacy_class = {
            **CLASS_WITHOUT_EXAMPLES,
            "x-aws-idp-examples": [
                {
                    "name": "legacy-keys",
                    "attributesPrompt": "legacy example body",
                }
            ],
        }
        service = _service_for(config_with, legacy_class)

        content = service._build_prompt_content(
            service.config.extraction.task_prompt, image_content=None
        )

        rendered = "".join(item.get("text", "") for item in content)
        assert "legacy example body" in rendered


@pytest.mark.unit
class TestUnreadableExampleImageDegrades:
    """An unreachable imagePath must not fail the document.

    Example images are easy to lose (config copied between accounts or
    partitions, bucket deleted, no s3:GetObject), and now that the shipped
    prompts offer the placeholder, a hard failure here would take the whole
    document down.
    """

    CLASS_WITH_BROKEN_IMAGE = {
        **CLASS_WITHOUT_EXAMPLES,
        "x-aws-idp-examples": [
            {
                "name": "broken-image",
                "x-aws-idp-attributes-prompt": "expected attributes are: {}",
                "x-aws-idp-image-path": "s3://bucket-that-does-not-exist/x.png",
            }
        ],
    }

    def test_image_listing_failure_keeps_text_example(self, caplog):
        with patch(
            "idp_common.utils.few_shot_example_builder._get_image_files_from_path",
            side_effect=RuntimeError("AccessDenied"),
        ):
            with caplog.at_level("WARNING"):
                content = build_few_shot_extraction_examples_content(
                    self.CLASS_WITH_BROKEN_IMAGE
                )

        assert content == [{"text": "expected attributes are: {}"}]
        assert "without images" in caplog.text

    def test_image_read_failure_keeps_text_example(self, caplog):
        with (
            patch(
                "idp_common.utils.few_shot_example_builder._get_image_files_from_path",
                return_value=["s3://bucket-that-does-not-exist/x.png"],
            ),
            patch(
                "idp_common.utils.few_shot_example_builder.s3.get_binary_content",
                side_effect=RuntimeError("NoSuchKey"),
            ),
        ):
            with caplog.at_level("WARNING"):
                content = build_few_shot_extraction_examples_content(
                    self.CLASS_WITH_BROKEN_IMAGE
                )

        assert content == [{"text": "expected attributes are: {}"}]
        assert "Failed to load image" in caplog.text
