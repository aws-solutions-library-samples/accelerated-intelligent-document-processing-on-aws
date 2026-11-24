# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routing step's handler (``processresults_function/routing_handler.py``).

The real handler with the real ``decide``, against a moto review table and output
bucket: what it answers, what it wrote, and what it handed to ``decide``.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from idp_common.config.models import IDPConfig
from idp_common.models import Document
from idp_common.review_queues.routing_step import RoutingDecision
from idp_common.review_queues.statuses import QueueItemState
from idp_common.utils.transient_errors import TransientError
from tests.unit.lambdas._goldens import load_handler
from tests.unit.lambdas._review_env import (
    EXEC,
    INVOICE,
    KEY,
    LAMBDA_CONTEXT,
    config,
    document,
    finalizing_item,
    section,
    swapped,
)

pytestmark = pytest.mark.unit

routing_index = load_handler(
    "routing_handler_index", "processresults_function/routing_handler.py"
)

RESPONSE_KEYS = {"document", "decision", "reason", "queue", "cycle_id", "rule_id"}
PLAN = {
    "v": 1,
    "kind": "reprocess",
    "startStep": "beginning",
    "startAt": "beginning",
    "mode": "pipeline",
    "reviewed": False,
}
FINALIZE_PLAN = {
    "v": 1,
    "kind": "review_completed",
    "startStep": "apply_review_changes",
    "startAt": "apply_review_changes",
    "mode": "pipeline",
    "reviewed": True,
}


def _event(doc, execution_input="absent"):
    event = {"execution_arn": EXEC, "document": doc.to_dict()}
    if execution_input != "absent":
        event["execution_input"] = execution_input
    return event


def _run(env, doc, execution_input="absent", *, cfg=None, **swaps):
    """Run the handler; returns (response, the configuration reads)."""
    reads = []

    def get_config(**kwargs):
        reads.append(kwargs)
        return cfg if cfg is not None else config()

    attributes = dict(
        get_config=get_config,
        create_document_service=lambda *a, **kw: MagicMock(),
        _REVIEW_STORE=env.store,
    )
    attributes.update(swaps)
    with swapped(routing_index, **attributes):
        response = routing_index.handler(_event(doc, execution_input), LAMBDA_CONTEXT)
    return response, reads


def _spy():
    """A ``decide`` that records its arguments and lets the run continue."""
    calls = []

    def decide(doc, cfg, **kwargs):
        calls.append((doc, cfg, kwargs))
        return RoutingDecision(decision="continue", reason="default_outcome")

    return decide, calls


class TestTheAnswer:
    def test_a_document_with_an_alert_is_held(self, review_env):
        doc = document(section(review_env, "1", alert=True))

        response, _ = _run(review_env, doc, {"runPlan": PLAN})

        assert set(response) == RESPONSE_KEYS
        item = review_env.store.get_item(KEY)
        assert item.state == QueueItemState.HOLDING.value
        assert item.phase1_execution_arn == EXEC
        assert response["decision"] == "hold"
        assert response["queue"] == "default"
        assert response["cycle_id"] == item.cycle_id
        assert response["reason"] == "rule"
        assert response["rule_id"] == item.routed_by.rule_id != ""

    def test_a_document_nothing_asks_a_review_for_continues(self, review_env):
        doc = document(section(review_env, "1"))

        response, _ = _run(review_env, doc, {"runPlan": PLAN})

        assert set(response) == RESPONSE_KEYS
        assert response["decision"] == "continue"
        assert response["queue"] == ""
        assert response["cycle_id"] == ""
        assert review_env.store.get_item(KEY) is None

    def test_the_finalize_run_of_a_rejected_review_is_rejected(self, review_env):
        """A finalize message that carried no plan: the run ends as rejected."""
        finalizing_item(review_env, disposition="rejected")
        doc = document(section(review_env, "1", alert=True))

        response, _ = _run(review_env, doc)

        assert response["decision"] == "rejected"
        assert response["reason"] == "review_rejected"

    def test_the_document_goes_on_as_it_came(self, review_env):
        doc = document(section(review_env, "1"), section(review_env, "2"))

        response, _ = _run(review_env, doc, {"runPlan": PLAN})

        returned = Document.from_dict(response["document"])
        assert returned.input_key == KEY
        assert [s.section_id for s in returned.sections] == ["1", "2"]


class TestWhyNothingIsHeld:
    """The reasons the step logs for a document it does not route."""

    def test_a_stack_with_no_review_table(self, review_env):
        doc = document(section(review_env, "1", alert=True))

        response, _ = _run(review_env, doc, {"runPlan": PLAN}, _REVIEW_STORE=None)

        assert (response["decision"], response["reason"]) == (
            "continue",
            "no_review_store",
        )

    def test_review_routing_off_for_the_profile(self, review_env):
        doc = document(section(review_env, "1", alert=True))

        response, _ = _run(review_env, doc, {"runPlan": PLAN}, cfg=config(hitl=False))

        assert (response["decision"], response["reason"]) == ("continue", "hitl_off")
        assert review_env.store.get_item(KEY) is None


def _watch_list_config():
    """A profile whose routing rule's reason quotes an extracted value."""
    return IDPConfig.model_validate(
        {
            "hitl": {"enabled": True, "confidence_threshold": 0.8},
            "classes": [INVOICE],
            "routing": {
                "tables": [
                    {
                        "rules": [
                            {
                                "id": "watch-list",
                                "label": "Vendors on the watch list",
                                "conditions": [
                                    {
                                        "input": "field_value",
                                        "path": "Invoice:Vendor",
                                        "test": '"ACME"',
                                    }
                                ],
                                "actions": [
                                    {"type": "review_queue", "queue": "default"}
                                ],
                                "reason": (
                                    "Vendor {field_value:Invoice:Vendor} is on the "
                                    "watch list"
                                ),
                            }
                        ]
                    }
                ]
            },
        }
    )


class TestTheReason:
    def test_it_is_on_the_item_and_in_neither_the_answer_nor_the_log(
        self, review_env, caplog
    ):
        """The reason can hold extracted values, and the handler logs its answer."""
        doc = document(section(review_env, "1"))

        with caplog.at_level(logging.INFO):
            response, _ = _run(
                review_env, doc, {"runPlan": PLAN}, cfg=_watch_list_config()
            )

        routed = review_env.store.get_item(KEY).routed_by
        assert routed.reason == "Vendor ACME is on the watch list"
        assert (routed.reason_source, routed.rule_id) == ("admin", "watch-list")
        # The answer is what it was: ``reason`` is why the step decided.
        assert set(response) == RESPONSE_KEYS
        assert (response["decision"], response["reason"]) == ("hold", "rule")
        assert "watch list" not in json.dumps(response, default=str)
        logged = [record.getMessage() for record in caplog.records]
        assert [m for m in logged if m.startswith("Response: ")]
        assert [m for m in logged if "reason_source=admin" in m]
        assert not [m for m in logged if "watch list" in m]


class TestThePlan:
    def test_it_is_read_from_the_execution_input(self, review_env):
        """A completed review continues unplanned, and is routed with a plan."""
        finalizing_item(review_env)
        doc = document(section(review_env, "1"))

        unplanned, _ = _run(review_env, doc, {"document": {}})
        planned, _ = _run(review_env, doc, {"runPlan": FINALIZE_PLAN})

        assert unplanned["reason"] == "unplanned_finalize"
        assert planned["decision"] == "continue"
        assert planned["reason"] != "unplanned_finalize"

    def test_decide_gets_the_plan_the_store_and_the_execution(self, review_env):
        decide, calls = _spy()
        doc = document(section(review_env, "1"))

        _run(review_env, doc, {"runPlan": PLAN}, decide=decide)

        (passed, cfg, kwargs) = calls[0]
        assert passed.input_key == KEY
        assert kwargs["run_plan"] == PLAN
        assert kwargs["execution_arn"] == EXEC
        assert kwargs["store"] is review_env.store
        assert kwargs["output_bucket"] == review_env.bucket
        assert kwargs["mode"] == "pipeline"

    @pytest.mark.parametrize("execution_input", ["absent", None, {}, {"document": {}}])
    def test_a_run_without_a_plan_is_unplanned(self, review_env, execution_input):
        """A run that started before the state machine passed its input."""
        decide, calls = _spy()

        response, _ = _run(
            review_env,
            document(section(review_env, "1")),
            execution_input,
            decide=decide,
        )

        assert calls[0][2]["run_plan"] is None
        assert calls[0][2]["mode"] == "pipeline"
        assert response["decision"] == "continue"

    @pytest.mark.parametrize(
        "execution_input, mode",
        [
            ({"runPlan": {**PLAN, "mode": "bda"}}, "bda"),
            # The plan's mode wins over the flag the run started with.
            ({"runPlan": PLAN, "document": {"use_bda": True}}, "pipeline"),
            # No plan: the flag on the document the run started with.
            ({"document": {"use_bda": True}}, "bda"),
            ({"document": {"use_bda": False}}, "pipeline"),
            ({"document": {"use_bda": "true"}}, "pipeline"),
        ],
    )
    def test_the_mode(self, review_env, execution_input, mode):
        decide, calls = _spy()

        _run(
            review_env,
            document(section(review_env, "1")),
            execution_input,
            decide=decide,
        )

        assert calls[0][2]["mode"] == mode


class TestTheConfiguration:
    def test_it_is_the_revision_the_document_is_pinned_to(self, review_env):
        doc = document(section(review_env, "1"))
        doc.config_version = "lending"
        doc.config_revision = 4

        _, reads = _run(review_env, doc, {"runPlan": PLAN})

        assert reads == [{"as_model": True, "version": "lending", "revision": 4}]


class TestMetering:
    def test_the_step_is_metered_as_routing(self, review_env):
        doc = document(section(review_env, "1"))
        doc.metering = {"Extraction/bedrock/m": {"inputTokens": 7}}

        response, _ = _run(review_env, doc, {"runPlan": PLAN})

        metering = response["document"]["metering"]
        assert metering["Routing/lambda/requests"] == {"requests": 1}
        assert "gb_seconds" in metering["Routing/lambda/duration"]
        # What the document carried is kept.
        assert metering["Extraction/bedrock/m"] == {"inputTokens": 7}


class TestFailures:
    def test_a_transient_cause_is_raised_under_the_name_the_state_retries(
        self, review_env
    ):
        def throttled(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                "GetItem",
            )

        with pytest.raises(TransientError):
            _run(review_env, document(section(review_env, "1")), decide=throttled)

    def test_a_throttled_queue_definition_read_is_retried_not_skipped(
        self, review_env, monkeypatch
    ):
        """The real ``decide``: a queue that could not be read for a moment is
        not a queue that is off, so the state retries and nothing continues."""

        def throttled(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                "Query",
            )

        monkeypatch.setattr(review_env.store.queue_defs, "list", throttled)
        doc = document(section(review_env, "1", alert=True))

        with pytest.raises(TransientError, match="Default review queue definition"):
            _run(review_env, doc, {"runPlan": PLAN})

        assert review_env.store.get_item(KEY) is None

    def test_a_fault_fails_the_run_as_itself(self, review_env):
        def broken(*args, **kwargs):
            raise ValueError("not a table")

        with pytest.raises(ValueError, match="not a table"):
            _run(review_env, document(section(review_env, "1")), decide=broken)
