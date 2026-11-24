# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the two operations return.

``reprocessDocument`` answers with how many documents were queued and what
happened to each one, so a caller can tell a refused document from a queued one.
``getReprocessOptions`` lists, per document, the steps a run can start at. Both
run through the real ``ReprocessService`` here, over doubles of the tracking
table, S3 and the queue.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from _harness import OPTIONS, load, rest_event

from idp_common.models import Document, Page, Section, Status
from idp_common.run_plan.models import RUN_PLAN_ATTRIBUTE, decode
from idp_common.run_plan.reprocess import ReprocessOutcome

pytestmark = pytest.mark.unit

index = load("reprocess_resolver_index_results")


def _processed(key):
    """A document that has been through the whole pipeline."""
    return Document(
        id=key,
        input_bucket="test-in",
        input_key=key,
        output_bucket="test-out",
        status=Status.COMPLETED,
        num_pages=1,
        config_version="lending",
        pages={
            "1": Page(
                page_id="1",
                image_uri=f"s3://test-out/{key}/pages/1/image.jpg",
                raw_text_uri=f"s3://test-out/{key}/pages/1/rawText.json",
                parsed_text_uri=f"s3://test-out/{key}/pages/1/result.json",
                text_confidence_uri=f"s3://test-out/{key}/pages/1/textConfidence.json",
                classification="Invoice",
            )
        },
        sections=[
            Section(
                section_id="1",
                classification="Invoice",
                page_ids=["1"],
                extraction_result_uri=f"s3://test-out/{key}/sections/1/result.json",
            )
        ],
    )


@pytest.fixture
def deployment(monkeypatch):
    """The handler over doubles of the tracking table, S3 and the queue."""
    documents = MagicMock()
    documents.get_document.return_value = None
    sqs = MagicMock()
    sqs.send_message.return_value = {"MessageId": "msg-1"}
    monkeypatch.setattr(index, "document_service", documents)
    monkeypatch.setattr(index, "sqs_client", sqs)
    monkeypatch.setattr(index, "s3_client", MagicMock())
    monkeypatch.setattr(index, "cloudwatch", MagicMock())
    monkeypatch.setattr(
        "idp_common.run_plan.reprocess.delete_current_output_objects",
        MagicMock(return_value=0),
    )
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    monkeypatch.delenv("HITL_TABLE_NAME", raising=False)
    monkeypatch.setattr(
        index, "sanitize_event_for_logging", lambda event: {"redacted": True}
    )
    index._REVIEW_SERVICE_CACHE.clear()
    return SimpleNamespace(documents=documents, sqs=sqs)


def _sent_plans(deployment):
    return {
        call.kwargs["MessageAttributes"]["ObjectKey"]["StringValue"]: decode(
            call.kwargs["MessageAttributes"][RUN_PLAN_ATTRIBUTE]["StringValue"]
        )
        for call in deployment.sqs.send_message.call_args_list
    }


class TestReprocessDocument:
    def test_mixed_outcomes_give_the_count_and_a_result_for_each(self, deployment):
        quarantined = MagicMock()
        quarantined.status = Status.QUARANTINED
        deployment.documents.get_document.side_effect = lambda key, **_kwargs: (
            quarantined if key == "claims/infected.pdf" else None
        )

        result = index.handler(
            rest_event(["Admin"], ("claims/ok.pdf", "claims/infected.pdf")), None
        )

        assert result["queued"] == 1
        ok, refused = result["results"]
        assert ok == {
            "objectKey": "claims/ok.pdf",
            "status": "QUEUED",
            "startStep": "beginning",
            "reason": None,
            "message": None,
        }
        assert (refused["objectKey"], refused["status"]) == (
            "claims/infected.pdf",
            "REFUSED",
        )
        assert refused["reason"] and refused["message"]
        assert list(_sent_plans(deployment)) == ["claims/ok.pdf"]

    def test_the_results_are_in_the_order_asked(self, deployment):
        keys = ("c.pdf", "a.pdf", "b.pdf")

        result = index.handler(rest_event(["Admin"], keys), None)

        assert [r["objectKey"] for r in result["results"]] == list(keys)
        assert result["queued"] == 3

    def test_the_start_step_and_the_skipped_steps_reach_the_run_plan(self, deployment):
        deployment.documents.get_document.side_effect = lambda key, **_kw: _processed(
            key
        )

        result = index.handler(
            rest_event(
                ["Admin"],
                ("claims/a.pdf",),
                startStep="policy_rule_validation",
                skipSteps=["summarization"],
            ),
            None,
        )

        (only,) = result["results"]
        assert (only["status"], only["startStep"]) == (
            "QUEUED",
            "policy_rule_validation",
        )
        plan = _sent_plans(deployment)["claims/a.pdf"]
        assert plan.start == "policy_rule_validation"
        assert list(plan.skip) == ["summarization"]

    def test_no_start_step_is_a_run_from_the_beginning(self, deployment):
        index.handler(rest_event(["Admin"]), None)

        assert _sent_plans(deployment)["claims/a.pdf"].start == "beginning"

    def test_a_step_that_does_not_exist_is_a_refused_result(self, deployment):
        result = index.handler(rest_event(["Admin"], startStep="nowhere"), None)

        assert result["queued"] == 0
        (only,) = result["results"]
        assert only["status"] == "REFUSED"
        assert only["reason"]
        deployment.sqs.send_message.assert_not_called()

    def test_every_call_gets_a_result_object(self, deployment):
        assert index.handler(rest_event(["Admin"], ()), None) == {
            "queued": 0,
            "results": [],
        }

    def test_an_empty_value_is_null_in_the_result(self, monkeypatch):
        outcome = ReprocessOutcome(object_key="a.pdf", status="FAILED")

        assert index._result(outcome) == {
            "objectKey": "a.pdf",
            "status": "FAILED",
            "startStep": None,
            "reason": None,
            "message": None,
        }


class TestGetReprocessOptions:
    def _options(self, deployment, keys=("claims/a.pdf",), **arguments):
        return index.handler(
            rest_event(["Admin"], keys, field=OPTIONS, **arguments), None
        )

    def test_a_document_gets_its_mode_and_its_steps(self, deployment):
        deployment.documents.get_document.side_effect = lambda key, **_kw: _processed(
            key
        )

        (option,) = self._options(deployment)

        assert option["objectKey"] == "claims/a.pdf"
        assert option["mode"] == "pipeline"
        assert option["defaultStep"] == "beginning"
        steps = {step["id"]: step for step in option["steps"]}
        assert list(steps)[0] == "beginning"
        assert {"classification", "extraction", "assessment"} <= set(steps)
        assert steps["beginning"]["available"] is True
        assert steps["beginning"]["skippable"] is False
        assert steps["assessment"]["skippable"] is True
        assert all(
            set(step) == {"id", "available", "skippable", "reason"}
            for step in option["steps"]
        )

    def test_a_step_that_cannot_be_started_says_why(self, deployment):
        """A document with no tracking row has nothing to restart from later."""
        (option,) = self._options(deployment)

        steps = {step["id"]: step for step in option["steps"]}
        assert steps["beginning"]["available"] is True
        assert steps["extraction"]["available"] is False
        assert steps["extraction"]["reason"]

    def test_there_is_an_entry_for_each_document_in_the_order_asked(self, deployment):
        options = self._options(deployment, ("b.pdf", "a.pdf"))

        assert [option["objectKey"] for option in options] == ["b.pdf", "a.pdf"]

    def test_it_reads_only(self, deployment):
        self._options(deployment)

        deployment.sqs.send_message.assert_not_called()
        deployment.documents.create_document.assert_not_called()
        deployment.documents.update_document.assert_not_called()

    def test_no_documents_gives_no_options(self, deployment):
        assert self._options(deployment, ()) == []
