# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Skip All Reviews on a queue item: the ``skipped`` disposition (moto HITLTable)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from idp_common.review_queues import tracker
from idp_common.review_queues.exceptions import (
    ReviewAccessDenied,
    ReviewConflict,
    ReviewHeldByOther,
    ReviewValidationError,
)
from idp_common.review_queues.models import CallerContext
from idp_common.review_queues.statuses import QueueItemState

from .test_remove_and_delete_queue import (  # noqa: F401 - pytest fixture
    ADMIN,
    ANN,
    CYCLE,
    _history_events,
    _seed,
    service,
)

REASON = "Duplicate document"
REV = CallerContext(
    reviewer_key="rev@example.com",
    email="rev@example.com",
    groups=frozenset({"Reviewer"}),
)
OTHER = CallerContext(
    reviewer_key="other@example.com",
    email="other@example.com",
    groups=frozenset({"Reviewer"}),
)
SCOPED = CallerContext(
    reviewer_key="scoped@example.com",
    email="scoped@example.com",
    groups=frozenset({"Reviewer"}),
    allowed_versions=["claims-*"],
)


@pytest.mark.unit
class TestSkipAllReviews:
    def test_a_reviewer_skips_an_open_item(self, service):  # noqa: F811
        _seed(service, "a.pdf")
        view, sent = service.skip_all_reviews(
            REV, "a.pdf", CYCLE, reason=REASON, note="noise", job_id="brj-1"
        )
        stored = service.store.get_item("a.pdf")
        assert (stored.state, stored.disposition) == ("FINALIZING", "skipped")
        assert stored.skip_job_id == "brj-1" and stored.disposed_by == REV.email
        assert sent is True and view.disposition == "skipped"
        assert _history_events(service, "a.pdf") == ["skipped"]
        [entry] = service.store.list_history("a.pdf", CYCLE)
        assert entry.details == {
            "reason": REASON,
            "note": "noise",
            "source": "ui",
            "jobId": "brj-1",
        }
        service.notify.claim_released.assert_not_called()

    def test_a_reviewer_skips_their_own_claim_without_a_notice(
        self,
        service,  # noqa: F811
    ):
        _seed(service, "b.pdf", state="CLAIMED", owner=REV)
        service.skip_all_reviews(REV, "b.pdf", CYCLE, reason=REASON)
        assert service.store.get_item("b.pdf").state == "FINALIZING"
        assert service.store.counter(REV.reviewer_key) == -1
        service.notify.claim_released.assert_not_called()

    def test_a_reviewer_may_not_skip_another_reviewers_claim(
        self,
        service,  # noqa: F811
    ):
        _seed(service, "c.pdf", state="CLAIMED", owner=OTHER)
        with pytest.raises(ReviewHeldByOther) as exc:
            service.skip_all_reviews(REV, "c.pdf", CYCLE, reason=REASON)
        assert exc.value.code == "HELD_BY_OTHER"
        assert service.store.get_item("c.pdf").state == "CLAIMED"

    def test_an_admin_skips_a_claim_and_the_claimant_is_told(
        self,
        service,  # noqa: F811
    ):
        _seed(service, "d.pdf", state="CLAIMED", owner=ANN)
        service.skip_all_reviews(ADMIN, "d.pdf", CYCLE, reason=REASON)
        _item, email, reason = service.notify.claim_released.call_args.args
        assert (email, reason) == (ANN.email, "skipped")

    def test_out_of_scope_is_refused(self, service):  # noqa: F811
        _seed(service, "e.pdf")
        with pytest.raises(ReviewAccessDenied) as exc:
            service.skip_all_reviews(SCOPED, "e.pdf", CYCLE, reason=REASON)
        assert exc.value.code == "OUT_OF_SCOPE"

    @pytest.mark.parametrize("state", ["HOLDING", "FINALIZING", "COMPLETE"])
    def test_only_open_or_claimed_items(self, service, state):  # noqa: F811
        _seed(service, "f.pdf", state=state)
        with pytest.raises(ReviewConflict) as exc:
            service.skip_all_reviews(ADMIN, "f.pdf", CYCLE, reason=REASON)
        assert exc.value.code == "INVALID_STATE"

    def test_the_reason_must_be_a_skip_all_reason(self, service):  # noqa: F811
        _seed(service, "g.pdf")
        with pytest.raises(ReviewValidationError) as exc:
            service.skip_all_reviews(ADMIN, "g.pdf", CYCLE, reason="Spot check")
        assert exc.value.code == "REASON_REQUIRED"

    def test_a_failed_finalize_send_is_recorded_in_history(
        self,
        service,  # noqa: F811
    ):
        service.finalize_sender.send.side_effect = RuntimeError("sqs down")
        _seed(service, "h.pdf")
        _view, sent = service.skip_all_reviews(
            ADMIN, "h.pdf", CYCLE, reason=REASON, job_id="brj-2"
        )
        assert sent is False
        assert service.store.get_item("h.pdf").state == "FINALIZING"
        assert sorted(_history_events(service, "h.pdf")) == [
            "finalize_send_failed",
            "skipped",
        ]


@pytest.mark.unit
class TestSkippedFinalizeRun:
    def test_completes_with_machine_results_and_deletes_the_status_object(
        self,
        service,  # noqa: F811
    ):
        _seed(service, "s.pdf")
        service.skip_all_reviews(REV, "s.pdf", CYCLE, reason=REASON)
        item = service.store.get_item("s.pdf")
        ctx = tracker.load_context(
            "finalize", item.finalize_token, "s.pdf", None, service.store
        )
        assert ctx is not None and ctx.outcome == "complete"
        s3 = MagicMock()
        documents = MagicMock()
        tracker.after_completion(
            ctx,
            workflow_status="SUCCEEDED",
            run_ref=None,
            document=SimpleNamespace(metering={}),
            store=service.store,
            s3=s3,
            output_bucket="out",
            notifier=MagicMock(),
            document_service=documents,
            retention_days=30,
        )
        s3.delete_content.assert_called_once_with("out", "s.pdf/review/status.json")
        s3.write_content.assert_not_called()
        assert (
            "ReviewStatusUri"
            in documents.update_review_attributes.call_args.kwargs["remove_attrs"]
        )
        done = service.store.get_item("s.pdf")
        assert (done.state, done.disposition) == (
            QueueItemState.COMPLETE.value,
            "skipped",
        )
        assert tracker.reporting_info(ctx).disposition == "skipped"
