# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The run plan a message carries becomes ``runPlan`` in the execution input.

Every message the product sends carries a ``RunPlan`` attribute. The queue
processor decodes it, resolves the processing mode the run takes, and writes the
``runPlan`` block the state machine reads: where the run starts and which steps
it skips. A message with no plan, or with one this code cannot read, gets no
``runPlan`` key and starts at the first step, as every run did before run plans.
The plan never changes what an execution is named.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from test_idempotent_start import (  # noqa: F401 - pytest fixture
    FINALIZE_TOKEN,
    MESSAGE_ID,
    _record,
    index_module,
)

from idp_common.run_plan.models import RunKind, RunPlan, encode

#: Every key of the block, which is always written whole.
RUN_PLAN_KEYS = {
    "v",
    "kind",
    "startStep",
    "startAt",
    "skipAssessment",
    "skipPolicyRuleValidation",
    "skipSummarization",
    "skipEvaluation",
    "policyRuleValidationEnabled",
    "sections",
    "reviewed",
    "mode",
    "cycleId",
}


def _planned(plan=None, raw=None, message_id=MESSAGE_ID, finalize=None):
    """A record carrying a plan (or a raw attribute value)."""
    record = _record(message_id=message_id)
    attributes = {}
    if plan is not None or raw is not None:
        attributes["RunPlan"] = {
            "stringValue": encode(plan) if plan is not None else raw,
            "dataType": "String",
        }
    if finalize:
        attributes["ReviewFinalizeToken"] = {
            "stringValue": finalize,
            "dataType": "String",
        }
    if attributes:
        record["messageAttributes"] = attributes
    return record


def _input(index_module):  # noqa: F811
    return json.loads(index_module.sfn.start_execution.call_args.kwargs["input"])


def _configuration(
    index_module,  # noqa: F811
    monkeypatch,
    *,
    use_bda=False,
    project="arn:aws:bedrock:us-east-1:111122223333:data-automation-project/p",
    rule_validation=True,
    policies=({"$id": "lending"},),
):
    """A configuration table whose pinned revision reads as given."""
    monkeypatch.setenv("CONFIG_TABLE", "test-config-table")
    manager = MagicMock()
    manager.get_merged_configuration.return_value = SimpleNamespace(
        use_bda=use_bda,
        rule_validation=SimpleNamespace(enabled=rule_validation),
        policy_classes=list(policies),
    )
    manager.get_bda_project_arn.return_value = project
    index_module.ConfigurationManager = MagicMock(return_value=manager)
    return manager


def _reprocess(start, **fields):
    return RunPlan(kind=RunKind.REPROCESS, start=start, **fields)


class TestRunPlanOf:
    def test_a_valid_attribute_is_decoded(self, index_module):  # noqa: F811
        plan = _reprocess("summarization", skip=["evaluation"])

        assert index_module.run_plan_of(_planned(plan)) == plan

    def test_no_attribute_is_no_plan_and_no_warning(self, index_module):  # noqa: F811
        with patch.object(index_module.logger, "warning") as warning:
            assert index_module.run_plan_of(_record()) is None
        warning.assert_not_called()

    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            "[]",
            '{"v":2,"kind":"reprocess","start":"beginning"}',
            '{"v":1,"kind":"reprocess","start":"nowhere"}',
            '{"v":1,"kind":"reprocess","start":"beginning","extra":1}',
            '{"v":1,"kind":"new_upload","start":"extraction"}',
            "",
        ],
    )
    def test_an_invalid_attribute_is_ignored_with_a_warning(
        self,
        index_module,  # noqa: F811
        raw,
    ):
        with patch.object(index_module.logger, "warning") as warning:
            assert index_module.run_plan_of(_planned(raw=raw)) is None
        warning.assert_called_once()
        assert warning.call_args.args[0].startswith(
            "Ignoring an invalid RunPlan message attribute"
        )


class TestTheExecutionInput:
    def test_a_message_with_no_plan_has_no_run_plan_key(self, index_module):  # noqa: F811
        index_module.process_message(_record())

        assert "runPlan" not in _input(index_module)

    def test_an_invalid_plan_has_no_key_and_is_logged(self, index_module):  # noqa: F811
        with patch.object(index_module.logger, "warning") as warning:
            index_module.process_message(_planned(raw='{"v":2}'))

        assert "runPlan" not in _input(index_module)
        assert any(
            str(call.args[0]).startswith("Ignoring an invalid RunPlan")
            for call in warning.call_args_list
        )

    def test_a_valid_plan_writes_every_key(self, index_module, monkeypatch):  # noqa: F811
        _configuration(index_module, monkeypatch)
        plan = _reprocess("summarization", skip=["evaluation"])

        index_module.process_message(_planned(plan))

        block = _input(index_module)["runPlan"]
        assert set(block) == RUN_PLAN_KEYS
        assert block == {
            "v": 1,
            "kind": "reprocess",
            "startStep": "summarization",
            # Summarization enters at policy rule validation, with it skipped.
            "startAt": "policy_rule_validation",
            "skipAssessment": False,
            "skipPolicyRuleValidation": True,
            "skipSummarization": False,
            "skipEvaluation": True,
            "policyRuleValidationEnabled": True,
            "sections": [],
            "reviewed": False,
            "mode": "pipeline",
            "cycleId": "",
        }

    def test_the_document_and_the_review_block_are_as_without_a_plan(
        self,
        index_module,  # noqa: F811
    ):
        index_module.process_message(_planned(finalize=FINALIZE_TOKEN))
        without = _input(index_module)
        plan = RunPlan(
            kind=RunKind.REVIEW_COMPLETED,
            start="apply_review_changes",
            cycle_id="c-1",
            reviewed=True,
        )

        index_module.process_message(_planned(plan, finalize=FINALIZE_TOKEN))

        planned = _input(index_module)
        block = planned.pop("runPlan")
        assert planned == without
        assert (
            block["startAt"],
            block["kind"],
            block["cycleId"],
            block["reviewed"],
        ) == (
            "apply_review_changes",
            "review_completed",
            "c-1",
            True,
        )

    def test_the_sections_of_an_extraction_plan_reach_the_input(
        self,
        index_module,  # noqa: F811
    ):
        index_module.process_message(
            _planned(_reprocess("extraction", sections=["2", "5"]))
        )

        block = _input(index_module)["runPlan"]
        assert (block["startAt"], block["sections"]) == (
            "pipeline.extraction_sections",
            ["2", "5"],
        )


class TestTheModeTheRunTakes:
    def test_a_classification_plan_on_a_bda_profile_starts_at_the_beginning(
        self,
        index_module,  # noqa: F811
        monkeypatch,
    ):
        """One BDA call does OCR, classification and extraction, so the step is
        the beginning there. That is an alias, not a fallback: nothing is
        reported."""
        _configuration(index_module, monkeypatch, use_bda=True)
        index_module._emit_concurrency_event_metric = MagicMock()

        index_module.process_message(_planned(_reprocess("classification")))

        payload = _input(index_module)
        block = payload["runPlan"]
        assert (block["startAt"], block["startStep"], block["mode"]) == (
            "beginning",
            "beginning",
            "bda",
        )
        assert payload["document"]["use_bda"] is True
        index_module._emit_concurrency_event_metric.assert_not_called()

    def test_a_step_the_mode_does_not_have_falls_back_and_is_reported(
        self,
        index_module,  # noqa: F811
        monkeypatch,
    ):
        """The plan was built for pipeline mode and the run takes BDA mode, which
        has no assessment step. The run starts at the beginning, with a WARNING
        and a metric an operator can alarm on."""
        _configuration(index_module, monkeypatch, use_bda=True)
        index_module._emit_concurrency_event_metric = MagicMock()
        plan = RunPlan(kind=RunKind.REPROCESS, start="assessment")

        with patch.object(index_module.logger, "warning") as warning:
            index_module.process_message(_planned(plan))

        block = _input(index_module)["runPlan"]
        assert (block["startAt"], block["startStep"]) == ("beginning", "beginning")
        index_module._emit_concurrency_event_metric.assert_called_once_with(
            "RunPlanStartFallback"
        )
        assert any(
            "does not exist in bda mode" in str(call.args[0])
            for call in warning.call_args_list
        )

    def test_a_bda_profile_without_a_project_runs_in_pipeline_mode(
        self,
        index_module,  # noqa: F811
        monkeypatch,
    ):
        _configuration(index_module, monkeypatch, use_bda=True, project=None)

        index_module.process_message(_planned(_reprocess("classification")))

        payload = _input(index_module)
        block = payload["runPlan"]
        assert (block["mode"], block["startAt"]) == (
            "pipeline",
            "pipeline.classification",
        )
        assert payload["document"]["use_bda"] is False

    def test_no_configuration_table_is_pipeline_mode(self, index_module):  # noqa: F811
        index_module.process_message(_planned(_reprocess("assessment")))

        block = _input(index_module)["runPlan"]
        assert (block["mode"], block["startAt"]) == ("pipeline", "pipeline.assessment")
        assert block["policyRuleValidationEnabled"] is False

    def test_the_mode_is_read_at_the_pinned_revision(
        self,
        index_module,  # noqa: F811
        monkeypatch,
    ):
        manager = _configuration(index_module, monkeypatch)

        index_module.process_message(_planned(_reprocess("extraction")))

        manager.get_merged_configuration.assert_called_once_with("default", revision=3)


class TestPolicyRuleValidationEnabled:
    @pytest.mark.parametrize(
        ("rule_validation", "policies", "expected"),
        [
            (True, ({"$id": "lending"},), True),
            (False, ({"$id": "lending"},), False),
            # Enabled with no policies: there is nothing to validate.
            (True, (), False),
        ],
    )
    def test_it_follows_the_pinned_configuration(
        self,
        index_module,  # noqa: F811
        monkeypatch,
        rule_validation,
        policies,
        expected,
    ):
        _configuration(
            index_module,
            monkeypatch,
            rule_validation=rule_validation,
            policies=policies,
        )

        index_module.process_message(_planned(_reprocess("policy_rule_validation")))

        assert (
            _input(index_module)["runPlan"]["policyRuleValidationEnabled"] is expected
        )


class TestTheExecutionName:
    def test_a_finalize_message_is_named_the_same_with_and_without_a_plan(
        self,
        index_module,  # noqa: F811
    ):
        plan = RunPlan(
            kind=RunKind.REVIEW_COMPLETED,
            start="apply_review_changes",
            cycle_id="c-1",
            reviewed=True,
        )

        index_module.process_message(
            _planned(finalize=FINALIZE_TOKEN, message_id="m-1")
        )
        index_module.process_message(
            _planned(plan, finalize=FINALIZE_TOKEN, message_id="m-2")
        )

        names = [
            call.kwargs["name"]
            for call in index_module.sfn.start_execution.call_args_list
        ]
        assert names[0] == names[1]
        assert names[0].endswith(FINALIZE_TOKEN)

    def test_a_planned_message_is_named_from_its_message_id(self, index_module):  # noqa: F811
        index_module.process_message(_planned(_reprocess("extraction")))

        name = index_module.sfn.start_execution.call_args.kwargs["name"]
        assert name.endswith(MESSAGE_ID)
