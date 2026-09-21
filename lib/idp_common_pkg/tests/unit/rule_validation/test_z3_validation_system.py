# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for ValidationSystem, the standalone orchestrator over RuleTranslator,
DataExtractor and Z3Validator.

Its three components are stubbed. The point of these tests is not the components
-- each has its own suite -- but the wiring between them, which is where this class
makes every decision it makes: which extraction strategy is chosen, what happens
when one fails, which errors propagate unchanged and which are wrapped, and
whether a batch keeps going after a bad rule.

Two behaviours are worth stating up front because they are easy to get backwards
and neither is obvious from the call site:

* `extract()` falls back from path-based to LLM extraction, and when the fallback
  ALSO fails it raises the **original** path-based error, not the LLM one. A test
  that only checked "something raised" would not notice that inverting.
* `validate_batch()` turns a failing rule into an error `ValidationResult` and
  carries on, so the returned list is always one entry per input rule. Callers
  index results against rules positionally, so a batch that silently returned a
  shorter list would misattribute every verdict after the failure.

The Z3Validator is left real in the batch and end-to-end cases rather than
stubbed, because it is offline and deterministic, and a stub there would only be
asserting that the orchestrator calls a mock.
"""

from unittest.mock import MagicMock, patch

import pytest

from idp_common.rule_validation.z3.exceptions import (
    ExtractionError,
    TranslationError,
    ValidationError,
    ValidationSystemError,
)
from idp_common.rule_validation.z3.models import (
    Parameter,
    PathMapping,
    RuleJSON,
    ValidationResult,
)
from idp_common.rule_validation.z3.validation_system import ValidationSystem
from idp_common.rule_validation.z3.z3_validator import Z3Validator


def _rule(with_paths: bool = False, rule_id: str = "coverage_ratio") -> RuleJSON:
    return RuleJSON(
        rule_id=rule_id,
        version="1.0",
        description="Coverage must not exceed 20x income",
        natural_language_rule="coverage / income <= 20",
        parameters=[
            Parameter(name="coverage", type="Real"),
            Parameter(name="income", type="Real"),
        ],
        constraints=["(> income 0)", "(<= (/ coverage income) 20)"],
        path_mappings=[
            PathMapping(parameter_name="coverage", data_path="doc.coverage"),
            PathMapping(parameter_name="income", data_path="doc.income"),
        ]
        if with_paths
        else [],
    )


def _system(*, real_solver: bool = False) -> ValidationSystem:
    """A ValidationSystem with stubbed components and no constructor side effects."""
    system = object.__new__(ValidationSystem)
    system.translator = MagicMock()
    system.extractor = MagicMock()
    system.validator = Z3Validator(timeout_ms=5000) if real_solver else MagicMock()
    return system


@pytest.mark.unit
class TestConstruction:
    """__init__: the three components and how a construction failure surfaces."""

    def test_the_three_components_are_built_and_the_timeout_is_passed_through(self):
        with (
            patch(
                "idp_common.rule_validation.z3.validation_system.RuleTranslator"
            ) as translator,
            patch(
                "idp_common.rule_validation.z3.validation_system.DataExtractor"
            ) as extractor,
            patch(
                "idp_common.rule_validation.z3.validation_system.Z3Validator"
            ) as validator,
        ):
            ValidationSystem(z3_timeout_ms=1234, region="us-west-2")

        translator.assert_called_once()
        extractor.assert_called_once()
        validator.assert_called_once_with(timeout_ms=1234)
        assert translator.call_args.kwargs["region"] == "us-west-2"

    def test_prebuilt_configs_are_handed_to_the_translator(self):
        translator_config = object()
        extraction_config = object()
        with (
            patch(
                "idp_common.rule_validation.z3.validation_system.RuleTranslator"
            ) as translator,
            patch("idp_common.rule_validation.z3.validation_system.DataExtractor"),
            patch("idp_common.rule_validation.z3.validation_system.Z3Validator"),
        ):
            ValidationSystem(
                translator_config=translator_config,
                extraction_config=extraction_config,
            )
        kwargs = translator.call_args.kwargs
        assert kwargs["translator_config"] is translator_config
        assert kwargs["extraction_config"] is extraction_config

    def test_a_config_path_is_handed_to_the_translator(self):
        with (
            patch(
                "idp_common.rule_validation.z3.validation_system.RuleTranslator"
            ) as translator,
            patch("idp_common.rule_validation.z3.validation_system.DataExtractor"),
            patch("idp_common.rule_validation.z3.validation_system.Z3Validator"),
        ):
            ValidationSystem(translator_config_path="/tmp/cfg.yaml")
        assert translator.call_args.kwargs["config_path"] == "/tmp/cfg.yaml"

    def test_a_component_that_cannot_be_built_propagates_unchanged(self):
        # The constructor logs and re-raises rather than leaving a half-built
        # system whose next method call fails somewhere less informative.
        with patch(
            "idp_common.rule_validation.z3.validation_system.RuleTranslator",
            side_effect=RuntimeError("no config"),
        ):
            with pytest.raises(RuntimeError, match="no config"):
                ValidationSystem()


@pytest.mark.unit
class TestExtractStrategyChoice:
    """extract(): which of the two extraction strategies runs, and when."""

    def test_use_llm_goes_straight_to_the_llm_and_skips_paths(self):
        system = _system()
        system.translator.extract_values_with_llm.return_value = {"coverage": 1.0}
        assert system.extract(_rule(with_paths=True), {}, use_llm=True) == {
            "coverage": 1.0
        }
        system.extractor.extract_values.assert_not_called()

    def test_a_rule_with_no_path_mappings_uses_the_llm(self):
        system = _system()
        system.translator.extract_values_with_llm.return_value = {"coverage": 2.0}
        assert system.extract(_rule(with_paths=False), {}) == {"coverage": 2.0}
        system.extractor.extract_values.assert_not_called()

    def test_a_rule_with_path_mappings_uses_paths_and_does_not_call_the_model(self):
        system = _system()
        system.extractor.extract_values.return_value = {"coverage": 3.0}
        assert system.extract(_rule(with_paths=True), {"doc": {}}) == {"coverage": 3.0}
        system.translator.extract_values_with_llm.assert_not_called()

    def test_the_rule_and_data_reach_the_path_extractor_unchanged(self):
        system = _system()
        system.extractor.extract_values.return_value = {}
        rule, data = _rule(with_paths=True), {"doc": {"coverage": 1}}
        system.extract(rule, data)
        kwargs = system.extractor.extract_values.call_args.kwargs
        assert kwargs["rule_json"] is rule
        assert kwargs["data"] is data


@pytest.mark.unit
class TestExtractFallback:
    """extract(): the path-based -> LLM fallback and its error precedence."""

    def test_a_path_extraction_failure_falls_back_to_the_llm(self):
        system = _system()
        system.extractor.extract_values.side_effect = ExtractionError(
            message="path not found", operation="extract_values"
        )
        system.translator.extract_values_with_llm.return_value = {"coverage": 9.0}
        assert system.extract(_rule(with_paths=True), {}) == {"coverage": 9.0}

    def test_an_unexpected_path_extraction_error_also_falls_back(self):
        # Not only ExtractionError: a KeyError or TypeError out of the extractor is
        # still a reason to try the model rather than to fail the rule.
        system = _system()
        system.extractor.extract_values.side_effect = KeyError("doc")
        system.translator.extract_values_with_llm.return_value = {"coverage": 8.0}
        assert system.extract(_rule(with_paths=True), {}) == {"coverage": 8.0}

    def test_when_both_fail_the_original_path_error_is_raised_not_the_llm_one(self):
        # This is the precedence that matters: the path error names the data path
        # that was missing, which is actionable; the LLM error is usually a
        # throttle or a parse failure, which is not.
        system = _system()
        original = ExtractionError(
            message="doc.coverage missing", operation="extract_values"
        )
        system.extractor.extract_values.side_effect = original
        system.translator.extract_values_with_llm.side_effect = TranslationError(
            message="model unavailable", operation="extract_values_with_llm"
        )
        with pytest.raises(ExtractionError) as excinfo:
            system.extract(_rule(with_paths=True), {})
        assert excinfo.value is original

    def test_when_both_fail_after_an_unexpected_path_error_the_original_is_raised(self):
        system = _system()
        system.extractor.extract_values.side_effect = KeyError("doc")
        system.translator.extract_values_with_llm.side_effect = RuntimeError("boom")
        with pytest.raises(KeyError):
            system.extract(_rule(with_paths=True), {})

    def test_an_llm_only_failure_propagates_when_there_is_no_path_strategy(self):
        # With no path_mappings there is nothing to fall back from, so the model's
        # error is the only one and must not be swallowed.
        system = _system()
        system.translator.extract_values_with_llm.side_effect = TranslationError(
            message="model unavailable", operation="extract_values_with_llm"
        )
        with pytest.raises(TranslationError):
            system.extract(_rule(with_paths=False), {})


@pytest.mark.unit
class TestDeprecatedExtractAliases:
    """extract_with_paths / extract_with_llm: kept for callers that predate extract()."""

    def test_extract_with_paths_delegates_with_use_llm_false(self):
        system = _system()
        system.extractor.extract_values.return_value = {"coverage": 1.0}
        assert system.extract_with_paths(_rule(with_paths=True), {}) == {
            "coverage": 1.0
        }
        system.translator.extract_values_with_llm.assert_not_called()

    def test_extract_with_llm_delegates_with_use_llm_true(self):
        system = _system()
        system.translator.extract_values_with_llm.return_value = {"coverage": 1.0}
        assert system.extract_with_llm(_rule(with_paths=True), {}) == {"coverage": 1.0}
        system.extractor.extract_values.assert_not_called()


@pytest.mark.unit
class TestValidate:
    """validate(): the solver step and its error wrapping."""

    def test_a_satisfied_rule_comes_back_sat_with_a_measured_time(self):
        system = _system(real_solver=True)
        result = system.validate(_rule(), {"coverage": 100.0, "income": 50.0})
        assert result.outcome == "sat"
        assert result.satisfied is True
        assert result.execution_time_ms >= 0

    def test_a_violated_rule_comes_back_unsat(self):
        system = _system(real_solver=True)
        result = system.validate(_rule(), {"coverage": 10_000.0, "income": 1.0})
        assert result.outcome == "unsat"

    def test_the_total_time_replaces_the_solver_time(self):
        # The solver reports its own elapsed time; validate() overwrites it with
        # the wall time it measured, so a caller reading execution_time_ms sees
        # the cost of the call it made rather than of an inner step.
        system = _system()
        system.validator.validate.return_value = ValidationResult(
            rule_id="r1",
            outcome="sat",
            satisfied=True,
            extracted_values={},
            execution_time_ms=999_999.0,
        )
        assert system.validate(_rule(), {}).execution_time_ms < 999_999.0

    def test_a_validation_error_propagates_unwrapped(self):
        # ValidationError already carries the rule, constraints and values; wrapping
        # it would bury them one level deeper for no gain.
        system = _system()
        system.validator.validate.side_effect = ValidationError(
            message="bad constraint", operation="validate", rule_id="coverage_ratio"
        )
        with pytest.raises(ValidationError):
            system.validate(_rule(), {})

    def test_an_unexpected_error_is_wrapped_with_the_component_and_operation(self):
        system = _system()
        system.validator.validate.side_effect = RuntimeError("solver segfault")
        with pytest.raises(ValidationSystemError) as excinfo:
            system.validate(_rule(), {})
        assert excinfo.value.component == "ValidationSystem"
        assert excinfo.value.operation == "validate"
        assert excinfo.value.context["error_type"] == "RuntimeError"


@pytest.mark.unit
class TestValidateRule:
    """validate_rule(): translate, extract, solve."""

    def test_the_three_steps_run_in_order_and_the_arguments_are_forwarded(self):
        system = _system(real_solver=True)
        system.translator.translate_rule.return_value = _rule()
        system.extractor.extract_values.return_value = {
            "coverage": 100.0,
            "income": 50.0,
        }
        result = system.validate_rule(
            natural_language_rule="coverage / income <= 20",
            data_example={"coverage": 1, "income": 1},
            actual_data={"coverage": 100, "income": 50},
            rule_id="cr",
            version="2.0",
            description="d",
        )
        kwargs = system.translator.translate_rule.call_args.kwargs
        assert kwargs["rule_id"] == "cr"
        assert kwargs["version"] == "2.0"
        assert kwargs["description"] == "d"
        assert result.outcome == "sat"

    def test_extraction_uses_the_translated_rule_and_the_actual_data(self):
        system = _system(real_solver=True)
        translated = _rule()
        system.translator.translate_rule.return_value = translated
        system.extractor.extract_values.return_value = {
            "coverage": 1.0,
            "income": 1.0,
        }
        system.validate_rule("r", {"a": 1}, {"b": 2})
        kwargs = system.extractor.extract_values.call_args.kwargs
        assert kwargs["rule_json"] is translated
        assert kwargs["data"] == {"b": 2}

    def test_this_path_uses_the_extractor_directly_and_never_falls_back_to_the_llm(
        self,
    ):
        # Unlike validate_with_rule_json, validate_rule calls extractor.extract_values
        # rather than self.extract(), so an extraction failure here is terminal.
        system = _system()
        system.translator.translate_rule.return_value = _rule(with_paths=True)
        system.extractor.extract_values.side_effect = ExtractionError(
            message="missing", operation="extract_values"
        )
        with pytest.raises(ExtractionError):
            system.validate_rule("r", {}, {})
        system.translator.extract_values_with_llm.assert_not_called()

    @pytest.mark.parametrize(
        "error",
        [
            TranslationError(message="t", operation="translate_rule"),
            ExtractionError(message="e", operation="extract_values"),
            ValidationError(message="v", operation="validate"),
        ],
    )
    def test_the_three_known_error_types_propagate_unwrapped(self, error):
        system = _system()
        system.translator.translate_rule.side_effect = error
        with pytest.raises(type(error)):
            system.validate_rule("r", {}, {})

    def test_an_unexpected_error_is_wrapped_and_carries_a_truncated_rule_text(self):
        system = _system()
        system.translator.translate_rule.side_effect = RuntimeError("boom")
        with pytest.raises(ValidationSystemError) as excinfo:
            system.validate_rule("x" * 500, {}, {})
        assert excinfo.value.operation == "validate_rule"
        assert len(excinfo.value.context["natural_language_rule"]) == 200


@pytest.mark.unit
class TestValidateWithRuleJson:
    """validate_with_rule_json(): skip translation, keep the extraction fallback."""

    def test_no_translation_happens(self):
        system = _system(real_solver=True)
        system.extractor.extract_values.return_value = {
            "coverage": 100.0,
            "income": 50.0,
        }
        assert (
            system.validate_with_rule_json(_rule(with_paths=True), {}).outcome == "sat"
        )
        system.translator.translate_rule.assert_not_called()

    def test_this_path_does_get_the_llm_fallback(self):
        # It routes through self.extract(), so a path failure is recoverable here
        # where it is terminal in validate_rule.
        system = _system(real_solver=True)
        system.extractor.extract_values.side_effect = ExtractionError(
            message="missing", operation="extract_values"
        )
        system.translator.extract_values_with_llm.return_value = {
            "coverage": 100.0,
            "income": 50.0,
        }
        assert (
            system.validate_with_rule_json(_rule(with_paths=True), {}).outcome == "sat"
        )

    def test_an_extraction_error_propagates_unwrapped(self):
        system = _system()
        system.extractor.extract_values.side_effect = ExtractionError(
            message="missing", operation="extract_values"
        )
        system.translator.extract_values_with_llm.side_effect = ExtractionError(
            message="also missing", operation="extract_values_with_llm"
        )
        with pytest.raises(ExtractionError):
            system.validate_with_rule_json(_rule(with_paths=True), {})

    def test_an_unexpected_error_is_wrapped_with_this_operation_name(self):
        system = _system()
        system.extractor.extract_values.return_value = {}
        system.validator.validate.side_effect = RuntimeError("boom")
        with pytest.raises(ValidationSystemError) as excinfo:
            system.validate_with_rule_json(_rule(with_paths=True), {})
        assert excinfo.value.operation == "validate_with_rule_json"


@pytest.mark.unit
class TestValidateWithLlmExtraction:
    """validate_with_llm_extraction(): forced LLM extraction, then solve."""

    def test_the_path_extractor_is_not_consulted(self):
        system = _system(real_solver=True)
        system.translator.extract_values_with_llm.return_value = {
            "coverage": 100.0,
            "income": 50.0,
        }
        result = system.validate_with_llm_extraction(
            _rule(with_paths=True), "Coverage is 100 and income is 50"
        )
        assert result.outcome == "sat"
        system.extractor.extract_values.assert_not_called()

    def test_unstructured_data_is_passed_through_untouched(self):
        system = _system(real_solver=True)
        system.translator.extract_values_with_llm.return_value = {
            "coverage": 1.0,
            "income": 1.0,
        }
        system.validate_with_llm_extraction(_rule(), "free text")
        assert system.translator.extract_values_with_llm.call_args.kwargs["data"] == (
            "free text"
        )

    def test_a_translation_error_propagates_unwrapped(self):
        system = _system()
        system.translator.extract_values_with_llm.side_effect = TranslationError(
            message="model unavailable", operation="extract_values_with_llm"
        )
        with pytest.raises(TranslationError):
            system.validate_with_llm_extraction(_rule(), "text")

    def test_an_unexpected_error_is_wrapped_with_this_operation_name(self):
        system = _system()
        system.translator.extract_values_with_llm.side_effect = RuntimeError("boom")
        with pytest.raises(ValidationSystemError) as excinfo:
            system.validate_with_llm_extraction(_rule(), "text")
        assert excinfo.value.operation == "validate_with_llm_extraction"


@pytest.mark.unit
class TestValidateBatch:
    """validate_batch(): one result per rule, and the cache discipline."""

    def _batch_system(self):
        system = _system(real_solver=True)
        system.extractor.extract_values.return_value = {
            "coverage": 100.0,
            "income": 50.0,
        }
        return system

    def test_one_result_per_rule_in_input_order(self):
        system = self._batch_system()
        rules = [_rule(with_paths=True, rule_id=f"r{i}") for i in range(3)]
        results = system.validate_batch(rules, {})
        assert [r.rule_id for r in results] == ["r0", "r1", "r2"]

    def test_the_extraction_cache_is_cleared_once_per_rule(self):
        # The cache is keyed by data path, and the same path means different things
        # for different rules, so a cache carried across rules would return one
        # rule's reading for another's parameter.
        system = self._batch_system()
        system.validate_batch([_rule(with_paths=True) for _ in range(3)], {})
        assert system.extractor.clear_cache.call_count == 3

    def test_a_failing_rule_becomes_an_error_result_and_the_batch_continues(self):
        # Positional correspondence is what callers rely on; a shorter list would
        # misattribute every verdict after the failure.
        system = self._batch_system()
        rules = [_rule(with_paths=True, rule_id=f"r{i}") for i in range(3)]
        system.extractor.extract_values.side_effect = [
            {"coverage": 100.0, "income": 50.0},
            ExtractionError(message="missing", operation="extract_values"),
            {"coverage": 100.0, "income": 50.0},
        ]
        system.translator.extract_values_with_llm.side_effect = ExtractionError(
            message="also missing", operation="extract_values_with_llm"
        )
        results = system.validate_batch(rules, {})
        assert len(results) == 3
        assert [r.outcome for r in results] == ["sat", "error", "sat"]
        assert results[1].rule_id == "r1"
        assert results[1].error_message

    def test_an_unexpected_error_also_becomes_an_error_result(self):
        system = self._batch_system()
        system.extractor.extract_values.side_effect = RuntimeError("boom")
        system.translator.extract_values_with_llm.side_effect = RuntimeError("boom")
        results = system.validate_batch([_rule(with_paths=True)], {})
        assert results[0].outcome == "error"
        assert results[0].error_message is not None
        assert "Unexpected error" in results[0].error_message

    def test_stop_on_error_aborts_and_reports_the_position(self):
        system = self._batch_system()
        rules = [_rule(with_paths=True, rule_id=f"r{i}") for i in range(3)]
        system.extractor.extract_values.side_effect = [
            {"coverage": 100.0, "income": 50.0},
            ExtractionError(message="missing", operation="extract_values"),
            {"coverage": 100.0, "income": 50.0},
        ]
        system.translator.extract_values_with_llm.side_effect = ExtractionError(
            message="also missing", operation="extract_values_with_llm"
        )
        with pytest.raises(ValidationSystemError) as excinfo:
            system.validate_batch(rules, {}, stop_on_error=True)
        assert excinfo.value.context["rule_index"] == 2
        assert excinfo.value.context["total_rules"] == 3
        assert excinfo.value.context["completed_rules"] == 1

    def test_stop_on_error_also_aborts_on_an_unexpected_error(self):
        # The recorded error_type is ValidationSystemError rather than RuntimeError:
        # validate_with_rule_json has already wrapped the RuntimeError by the time
        # this loop sees it, so the batch's own "unexpected" branch reports the
        # wrapper. The original RuntimeError is named in the wrapped message.
        system = self._batch_system()
        system.extractor.extract_values.side_effect = RuntimeError("boom")
        system.translator.extract_values_with_llm.side_effect = RuntimeError("boom")
        with pytest.raises(ValidationSystemError) as excinfo:
            system.validate_batch([_rule(with_paths=True)], {}, stop_on_error=True)
        assert excinfo.value.context["error_type"] == "ValidationSystemError"
        assert "boom" in str(excinfo.value)

    def test_an_empty_rule_list_gives_an_empty_result_list(self):
        assert self._batch_system().validate_batch([], {}) == []


@pytest.mark.unit
class TestGetSummary:
    """get_summary(): the counts a caller reports."""

    def _results(self, outcomes):
        return [
            ValidationResult(
                rule_id=f"r{i}",
                outcome=outcome,
                satisfied=outcome == "sat",
                extracted_values={},
                error_message="e" if outcome == "error" else None,
                execution_time_ms=10.0,
            )
            for i, outcome in enumerate(outcomes)
        ]

    def test_the_three_outcomes_are_counted_separately(self):
        summary = ValidationSystem.get_summary(
            _system(), self._results(["sat", "sat", "unsat", "error"])
        )
        assert summary["total_rules"] == 4
        assert summary["satisfied_count"] == 2
        assert summary["unsatisfied_count"] == 1
        assert summary["error_count"] == 1

    def test_an_error_is_not_counted_as_unsatisfied(self):
        # "could not evaluate" and "evaluated and failed" are different findings,
        # and a summary that merged them would report a compliance failure that did
        # not happen.
        summary = ValidationSystem.get_summary(_system(), self._results(["error"]))
        assert summary["unsatisfied_count"] == 0
        assert summary["error_count"] == 1

    def test_times_are_summed_and_averaged(self):
        summary = ValidationSystem.get_summary(
            _system(), self._results(["sat", "unsat"])
        )
        assert summary["total_time_ms"] == 20.0
        assert summary["avg_time_ms"] == 10.0

    def test_the_satisfaction_rate_is_a_fraction_of_all_rules(self):
        summary = ValidationSystem.get_summary(
            _system(), self._results(["sat", "unsat", "error", "sat"])
        )
        assert summary["satisfaction_rate"] == 0.5

    def test_an_empty_result_list_gives_zeroes_rather_than_a_division_error(self):
        summary = ValidationSystem.get_summary(_system(), [])
        assert summary["total_rules"] == 0
        assert summary["avg_time_ms"] == 0.0
        assert summary["satisfaction_rate"] == 0.0
