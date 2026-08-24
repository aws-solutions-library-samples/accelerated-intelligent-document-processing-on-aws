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

import pathlib
from unittest.mock import patch

import pytest
import yaml

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


def _assert_placeholder_is_cacheable(prompt: str, label: str) -> None:
    """The placeholder must appear exactly once, ahead of the cache point."""
    assert "{FEW_SHOT_EXAMPLES}" in prompt, label
    # Exactly one occurrence — the builders split on it and silently ignore the
    # placeholder entirely when there are two or more.
    assert prompt.count("{FEW_SHOT_EXAMPLES}") == 1, label
    # Examples are static per class/config, so they belong in the cacheable prefix.
    assert prompt.index("{FEW_SHOT_EXAMPLES}") < prompt.index("<<CACHEPOINT>>"), label


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

        _assert_placeholder_is_cacheable(prompt, prompt_key)

    def test_default_classification_prompt_includes_few_shot_placeholder(self):
        """Classification carries it too — same silent-drop failure otherwise."""
        defaults = load_system_defaults("pattern-2")

        _assert_placeholder_is_cacheable(
            defaults["classification"]["task_prompt"], "classification.task_prompt"
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


@pytest.mark.unit
class TestCanonicalAndLegacyKeys:
    """Both example-field spellings must work, everywhere they are read.

    The examples *container* has always been ``x-aws-idp-examples``, but entry
    fields shipped as legacy camelCase (``classPrompt`` / ``attributesPrompt`` /
    ``imagePath``) while the docs — and the ``X_AWS_IDP_*`` constants in both the
    Python and TypeScript schema-constant modules — described the canonical
    ``x-aws-idp-*`` names. Canonical is now what gets written; legacy stays
    readable so no existing config has to change.
    """

    @staticmethod
    def _class_with(example):
        return {**CLASS_WITHOUT_EXAMPLES, "x-aws-idp-examples": [example]}

    @pytest.mark.parametrize("key", ["x-aws-idp-attributes-prompt", "attributesPrompt"])
    def test_extraction_reads_either_spelling(self, key):
        content = build_few_shot_extraction_examples_content(
            self._class_with({"name": "e", key: "body"})
        )
        assert content == [{"text": "body"}]

    @pytest.mark.parametrize("key", ["x-aws-idp-class-prompt", "classPrompt"])
    def test_classification_reads_either_spelling(self, key):
        from idp_common.config.models import IDPConfig
        from idp_common.utils.few_shot_example_builder import (
            build_few_shot_examples_content,
        )

        config = IDPConfig.model_validate(
            {"classes": [self._class_with({"name": "e", key: "body"})]}
        )
        assert build_few_shot_examples_content(config) == [{"text": "body"}]

    def test_canonical_wins_when_both_present(self):
        content = build_few_shot_extraction_examples_content(
            self._class_with(
                {
                    "name": "e",
                    "x-aws-idp-attributes-prompt": "canonical",
                    "attributesPrompt": "legacy",
                }
            )
        )
        assert content == [{"text": "canonical"}]

    def test_migration_normalizes_example_field_keys(self):
        """Legacy entry fields are converted when a config is migrated."""
        from idp_common.config.migration import migrate_legacy_to_schema

        migrated = migrate_legacy_to_schema(
            [
                {
                    "name": "letter",
                    "description": "A letter",
                    "attributes": [{"name": "sender", "description": "Sender"}],
                    "examples": [
                        {
                            "name": "letter1",
                            "classPrompt": "class body",
                            "attributesPrompt": "attrs body",
                            "imagePath": "examples/letter1.jpg",
                        }
                    ],
                }
            ]
        )

        example = migrated[0]["x-aws-idp-examples"][0]
        assert example == {
            "name": "letter1",
            "x-aws-idp-class-prompt": "class body",
            "x-aws-idp-attributes-prompt": "attrs body",
            "x-aws-idp-image-path": "examples/letter1.jpg",
        }

    def test_migration_leaves_canonical_examples_alone(self):
        from idp_common.config.migration import _migrate_examples

        canonical = [{"name": "e", "x-aws-idp-class-prompt": "body"}]
        assert _migrate_examples(canonical) == canonical
        # Malformed input must not break migration.
        assert _migrate_examples("not-a-list") == "not-a-list"
        assert _migrate_examples([None, 7]) == [None, 7]


@pytest.mark.unit
class TestShippedConfigsUseCanonicalKeys:
    """Shipped configs must not reintroduce the legacy spelling."""

    # lib/idp_common_pkg/tests/unit/extraction/<this file> -> repo root
    REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]

    @classmethod
    def _shipped_examples(cls):
        """Yield ``(relative_path, example_dict)`` for every shipped example."""
        for path in sorted(cls.REPO_ROOT.glob("config_library/**/config*.yaml")):
            try:
                config = yaml.safe_load(path.read_text()) or {}
            except yaml.YAMLError:
                continue
            if not isinstance(config, dict):
                continue
            for doc_class in config.get("classes") or []:
                if not isinstance(doc_class, dict):
                    continue
                for example in doc_class.get("x-aws-idp-examples") or []:
                    if isinstance(example, dict):
                        yield path.relative_to(cls.REPO_ROOT), example

    def test_scan_actually_finds_shipped_examples(self):
        """Guard the guard: a mis-resolved repo root would pass vacuously.

        This assertion exists because the first version of the test below used
        ``parents[4]`` (= ``lib/``), so its glob matched nothing and it reported
        success without inspecting a single config.
        """
        assert list(self._shipped_examples()), (
            f"no shipped few-shot examples found under {self.REPO_ROOT}/config_library "
            "— the repo-root resolution is probably wrong"
        )

    def test_config_library_examples_use_canonical_keys(self):
        legacy_keys = {"classPrompt", "attributesPrompt", "imagePath"}
        offenders = [
            f"{rel}: {sorted(legacy_keys & set(example))}"
            for rel, example in self._shipped_examples()
            if legacy_keys & set(example)
        ]

        assert not offenders, (
            "shipped configs should use the canonical x-aws-idp-* example keys: "
            + "; ".join(offenders)
        )
