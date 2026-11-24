# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The handler that applies a completed review
(``processresults_function/apply_review_changes_handler.py``).

The real handler with the real ``apply_review_changes``, against a moto review
table and output bucket: what it answers, what it stored, and what it wrote to
the tracking row.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from idp_common.models import Document, Status
from idp_common.review_queues.exceptions import ReviewConflict
from idp_common.review_queues.service import BASE_KEY_FORMAT
from idp_common.utils.transient_errors import TransientError
from tests.unit.lambdas._goldens import load_handler
from tests.unit.lambdas._review_env import (
    ALERT,
    CYCLE,
    EXEC,
    KEY,
    LAMBDA_CONTEXT,
    MACHINE,
    REVIEWER,
    config,
    document,
    draft,
    finalizing_item,
    put_json,
    section,
    stored_result,
    swapped,
)

pytestmark = pytest.mark.unit

apply_index = load_handler(
    "apply_review_changes_handler_index",
    "processresults_function/apply_review_changes_handler.py",
)

PLAN = {
    "v": 1,
    "kind": "review_completed",
    "startStep": "apply_review_changes",
    "startAt": "apply_review_changes",
    "mode": "pipeline",
    "reviewed": True,
}
#: The plan of a completed review whose run extracts some sections again.
EXTRACTION_PLAN = {
    "v": 1,
    "kind": "review_completed",
    "startStep": "extraction",
    "startAt": "pipeline.extraction_sections",
    "mode": "pipeline",
    "reviewed": False,
    "sections": ["1"],
}


def _run(env, doc, *, cfg=None, plan=PLAN, **swaps):
    """Run the handler; returns (response, the tracking-row writes)."""
    writes = []
    service = MagicMock()
    service.update_document.side_effect = lambda d: writes.append(
        {
            "status": d.status,
            "alerts": [list(s.confidence_threshold_alerts) for s in d.sections],
            "alert_count": d.confidence_alert_count,
        }
    )
    attributes = dict(
        get_config=lambda **kw: cfg if cfg is not None else config(),
        create_document_service=lambda *a, **kw: service,
        _REVIEW_STORE=env.store,
    )
    attributes.update(swaps)
    with swapped(apply_index, **attributes):
        response = apply_index.handler(
            {
                "execution_arn": EXEC,
                "document": doc.to_dict(),
                "execution_input": {"runPlan": plan},
            },
            LAMBDA_CONTEXT,
        )
    return response, writes


def _reviewed(env):
    """A completed review with a correction of section 1, and its document."""
    finalizing_item(env, sections=["1", "2"])
    draft(env, "1")
    # The status a run has when its first step begins.
    return document(
        section(env, "1", alert=True), section(env, "2"), status=Status.RUNNING
    )


class TestTheAnswer:
    def test_the_response_is_the_document_and_the_policy_flag(self, review_env):
        response, _ = _run(review_env, _reviewed(review_env))

        assert set(response) == {"document", "rule_validation_enabled"}
        returned = Document.from_dict(response["document"])
        assert returned.status == Status.POSTPROCESSING
        assert [s.section_id for s in returned.sections] == ["1", "2"]
        # The review answered the alert.
        assert returned.sections[0].confidence_threshold_alerts == []

    @pytest.mark.parametrize("policies", [True, False])
    def test_the_policy_flag_follows_the_configuration(self, review_env, policies):
        response, _ = _run(
            review_env, _reviewed(review_env), cfg=config(policies=policies)
        )

        assert response["rule_validation_enabled"] is policies


class TestWhatItStores:
    def test_the_corrections_and_the_review_record_are_on_the_results(self, review_env):
        _run(review_env, _reviewed(review_env))

        corrected = stored_result(review_env, "1")
        assert corrected["inference_result"] == {"Vendor": "ACME Corp", "Total": 120}
        assert corrected["review"]["cycle_id"] == CYCLE
        assert corrected["review"]["reviewed_by"] == REVIEWER
        assert corrected["review"]["changed_paths"] == ["Vendor", "Total"]
        untouched = stored_result(review_env, "2")
        assert untouched["inference_result"] == {"Vendor": "ACME", "Total": 100}
        assert untouched["review"]["confirmed"] is True

    def test_the_tracking_row_is_written_once_after_the_review_is_applied(
        self, review_env
    ):
        _, writes = _run(review_env, _reviewed(review_env))

        assert writes == [
            {"status": Status.POSTPROCESSING, "alerts": [[], []], "alert_count": 0}
        ]


class TestARunThatExtractedAgain:
    """The step after the extraction and the assembly of such a run."""

    def _extracted(self, env):
        """Section 1 was extracted again; the reviewer had corrected its vendor."""
        finalizing_item(
            env,
            sections=["1", "2"],
            restart_step="extraction",
            restart_sections=["1"],
        )
        put_json(
            env, BASE_KEY_FORMAT.format(doc=KEY, cycle=CYCLE, section="1"), MACHINE
        )
        draft(
            env,
            "1",
            corrected={"inference_result": {"Vendor": "ACME Corp", "Total": 100}},
        )
        new = {**MACHINE, "inference_result": {"Vendor": "ACME Inc", "Total": 105}}
        return document(
            section(env, "1", alert=True, result=new),
            section(env, "2", alert=True),
            status=Status.RUNNING,
        )

    def test_the_corrections_are_set_on_the_new_result(self, review_env):
        _run(review_env, self._extracted(review_env), plan=EXTRACTION_PLAN)

        merged = stored_result(review_env, "1")
        # The vendor the reviewer corrected, and the total the extraction read.
        assert merged["inference_result"] == {"Vendor": "ACME Corp", "Total": 105}
        assert merged["review"]["confirmed"] is False
        assert merged["review"]["extracted_again"] is True
        assert merged["review"]["changed_paths"] == ["Vendor"]
        assert merged["review"]["unapplied_paths"] == []
        assert stored_result(review_env, "2")["review"]["confirmed"] is True

    def test_the_row_keeps_the_alert_the_corrections_did_not_answer(self, review_env):
        response, writes = _run(
            review_env, self._extracted(review_env), plan=EXTRACTION_PLAN
        )

        # Section 1's alert is on its total, which the reviewer did not correct.
        assert writes == [
            {"status": Status.POSTPROCESSING, "alerts": [[ALERT], []], "alert_count": 1}
        ]
        returned = Document.from_dict(response["document"])
        assert returned.sections[0].confidence_threshold_alerts == [ALERT]
        assert returned.sections[1].confidence_threshold_alerts == []
        assert returned.confidence_alert_count == 1

    def test_a_retry_writes_the_same_objects(self, review_env):
        doc = self._extracted(review_env)
        _run(review_env, doc, plan=EXTRACTION_PLAN)
        first = {sid: stored_result(review_env, sid) for sid in ("1", "2")}

        _run(review_env, doc, plan=EXTRACTION_PLAN)

        assert {sid: stored_result(review_env, sid) for sid in ("1", "2")} == first


class TestMetering:
    def test_the_step_is_metered_under_its_own_name(self, review_env):
        doc = _reviewed(review_env)
        doc.metering = {"Extraction/bedrock/m": {"inputTokens": 7}}

        response, _ = _run(review_env, doc)

        metering = response["document"]["metering"]
        assert metering["ApplyReviewChanges/lambda/requests"] == {"requests": 1}
        assert "gb_seconds" in metering["ApplyReviewChanges/lambda/duration"]
        assert metering["Extraction/bedrock/m"] == {"inputTokens": 7}


class TestFailures:
    def test_a_run_that_follows_no_completed_review_fails(self, review_env):
        """Nothing is applied and the row is not written."""
        finalizing_item(review_env, disposition="rejected")
        doc = document(section(review_env, "1", alert=True))

        with pytest.raises(ReviewConflict) as refused:
            _run(review_env, doc)

        assert refused.value.code == "NOT_A_COMPLETED_REVIEW"
        assert "review" not in stored_result(review_env, "1")

    def test_a_deployment_without_the_review_table_fails(self, review_env):
        with pytest.raises(RuntimeError, match="HITL_TABLE_NAME is not set"):
            _run(review_env, _reviewed(review_env), _REVIEW_STORE=None)

    def test_a_transient_cause_is_raised_under_the_name_the_state_retries(
        self, review_env
    ):
        def throttled(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                "Query",
            )

        with pytest.raises(TransientError):
            _run(review_env, _reviewed(review_env), apply_review_changes=throttled)

    def test_a_retry_writes_the_same_objects(self, review_env):
        """The state retries the step: a second run leaves what the first did."""
        doc = _reviewed(review_env)
        _run(review_env, doc)
        first = {sid: stored_result(review_env, sid) for sid in ("1", "2")}

        _run(review_env, doc)

        assert {sid: stored_result(review_env, sid) for sid in ("1", "2")} == first
