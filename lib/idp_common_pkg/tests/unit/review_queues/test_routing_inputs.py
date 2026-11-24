# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The values a routing table reads from a document (``routing_inputs``).

Each input of the catalogue, in both processing modes; what a confirmed and an
excluded section contribute; and what is read from S3 to get them: only what the
table's enabled rules use, each section result once, in parallel.
"""

import threading
from types import SimpleNamespace

import pytest

from idp_common.config.routing_models import (
    BUILTIN_CONFIDENCE_REASON,
    RoutingCondition,
    RoutingRule,
    RoutingTable,
    queue_actions,
)
from idp_common.config.routing_validation import ROUTING_INPUTS
from idp_common.decision_tables import evaluate
from idp_common.review_queues import routing_inputs
from idp_common.review_queues.routing_dmn import expression_for, to_decision_table
from idp_common.review_queues.routing_inputs import (
    FailedPolicyRule,
    InputReader,
    RoutingInputs,
    build_inputs,
    load_section_facts,
)

pytestmark = pytest.mark.unit

BUCKET = "out"
KEY = "claims/packet.pdf"
SUMMARY_MD = f"s3://{BUCKET}/{KEY}/rule_validation/consolidated/consolidated_summary.md"
SUMMARY_JSON = SUMMARY_MD[:-3] + ".json"

INVOICE_SCHEMA = {
    "x-aws-idp-document-type": "Invoice",
    "type": "object",
    "required": ["Vendor", "Total"],
    "properties": {
        "Vendor": {"type": "string"},
        "Total": {"type": "number"},
        "Lines": {
            "type": "array",
            "items": {"type": "object", "properties": {"Amount": {"type": "number"}}},
        },
    },
}
RECEIPT_SCHEMA = {
    "x-aws-idp-document-type": "Receipt",
    "type": "object",
    "properties": {"Total": {"type": "number"}},
}


class _S3:
    """Section results and the policy summary, by URI; records every read."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.reads = []
        self.threads = set()
        self.barrier = None
        self._lock = threading.Lock()

    def get_json_content(self, uri):
        with self._lock:
            self.reads.append(uri)
            self.threads.add(threading.current_thread().name)
        if self.barrier is not None and "/sections/" in uri:
            # Every section read must be in flight at once to get past this.
            self.barrier.wait(timeout=10)
        if uri not in self.objects:
            raise FileNotFoundError(uri)
        return self.objects[uri]


def _uri(section_id):
    return f"s3://{BUCKET}/{KEY}/sections/{section_id}/result.json"


def _alert(name="Total", confidence=0.4, threshold=0.8):
    return {
        "attribute_name": name,
        "confidence": confidence,
        "confidence_threshold": threshold,
    }


def _section(section_id, classification="Invoice", *, alerts=(), result=True, **more):
    fields = dict(
        section_id=section_id,
        classification=classification,
        extraction_result_uri=_uri(section_id) if result else None,
        confidence_threshold_alerts=list(alerts),
        confidence_alert_total=None,
        missing_required_fields=[],
        confidence=None,
        blueprint_unscored=False,
        excluded=False,
    )
    fields.update(more)
    return SimpleNamespace(**fields)


def _result(values, *, confirmed=False, explainability=None):
    result = {"inference_result": values, "explainability_info": explainability or [{}]}
    if confirmed:
        result["review"] = {"v": 1, "cycle_id": "c1", "confirmed": True}
    return result


def _document(*sections, pages=3, policy=False):
    return SimpleNamespace(
        input_key=KEY,
        num_pages=pages,
        sections=list(sections),
        rule_validation_result=SimpleNamespace(output_uri=SUMMARY_MD)
        if policy
        else None,
    )


def _config(*, route_missing=None, threshold=0.8):
    return SimpleNamespace(
        classes=[INVOICE_SCHEMA, RECEIPT_SCHEMA],
        hitl=SimpleNamespace(
            enabled=True,
            confidence_threshold=threshold,
            route_missing_required_fields=route_missing,
        ),
    )


def _condition(name, path="", aggregate="any", test="-"):
    return RoutingCondition(input=name, path=path, aggregate=aggregate, test=test)


def _table(*conditions, enabled=True):
    """A table with one rule that reads the given conditions."""
    return RoutingTable(
        rules=[
            RoutingRule(
                id="r1",
                enabled=enabled,
                conditions=list(conditions),
                actions=queue_actions("default"),
            )
        ]
    )


def _value(document, condition, *, s3=None, mode="pipeline", config=None):
    """The value of one condition, and the inputs it came with."""
    inputs = build_inputs(
        document,
        config if config is not None else _config(),
        _table(condition),
        mode=mode,
        s3=s3 if s3 is not None else _S3(),
    )
    return inputs.values[expression_for(condition)], inputs


SUMMARY = {
    "overall_statistics": {"verdict_counts": {"pass": 1, "fail": 2}},
    "rule_details": {
        "lending": {
            "rules": [
                {"rule_id": "lending.max_ltv", "verdict": "fail"},
                {"rule_id": "lending.income", "verdict": "pass"},
            ]
        },
        "kyc": {"rules": [{"rule_id": "kyc.id_match", "verdict": "fail"}]},
    },
}


class TestInputsOnTheDocument:
    """These read nothing from S3."""

    @pytest.mark.parametrize("mode", ["pipeline", "bda"])
    def test_processing_mode(self, mode):
        s3 = _S3()

        value, inputs = _value(
            _document(_section("1")), _condition("processing_mode"), mode=mode, s3=s3
        )

        assert value == mode
        assert (s3.reads, inputs.reads) == ([], 0)

    def test_document_class_is_the_first_processed_sections(self):
        document = _document(
            _section("1", "Cover", excluded=True),
            _section("2", "Invoice"),
            _section("3", "Receipt"),
        )

        assert _value(document, _condition("document_class"))[0] == "Invoice"

    def test_document_class_is_none_with_no_processed_section(self):
        assert _value(_document(), _condition("document_class"))[0] is None

    def test_section_classes(self):
        document = _document(
            _section("1", "Invoice"),
            _section("2", "Receipt"),
            _section("3", "Cover", excluded=True),
        )

        assert _value(document, _condition("section_classes"))[0] == [
            "Invoice",
            "Receipt",
        ]
        assert (
            _value(document, _condition("section_classes", aggregate="count"))[0] == 2
        )

    def test_page_count_and_section_count(self):
        document = _document(
            _section("1"), _section("2", excluded=True), _section("3"), pages=7
        )

        assert _value(document, _condition("page_count"))[0] == 7
        assert _value(document, _condition("section_count"))[0] == 2

    @pytest.mark.parametrize(
        ("setting", "expected"), [(None, True), (True, True), (False, False)]
    )
    def test_route_missing_required_fields(self, setting, expected):
        value, _ = _value(
            _document(_section("1")),
            _condition("route_missing_required_fields"),
            config=_config(route_missing=setting),
        )

        assert value is expected


class TestClassPresent:
    """Whether a processed section has the named class. Reads nothing from S3."""

    def _present(self, document, class_name="Receipt"):
        s3 = _S3()
        value, inputs = _value(document, _condition("class_present", class_name), s3=s3)
        assert (s3.reads, inputs.reads) == ([], 0)
        return value, inputs

    def test_true_when_a_processed_section_has_the_class(self):
        document = _document(_section("1", "Invoice"), _section("2", "Receipt"))

        value, inputs = self._present(document)

        assert value is True
        assert inputs.stale == []

    def test_false_when_no_processed_section_has_it(self):
        document = _document(_section("1", "Invoice"), _section("2", "Invoice"))

        value, inputs = self._present(document)

        assert value is False
        assert inputs.stale == []

    @pytest.mark.parametrize("classification", [None, ""])
    def test_false_when_no_section_is_classified(self, classification):
        document = _document(_section("1", classification), _section("2", None))

        value, inputs = self._present(document)

        assert value is False
        assert inputs.stale == []

    def test_false_with_no_section(self):
        assert self._present(_document())[0] is False

    def test_a_class_the_configuration_does_not_have_is_none_and_stale(self):
        """The document does have a section of that class: the answer is still
        "not known", because the routing rule names a class the profile lost."""
        document = _document(_section("1", "Statement"))

        value, inputs = self._present(document, "Statement")

        assert value is None
        assert inputs.stale == ["class:Statement"]

    @pytest.mark.parametrize("written", ["receipt", "RECEIPT", " Receipt "])
    def test_the_class_is_matched_without_regard_to_case(self, written):
        document = _document(_section("1", "Invoice"), _section("2", "rEcEiPt"))

        value, inputs = self._present(document, written)

        assert value is True
        assert inputs.stale == []

    def test_an_excluded_section_is_ignored(self):
        document = _document(
            _section("1", "Receipt", excluded=True), _section("2", "Invoice")
        )

        assert self._present(document)[0] is False

    def test_the_value_feeds_a_test_for_a_missing_class(self):
        """The routing rule this input exists for: hold a document that has no
        section of a class."""
        table = RoutingTable(
            rules=[
                RoutingRule(
                    id="no-receipt",
                    conditions=[_condition("class_present", "Receipt", test="false")],
                    actions=queue_actions("default"),
                )
            ]
        )

        def outcome(document):
            values = build_inputs(
                document, _config(), table, mode="pipeline", s3=_S3()
            ).values
            return evaluate(to_decision_table(table), values).outputs["outcome"]

        assert outcome(_document(_section("1", "Invoice"))) == "queue"
        assert outcome(_document(_section("1", "Receipt"))) == "no_review"


def _failed_extraction(section_id, severity="error", code="extraction_failed"):
    """The issue extraction leaves on a section it could not extract (#2038)."""
    return SimpleNamespace(
        stage="extraction",
        severity=severity,
        code=code,
        message="The model's reply was withheld.",
        section_id=section_id,
    )


class TestExtractionFailedCount:
    """``extraction_failed_count``: the sections whose extraction failed without
    failing the document. They have no fields and no confidence scores, so no
    other input counts them; the built-in routing rule on this one holds them."""

    def _count(self, document, s3):
        return _value(document, _condition("extraction_failed_count"), s3=s3)[0]

    def test_it_is_the_processed_sections_whose_extraction_failed(self):
        document = _document(
            _section("1"),
            _section("2", result=False, processing_issues=[_failed_extraction("2")]),
            _section("3", result=False, processing_issues=[_failed_extraction("3")]),
        )

        assert self._count(document, _S3({_uri("1"): _result({})})) == 2

    @pytest.mark.parametrize(
        "issue",
        [
            _failed_extraction("1", severity="warning"),
            _failed_extraction("1", code="page_text_missing"),
        ],
        ids=["a-warning", "another-code"],
    )
    def test_only_an_error_of_that_code_counts(self, issue):
        document = _document(_section("1", processing_issues=[issue]))

        assert self._count(document, _S3({_uri("1"): _result({})})) == 0

    def test_a_section_with_no_issues_counts_nothing(self):
        document = _document(_section("1"), _section("2", processing_issues=[]))
        s3 = _S3({_uri("1"): _result({}), _uri("2"): _result({})})

        assert self._count(document, s3) == 0

    def test_a_section_a_reviewer_confirmed_is_not_counted_again(self):
        """The reviewer entered its values: the next run does not hold it for
        the extraction that failed before."""
        document = _document(
            _section("1", processing_issues=[_failed_extraction("1")]),
            _section("2", result=False, processing_issues=[_failed_extraction("2")]),
        )
        s3 = _S3({_uri("1"): _result({"Total": 5}, confirmed=True)})

        assert self._count(document, s3) == 1

    def test_an_excluded_section_contributes_nothing(self):
        document = _document(
            _section(
                "1",
                result=False,
                excluded=True,
                processing_issues=[_failed_extraction("1")],
            )
        )

        assert self._count(document, _S3({})) == 0


class TestConfidenceAlertCount:
    def _count(self, document, s3, mode="pipeline", config=None):
        return _value(
            document,
            _condition("confidence_alert_count"),
            s3=s3,
            mode=mode,
            config=config,
        )[0]

    def test_it_is_the_alerts_of_the_processed_sections(self):
        document = _document(
            _section("1", alerts=[_alert("Total"), _alert("Vendor")]),
            _section("2", alerts=[_alert("Total")]),
        )
        s3 = _S3({_uri("1"): _result({}), _uri("2"): _result({})})

        assert self._count(document, s3) == 3

    def test_an_alert_at_or_over_its_threshold_is_not_one(self):
        document = _document(_section("1", alerts=[_alert(confidence=0.9)]))

        assert self._count(document, _S3({_uri("1"): _result({})})) == 0

    def test_a_confirmed_section_contributes_no_alert(self):
        document = _document(
            _section("1", alerts=[_alert("Total"), _alert("Vendor")]),
            _section("2", alerts=[_alert("Total")]),
        )
        s3 = _S3({_uri("1"): _result({}, confirmed=True), _uri("2"): _result({})})

        assert self._count(document, s3) == 1

    def test_an_excluded_section_contributes_nothing(self):
        document = _document(
            _section("1", alerts=[_alert()], excluded=True),
            _section("2", alerts=[_alert()]),
        )
        s3 = _S3({_uri("2"): _result({})})

        assert self._count(document, s3) == 1
        assert s3.reads == [_uri("2")]

    def test_a_capped_alert_list_counts_its_total(self):
        """The tracking row stores what fits of a section's alerts, and the true
        figure beside it. A document reloaded from the row counts the figure."""
        document = _document(
            _section("1", alerts=[_alert("A"), _alert("B")], confidence_alert_total=40),
            _section("2", alerts=[_alert("C")]),
        )
        s3 = _S3({_uri("1"): _result({}), _uri("2"): _result({})})

        assert self._count(document, s3) == 41

    def test_a_section_with_no_result_is_not_confirmed(self):
        document = _document(_section("1", alerts=[_alert()], result=False))
        s3 = _S3()

        assert self._count(document, s3) == 1
        assert s3.reads == []

    def test_bda_mode_adds_the_blueprint_confidence(self):
        document = _document(
            _section("1", confidence=0.5),  # under the threshold
            _section("2", confidence=0.95),
            _section("3", blueprint_unscored=True),  # matched, no usable score
        )
        s3 = _S3({_uri(i): _result({}) for i in ("1", "2", "3")})

        assert self._count(document, s3, mode="bda") == 2
        # The blueprint confidence is not a reason in pipeline mode.
        assert self._count(document, s3, mode="pipeline") == 0

    def test_a_confirmed_bda_section_has_no_blueprint_reason(self):
        document = _document(_section("1", confidence=0.5))
        s3 = _S3({_uri("1"): _result({}, confirmed=True)})

        assert self._count(document, s3, mode="bda") == 0


class TestMissingRequiredFieldCount:
    def _count(self, document, s3):
        return _value(document, _condition("missing_required_field_count"), s3=s3)[0]

    def test_it_is_read_from_the_section_results(self):
        document = _document(_section("1"), _section("2"))
        s3 = _S3(
            {
                _uri("1"): _result({"Vendor": "ACME"}),  # Total is missing
                _uri("2"): _result({}),  # both are missing
            }
        )

        assert self._count(document, s3) == 3

    def test_a_complete_section_has_none(self):
        s3 = _S3({_uri("1"): _result({"Vendor": "ACME", "Total": 10})})

        assert self._count(_document(_section("1")), s3) == 0

    def test_a_confirmed_section_contributes_no_missing_field(self):
        document = _document(_section("1"), _section("2"))
        s3 = _S3(
            {
                _uri("1"): _result({}, confirmed=True),
                _uri("2"): _result({"Vendor": "ACME"}),
            }
        )

        assert self._count(document, s3) == 1

    def test_an_excluded_section_and_a_class_that_requires_nothing_have_none(self):
        document = _document(
            _section("1", excluded=True), _section("2", "Receipt"), _section("3")
        )
        s3 = _S3({_uri("2"): _result({}), _uri("3"): _result({"Vendor": "ACME"})})

        assert self._count(document, s3) == 1

    def test_the_assessment_may_be_a_dict_or_a_list_holding_one(self):
        document = _document(_section("1"), _section("2"))
        s3 = _S3(
            {
                _uri("1"): _result({"Vendor": "A"}, explainability={"Vendor": {}}),
                _uri("2"): _result({"Vendor": "A"}, explainability=[{"Vendor": {}}]),
            }
        )

        assert self._count(document, s3) == 2


class TestReviewed:
    def _reviewed(self, document, s3):
        return _value(document, _condition("reviewed"), s3=s3)[0]

    def test_true_when_every_processed_section_is_confirmed(self):
        document = _document(_section("1"), _section("2"), _section("3", excluded=True))
        s3 = _S3(
            {
                _uri("1"): _result({}, confirmed=True),
                _uri("2"): _result({}, confirmed=True),
            }
        )

        assert self._reviewed(document, s3) is True

    def test_false_when_one_is_not(self):
        document = _document(_section("1"), _section("2"))
        s3 = _S3({_uri("1"): _result({}, confirmed=True), _uri("2"): _result({})})

        assert self._reviewed(document, s3) is False

    def test_false_when_a_section_has_no_result(self):
        document = _document(_section("1"), _section("2", result=False))
        s3 = _S3({_uri("1"): _result({}, confirmed=True)})

        assert self._reviewed(document, s3) is False

    def test_false_with_no_processed_section(self):
        assert self._reviewed(_document(_section("1", excluded=True)), _S3()) is False

    def test_only_a_true_flag_confirms(self):
        result = _result({})
        result["review"] = {"confirmed": "yes"}

        assert (
            self._reviewed(_document(_section("1")), _S3({_uri("1"): result})) is False
        )


class TestFieldValue:
    def _s3(self):
        return _S3(
            {
                _uri("1"): _result(
                    {"Total": 12000, "Lines": [{"Amount": 5}, {"Amount": 7}]}
                ),
                _uri("2"): _result({"Total": 40}),
                _uri("3"): _result({"Vendor": "ACME"}),
            }
        )

    def _document(self):
        return _document(
            _section("1", "Invoice"), _section("2", "Receipt"), _section("3", "Invoice")
        )

    def test_without_a_class_every_section_is_read(self):
        value, _ = _value(
            self._document(), _condition("field_value", "Total"), s3=self._s3()
        )

        # Section 3 has no Total: an absent value is left out.
        assert value == [12000, 40]

    def test_with_a_class_only_its_sections_are_read(self):
        value, _ = _value(
            self._document(), _condition("field_value", "Invoice:Total"), s3=self._s3()
        )

        assert value == [12000]

    def test_the_class_is_matched_without_regard_to_case(self):
        value, _ = _value(
            self._document(), _condition("field_value", "invoice:Total"), s3=self._s3()
        )

        assert value == [12000]

    def test_a_list_index(self):
        value, _ = _value(
            self._document(),
            _condition("field_value", "Invoice:Lines[1].Amount"),
            s3=self._s3(),
        )

        assert value == [7]

    def test_a_path_no_section_has_is_an_empty_list(self):
        value, inputs = _value(
            self._document(),
            _condition("field_value", "Invoice:Nope.Deeper"),
            s3=self._s3(),
        )

        assert value == []
        assert inputs.stale == []

    def test_the_count_aggregate_is_how_many_values(self):
        value, _ = _value(
            self._document(),
            _condition("field_value", "Total", aggregate="count"),
            s3=self._s3(),
        )

        assert value == 2

    def test_a_class_the_configuration_does_not_have_is_stale(self):
        s3 = self._s3()

        value, inputs = _value(
            self._document(), _condition("field_value", "Statement:Total"), s3=s3
        )

        assert value == []
        assert inputs.stale == ["class:Statement"]
        assert s3.reads == []

    def test_an_excluded_section_is_not_read(self):
        document = _document(_section("1", excluded=True), _section("2", "Receipt"))
        s3 = self._s3()

        value, _ = _value(document, _condition("field_value", "Total"), s3=s3)

        assert value == [40]
        assert s3.reads == [_uri("2")]

    def test_a_section_result_that_cannot_be_read_is_stale(self):
        s3 = self._s3()
        del s3.objects[_uri("2")]

        value, inputs = _value(
            self._document(), _condition("field_value", "Total"), s3=s3
        )

        assert value == [12000]
        assert inputs.stale == ["section:2"]


class TestPolicyInputs:
    def _s3(self):
        return _S3({SUMMARY_JSON: SUMMARY})

    def test_a_rules_verdict(self):
        s3 = self._s3()

        value, inputs = _value(
            _document(_section("1"), policy=True),
            _condition("policy_rule_verdict", "lending.max_ltv"),
            s3=s3,
        )

        assert value == "fail"
        # The summary JSON beside the report, read once, and no section result.
        assert s3.reads == [SUMMARY_JSON]
        assert inputs.reads == 1

    def test_a_rule_the_summary_does_not_have_is_none(self):
        value, inputs = _value(
            _document(_section("1"), policy=True),
            _condition("policy_rule_verdict", "lending.unknown"),
            s3=self._s3(),
        )

        assert value is None
        assert inputs.stale == []

    def test_no_summary_is_none_and_stale(self):
        value, inputs = _value(
            _document(_section("1")),
            _condition("policy_rule_verdict", "lending.max_ltv"),
            s3=self._s3(),
        )

        assert value is None
        assert inputs.stale == ["policy_rule:lending.max_ltv"]

    def test_a_summary_that_cannot_be_read_is_none_and_stale(self):
        value, inputs = _value(
            _document(_section("1"), policy=True),
            _condition("policy_rule_verdict", "lending.max_ltv"),
            s3=_S3(),
        )

        assert value is None
        assert inputs.stale == ["policy_rule:lending.max_ltv"]

    def test_the_failed_rules_of_every_policy(self):
        value, _ = _value(
            _document(_section("1"), policy=True),
            _condition("failed_policy_rule_count"),
            s3=self._s3(),
        )

        assert value == 2

    def test_the_failed_rules_of_one_policy(self):
        value, _ = _value(
            _document(_section("1"), policy=True),
            _condition("failed_policy_rule_count", "kyc"),
            s3=self._s3(),
        )

        assert value == 1

    def test_no_summary_counts_no_failed_rule(self):
        value, inputs = _value(
            _document(_section("1")), _condition("failed_policy_rule_count"), s3=_S3()
        )

        assert value == 0
        assert inputs.stale == []

    def test_the_summary_is_read_once_for_several_conditions(self):
        s3 = self._s3()
        table = _table(
            _condition("policy_rule_verdict", "lending.max_ltv"),
            _condition("policy_rule_verdict", "kyc.id_match"),
            _condition("failed_policy_rule_count"),
            _condition("failed_policy_rule_count", "lending"),
        )

        inputs = build_inputs(
            _document(_section("1"), policy=True),
            _config(),
            table,
            mode="pipeline",
            s3=s3,
        )

        assert s3.reads == [SUMMARY_JSON]
        assert sorted(inputs.values.values(), key=str) == [1, 2, "fail", "fail"]


def _failed(rule_id, reasoning="", pages=(), verdict="fail", **more):
    entry = {
        "rule_id": rule_id,
        "verdict": verdict,
        "reasoning": reasoning,
        "supporting_pages": list(pages),
    }
    entry.update(more)
    return entry


def _summary(**policies):
    return {
        "rule_details": {name: {"rules": rules} for name, rules in policies.items()}
    }


class TestFailedPolicyRules:
    """The failed policy rules a reviewer is shown, from the summary."""

    def _failures(self, summary, *, limit=10, reason_bytes=500, max_pages=10):
        s3 = _S3({SUMMARY_JSON: summary} if summary is not None else {})
        reader = InputReader(
            _document(_section("1"), policy=True), _config(), "pipeline", s3
        )
        return reader.failed_policy_rules(
            limit=limit, reason_bytes=reason_bytes, max_pages=max_pages
        )

    def test_each_failed_rule_with_its_policy_name_reason_and_pages(self):
        summary = _summary(
            lending=[
                _failed("lending.max_ltv", "LTV is 92%, over 80%.", ["3", "4"]),
                _failed("lending.income", "Income verified.", ["1"], verdict="pass"),
            ],
            kyc=[_failed("kyc.id_match", "The names differ.", [2])],
        )

        failures, count = self._failures(summary)

        assert failures == [
            FailedPolicyRule(
                rule_id="lending.max_ltv",
                policy="lending",
                name="max_ltv",
                reason="LTV is 92%, over 80%.",
                pages=(3, 4),
            ),
            FailedPolicyRule(
                rule_id="kyc.id_match",
                policy="kyc",
                name="id_match",
                reason="The names differ.",
                pages=(2,),
            ),
        ]
        assert count == 2

    def test_only_a_fail_verdict_is_a_failure(self):
        summary = _summary(
            p=[
                _failed("p.a", verdict="pass"),
                _failed("p.b", verdict="not_found"),
                _failed("p.c", verdict="unknown"),
                _failed("p.d", verdict="FAIL"),
            ]
        )

        assert self._failures(summary) == ([], 0)

    def test_the_list_stops_at_the_limit_and_the_count_does_not(self):
        summary = _summary(
            a=[_failed(f"a.r{i}") for i in range(4)],
            b=[_failed(f"b.r{i}") for i in range(3)],
        )

        failures, count = self._failures(summary, limit=5)

        assert [f.rule_id for f in failures] == ["a.r0", "a.r1", "a.r2", "a.r3", "b.r0"]
        assert count == 7

    def test_the_name_is_what_follows_the_first_dot(self):
        summary = _summary(p=[_failed("p.group.key"), _failed("bare"), _failed(None)])

        failures, count = self._failures(summary)

        assert [(f.rule_id, f.name) for f in failures] == [
            ("p.group.key", "group.key"),
            ("bare", "bare"),
            ("", ""),
        ]
        assert count == 3

    def test_the_reason_is_one_line_with_no_control_character(self):
        reasoning = "  The total\n\n is\tover \x00the\x1b[31m limit.\x7f\r\n "
        summary = _summary(p=[_failed("p.a", reasoning)])

        assert (
            self._failures(summary)[0][0].reason == "The total is over the[31m limit."
        )

    @pytest.mark.parametrize("reasoning", [None, 12, ["a"], {"a": 1}])
    def test_a_reason_that_is_not_text_is_empty(self, reasoning):
        summary = _summary(p=[_failed("p.a", reasoning)])

        assert self._failures(summary)[0][0].reason == ""

    def test_the_reason_is_cut_to_its_bytes_between_two_characters(self):
        summary = _summary(p=[_failed("p.a", "é" * 20)])

        reason = self._failures(summary, reason_bytes=12)[0][0].reason

        # Four two-byte characters and the three-byte ellipsis: 11 of 12 bytes,
        # because a fifth character would not fit whole.
        assert reason == "éééé…"
        assert len(reason.encode("utf-8")) <= 12

    def test_the_pages_are_the_page_numbers_as_integers(self):
        pages = ["7", 2, " 11 ", "page 3", "", None, True, "²", "1.5", "-4", ["9"]]
        summary = _summary(p=[_failed("p.a", pages=pages)])

        assert self._failures(summary)[0][0].pages == (7, 2, 11)

    def test_a_page_number_too_long_for_the_api_is_left_out(self):
        summary = _summary(
            p=[_failed("p.a", pages=["999999999", "1000000000", "9" * 5000])]
        )

        assert self._failures(summary)[0][0].pages == (999999999,)

    def test_the_pages_stop_at_their_limit(self):
        summary = _summary(p=[_failed("p.a", pages=[str(i) for i in range(1, 30)])])

        assert self._failures(summary, max_pages=3)[0][0].pages == (1, 2, 3)

    @pytest.mark.parametrize("pages", [None, "3", 3, {"3": 1}])
    def test_pages_that_are_not_a_list_are_none(self, pages):
        entry = _failed("p.a")
        entry["supporting_pages"] = pages

        assert self._failures(_summary(p=[entry]))[0][0].pages == ()

    def test_no_summary_is_no_failure(self):
        reader = InputReader(_document(_section("1")), _config(), "pipeline", _S3())

        assert reader.failed_policy_rules(limit=10, reason_bytes=500, max_pages=10) == (
            [],
            0,
        )

    def test_a_summary_that_cannot_be_read_is_no_failure(self):
        assert self._failures(None) == ([], 0)

    @pytest.mark.parametrize(
        "summary",
        [
            [],
            "text",
            {"rule_details": None},
            {"rule_details": ["x"]},
            {"rule_details": {"p": None}},
            {"rule_details": {"p": {"rules": None}}},
            {"rule_details": {"p": {"rules": 5}}},
            {"rule_details": {"p": {"rules": ["x", None, 3]}}},
        ],
    )
    def test_a_summary_of_another_shape_never_raises(self, summary):
        assert self._failures(summary) == ([], 0)

    def test_a_malformed_entry_does_not_hide_the_others(self):
        summary = {
            "rule_details": {
                "p": {"rules": ["not an entry", _failed("p.a", "Wrong."), None]},
                "q": "not a policy",
                "r": {"rules": [_failed("r.b")]},
            }
        }

        failures, count = self._failures(summary)

        assert [f.rule_id for f in failures] == ["p.a", "r.b"]
        assert count == 2


class TestOneReaderForTheWholeEvaluation:
    """The routing step builds one reader and passes it on, so the table's
    inputs, a reason's placeholders and the failed policy rules share its reads."""

    def _reader(self, s3, *sections):
        document = _document(*(sections or (_section("1"),)), policy=True)
        return InputReader(document, _config(), "pipeline", s3)

    def test_the_summary_is_read_once_for_the_inputs_and_the_failures(self):
        s3 = _S3({SUMMARY_JSON: SUMMARY})
        reader = self._reader(s3)
        table = _table(_condition("policy_rule_verdict", "lending.max_ltv"))

        inputs = build_inputs(
            reader.document, reader.config, table, mode="pipeline", reader=reader
        )
        failures, count = reader.failed_policy_rules(
            limit=10, reason_bytes=500, max_pages=10
        )

        assert inputs.values == {
            expression_for(_condition("policy_rule_verdict", "lending.max_ltv")): "fail"
        }
        assert [f.rule_id for f in failures] == ["lending.max_ltv", "kyc.id_match"]
        assert count == 2
        assert s3.reads == [SUMMARY_JSON]
        assert reader.reads == 1

    def test_the_failures_alone_read_the_summary_once(self):
        s3 = _S3({SUMMARY_JSON: SUMMARY})
        reader = self._reader(s3)

        for _ in range(2):
            reader.failed_policy_rules(limit=10, reason_bytes=500, max_pages=10)

        assert s3.reads == [SUMMARY_JSON]

    def test_a_missing_summary_is_asked_for_once(self):
        s3 = _S3()
        reader = self._reader(s3)

        build_inputs(
            reader.document,
            reader.config,
            _table(_condition("failed_policy_rule_count")),
            mode="pipeline",
            reader=reader,
        )
        reader.failed_policy_rules(limit=10, reason_bytes=500, max_pages=10)

        assert s3.reads == [SUMMARY_JSON]

    def test_a_value_read_after_the_inputs_reads_no_section_twice(self):
        s3 = _S3({_uri("1"): _result({"Total": 5}), _uri("2"): _result({"Total": 9})})
        reader = self._reader(s3, _section("1"), _section("2"))

        build_inputs(
            reader.document,
            reader.config,
            _table(_condition("reviewed")),
            mode="pipeline",
            reader=reader,
        )
        value = reader.value(_condition("field_value", "Invoice:Total"))

        assert value == [5, 9]
        assert sorted(s3.reads) == [_uri("1"), _uri("2")]
        assert reader.reads == 2

    def test_the_reader_given_is_the_one_used(self):
        """``build_inputs`` reads through the reader, not through its own ``s3``."""
        mine, other = _S3({_uri("1"): _result({}, confirmed=True)}), _S3()
        reader = self._reader(mine)

        inputs = build_inputs(
            reader.document,
            reader.config,
            _table(_condition("reviewed")),
            mode="pipeline",
            s3=other,
            reader=reader,
        )

        assert inputs.values == {"reviewed": True}
        assert (mine.reads, other.reads) == ([_uri("1")], [])
        assert inputs.reads == 1

    def test_what_is_stale_afterwards_is_not_in_the_inputs_already_returned(self):
        reader = self._reader(_S3())

        inputs = build_inputs(
            reader.document,
            reader.config,
            _table(_condition("page_count")),
            mode="pipeline",
            reader=reader,
        )
        reader.value(_condition("class_present", "Statement"))

        assert inputs.stale == []
        assert reader.stale == ["class:Statement"]


class TestWhatIsRead:
    def test_every_input_of_the_catalogue_is_computed(self):
        """A table that uses every input gets a value for each."""
        paths = {
            "class_present": "Invoice",
            "field_value": "Invoice:Total",
            "policy_rule_verdict": "lending.max_ltv",
        }
        conditions = [
            _condition(entry.name, paths.get(entry.name, ""))
            for entry in ROUTING_INPUTS
        ]
        s3 = _S3(
            {_uri("1"): _result({"Vendor": "ACME", "Total": 5}), SUMMARY_JSON: SUMMARY}
        )

        inputs = build_inputs(
            _document(_section("1", alerts=[_alert()]), policy=True),
            _config(),
            _table(*conditions),
            mode="pipeline",
            s3=s3,
        )

        assert inputs.stale == []
        assert inputs.values == {
            "document_class": "Invoice",
            expression_for(_condition("section_classes")): ["Invoice"],
            expression_for(_condition("class_present", "Invoice")): True,
            "page_count": 3,
            "section_count": 1,
            "confidence_alert_count": 1,
            "missing_required_field_count": 0,
            "extraction_failed_count": 0,
            "route_missing_required_fields": True,
            expression_for(_condition("field_value", "Invoice:Total")): [5],
            expression_for(
                _condition("policy_rule_verdict", "lending.max_ltv")
            ): "fail",
            "failed_policy_rule_count": 2,
            "reviewed": False,
            "processing_mode": "pipeline",
        }
        assert len(inputs.values) == len(ROUTING_INPUTS)

    def test_only_the_inputs_the_table_uses_cause_reads(self):
        document = _document(_section("1"), _section("2"), policy=True)
        objects = {
            _uri("1"): _result({}),
            _uri("2"): _result({}),
            SUMMARY_JSON: SUMMARY,
        }

        nothing = _S3(objects)
        build_inputs(
            document,
            _config(),
            _table(_condition("page_count"), _condition("document_class")),
            mode="pipeline",
            s3=nothing,
        )
        sections_only = _S3(objects)
        build_inputs(
            document,
            _config(),
            _table(_condition("reviewed")),
            mode="pipeline",
            s3=sections_only,
        )
        summary_only = _S3(objects)
        build_inputs(
            document,
            _config(),
            _table(_condition("failed_policy_rule_count")),
            mode="pipeline",
            s3=summary_only,
        )

        assert nothing.reads == []
        assert sorted(sections_only.reads) == [_uri("1"), _uri("2")]
        assert summary_only.reads == [SUMMARY_JSON]

    def test_each_section_result_is_read_once(self):
        document = _document(_section("1"), _section("2"))
        s3 = _S3({_uri("1"): _result({"Total": 1}), _uri("2"): _result({"Total": 2})})
        table = _table(
            _condition("reviewed"),
            _condition("confidence_alert_count"),
            _condition("missing_required_field_count"),
            _condition("field_value", "Total"),
            _condition("field_value", "Vendor"),
        )

        inputs = build_inputs(document, _config(), table, mode="pipeline", s3=s3)

        assert sorted(s3.reads) == [_uri("1"), _uri("2")]
        assert inputs.reads == 2

    def test_a_disabled_rule_reads_nothing(self):
        s3 = _S3({_uri("1"): _result({})})

        inputs = build_inputs(
            _document(_section("1")),
            _config(),
            _table(_condition("reviewed"), enabled=False),
            mode="pipeline",
            s3=s3,
        )

        assert inputs == RoutingInputs()
        assert s3.reads == []

    def test_the_section_results_are_read_in_parallel(self):
        ids = [str(i) for i in range(1, 5)]
        s3 = _S3({_uri(i): _result({}, confirmed=True) for i in ids})
        # Every read waits here until all four are in flight: reads made one
        # after another never get past it.
        s3.barrier = threading.Barrier(len(ids))
        document = _document(*[_section(i) for i in ids])

        facts = load_section_facts(document, s3)

        assert all(f.readable and f.confirmed for f in facts.values())
        assert len(s3.threads) == len(ids)
        assert all(name.startswith("routing-inputs") for name in s3.threads)

    def test_no_more_reads_at_once_than_the_limit(self):
        ids = [str(i) for i in range(1, 7)]
        s3 = _S3({_uri(i): _result({}) for i in ids})

        load_section_facts(_document(*[_section(i) for i in ids]), s3, max_workers=2)

        assert len(s3.threads) <= 2
        assert sorted(s3.reads) == sorted(_uri(i) for i in ids)

    def test_a_stale_reference_is_logged_once_and_counted(self, monkeypatch, caplog):
        published = []
        monkeypatch.setattr(
            "idp_common.review_queues.service._put_metric",
            lambda name, value=1, dimensions=None: published.append((name, value)),
        )
        table = _table(
            _condition("field_value", "Statement:Total"),
            _condition("field_value", "Statement:Vendor"),
            _condition("policy_rule_verdict", "lending.max_ltv"),
        )

        with caplog.at_level("WARNING", logger=routing_inputs.logger.name):
            inputs = build_inputs(
                _document(_section("1")), _config(), table, mode="pipeline", s3=_S3()
            )

        assert inputs.stale == ["class:Statement", "policy_rule:lending.max_ltv"]
        lines = [
            r.getMessage() for r in caplog.records if "routing_stale" in r.getMessage()
        ]
        assert len(lines) == 1
        assert "class:Statement" in lines[0]
        assert published == [("RoutingStaleReference", 2)]


class TestTheValuesFeedTheTable:
    """The keys are the columns' texts, so the values are the evaluation context."""

    def _decide(self, document, s3, table=None, mode="pipeline", config=None):
        table = table if table is not None else RoutingTable()
        inputs = build_inputs(
            document,
            config if config is not None else _config(),
            table,
            mode=mode,
            s3=s3,
        )
        return evaluate(to_decision_table(table), inputs.values).outputs

    def test_the_built_in_table_holds_a_document_with_an_alert(self):
        document = _document(_section("1", alerts=[_alert()]))
        s3 = _S3({_uri("1"): _result({"Vendor": "ACME", "Total": 1})})

        assert self._decide(document, s3) == {
            "outcome": "queue",
            "queue": "default",
            "reason": BUILTIN_CONFIDENCE_REASON,
        }

    def test_the_built_in_table_lets_a_clean_document_through(self):
        s3 = _S3({_uri("1"): _result({"Vendor": "ACME", "Total": 1})})

        assert self._decide(_document(_section("1")), s3)["outcome"] == "no_review"

    def test_the_built_in_table_lets_a_reviewed_document_through(self):
        document = _document(_section("1", alerts=[_alert()]))
        s3 = _S3({_uri("1"): _result({}, confirmed=True)})

        assert self._decide(document, s3)["outcome"] == "no_review"

    def test_a_missing_required_field_holds_only_when_the_setting_is_on(self):
        document = _document(_section("1"))
        s3 = _S3({_uri("1"): _result({"Vendor": "ACME"})})

        assert self._decide(document, s3)["outcome"] == "queue"
        assert (
            self._decide(document, s3, config=_config(route_missing=False))["outcome"]
            == "no_review"
        )

    def test_a_rule_on_a_field_value(self):
        table = RoutingTable(
            rules=[
                RoutingRule(
                    id="high-value",
                    conditions=[
                        _condition("field_value", "Invoice:Total", test="> 10000")
                    ],
                    actions=queue_actions("high-value"),
                )
            ]
        )
        s3 = _S3(
            {_uri("1"): _result({"Total": 12000}), _uri("2"): _result({"Total": 5})}
        )

        held = self._decide(_document(_section("1")), s3, table)
        passed = self._decide(_document(_section("2")), s3, table)

        assert held == {"outcome": "queue", "queue": "high-value", "reason": ""}
        assert passed["outcome"] == "no_review"
