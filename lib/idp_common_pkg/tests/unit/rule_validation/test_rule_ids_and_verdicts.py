# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Stable policy rule ids, normalised verdicts, and verdicts for one section.

A policy rule was identified in every output only by its description, its
verdict was a free-text label, and a document with one unchunked section never
got a verdict at all: its report read "Unknown" for every rule. Review routing
names a rule and compares its verdict, so each of the three had to become
something code can rely on.
"""

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from idp_common.models import Document
from idp_common.rule_validation.models import FactExtractionResponse, LLMResponse
from idp_common.rule_validation.orchestrator import (
    VERDICTS,
    RuleValidationOrchestratorService,
    normalise_verdict,
)
from idp_common.rule_validation.service import RuleValidationService

pytestmark = pytest.mark.unit

_ORCHESTRATOR = {
    "model": "us.amazon.nova-lite-v1:0",
    "system_prompt": "You decide policy rule verdicts.",
    "task_prompt": (
        "Rule: {rule}\nPolicy: {policy_class}\nOptions: {recommendation_options}\n"
        "Evidence: {extracted_evidence}"
    ),
}

_POLICY_CLASSES = [
    {
        "$id": "invoice_controls",
        "x-aws-idp-policy-type": "invoice_controls",
        "rule_properties": {
            "total_matches_lines": {
                "description": "The amounts agree",
            },
            "tax_matches_rate": {
                # The same description under another key: a different rule.
                "description": "The amounts agree",
            },
            "threshold_check": {
                "description": "The total is under the limit",
                "x-aws-idp-validation-engine": "z3",
                "x-aws-idp-rule-id": "z3-limit-001",
                "x-aws-idp-rule-json": {"rule_id": "z3-limit-001", "parameters": []},
            },
            "undescribed": {"x-aws-idp-rule-id": "ignored"},
        },
    }
]


def _config(orchestrator=True) -> dict:
    rule_validation = {"enabled": True, "recommendation_options": "Pass, Fail"}
    if orchestrator:
        rule_validation["rule_validation_orchestrator"] = dict(_ORCHESTRATOR)
    return {"rule_validation": rule_validation, "policy_classes": _POLICY_CLASSES}


def _fact(rule_id, rule="The amounts agree", **extra):
    """A section's fact extraction for one rule: facts, and no verdict."""
    response = {
        "policy_type": "invoice_controls",
        "rule": rule,
        "extracted_facts": [{"fact": "Total 100", "citation": "1"}],
        "extraction_summary": "found",
    }
    if rule_id is not None:
        response["rule_id"] = rule_id
        response["z3_rule_id"] = None
    response.update(extra)
    return response


def _model_response(payload: dict) -> dict:
    text = f"<response>{json.dumps(payload)}</response>"
    return {"response": {"output": {"message": {"content": [{"text": text}]}}}}


class _Run:
    """One consolidation run against a stubbed S3 and a stubbed model."""

    def __init__(self, section_files: dict, verdicts: dict, config: dict):
        self.section_files = section_files
        self.verdicts = verdicts
        self.config = config
        self.prompts: list[str] = []
        self.written: dict[str, Any] = {}

    async def _invoke(self, **kwargs):
        prompt = kwargs["content"]
        self.prompts.append(prompt)
        # The evidence in the prompt names the rule the call is about.
        for rule_id, recommendation in self.verdicts.items():
            if f'"rule_id": "{rule_id}"' in prompt:
                return _model_response(
                    {
                        "policy_type": "invoice_controls",
                        "rule": "ignored: the orchestrator sets it",
                        "recommendation": recommendation,
                        "reasoning": f"decided {rule_id}",
                        "supporting_pages": ["1"],
                    }
                )
        return _model_response(
            {
                "recommendation": self.verdicts.get("*", "Pass"),
                "reasoning": "decided by text",
                "supporting_pages": [],
            }
        )

    def run(self) -> tuple[Document, dict]:
        service = RuleValidationOrchestratorService(self.config)
        service._invoke_model_async = self._invoke
        document = Document(id="doc-1", input_key="doc", output_bucket="bucket")
        uris = [f"s3://bucket/{key}" for key in self.section_files]

        with patch("idp_common.rule_validation.orchestrator.s3") as s3_mock:
            s3_mock.get_json_content.side_effect = lambda uri: self.section_files[
                uri.removeprefix("s3://bucket/")
            ]
            s3_mock.write_content.side_effect = lambda content, bucket, key, **kw: (
                self.written.__setitem__(key, content)
            )
            document = asyncio.run(
                service.consolidate_and_save_all(
                    document, self.config, section_uris=uris
                )
            )
        summary = self.written[
            "doc/rule_validation/consolidated/consolidated_summary.json"
        ]
        return document, summary


def _section(section_id: int, responses: list) -> tuple[str, dict]:
    key = f"doc/rule_validation/sections/section_{section_id}_responses.json"
    return key, {
        "section_id": str(section_id),
        "chunking_occurred": False,
        "responses": {"invoice_controls": responses},
    }


class TestRuleMetadata:
    def _metadata(self):
        service = RuleValidationService.__new__(RuleValidationService)
        return service._get_rule_metadata(_config(), "invoice_controls")

    def test_each_rule_carries_its_key_and_its_stable_id(self):
        metadata = self._metadata()

        assert [(m["rule_key"], m["policy_rule_id"]) for m in metadata] == [
            ("total_matches_lines", "invoice_controls.total_matches_lines"),
            ("tax_matches_rate", "invoice_controls.tax_matches_rate"),
            ("threshold_check", "invoice_controls.threshold_check"),
        ]

    def test_the_z3_id_stays_under_rule_id(self):
        by_key = {m["rule_key"]: m for m in self._metadata()}

        assert by_key["threshold_check"]["rule_id"] == "z3-limit-001"
        assert by_key["threshold_check"]["engine"] == "z3"
        assert by_key["total_matches_lines"]["rule_id"] is None

    def test_two_rules_with_one_description_have_two_ids(self):
        ids = {
            m["policy_rule_id"]
            for m in self._metadata()
            if m["description"] == "The amounts agree"
        }

        assert len(ids) == 2

    def test_the_legacy_discriminator_names_the_policy_type(self):
        config = _config()
        config["policy_classes"] = [
            {
                "x-aws-idp-rule-type": "legacy_policy",
                "rule_properties": {"signed": {"description": "Signed"}},
            }
        ]
        service = RuleValidationService.__new__(RuleValidationService)

        (entry,) = service._get_rule_metadata(config, "legacy_policy")

        assert entry["policy_rule_id"] == "legacy_policy.signed"


class TestResponseModels:
    def test_a_fact_extraction_carries_both_ids(self):
        response = FactExtractionResponse(
            policy_type="p", rule="r", rule_id="p.key", z3_rule_id="z3-1"
        )

        assert response.to_dict()["rule_id"] == "p.key"
        assert response.to_dict()["z3_rule_id"] == "z3-1"

    def test_a_verdict_carries_both_ids_and_the_normalised_verdict(self):
        response = LLMResponse(
            policy_type="p",
            rule="r",
            recommendation="Pass",
            reasoning="ok",
            rule_id="p.key",
            z3_rule_id="z3-1",
            verdict="pass",
        )

        assert response.to_dict() | {} == {
            "policy_type": "p",
            "rule": "r",
            "recommendation": "Pass",
            "reasoning": "ok",
            "supporting_pages": [],
            "rule_id": "p.key",
            "z3_rule_id": "z3-1",
            "verdict": "pass",
        }

    def test_the_ids_are_optional(self):
        assert FactExtractionResponse(policy_type="p", rule="r").rule_id is None
        assert (
            LLMResponse(
                policy_type="p", rule="r", recommendation="Pass", reasoning="x"
            ).verdict
            is None
        )

    @pytest.mark.parametrize("model", [FactExtractionResponse, LLMResponse])
    def test_an_unknown_field_is_still_refused(self, model):
        with pytest.raises(TypeError, match="confidence"):
            model(policy_type="p", rule="r", confidence=0.5)


class TestNormaliseVerdict:
    @pytest.mark.parametrize(
        ("label", "verdict"),
        [
            ("Pass", "pass"),
            (" pass ", "pass"),
            ("PASS", "pass"),
            ("Fail", "fail"),
            ("FAIL", "fail"),
            ("Information Not Found", "not_found"),
            ("information not found", "not_found"),
            ("Approve", "unknown"),
            ("Passed", "unknown"),
            ("Unknown", "unknown"),
            ("", "unknown"),
            (None, "unknown"),
            (True, "unknown"),
            (1, "unknown"),
        ],
    )
    def test_only_the_three_default_labels_are_recognised(self, label, verdict):
        assert normalise_verdict(label) == verdict

    def test_every_result_is_one_of_the_four_verdicts(self):
        assert set(VERDICTS) == {"pass", "fail", "not_found", "unknown"}


class TestConsolidatedSummary:
    def _summary(self, responses):
        service = RuleValidationOrchestratorService({})
        return service._generate_consolidated_summary({"invoice_controls": responses})

    def test_each_rule_entry_has_its_id_and_verdict(self):
        summary = self._summary(
            [
                {
                    "rule": "The amounts agree",
                    "rule_id": "invoice_controls.total_matches_lines",
                    "z3_rule_id": None,
                    "recommendation": "Fail",
                    "reasoning": "off by 3",
                    "supporting_pages": ["2"],
                }
            ]
        )

        assert summary["rule_details"]["invoice_controls"]["rules"] == [
            {
                "rule_id": "invoice_controls.total_matches_lines",
                "z3_rule_id": None,
                "rule": "The amounts agree",
                "recommendation": "Fail",
                "verdict": "fail",
                "supporting_pages": ["2"],
                "reasoning": "off by 3",
            }
        ]

    def test_verdict_counts_sum_to_the_rules_counted(self):
        labels = ["Pass", "pass", "Fail", "Information Not Found", "Approve", None]
        summary = self._summary(
            [
                {"rule": f"r{i}", "recommendation": label}
                if label is not None
                else {"rule": f"r{i}"}
                for i, label in enumerate(labels)
            ]
        )

        for statistics in (
            summary["overall_statistics"],
            summary["rule_details"]["invoice_controls"],
        ):
            assert statistics["verdict_counts"] == {
                "pass": 2,
                "fail": 1,
                "not_found": 1,
                "unknown": 2,
            }
            assert (
                sum(statistics["verdict_counts"].values()) == statistics["total_rules"]
            )

    def test_the_label_counts_are_unchanged(self):
        summary = self._summary(
            [
                {"rule": "a", "recommendation": "Pass"},
                {"rule": "b", "recommendation": "pass"},
                {"rule": "c", "recommendation": "Fail"},
            ]
        )

        statistics = summary["overall_statistics"]
        # The label counts stay exact-match, as every existing reader expects.
        assert statistics["recommendation_counts"] == {"Pass": 1, "pass": 1, "Fail": 1}
        assert (statistics["pass_count"], statistics["fail_count"]) == (1, 1)
        assert statistics["verdict_counts"]["pass"] == 2

    def test_an_empty_summary_has_zero_counts(self):
        summary = RuleValidationOrchestratorService({})._generate_consolidated_summary(
            {}
        )

        assert summary["overall_statistics"]["verdict_counts"] == dict.fromkeys(
            VERDICTS, 0
        )

    def test_the_report_shows_the_rule_id_after_the_rule_text(self):
        service = RuleValidationOrchestratorService({})
        summary = self._summary(
            [
                {
                    "rule": "The amounts agree",
                    "rule_id": "invoice_controls.total_matches_lines",
                    "recommendation": "pass",
                    "reasoning": "ok",
                },
                {"rule": "No id on this one", "recommendation": "Fail"},
            ]
        )

        markdown = service._format_summary_as_markdown(summary)

        assert (
            "The amounts agree (invoice_controls.total_matches_lines)</td>" in markdown
        )
        assert "No id on this one</td>" in markdown
        # The icon follows the normalised verdict, so "pass" reads as "Pass" does.
        assert "✅ pass</td>" in markdown
        assert "❌ Fail</td>" in markdown


class TestASingleSectionGetsVerdicts:
    def _single_section(self, **kwargs):
        key, body = _section(
            1,
            [
                _fact("invoice_controls.total_matches_lines"),
                _fact("invoice_controls.tax_matches_rate"),
            ],
        )
        return _Run(
            {key: body},
            {
                "invoice_controls.total_matches_lines": "Pass",
                "invoice_controls.tax_matches_rate": "Fail",
            },
            _config(**kwargs),
        )

    def test_each_rule_gets_a_real_verdict(self):
        run = self._single_section()

        _, summary = run.run()

        rules = summary["rule_details"]["invoice_controls"]["rules"]
        assert [(r["rule_id"], r["recommendation"], r["verdict"]) for r in rules] == [
            ("invoice_controls.total_matches_lines", "Pass", "pass"),
            ("invoice_controls.tax_matches_rate", "Fail", "fail"),
        ]
        assert summary["overall_statistics"]["verdict_counts"] == {
            "pass": 1,
            "fail": 1,
            "not_found": 0,
            "unknown": 0,
        }

    def test_the_verdict_pass_makes_one_call_per_rule(self):
        run = self._single_section()

        run.run()

        assert len(run.prompts) == 2

    def test_two_rules_with_one_description_stay_two_entries(self):
        run = self._single_section()

        _, summary = run.run()

        rules = summary["rule_details"]["invoice_controls"]["rules"]
        assert [r["rule"] for r in rules] == ["The amounts agree", "The amounts agree"]
        assert len({r["rule_id"] for r in rules}) == 2

    def test_without_an_orchestrator_the_facts_have_no_verdict(self):
        run = self._single_section(orchestrator=False)

        _, summary = run.run()

        assert run.prompts == []
        rules = summary["rule_details"]["invoice_controls"]["rules"]
        assert {r["verdict"] for r in rules} == {"unknown"}
        assert [r["rule_id"] for r in rules] == [
            "invoice_controls.total_matches_lines",
            "invoice_controls.tax_matches_rate",
        ]

    def test_the_policy_classification_result_is_kept(self):
        """One path now: a single section keeps what policy classification found."""
        key, body = _section(1, [_fact("invoice_controls.total_matches_lines")])
        run = _Run(
            {key: body}, {"invoice_controls.total_matches_lines": "Pass"}, _config()
        )
        service = RuleValidationOrchestratorService(run.config)
        service._invoke_model_async = run._invoke
        document = Document(id="doc-1", input_key="doc", output_bucket="bucket")
        document.rule_validation_result = MagicMock(
            matched_policy_types=["invoice_controls"], matched_page_ids={"x": "3"}
        )

        with patch("idp_common.rule_validation.orchestrator.s3") as s3_mock:
            s3_mock.get_json_content.return_value = body
            document = asyncio.run(
                service.consolidate_and_save_all(
                    document, run.config, section_uris=[f"s3://bucket/{key}"]
                )
            )

        assert document.rule_validation_result.matched_policy_types == [
            "invoice_controls"
        ]
        assert document.rule_validation_result.matched_page_ids == {"x": "3"}


class TestSeveralSections:
    def test_one_verdict_per_rule_across_sections(self):
        first = _section(1, [_fact("invoice_controls.total_matches_lines")])
        second = _section(2, [_fact("invoice_controls.total_matches_lines")])
        run = _Run(
            dict([first, second]),
            {"invoice_controls.total_matches_lines": "Fail"},
            _config(),
        )

        _, summary = run.run()

        rules = summary["rule_details"]["invoice_controls"]["rules"]
        assert [(r["rule_id"], r["verdict"]) for r in rules] == [
            ("invoice_controls.total_matches_lines", "fail")
        ]
        assert len(run.prompts) == 1


class TestSectionFilesWrittenBeforeIds:
    def test_they_still_consolidate_grouped_by_the_rule_text(self):
        first = _section(
            1,
            [
                _fact(None, rule="The amounts agree"),
                _fact(None, rule="The form is signed"),
            ],
        )
        second = _section(2, [_fact(None, rule="The amounts agree")])
        run = _Run(dict([first, second]), {"*": "Pass"}, _config())

        _, summary = run.run()

        rules = summary["rule_details"]["invoice_controls"]["rules"]
        assert [(r["rule"], r["rule_id"], r["verdict"]) for r in rules] == [
            ("The amounts agree", None, "pass"),
            ("The form is signed", None, "pass"),
        ]
        assert len(run.prompts) == 2


class TestZ3RulesAreMatchedByKey:
    def _service(self):
        service = RuleValidationOrchestratorService(_config())
        calls = []

        async def _single(
            compound_key, rule_id, policy_type, description, responses, config
        ):
            calls.append((compound_key, rule_id, len(responses)))
            return {
                "policy_type": policy_type,
                "rule": description,
                "recommendation": "Pass",
                "reasoning": "solved",
                "supporting_pages": [],
                "_z3_validated": True,
            }

        service._process_single_z3_rule = _single
        return service, calls

    def test_a_response_naming_the_z3_rule_is_solved(self):
        service, calls = self._service()
        responses = {
            "invoice_controls": [
                _fact(
                    "invoice_controls.threshold_check",
                    rule="A description that was edited since",
                    z3_rule_id="z3-limit-001",
                )
            ]
        }

        result = asyncio.run(
            service._process_z3_cross_section_rules(responses, _config())
        )

        assert calls == [
            (
                ("invoice_controls", "invoice_controls.threshold_check"),
                "z3-limit-001",
                1,
            )
        ]
        (verdict,) = result["invoice_controls"]
        assert verdict["rule_id"] == "invoice_controls.threshold_check"
        assert verdict["z3_rule_id"] == "z3-limit-001"
        assert verdict["recommendation"] == "Pass"

    def test_a_response_with_no_id_is_matched_by_its_description(self):
        service, calls = self._service()
        responses = {
            "invoice_controls": [_fact(None, rule="The total is under the limit")]
        }

        result = asyncio.run(
            service._process_z3_cross_section_rules(responses, _config())
        )

        assert [call[1] for call in calls] == ["z3-limit-001"]
        assert result["invoice_controls"][0]["rule_id"] == (
            "invoice_controls.threshold_check"
        )

    def test_an_llm_rule_with_a_z3_rules_description_is_not_solved(self):
        """The id decides. A shared description must not send an LLM rule to Z3."""
        service, calls = self._service()
        llm_rule = _fact(
            "invoice_controls.total_matches_lines", rule="The total is under the limit"
        )
        responses = {"invoice_controls": [llm_rule]}

        result = asyncio.run(
            service._process_z3_cross_section_rules(responses, _config())
        )

        assert calls == []
        assert result["invoice_controls"] == [llm_rule]

    def test_a_z3_rule_in_two_sections_gets_one_verdict(self):
        service, calls = self._service()
        z3_fact = _fact("invoice_controls.threshold_check", z3_rule_id="z3-limit-001")
        responses = {"invoice_controls": [dict(z3_fact), dict(z3_fact)]}

        result = asyncio.run(
            service._process_z3_cross_section_rules(responses, _config())
        )

        assert [call[2] for call in calls] == [2]
        assert len(result["invoice_controls"]) == 1
