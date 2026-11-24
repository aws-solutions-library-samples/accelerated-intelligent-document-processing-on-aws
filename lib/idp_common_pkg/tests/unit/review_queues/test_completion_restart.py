# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Where the run after a completed review starts (moto HITLTable).

``complete`` decides it from the document as the reviewer left it: at applying
the review's changes, or at extraction when a section was regrouped or has no
result. The decision is stored on the item and in the cycle's history, and the
results the run is about to write over are copied first. ``completion_plan``
returns the same decision before the reviewer confirms.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from idp_common.models import Document, Section
from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.exceptions import (
    ReviewAccessDenied,
    ReviewConflict,
    ReviewItemNotFound,
)
from idp_common.review_queues.models import CallerContext, ReviewItemRecord
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.service import (
    BASE_KEY_FORMAT,
    MAX_HISTORY_RESTART_SECTIONS,
    ReviewQueueService,
)
from idp_common.review_queues.store import ReviewStore, item_to_dynamo
from idp_common.run_plan.models import RunKind, RunPlan, RunPlanError, encode
from idp_common.run_plan.restart import layout_of

from ._table import REGION

pytestmark = pytest.mark.unit

ADMIN = CallerContext(
    reviewer_key="admin@example.com",
    email="admin@example.com",
    groups=frozenset({"Admin"}),
    is_admin=True,
)
REVIEWER = CallerContext(
    reviewer_key="rev@example.com",
    email="rev@example.com",
    groups=frozenset({"Reviewer"}),
)
OTHER_REVIEWER = CallerContext(
    reviewer_key="other@example.com",
    email="other@example.com",
    groups=frozenset({"Reviewer"}),
)
SCOPED_REVIEWER = CallerContext(
    reviewer_key="scoped@example.com",
    email="scoped@example.com",
    groups=frozenset({"Reviewer"}),
    allowed_versions=["another-profile"],
)
CYCLE = "0" * 21
KEY = "claims/a.pdf"
BUCKET = "out"
START = datetime(2026, 11, 16, 9, 0, 0, tzinfo=timezone.utc)

APPLY = "apply_review_changes"
EXTRACTION = "extraction"


def _uri(section_id):
    return f"s3://{BUCKET}/{KEY}/sections/{section_id}/result.json"


def _section(section_id, pages, *, result=True, excluded=False):
    return Section(
        section_id=section_id,
        classification="Invoice",
        page_ids=list(pages),
        extraction_result_uri=_uri(section_id) if result else None,
        excluded=excluded,
    )


def _document(*sections):
    document = Document(id=KEY, input_key=KEY)
    document.sections.extend(sections)
    return document


def _placed():
    """The document as it was placed for review: two sections, each with a result."""
    return _document(_section("1", ["1", "2"]), _section("2", ["3"]))


def _regrouped():
    """The reviewer moved page 2 out of section 1 into a new section 3."""
    return _document(
        _section("1", ["1"]),
        _section("2", ["3"]),
        _section("3", ["2"], result=False),
    )


class _Env:
    """A service over a moto table, with a clock and the document it loads."""

    def __init__(self, table, *, with_loader=True, configuration_of=None):
        self.now = START
        self.document = _placed()
        self.loads = []
        self.events = []
        self.s3 = MagicMock(name="s3")
        self.s3.copy_object.side_effect = lambda *a, **k: self.events.append("copy")
        self.sender = MagicMock(name="finalize_sender")
        self.store = ReviewStore(table, region=REGION)
        self.store.queue_defs.put(
            QueueDefinition(id="claims", priority=70), create=True
        )
        self.service = ReviewQueueService(
            self.store,
            QueueDirectory(self.store.queue_defs),
            notifier=MagicMock(),
            finalize_sender=self.sender,
            clock=lambda: self.now,
            s3=self.s3,
            document_loader=self._load if with_loader else None,
            output_bucket=BUCKET,
            document_service=MagicMock(),
            configuration_of=configuration_of,
        )
        replace = self.store.replace_item

        def recorded(*args, **kwargs):
            self.events.append("write")
            return replace(*args, **kwargs)

        self.store.replace_item = recorded

    def _load(self, key):
        self.loads.append(key)
        return self.document

    def seed(self, *, state="CLAIMED", owner=REVIEWER, mode="pipeline", **more):
        now_ms = int(self.now.timestamp() * 1000)
        fields = dict(
            doc_key=KEY,
            cycle_id=CYCLE,
            profile="default",
            config_revision=4,
            queue="claims",
            state=state,
            enqueued_at="2026-11-16T00:00:00.000Z",
            enqueued_at_ms=now_ms,
            sections=["1", "2"],
            version=1,
            processing_mode=mode,
            placed_layout=layout_of(_placed()),
        )
        if state == "CLAIMED" and owner is not None:
            fields.update(
                owner_key=owner.reviewer_key,
                owner_email=owner.email,
                claimed_at_ms=now_ms,
                lease_minutes=60,
                lease_expires_at_ms=now_ms + 3_600_000,
            )
        if state in ("HOLDING", "FINALIZING"):
            fields["sweep_at_ms"] = now_ms + 600_000
        fields.update(more)
        record = ReviewItemRecord(**fields)
        self.store.client.put_item(item_to_dynamo(record))
        return record

    def draft(self, section_id, caller=REVIEWER):
        self.service.save_draft(
            caller, KEY, CYCLE, section_id, {"inference_result": {"Total": 5}}, None
        )
        self.events.clear()
        # History entries are ordered by their time: what follows comes later.
        self.now += timedelta(seconds=1)

    def item(self):
        return self.store.get_item(KEY)

    def history(self):
        return [(h.event, h.details) for h in self.store.list_history(KEY, CYCLE)]

    def copies(self):
        return [call.args for call in self.s3.copy_object.call_args_list]


@pytest.fixture
def env(review_table):
    return _Env(review_table)


def _base_key(section_id):
    return BASE_KEY_FORMAT.format(doc=KEY, cycle=CYCLE, section=section_id)


def _config(**sections):
    """A configuration in its dict form, with the named steps switched off."""
    return {name: {"enabled": enabled} for name, enabled in sections.items()}


def _resolver(mode="pipeline", config=None, calls=None):
    def configuration_of(profile, revision):
        if calls is not None:
            calls.append((profile, revision))
        return SimpleNamespace(mode=mode, config={} if config is None else config)

    return configuration_of


class TestOnlyValuesWereCorrected:
    def test_the_run_starts_at_applying_the_reviews_changes(self, env):
        env.seed()
        env.draft("1")

        view = env.service.complete(REVIEWER, KEY, CYCLE)

        item = env.item()
        assert (item.state, item.disposition) == ("FINALIZING", "complete")
        assert (item.restart_step, item.restart_sections) == (APPLY, [])
        assert view.disposition == "complete"
        # Nothing is extracted again, so no result is copied.
        assert env.copies() == []

    def test_the_decision_is_what_the_finalize_message_is_sent_from(self, env):
        env.seed()

        env.service.complete(REVIEWER, KEY, CYCLE)

        (sent,) = [call.args[0] for call in env.sender.send.call_args_list]
        assert (sent.restart_step, sent.restart_sections) == (APPLY, [])

    def test_the_history_records_the_start(self, env):
        env.seed()

        env.service.complete(REVIEWER, KEY, CYCLE)

        assert env.history() == [
            ("completed", {"restart_step": APPLY, "restart_sections": []})
        ]

    def test_a_note_is_recorded_beside_it(self, env):
        env.seed()

        env.service.complete(REVIEWER, KEY, CYCLE, note="checked the totals")

        assert env.history() == [
            (
                "completed",
                {
                    "restart_step": APPLY,
                    "restart_sections": [],
                    "note": "checked the totals",
                },
            )
        ]
        assert env.item().complete_note == "checked the totals"

    def test_an_item_placed_before_the_layout_was_recorded(self, env):
        """With no layout to compare with, only a missing result extracts."""
        env.seed(placed_layout={})
        env.document = _document(_section("1", ["1"]), _section("9", ["2", "3"]))

        env.service.complete(REVIEWER, KEY, CYCLE)

        assert env.item().restart_step == APPLY


class TestASectionWasRegrouped:
    def test_the_run_starts_at_extraction_for_the_sections_that_changed(self, env):
        env.seed()
        env.document = _regrouped()

        env.service.complete(REVIEWER, KEY, CYCLE)

        item = env.item()
        assert (item.state, item.disposition) == ("FINALIZING", "complete")
        assert (item.restart_step, item.restart_sections) == (EXTRACTION, ["1", "3"])
        assert env.history() == [
            ("completed", {"restart_step": EXTRACTION, "restart_sections": ["1", "3"]})
        ]

    def test_the_result_the_reviewer_corrected_is_copied_first(self, env):
        """Extraction writes over a section's result. The result a draft was
        made from is copied to the review's base folder before the item is
        written, so the run cannot start before the copy exists."""
        env.seed()
        env.draft("1")
        env.draft("2")
        env.document = _regrouped()

        env.service.complete(REVIEWER, KEY, CYCLE)

        # Section 1: extracted again, with a draft and a result. Section 2 is not
        # extracted again; section 3 has no draft and no result.
        assert env.copies() == [
            (BUCKET, f"{KEY}/sections/1/result.json", BUCKET, _base_key("1"))
        ]
        assert _base_key("1") == f"{KEY}/review/base/{CYCLE}/1.json"
        assert env.events == ["copy", "write"]

    def test_the_copy_is_made_even_when_the_write_is_then_refused(self, env):
        """The proof of the order: the write fails (the claim expired), and the
        copy is there all the same."""
        env.seed()
        env.draft("1")
        env.document = _regrouped()
        env.now = START + timedelta(hours=2)

        with pytest.raises(ReviewConflict) as refused:
            env.service.complete(REVIEWER, KEY, CYCLE)

        assert refused.value.code == "LEASE_EXPIRED"
        assert env.copies() == [
            (BUCKET, f"{KEY}/sections/1/result.json", BUCKET, _base_key("1"))
        ]
        item = env.item()
        assert (item.state, item.restart_step) == ("CLAIMED", None)
        env.sender.send.assert_not_called()

    def test_a_copy_that_fails_leaves_the_review_as_it_was(self, env):
        env.seed()
        env.draft("1")
        env.document = _regrouped()
        env.s3.copy_object.side_effect = RuntimeError("SlowDown")

        with pytest.raises(RuntimeError, match="SlowDown"):
            env.service.complete(REVIEWER, KEY, CYCLE)

        item = env.item()
        assert (item.state, item.restart_step) == ("CLAIMED", None)
        assert env.history() == [("draft_saved", {"section": "1"})]
        env.sender.send.assert_not_called()

    def test_a_section_with_no_draft_is_not_copied(self, env):
        env.seed()
        env.document = _regrouped()

        env.service.complete(REVIEWER, KEY, CYCLE)

        assert env.item().restart_step == EXTRACTION
        assert env.copies() == []

    def test_an_excluded_section_is_not_extracted(self, env):
        env.seed()
        env.document = _document(
            _section("1", ["1", "2"]),
            _section("2", ["3"]),
            _section("3", ["4"], result=False, excluded=True),
        )

        env.service.complete(REVIEWER, KEY, CYCLE)

        assert env.item().restart_step == APPLY

    def test_a_section_with_no_result_is_extracted(self, env):
        env.seed()
        env.document = _document(
            _section("1", ["1", "2"]), _section("2", ["3"], result=False)
        )

        env.service.complete(REVIEWER, KEY, CYCLE)

        item = env.item()
        assert (item.restart_step, item.restart_sections) == (EXTRACTION, ["2"])


class TestAPlanTooLargeToName:
    """More sections than a run plan can name: the run extracts every one."""

    def _many(self):
        added = [_section(f"{n:03d}-" + "x" * 56, [str(n)]) for n in range(10, 110)]
        return _document(_section("1", ["1", "2"]), _section("2", ["3"]), *added)

    def test_the_sections_do_not_fit_a_plan(self):
        ids = [s.section_id for s in self._many().sections[2:]]

        with pytest.raises(RunPlanError):
            encode(
                RunPlan(
                    kind=RunKind.REVIEW_COMPLETED,
                    start=EXTRACTION,
                    sections=ids,
                    cycle_id=CYCLE,
                )
            )

    def test_no_section_is_named_which_means_every_one(self, env):
        env.seed()
        env.draft("1")
        env.document = self._many()

        env.service.complete(REVIEWER, KEY, CYCLE)

        item = env.item()
        assert (item.restart_step, item.restart_sections) == (EXTRACTION, [])
        assert env.history() == [
            ("draft_saved", {"section": "1"}),
            ("completed", {"restart_step": EXTRACTION, "restart_sections": []}),
        ]
        # Every processed section is extracted again, so the drafted one is
        # copied although its own pages did not change.
        assert env.copies() == [
            (BUCKET, f"{KEY}/sections/1/result.json", BUCKET, _base_key("1"))
        ]

    def test_the_history_names_a_bounded_number_of_sections(self, env):
        env.seed()
        added = [_section(f"n{n}", [str(n)]) for n in range(10, 90)]
        env.document = _document(
            _section("1", ["1", "2"]), _section("2", ["3"]), *added
        )

        env.service.complete(REVIEWER, KEY, CYCLE)

        item = env.item()
        assert len(item.restart_sections) == 80
        ((_, details),) = env.history()
        assert details["restart_sections"] == item.restart_sections[:50]
        assert MAX_HISTORY_RESTART_SECTIONS == 50


class TestTheProcessingMode:
    def test_bda_mode_never_extracts(self, env):
        """One call does OCR, classification and extraction there: a regrouped
        section cannot be extracted on its own."""
        env.seed(mode="bda")
        env.draft("1")
        env.document = _regrouped()

        env.service.complete(REVIEWER, KEY, CYCLE)

        item = env.item()
        assert (item.restart_step, item.restart_sections) == (APPLY, [])
        assert env.copies() == []

    def test_the_recorded_mode_is_used_without_reading_the_configuration(
        self, review_table
    ):
        calls = []
        env = _Env(review_table, configuration_of=_resolver("bda", calls=calls))
        env.seed(mode="pipeline")
        env.document = _regrouped()

        env.service.complete(REVIEWER, KEY, CYCLE)

        assert env.item().restart_step == EXTRACTION
        assert calls == []

    @pytest.mark.parametrize(
        "mode, start",
        [("pipeline", EXTRACTION), ("bda", APPLY)],
        ids=["pipeline", "bda"],
    )
    def test_an_item_sent_by_hand_takes_the_mode_of_its_configuration(
        self, review_table, mode, start
    ):
        """Send to review records no mode: it is resolved from the item's
        profile at its revision."""
        calls = []
        env = _Env(review_table, configuration_of=_resolver(mode, calls=calls))
        env.seed(mode=None, origin="manual")
        env.document = _regrouped()

        env.service.complete(REVIEWER, KEY, CYCLE)

        assert env.item().restart_step == start
        assert calls == [("default", 4)]

    def test_with_no_resolver_the_mode_is_unknown_and_nothing_is_extracted(self, env):
        """A guessed Pipeline mode on a BDA document would start it over and
        lose the corrections."""
        env.seed(mode=None, origin="manual")
        env.draft("1")
        env.document = _regrouped()

        env.service.complete(REVIEWER, KEY, CYCLE)

        item = env.item()
        assert (item.restart_step, item.restart_sections) == (APPLY, [])
        assert env.copies() == []

    def test_a_resolver_that_raises_leaves_the_mode_unknown(self, review_table, caplog):
        def broken(profile, revision):
            raise RuntimeError("throttled")

        env = _Env(review_table, configuration_of=broken)
        env.seed(mode=None, origin="manual")
        env.document = _regrouped()

        with caplog.at_level("WARNING"):
            env.service.complete(REVIEWER, KEY, CYCLE)

        assert env.item().restart_step == APPLY
        assert "event=review_configuration_unresolved" in caplog.text

    def test_a_configuration_that_could_not_be_read_leaves_the_mode_unknown(
        self, review_table, caplog
    ):
        """The resolver then answers Pipeline mode as a default, with no
        configuration: that is a guess, not the document's mode."""
        unread = lambda profile, revision: SimpleNamespace(  # noqa: E731
            mode="pipeline", config=None
        )
        env = _Env(review_table, configuration_of=unread)
        env.seed(mode=None, origin="manual")
        env.document = _regrouped()

        with caplog.at_level("WARNING"):
            env.service.complete(REVIEWER, KEY, CYCLE)

        assert env.item().restart_step == APPLY
        assert "event=review_mode_unknown" in caplog.text


class TestTheDocument:
    def test_a_document_that_no_longer_exists_is_not_found(self, env):
        env.seed()
        env.document = None

        with pytest.raises(ReviewItemNotFound) as missing:
            env.service.complete(REVIEWER, KEY, CYCLE)

        assert missing.value.code == "DOCUMENT_NOT_FOUND"
        item = env.item()
        assert (item.state, item.withdrawn_reason) == ("WITHDRAWN", "document_missing")
        env.sender.send.assert_not_called()

    def test_with_no_loader_the_run_applies_the_reviews_changes(self, review_table):
        env = _Env(review_table, with_loader=False)
        env.seed()

        env.service.complete(REVIEWER, KEY, CYCLE)

        item = env.item()
        assert (item.state, item.restart_step) == ("FINALIZING", APPLY)

    def test_the_document_is_read_once(self, env):
        env.seed()
        env.document = _regrouped()

        env.service.complete(REVIEWER, KEY, CYCLE)

        assert env.loads == [KEY]


class TestRefusedBeforeAnythingIsReadOrCopied:
    def test_someone_who_does_not_hold_the_item(self, env):
        env.seed()
        env.draft("1")
        env.document = _regrouped()

        with pytest.raises(ReviewAccessDenied) as refused:
            env.service.complete(OTHER_REVIEWER, KEY, CYCLE)

        assert refused.value.code == "NOT_LEASE_HOLDER"
        assert env.loads == [KEY]  # the draft save's read, not a second one
        assert env.copies() == []

    @pytest.mark.parametrize("state", ["OPEN", "FINALIZING"])
    def test_an_item_that_is_not_claimed(self, env, state):
        env.seed(state=state)
        env.document = None

        with pytest.raises(ReviewConflict) as refused:
            env.service.complete(ADMIN, KEY, CYCLE)

        # Not the missing document: the item's state is refused first, and the
        # review is not withdrawn.
        assert refused.value.code == "INVALID_STATE"
        assert env.item().state == state
        assert env.loads == []

    def test_an_older_cycle(self, env):
        env.seed()

        with pytest.raises(ReviewConflict) as refused:
            env.service.complete(REVIEWER, KEY, "1" * 21)

        assert refused.value.code == "STALE_CYCLE"
        assert env.loads == []

    def test_an_admin_completes_a_claim_they_do_not_hold(self, env):
        env.seed()
        env.document = _regrouped()

        env.service.complete(ADMIN, KEY, CYCLE)

        item = env.item()
        assert (item.state, item.disposed_by) == ("FINALIZING", ADMIN.email)
        assert item.restart_step == EXTRACTION


def _states(planned):
    return {step.id: step.state for step in planned.steps}


class TestTheCompletionPlan:
    def test_it_is_what_complete_then_stores(self, env):
        env.seed()
        env.draft("1")
        env.document = _regrouped()

        plan = env.service.completion_plan(REVIEWER, KEY, CYCLE)
        env.service.complete(REVIEWER, KEY, CYCLE)

        item = env.item()
        assert plan.complete.start_step == item.restart_step == EXTRACTION
        assert [s.section_id for s in plan.complete.sections] == item.restart_sections
        assert {s.section_id: s.cause for s in plan.complete.sections} == {
            "1": "pages_changed",
            "3": "added",
        }

    @pytest.mark.parametrize(
        "document, start",
        [(_placed, APPLY), (_regrouped, EXTRACTION)],
        ids=["values-only", "regrouped"],
    )
    def test_it_agrees_with_complete_for_either_start(self, env, document, start):
        env.seed()
        env.document = document()

        plan = env.service.completion_plan(REVIEWER, KEY, CYCLE)
        env.service.complete(REVIEWER, KEY, CYCLE)

        assert plan.complete.start_step == env.item().restart_step == start

    def test_values_only_lists_the_steps_from_applying_the_changes(self, env):
        env.seed()

        plan = env.service.completion_plan(REVIEWER, KEY, CYCLE)

        assert (plan.complete.mode, plan.complete.start_step) == ("pipeline", APPLY)
        assert plan.complete.sections == []
        assert _states(plan.complete) == {
            "beginning": "skipped",
            "classification": "skipped",
            "extraction": "skipped",
            "assessment": "skipped",
            APPLY: "run",
            "policy_rule_validation": "run",
            "review_routing": "run",
            "summarization": "run",
            "evaluation": "run",
        }

    def test_a_regrouped_document_lists_extraction_and_what_follows(self, env):
        env.seed()
        env.document = _regrouped()

        plan = env.service.completion_plan(REVIEWER, KEY, CYCLE)

        states = _states(plan.complete)
        assert states["classification"] == "skipped"
        assert (states["extraction"], states["assessment"], states[APPLY]) == (
            "run",
            "run",
            "run",
        )

    def test_send_back_starts_at_classification_and_applies_nothing(self, env):
        env.seed()

        plan = env.service.completion_plan(REVIEWER, KEY, CYCLE)

        assert (plan.send_back.mode, plan.send_back.start_step) == (
            "pipeline",
            "classification",
        )
        assert plan.send_back.sections == []
        states = _states(plan.send_back)
        assert APPLY not in states
        assert (states["beginning"], states["classification"]) == ("skipped", "run")

    def test_in_bda_mode(self, env):
        env.seed(mode="bda")
        env.document = _regrouped()

        plan = env.service.completion_plan(REVIEWER, KEY, CYCLE)

        assert (plan.complete.mode, plan.complete.start_step) == ("bda", APPLY)
        assert (plan.send_back.mode, plan.send_back.start_step) == ("bda", "beginning")
        assert "classification" not in _states(plan.complete)

    def test_a_step_the_configuration_switches_off_is_not_enabled(self, review_table):
        calls = []
        config = _config(summarization=False, hitl=True, evaluation=False)
        env = _Env(
            review_table, configuration_of=_resolver("bda", config=config, calls=calls)
        )
        env.seed(mode="pipeline")

        plan = env.service.completion_plan(REVIEWER, KEY, CYCLE)

        states = _states(plan.complete)
        assert (states["summarization"], states["evaluation"]) == (
            "not_enabled",
            "not_enabled",
        )
        assert states["review_routing"] == "run"
        assert _states(plan.send_back)["summarization"] == "not_enabled"
        # The item's recorded mode, not the resolver's; one configuration read.
        assert plan.complete.mode == "pipeline"
        assert calls == [("default", 4)]

    def test_with_no_resolver_every_step_reads_as_enabled(self, env):
        env.seed()

        plan = env.service.completion_plan(REVIEWER, KEY, CYCLE)

        assert "not_enabled" not in _states(plan.complete).values()

    @pytest.mark.parametrize(
        "resolver",
        [
            None,
            lambda profile, revision: SimpleNamespace(mode="pipeline", config=None),
        ],
        ids=["no-resolver", "configuration-unreadable"],
    )
    def test_an_unknown_mode_lists_the_pipeline_steps_from_applying_the_changes(
        self, review_table, resolver
    ):
        env = _Env(review_table, configuration_of=resolver)
        env.seed(mode=None, origin="manual")
        env.document = _regrouped()

        plan = env.service.completion_plan(REVIEWER, KEY, CYCLE)
        env.service.complete(REVIEWER, KEY, CYCLE)

        assert (plan.complete.mode, plan.complete.start_step) == ("pipeline", APPLY)
        assert plan.complete.sections == []
        assert (plan.send_back.mode, plan.send_back.start_step) == (
            "pipeline",
            "classification",
        )
        assert env.item().restart_step == APPLY

    def test_a_plan_too_large_names_no_section(self, env):
        env.seed()
        added = [_section(f"{n:03d}-" + "x" * 56, [str(n)]) for n in range(10, 110)]
        env.document = _document(
            _section("1", ["1", "2"]), _section("2", ["3"]), *added
        )

        plan = env.service.completion_plan(REVIEWER, KEY, CYCLE)

        assert plan.complete.start_step == EXTRACTION
        assert plan.complete.sections == []

    def test_it_is_dumped_in_the_api_shape(self, env):
        env.seed()
        env.document = _regrouped()

        dumped = env.service.completion_plan(REVIEWER, KEY, CYCLE).dump()

        assert set(dumped) == {"complete", "sendBack"}
        assert set(dumped["complete"]) == {"mode", "startStep", "steps", "sections"}
        assert dumped["complete"]["sections"] == [
            {"sectionId": "1", "cause": "pages_changed"},
            {"sectionId": "3", "cause": "added"},
        ]
        assert dumped["complete"]["steps"][2] == {
            "id": "extraction",
            "label": "Extraction",
            "state": "run",
        }

    def test_it_changes_nothing(self, env):
        before = env.seed()
        env.draft("1")
        env.document = _regrouped()
        drafted = env.item()

        env.service.completion_plan(REVIEWER, KEY, CYCLE)

        after = env.item()
        assert (after.state, after.version, after.restart_step) == (
            before.state,
            drafted.version,
            None,
        )
        assert env.history() == [("draft_saved", {"section": "1"})]
        assert env.copies() == []
        assert env.events == []


class TestWhoMayReadTheCompletionPlan:
    def test_the_holder_may(self, env):
        env.seed()

        assert env.service.completion_plan(REVIEWER, KEY, CYCLE).complete

    @pytest.mark.parametrize("state", ["OPEN", "CLAIMED"])
    def test_an_admin_may_on_an_open_or_a_claimed_item(self, env, state):
        env.seed(state=state)

        plan = env.service.completion_plan(ADMIN, KEY, CYCLE)

        assert plan.complete.start_step == APPLY

    def test_another_reviewer_may_not(self, env):
        env.seed()

        with pytest.raises(ReviewAccessDenied) as refused:
            env.service.completion_plan(OTHER_REVIEWER, KEY, CYCLE)

        assert refused.value.code == "NOT_LEASE_HOLDER"
        assert env.loads == []

    def test_a_reviewer_may_not_on_an_item_nobody_holds(self, env):
        env.seed(state="OPEN")

        with pytest.raises(ReviewConflict) as refused:
            env.service.completion_plan(REVIEWER, KEY, CYCLE)

        assert refused.value.code == "INVALID_STATE"

    @pytest.mark.parametrize("state", ["HOLDING", "FINALIZING", "COMPLETE"])
    @pytest.mark.parametrize("caller", [ADMIN, REVIEWER], ids=["admin", "reviewer"])
    def test_a_review_that_is_not_open_or_claimed_is_refused(self, env, state, caller):
        env.seed(state=state)

        with pytest.raises(ReviewConflict) as refused:
            env.service.completion_plan(caller, KEY, CYCLE)

        assert refused.value.code == "INVALID_STATE"

    def test_an_item_outside_the_callers_scope_is_refused(self, env):
        env.seed(owner=SCOPED_REVIEWER)

        with pytest.raises(ReviewAccessDenied) as refused:
            env.service.completion_plan(SCOPED_REVIEWER, KEY, CYCLE)

        assert refused.value.code == "OUT_OF_SCOPE"

    def test_an_older_cycle_is_refused(self, env):
        env.seed()

        with pytest.raises(ReviewConflict) as refused:
            env.service.completion_plan(REVIEWER, KEY, "1" * 21)

        assert refused.value.code == "STALE_CYCLE"

    def test_a_document_with_no_item_is_not_found(self, env):
        with pytest.raises(ReviewItemNotFound) as missing:
            env.service.completion_plan(ADMIN, KEY, CYCLE)

        assert missing.value.code == "ITEM_NOT_FOUND"

    def test_a_document_that_no_longer_exists_is_not_found(self, env):
        env.seed()
        env.document = None

        with pytest.raises(ReviewItemNotFound) as missing:
            env.service.completion_plan(REVIEWER, KEY, CYCLE)

        assert missing.value.code == "DOCUMENT_NOT_FOUND"
        assert env.item().state == "WITHDRAWN"
