# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the rule_validation Z3 data models: Parameter, PathMapping,
RuleJSON, RuleWithValues and ValidationResult.

These dataclasses validate in __post_init__, which makes them the first gate a
rule translated by an LLM passes through. Everything downstream -- variable
creation, constraint parsing, solving -- assumes they have already rejected a
malformed rule, so each rejection is asserted individually rather than by
sampling: a validator that silently accepts one bad shape moves the failure to
solve time, where the rule_id is all the operator gets.

Serialisation is checked as a round trip, because a rule JSON is persisted to S3
and re-read on a later invocation; a to_dict/from_dict pair that loses a field
turns a cached rule into a different rule.
"""

import pytest

from idp_common.rule_validation.z3.models import (
    Parameter,
    PathMapping,
    RuleJSON,
    RuleWithValues,
    ValidationResult,
)


@pytest.mark.unit
class TestParameter:
    """Parameter: name, type and required-flag validation."""

    @pytest.mark.parametrize("declared", ["Int", "Real", "Bool", "String"])
    def test_every_supported_type_is_accepted(self, declared):
        assert Parameter(name="p", type=declared).type == declared

    def test_required_defaults_to_true(self):
        assert Parameter(name="p", type="Int").required is True

    def test_underscores_and_digits_are_legal_in_a_name(self):
        assert Parameter(name="annual_income_2024", type="Real").name

    @pytest.mark.parametrize("name", ["", None])
    def test_empty_or_missing_name_is_rejected(self, name):
        with pytest.raises(ValueError, match="non-empty string"):
            Parameter(name=name, type="Int")

    @pytest.mark.parametrize(
        "name", ["has space", "has-hyphen", "has.dot", "a$b", "a(b)"]
    )
    def test_a_name_that_is_not_a_legal_smt_identifier_is_rejected(self, name):
        # These characters are the tokenizer's delimiters or operators, so a
        # parameter spelled this way could never be matched back out of a
        # constraint string.
        with pytest.raises(ValueError, match="alphanumeric"):
            Parameter(name=name, type="Int")

    @pytest.mark.parametrize("declared", ["int", "REAL", "Decimal", "float", ""])
    def test_an_unsupported_type_is_rejected_including_wrong_case(self, declared):
        with pytest.raises(ValueError, match="must be one of"):
            Parameter(name="p", type=declared)

    @pytest.mark.parametrize("required", ["true", 1, 0, None])
    def test_a_non_boolean_required_flag_is_rejected(self, required):
        # `required` decides whether a missing reading blocks evaluation, so a
        # truthy string here would silently make every parameter mandatory.
        with pytest.raises(ValueError, match="must be a boolean"):
            Parameter(name="p", type="Int", required=required)

    def test_to_dict_omits_an_absent_description(self):
        assert Parameter(name="p", type="Int").to_dict() == {
            "name": "p",
            "type": "Int",
            "required": True,
        }

    def test_to_dict_includes_a_present_description(self):
        assert (
            Parameter(name="p", type="Int", description="d").to_dict()["description"]
            == "d"
        )

    def test_from_dict_round_trips(self):
        original = Parameter(name="p", type="Real", required=False, description="d")
        assert Parameter.from_dict(original.to_dict()) == original

    def test_from_dict_defaults_required_to_true_when_absent(self):
        assert Parameter.from_dict({"name": "p", "type": "Int"}).required is True

    @pytest.mark.parametrize(
        "data,missing", [({"type": "Int"}, "name"), ({"name": "p"}, "type")]
    )
    def test_from_dict_names_the_missing_field(self, data, missing):
        with pytest.raises(ValueError, match=missing):
            Parameter.from_dict(data)


@pytest.mark.unit
class TestPathMapping:
    """PathMapping: dot-notation data path validation."""

    def test_a_well_formed_mapping_is_accepted(self):
        mapping = PathMapping(
            parameter_name="amount",
            data_path="documents.tax_bill.inference_result.amount",
        )
        assert mapping.data_path.endswith("amount")

    def test_a_single_segment_path_is_legal(self):
        assert PathMapping(parameter_name="a", data_path="amount").data_path == "amount"

    @pytest.mark.parametrize("value", ["", None])
    def test_empty_parameter_name_is_rejected(self, value):
        with pytest.raises(ValueError, match="parameter_name"):
            PathMapping(parameter_name=value, data_path="a.b")

    @pytest.mark.parametrize("value", ["", None])
    def test_empty_data_path_is_rejected(self, value):
        with pytest.raises(ValueError, match="data_path"):
            PathMapping(parameter_name="p", data_path=value)

    def test_consecutive_dots_are_rejected(self):
        # "a..b" would index an empty key and read None, which the extractor
        # cannot distinguish from a genuinely absent value.
        with pytest.raises(ValueError, match="consecutive dots"):
            PathMapping(parameter_name="p", data_path="a..b")

    @pytest.mark.parametrize("path", [".a.b", "a.b."])
    def test_a_leading_or_trailing_dot_is_rejected(self, path):
        with pytest.raises(ValueError, match="start or end with a dot"):
            PathMapping(parameter_name="p", data_path=path)

    def test_round_trips_through_dict(self):
        original = PathMapping(parameter_name="p", data_path="a.b")
        assert PathMapping.from_dict(original.to_dict()) == original

    @pytest.mark.parametrize(
        "data,missing",
        [
            ({"data_path": "a.b"}, "parameter_name"),
            ({"parameter_name": "p"}, "data_path"),
        ],
    )
    def test_from_dict_names_the_missing_field(self, data, missing):
        with pytest.raises(ValueError, match=missing):
            PathMapping.from_dict(data)


def _rule_kwargs(**overrides):
    kwargs = {
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
    return kwargs


@pytest.mark.unit
class TestRuleJSONRequiredFields:
    """RuleJSON: the non-empty string fields and the two list fields."""

    def test_a_minimal_workflow_b_rule_is_accepted(self):
        rule = RuleJSON(**_rule_kwargs())
        assert rule.path_mappings == []
        assert rule.metadata == {}
        assert rule.has_path_mappings() is False

    @pytest.mark.parametrize(
        "field", ["rule_id", "version", "description", "natural_language_rule"]
    )
    @pytest.mark.parametrize("value", ["", None])
    def test_each_string_field_must_be_a_non_empty_string(self, field, value):
        with pytest.raises(ValueError, match=field):
            RuleJSON(**_rule_kwargs(**{field: value}))

    def test_parameters_must_be_a_list(self):
        with pytest.raises(ValueError, match="parameters must be a list"):
            RuleJSON(**_rule_kwargs(parameters={"a": 1}))

    def test_an_empty_parameter_list_is_rejected(self):
        # A rule with no parameters reads nothing from the document, so it would
        # return the same verdict for every input.
        with pytest.raises(ValueError, match="cannot be empty"):
            RuleJSON(**_rule_kwargs(parameters=[]))

    def test_a_raw_dict_among_the_parameters_is_rejected(self):
        with pytest.raises(ValueError, match=r"parameters\[1\]"):
            RuleJSON(
                **_rule_kwargs(
                    parameters=[
                        Parameter(name="coverage", type="Real"),
                        {"name": "income", "type": "Real"},
                    ]
                )
            )

    def test_constraints_must_be_a_list(self):
        with pytest.raises(ValueError, match="constraints must be a list"):
            RuleJSON(**_rule_kwargs(constraints="(> x 1)"))

    def test_an_empty_constraint_list_is_rejected(self):
        # No constraints means nothing to solve; the solver would return sat for
        # any reading and the rule would always report PASS.
        with pytest.raises(ValueError, match="cannot be empty"):
            RuleJSON(**_rule_kwargs(constraints=[]))

    @pytest.mark.parametrize("bad", ["", "   ", None, 42])
    def test_a_blank_or_non_string_constraint_is_rejected(self, bad):
        with pytest.raises(ValueError, match=r"constraints\[0\]"):
            RuleJSON(**_rule_kwargs(constraints=[bad]))

    def test_the_index_of_the_offending_constraint_is_reported(self):
        with pytest.raises(ValueError, match=r"constraints\[1\]"):
            RuleJSON(**_rule_kwargs(constraints=["(> coverage 0)", ""]))

    def test_path_mappings_must_be_a_list(self):
        with pytest.raises(ValueError, match="path_mappings must be a list"):
            RuleJSON(**_rule_kwargs(path_mappings={"a": 1}))

    def test_a_raw_dict_among_the_path_mappings_is_rejected(self):
        with pytest.raises(ValueError, match=r"path_mappings\[0\]"):
            RuleJSON(
                **_rule_kwargs(
                    path_mappings=[{"parameter_name": "coverage", "data_path": "a"}]
                )
            )

    def test_metadata_must_be_a_dictionary(self):
        with pytest.raises(ValueError, match="metadata must be a dictionary"):
            RuleJSON(**_rule_kwargs(metadata=["a"]))


@pytest.mark.unit
class TestRuleJSONPathMappingBijection:
    """RuleJSON: the parameter <-> path-mapping correspondence (Workflow A)."""

    def _workflow_a(self, **overrides):
        kwargs = _rule_kwargs(
            path_mappings=[
                PathMapping(parameter_name="coverage", data_path="doc.coverage"),
                PathMapping(parameter_name="income", data_path="doc.income"),
            ]
        )
        kwargs.update(overrides)
        return RuleJSON(**kwargs)

    def test_one_mapping_per_parameter_is_accepted(self):
        rule = self._workflow_a()
        assert rule.has_path_mappings() is True
        assert len(rule.path_mappings) == 2

    def test_a_mapping_for_an_undeclared_parameter_is_rejected(self):
        with pytest.raises(ValueError, match="undeclared parameter: net_worth"):
            self._workflow_a(
                path_mappings=[
                    PathMapping(parameter_name="coverage", data_path="doc.coverage"),
                    PathMapping(parameter_name="income", data_path="doc.income"),
                    PathMapping(parameter_name="net_worth", data_path="doc.net_worth"),
                ]
            )

    def test_a_required_parameter_with_no_mapping_is_rejected(self):
        # Under Workflow A the mapping is the only way the value is read, so a
        # required parameter without one can never be populated.
        with pytest.raises(
            ValueError, match="'income' has no corresponding path mapping"
        ):
            self._workflow_a(
                path_mappings=[
                    PathMapping(parameter_name="coverage", data_path="doc.coverage")
                ]
            )

    def test_two_mappings_for_one_required_parameter_are_rejected(self):
        with pytest.raises(ValueError, match="multiple path mappings"):
            self._workflow_a(
                path_mappings=[
                    PathMapping(parameter_name="coverage", data_path="doc.a"),
                    PathMapping(parameter_name="coverage", data_path="doc.b"),
                    PathMapping(parameter_name="income", data_path="doc.income"),
                ]
            )

    def test_an_optional_parameter_may_have_no_mapping(self):
        # An unmapped optional parameter is a constant defined inside the
        # constraints rather than a reading from the document.
        rule = RuleJSON(
            **_rule_kwargs(
                parameters=[
                    Parameter(name="coverage", type="Real"),
                    Parameter(name="limit", type="Real", required=False),
                ],
                constraints=["(<= coverage limit)"],
                path_mappings=[
                    PathMapping(parameter_name="coverage", data_path="doc.coverage")
                ],
            )
        )
        assert len(rule.path_mappings) == 1

    def test_two_mappings_for_one_optional_parameter_are_still_rejected(self):
        with pytest.raises(ValueError, match="at most one"):
            RuleJSON(
                **_rule_kwargs(
                    parameters=[
                        Parameter(name="coverage", type="Real"),
                        Parameter(name="limit", type="Real", required=False),
                    ],
                    constraints=["(<= coverage limit)"],
                    path_mappings=[
                        PathMapping(
                            parameter_name="coverage", data_path="doc.coverage"
                        ),
                        PathMapping(parameter_name="limit", data_path="doc.a"),
                        PathMapping(parameter_name="limit", data_path="doc.b"),
                    ],
                )
            )

    def test_the_bijection_check_is_skipped_entirely_for_workflow_b(self):
        # With no mappings at all the rule is LLM-extracted, so "every required
        # parameter needs a mapping" must not fire.
        assert RuleJSON(**_rule_kwargs(path_mappings=[])).has_path_mappings() is False


@pytest.mark.unit
class TestRuleJSONSerialisation:
    """RuleJSON: to_dict / from_dict, the S3 persistence path."""

    def test_full_round_trip_preserves_every_field(self):
        original = RuleJSON(
            **_rule_kwargs(
                path_mappings=[
                    PathMapping(parameter_name="coverage", data_path="doc.coverage"),
                    PathMapping(parameter_name="income", data_path="doc.income"),
                ],
                metadata={"rule_type": "ratio", "created_at": "2026-01-01"},
            )
        )
        restored = RuleJSON.from_dict(original.to_dict())
        assert restored.to_dict() == original.to_dict()
        assert restored.parameters == original.parameters
        assert restored.path_mappings == original.path_mappings
        assert restored.metadata == original.metadata

    def test_to_dict_emits_parameters_and_mappings_as_plain_dicts(self):
        payload = RuleJSON(**_rule_kwargs()).to_dict()
        assert all(isinstance(p, dict) for p in payload["parameters"])
        assert payload["path_mappings"] == []

    @pytest.mark.parametrize(
        "field",
        [
            "rule_id",
            "version",
            "description",
            "natural_language_rule",
            "parameters",
            "constraints",
            "path_mappings",
        ],
    )
    def test_from_dict_names_each_missing_required_field(self, field):
        payload = RuleJSON(**_rule_kwargs()).to_dict()
        del payload[field]
        with pytest.raises(ValueError, match=field):
            RuleJSON.from_dict(payload)

    def test_from_dict_defaults_metadata_to_empty(self):
        payload = RuleJSON(**_rule_kwargs()).to_dict()
        del payload["metadata"]
        assert RuleJSON.from_dict(payload).metadata == {}

    def test_a_malformed_parameter_is_reported_as_a_parameter_failure(self):
        payload = RuleJSON(**_rule_kwargs()).to_dict()
        del payload["parameters"][0]["type"]
        with pytest.raises(ValueError, match="Failed to parse parameters"):
            RuleJSON.from_dict(payload)

    def test_a_malformed_path_mapping_is_reported_as_a_mapping_failure(self):
        payload = RuleJSON(**_rule_kwargs()).to_dict()
        payload["path_mappings"] = [{"parameter_name": "coverage"}]
        with pytest.raises(ValueError, match="Failed to parse path_mappings"):
            RuleJSON.from_dict(payload)


@pytest.mark.unit
class TestConstraintParameterReferences:
    """
    RuleJSON._validate_constraint_parameters: what it does and does not catch.

    The method is documented as validating that every parameter referenced in a
    constraint is declared, and as raising ValueError when one is not. It does
    neither: the loop over candidate tokens has no raise, so the whole method can
    be replaced with `return` without any test noticing. That is issue #1058.

    The cost is deferred detection rather than a missing check outright --
    Z3Validator._parse_smt_atom does reject the unknown atom -- but by then the
    rule has been translated and persisted, so one bad translation becomes an
    error on every document evaluated against the cached rule. These tests pin the
    current behaviour so this suite is honest about where the error is caught, and
    the xfail below is the signal for #1058.

    Note for whoever fixes #1058: a fix reds **two** tests here, not one. The strict
    xfail is the intended signal, and
    `test_an_undeclared_reference_is_not_rejected_at_construction_time` goes red with
    it, because its whole subject is that the rejection does not happen. Delete that
    one and drop the marker from its neighbour. Under option 2 in #1058 -- remove the
    method and its `Raises:` clause --
    `test_smt_keywords_in_a_constraint_are_not_mistaken_for_parameters` goes too,
    since it names a keyword-filtering behaviour that would no longer exist.
    """

    def test_a_constraint_referencing_only_declared_parameters_is_accepted(self):
        assert RuleJSON(**_rule_kwargs()).rule_id == "coverage_ratio"

    def test_smt_keywords_in_a_constraint_are_not_mistaken_for_parameters(self):
        rule = RuleJSON(
            **_rule_kwargs(
                parameters=[Parameter(name="flag", type="Bool")],
                constraints=["(and (not flag) true)"],
            )
        )
        assert rule.constraints == ["(and (not flag) true)"]

    def test_an_undeclared_reference_is_not_rejected_at_construction_time(self):
        # "incom" is a misspelling of "income" and no parameter declares it. The
        # rule is still built; Z3Validator raises when it cannot resolve the atom.
        # See test_z3_validator_smt.py::TestValidateFacade for that side.
        rule = RuleJSON(**_rule_kwargs(constraints=["(> incom 0)"]))
        assert rule.constraints == ["(> incom 0)"]

    @pytest.mark.xfail(
        strict=True,
        reason="_validate_constraint_parameters cannot raise: its loop body is "
        "`continue` plus two comments, so a misspelled parameter name is accepted "
        "at construction and first fails at solve time, after the rule has been "
        "translated and persisted. See "
        "https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1058",
    )
    def test_an_undeclared_reference_should_be_rejected_at_construction_time(self):
        with pytest.raises(ValueError, match="incom"):
            RuleJSON(**_rule_kwargs(constraints=["(> incom 0)"]))


@pytest.mark.unit
class TestRuleWithValues:
    """RuleWithValues: rule plus readings, the LLM-extraction carrier."""

    def _kwargs(self, **overrides):
        kwargs = {
            "rule_id": "coverage_ratio",
            "version": "1.0",
            "description": "Coverage must not exceed 20x income",
            "natural_language_rule": "coverage / income <= 20",
            "parameters": [
                Parameter(name="coverage", type="Real"),
                Parameter(name="income", type="Real"),
            ],
            "constraints": ["(<= (/ coverage income) 20)"],
            "extracted_values": {"coverage": 100.0, "income": 50.0},
        }
        kwargs.update(overrides)
        return kwargs

    def test_a_complete_instance_is_accepted(self):
        assert RuleWithValues(**self._kwargs()).extracted_values["income"] == 50.0

    def test_dict_parameters_are_coerced_to_parameter_objects(self):
        rule = RuleWithValues(
            **self._kwargs(
                parameters=[
                    {"name": "coverage", "type": "Real"},
                    {"name": "income", "type": "Real"},
                ]
            )
        )
        assert all(isinstance(p, Parameter) for p in rule.parameters)

    def test_an_empty_rule_id_is_rejected(self):
        with pytest.raises(ValueError, match="rule_id cannot be empty"):
            RuleWithValues(**self._kwargs(rule_id=""))

    def test_an_empty_parameter_list_is_rejected(self):
        with pytest.raises(ValueError, match="parameters list cannot be empty"):
            RuleWithValues(**self._kwargs(parameters=[]))

    def test_an_empty_constraint_list_is_rejected(self):
        with pytest.raises(ValueError, match="constraints list cannot be empty"):
            RuleWithValues(**self._kwargs(constraints=[]))

    def test_non_dict_extracted_values_are_rejected(self):
        with pytest.raises(ValueError, match="must be a dictionary"):
            RuleWithValues(**self._kwargs(extracted_values=[("coverage", 1.0)]))

    def test_a_required_parameter_with_no_reading_is_rejected_and_named(self):
        with pytest.raises(ValueError, match="income"):
            RuleWithValues(**self._kwargs(extracted_values={"coverage": 100.0}))

    def test_every_missing_required_reading_is_named(self):
        with pytest.raises(ValueError) as excinfo:
            RuleWithValues(**self._kwargs(extracted_values={}))
        message = str(excinfo.value)
        assert "coverage" in message and "income" in message

    def test_a_key_present_with_value_none_satisfies_the_presence_check(self):
        # Presence is checked by key, not by value; a null reading passes here and
        # is caught later by Z3Validator._check_null_values, which reports it as
        # an error rather than a failure.
        rule = RuleWithValues(
            **self._kwargs(extracted_values={"coverage": None, "income": 50.0})
        )
        assert rule.extracted_values["coverage"] is None

    def test_an_optional_parameter_needs_no_reading(self):
        rule = RuleWithValues(
            **self._kwargs(
                parameters=[
                    Parameter(name="coverage", type="Real"),
                    Parameter(name="limit", type="Real", required=False),
                ],
                extracted_values={"coverage": 100.0},
            )
        )
        assert "limit" not in rule.extracted_values

    def test_round_trips_through_dict(self):
        # Comparing to_dict() against to_dict() cannot detect a field the pair
        # drops SYMMETRICALLY -- deleting `metadata` from to_dict leaves such a
        # comparison green, which is the one thing a round-trip test exists to
        # catch. So the key set is asserted against the dataclass's own fields, and
        # the values are asserted attribute by attribute after the round trip.
        import dataclasses

        original = RuleWithValues(**self._kwargs(metadata={"source": "llm"}))
        payload = original.to_dict()
        assert set(payload) == {f.name for f in dataclasses.fields(original)}

        restored = RuleWithValues.from_dict(payload)
        assert restored.rule_id == original.rule_id
        assert restored.version == original.version
        assert restored.description == original.description
        assert restored.natural_language_rule == original.natural_language_rule
        assert restored.parameters == original.parameters
        assert restored.constraints == original.constraints
        assert restored.extracted_values == original.extracted_values
        assert restored.metadata == {"source": "llm"}
        assert (
            RuleWithValues.from_dict(original.to_dict()).to_dict() == original.to_dict()
        )

    def test_from_rule_json_carries_the_rule_identity_and_metadata_across(self):
        rule_json = RuleJSON(**_rule_kwargs(metadata={"rule_type": "ratio"}))
        combined = RuleWithValues.from_rule_json(
            rule_json, {"coverage": 100.0, "income": 50.0}
        )
        assert combined.rule_id == rule_json.rule_id
        assert combined.version == rule_json.version
        assert combined.constraints == rule_json.constraints
        assert combined.parameters == rule_json.parameters
        assert combined.metadata == {"rule_type": "ratio"}

    def test_from_rule_json_still_enforces_the_required_reading_check(self):
        rule_json = RuleJSON(**_rule_kwargs())
        with pytest.raises(ValueError, match="income"):
            RuleWithValues.from_rule_json(rule_json, {"coverage": 100.0})


@pytest.mark.unit
class TestValidationResult:
    """ValidationResult: the outcome/satisfied consistency rules."""

    def test_a_sat_result_is_accepted(self):
        result = ValidationResult(
            rule_id="r1", outcome="sat", satisfied=True, extracted_values={"a": 1}
        )
        assert result.passes() is True
        assert result.fails() is False
        assert result.is_success() is True
        assert result.is_error() is False

    def test_an_unsat_result_is_accepted(self):
        result = ValidationResult(
            rule_id="r1", outcome="unsat", satisfied=False, extracted_values={}
        )
        assert result.fails() is True
        assert result.passes() is False
        assert result.is_success() is True

    def test_an_error_result_is_accepted_and_is_neither_pass_nor_fail(self):
        # "could not evaluate" must not collapse into either verdict.
        result = ValidationResult(
            rule_id="r1",
            outcome="error",
            satisfied=False,
            extracted_values={},
            error_message="Required parameters have null values: a",
        )
        assert result.is_error() is True
        assert result.is_success() is False
        assert result.passes() is False
        assert result.fails() is False

    def test_passes_requires_the_outcome_as_well_as_the_flag(self):
        # An "error" result with satisfied=True is accepted by _validate (the
        # consistency rules only constrain sat and unsat), and it is the one shape
        # that distinguishes `satisfied and outcome == "sat"` from `satisfied`
        # alone. Without this case, passes() could be made to ignore the outcome
        # entirely and no test would notice.
        result = ValidationResult(
            rule_id="r1",
            outcome="error",
            satisfied=True,
            extracted_values={},
            error_message="partial evaluation",
        )
        assert result.passes() is False
        assert result.is_error() is True

    @pytest.mark.parametrize("rule_id", ["", None])
    def test_an_empty_rule_id_is_rejected(self, rule_id):
        with pytest.raises(ValueError, match="rule_id"):
            ValidationResult(
                rule_id=rule_id, outcome="sat", satisfied=True, extracted_values={}
            )

    @pytest.mark.parametrize("outcome", ["SAT", "pass", "unknown", ""])
    def test_an_outcome_outside_the_three_legal_values_is_rejected(self, outcome):
        with pytest.raises(ValueError, match="outcome must be one of"):
            ValidationResult(
                rule_id="r1", outcome=outcome, satisfied=True, extracted_values={}
            )

    def test_a_non_boolean_satisfied_flag_is_rejected(self):
        with pytest.raises(ValueError, match="satisfied must be a boolean"):
            ValidationResult(
                rule_id="r1", outcome="sat", satisfied="yes", extracted_values={}
            )

    def test_non_dict_extracted_values_are_rejected(self):
        with pytest.raises(ValueError, match="extracted_values must be a dictionary"):
            ValidationResult(
                rule_id="r1", outcome="sat", satisfied=True, extracted_values=[]
            )

    def test_a_non_numeric_execution_time_is_rejected(self):
        with pytest.raises(ValueError, match="execution_time_ms must be a number"):
            ValidationResult(
                rule_id="r1",
                outcome="sat",
                satisfied=True,
                extracted_values={},
                execution_time_ms="12ms",
            )

    def test_a_negative_execution_time_is_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            ValidationResult(
                rule_id="r1",
                outcome="sat",
                satisfied=True,
                extracted_values={},
                execution_time_ms=-1.0,
            )

    def test_sat_with_satisfied_false_is_rejected(self):
        # The pair is what callers branch on; an inconsistent pair would let one
        # reader see PASS and another see FAIL from the same result.
        with pytest.raises(ValueError, match="'sat' requires satisfied=True"):
            ValidationResult(
                rule_id="r1", outcome="sat", satisfied=False, extracted_values={}
            )

    def test_unsat_with_satisfied_true_is_rejected(self):
        with pytest.raises(ValueError, match="'unsat' requires satisfied=False"):
            ValidationResult(
                rule_id="r1", outcome="unsat", satisfied=True, extracted_values={}
            )

    def test_an_error_outcome_without_a_message_is_rejected(self):
        # An error with no explanation is indistinguishable from a bug in the
        # caller, so the message is mandatory.
        with pytest.raises(ValueError, match="requires error_message"):
            ValidationResult(
                rule_id="r1", outcome="error", satisfied=False, extracted_values={}
            )

    def test_to_dict_emits_every_field_including_the_nulls(self):
        payload = ValidationResult(
            rule_id="r1",
            outcome="unsat",
            satisfied=False,
            extracted_values={"a": 1},
            execution_time_ms=12.5,
        ).to_dict()
        assert payload == {
            "rule_id": "r1",
            "outcome": "unsat",
            "satisfied": False,
            "extracted_values": {"a": 1},
            "model": None,
            "error_message": None,
            "execution_time_ms": 12.5,
        }
