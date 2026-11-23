# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``testRoutingTable``: what a routing table decides for a document, changing nothing.

The handler loads the document and the configuration it is pinned to, and hands
them to ``routing_step.test_table``. That function is the real one here, over
queue definitions in a moto table, so what is pinned is the answer an Admin gets
and that the review table is left exactly as it was.
"""

import json
from unittest.mock import MagicMock

import boto3
import pytest
from _harness import event, load
from moto import mock_aws

import idp_common.s3 as s3_module
from idp_common.api_adapter import ResourceNotFound
from idp_common.config.models import IDPConfig
from idp_common.config.routing_models import (
    RoutingCondition,
    RoutingConfig,
    RoutingRule,
    RoutingTable,
    builtin_rules,
    queue_actions,
)
from idp_common.models import Document, Section, Status
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.store import ReviewStore
from idp_common.run_plan.mode import ProcessingMode

pytestmark = pytest.mark.unit

REGION = "us-east-1"
TABLE = "hitl"
KEY = "claims/invoice.pdf"
RESULT_URI = f"s3://out/{KEY}/sections/1/result.json"
INVOICE = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "Invoice",
    "x-aws-idp-document-type": "Invoice",
    "type": "object",
    "properties": {"Vendor": {"type": "string"}, "Total": {"type": "number"}},
}


def _rule(rule_id, queue, label="", reason=""):
    return RoutingRule(
        id=rule_id,
        label=label or rule_id,
        conditions=[
            RoutingCondition(input="field_value", path="Invoice:Total", test="> 10000")
        ],
        actions=queue_actions(queue),
        reason=reason,
    )


def _routing(queue="high-value", reason=""):
    table = RoutingTable(
        rules=[_rule("big", queue, "Invoices over 10,000", reason)] + builtin_rules()
    )
    return RoutingConfig(tables=[table])


def _config(routing=None):
    return IDPConfig.model_validate(
        {
            "hitl": {"enabled": True, "confidence_threshold": 0.8},
            "classes": [INVOICE],
            "routing": (routing or _routing()).model_dump(),
        }
    )


def _document():
    return Document(
        id=KEY,
        input_bucket="in",
        input_key=KEY,
        output_bucket="out",
        status=Status.COMPLETED,
        num_pages=1,
        config_version="lending",
        config_revision=4,
        sections=[
            Section(
                section_id="1",
                classification="Invoice",
                page_ids=["1"],
                extraction_result_uri=RESULT_URI,
            )
        ],
    )


def _stored():
    """Every item in the review table, for a before-and-after comparison."""
    items = boto3.resource("dynamodb", region_name=REGION).Table(TABLE).scan()["Items"]
    return sorted(json.dumps(item, sort_keys=True, default=str) for item in items)


@pytest.fixture
def module(monkeypatch):
    """The handler over queue definitions in a moto table and a stored result."""
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        store = ReviewStore(TABLE, region=REGION)
        for definition in (
            QueueDefinition(id="default", name="Default"),
            QueueDefinition(id="high-value", name="High value"),
            QueueDefinition(id="closed", name="Closed", enabled=False),
        ):
            store.queue_defs.put(definition, create=True)

        m = load()
        m._SERVICE = MagicMock(name="ReviewQueueService")
        m._SERVICE.store = store
        m._SERVICE.document_service.get_document.return_value = _document()
        m.resolved = []

        def resolve(manager, version, revision):
            m.resolved.append((version, revision))
            return ProcessingMode("pipeline", None, m.config, False)

        m.config = _config()
        monkeypatch.setattr(m, "_configuration_manager", lambda: object())
        monkeypatch.setattr(m, "resolve_processing_mode", resolve)
        monkeypatch.setattr(
            s3_module,
            "get_json_content",
            lambda uri: {
                "inference_result": {"Vendor": "ACME", "Total": 12000},
                "explainability_info": [{}],
            },
        )
        yield m


def _test(module, routing="absent", **more):
    args = {"objectKey": KEY}
    if routing != "absent":
        args["routing"] = routing
    return module.handler(event("testRoutingTable", args, **more), None)


class TestTheSavedTable:
    def test_the_answer_names_the_rule_the_queue_and_the_inputs(self, module):
        result = _test(module)

        assert result == {
            "decision": "hold",
            "matchedRuleId": "big",
            "ruleLabel": "Invoices over 10,000",
            "outcomeKind": "queue",
            "outcomeQueue": "high-value",
            "placedQueue": "high-value",
            "fallbackReason": "",
            "inputs": json.dumps({'field_value("Invoice:Total","any")': "[12000]"}),
            "staleReferences": [],
            # The routing rule has no reason of its own.
            "reason": (
                "Invoices over 10,000: Field value Invoice:Total > 10000 (found 12000)"
            ),
            "reasonSource": "fallback",
        }

    def test_the_answer_carries_the_reason_the_reviewer_would_read(self, module):
        module.config = _config(
            _routing(reason="{field_value:Invoice:Vendor}: {field_value:Invoice:Total}")
        )

        result = _test(module)

        assert (result["reason"], result["reasonSource"]) == ("ACME: 12000", "admin")

    def test_the_configuration_is_the_one_the_document_is_pinned_to(self, module):
        _test(module)

        assert module.resolved == [("lending", 4)]

    def test_a_rule_that_does_not_match_gives_the_default_outcome(self, module):
        module.config = _config(RoutingConfig(tables=[RoutingTable(rules=[])]))

        result = _test(module)

        assert (result["decision"], result["outcomeKind"]) == ("continue", "no_review")
        assert result["matchedRuleId"] is None
        assert result["placedQueue"] is None
        assert (result["reason"], result["reasonSource"]) == (None, None)


class TestATableThatIsNotSaved:
    def test_it_is_the_one_tested(self, module):
        """The saved table names one queue; the one on trial names another."""
        result = _test(module, _routing("closed").model_dump())

        assert (result["outcomeQueue"], result["placedQueue"]) == ("closed", "default")
        assert result["fallbackReason"] == "queue_disabled"

    def test_it_can_be_sent_as_json_text(self, module):
        result = _test(module, json.dumps(_routing("closed").model_dump()))

        assert result["placedQueue"] == "default"

    def test_one_a_save_would_refuse_gives_the_validation_message(self, module):
        with pytest.raises(ValueError, match="INVALID_ROUTING: .*gone") as refused:
            _test(module, _routing("gone").model_dump())

        assert type(refused.value) is ValueError

    @pytest.mark.parametrize(
        ("single_outcome", "named"),
        [
            (
                {"rules": [{"id": "big", "outcome": {"kind": "queue", "queue": "x"}}]},
                "rule 'big'",
            ),
            ({"default_outcome": {"kind": "queue", "queue": "x"}}, "default outcome"),
        ],
    )
    @pytest.mark.parametrize("as_text", [False, True])
    def test_one_written_with_the_single_outcome_is_refused(
        self, module, single_outcome, named, as_text
    ):
        """The models drop the key, so the table would be tested as "no
        review" and the answer would be about a table nobody wrote."""
        routing = {"tables": [dict(single_outcome, id="review-routing")]}

        with pytest.raises(ValueError, match="INVALID_ROUTING: ") as refused:
            _test(module, json.dumps(routing) if as_text else routing)

        assert type(refused.value) is ValueError
        assert named in str(refused.value)
        assert "list of actions" in str(refused.value)
        assert module._SERVICE.mock_calls == []

    @pytest.mark.parametrize(
        "routing",
        ["{not json", "[]", 7, {"tables": "none"}, {"tables": [{"rules": 3}]}],
    )
    def test_one_that_is_not_a_routing_section_is_a_bad_request(self, module, routing):
        with pytest.raises(ValueError, match="BAD_ARGUMENT"):
            _test(module, routing)


class TestNothingIsWritten:
    @pytest.mark.parametrize("routing", ["absent", "trial", "invalid"])
    def test_the_review_table_is_left_as_it_was(self, module, routing):
        before = _stored()
        store = module._SERVICE.store
        puts = []
        for name in ("put_item", "update_item", "delete_item", "transact_write_items"):
            original = getattr(store.client, name)
            setattr(
                store.client,
                name,
                lambda *a, _n=name, _o=original, **kw: puts.append(_n) or _o(*a, **kw),
            )

        if routing == "invalid":
            with pytest.raises(ValueError):
                _test(module, _routing("gone").model_dump())
        elif routing == "trial":
            _test(module, _routing("closed").model_dump())
        else:
            _test(module)

        assert puts == []
        assert _stored() == before

    def test_the_service_is_asked_only_for_the_document(self, module):
        _test(module)

        assert [call[0] for call in module._SERVICE.mock_calls] == [
            "document_service.get_document"
        ]


class TestRefusals:
    @pytest.mark.parametrize("groups", [("Reviewer",), ("Author",), ("Annotator",), ()])
    def test_only_an_admin_may_test_a_table(self, module, groups):
        with pytest.raises(PermissionError, match="ROLE: .*Admin"):
            _test(module, groups=groups)
        assert module._SERVICE.mock_calls == []

    def test_a_document_that_does_not_exist(self, module):
        module._SERVICE.document_service.get_document.return_value = None

        with pytest.raises(ResourceNotFound, match="NOT_FOUND"):
            _test(module)

    def test_a_configuration_that_cannot_be_read(self, module):
        module.config = None

        with pytest.raises(ResourceNotFound, match="configuration"):
            _test(module)

    def test_a_call_without_a_document(self, module):
        with pytest.raises(ValueError, match="BAD_ARGUMENT"):
            module.handler(event("testRoutingTable", {}), None)
