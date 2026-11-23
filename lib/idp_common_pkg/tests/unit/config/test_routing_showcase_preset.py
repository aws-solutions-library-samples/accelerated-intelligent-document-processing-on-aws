# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The review routing showcase preset (``config_library/unified/review-routing-showcase``).

It loads as a configuration, and its routing table passes the validation a save
runs: the routing rules name inputs, paths, tests and reason placeholders that
exist for the preset's own classes and policy classes, and three review queues. A
save on a stack that lacks one of those queues is refused by name, which is why
the preset's README says to create them first.

The scenario documents of ``samples/review-routing`` are evaluated against the
table round by round (``TestTheScenarioRounds``), so a wrong order of the routing
rules shows here before it shows on a deployed stack.
"""

import copy
import json
import re
from pathlib import Path

import pytest
import yaml

import idp_common.config
from idp_common.config.configuration_manager import ConfigurationValidationError
from idp_common.config.models import IDPConfig
from idp_common.config.routing_models import review_queue_of
from idp_common.config.routing_reason import parse_reason
from idp_common.config.routing_validation import (
    QueueInfo,
    policy_rule_ids,
    validate_routing,
)
from idp_common.decision_tables.sfeel import display_values
from idp_common.models import Document, RuleValidationResult, Section
from idp_common.review_queues import routing_dmn
from idp_common.review_queues.routing_dmn import (
    KIND_NO_REVIEW,
    KIND_QUEUE,
    OUTPUT_OUTCOME,
    OUTPUT_QUEUE,
    expression_for,
)
from idp_common.review_queues.routing_inputs import build_inputs
from idp_common.review_queues.routing_step import evaluate_table, routing_table

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[5]
UNIFIED = REPO_ROOT / "config_library" / "unified"
PRESET_DIR = UNIFIED / "review-routing-showcase"
SCENARIO_DIR = REPO_ROOT / "samples" / "review-routing"
SYSTEM_DEFAULTS = Path(idp_common.config.__file__).parent / "system_defaults"

#: The queues the showcase needs besides Default, which every stack has.
SHOWCASE_QUEUES = ("specialist", "compliance", "approvals")
SCENARIO_RULES = [
    "scenario-po-without-purchase-order",
    "scenario-net-amount-check",
    "scenario-vendor-not-approved",
    "scenario-payment-terms-missing",
]
SHOWCASE_RULES = [
    "specialist-bank-checks",
    "compliance-policy-failure",
    "approvals-high-value",
    "default-w2-forms",
]
BUILT_IN_RULES = [
    "builtin-extraction-failed",
    "builtin-confidence-alerts",
    "builtin-missing-required",
]

SCENARIO_CLASS = "Scenario-Invoice"
#: The class no scenario document has a section of.
MISSING_CLASS = "Scenario-Purchase-Order"
SCENARIO_POLICY = "scenario_invoice_checks"
NET_AMOUNT_RULE = f"{SCENARIO_POLICY}.net_amount_matches"
NAME_REGEX = "x-aws-idp-document-name-regex"
THRESHOLD = "x-aws-idp-confidence-threshold"
#: The registered name a reviewer enters for the vendor printed as "AnyCo Office".
REGISTERED_VENDOR = "AnyCompany Office Supplies"

#: The samples the showcase routing rules are written for. One more is any W-2 of
#: ``samples/w2``.
SHOWCASE_SAMPLES = (
    "samples/lending_package.pdf",
    "samples/rule-validation/medicare_respiratory_pa_packet.pdf",
    "samples/fcc-invoices-7429ba503d96718740c2f714598b40a4.pdf",
)

#: The review rounds of each scenario document: the routing rule that places it
#: and the queue it goes to, then what the reviewer corrects before completing
#: that review. The last round of each document matches no routing rule.
SCENARIO_ROUNDS = {
    "one": [
        ("scenario-vendor-not-approved", "approvals", {"Vendor": REGISTERED_VENDOR}),
        ("", "", {}),
    ],
    "two": [
        ("scenario-net-amount-check", "compliance", {"NetAmountDue": 1100}),
        ("scenario-payment-terms-missing", "default", {"PaymentTerms": "Net 30"}),
        ("", "", {}),
    ],
    "three": [
        ("scenario-po-without-purchase-order", "approvals", {}),
        ("scenario-vendor-not-approved", "approvals", {"Vendor": REGISTERED_VENDOR}),
        ("scenario-payment-terms-missing", "default", {"PaymentTerms": "Net 30"}),
        ("", "", {}),
    ],
}

_KEY = "routing-scenario.pdf"
_SUMMARY_MD = f"s3://output/{_KEY}/rule_validation/consolidated/consolidated_summary.md"
_SUMMARY_JSON = _SUMMARY_MD[: -len(".md")] + ".json"


@pytest.fixture(scope="module")
def raw() -> dict:
    return yaml.safe_load((PRESET_DIR / "config.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def config(raw) -> IDPConfig:
    return IDPConfig.model_validate(raw)


@pytest.fixture(scope="module")
def truth() -> dict:
    """What each scenario invoice prints (``scenarios.truth.json``)."""
    return json.loads(
        (SCENARIO_DIR / "scenarios.truth.json").read_text(encoding="utf-8")
    )


def _lookup(*queue_ids):
    """A stack whose production queues are ``queue_ids`` and Default."""
    known = {"default", *queue_ids}

    def lookup(queue_id):
        if queue_id not in known:
            return None
        return QueueInfo(id=queue_id, name=queue_id.title(), enabled=True)

    return lookup


def _rules(config):
    (table,) = config.routing.tables
    return table.rules


def _by_id(config):
    return {rule.id: rule for rule in _rules(config)}


def _conditions(rule):
    return [(c.input, c.path, c.aggregate, c.test) for c in rule.conditions]


def _source(name: str) -> dict:
    return yaml.safe_load((UNIFIED / name / "config.yaml").read_text(encoding="utf-8"))


def _class(raw: dict, name: str) -> dict:
    (schema,) = [s for s in raw["classes"] if s["x-aws-idp-document-type"] == name]
    return schema


def _scenario_policy(raw: dict) -> dict:
    (policy,) = [
        p
        for p in raw["policy_classes"]
        if p["x-aws-idp-policy-type"] == SCENARIO_POLICY
    ]
    return policy


class TestThePresetLoads:
    def test_it_is_a_configuration_with_review_and_policy_rule_validation_on(
        self, config
    ):
        assert config.hitl.enabled is True
        assert config.rule_validation.enabled is True
        assert config.use_bda is False
        # High, so that the samples show confidence alerts among their reasons.
        assert config.hitl.confidence_threshold == 0.99

    def test_the_routing_rules_are_the_scenarios_the_showcase_then_the_built_in_ones(
        self, config
    ):
        rules = _rules(config)

        assert [rule.id for rule in rules] == [
            *SCENARIO_RULES,
            *SHOWCASE_RULES,
            *BUILT_IN_RULES,
        ]
        assert all(rule.enabled for rule in rules)
        queues = [
            "approvals",
            "compliance",
            "approvals",
            "default",
            "specialist",
            "compliance",
            "approvals",
            "default",
            "default",
            "default",
            "default",
        ]
        assert [[action.model_dump() for action in rule.actions] for rule in rules] == [
            [{"type": "review_queue", "queue": queue}] for queue in queues
        ]
        assert [review_queue_of(rule.actions) for rule in rules] == queues
        (table,) = config.routing.tables
        # A document no routing rule matches is not reviewed.
        assert table.default_outcome.actions == []

    def test_the_preset_is_written_with_actions(self, raw):
        """The single outcome is refused at save, so the file must not carry it."""
        (table,) = raw["routing"]["tables"]

        assert all("outcome" not in rule for rule in table["rules"])
        assert all("actions" in rule for rule in table["rules"])
        assert table["default_outcome"] == {"actions": []}

    def test_each_showcase_rule_tests_what_its_name_says(self, config):
        by_id = {
            rule.id: [
                condition
                for condition in _conditions(rule)
                if condition[0] != "reviewed"
            ]
            for rule in _rules(config)
        }

        assert by_id["specialist-bank-checks"] == [
            ("section_classes", "", "any", '"Bank-checks"')
        ]
        assert by_id["compliance-policy-failure"] == [
            ("failed_policy_rule_count", "", "any", "> 0")
        ]
        assert by_id["approvals-high-value"] == [
            ("field_value", "Invoice:GrossTotal", "any", "> 1000")
        ]
        assert by_id["default-w2-forms"] == [("section_classes", "", "any", '"W2"')]

    def test_each_scenario_routing_rule_tests_one_problem(self, config):
        by_id = {rule.id: _conditions(rule) for rule in _rules(config)}

        assert by_id["scenario-po-without-purchase-order"] == [
            ("reviewed", "", "any", "false"),
            ("field_value", f"{SCENARIO_CLASS}:InvoiceType", "any", '"PO-backed"'),
            ("class_present", MISSING_CLASS, "any", "false"),
        ]
        assert by_id["scenario-net-amount-check"] == [
            ("reviewed", "", "any", "false"),
            ("policy_rule_verdict", NET_AMOUNT_RULE, "any", '"fail"'),
        ]
        assert by_id["scenario-vendor-not-approved"] == [
            (
                "field_value",
                f"{SCENARIO_CLASS}:Vendor",
                "any",
                'not("AnyCompany Office Supplies", "Example Print Services")',
            )
        ]
        assert by_id["scenario-payment-terms-missing"] == [
            ("class_present", SCENARIO_CLASS, "any", "true"),
            ("field_value", f"{SCENARIO_CLASS}:PaymentTerms", "count", "0"),
        ]

    def test_a_routing_rule_matches_a_document_that_lacks_a_class(self, raw, config):
        """The missing-class condition: ``class_present`` of a class of the
        profile, with the test ``false``."""
        lacking = [
            (rule.id, condition.path)
            for rule in _rules(config)
            for condition in rule.conditions
            if condition.input == "class_present" and condition.test == "false"
        ]

        assert lacking == [("scenario-po-without-purchase-order", MISSING_CLASS)]
        assert _class(raw, MISSING_CLASS)["properties"].keys() == {"PONumber"}

    def test_no_sample_reaches_its_queue_through_a_confidence_score(self, config):
        """A confidence score comes from a model and differs between runs, so a
        routing rule that reads it cannot place a sample reliably. Each scenario
        and showcase routing rule comes before the built-in ones and reads
        something else."""
        rules = _rules(config)
        own = [rule for rule in rules if rule.id not in BUILT_IN_RULES]

        assert rules[: len(own)] == own
        for rule in own:
            assert "confidence_alert_count" not in [c.input for c in rule.conditions]

    def test_which_routing_rules_apply_only_before_the_first_review(self, config):
        """Every showcase routing rule, the first two scenario routing rules and
        the built-in ones start by requiring a document that has not been
        reviewed, so a completed review is not held by them again. The last two
        scenario routing rules do not read ``reviewed`` at all: they hold a
        reviewed document until the value they test is corrected."""
        by_id = _by_id(config)

        for rule_id in (*SCENARIO_RULES[:2], *SHOWCASE_RULES, *BUILT_IN_RULES):
            first = by_id[rule_id].conditions[0]
            assert (first.input, first.test) == ("reviewed", "false"), rule_id
        for rule_id in SCENARIO_RULES[2:]:
            inputs = [condition.input for condition in by_id[rule_id].conditions]
            assert "reviewed" not in inputs, rule_id

    def test_the_built_in_rules_are_the_ones_of_the_system_defaults(self, raw, config):
        defaults = {rule.id: rule for rule in _rules(IDPConfig())}
        by_id = _by_id(config)

        for rule_id in BUILT_IN_RULES:
            assert by_id[rule_id] == defaults[rule_id]
            assert by_id[rule_id].reason

        # And as the system defaults file writes them, reason included.
        written = yaml.safe_load(
            (SYSTEM_DEFAULTS / "base-routing.yaml").read_text(encoding="utf-8")
        )
        (default_table,) = written["routing"]["tables"]
        (table,) = raw["routing"]["tables"]
        assert [r for r in table["rules"] if r["id"] in BUILT_IN_RULES] == [
            r for r in default_table["rules"] if r["id"] in BUILT_IN_RULES
        ]
        assert [r["id"] for r in default_table["rules"]] == BUILT_IN_RULES


class TestItsReasons:
    def test_every_routing_rule_has_a_reason_with_a_placeholder(self, config):
        for rule in _rules(config):
            assert rule.reason.strip(), rule.id
            assert parse_reason(rule.reason), rule.id

    def test_each_scenario_reason_shows_the_values_its_problem_is_about(self, config):
        placeholders = {
            rule.id: [placeholder.text for placeholder in parse_reason(rule.reason)]
            for rule in _rules(config)
        }

        assert placeholders["scenario-po-without-purchase-order"] == [
            "{field_value:Scenario-Invoice:InvoiceType}"
        ]
        assert placeholders["scenario-net-amount-check"] == [
            "{field_value:Scenario-Invoice:NetAmountDue}",
            "{field_value:Scenario-Invoice:GrossTotal}",
            "{field_value:Scenario-Invoice:Discount}",
        ]
        assert placeholders["scenario-vendor-not-approved"] == [
            "{field_value:Scenario-Invoice:Vendor}",
            "{condition_1.value}",
        ]
        assert placeholders["scenario-payment-terms-missing"] == [
            "{field_value:Scenario-Invoice:InvoiceNumber}"
        ]

    def test_the_vendor_reason_lists_the_vendors_its_condition_accepts(self, config):
        """``{condition_1.value}`` is what the first condition compares with: the
        approved vendors, among them the name the reviewer corrects to."""
        rule = _by_id(config)["scenario-vendor-not-approved"]

        assert display_values(rule.conditions[0].test) == [
            REGISTERED_VENDOR,
            "Example Print Services",
        ]

    def test_the_default_outcome_has_no_reason(self, config):
        """It sends a document to no queue, so there is no reviewer to read one."""
        (table,) = config.routing.tables

        assert table.default_outcome.reason == ""


class TestItsClasses:
    def test_they_are_the_classes_of_three_presets_and_the_scenario_classes(self, raw):
        lending = _source("lending-package-sample")
        rules = _source("rule-validation")

        names = [schema["x-aws-idp-document-type"] for schema in raw["classes"]]
        assert names[0] == "Invoice"
        assert names[-2:] == [SCENARIO_CLASS, MISSING_CLASS]
        assert raw["classes"][1:-2] == [*lending["classes"], *rules["classes"]]
        assert raw["policy_classes"][:-1] == rules["policy_classes"]
        assert raw["policy_classes"][-1] == _scenario_policy(raw)
        assert len(names) == len(set(names))

    def test_the_invoice_is_the_fcc_invoice_with_no_confidence_alert(self, raw):
        """Its fields carry a threshold of 0, so an invoice is in review only
        because of its amount; nothing else about the class is changed."""
        (source,) = _source("realkie-fcc-verified")["classes"]
        invoice = raw["classes"][0]

        def without_thresholds(schema: dict) -> dict:
            copied = yaml.safe_load(yaml.safe_dump(schema))
            groups = [copied["properties"]]
            groups += [d["properties"] for d in copied.get("$defs", {}).values()]
            for group in groups:
                for field in group.values():
                    field.pop(THRESHOLD, None)
            return copied

        assert without_thresholds(invoice) == without_thresholds(source)
        leaves = [
            field
            for group in (
                invoice["properties"],
                *(d["properties"] for d in invoice["$defs"].values()),
            )
            for field in group.values()
            if field.get("type") not in ("object", "array")
        ]
        assert leaves and all(field[THRESHOLD] == "0.0" for field in leaves)
        assert invoice["properties"]["GrossTotal"]["type"] == "number"

    def test_the_scenario_invoice_has_the_fields_the_truth_file_records(
        self, raw, truth
    ):
        schema = _class(raw, SCENARIO_CLASS)
        properties = schema["properties"]

        assert truth["document_class"] == SCENARIO_CLASS
        assert {name: field["type"] for name, field in properties.items()} == {
            "Vendor": "string",
            "InvoiceNumber": "string",
            "InvoiceType": "string",
            "PaymentTerms": "string",
            "GrossTotal": "number",
            "Discount": "number",
            "NetAmountDue": "number",
        }
        # No confidence alert: a scenario document is in review for its problems.
        assert all(field[THRESHOLD] == "0.0" for field in properties.values())
        assert schema["required"] == ["Vendor", "InvoiceNumber", "GrossTotal"]
        for document in truth["documents"]:
            printed = document["fields"]
            assert list(printed) == list(properties), document["file"]
            for name, value in printed.items():
                if value is None:
                    # A field a page does not print is never a required one.
                    assert name not in schema["required"], document["file"]
                elif properties[name]["type"] == "number":
                    assert isinstance(value, (int, float)), (document["file"], name)
                else:
                    assert isinstance(value, str), (document["file"], name)

    def test_a_scenario_document_gets_its_class_and_its_policy_from_its_name(
        self, raw, truth
    ):
        """The class of a scenario document must not depend on a model, and the
        scenario policy must be the only one that checks it."""
        scenario_policy = _scenario_policy(raw)
        named = {
            schema["x-aws-idp-document-type"]: schema[NAME_REGEX]
            for schema in raw["classes"]
            if schema.get(NAME_REGEX)
        }

        assert list(named) == [SCENARIO_CLASS]
        assert NAME_REGEX not in _class(raw, MISSING_CLASS)
        files = [document["file"] for document in truth["documents"]]
        assert len(files) == len(SCENARIO_ROUNDS)
        for file in files:
            assert (SCENARIO_DIR / file).is_file(), file
            assert re.search(named[SCENARIO_CLASS], file), file
            checked_by = [
                policy["x-aws-idp-policy-type"]
                for policy in raw["policy_classes"]
                if re.search(policy[NAME_REGEX], file)
            ]
            assert checked_by == [SCENARIO_POLICY], file
        # Neither pattern reaches one of the other samples.
        others = [Path(sample).name for sample in SHOWCASE_SAMPLES]
        others += [path.name for path in (REPO_ROOT / "samples" / "w2").iterdir()]
        for name in others:
            assert not re.search(named[SCENARIO_CLASS], name), name
            assert not re.search(scenario_policy[NAME_REGEX], name), name

    def test_the_scenario_policy_has_the_policy_rule_a_routing_rule_reads(
        self, raw, config
    ):
        policy = _scenario_policy(raw)

        assert policy["$id"] == SCENARIO_POLICY
        assert list(policy["rule_properties"]) == ["net_amount_matches"]
        assert NET_AMOUNT_RULE in policy_rule_ids(config)


class TestItsRoutingValidates:
    def test_with_the_three_queues_it_is_accepted(self, config):
        validate_routing(config, queue_lookup=_lookup(*SHOWCASE_QUEUES))

    def test_without_a_queue_lookup_it_is_accepted(self, config):
        validate_routing(config)

    @pytest.mark.parametrize(
        ("missing", "rule_id"),
        [
            ("specialist", "specialist-bank-checks"),
            ("compliance", "scenario-net-amount-check"),
            ("approvals", "scenario-po-without-purchase-order"),
        ],
    )
    def test_a_stack_without_one_of_the_queues_refuses_it_by_name(
        self, config, missing, rule_id
    ):
        """The first routing rule, in table order, that names the queue."""
        present = [queue for queue in SHOWCASE_QUEUES if queue != missing]

        with pytest.raises(ConfigurationValidationError) as refused:
            validate_routing(config, queue_lookup=_lookup(*present))

        message = str(refused.value)
        assert f"'{missing}'" in message
        assert f"rule '{rule_id}'" in message

    def test_a_stack_with_only_the_default_queue_refuses_the_first_rule(self, config):
        with pytest.raises(ConfigurationValidationError, match="'approvals'"):
            validate_routing(config, queue_lookup=_lookup())

    def test_the_missing_class_routing_rule_needs_the_class_in_the_profile(self, raw):
        """Which is why the preset defines a class no sample is of."""
        changed = copy.deepcopy(raw)
        changed["classes"] = [
            schema
            for schema in changed["classes"]
            if schema["x-aws-idp-document-type"] != MISSING_CLASS
        ]

        with pytest.raises(ConfigurationValidationError) as refused:
            validate_routing(IDPConfig.model_validate(changed))

        message = str(refused.value)
        assert "rule 'scenario-po-without-purchase-order'" in message
        assert f"'{MISSING_CLASS}' is not a class of this profile" in message

    def test_the_net_amount_routing_rule_needs_the_scenario_policy(self, raw):
        changed = copy.deepcopy(raw)
        changed["policy_classes"] = [
            policy
            for policy in changed["policy_classes"]
            if policy["x-aws-idp-policy-type"] != SCENARIO_POLICY
        ]

        with pytest.raises(ConfigurationValidationError) as refused:
            validate_routing(IDPConfig.model_validate(changed))

        message = str(refused.value)
        assert "rule 'scenario-net-amount-check'" in message
        assert "not in the Policy Schema" in message

    def test_a_reason_is_checked_against_the_fields_of_the_scenario_class(self, raw):
        changed = copy.deepcopy(raw)
        scenario = _class(changed, SCENARIO_CLASS)
        del scenario["properties"]["InvoiceNumber"]
        scenario["required"].remove("InvoiceNumber")

        with pytest.raises(ConfigurationValidationError) as refused:
            validate_routing(IDPConfig.model_validate(changed))

        message = str(refused.value)
        assert "rule 'scenario-payment-terms-missing'" in message
        assert "'{field_value:Scenario-Invoice:InvoiceNumber}'" in message

    @pytest.mark.parametrize("dmn_version", ["1.5", "1.3"])
    def test_the_table_is_the_same_after_a_dmn_export_and_import(
        self, config, dmn_version
    ):
        """Every test is written the way an export writes it, so the table that
        comes back is the table that went out, reasons included. DMN 1.3 writes
        a list of values with a space after each comma."""
        exported = routing_dmn.export_xml(config.routing, dmn_version=dmn_version)

        back = routing_dmn.import_xml(exported)

        assert back.model_dump()["tables"] == config.routing.model_dump()["tables"]


class _Stored:
    """The objects a run reads from S3, by URI."""

    def __init__(self, objects: dict):
        self._objects = objects

    def get_json_content(self, uri: str):
        return self._objects[uri]


def _decide(config, sections, *, pages=1, reviewed=False, verdicts=None):
    """What the preset's table decides for a document, read as a run reads it.

    ``sections`` is one ``(class, extracted values)`` per section, ``verdicts``
    the verdict of each policy rule by id, and ``reviewed`` whether a completed
    review has confirmed every section. The values are put where a run stores
    them, in each section's result and in the policy rule validation summary.
    The routing step's own reader (``build_inputs``) builds the table's inputs
    from there, keyed by the text of each condition's column
    (``expression_for``), and its own evaluator (``evaluate_table``) decides.
    """
    stored = {}
    document_sections = []
    for number, (class_name, values) in enumerate(sections, start=1):
        uri = f"s3://output/{_KEY}/sections/{number}/result.json"
        result = {"inference_result": dict(values), "explainability_info": [{}]}
        if reviewed:
            result["review"] = {"confirmed": True}
        stored[uri] = result
        document_sections.append(
            Section(
                section_id=str(number),
                classification=class_name,
                page_ids=[str(number)],
                extraction_result_uri=uri,
            )
        )
    details: dict = {}
    for rule_id, verdict in (verdicts or {}).items():
        policy = details.setdefault(rule_id.partition(".")[0], {"rules": []})
        policy["rules"].append({"rule_id": rule_id, "verdict": verdict})
    stored[_SUMMARY_JSON] = {"rule_details": details}
    document = Document(
        id=_KEY,
        input_key=_KEY,
        num_pages=pages,
        sections=document_sections,
        rule_validation_result=RuleValidationResult(
            request_id=_KEY, output_uri=_SUMMARY_MD
        ),
    )
    table = routing_table(config)

    built = build_inputs(document, config, table, mode="pipeline", s3=_Stored(stored))

    # Every condition of the table read a value under its own column, and every
    # class, field and policy rule the table names was found.
    assert built.stale == []
    assert set(built.values) == {
        expression_for(condition)
        for rule in table.rules
        for condition in rule.conditions
    }
    return evaluate_table(table, built.values)


def _landing(result) -> tuple:
    """The routing rule that matched and the queue it names; empty for none."""
    if not result.matched_rule_id:
        assert result.default_used
        assert result.outputs[OUTPUT_OUTCOME] == KIND_NO_REVIEW
        return "", ""
    assert result.outputs[OUTPUT_OUTCOME] == KIND_QUEUE
    return result.matched_rule_id, result.outputs[OUTPUT_QUEUE]


def _net_amount_verdict(printed: dict) -> str:
    """The verdict of the policy rule ``net_amount_matches`` for what a page prints."""
    adds_up = printed["NetAmountDue"] == printed["GrossTotal"] - printed["Discount"]
    return "pass" if adds_up else "fail"


def _scenario(truth: dict, scenario: str) -> dict:
    (document,) = [d for d in truth["documents"] if d["scenario"] == scenario]
    return document


class TestTheScenarioRounds:
    """Each scenario document, evaluated against the preset's table round by round.

    A round is one evaluation of the table: the first after the document is
    processed, the next after each completed review. A document goes to review
    for the first routing rule that matches, so it returns once for each problem
    that remains, and the order of the routing rules decides which problem its
    reviewer reads about. ``SCENARIO_ROUNDS`` is that sequence.

    The table, its conditions, their order and their aggregates are the
    preset's, and the reader and the evaluator are the routing step's own. What
    the simulation assumes is what the other steps of a run produce:

    - Classification: the document is one section of the class
      ``Scenario-Invoice`` (its file name matches the class's name pattern), and
      it has no ``Scenario-Purchase-Order`` section.
    - Extraction: the section's values are what the page prints
      (``scenarios.truth.json``), and a field the page does not print has no
      value. No field raises a confidence alert.
    - Policy rule validation, in every round: ``net_amount_matches`` has the
      verdict ``fail`` when the printed net amount due is not the printed gross
      total minus the printed discount, and ``pass`` otherwise. The verdict does
      not follow a reviewer's correction, because policy rule validation reads
      the page text as well, and the page does not change.
    - A completed review: every section is confirmed, so ``reviewed`` is true
      from the second round on, and the reviewer's corrections are the
      section's values. A class cannot be corrected.
    """

    @pytest.mark.parametrize("scenario", list(SCENARIO_ROUNDS))
    def test_each_round_lands_on_the_routing_rule_of_the_next_problem(
        self, config, truth, scenario
    ):
        document = _scenario(truth, scenario)
        verdict = _net_amount_verdict(document["fields"])
        values = dict(document["fields"])

        for number, (rule_id, queue, correction) in enumerate(
            SCENARIO_ROUNDS[scenario], start=1
        ):
            result = _decide(
                config,
                [(truth["document_class"], values)],
                pages=document["page_count"],
                reviewed=number > 1,
                verdicts={NET_AMOUNT_RULE: verdict},
            )

            assert _landing(result) == (rule_id, queue), f"round {number}"
            values.update(correction)

    def test_the_documents_have_one_two_and_three_problems_within_the_limit(
        self, config
    ):
        """A document is placed once per problem. The one with three reaches
        the placement limit, and none goes over it: above the limit a run
        continues without another review."""
        placements = {
            scenario: len([rule_id for rule_id, _, _ in rounds if rule_id])
            for scenario, rounds in SCENARIO_ROUNDS.items()
        }

        assert placements == {"one": 1, "two": 2, "three": 3}
        assert max(placements.values()) == config.routing.max_consecutive_placements
        for rounds in SCENARIO_ROUNDS.values():
            # The last round, and only the last, matches no routing rule.
            assert [rule_id for rule_id, _, _ in rounds if not rule_id] == [""]
            assert rounds[-1] == ("", "", {})

    def test_a_value_left_uncorrected_holds_the_document_again(self, config, truth):
        """The vendor routing rule has no ``reviewed`` condition, so completing
        the review without correcting the vendor brings the document back."""
        document = _scenario(truth, "one")

        result = _decide(
            config,
            [(truth["document_class"], document["fields"])],
            reviewed=True,
            verdicts={NET_AMOUNT_RULE: _net_amount_verdict(document["fields"])},
        )

        assert _landing(result) == ("scenario-vendor-not-approved", "approvals")

    @pytest.mark.parametrize(
        ("sections", "verdicts", "rule_id", "queue"),
        [
            (
                [("Payslip", {}), ("Bank-checks", {}), ("W2", {})],
                {},
                "specialist-bank-checks",
                "specialist",
            ),
            (
                [("PA-Administrative", {})],
                {"bundling.bundling_hcpcs": "fail"},
                "compliance-policy-failure",
                "compliance",
            ),
            (
                [("Invoice", {"GrossTotal": 2500.0})],
                {},
                "approvals-high-value",
                "approvals",
            ),
            ([("W2", {})], {}, "default-w2-forms", "default"),
        ],
        ids=["lending-package", "prior-authorization", "invoice", "w2"],
    )
    def test_no_scenario_routing_rule_takes_a_showcase_sample(
        self, config, sections, verdicts, rule_id, queue
    ):
        """The scenario routing rules come first in the table. A document with
        no ``Scenario-Invoice`` section passes all four and lands where its own
        routing rule sends it."""
        result = _decide(config, sections, verdicts=verdicts)

        assert _landing(result) == (rule_id, queue)


class TestTheReadme:
    @pytest.fixture(scope="class")
    def readme(self) -> str:
        return (PRESET_DIR / "README.md").read_text(encoding="utf-8")

    def test_it_names_the_queues_the_samples_and_the_rules(self, config, readme):
        for queue in SHOWCASE_QUEUES:
            assert f"--id {queue}" in readme
        for rule in _rules(config):
            assert f"`{rule.id}`" in readme
        for sample in SHOWCASE_SAMPLES:
            assert sample in readme
            assert (REPO_ROOT / sample).is_file(), sample
        assert any((REPO_ROOT / "samples" / "w2").iterdir())
        assert "is not reviewed" in readme

    def test_it_names_the_scenario_documents_and_their_rounds(self, truth, readme):
        """The rounds the README lists are the rounds the simulation proves."""
        for document in truth["documents"]:
            file = document["file"]
            assert f"`samples/review-routing/{file}`" in readme
            for number, (rule_id, queue, _) in enumerate(
                SCENARIO_ROUNDS[document["scenario"]], start=1
            ):
                landing = f"`{rule_id}` | `{queue}`" if rule_id else "none"
                assert f"| `{file}` | {number} | {landing} |" in readme
        assert "returns to review once for each problem that remains" in readme

    def test_it_explains_reasons_and_the_missing_class_routing_rule(
        self, config, readme
    ):
        by_id = _by_id(config)

        # The reason it quotes is the one the preset holds.
        assert by_id["scenario-vendor-not-approved"].reason in readme
        for placeholder in (
            "{input}",
            "{input:path}",
            "{condition_N.value}",
            "{confidence_threshold}",
        ):
            assert f"`{placeholder}`" in readme
        assert "`class_present`" in readme
        assert f"`{MISSING_CLASS}`" in readme

    def test_it_calls_the_tab_by_its_name(self, readme):
        assert "Routing Rules tab" in readme
        assert "Routing** tab" not in readme
