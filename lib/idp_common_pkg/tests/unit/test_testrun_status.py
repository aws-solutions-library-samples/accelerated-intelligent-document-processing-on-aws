# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``idp_common.testrun_status``.

The module exists because this derivation had two copies and a value classified
in neither pinned runs at EVALUATING forever — twice in one release cycle
(#1330). So the tests that matter here are the closure ones: every
``EvaluationStatus`` any writer can set lands in exactly one set, and every
``ObjectStatus`` is accounted for. The per-branch tests are there to pin the
derivation that the resolver used to own, so the refactor in #1620 cannot have
changed a verdict.
"""

import gzip
import json
from decimal import Decimal

import pytest

from idp_common import testrun_status as trs


@pytest.mark.unit
class TestEvaluationStatusClassification:
    """The three sets must partition the universe — no overlaps, no gaps."""

    def test_the_three_sets_are_disjoint(self):
        # An overlap would make the tally depend on branch order rather than on
        # the value, which is the bug class this module exists to close.
        assert not (trs.EVAL_STATUS_SETTLED & trs.EVAL_STATUS_UNSUCCESSFUL)
        assert not (trs.EVAL_STATUS_SETTLED & trs.EVAL_STATUS_IN_FLIGHT)
        assert not (trs.EVAL_STATUS_UNSUCCESSFUL & trs.EVAL_STATUS_IN_FLIGHT)

    def test_every_status_any_writer_can_set_is_classified(self):
        """Closure over the writers known today.

        The universe is listed here rather than derived, because the writers live
        in four trees (the pipeline's evaluation function, the baseline copier,
        the SDK's evaluation processor, and the abort path) and importing them
        all into a library test would couple this suite to three Lambda
        directories. The residual is a *fifth* writer, which this cannot see —
        the sweeper's ``TestRunsUnrecognisedEvalStatus`` metric is what reports
        that in a deployed stack.
        """
        written_by_pipeline = {"RUNNING", "COMPLETED", "FAILED", "NO_BASELINE"}
        written_by_timeout_policy = {"TIMED_OUT"}  # #917
        written_when_evaluation_is_off = {"DISABLED"}  # #1330
        written_by_baseline_promotion = {
            "BASELINE_COPYING",
            "BASELINE_AVAILABLE",
            "BASELINE_ERROR",
        }
        universe = (
            written_by_pipeline
            | written_by_timeout_policy
            | written_when_evaluation_is_off
            | written_by_baseline_promotion
        )
        unclassified = universe - trs.EVAL_STATUS_CLASSIFIED
        assert not unclassified, (
            f"{sorted(unclassified)} is written by a known writer but classified "
            "in none of the three sets, so a run holding one never reaches a "
            "terminal status — this is #1330"
        )

    def test_the_union_constant_matches_the_three_sets(self):
        # EVAL_STATUS_CLASSIFIED is what consumers assert closure against, so it
        # going stale would make the closure test above vacuous.
        assert trs.EVAL_STATUS_CLASSIFIED == (
            trs.EVAL_STATUS_SETTLED
            | trs.EVAL_STATUS_UNSUCCESSFUL
            | trs.EVAL_STATUS_IN_FLIGHT
        )

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("completed", "COMPLETED"),
            ("  Completed  ", "COMPLETED"),
            ("COMPLETED", "COMPLETED"),
            ("", None),
            ("   ", None),
            (None, None),
        ],
    )
    def test_normalization(self, raw, expected):
        assert trs.normalized_eval_status({"EvaluationStatus": raw}) == expected

    def test_a_missing_attribute_is_not_recorded(self):
        assert trs.normalized_eval_status({}) is None


@pytest.mark.unit
class TestEvaluationDisabledReading:
    """Must agree with pydantic's ``EvaluationConfig.enabled: bool``."""

    @pytest.mark.parametrize(
        "value", [False, "false", "FALSE", "no", "n", "off", "f", "0", 0, Decimal("0")]
    )
    def test_values_pydantic_reads_as_false(self, value):
        assert trs.evaluation_disabled_in_config({"evaluation": {"enabled": value}})

    @pytest.mark.parametrize(
        "value", [True, "true", "yes", "on", "1", 1, Decimal("1"), " false", "maybe"]
    )
    def test_everything_else_reads_as_enabled(self, value):
        # ``" false"`` in particular: pydantic REJECTS it, so the configuration
        # never ran. Reading it as "evaluation was disabled" would report a run
        # complete on the strength of a config that failed to load.
        assert not trs.evaluation_disabled_in_config({"evaluation": {"enabled": value}})

    def test_an_absent_section_is_not_disabled(self):
        assert not trs.evaluation_disabled_in_config({})
        assert not trs.evaluation_disabled_in_config({"evaluation": {}})

    def test_a_non_dict_config_is_not_disabled(self):
        assert not trs.evaluation_disabled_in_config(None)
        assert not trs.evaluation_disabled_in_config("nope")


@pytest.mark.unit
class TestCapturedConfig:
    def test_reads_a_compressed_body(self):
        body = {"evaluation": {"enabled": False}}
        item = {
            "_config_storage": "compressed",
            "_compressed_config": gzip.compress(json.dumps(body).encode()),
        }
        assert trs.captured_config_of(item) == body
        assert trs.ran_with_evaluation_disabled(item)

    def test_reads_a_legacy_inline_body(self):
        item = {"Config": {"evaluation": {"enabled": False}}}
        assert trs.ran_with_evaluation_disabled(item)

    def test_an_undecompressable_body_degrades_to_empty(self):
        item = {"_config_storage": "compressed", "_compressed_config": b"not gzip"}
        assert trs.captured_config_of(item) == {}
        # And therefore does NOT claim evaluation was disabled, which would
        # report the run complete while results were still coming.
        assert not trs.ran_with_evaluation_disabled(item)

    def test_a_missing_blob_degrades_to_empty(self):
        assert trs.captured_config_of({"_config_storage": "compressed"}) == {}

    def test_floats_land_as_decimal_on_the_compressed_path(self):
        # Matches the inline path, where DynamoDB's resource client already
        # returns Decimal.
        item = {
            "_config_storage": "compressed",
            "_compressed_config": gzip.compress(json.dumps({"t": 0.5}).encode()),
        }
        assert isinstance(trs.captured_config_of(item)["t"], Decimal)


def _docs(**by_key):
    """A loader over a literal mapping, for ``tally_documents``."""
    return by_key.get


@pytest.mark.unit
class TestTallyDocuments:
    def test_settled_evaluation_counts_as_completed(self):
        for status in sorted(trs.EVAL_STATUS_SETTLED):
            tally = trs.tally_documents(
                ["a"],
                _docs(a={"ObjectStatus": "COMPLETED", "EvaluationStatus": status}),
                {},
            )
            assert tally.completed == 1, status
            assert tally.evaluating == 0, status

    def test_unsuccessful_evaluation_counts_as_failed(self):
        for status in sorted(trs.EVAL_STATUS_UNSUCCESSFUL):
            tally = trs.tally_documents(
                ["a"],
                _docs(a={"ObjectStatus": "COMPLETED", "EvaluationStatus": status}),
                {},
            )
            assert tally.failed == 1, status

    def test_in_flight_evaluation_counts_as_evaluating(self):
        tally = trs.tally_documents(
            ["a"],
            _docs(a={"ObjectStatus": "COMPLETED", "EvaluationStatus": "RUNNING"}),
            {},
        )
        assert tally.evaluating == 1

    def test_an_unrecognised_status_is_counted_and_surfaced(self):
        tally = trs.tally_documents(
            ["a"],
            _docs(a={"ObjectStatus": "COMPLETED", "EvaluationStatus": "WAT"}),
            {},
        )
        # Held short of terminal, AND reported — the second half is what the
        # sweeper's metric reads, and without it a fifth writer is invisible.
        assert tally.evaluating == 1
        assert tally.unrecognised == 1

    def test_no_evaluation_status_with_evaluation_on_is_still_evaluating(self):
        tally = trs.tally_documents(["a"], _docs(a={"ObjectStatus": "COMPLETED"}), {})
        assert tally.evaluating == 1
        assert tally.completed == 0

    def test_no_evaluation_status_with_evaluation_off_is_completed(self):
        run = {"Config": {"evaluation": {"enabled": False}}}
        tally = trs.tally_documents(["a"], _docs(a={"ObjectStatus": "COMPLETED"}), run)
        assert tally.completed == 1
        assert tally.evaluating == 0

    def test_the_captured_config_is_read_at_most_once(self):
        """Laziness is load-bearing: reading it decompresses the config."""
        reads = []

        class Run(dict):
            def get(self, key, default=None):
                if key in ("_config_storage", "Config"):
                    reads.append(key)
                return super().get(key, default)

        run = Run({"Config": {"evaluation": {"enabled": False}}})
        trs.tally_documents(
            ["a", "b", "c"],
            _docs(
                a={"ObjectStatus": "COMPLETED"},
                b={"ObjectStatus": "COMPLETED"},
                c={"ObjectStatus": "COMPLETED"},
            ),
            run,
        )
        # Once for the whole run, not once per document.
        assert reads.count("_config_storage") == 1

    def test_the_captured_config_is_not_read_when_no_document_needs_it(self):
        reads = []

        class Run(dict):
            def get(self, key, default=None):
                if key in ("_config_storage", "Config"):
                    reads.append(key)
                return super().get(key, default)

        trs.tally_documents(
            ["a"],
            _docs(a={"ObjectStatus": "COMPLETED", "EvaluationStatus": "COMPLETED"}),
            Run({"Config": {}}),
        )
        assert reads == []

    @pytest.mark.parametrize("doc_status", ["FAILED", "ABORTED"])
    def test_processing_failures_count_as_failed(self, doc_status):
        tally = trs.tally_documents(["a"], _docs(a={"ObjectStatus": doc_status}), {})
        assert tally.failed == 1

    def test_queued_counts_as_queued(self):
        tally = trs.tally_documents(["a"], _docs(a={"ObjectStatus": "QUEUED"}), {})
        assert tally.queued == 1

    def test_a_missing_document_counts_as_queued(self):
        tally = trs.tally_documents(["a"], _docs(), {})
        assert tally.queued == 1

    def test_a_live_pipeline_stage_is_left_unaccounted(self):
        # Deliberate: being unaccounted is what makes the run read RUNNING.
        tally = trs.tally_documents(["a"], _docs(a={"ObjectStatus": "EXTRACTING"}), {})
        assert tally.accounted == 0

    def test_a_document_with_no_object_status_defaults_to_queued(self):
        tally = trs.tally_documents(["a"], _docs(a={}), {})
        assert tally.queued == 1


def _tally(completed=0, failed=0, evaluating=0, queued=0):
    return trs.DocumentTally(
        completed=completed, failed=failed, evaluating=evaluating, queued=queued
    )


@pytest.mark.unit
class TestDeriveOverallStatus:
    def test_all_completed_is_complete(self):
        assert (
            trs.derive_overall_status(_tally(completed=3), 3, 0, "RUNNING")
            == "COMPLETE"
        )

    def test_a_failure_with_everything_accounted_is_partial_complete(self):
        assert (
            trs.derive_overall_status(_tally(completed=2, failed=1), 3, 0, "RUNNING")
            == "PARTIAL_COMPLETE"
        )

    def test_a_baseline_failure_counts_toward_partial_complete(self):
        # BaselineFailedFiles is set by the copier and never updated, so it has
        # to be added rather than rediscovered from the documents.
        assert (
            trs.derive_overall_status(_tally(completed=2), 3, 1, "RUNNING")
            == "PARTIAL_COMPLETE"
        )

    def test_a_baseline_failure_prevents_complete(self):
        assert (
            trs.derive_overall_status(_tally(completed=3), 3, 1, "RUNNING")
            != "COMPLETE"
        )

    def test_an_evaluating_document_holds_the_run_at_evaluating(self):
        assert (
            trs.derive_overall_status(
                _tally(completed=2, evaluating=1), 3, 0, "RUNNING"
            )
            == "EVALUATING"
        )

    def test_all_queued_is_queued(self):
        assert trs.derive_overall_status(_tally(queued=3), 3, 0, "RUNNING") == "QUEUED"

    def test_partly_accounted_is_running(self):
        assert (
            trs.derive_overall_status(_tally(completed=1), 3, 0, "QUEUED") == "RUNNING"
        )

    def test_a_shape_no_branch_describes_keeps_the_stored_status(self):
        """Never invent a transition: an unreadable shape must not move the run.

        More documents accounted for than the run claims to hold — which happens
        if ``FilesCount`` and ``Files`` disagree — falls through every branch:
        it is not all-complete (the counts differ), has no failures, nothing
        evaluating, is not all-queued, and is not short of its total.
        """
        assert trs.derive_overall_status(_tally(completed=4), 3, 0, "WEIRD") == "WEIRD"

    def test_a_zero_file_run_reports_queued(self):
        """Pins the derivation this replaced, which answers QUEUED here.

        There is no ``files_count > 0`` guard on the QUEUED branch, deliberately
        — adding one would change a degenerate run's status as a side effect of
        the #1620 refactor rather than as a decision.
        """
        assert trs.derive_overall_status(_tally(), 0, 0, "RUNNING") == "QUEUED"

    def test_total_failed_adds_the_copiers_count(self):
        assert trs.total_failed_files(_tally(failed=2), 3) == 5


@pytest.mark.unit
class TestAwaitingMetrics:
    def test_terminal_without_metrics_is_awaiting(self):
        for status in sorted(trs.METRICS_ELIGIBLE_STATUSES):
            assert trs.awaiting_metrics({}, status), status

    def test_terminal_with_metrics_is_not_awaiting(self):
        assert not trs.awaiting_metrics({"testRunResult": {"x": 1}}, "COMPLETE")

    def test_an_aborted_run_is_never_awaiting(self):
        # ABORTED is terminal but not metrics-eligible: it must keep reporting
        # ABORTED rather than being masked as EVALUATING.
        assert not trs.awaiting_metrics({}, "ABORTED")

    def test_a_non_terminal_run_is_not_awaiting(self):
        assert not trs.awaiting_metrics({}, "RUNNING")

    def test_a_draft_labeling_run_is_never_awaiting(self):
        # It produces the baseline rather than being scored against one, so it
        # has no metrics to wait for, ever.
        assert not trs.awaiting_metrics({"Purpose": "draft-labeling"}, "COMPLETE")

    def test_the_legacy_context_fallback_is_exact(self):
        assert trs.is_draft_labeling_run({"Context": "Draft labeling run"})
        assert not trs.is_draft_labeling_run({"Context": "Draft labeling run (v2)"})

    def test_purpose_wins_over_context(self):
        assert not trs.is_draft_labeling_run(
            {"Purpose": "benchmark", "Context": "Draft labeling run"}
        )

    def test_everything_it_reads_is_on_the_run_record(self):
        """The property that keeps the badge from splitting in two.

        Only one caller reads a run's documents. If this answer depended on them,
        two surfaces would disagree about whether an aggregation is outstanding
        and neither would enqueue one, so the split would never resolve.
        """
        run = {"Purpose": "draft-labeling", "testRunResult": None}
        # Answerable with no document loader in sight.
        assert trs.awaiting_metrics(run, "COMPLETE") is False


@pytest.mark.unit
class TestNeedsReconciliation:
    """The sweeper's admission test — it must be answerable from the run record."""

    @pytest.mark.parametrize("status", ["RUNNING", "QUEUED", "EVALUATING"])
    def test_a_non_terminal_run_needs_a_look(self, status):
        assert trs.needs_reconciliation({"Status": status})

    def test_a_terminal_run_without_metrics_needs_a_look(self):
        # This is the permanent-EVALUATING-badge state (#619).
        assert trs.needs_reconciliation({"Status": "COMPLETE"})

    def test_a_terminal_run_with_metrics_is_left_alone(self):
        assert not trs.needs_reconciliation(
            {"Status": "COMPLETE", "testRunResult": {"overallAccuracy": 1}}
        )

    def test_an_aborted_run_is_left_alone(self):
        assert not trs.needs_reconciliation({"Status": "ABORTED"})

    def test_a_draft_labeling_run_is_left_alone_once_terminal(self):
        # Otherwise every sweep would enqueue a full aggregation — re-reading
        # every document's results.json — for metrics that are structurally
        # empty.
        assert not trs.needs_reconciliation(
            {"Status": "COMPLETE", "Purpose": "draft-labeling"}
        )

    def test_a_record_with_no_status_needs_a_look(self):
        # Defaults to RUNNING, which is the safe direction: look at it.
        assert trs.needs_reconciliation({})


@pytest.mark.unit
class TestLatestCompletionTime:
    def test_picks_the_newest(self):
        docs = [
            {"CompletionTime": "2026-10-01T10:00:00Z"},
            {"CompletionTime": "2026-10-01T12:00:00Z"},
            {"CompletionTime": "2026-10-01T11:00:00Z"},
        ]
        assert trs.latest_completion_time(docs) == "2026-10-01T12:00:00Z"

    def test_normalizes_the_offset_spelling_before_comparing(self):
        # The comparison is lexicographic on the string, so two spellings of the
        # same instant would otherwise order by punctuation.
        docs = [
            {"CompletionTime": "2026-10-01T12:00:00+00:00"},
            {"CompletionTime": "2026-10-01T11:00:00Z"},
        ]
        assert trs.latest_completion_time(docs) == "2026-10-01T12:00:00Z"

    def test_skips_documents_with_no_completion_time(self):
        docs = [
            {},
            {"CompletionTime": None},
            {"CompletionTime": "2026-10-01T10:00:00Z"},
        ]
        assert trs.latest_completion_time(docs) == "2026-10-01T10:00:00Z"

    def test_none_when_nothing_has_one(self):
        assert trs.latest_completion_time([{}, {}]) is None
        assert trs.latest_completion_time([]) is None


@pytest.mark.unit
class TestSweepAggregationThrottle:
    """The sweeper's backoff. See the constant's own note for why it exists."""

    def test_the_base_exceeds_the_sweep_interval(self):
        # The whole point: a throttle equal to the sweep interval is cleared by
        # every sweep and so damps nothing.
        assert trs.SWEEP_AGGREGATION_BASE_SECONDS > 300

    def test_it_is_longer_than_the_poll_throttle(self):
        # The browser poll stays responsive; only the background retry backs off.
        assert trs.SWEEP_AGGREGATION_BASE_SECONDS > trs.CACHE_UPDATE_THROTTLE_SECONDS

    def test_it_doubles_per_attempt(self):
        base = trs.SWEEP_AGGREGATION_BASE_SECONDS
        assert trs.sweep_aggregation_throttle(0) == base
        assert trs.sweep_aggregation_throttle(1) == base * 2
        assert trs.sweep_aggregation_throttle(3) == base * 8

    def test_it_is_capped(self):
        assert trs.sweep_aggregation_throttle(999) == trs.SWEEP_AGGREGATION_MAX_SECONDS

    @pytest.mark.parametrize("value", [None, "", "abc", -5, object()])
    def test_an_unreadable_count_reads_as_no_attempts(self, value):
        # Fails toward doing the work: the defect being removed is work that
        # never happens, so a corrupt counter must not be able to stop a retry.
        assert (
            trs.sweep_aggregation_throttle(value) == trs.SWEEP_AGGREGATION_BASE_SECONDS
        )

    def test_a_decimal_count_is_accepted(self):
        # DynamoDB hands numbers back as Decimal.
        assert trs.sweep_aggregation_throttle(Decimal("2")) == (
            trs.SWEEP_AGGREGATION_BASE_SECONDS * 4
        )
