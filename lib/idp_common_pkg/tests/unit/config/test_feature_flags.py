# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``idp_common.config.feature_flags``: one answer per stage for every caller.

The two assembly Lambdas return ``policy_rule_validation_enabled`` to the state
machine and the queue processor writes it into the run plan. They must agree, or a
run that starts after assembly would take a different branch than a run that
starts at the beginning. ``step_enabled`` gives the answer of every optional step
by step id, each by the rule the step itself reads.
"""

import ast
import copy
from pathlib import Path

import pytest

from idp_common.config import feature_flags
from idp_common.config.feature_flags import (
    confidence_enabled,
    policy_rule_validation_enabled,
    review_routing_enabled,
    step_enabled,
)
from idp_common.config.models import IDPConfig

_POLICY = {"$id": "Policy", "rule_properties": {}}


def _as_dict(config):
    return copy.deepcopy(config)


def _as_model(config):
    return IDPConfig.model_validate(copy.deepcopy(config))


#: The two forms every function accepts.
_FORMS = [pytest.param(_as_dict, id="dict"), pytest.param(_as_model, id="model")]


@pytest.mark.unit
class TestPolicyRuleValidationEnabled:
    def test_enabled_with_policies(self):
        config = IDPConfig.model_validate(
            {"rule_validation": {"enabled": True}, "policy_classes": [_POLICY]}
        )

        assert policy_rule_validation_enabled(config) is True

    def test_enabled_without_policies_is_off(self):
        config = IDPConfig.model_validate({"rule_validation": {"enabled": True}})

        assert policy_rule_validation_enabled(config) is False

    def test_disabled_with_policies_is_off(self):
        config = IDPConfig.model_validate(
            {"rule_validation": {"enabled": False}, "policy_classes": [_POLICY]}
        )

        assert policy_rule_validation_enabled(config) is False

    def test_no_configuration_is_off(self):
        assert policy_rule_validation_enabled(None) is False

    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            pytest.param(
                {"rule_validation": {"enabled": True}, "policy_classes": [_POLICY]},
                True,
                id="enabled-with-policies",
            ),
            pytest.param(
                {"rule_validation": {"enabled": "false"}, "policy_classes": [_POLICY]},
                False,
                id="string-false",
            ),
            pytest.param(
                {"rule_validation": {"enabled": "true"}, "policy_classes": [_POLICY]},
                True,
                id="string-true",
            ),
            pytest.param(
                {"rule_validation": {"enabled": True}, "policy_classes": []},
                False,
                id="no-policies",
            ),
            pytest.param(
                {"rule_validation": {"enabled": True}}, False, id="policies-absent"
            ),
            pytest.param({"policy_classes": [_POLICY]}, True, id="model-default-is-on"),
        ],
    )
    def test_the_dict_form_agrees_with_the_model(self, config, expected):
        assert policy_rule_validation_enabled(config) is expected
        # The dict form and the model built from it give the same answer.
        assert (
            policy_rule_validation_enabled(IDPConfig.model_validate(config)) is expected
        )


@pytest.mark.unit
class TestReviewRoutingEnabled:
    @pytest.mark.parametrize(
        "config",
        [
            pytest.param(None, id="no-configuration"),
            pytest.param({}, id="empty-dict"),
            pytest.param({"hitl": {}}, id="hitl-without-enabled"),
            pytest.param({"hitl": {"confidence_threshold": 0.5}}, id="other-setting"),
            pytest.param(IDPConfig.model_validate({}), id="model-default"),
        ],
    )
    def test_unset_is_off(self, config):
        assert review_routing_enabled(config) is False

    @pytest.mark.parametrize("form", _FORMS)
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(True, True), (False, False), ("true", True), ("false", False)],
    )
    def test_it_is_hitl_enabled(self, form, value, expected):
        config = form({"hitl": {"enabled": value}})

        assert review_routing_enabled(config) is expected

    def test_the_model_answer_is_the_model_field(self):
        for value in (True, False):
            config = _as_model({"hitl": {"enabled": value}})

            assert review_routing_enabled(config) is config.hitl.enabled

    def test_the_private_copy_in_routing_is_gone(self):
        from idp_common.review_queues import routing, routing_step

        assert not hasattr(routing, "_hitl_enabled")
        assert not hasattr(routing_step, "_hitl_enabled")
        # Both read the flag through this module.
        assert routing.review_routing_enabled is review_routing_enabled
        assert routing_step.review_routing_enabled is review_routing_enabled


@pytest.mark.unit
class TestConfidenceEnabled:
    @pytest.mark.parametrize(
        "config",
        [
            pytest.param(None, id="no-configuration"),
            pytest.param({}, id="empty-dict"),
            pytest.param({"extraction": {}}, id="extraction-without-confidence"),
            pytest.param({"extraction": {"confidence": {}}}, id="confidence-empty"),
            pytest.param(IDPConfig.model_validate({}), id="model-default"),
        ],
    )
    def test_unset_is_on(self, config):
        assert confidence_enabled(config) is True

    @pytest.mark.parametrize("form", _FORMS)
    @pytest.mark.parametrize(
        ("confidence", "expected"),
        [
            pytest.param({"mode": "off"}, False, id="mode-off"),
            pytest.param({"mode": " OFF "}, False, id="mode-off-as-typed"),
            pytest.param({"mode": "separate"}, True, id="mode-separate"),
            pytest.param({"mode": "integrated"}, True, id="mode-integrated"),
            pytest.param({"mode": ""}, True, id="mode-blank-is-the-default"),
            pytest.param({"enabled": False}, False, id="older-enabled-false"),
            pytest.param({"enabled": "false"}, False, id="older-enabled-string"),
            pytest.param(
                {"enabled": False, "mode": "integrated"},
                False,
                id="older-enabled-false-beats-a-scoring-mode",
            ),
            pytest.param(
                {"enabled": True, "mode": "off"}, False, id="mode-off-beats-enabled"
            ),
            pytest.param({"enabled": True}, True, id="enabled-true"),
        ],
    )
    def test_it_is_the_value_the_model_derives_from_the_mode(
        self, form, confidence, expected
    ):
        config = {"extraction": {"confidence": confidence}}

        assert confidence_enabled(form(config)) is expected
        # The rule is the model's: the assessment service reads this field.
        assert _as_model(config).extraction.confidence.enabled is expected


_ALL_ON = {
    "extraction": {"confidence": {"mode": "separate"}},
    "rule_validation": {"enabled": True},
    "policy_classes": [_POLICY],
    "hitl": {"enabled": True},
    "summarization": {"enabled": True},
    "evaluation": {"enabled": True},
}
_STEP_IDS = (
    "assessment",
    "policy_rule_validation",
    "review_routing",
    "summarization",
    "evaluation",
)


@pytest.mark.unit
class TestStepEnabled:
    @pytest.mark.parametrize(
        "config",
        [
            pytest.param(None, id="no-configuration"),
            pytest.param({}, id="empty-dict"),
            pytest.param(IDPConfig.model_validate({}), id="model-default"),
        ],
    )
    def test_an_unset_configuration_reads_as_the_model_defaults(self, config):
        assert step_enabled(config) == {
            "assessment": True,
            "policy_rule_validation": False,
            "review_routing": False,
            "summarization": True,
            "evaluation": True,
        }

    @pytest.mark.parametrize("form", _FORMS)
    def test_every_step_on(self, form):
        assert step_enabled(form(_ALL_ON)) == dict.fromkeys(_STEP_IDS, True)

    @pytest.mark.parametrize("form", _FORMS)
    @pytest.mark.parametrize(
        ("step_id", "switched_off"),
        [
            pytest.param(
                "assessment",
                {"extraction": {"confidence": {"mode": "off"}}},
                id="assessment-extraction-confidence-mode-off",
            ),
            pytest.param(
                "policy_rule_validation",
                {"rule_validation": {"enabled": False}},
                id="policy-rule-validation-disabled",
            ),
            pytest.param(
                "policy_rule_validation",
                {"policy_classes": []},
                id="policy-rule-validation-without-policies",
            ),
            pytest.param(
                "review_routing", {"hitl": {"enabled": False}}, id="review-routing"
            ),
            pytest.param(
                "summarization",
                {"summarization": {"enabled": False}},
                id="summarization",
            ),
            pytest.param(
                "evaluation", {"evaluation": {"enabled": False}}, id="evaluation"
            ),
        ],
    )
    def test_switching_one_step_off_changes_only_its_key(
        self, form, step_id, switched_off
    ):
        config = form({**_ALL_ON, **switched_off})

        assert step_enabled(config) == {key: key != step_id for key in _STEP_IDS}

    @pytest.mark.parametrize("form", _FORMS)
    @pytest.mark.parametrize("section", ["summarization", "evaluation"])
    def test_a_section_without_enabled_is_on(self, form, section):
        config = form({section: {}})

        assert step_enabled(config)[section] is True

    @pytest.mark.parametrize(
        "config",
        [
            pytest.param({}, id="defaults"),
            pytest.param(_ALL_ON, id="all-on"),
            pytest.param(
                {
                    "extraction": {"confidence": {"mode": "off"}},
                    "hitl": {"enabled": False},
                    "summarization": {"enabled": False},
                    "evaluation": {"enabled": False},
                },
                id="all-off",
            ),
        ],
    )
    def test_each_value_is_the_field_the_step_reads(self, config):
        model = _as_model(config)
        enabled = step_enabled(model)

        # assessment/service.py, summarization/service.py, the evaluation
        # function and routing_step.decide read these fields of the model.
        assert enabled["assessment"] is model.extraction.confidence.enabled
        assert enabled["summarization"] is model.summarization.enabled
        assert enabled["evaluation"] is model.evaluation.enabled
        assert enabled["review_routing"] is model.hitl.enabled
        # Both assembly Lambdas return this function's answer.
        assert enabled["policy_rule_validation"] is policy_rule_validation_enabled(
            model
        )

    def test_the_keys_are_the_optional_steps_of_the_catalogue(self):
        from idp_common.run_plan.steps import DISPLAY_STEPS, REVIEW_ROUTING, RUN_STEPS

        optional = {step.id for step in RUN_STEPS if not step.required}

        # Review Routing is not a step a run can start at, so the list of run
        # steps does not hold it: it is a display step of the catalogue.
        assert REVIEW_ROUTING in {display.id for display in DISPLAY_STEPS}
        assert set(step_enabled(None)) == optional | {REVIEW_ROUTING}
        assert tuple(step_enabled(None)) == _STEP_IDS

    def test_the_module_does_not_import_the_run_plan(self):
        # ``run_plan`` imports ``config``; the step ids are literals here.
        tree = ast.parse(Path(feature_flags.__file__).read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module or "")

        assert modules
        assert not [name for name in modules if "run_plan" in name]
