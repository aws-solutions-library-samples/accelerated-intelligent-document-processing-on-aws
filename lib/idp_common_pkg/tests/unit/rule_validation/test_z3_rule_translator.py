# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for `RuleTranslator`, which turns a natural-language business rule into
the SMT-LIB `RuleJSON` the solver evaluates, and extracts parameter values from a
document when no path mappings exist.

Everything the translator produces is downstream of one model call, so the parsing
and validation around that call is the only thing standing between a hallucinated
response and a rule that is persisted and then applied to every matching document.
Two consequences shape these tests.

**Every rejection is asserted individually.** `_parse_llm_output` checks presence,
then type, then non-emptiness, in three separate passes, and each pass raises a
different message. A rule that reaches `_build_rule_json` with an empty
`constraints` list would construct a `RuleJSON` the solver reports `sat` for
unconditionally — a rule that passes every document. So "empty list is rejected" is
its own case rather than folded into a happy-path assertion.

**The prompt is asserted on content, because nothing downstream checks it.** A
missing `{rule}` substitution or a dropped few-shot block does not fail; it produces
a confident translation of the wrong thing. `_build_prompt` and
`_build_extraction_prompt` are therefore checked for the substrings that must reach
the model.

These tests make no model call and no AWS call. Most of them stub `_invoke_bedrock`
outright and assert that `translate_rule` re-raises a `TranslationError` from it
unchanged and wraps anything else. `TestInvokeBedrockRetryLadder` covers the method
itself against a mocked Bedrock client, with `time.sleep` patched out: which error
codes are retried, which fail immediately, how the backoff progresses and what the
exhaustion path raises. Both directions matter there — a retryable code dropped from the
set turns a transient throttle into a hard failure for every rule, and a non-retryable
one added to it delays the error an operator needs to see.

Two behaviours worth knowing before reading. `_parse_extraction_output` compares every
value against its declared `type` and refuses a lossy reading; the refusal is
**per-attempt rather than per-rule**, because the caller catches `TranslationError` and
retries extraction against the raw document text, which is a second model call. So an
over-strict check here shows up as doubled cost on a correct answer rather than as an
outright failure, and both directions are asserted for that reason. And
`_generate_rule_id` is not unique despite its comment saying so — see
[#1118](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1118).
"""

import io
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from idp_common.rule_validation.z3.config_loader import (
    TranslatorConfig,
    ValueExtractionConfig,
)
from idp_common.rule_validation.z3.exceptions import TranslationError
from idp_common.rule_validation.z3.models import Parameter, RuleJSON
from idp_common.rule_validation.z3.rule_translator import RuleTranslator

MODULE = "idp_common.rule_validation.z3.rule_translator"

TRANSLATOR_TEMPLATE = "Translate this rule: {rule}\nAgainst this data: {data_example}"
EXTRACTION_TEMPLATE = (
    "Rule: {rule_description}\nNL: {natural_language_rule}\n"
    "Params: {parameters_json}\nType: {data_type}\nData: {data}"
)


def _translator_config(**overrides: Any) -> TranslatorConfig:
    kwargs: dict[str, Any] = {
        "model_id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "temperature": 0.0,
        "max_tokens": 4096,
        "system_prompt": "SYSTEM_PROMPT_MARKER",
        "task_prompt_template": TRANSLATOR_TEMPLATE,
    }
    kwargs.update(overrides)
    return TranslatorConfig(**kwargs)


def _extraction_config(**overrides: Any) -> ValueExtractionConfig:
    kwargs: dict[str, Any] = {
        "model_id": "us.amazon.nova-lite-v1:0",
        "temperature": 0.0,
        "max_tokens": 2048,
        "system_prompt": "EXTRACTION_SYSTEM_MARKER",
        "task_prompt_template": EXTRACTION_TEMPLATE,
    }
    kwargs.update(overrides)
    return ValueExtractionConfig(**kwargs)


def _translator(**config_overrides: Any) -> RuleTranslator:
    """A RuleTranslator with pre-built configs and a stubbed Bedrock client."""
    with patch(f"{MODULE}.boto3.client"):
        return RuleTranslator(
            translator_config=_translator_config(**config_overrides),
            extraction_config=_extraction_config(),
        )


def _rule_json(**overrides: Any) -> RuleJSON:
    kwargs: dict[str, Any] = {
        "rule_id": "coverage_ratio",
        "version": "1.0",
        "description": "Coverage must not exceed 20x income",
        "natural_language_rule": "coverage / income <= 20",
        "parameters": [
            Parameter(name="coverage", type="Real"),
            Parameter(name="income", type="Real"),
        ],
        "constraints": ["(<= (/ coverage income) 20)"],
    }
    kwargs.update(overrides)
    return RuleJSON(**kwargs)


VALID_OUTPUT: dict[str, Any] = {
    "parameters": [
        {"name": "coverage", "type": "Real"},
        {"name": "income", "type": "Real"},
    ],
    "path_mappings": [
        {"parameter_name": "coverage", "data_path": "doc.coverage"},
        {"parameter_name": "income", "data_path": "doc.income"},
    ],
    "constraints": ["(<= (/ coverage income) 20)"],
}


@pytest.mark.unit
class TestConstruction:
    """__init__: pre-built configs, the YAML path, and the Bedrock client."""

    def test_prebuilt_configs_are_used_without_touching_the_filesystem(self):
        with (
            patch(f"{MODULE}.boto3.client"),
            patch(f"{MODULE}.ConfigLoader.load") as load,
        ):
            translator = RuleTranslator(
                translator_config=_translator_config(),
                extraction_config=_extraction_config(),
            )
        load.assert_not_called()
        assert translator.translator_config.system_prompt == "SYSTEM_PROMPT_MARKER"

    def test_the_default_config_path_is_used_when_nothing_is_supplied(self):
        with (
            patch(f"{MODULE}.boto3.client"),
            patch(f"{MODULE}.ConfigLoader.load") as load,
            patch(
                f"{MODULE}.ConfigLoader.get_default_config_path",
                return_value="/default.yaml",
            ),
        ):
            load.return_value = MagicMock(
                rule_translator=_translator_config(),
                value_extraction=_extraction_config(),
            )
            RuleTranslator()
        load.assert_called_once_with("/default.yaml")

    def test_an_explicit_config_path_is_used(self):
        with (
            patch(f"{MODULE}.boto3.client"),
            patch(f"{MODULE}.ConfigLoader.load") as load,
        ):
            load.return_value = MagicMock(
                rule_translator=_translator_config(),
                value_extraction=_extraction_config(),
            )
            RuleTranslator(config_path="/custom.yaml")
        load.assert_called_once_with("/custom.yaml")

    def test_only_one_prebuilt_config_falls_back_to_the_yaml_file(self):
        # The guard requires BOTH, so supplying one is not a partial override -- it
        # loads the file and discards the one that was passed. Pinned because a
        # caller supplying only a translator config would silently get the file's.
        with (
            patch(f"{MODULE}.boto3.client"),
            patch(f"{MODULE}.ConfigLoader.load") as load,
        ):
            load.return_value = MagicMock(
                rule_translator=_translator_config(system_prompt="FROM_FILE"),
                value_extraction=_extraction_config(),
            )
            translator = RuleTranslator(translator_config=_translator_config())
        load.assert_called_once()
        assert translator.translator_config.system_prompt == "FROM_FILE"

    def test_a_config_load_failure_becomes_a_translation_error(self):
        with (
            patch(f"{MODULE}.boto3.client"),
            patch(
                f"{MODULE}.ConfigLoader.load", side_effect=FileNotFoundError("absent")
            ),
        ):
            with pytest.raises(TranslationError) as excinfo:
                RuleTranslator(config_path="/absent.yaml")
        assert excinfo.value.operation == "load_config"

    def test_the_region_argument_is_passed_to_the_bedrock_client(self):
        with patch(f"{MODULE}.boto3.client") as client:
            RuleTranslator(
                translator_config=_translator_config(),
                extraction_config=_extraction_config(),
                region="eu-west-1",
            )
        assert client.call_args.kwargs["region_name"] == "eu-west-1"
        assert client.call_args.kwargs["service_name"] == "bedrock-runtime"

    def test_the_environment_region_is_used_when_none_is_given(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
        with patch(f"{MODULE}.boto3.client") as client:
            RuleTranslator(
                translator_config=_translator_config(),
                extraction_config=_extraction_config(),
            )
        assert client.call_args.kwargs["region_name"] == "ap-southeast-2"

    def test_a_default_region_is_used_when_the_environment_is_unset(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        with patch(f"{MODULE}.boto3.client") as client:
            RuleTranslator(
                translator_config=_translator_config(),
                extraction_config=_extraction_config(),
            )
        assert client.call_args.kwargs["region_name"] == "us-east-1"

    def test_a_client_construction_failure_becomes_a_translation_error(self):
        with patch(f"{MODULE}.boto3.client", side_effect=RuntimeError("no creds")):
            with pytest.raises(TranslationError) as excinfo:
                RuleTranslator(
                    translator_config=_translator_config(),
                    extraction_config=_extraction_config(),
                )
        assert excinfo.value.operation == "init_bedrock_client"


@pytest.mark.unit
class TestGenerateRuleId:
    """_generate_rule_id: a stable prefix and a unique suffix."""

    def test_the_id_has_the_documented_prefix(self):
        assert _translator()._generate_rule_id("some rule").startswith("rule_")

    def test_ids_for_the_same_text_collide_within_a_millisecond(self):
        """The id is not unique, despite the code saying it is. See #1118.

        `_generate_rule_id` hashes the rule text plus `int(time.time() * 1000)`, and its
        comment reads "Use hash of rule text + timestamp for uniqueness". Millisecond
        resolution is far coarser than the call rate: 2,000 back-to-back calls on
        identical text produced **3** distinct ids.

        This is asserted in the direction that is TRUE rather than as a strict xfail on
        the direction that is wanted, because "unique" is not in fact the requirement
        anywhere — every consumer traced (`rule_translator.py` lines 164-299) uses the
        id for log and error context and to populate the returned rule object, and
        nothing keys a cache or an S3 object on it. So there is no current defect to
        pin, only a comment that promises a property the code does not have and that a
        future caller could reasonably rely on.

        An earlier version of this test asserted `first != second or len(first) ==
        len(second)`, whose second disjunct is unconditionally true for a `"rule_"`
        prefix plus 8 hex characters. It could not fail, and it stated the opposite of
        what the code does.
        """
        translator = _translator()
        ids = [translator._generate_rule_id("same text") for _ in range(500)]
        assert len(set(ids)) < len(ids), (
            "ids no longer collide, so #1118 may be fixed; if the generator is now "
            "genuinely unique, assert that instead and update the docstring"
        )

    def test_different_text_gives_different_ids(self):
        # The property that does hold and that the id is actually used for: two
        # different rules are distinguishable in logs even within the same millisecond.
        translator = _translator()
        assert translator._generate_rule_id("rule one") != translator._generate_rule_id(
            "rule two"
        )

    def test_the_suffix_is_eight_hex_characters(self):
        suffix = _translator()._generate_rule_id("rule")[len("rule_") :]
        assert len(suffix) == 8
        assert all(c in "0123456789abcdef" for c in suffix)


@pytest.mark.unit
class TestBuildPrompt:
    """_build_prompt: what actually reaches the model."""

    def test_the_rule_and_the_data_example_are_both_substituted(self):
        # A template that fails to substitute produces a prompt asking the model to
        # translate the literal string "{rule}", and nothing downstream notices.
        prompt = _translator()._build_prompt("RULE_MARKER", {"k": "DATA_MARKER"})
        assert "RULE_MARKER" in prompt
        assert "DATA_MARKER" in prompt

    def test_the_system_prompt_comes_first(self):
        prompt = _translator()._build_prompt("rule", {})
        assert prompt.startswith("SYSTEM_PROMPT_MARKER")

    def test_the_data_example_is_rendered_as_indented_json(self):
        prompt = _translator()._build_prompt("rule", {"a": {"b": 1}})
        assert '"a"' in prompt and '"b"' in prompt

    def test_no_few_shot_examples_means_no_examples_section(self):
        prompt = _translator()._build_prompt("rule", {})
        assert "correct translations" not in prompt

    def test_few_shot_examples_are_rendered_with_rule_data_and_output(self):
        examples = [
            {
                "rule": "EXAMPLE_RULE_MARKER",
                "data_example": {"x": "EXAMPLE_DATA_MARKER"},
                "output": {
                    "parameters": [{"name": "x", "type": "Real"}],
                    "path_mappings": [],
                    "constraints": ["(> x 0)"],
                },
            }
        ]
        prompt = _translator(few_shot_examples=examples)._build_prompt("rule", {})
        assert "Example 1:" in prompt
        assert "EXAMPLE_RULE_MARKER" in prompt
        assert "EXAMPLE_DATA_MARKER" in prompt
        assert "(> x 0)" in prompt

    def test_several_examples_are_numbered_in_order(self):
        examples = [
            {
                "rule": f"r{i}",
                "data_example": {},
                "output": {"parameters": [], "path_mappings": [], "constraints": []},
            }
            for i in range(3)
        ]
        prompt = _translator(few_shot_examples=examples)._build_prompt("rule", {})
        assert prompt.index("Example 1:") < prompt.index("Example 2:")
        assert "Example 3:" in prompt

    def test_workflow_b_tells_the_model_not_to_produce_path_mappings(self):
        # Without this instruction the model returns path_mappings the caller then
        # discards, and the prompt has spent tokens on the wrong task.
        prompt = _translator()._build_prompt("rule", {}, extract_paths=False)
        assert "do NOT generate path_mappings" in prompt

    def test_workflow_a_does_not_carry_that_instruction(self):
        prompt = _translator()._build_prompt("rule", {}, extract_paths=True)
        assert "do NOT generate path_mappings" not in prompt

    def test_a_rule_containing_braces_does_not_break_substitution(self):
        # Rule text is user-authored and `.format` is used, so an unescaped brace in
        # the RULE would raise if it were substituted into the template a second
        # time. The rule is a value, not part of the template, so it must be safe.
        prompt = _translator()._build_prompt("if {x} then y", {})
        assert "if {x} then y" in prompt


@pytest.mark.unit
class TestParseLlmOutput:
    """_parse_llm_output: presence, then type, then non-emptiness."""

    def _parse(self, payload: Any, **kwargs: Any):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return _translator()._parse_llm_output(text, "r1", **kwargs)

    def test_a_valid_response_is_returned_parsed(self):
        parsed = self._parse(VALID_OUTPUT)
        assert len(parsed["parameters"]) == 2
        assert parsed["constraints"] == ["(<= (/ coverage income) 20)"]

    def test_a_fenced_json_block_is_unwrapped(self):
        # Models wrap JSON in ```json fences unprompted; failing to strip them makes
        # every translation a parse error.
        parsed = self._parse(f"```json\n{json.dumps(VALID_OUTPUT)}\n```")
        assert len(parsed["parameters"]) == 2

    def test_an_unlabelled_fence_is_unwrapped(self):
        parsed = self._parse(f"```\n{json.dumps(VALID_OUTPUT)}\n```")
        assert len(parsed["parameters"]) == 2

    def test_surrounding_whitespace_is_tolerated(self):
        parsed = self._parse(f"\n\n  {json.dumps(VALID_OUTPUT)}  \n\n")
        assert len(parsed["parameters"]) == 2

    @pytest.mark.parametrize(
        "text", ["not json", "", "{unclosed", "```json\nnope\n```"]
    )
    def test_unparseable_output_raises_with_the_raw_response_attached(self, text):
        # The raw response is the only evidence of what the model actually said, and
        # it is what an operator needs to tell a prompt bug from a model bug.
        with pytest.raises(TranslationError) as excinfo:
            self._parse(text)
        assert excinfo.value.operation == "parse_llm_output"
        assert excinfo.value.rule_id == "r1"

    @pytest.mark.parametrize("field", ["parameters", "path_mappings", "constraints"])
    def test_each_required_field_is_checked_for_workflow_a(self, field):
        payload = {k: v for k, v in VALID_OUTPUT.items() if k != field}
        with pytest.raises(TranslationError) as excinfo:
            self._parse(payload)
        assert field in str(excinfo.value)

    def test_path_mappings_are_not_required_for_workflow_b(self):
        payload = {k: v for k, v in VALID_OUTPUT.items() if k != "path_mappings"}
        parsed = self._parse(payload, extract_paths=False)
        assert parsed["path_mappings"] == []

    def test_workflow_b_normalises_path_mappings_to_an_empty_list(self):
        # Even if the model volunteers them, Workflow B must not carry them: they
        # would make RuleJSON demand a mapping per required parameter.
        parsed = self._parse(VALID_OUTPUT, extract_paths=False)
        assert parsed["path_mappings"] == []

    def test_every_missing_field_is_named_in_one_message(self):
        with pytest.raises(TranslationError) as excinfo:
            self._parse({"parameters": []})
        message = str(excinfo.value)
        assert "path_mappings" in message and "constraints" in message

    @pytest.mark.parametrize("field", ["parameters", "constraints", "path_mappings"])
    def test_a_field_of_the_wrong_type_is_rejected(self, field):
        payload = dict(VALID_OUTPUT)
        payload[field] = {"not": "a list"}
        with pytest.raises(TranslationError) as excinfo:
            self._parse(payload)
        assert "invalid field types" in str(excinfo.value)

    def test_an_empty_parameters_list_is_rejected(self):
        # A rule with no parameters reads nothing from the document, so it returns
        # the same verdict for every input.
        payload = dict(VALID_OUTPUT, parameters=[])
        with pytest.raises(TranslationError) as excinfo:
            self._parse(payload)
        assert "empty" in str(excinfo.value)

    def test_an_empty_constraints_list_is_rejected(self):
        # This is the dangerous one: no constraints means the solver returns sat for
        # any reading, so the rule would report PASS on every document.
        payload = dict(VALID_OUTPUT, constraints=[])
        with pytest.raises(TranslationError) as excinfo:
            self._parse(payload)
        assert "empty" in str(excinfo.value)

    def test_empty_path_mappings_are_accepted(self):
        # Deliberately allowed: the model signals "use LLM extraction" this way, and
        # RuleJSON skips its bijection check when there are none.
        parsed = self._parse(dict(VALID_OUTPUT, path_mappings=[]))
        assert parsed["path_mappings"] == []

    def test_type_errors_are_reported_before_emptiness_is_considered(self):
        # A non-list cannot be measured for length, so the order of the two passes
        # is what keeps this from raising TypeError instead of TranslationError.
        payload = dict(VALID_OUTPUT, parameters="", constraints="")
        with pytest.raises(TranslationError) as excinfo:
            self._parse(payload)
        assert "invalid field types" in str(excinfo.value)


@pytest.mark.unit
class TestBuildRuleJson:
    """_build_rule_json: parsed output -> a validated RuleJSON."""

    def _build(self, parsed: dict[str, Any], **kwargs: Any) -> RuleJSON:
        defaults: dict[str, Any] = {
            "rule_id": "r1",
            "version": "1.0",
            "description": "d",
            "natural_language_rule": "nl",
        }
        defaults.update(kwargs)
        return _translator()._build_rule_json(parsed_output=parsed, **defaults)

    def test_parameters_and_mappings_become_typed_objects(self):
        rule = self._build(VALID_OUTPUT)
        assert all(isinstance(p, Parameter) for p in rule.parameters)
        assert len(rule.path_mappings) == 2

    def test_the_identity_fields_are_carried_through(self):
        rule = self._build(
            VALID_OUTPUT, rule_id="rid", version="2.0", description="desc"
        )
        assert rule.rule_id == "rid"
        assert rule.version == "2.0"
        assert rule.description == "desc"

    def test_the_metadata_records_the_model_and_the_workflow(self):
        # Which model translated a rule, and under which workflow, is what makes a
        # cached rule's provenance auditable.
        rule = self._build(VALID_OUTPUT)
        assert rule.metadata["model_id"].startswith("us.anthropic")
        assert rule.metadata["workflow"] == "path_based"
        assert rule.metadata["created_at"].endswith("Z")
        assert rule.metadata["translator_version"] == "1.0"

    def test_workflow_b_is_recorded_in_the_metadata(self):
        payload = dict(VALID_OUTPUT, path_mappings=[])
        rule = self._build(payload, extract_paths=False)
        assert rule.metadata["workflow"] == "llm_based"

    def test_workflow_b_ignores_any_path_mappings_present(self):
        rule = self._build(VALID_OUTPUT, extract_paths=False)
        assert rule.path_mappings == []

    def test_a_malformed_parameter_is_reported_with_its_content(self):
        payload = dict(VALID_OUTPUT, parameters=[{"name": "x"}])
        with pytest.raises(ValueError, match="Failed to parse parameter"):
            self._build(payload)

    def test_a_malformed_path_mapping_is_reported_with_its_content(self):
        payload = dict(VALID_OUTPUT, path_mappings=[{"parameter_name": "coverage"}])
        with pytest.raises(ValueError, match="Failed to parse path mapping"):
            self._build(payload)

    def test_rule_json_validation_still_applies(self):
        # _build_rule_json does not re-implement validation; it relies on
        # RuleJSON.__post_init__, so a mapping for an undeclared parameter must
        # still be rejected here.
        payload = dict(
            VALID_OUTPUT,
            path_mappings=[{"parameter_name": "unknown", "data_path": "a.b"}],
        )
        with pytest.raises(ValueError):
            self._build(payload)


@pytest.mark.unit
class TestTranslateRule:
    """translate_rule: the five steps and their error attribution."""

    def _translate(self, response: Any = None, **kwargs: Any):
        translator = _translator()
        text = json.dumps(VALID_OUTPUT) if response is None else response
        with patch.object(translator, "_invoke_bedrock", return_value=text) as invoke:
            rule = translator.translate_rule(
                natural_language_rule="coverage / income <= 20",
                data_example={"doc": {"coverage": 1, "income": 1}},
                **kwargs,
            )
        return rule, invoke

    def test_a_successful_translation_returns_a_rule_json(self):
        rule, _ = self._translate()
        assert isinstance(rule, RuleJSON)
        assert len(rule.parameters) == 2

    def test_a_rule_id_is_generated_when_none_is_supplied(self):
        rule, _ = self._translate()
        assert rule.rule_id.startswith("rule_")

    def test_a_supplied_rule_id_is_used(self):
        rule, _ = self._translate(rule_id="explicit_id")
        assert rule.rule_id == "explicit_id"

    def test_the_description_defaults_to_the_rule_text(self):
        rule, _ = self._translate()
        assert rule.description == "coverage / income <= 20"

    def test_a_long_rule_text_is_truncated_for_the_description(self):
        # RuleJSON accepts any non-empty description, so this bound exists to keep a
        # pasted paragraph out of every report that prints it.
        translator = _translator()
        with patch.object(
            translator, "_invoke_bedrock", return_value=json.dumps(VALID_OUTPUT)
        ):
            rule = translator.translate_rule("x" * 500, {"doc": {}})
        assert len(rule.description) == 200

    def test_a_supplied_description_is_used(self):
        rule, _ = self._translate(description="explicit description")
        assert rule.description == "explicit description"

    def test_the_prompt_reaches_bedrock(self):
        _, invoke = self._translate()
        prompt = invoke.call_args.args[0]
        assert "coverage / income <= 20" in prompt

    def test_a_translation_error_from_bedrock_propagates_unwrapped(self):
        # It already carries the rule id and the raw response; wrapping would bury
        # them one level deeper.
        translator = _translator()
        original = TranslationError(message="throttled", operation="invoke_bedrock")
        with patch.object(translator, "_invoke_bedrock", side_effect=original):
            with pytest.raises(TranslationError) as excinfo:
                translator.translate_rule("rule", {})
        assert excinfo.value is original

    def test_an_unexpected_bedrock_error_is_wrapped_and_attributed(self):
        translator = _translator()
        with patch.object(
            translator, "_invoke_bedrock", side_effect=RuntimeError("socket closed")
        ):
            with pytest.raises(TranslationError) as excinfo:
                translator.translate_rule("rule", {})
        assert excinfo.value.operation == "invoke_bedrock"
        assert "socket closed" in str(excinfo.value)

    def test_a_parse_failure_propagates_as_a_parse_error(self):
        translator = _translator()
        with patch.object(translator, "_invoke_bedrock", return_value="not json"):
            with pytest.raises(TranslationError) as excinfo:
                translator.translate_rule("rule", {})
        assert excinfo.value.operation == "parse_llm_output"

    def test_a_rule_json_construction_failure_is_attributed_to_that_step(self):
        # Attribution matters: "build_rule_json" tells an operator the model
        # answered and the answer was structurally wrong, where "invoke_bedrock"
        # would point them at the wrong thing.
        translator = _translator()
        payload = dict(VALID_OUTPUT, parameters=[{"name": "x", "type": "Decimal"}])
        with patch.object(
            translator, "_invoke_bedrock", return_value=json.dumps(payload)
        ):
            with pytest.raises(TranslationError) as excinfo:
                translator.translate_rule("rule", {})
        assert excinfo.value.operation == "build_rule_json"

    def test_workflow_b_produces_a_rule_with_no_path_mappings(self):
        translator = _translator()
        payload = {k: v for k, v in VALID_OUTPUT.items() if k != "path_mappings"}
        with patch.object(
            translator, "_invoke_bedrock", return_value=json.dumps(payload)
        ):
            rule = translator.translate_rule("rule", {}, extract_paths=False)
        assert rule.has_path_mappings() is False


@pytest.mark.unit
class TestBuildExtractionPrompt:
    """_build_extraction_prompt: the data-shape branches."""

    def test_the_rule_context_and_parameters_reach_the_prompt(self):
        prompt = _translator()._build_extraction_prompt(_rule_json(), {"a": 1})
        assert "Coverage must not exceed 20x income" in prompt
        assert "coverage / income <= 20" in prompt
        assert "coverage" in prompt and "Real" in prompt

    def test_the_system_prompt_comes_first(self):
        prompt = _translator()._build_extraction_prompt(_rule_json(), {})
        assert prompt.startswith("EXTRACTION_SYSTEM_MARKER")

    #: Every label `_build_extraction_prompt` can put in `data_type`. Asserted as a
    #: closed set so a branch cannot be relabelled without a test noticing.
    DATA_TYPE_LABELS = ("structured JSON", "text", "list/array", "unknown format")

    @pytest.mark.parametrize(
        "data, expected, rendered",
        [
            ({"a": 1}, "structured JSON", '"a": 1'),
            ("free prose here", "text", "free prose here"),
            ([1, 2], "list/array", "[\n  1,\n  2\n]"),
            (42, "unknown format", "42"),
        ],
    )
    def test_the_data_shape_is_described_to_the_model(self, data, expected, rendered):
        # Telling the model the data is JSON when it is prose makes it look for fields
        # that do not exist and report the values missing, so the label is load-bearing
        # rather than cosmetic.
        #
        # Asserted as "exactly this label and none of the others" rather than `expected
        # in prompt`. The previous version passed `"free text here"` as the data and
        # asserted `"text" in prompt`, which the DATA ITSELF satisfied -- relabelling
        # the string branch to "prose" would have gone undetected. Checking the whole
        # label set also makes the string case prove the dict label is absent, which is
        # the confusion that actually costs an extraction.
        prompt = _translator()._build_extraction_prompt(_rule_json(), data)
        present = [label for label in self.DATA_TYPE_LABELS if label in prompt]
        assert present == [expected], (
            f"expected only {expected!r} to describe {data!r}, found {present}"
        )
        # The data has to actually reach the model, not just be described. A branch that
        # labelled correctly and dropped the payload would satisfy the check above.
        assert rendered in prompt, (
            f"the data itself is missing from the prompt: {data!r}"
        )

    def test_the_required_flag_is_included_per_parameter(self):
        rule = _rule_json(
            parameters=[
                Parameter(name="coverage", type="Real"),
                Parameter(name="limit", type="Real", required=False),
            ],
            constraints=["(<= coverage limit)"],
        )
        prompt = _translator()._build_extraction_prompt(rule, {})
        assert "false" in prompt.lower()


@pytest.mark.unit
class TestParseExtractionOutput:
    """_parse_extraction_output: presence and non-nullness of required values."""

    def _parse(self, payload: Any, rule: RuleJSON | None = None):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return _translator()._parse_extraction_output(text, rule or _rule_json())

    def test_a_valid_response_returns_the_extracted_values(self):
        values = self._parse({"extracted_values": {"coverage": 100.0, "income": 50.0}})
        assert values == {"coverage": 100.0, "income": 50.0}

    def test_a_fenced_block_is_unwrapped(self):
        payload = json.dumps({"extracted_values": {"coverage": 1.0, "income": 1.0}})
        assert self._parse(f"```json\n{payload}\n```")["coverage"] == 1.0

    @pytest.mark.parametrize("text", ["not json", "", "{unclosed"])
    def test_unparseable_output_raises_with_the_rule_id(self, text):
        with pytest.raises(TranslationError) as excinfo:
            self._parse(text)
        assert excinfo.value.operation == "parse_extraction_output"
        assert excinfo.value.rule_id == "coverage_ratio"

    def test_a_missing_extracted_values_key_is_rejected(self):
        with pytest.raises(TranslationError, match="extracted_values"):
            self._parse({"values": {"coverage": 1.0}})

    def test_extracted_values_of_the_wrong_type_is_rejected(self):
        with pytest.raises(TranslationError, match="must be a dictionary"):
            self._parse({"extracted_values": [1, 2]})

    def test_a_missing_required_value_is_rejected_and_named(self):
        with pytest.raises(TranslationError) as excinfo:
            self._parse({"extracted_values": {"coverage": 100.0}})
        assert "income" in str(excinfo.value)

    def test_a_required_value_present_but_null_is_rejected(self):
        # Null and absent are different model behaviours and both mean the value was
        # not found, so both have to be refused rather than reaching the solver as a
        # missing binding.
        with pytest.raises(TranslationError) as excinfo:
            self._parse({"extracted_values": {"coverage": 100.0, "income": None}})
        assert "null" in str(excinfo.value)

    def test_every_missing_required_value_is_named_in_one_message(self):
        with pytest.raises(TranslationError) as excinfo:
            self._parse({"extracted_values": {}})
        message = str(excinfo.value)
        assert "coverage" in message and "income" in message

    def test_an_optional_value_may_be_absent(self):
        rule = _rule_json(
            parameters=[
                Parameter(name="coverage", type="Real"),
                Parameter(name="limit", type="Real", required=False),
            ],
            constraints=["(<= coverage limit)"],
        )
        values = self._parse({"extracted_values": {"coverage": 100.0}}, rule)
        assert values == {"coverage": 100.0}

    def test_an_optional_value_may_be_null(self):
        rule = _rule_json(
            parameters=[
                Parameter(name="coverage", type="Real"),
                Parameter(name="limit", type="Real", required=False),
            ],
            constraints=["(<= coverage limit)"],
        )
        values = self._parse(
            {"extracted_values": {"coverage": 100.0, "limit": None}}, rule
        )
        assert values["limit"] is None

    def test_extra_values_the_rule_did_not_ask_for_are_passed_through(self):
        # The solver ignores values with no declared parameter, so filtering them
        # here would only hide what the model actually returned.
        values = self._parse(
            {"extracted_values": {"coverage": 1.0, "income": 1.0, "stray": 9}}
        )
        assert values["stray"] == 9

    @pytest.mark.parametrize(
        "value, fragment",
        [
            ("not a number", "Cannot convert string"),
            (30.9, "without loss"),
            (True, "declare the parameter as Bool"),
        ],
    )
    def test_a_value_that_is_not_the_declared_type_is_refused(self, value, fragment):
        # Each of these used to pass straight through, because this method validated
        # presence and non-nullness and never referenced `param.type` (#1057). The
        # refusal message is asserted on, not just the exception: "refused" without a
        # reason gives whoever reads the log nothing to act on, and all three of these
        # reach the same raise from different causes.
        rule = _rule_json(
            parameters=[Parameter(name="n", type="Int")], constraints=["(<= n 30)"]
        )
        with pytest.raises(TranslationError) as excinfo:
            self._parse({"extracted_values": {"n": value}}, rule)
        assert any(fragment in e for e in (excinfo.value.validation_errors or [])), (
            excinfo.value.validation_errors
        )

    @pytest.mark.parametrize(
        "declared, value, expected",
        [
            ("Int", 30, 30),
            ("Int", "30", 30),  # a numeric string is a lossless reading, not a refusal
            ("Real", 30.9, 30.9),
        ],
    )
    def test_a_value_that_IS_the_declared_type_still_passes_through(
        self, declared, value, expected
    ):
        # The over-refusal direction, which is the one a type check is most likely to
        # get wrong and the one nothing else here would catch: every refusal on this
        # route costs a whole extraction attempt, because the caller catches
        # TranslationError and retries against the raw document text — a second
        # Bedrock call. A check that refused `"30"` for an Int would therefore double
        # the cost of a correct answer rather than fail outright, which is the kind of
        # regression that shows up on a bill instead of in a test.
        rule = _rule_json(
            parameters=[Parameter(name="n", type=declared)], constraints=["(<= n 30)"]
        )
        assert self._parse({"extracted_values": {"n": value}}, rule) == {"n": expected}


@pytest.mark.unit
class TestExtractValuesWithLlm:
    """extract_values_with_llm: prompt, invoke, parse."""

    def test_a_successful_extraction_returns_the_values(self):
        translator = _translator()
        payload = json.dumps({"extracted_values": {"coverage": 100.0, "income": 50.0}})
        with patch.object(translator, "_invoke_bedrock", return_value=payload):
            values = translator.extract_values_with_llm(_rule_json(), {"doc": {}})
        assert values == {"coverage": 100.0, "income": 50.0}

    def test_the_extraction_config_is_used_rather_than_the_translator_one(self):
        # The two steps are separately configurable precisely so extraction can run
        # on a cheaper model; invoking with the translator config would silently
        # spend the expensive one on every document.
        translator = _translator()
        payload = json.dumps({"extracted_values": {"coverage": 1.0, "income": 1.0}})
        with patch.object(
            translator, "_invoke_bedrock", return_value=payload
        ) as invoke:
            translator.extract_values_with_llm(_rule_json(), {})
        assert invoke.call_args.kwargs.get("use_extraction_config") is True

    def test_the_data_reaches_the_prompt(self):
        translator = _translator()
        payload = json.dumps({"extracted_values": {"coverage": 1.0, "income": 1.0}})
        with patch.object(
            translator, "_invoke_bedrock", return_value=payload
        ) as invoke:
            translator.extract_values_with_llm(_rule_json(), "DATA_MARKER")
        assert "DATA_MARKER" in invoke.call_args.args[0]

    def test_a_parse_failure_propagates_as_a_translation_error(self):
        translator = _translator()
        with patch.object(translator, "_invoke_bedrock", return_value="not json"):
            with pytest.raises(TranslationError):
                translator.extract_values_with_llm(_rule_json(), {})


@pytest.mark.unit
class TestInvokeBedrockRetryLadder:
    """_invoke_bedrock: which failures are retried, which are not, and what is raised.

    This is the one Bedrock-facing method in the translator, and every call into the
    model goes through it, so an error classified wrongly here either burns attempts on
    something that will never succeed or gives up on something transient. It had no
    tests: `for attempt in range(max_retries)` never executed, so the whole ladder --
    both retry branches, the exhaustion paths and the classification set -- was
    unexercised.

    `time.sleep` is patched out in every case. The backoff doubles from
    `initial_backoff`, so a test that actually slept would add real seconds to the suite
    for no assurance, and the delay *values* are asserted directly instead.
    """

    @staticmethod
    def _client_error(code: str) -> ClientError:
        return ClientError(
            {"Error": {"Code": code, "Message": f"{code} happened"}}, "InvokeModel"
        )

    @staticmethod
    def _ok_response(text: str = "RESPONSE_MARKER") -> dict:
        body = {
            "content": [{"text": text}],
            "usage": {"input_tokens": 11, "output_tokens": 22},
        }
        return {"body": io.BytesIO(json.dumps(body).encode())}

    def _translator_with(self, *side_effects):
        translator = _translator()
        translator.bedrock_client = MagicMock()
        translator.bedrock_client.invoke_model.side_effect = list(side_effects)
        return translator

    def test_a_first_attempt_success_does_not_retry(self):
        translator = self._translator_with(self._ok_response())
        with patch(f"{MODULE}.time.sleep") as sleep:
            assert translator._invoke_bedrock("p") == "RESPONSE_MARKER"
        assert translator.bedrock_client.invoke_model.call_count == 1
        sleep.assert_not_called()

    @pytest.mark.parametrize(
        "code",
        [
            "ThrottlingException",
            "ServiceUnavailableException",
            "TooManyRequestsException",
            "RequestTimeoutException",
        ],
    )
    def test_each_retryable_code_is_retried_and_can_then_succeed(self, code):
        # The set is asserted member by member rather than as a whole: dropping one
        # entry turns a transient throttle into an immediate hard failure for every
        # rule, and a single-member test would not see it.
        translator = self._translator_with(
            self._client_error(code), self._ok_response()
        )
        with patch(f"{MODULE}.time.sleep"):
            assert translator._invoke_bedrock("p") == "RESPONSE_MARKER"
        assert translator.bedrock_client.invoke_model.call_count == 2

    def test_a_non_retryable_code_fails_on_the_first_attempt(self):
        # AccessDeniedException will never succeed on a retry, and retrying it delays
        # the error the operator needs to see.
        translator = self._translator_with(
            self._client_error("AccessDeniedException"), self._ok_response()
        )
        with patch(f"{MODULE}.time.sleep") as sleep:
            with pytest.raises(TranslationError) as excinfo:
                translator._invoke_bedrock("p")
        assert translator.bedrock_client.invoke_model.call_count == 1
        sleep.assert_not_called()
        assert excinfo.value.context["error_code"] == "AccessDeniedException"

    def test_the_default_allows_two_attempts_not_three(self):
        # `max_retries: int = 2` with `range(max_retries)` is TWO attempts in total,
        # i.e. one retry. The docstring says "default: 3". Pinned at the observable
        # count so the mismatch is visible to whoever reconciles them; if the default
        # is raised to 3 this test is the one that says so.
        translator = self._translator_with(
            self._client_error("ThrottlingException"),
            self._client_error("ThrottlingException"),
            self._ok_response(),
        )
        with patch(f"{MODULE}.time.sleep"):
            with pytest.raises(TranslationError):
                translator._invoke_bedrock("p")
        assert translator.bedrock_client.invoke_model.call_count == 2

    def test_the_backoff_doubles_between_attempts(self):
        translator = self._translator_with(
            *([self._client_error("ThrottlingException")] * 4)
        )
        with patch(f"{MODULE}.time.sleep") as sleep:
            with pytest.raises(TranslationError):
                translator._invoke_bedrock("p", max_retries=4, initial_backoff=0.5)
        assert [call.args[0] for call in sleep.call_args_list] == [0.5, 1.0, 2.0]

    def test_exhausting_the_retries_reports_the_attempt_count(self):
        translator = self._translator_with(
            *([self._client_error("ThrottlingException")] * 3)
        )
        with patch(f"{MODULE}.time.sleep"):
            with pytest.raises(TranslationError) as excinfo:
                translator._invoke_bedrock("p", rule_id="r1", max_retries=3)
        assert excinfo.value.context["max_retries"] == 3
        assert excinfo.value.rule_id == "r1"

    def test_a_connection_error_is_retried(self):
        # BotoCoreError has its own branch, with no code to classify -- every one of
        # them is treated as transient.
        translator = self._translator_with(
            EndpointConnectionError(endpoint_url="https://bedrock"), self._ok_response()
        )
        with patch(f"{MODULE}.time.sleep"):
            assert translator._invoke_bedrock("p") == "RESPONSE_MARKER"
        assert translator.bedrock_client.invoke_model.call_count == 2

    def test_a_connection_error_that_never_clears_raises(self):
        translator = self._translator_with(
            *([EndpointConnectionError(endpoint_url="https://bedrock")] * 2)
        )
        with patch(f"{MODULE}.time.sleep"):
            with pytest.raises(TranslationError) as excinfo:
                translator._invoke_bedrock("p")
        assert excinfo.value.context["error_type"] == "EndpointConnectionError"

    def test_a_response_with_no_content_field_is_not_retried(self):
        # Raised inside the `try`, so it is caught by the bare `except Exception` and
        # re-wrapped rather than retried. Worth pinning because a malformed response is
        # the one failure here that looks transient and is not.
        translator = self._translator_with({"body": io.BytesIO(b'{"usage": {}}')})
        with patch(f"{MODULE}.time.sleep") as sleep:
            with pytest.raises(TranslationError):
                translator._invoke_bedrock("p")
        assert translator.bedrock_client.invoke_model.call_count == 1
        sleep.assert_not_called()

    def test_an_unparseable_body_is_not_retried(self):
        translator = self._translator_with({"body": io.BytesIO(b"not json")})
        with patch(f"{MODULE}.time.sleep") as sleep:
            with pytest.raises(TranslationError) as excinfo:
                translator._invoke_bedrock("p")
        assert "JSON" in excinfo.value.message
        sleep.assert_not_called()

    def test_the_extraction_config_selects_the_extraction_model(self):
        # The two configs exist so extraction can run on a cheaper model; picking the
        # translator config here would spend the expensive one on every document.
        translator = self._translator_with(self._ok_response())
        with patch(f"{MODULE}.time.sleep"):
            translator._invoke_bedrock("p", use_extraction_config=True)
        kwargs = translator.bedrock_client.invoke_model.call_args.kwargs
        assert kwargs["modelId"] == translator.extraction_config.model_id
        assert kwargs["modelId"] != translator.translator_config.model_id

    def test_the_request_body_carries_the_configured_limits(self):
        translator = self._translator_with(self._ok_response())
        with patch(f"{MODULE}.time.sleep"):
            translator._invoke_bedrock("PROMPT_MARKER")
        body = json.loads(
            translator.bedrock_client.invoke_model.call_args.kwargs["body"]
        )
        assert body["max_tokens"] == translator.translator_config.max_tokens
        assert body["temperature"] == translator.translator_config.temperature
        assert body["messages"] == [{"role": "user", "content": "PROMPT_MARKER"}]
