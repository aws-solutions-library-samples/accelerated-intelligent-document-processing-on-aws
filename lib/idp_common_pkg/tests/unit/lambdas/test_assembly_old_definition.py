# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A run that started under the definition before the routing step keeps working.

An execution keeps the state machine definition it started with, but calls the
Lambda code that is deployed now. A run in progress across an upgrade therefore
reaches the assembly handlers with the Parameters the earlier definition sends:
no ``routing_step``. For that run the handlers make the review decision
themselves, as they did. The definition with a routing step sends
``routing_step: true``, and the same input then writes no review item and adds
no review key: the routing step decides.
"""

from __future__ import annotations

import pytest

from idp_common.review_queues.statuses import QueueItemState
from tests.unit.lambdas._assembly import (
    TASKS,
    assembled,
    run_bda,
    run_bda_skip,
    run_pipeline,
    section_result,
)
from tests.unit.lambdas._review_env import EXEC, KEY, config, document, section

pytestmark = pytest.mark.unit

#: What every assembly task returns, in either definition.
COMMON_KEYS = {"document", "hitl_triggered", "rule_validation_enabled"}
#: What the BDA tasks add to it.
EXTRA_KEYS = {
    "pipeline": set(),
    "bda": {"bda_response_count"},
    "bda_skip": {"bda_response_count", "skip_bda"},
}
REVIEW_KEYS = {"review_hold", "review_outcome"}


def _run(task, env, *, store, routing_step, cfg=None, alert=True):
    """One assembly task over a one-section document with a confidence alert."""
    cfg = cfg if cfg is not None else config(policies=True)
    if task == "pipeline":
        return run_pipeline(
            store,
            document(),
            [section_result(section(env, "1", alert=alert))],
            cfg=cfg,
            routing_step=routing_step,
        )
    if task == "bda":
        section(env, "1")  # the section result the BDA copier would have stored
        return run_bda(
            store,
            cfg=cfg,
            routing_step=routing_step,
            field_confidence=0.4 if alert else 0.97,
        )
    return run_bda_skip(
        store,
        document(section(env, "1", alert=alert)),
        cfg=cfg,
        routing_step=routing_step,
    )


def _rest(response):
    """The response without its document (which may be a stored copy)."""
    return {key: value for key, value in response.items() if key != "document"}


@pytest.mark.parametrize("task", TASKS)
class TestTheDefinitionBeforeTheRoutingStep:
    def test_the_handler_holds_the_document_itself(self, review_env, task):
        response = _run(task, review_env, store=review_env.store, routing_step=False)

        assert response["review_hold"] is True
        assert response["hitl_triggered"] is False
        assert response["rule_validation_enabled"] is True
        assert set(response) == COMMON_KEYS | EXTRA_KEYS[task] | {"review_hold"}
        item = review_env.store.get_item(KEY)
        assert item.state == QueueItemState.HOLDING.value
        assert item.queue == "default"
        assert item.phase1_execution_arn == EXEC

    def test_a_document_nothing_asks_a_review_for_gets_no_review_key(
        self, review_env, task
    ):
        response = _run(
            task, review_env, store=review_env.store, routing_step=False, alert=False
        )

        assert set(response) == COMMON_KEYS | EXTRA_KEYS[task]
        assert review_env.store.get_item(KEY) is None


@pytest.mark.parametrize("task", TASKS)
class TestTheDefinitionWithTheRoutingStep:
    def test_the_same_input_writes_no_item_and_adds_no_review_key(
        self, review_env, task
    ):
        response = _run(task, review_env, store=review_env.store, routing_step=True)

        assert set(response) == COMMON_KEYS | EXTRA_KEYS[task]
        assert not REVIEW_KEYS & set(response)
        assert response["hitl_triggered"] is False
        assert response["rule_validation_enabled"] is True
        assert review_env.store.get_item(KEY) is None

    def test_the_document_is_the_one_the_earlier_definition_gets(
        self, review_env, task
    ):
        """Only the review decision moved: the assembled document is the same."""
        before = _run(task, review_env, store=review_env.store, routing_step=False)
        after = _run(task, review_env, store=review_env.store, routing_step=True)

        assert assembled(after) == assembled(before)
        assert assembled(after)["sections"][0]["section_id"] == "1"


@pytest.mark.parametrize("task", TASKS)
class TestTheLegacyFlow:
    """Without review queues the literal changes nothing at all."""

    def test_the_response_is_the_same_with_and_without_it(self, review_env, task):
        without = _run(task, review_env, store=None, routing_step=False)
        with_it = _run(task, review_env, store=None, routing_step=True)

        assert assembled(with_it) == assembled(without)
        assert _rest(with_it) == _rest(without)
        assert with_it["hitl_triggered"] is True
        assert not REVIEW_KEYS & set(with_it)
