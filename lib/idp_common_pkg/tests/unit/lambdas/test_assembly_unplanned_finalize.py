# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The finalize run of a review that started without a run plan.

A finalize message queued before the upgrade carries no plan, so its run goes
through the whole pipeline instead of starting at the step that applies the
review. Under the definition with a routing step the assembly handlers still
recognise that run and write the reviewer's corrections to the section results,
so policy rule validation reads the corrected values. They add no review key:
the routing step answers what the run does next.

A review that decided its run starts at extraction is the exception: its section
results are new, so the assembly copies no draft over them. The step that applies
the review merges the reviewer's corrections in.
"""

from __future__ import annotations

import pytest

from tests.unit.lambdas._assembly import run_bda_skip, run_pipeline, section_result
from tests.unit.lambdas._review_env import (
    KEY,
    MACHINE,
    config,
    document,
    draft,
    finalizing_item,
    section,
    stored_result,
)

pytestmark = pytest.mark.unit

RESPONSE_KEYS = {
    "pipeline": {"document", "hitl_triggered", "rule_validation_enabled"},
    "bda_skip": {
        "document",
        "hitl_triggered",
        "rule_validation_enabled",
        "bda_response_count",
        "skip_bda",
    },
}
TASKS = sorted(RESPONSE_KEYS)


def _run(task, env, *, routing_step=True):
    """An assembly task over the reviewed one-section document."""
    reviewed = section(env, "1", alert=True)
    if task == "pipeline":
        return run_pipeline(
            env.store,
            document(),
            [section_result(reviewed)],
            cfg=config(),
            routing_step=routing_step,
        )
    return run_bda_skip(
        env.store, document(reviewed), cfg=config(), routing_step=routing_step
    )


@pytest.mark.parametrize("task", TASKS)
class TestACompletedReview:
    def test_the_corrections_are_applied(self, review_env, task):
        finalizing_item(review_env)
        draft(review_env, "1")

        _run(task, review_env)

        assert stored_result(review_env, "1")["inference_result"] == {
            "Vendor": "ACME Corp",
            "Total": 120,
        }

    def test_no_response_key_is_added(self, review_env, task):
        finalizing_item(review_env)
        draft(review_env, "1")

        response = _run(task, review_env)

        assert set(response) == RESPONSE_KEYS[task]

    def test_the_item_records_that_its_finalize_run_started(self, review_env, task):
        finalizing_item(review_env)

        _run(task, review_env)

        item = review_env.store.get_item(KEY)
        assert item.state == "FINALIZING"
        assert item.finalize_started_at

    def test_the_earlier_definition_answers_complete_itself(self, review_env, task):
        """The same run under the definition without a routing step."""
        finalizing_item(review_env)
        draft(review_env, "1")

        response = _run(task, review_env, routing_step=False)

        assert response["review_outcome"] == "complete"
        assert stored_result(review_env, "1")["inference_result"]["Total"] == 120


@pytest.mark.parametrize("task", TASKS)
class TestAReviewWhoseRunStartedAtExtraction:
    """``restart_step`` on the item is ``extraction``: the results the assembly
    finds were extracted by this run, after the reviewer's draft was saved."""

    def test_no_draft_is_copied_over_the_new_result(self, review_env, task):
        finalizing_item(review_env, restart_step="extraction", restart_sections=["1"])
        draft(review_env, "1")

        _run(task, review_env)

        assert stored_result(review_env, "1") == MACHINE

    def test_nor_when_the_run_extracted_every_section(self, review_env, task):
        finalizing_item(review_env, restart_step="extraction")
        draft(review_env, "1")

        _run(task, review_env)

        assert stored_result(review_env, "1") == MACHINE

    def test_the_run_is_still_recognised_as_the_finalize_run(self, review_env, task):
        finalizing_item(review_env, restart_step="extraction")
        draft(review_env, "1")

        response = _run(task, review_env)

        assert set(response) == RESPONSE_KEYS[task]
        assert review_env.store.get_item(KEY).finalize_started_at

    def test_the_earlier_definition_copies_none_either(self, review_env, task):
        finalizing_item(review_env, restart_step="extraction")
        draft(review_env, "1")

        response = _run(task, review_env, routing_step=False)

        assert response["review_outcome"] == "complete"
        assert stored_result(review_env, "1") == MACHINE

    def test_a_start_at_applying_the_changes_still_has_its_drafts_applied(
        self, review_env, task
    ):
        finalizing_item(review_env, restart_step="apply_review_changes")
        draft(review_env, "1")

        _run(task, review_env)

        assert stored_result(review_env, "1")["inference_result"] == {
            "Vendor": "ACME Corp",
            "Total": 120,
        }

    def test_an_item_with_no_stored_start_still_has_its_drafts_applied(
        self, review_env, task
    ):
        """An item completed before the start was recorded."""
        item = finalizing_item(review_env)
        assert item.restart_step is None
        draft(review_env, "1")

        _run(task, review_env)

        assert stored_result(review_env, "1")["inference_result"]["Total"] == 120


@pytest.mark.parametrize("task", TASKS)
class TestNothingToApply:
    def test_a_rejected_review_applies_nothing_and_adds_no_key(self, review_env, task):
        finalizing_item(review_env, disposition="rejected")
        draft(review_env, "1")

        response = _run(task, review_env)

        assert set(response) == RESPONSE_KEYS[task]
        assert stored_result(review_env, "1") == MACHINE

    def test_another_run_of_the_document_applies_nothing(self, review_env, task):
        """Only the execution the item names is its finalize run."""
        finalizing_item(review_env, finalize_execution_name="some-other-run")
        draft(review_env, "1")

        response = _run(task, review_env)

        assert set(response) == RESPONSE_KEYS[task]
        assert stored_result(review_env, "1") == MACHINE
        assert not review_env.store.get_item(KEY).finalize_started_at

    def test_a_document_with_no_review_item_is_assembled(self, review_env, task):
        response = _run(task, review_env)

        assert set(response) == RESPONSE_KEYS[task]
        assert review_env.store.get_item(KEY) is None
