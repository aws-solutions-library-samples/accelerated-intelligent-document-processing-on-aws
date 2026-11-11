# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Prompt-template comments ({# ... #}) never reach a model (#1653)."""

import copy
import io
from unittest.mock import MagicMock, patch

import pytest
import yaml

from idp_common.bedrock.client import BedrockClient
from idp_common.classification.service import ClassificationService
from idp_common.config import ConfigurationReader
from idp_common.config.merge_utils import (
    get_system_defaults_dir,
    load_system_defaults,
    validate_config,
)
from idp_common.config.models import IDPConfig
from idp_common.prompt_comments import (
    has_prompt_comments,
    is_prompt_key,
    strip_prompt_comments,
    strip_prompt_comments_in_config,
)

pytestmark = pytest.mark.unit

CLIENT = BedrockClient(region="us-west-2")


class TestStripPromptComments:
    def test_a_template_without_comments_is_returned_unchanged(self):
        template = "Classify {DOCUMENT_TEXT}.\n\n  Keep 90% of <<CACHEPOINT>> {x}"
        assert strip_prompt_comments(template) is template

    def test_an_inline_comment_is_removed(self):
        assert strip_prompt_comments("Classify {# note #}the page") == (
            "Classify the page"
        )

    def test_a_comment_line_is_removed_with_its_line_break(self):
        template = "{# must return JSON #}\nClassify.\n  {# indented #}  \nDone"
        assert strip_prompt_comments(template) == "Classify.\nDone"

    def test_a_comment_may_span_lines(self):
        template = "{# Contract:\n  returns {DOCUMENT_TEXT}\n  <<CACHEPOINT>> #}\nBody"
        assert strip_prompt_comments(template) == "Body"

    def test_a_trailing_comment_without_a_line_break_is_removed(self):
        assert strip_prompt_comments("Body\n{# end #}") == "Body\n"

    def test_several_comments_are_all_removed(self):
        assert strip_prompt_comments("a{#1#}b{#2#}c") == "abc"

    @pytest.mark.parametrize(
        "template,expected",
        [
            # A comment followed by text on its line, then another comment: the
            # text between them must survive.
            ("{# a #}Keep this{# b #}\nBody", "Keep this\nBody"),
            (
                "{# note #} Classify the page.\n{# x #}\nBody",
                " Classify the page.\nBody",
            ),
            ("Keep {# a #} this #} too\nBody", "Keep  this #} too\nBody"),
            # Several comments alone on one line remove the whole line.
            ("{# a #} {# b #}\nBody", "Body"),
        ],
    )
    def test_a_comment_never_extends_past_its_own_close(self, template, expected):
        assert strip_prompt_comments(template) == expected

    def test_an_unterminated_opener_is_not_a_comment(self):
        template = "Body {# never closed"
        assert strip_prompt_comments(template) == template

    def test_windows_line_breaks(self):
        assert strip_prompt_comments("{# c #}\r\nBody") == "Body"

    def test_non_strings_pass_through(self):
        assert strip_prompt_comments(None) is None


class TestConfigWalk:
    def test_prompt_keys(self):
        for key in (
            "system_prompt",
            "task_prompt",
            "task_prompt_bbox",
            "user_prompt",
            "x-aws-idp-class-prompt",
            "x-aws-idp-extraction-task-prompt",
            "classPrompt",
        ):
            assert is_prompt_key(key), key
        for key in ("custom_prompt_lambda_arn", "description", "model", 3):
            assert not is_prompt_key(key), key

    def test_only_prompt_values_are_stripped_and_the_input_is_untouched(self):
        config = {
            "ocr": {"task_prompt": "{# c #}\nRead {DOCUMENT_IMAGE}"},
            "classes": [
                {
                    "description": "keeps {# this #}",
                    "x-aws-idp-examples": [{"x-aws-idp-class-prompt": "A{# c #}B"}],
                }
            ],
        }
        before = copy.deepcopy(config)
        stripped = strip_prompt_comments_in_config(config)
        assert config == before
        assert stripped["ocr"]["task_prompt"] == "Read {DOCUMENT_IMAGE}"
        assert stripped["classes"][0]["description"] == "keeps {# this #}"
        assert (
            stripped["classes"][0]["x-aws-idp-examples"][0]["x-aws-idp-class-prompt"]
            == "AB"
        )
        assert has_prompt_comments(config) and not has_prompt_comments(stripped)


class TestFormatPrompt:
    def test_comments_are_removed_before_substitution(self):
        rendered = CLIENT.format_prompt(
            "{# Contract: {DOCUMENT_TEXT} #}\nRead {DOCUMENT_TEXT}",
            {"DOCUMENT_TEXT": "page {# text #} from a document"},
        )
        # The template's comment is gone; document text is never touched.
        assert rendered == "Read page {# text #} from a document"

    def test_a_placeholder_only_inside_a_comment_is_missing(self):
        with pytest.raises(ValueError, match="DOCUMENT_TEXT"):
            CLIENT.format_prompt(
                "{# uses {DOCUMENT_TEXT} #}\nNo placeholder here",
                {"DOCUMENT_TEXT": "x"},
                required_placeholders=["DOCUMENT_TEXT"],
            )


class TestRuntimeReader:
    @staticmethod
    def _reader(config: IDPConfig) -> ConfigurationReader:
        reader = ConfigurationReader.__new__(ConfigurationReader)
        reader.manager = MagicMock()
        reader.manager.get_merged_configuration.return_value = config
        return reader

    def test_the_runtime_config_has_no_prompt_comments(self):
        stored = IDPConfig(
            ocr={"task_prompt": "{# must keep #}\nRead {DOCUMENT_IMAGE}"},
            summarization={"system_prompt": "You summarize.{# why #}"},
        )
        reader = self._reader(stored)

        model = reader.get_merged_configuration(as_model=True)
        as_dict = reader.get_merged_configuration(as_model=False)

        assert model.ocr.task_prompt == "Read {DOCUMENT_IMAGE}"
        assert model.summarization.system_prompt == "You summarize."
        assert as_dict["ocr"]["task_prompt"] == "Read {DOCUMENT_IMAGE}"
        # The stored (editable) configuration keeps its comments.
        assert stored.ocr.task_prompt == "{# must keep #}\nRead {DOCUMENT_IMAGE}"

    def test_a_config_without_comments_is_handed_on_as_is(self):
        stored = IDPConfig(ocr={"task_prompt": "Read {DOCUMENT_IMAGE}"})
        assert self._reader(stored).get_merged_configuration(as_model=True) is stored

    def test_rebuilding_the_model_loses_nothing(self):
        # The model form is rebuilt from a stripped dump; on a configuration whose
        # only difference is the comments, the rebuild must be lossless. Covers the
        # shipped defaults, class schemas with examples, and an extra="allow" model
        # (a pipeline hook carrying a field the model does not declare).
        defaults = load_system_defaults("pattern-2")
        defaults["classes"] = [
            {
                "$id": "Invoice",
                "type": "object",
                "description": "An invoice",
                "properties": {"total": {"type": "number", "description": "Due"}},
                "x-aws-idp-examples": [
                    {"x-aws-idp-name": "ex1", "x-aws-idp-class-prompt": "Looks like"}
                ],
            }
        ]
        defaults["extraction"]["postHook"] = [
            {
                "featureId": "demo",
                "arn": "arn:aws:lambda:us-east-1:111122223333:function:x",
                "undeclaredExtra": {"kept": True},
            }
        ]
        config = IDPConfig(**defaults)
        rebuilt = IDPConfig(**config.model_dump(mode="python"))
        assert rebuilt.model_dump() == config.model_dump()

    def test_the_dict_form_is_not_rebuilt_through_the_model(self):
        stored = IDPConfig(ocr={"task_prompt": "{# c #}\nRead {DOCUMENT_IMAGE}"})
        reader = self._reader(stored)
        import idp_common.config as config_module

        with patch.object(config_module, "IDPConfig", side_effect=IDPConfig) as model:
            as_dict = reader.get_merged_configuration(as_model=False)
        model.assert_not_called()
        assert as_dict["ocr"]["task_prompt"] == "Read {DOCUMENT_IMAGE}"
        with patch.object(config_module, "IDPConfig", side_effect=IDPConfig) as model:
            as_model = reader.get_merged_configuration(as_model=True)
        model.assert_called_once()
        assert as_model.ocr.task_prompt == "Read {DOCUMENT_IMAGE}"


class TestShippedDefaults:
    @staticmethod
    def _prompts(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}"
                if is_prompt_key(key) and isinstance(value, str):
                    yield here, value
                else:
                    yield from TestShippedDefaults._prompts(value, here)

    def test_every_default_classification_prompt_documents_its_contract(self):
        raw = yaml.safe_load(
            (get_system_defaults_dir() / "base-classification.yaml").read_text()
        )
        prompts = dict(self._prompts(raw))
        assert prompts, "no default classification prompts found"
        for path, prompt in prompts.items():
            assert prompt.lstrip().startswith("{#"), f"{path} has no contract comment"
            sent = strip_prompt_comments(prompt)
            assert "{#" not in sent and "#}" not in sent, path
            assert sent == sent.lstrip(), f"{path}: stripping left leading space"


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _sent_text(call) -> str:
    """Every piece of text in one captured Bedrock request."""
    kwargs = call.kwargs
    # The classification service passes the system prompt inside its call config.
    system = (kwargs.get("config") or {}).get("system_prompt") or kwargs.get(
        "system_prompt"
    )
    assert system, "no system prompt captured"
    parts = [str(system)]
    for block in kwargs.get("content", []) or []:
        if isinstance(block, dict) and "text" in block:
            parts.append(block["text"])
    return "\n".join(parts)


class TestNoCommentReachesARequest:
    """The acceptance test: classify a page with the shipped defaults, whose
    prompts all carry contract comments, and inspect what is sent."""

    @pytest.mark.parametrize("through_reader", [True, False])
    @pytest.mark.parametrize("confidence", ["off", "topk", "verbalized"])
    def test_classification_request_has_no_comment_text(
        self, through_reader, confidence, caplog
    ):
        defaults = load_system_defaults("pattern-2")
        defaults["classes"] = [
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "invoice",
                "x-aws-idp-document-type": "invoice",
                "type": "object",
                "description": "An invoice",
                "properties": {},
            }
        ]
        defaults["classification"]["model"] = "us.amazon.nova-2-lite-v1:0"
        defaults["classification"]["combined"]["llm"]["confidence"]["mode"] = confidence
        assert has_prompt_comments(defaults)
        config = IDPConfig(**defaults)
        if through_reader:
            reader = ConfigurationReader.__new__(ConfigurationReader)
            reader.manager = MagicMock()
            reader.manager.get_merged_configuration.return_value = config
            config = reader.get_merged_configuration(as_model=True)

        service = ClassificationService(region="us-west-2", config=config)
        reply = '{"class": "invoice", "document_boundary": "start"'
        reply += (
            ', "candidates": [{"class": "invoice", "probability": 0.9}]}'
            if confidence == "topk"
            else ', "confidence": 0.9}'
            if confidence == "verbalized"
            else "}"
        )
        with (
            patch("idp_common.s3.get_text_content", return_value="the page text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model") as invoke,
        ):
            invoke.return_value = {
                "response": {"output": {"message": {"content": [{"text": reply}]}}},
                "metering": {},
            }
            service.classify_page_bedrock(
                page_id="1", text_uri="s3://b/1.txt", image_uri="s3://b/1.jpg"
            )

        assert invoke.called
        # A placeholder left inside a comment would be counted twice and push the
        # service onto its fallback path; the template must reach it comment-free.
        assert "Invalid DOCUMENT_IMAGE placeholder usage" not in caplog.text
        sent = _sent_text(invoke.call_args)
        assert "{#" not in sent and "#}" not in sent
        for phrase in ("Contract:", "Sent as written", "is the count"):
            assert phrase not in sent, phrase
        # Sanity: the real prompt and the page itself were sent.
        assert "the page text" in sent
        assert "<boundary-detection-rules>" in sent


class TestValidation:
    def test_a_placeholder_only_inside_a_comment_fails_validation(self):
        result = validate_config(
            {
                "ocr": {
                    "backend": "bedrock",
                    "task_prompt": "{# needs {DOCUMENT_IMAGE} #}\nRead the page",
                }
            }
        )
        assert any("ocr.task_prompt" in e for e in result["errors"]), result

    def test_a_commented_prompt_with_the_placeholder_passes(self):
        result = validate_config(
            {
                "ocr": {
                    "backend": "bedrock",
                    "task_prompt": "{# needs it #}\nRead {DOCUMENT_IMAGE}",
                }
            }
        )
        assert not any("ocr.task_prompt" in e for e in result["errors"]), result


class TestShippedFallback:
    def test_an_empty_stage_prompt_falls_back_to_the_stripped_shipped_one(self):
        config = IDPConfig(
            classification={
                "model": "us.amazon.nova-2-lite-v1:0",
                "combined": {
                    "granularity": "packet",
                    "packet": {"system_prompt": "", "task_prompt": ""},
                },
            }
        )
        service = ClassificationService(region="us-west-2", config=config)
        resolved = service._get_classification_config()
        for key in ("system_prompt", "task_prompt"):
            assert resolved[key], key
            assert "{#" not in resolved[key] and "Contract:" not in resolved[key], key
        assert "segments" in resolved["task_prompt"]


class TestConfidenceBlockComments:
    def test_a_marker_named_in_a_block_comment_does_not_split_the_prompt(self):
        """An operator's comment in a confidence block may name a placeholder the
        service splits on; it must be gone before the block is spliced in."""
        block = (
            "{# Spliced before {DOCUMENT_IMAGE}; keep {TOP_K_CANDIDATES}. #}\n"
            '<class-confidence> Add "candidates" ({TOP_K_CANDIDATES}). '
            "</class-confidence>"
        )
        config = IDPConfig(
            classification={
                "model": "us.amazon.nova-2-lite-v1:0",
                "combined": {
                    "page": {
                        "system_prompt": "You classify.",
                        "task_prompt": "Types {CLASS_NAMES_AND_DESCRIPTIONS}\n"
                        "<<CACHEPOINT>>\n{DOCUMENT_TEXT}\n{DOCUMENT_IMAGE}",
                    },
                    "llm": {"confidence": {"mode": "topk", "task_prompt_topk": block}},
                },
            }
        )
        service = ClassificationService(region="us-west-2", config=config)
        task_prompt = service._get_classification_config()["task_prompt"]
        assert "{#" not in task_prompt and "Spliced before" not in task_prompt
        assert task_prompt.count("{DOCUMENT_IMAGE}") == 1
        assert "<class-confidence>" in task_prompt


class TestFewShotExamplePrompts:
    def test_a_config_handed_directly_sends_no_example_comments(self):
        from idp_common.utils.few_shot_example_builder import (
            build_few_shot_examples_content,
            build_few_shot_extraction_examples_content,
        )

        cls = {
            "$id": "Invoice",
            "x-aws-idp-document-type": "Invoice",
            "type": "object",
            "properties": {},
            "x-aws-idp-examples": [
                {
                    "x-aws-idp-name": "ex1",
                    "x-aws-idp-class-prompt": "{# why #}\nThis is an invoice.",
                    "x-aws-idp-attributes-prompt": "Total{# note #}: 12.00",
                }
            ],
        }
        config = IDPConfig(classes=[cls])
        texts = [
            c["text"] for c in build_few_shot_examples_content(config) if "text" in c
        ]
        texts += [
            c["text"]
            for c in build_few_shot_extraction_examples_content(cls)
            if "text" in c
        ]
        assert "This is an invoice." in texts and "Total: 12.00" in texts
        assert not any("{#" in t for t in texts)


class TestServicesHandedAConfigDirectly:
    """Extraction and assessment split their prompts on {DOCUMENT_IMAGE} (and
    {FEW_SHOT_EXAMPLES}) before format_prompt runs, so a config handed straight to
    the service, not through ConfigurationReader, must lose its comments at the
    service itself."""

    @staticmethod
    def _config(**over):
        cls = {
            "$id": "Invoice",
            "x-aws-idp-document-type": "Invoice",
            "type": "object",
            "properties": {"total": {"type": "string"}},
            "x-aws-idp-extraction-system-prompt": "{# per-class #}\nYou extract.",
        }
        return IDPConfig(
            classes=[cls],
            extraction={
                "system_prompt": "{# why #}\nYou are an extractor.",
                "task_prompt": "{# splits at {DOCUMENT_IMAGE} #}\n"
                "Extract {DOCUMENT_CLASS}\n{DOCUMENT_TEXT}\n{DOCUMENT_IMAGE}",
                "confidence": {
                    "task_prompt": "{# at {DOCUMENT_IMAGE} #}\nScore {DOCUMENT_IMAGE}"
                },
            },
            **over,
        )

    def test_extraction_service_sends_no_comment(self):
        from idp_common.extraction.prompt_assembly import (
            select_confidence_task_prompt,
            select_extraction_task_prompt,
        )
        from idp_common.extraction.service import ExtractionService

        config = self._config()
        service = ExtractionService(region="us-west-2", config=config)
        ex = service.config.extraction
        task = select_extraction_task_prompt(ex)
        assert task.count("{DOCUMENT_IMAGE}") == 1 and "{#" not in task
        assert "{#" not in ex.system_prompt
        assert "{#" not in select_confidence_task_prompt(ex)
        override = service._get_class_schema("Invoice")
        assert override["x-aws-idp-extraction-system-prompt"] == "You extract."
        # The caller's configuration keeps its comments.
        assert config.extraction.task_prompt.startswith("{#")

    def test_assessment_service_sends_no_comment(self):
        from idp_common.assessment.service import AssessmentService
        from idp_common.extraction.prompt_assembly import (
            select_confidence_task_prompt,
        )

        service = AssessmentService(region="us-west-2", config=self._config())
        # The separate confidence pass reads extraction.confidence.task_prompt.
        prompt = select_confidence_task_prompt(service.config.extraction)
        assert prompt == "Score {DOCUMENT_IMAGE}"

    def test_a_config_without_comments_is_kept_as_the_same_object(self):
        from idp_common.assessment.service import AssessmentService
        from idp_common.extraction.service import ExtractionService

        config = IDPConfig(extraction={"task_prompt": "Extract {DOCUMENT_TEXT}"})
        assert ExtractionService(region="us-west-2", config=config).config is config
        assert AssessmentService(region="us-west-2", config=config).config is config
