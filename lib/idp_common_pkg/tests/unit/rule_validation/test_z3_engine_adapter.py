# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for `Z3EngineAdapter`, the bridge between the Z3 validation system and
the IDP rule-validation pipeline.

The adapter does three things that decide what an operator sees, and none of them
raise on a wrong answer:

**It maps a solver outcome to a published recommendation.** `sat` becomes "Pass",
`unsat` becomes "Fail", and anything else becomes "Information Not Found". Getting
that mapping wrong inverts a compliance verdict, and the reasoning text would still
read plausibly. The whole table is asserted, including the default, and the
error path is asserted as **not** becoming Pass or Fail — "could not evaluate" is a
third answer, not a lenient version of either.

**It swallows every exception into a result.** `validate_rule`'s catch-all turns any
failure — translation, extraction, solver — into a normal-looking response carrying
`recommendation: "Information Not Found"` and a `_z3_error` marker. That is the right
behaviour for a pipeline stage, and it means the marker is the only way a caller can
tell a real "not found" from a crash, so it is asserted on every failure path.

**It caches translated rules, in memory and in S3, keyed by a hash of the rule
description.** A cache hit skips the model call entirely, so a key collision or a
stale read applies one rule's constraints under another rule's name. The key
derivation, both cache layers, and the precedence between them are asserted
directly. This cache is also what makes
[#1058](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1058)
recurring rather than one-off: a rule that fails validation at construction is
cached in its broken form.

`ValidationSystem` is stubbed at the adapter's import site and `idp_common.s3` is
patched, so nothing here reaches Bedrock or S3.
"""

import hashlib
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from idp_common.rule_validation.z3_engine import (
    _OUTCOME_TO_RECOMMENDATION,
    Z3EngineAdapter,
    _build_extraction_config,
    _build_translator_config,
)

MODULE = "idp_common.rule_validation.z3_engine"


def _adapter(config: Any = None, **kwargs: Any) -> tuple[Z3EngineAdapter, MagicMock]:
    """An adapter whose ValidationSystem is a stub. Returns (adapter, system).

    `ValidationSystem` is imported lazily inside `__init__`, so it is patched where
    it is DEFINED rather than on the adapter's module — patching
    `z3_engine.ValidationSystem` would create an attribute nothing reads.
    """
    with patch(
        "idp_common.rule_validation.z3.validation_system.ValidationSystem"
    ) as system_cls:
        adapter = Z3EngineAdapter(config=config, **kwargs)
    return adapter, system_cls.return_value


def _result(
    outcome: str, *, values: dict[str, Any] | None = None, error: str | None = None
):
    """A ValidationResult stand-in with the passes()/fails() contract."""
    result = MagicMock()
    result.outcome = outcome
    result.extracted_values = values if values is not None else {"coverage": 100.0}
    result.error_message = error
    result.execution_time_ms = 12.5
    result.passes.return_value = outcome == "sat"
    result.fails.return_value = outcome == "unsat"
    return result


def _rule_json(*, has_paths: bool = True, rule_id: str = "r1"):
    rule = MagicMock()
    rule.rule_id = rule_id
    rule.has_path_mappings.return_value = has_paths
    rule.to_dict.return_value = {"rule_id": rule_id}
    return rule


@pytest.mark.unit
class TestOutcomeMapping:
    """_OUTCOME_TO_RECOMMENDATION and _to_llm_response."""

    def test_the_three_outcomes_map_to_the_three_recommendations(self):
        assert _OUTCOME_TO_RECOMMENDATION == {
            "sat": "Pass",
            "unsat": "Fail",
            "error": "Information Not Found",
        }

    def test_sat_is_reported_as_pass(self):
        response = Z3EngineAdapter._to_llm_response(_result("sat"), "Lending", "rule")
        assert response["recommendation"] == "Pass"

    def test_unsat_is_reported_as_fail(self):
        response = Z3EngineAdapter._to_llm_response(_result("unsat"), "Lending", "rule")
        assert response["recommendation"] == "Fail"

    def test_an_error_is_neither_pass_nor_fail(self):
        # "could not evaluate" is a third answer. Collapsing it into either would
        # publish a verdict for a document that was never checked.
        response = Z3EngineAdapter._to_llm_response(
            _result("error", error="null required parameter"), "Lending", "rule"
        )
        assert response["recommendation"] == "Information Not Found"

    def test_an_unrecognised_outcome_defaults_to_information_not_found(self):
        response = Z3EngineAdapter._to_llm_response(
            _result("unknown", error="timeout"), "Lending", "rule"
        )
        assert response["recommendation"] == "Information Not Found"

    def test_the_rule_type_and_text_are_echoed_back(self):
        # The orchestrator groups results by rule_type and shows `rule` verbatim, so
        # both have to survive the conversion unchanged.
        response = Z3EngineAdapter._to_llm_response(
            _result("sat"), "LendingPolicy", "coverage / income <= 20"
        )
        assert response["rule_type"] == "LendingPolicy"
        assert response["rule"] == "coverage / income <= 20"

    def test_a_passing_result_explains_itself_with_the_values_it_used(self):
        # The extracted values are the audit trail: they are what makes a Pass
        # checkable rather than asserted.
        response = Z3EngineAdapter._to_llm_response(
            _result("sat", values={"coverage": 100.0, "income": 50.0}),
            "Lending",
            "rule",
        )
        assert "satisfied" in response["reasoning"]
        assert "coverage" in response["reasoning"]
        assert "100.0" in response["reasoning"]

    def test_a_failing_result_says_not_satisfied_and_shows_its_values(self):
        response = Z3EngineAdapter._to_llm_response(
            _result("unsat", values={"coverage": 9999.0}), "Lending", "rule"
        )
        assert "NOT satisfied" in response["reasoning"]
        assert "9999.0" in response["reasoning"]

    def test_an_error_result_reports_the_error_message(self):
        response = Z3EngineAdapter._to_llm_response(
            _result("error", error="Unknown atom 'incom'"), "Lending", "rule"
        )
        assert "Unknown atom 'incom'" in response["reasoning"]

    def test_the_execution_time_is_always_reported(self):
        for outcome in ("sat", "unsat", "error"):
            response = Z3EngineAdapter._to_llm_response(
                _result(outcome, error="e"), "Lending", "rule"
            )
            assert "12.5ms" in response["reasoning"]

    def test_supporting_pages_are_empty_because_z3_has_no_page_provenance(self):
        # The solver works from extracted values, not from page text, so an empty
        # list is the honest answer rather than a missing field.
        response = Z3EngineAdapter._to_llm_response(_result("sat"), "Lending", "rule")
        assert response["supporting_pages"] == []

    def test_a_successful_response_carries_no_error_marker(self):
        response = Z3EngineAdapter._to_llm_response(_result("sat"), "Lending", "rule")
        assert "_z3_error" not in response


@pytest.mark.unit
class TestConfigConversion:
    """_build_translator_config / _build_extraction_config."""

    def _idp_translator_cfg(self, **overrides: Any):
        cfg = MagicMock()
        cfg.model = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        cfg.temperature = 0.0
        cfg.max_tokens = 4096
        cfg.system_prompt = "sys"
        cfg.task_prompt = "Translate {rule} against {data_example}"
        cfg.few_shot_examples = None
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg

    def test_every_translator_field_is_mapped(self):
        # The IDP config names the template `task_prompt` and the internal dataclass
        # names it `task_prompt_template`; a rename on either side silently produces
        # a config that fails its own placeholder validation.
        converted = _build_translator_config(self._idp_translator_cfg())
        assert converted.model_id.startswith("us.anthropic")
        assert converted.temperature == 0.0
        assert converted.max_tokens == 4096
        assert converted.system_prompt == "sys"
        assert "{rule}" in converted.task_prompt_template

    def test_absent_few_shot_examples_become_an_empty_list(self):
        # TranslatorConfig rejects a non-list, so passing None through would make
        # every IDP-configured translation fail at construction.
        converted = _build_translator_config(self._idp_translator_cfg())
        assert converted.few_shot_examples == []

    def test_supplied_few_shot_examples_are_carried_through(self):
        examples = [
            {
                "rule": "r",
                "data_example": {},
                "output": {"parameters": [], "path_mappings": [], "constraints": []},
            }
        ]
        converted = _build_translator_config(
            self._idp_translator_cfg(few_shot_examples=examples)
        )
        assert converted.few_shot_examples == examples

    def test_every_extraction_field_is_mapped(self):
        cfg = MagicMock()
        cfg.model = "us.amazon.nova-lite-v1:0"
        cfg.temperature = 0.0
        cfg.max_tokens = 2048
        cfg.system_prompt = "sys"
        cfg.task_prompt = (
            "{rule_description} {natural_language_rule} {parameters_json} "
            "{data_type} {data}"
        )
        converted = _build_extraction_config(cfg)
        assert converted.model_id == "us.amazon.nova-lite-v1:0"
        assert converted.max_tokens == 2048
        assert "{data}" in converted.task_prompt_template


@pytest.mark.unit
class TestConstruction:
    """__init__: the config fallback rule, which is all-or-nothing."""

    def _idp_config(self, *, translator: bool, extraction: bool, timeout: int = 5000):
        config = MagicMock()
        rv = config.rule_validation
        rv.z3_timeout_ms = timeout
        if translator:
            rv.z3_rule_translator = MagicMock(
                model="m",
                temperature=0.0,
                max_tokens=10,
                system_prompt="sys",
                task_prompt="{rule} {data_example}",
                few_shot_examples=None,
            )
        else:
            rv.z3_rule_translator = None
        if extraction:
            rv.z3_value_extraction = MagicMock(
                model="m",
                temperature=0.0,
                max_tokens=10,
                system_prompt="sys",
                task_prompt=(
                    "{rule_description} {natural_language_rule} {parameters_json} "
                    "{data_type} {data}"
                ),
            )
        else:
            rv.z3_value_extraction = None
        return config

    def _build(self, config: Any):
        with patch(
            "idp_common.rule_validation.z3.validation_system.ValidationSystem"
        ) as system_cls:
            Z3EngineAdapter(config=config)
        return system_cls

    def test_no_config_falls_back_to_the_bundled_yaml_defaults(self):
        system_cls = self._build(None)
        kwargs = system_cls.call_args.kwargs
        assert "translator_config" not in kwargs
        assert kwargs["z3_timeout_ms"] == 5000

    def test_both_configs_present_are_passed_through(self):
        system_cls = self._build(self._idp_config(translator=True, extraction=True))
        kwargs = system_cls.call_args.kwargs
        assert kwargs["translator_config"] is not None
        assert kwargs["extraction_config"] is not None

    @pytest.mark.parametrize(
        "translator,extraction", [(True, False), (False, True), (False, False)]
    )
    def test_a_partial_config_falls_back_for_both_rather_than_mixing(
        self, translator, extraction
    ):
        # Deliberate all-or-nothing: mixing an IDP-configured translator with a
        # YAML-default extractor would pair prompts that were never written for each
        # other, and the mismatch is only visible in the model's output.
        system_cls = self._build(
            self._idp_config(translator=translator, extraction=extraction)
        )
        assert "translator_config" not in system_cls.call_args.kwargs

    def test_a_translator_config_with_no_system_prompt_is_treated_as_absent(self):
        config = self._idp_config(translator=True, extraction=True)
        config.rule_validation.z3_rule_translator.system_prompt = ""
        system_cls = self._build(config)
        assert "translator_config" not in system_cls.call_args.kwargs

    def test_the_configured_timeout_reaches_the_validation_system(self):
        system_cls = self._build(
            self._idp_config(translator=True, extraction=True, timeout=1234)
        )
        assert system_cls.call_args.kwargs["z3_timeout_ms"] == 1234

    def test_the_timeout_is_honoured_even_on_the_yaml_fallback_path(self):
        # The fallback abandons the prompts, not the timeout -- which is the one
        # setting that bounds a pathological constraint set inside a Lambda.
        system_cls = self._build(
            self._idp_config(translator=False, extraction=False, timeout=999)
        )
        assert system_cls.call_args.kwargs["z3_timeout_ms"] == 999

    def test_the_region_is_passed_through(self):
        with patch(
            "idp_common.rule_validation.z3.validation_system.ValidationSystem"
        ) as system_cls:
            Z3EngineAdapter(region="eu-west-1")
        assert system_cls.call_args.kwargs["region"] == "eu-west-1"

    def test_the_rule_cache_starts_empty(self):
        adapter, _ = _adapter()
        assert adapter._rule_cache == {}


@pytest.mark.unit
class TestS3Key:
    """_s3_key: deterministic, and namespaced under the caller's prefix."""

    def test_the_key_is_a_truncated_sha256_of_the_rule_description(self):
        expected = hashlib.sha256(b"my rule").hexdigest()[:12]
        assert (
            Z3EngineAdapter._s3_key("pfx", "my rule") == f"pfx/z3_rules/{expected}.json"
        )

    def test_the_same_description_always_gives_the_same_key(self):
        # Determinism is what makes the cache a cache; a salted or timestamped key
        # would translate every rule on every document.
        assert Z3EngineAdapter._s3_key("p", "rule") == Z3EngineAdapter._s3_key(
            "p", "rule"
        )

    def test_different_descriptions_give_different_keys(self):
        assert Z3EngineAdapter._s3_key("p", "rule a") != Z3EngineAdapter._s3_key(
            "p", "rule b"
        )

    def test_a_one_character_difference_changes_the_key(self):
        # A near-collision would apply one rule's constraints under another rule's
        # name, which is the worst failure this cache can have.
        assert Z3EngineAdapter._s3_key("p", "amount <= 100") != Z3EngineAdapter._s3_key(
            "p", "amount <= 200"
        )

    def test_the_prefix_scopes_the_key(self):
        assert Z3EngineAdapter._s3_key("a", "rule") != Z3EngineAdapter._s3_key(
            "b", "rule"
        )

    def test_non_ascii_rule_text_is_hashed_without_raising(self):
        assert Z3EngineAdapter._s3_key("p", "montant ≤ 100 €").endswith(".json")


@pytest.mark.unit
class TestS3Cache:
    """_load_from_s3 / _save_to_s3: both failure-tolerant."""

    def test_a_cached_rule_is_deserialised(self):
        adapter, _ = _adapter()
        payload = {
            "rule_id": "r1",
            "version": "1.0",
            "description": "d",
            "natural_language_rule": "nl",
            "parameters": [{"name": "x", "type": "Real"}],
            "constraints": ["(> x 0)"],
            "path_mappings": [],
        }
        with patch(f"{MODULE}.s3") as s3_mock:
            s3_mock.get_json_content.return_value = payload
            rule = adapter._load_from_s3("bucket", "pfx", "my rule")
        assert rule is not None
        assert rule.rule_id == "r1"

    def test_the_expected_uri_is_read(self):
        adapter, _ = _adapter()
        with patch(f"{MODULE}.s3") as s3_mock:
            s3_mock.get_json_content.return_value = None
            adapter._load_from_s3("bucket", "pfx", "my rule")
        uri = s3_mock.get_json_content.call_args.args[0]
        assert uri.startswith("s3://bucket/pfx/z3_rules/")

    def test_an_empty_object_is_treated_as_a_miss(self):
        adapter, _ = _adapter()
        with patch(f"{MODULE}.s3") as s3_mock:
            s3_mock.get_json_content.return_value = None
            assert adapter._load_from_s3("bucket", "pfx", "rule") is None

    def test_a_read_failure_is_a_miss_rather_than_an_error(self):
        # A cache that fails closed on a read error would make S3 availability a
        # dependency of rule validation rather than an optimisation.
        adapter, _ = _adapter()
        with patch(f"{MODULE}.s3") as s3_mock:
            s3_mock.get_json_content.side_effect = RuntimeError("AccessDenied")
            assert adapter._load_from_s3("bucket", "pfx", "rule") is None

    def test_a_malformed_cached_payload_is_a_miss(self):
        # A corrupted cache entry must not poison every later document; falling back
        # to translation costs one model call.
        adapter, _ = _adapter()
        with patch(f"{MODULE}.s3") as s3_mock:
            s3_mock.get_json_content.return_value = {"not": "a rule"}
            assert adapter._load_from_s3("bucket", "pfx", "rule") is None

    def test_a_rule_is_written_as_json_under_the_derived_key(self):
        adapter, _ = _adapter()
        rule = _rule_json()
        with patch(f"{MODULE}.s3") as s3_mock:
            adapter._save_to_s3("bucket", "pfx", "my rule", rule)
        args, kwargs = s3_mock.write_content.call_args
        assert args[0] == {"rule_id": "r1"}
        assert args[1] == "bucket"
        assert args[2].startswith("pfx/z3_rules/")
        assert kwargs["content_type"] == "application/json"

    def test_a_write_failure_does_not_propagate(self):
        adapter, _ = _adapter()
        with patch(f"{MODULE}.s3") as s3_mock:
            s3_mock.write_content.side_effect = RuntimeError("AccessDenied")
            adapter._save_to_s3("bucket", "pfx", "rule", _rule_json())

    def test_the_read_and_write_keys_agree(self):
        # If they diverged the cache would never hit, and the only symptom would be
        # a model call per document.
        adapter, _ = _adapter()
        with patch(f"{MODULE}.s3") as s3_mock:
            s3_mock.get_json_content.return_value = None
            adapter._load_from_s3("bucket", "pfx", "same rule")
            read_key = s3_mock.get_json_content.call_args.args[0].split("bucket/")[1]
            adapter._save_to_s3("bucket", "pfx", "same rule", _rule_json())
            write_key = s3_mock.write_content.call_args.args[2]
        assert read_key == write_key


@pytest.mark.unit
class TestGetOrTranslateRule:
    """_get_or_translate_rule: memory cache, then S3, then the model."""

    def test_a_memory_hit_skips_s3_and_the_model(self):
        adapter, system = _adapter()
        cached = _rule_json()
        adapter._rule_cache["my rule"] = cached
        with patch(f"{MODULE}.s3") as s3_mock:
            result = adapter._get_or_translate_rule("my rule", {}, "bucket", "pfx")
        assert result is cached
        s3_mock.get_json_content.assert_not_called()
        system.translator.translate_rule.assert_not_called()

    def test_an_s3_hit_skips_the_model_and_warms_the_memory_cache(self):
        adapter, system = _adapter()
        cached = _rule_json()
        with patch.object(adapter, "_load_from_s3", return_value=cached):
            result = adapter._get_or_translate_rule("my rule", {}, "bucket", "pfx")
        assert result is cached
        assert adapter._rule_cache["my rule"] is cached
        system.translator.translate_rule.assert_not_called()

    def test_a_full_miss_translates_and_caches_in_both_layers(self):
        adapter, system = _adapter()
        translated = _rule_json()
        system.translator.translate_rule.return_value = translated
        with (
            patch.object(adapter, "_load_from_s3", return_value=None),
            patch.object(adapter, "_save_to_s3") as save,
        ):
            result = adapter._get_or_translate_rule(
                "my rule", {"a": 1}, "bucket", "pfx"
            )
        assert result is translated
        assert adapter._rule_cache["my rule"] is translated
        save.assert_called_once()

    def test_the_data_example_reaches_the_translator(self):
        adapter, system = _adapter()
        system.translator.translate_rule.return_value = _rule_json()
        with (
            patch.object(adapter, "_load_from_s3", return_value=None),
            patch.object(adapter, "_save_to_s3"),
        ):
            adapter._get_or_translate_rule("my rule", {"doc": "DATA"}, "bucket", "pfx")
        kwargs = system.translator.translate_rule.call_args.kwargs
        assert kwargs["natural_language_rule"] == "my rule"
        assert kwargs["data_example"] == {"doc": "DATA"}

    def test_no_bucket_means_no_s3_layer_at_all(self):
        adapter, system = _adapter()
        system.translator.translate_rule.return_value = _rule_json()
        with (
            patch.object(adapter, "_load_from_s3") as load,
            patch.object(adapter, "_save_to_s3") as save,
        ):
            adapter._get_or_translate_rule("my rule", {})
        load.assert_not_called()
        save.assert_not_called()

    def test_a_bucket_with_no_prefix_also_skips_the_s3_layer(self):
        adapter, system = _adapter()
        system.translator.translate_rule.return_value = _rule_json()
        with patch.object(adapter, "_load_from_s3") as load:
            adapter._get_or_translate_rule("my rule", {}, "bucket", None)
        load.assert_not_called()

    def test_two_different_rules_are_cached_separately(self):
        adapter, system = _adapter()
        system.translator.translate_rule.side_effect = [
            _rule_json(rule_id="a"),
            _rule_json(rule_id="b"),
        ]
        with patch.object(adapter, "_load_from_s3", return_value=None):
            first = adapter._get_or_translate_rule("rule a", {})
            second = adapter._get_or_translate_rule("rule b", {})
        assert first.rule_id == "a"
        assert second.rule_id == "b"
        assert len(adapter._rule_cache) == 2

    def test_the_second_call_for_the_same_rule_does_not_translate_again(self):
        adapter, system = _adapter()
        system.translator.translate_rule.return_value = _rule_json()
        with patch.object(adapter, "_load_from_s3", return_value=None):
            adapter._get_or_translate_rule("my rule", {})
            adapter._get_or_translate_rule("my rule", {})
        assert system.translator.translate_rule.call_count == 1


@pytest.mark.unit
class TestExtractValues:
    """_extract_values: the three-step fallback ladder."""

    def test_structured_data_with_path_mappings_uses_path_extraction(self):
        adapter, system = _adapter()
        system.extractor.extract_values.return_value = {"coverage": 1.0}
        result = adapter._extract_values(_rule_json(has_paths=True), {"a": 1}, "text")
        assert result == {"coverage": 1.0}
        system.translator.extract_values_with_llm.assert_not_called()

    def test_a_path_failure_falls_back_to_the_llm_over_the_structured_data(self):
        adapter, system = _adapter()
        system.extractor.extract_values.side_effect = RuntimeError("path missing")
        system.translator.extract_values_with_llm.return_value = {"coverage": 2.0}
        result = adapter._extract_values(_rule_json(has_paths=True), {"a": 1}, "text")
        assert result == {"coverage": 2.0}
        assert system.translator.extract_values_with_llm.call_args.args[1] == {"a": 1}

    def test_a_rule_with_no_path_mappings_goes_straight_to_the_llm(self):
        adapter, system = _adapter()
        system.translator.extract_values_with_llm.return_value = {"coverage": 3.0}
        result = adapter._extract_values(_rule_json(has_paths=False), {"a": 1}, "text")
        assert result == {"coverage": 3.0}
        system.extractor.extract_values.assert_not_called()

    def test_no_structured_data_uses_the_document_text(self):
        # The last rung is the only one that can read an unstructured document, so
        # skipping it would make every text-only rule unevaluable.
        adapter, system = _adapter()
        system.translator.extract_values_with_llm.return_value = {"coverage": 4.0}
        result = adapter._extract_values(
            _rule_json(has_paths=True), {}, "DOCUMENT TEXT"
        )
        assert result == {"coverage": 4.0}
        assert (
            system.translator.extract_values_with_llm.call_args.args[1]
            == "DOCUMENT TEXT"
        )

    def test_a_structured_llm_failure_falls_back_to_the_document_text(self):
        adapter, system = _adapter()
        system.extractor.extract_values.side_effect = RuntimeError("path missing")
        system.translator.extract_values_with_llm.side_effect = [
            RuntimeError("bad structured data"),
            {"coverage": 5.0},
        ]
        result = adapter._extract_values(
            _rule_json(has_paths=True), {"a": 1}, "DOCUMENT TEXT"
        )
        assert result == {"coverage": 5.0}
        assert system.translator.extract_values_with_llm.call_count == 2

    def test_a_failure_on_the_last_rung_propagates(self):
        # There is nothing left to try, and returning {} would send the solver an
        # empty binding that reports "error" without saying extraction failed.
        adapter, system = _adapter()
        system.translator.extract_values_with_llm.side_effect = RuntimeError(
            "no values"
        )
        with pytest.raises(RuntimeError, match="no values"):
            adapter._extract_values(_rule_json(has_paths=False), {}, "text")


@pytest.mark.unit
class TestValidateRule:
    """validate_rule: the public entry point and its catch-all."""

    def _run(self, *, outcome: str = "sat", **kwargs: Any):
        adapter, system = _adapter()
        system.extractor.extract_values.return_value = {"coverage": 100.0}
        system.translator.translate_rule.return_value = _rule_json()
        system.validate.return_value = _result(outcome)
        with (
            patch.object(adapter, "_load_from_s3", return_value=None),
            patch.object(adapter, "_save_to_s3"),
        ):
            response = adapter.validate_rule(
                rule_description="coverage / income <= 20",
                rule_type="Lending",
                extraction_results=kwargs.pop("extraction_results", {"doc": {}}),
                document_text=kwargs.pop("document_text", "text"),
                **kwargs,
            )
        return response, adapter, system

    def test_a_satisfied_rule_is_reported_as_pass(self):
        response, _, _ = self._run(outcome="sat")
        assert response["recommendation"] == "Pass"
        assert "_z3_error" not in response

    def test_a_violated_rule_is_reported_as_fail(self):
        response, _, _ = self._run(outcome="unsat")
        assert response["recommendation"] == "Fail"

    def test_structured_results_are_used_as_the_translation_data_example(self):
        # The data example is what the model infers path mappings from, so handing
        # it the raw document text where structured results exist produces a rule
        # with no usable paths.
        _, _, system = self._run(extraction_results={"doc": {"coverage": 1}})
        kwargs = system.translator.translate_rule.call_args.kwargs
        assert kwargs["data_example"] == {"doc": {"coverage": 1}}

    def test_the_document_text_is_the_data_example_when_there_are_no_results(self):
        _, _, system = self._run(extraction_results={}, document_text="RAW TEXT")
        kwargs = system.translator.translate_rule.call_args.kwargs
        assert kwargs["data_example"] == "RAW TEXT"

    def test_the_cache_arguments_are_forwarded(self):
        adapter, system = _adapter()
        system.extractor.extract_values.return_value = {}
        system.translator.translate_rule.return_value = _rule_json()
        system.validate.return_value = _result("sat")
        with (
            patch.object(adapter, "_load_from_s3", return_value=None) as load,
            patch.object(adapter, "_save_to_s3"),
        ):
            adapter.validate_rule(
                rule_description="rule",
                rule_type="Lending",
                extraction_results={"a": 1},
                document_text="t",
                output_bucket="bucket",
                cache_prefix="pfx",
            )
        assert load.call_args.args[:2] == ("bucket", "pfx")

    @pytest.mark.parametrize(
        "failing_step",
        ["translate_rule", "extract_values", "validate"],
    )
    def test_a_failure_at_any_step_becomes_information_not_found(self, failing_step):
        # This is a pipeline stage: raising would fail the whole document, where a
        # per-rule "could not evaluate" leaves the other rules' verdicts intact.
        adapter, system = _adapter()
        system.translator.translate_rule.return_value = _rule_json()
        system.extractor.extract_values.return_value = {}
        system.validate.return_value = _result("sat")
        if failing_step == "translate_rule":
            system.translator.translate_rule.side_effect = RuntimeError("boom")
        elif failing_step == "extract_values":
            system.extractor.extract_values.side_effect = RuntimeError("boom")
            system.translator.extract_values_with_llm.side_effect = RuntimeError("boom")
        else:
            system.validate.side_effect = RuntimeError("boom")

        with (
            patch.object(adapter, "_load_from_s3", return_value=None),
            patch.object(adapter, "_save_to_s3"),
        ):
            response = adapter.validate_rule(
                rule_description="rule",
                rule_type="Lending",
                extraction_results={"a": 1},
                document_text="t",
            )
        assert response["recommendation"] == "Information Not Found"
        assert response["_z3_error"] is True

    def test_the_error_marker_distinguishes_a_crash_from_a_genuine_not_found(self):
        # Without it, a solver that could not evaluate a rule and an adapter that
        # threw are indistinguishable in the published report.
        adapter, system = _adapter()
        system.translator.translate_rule.side_effect = RuntimeError("boom")
        with patch.object(adapter, "_load_from_s3", return_value=None):
            crashed = adapter.validate_rule("rule", "Lending", {"a": 1}, "t")
        genuine = Z3EngineAdapter._to_llm_response(
            _result("error", error="null parameter"), "Lending", "rule"
        )
        assert crashed["_z3_error"] is True
        assert "_z3_error" not in genuine
        assert crashed["recommendation"] == genuine["recommendation"]

    def test_the_failure_response_keeps_the_rule_identity_for_the_report(self):
        adapter, system = _adapter()
        system.translator.translate_rule.side_effect = RuntimeError("boom")
        with patch.object(adapter, "_load_from_s3", return_value=None):
            response = adapter.validate_rule(
                "my rule text", "LendingPolicy", {"a": 1}, "t"
            )
        assert response["rule"] == "my rule text"
        assert response["rule_type"] == "LendingPolicy"
        assert response["supporting_pages"] == []

    def test_the_failure_reasoning_names_the_underlying_error(self):
        adapter, system = _adapter()
        system.translator.translate_rule.side_effect = RuntimeError(
            "throttled by bedrock"
        )
        with patch.object(adapter, "_load_from_s3", return_value=None):
            response = adapter.validate_rule("rule", "Lending", {"a": 1}, "t")
        assert "throttled by bedrock" in response["reasoning"]

    def test_every_response_shape_carries_the_same_five_published_keys(self):
        # The orchestrator reads these unconditionally when building the report.
        expected = {
            "rule_type",
            "rule",
            "recommendation",
            "reasoning",
            "supporting_pages",
        }
        success, _, _ = self._run(outcome="sat")
        assert expected <= set(success)

        adapter, system = _adapter()
        system.translator.translate_rule.side_effect = RuntimeError("boom")
        with patch.object(adapter, "_load_from_s3", return_value=None):
            failure = adapter.validate_rule("rule", "Lending", {"a": 1}, "t")
        assert expected <= set(failure)

    def test_the_reasoning_is_json_serialisable_for_the_published_report(self):
        # The response is written to S3 as JSON, so a value that json.dumps cannot
        # handle would fail the write after every rule had already been evaluated.
        response, _, _ = self._run(outcome="sat")
        json.dumps(response)
