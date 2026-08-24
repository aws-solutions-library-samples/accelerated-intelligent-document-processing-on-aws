# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for confidence-curve persistence.

Run against a real (moto) DynamoDB rather than a mock table: the store's central
claim is that concurrent updates from several reviewers all survive, which is a
property of DynamoDB's atomic ADD and cannot be demonstrated against a mock that
replays whatever the test told it.
"""

import boto3
import pytest
from moto import mock_aws

from idp_common.evaluation.confidence_curve import ConfidenceCurve
from idp_common.evaluation.curve_store import (
    CurveStore,
    curve_sk,
    observations_from_baseline_review,
    observations_from_comparison_results,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        yield ddb.create_table(
            TableName="tracking",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )


class TestReadWrite:
    def test_missing_curve_reads_as_empty_not_an_error(self, table):
        """A set that was never reviewed legitimately has no curve."""
        curve = CurveStore(table).get_curve("never-seen")
        assert curve.total_observations == 0

    def test_observations_round_trip(self, table):
        store = CurveStore(table)
        accepted = store.add_observations(
            "ts1", [(0.2, False), (0.25, False), (0.95, True)], config_version="v1"
        )
        assert accepted == 3

        curve = store.get_curve("ts1", "v1")
        assert curve.total_observations == 3
        # Low-confidence bin saw two wrong fields.
        assert curve.accuracy_at(0.22) < 0.5

    def test_writes_both_the_config_curve_and_the_set_aggregate(self, table):
        """An estimate must be servable whether or not the config is known."""
        store = CurveStore(table)
        store.add_observations("ts1", [(0.3, False)] * 5, config_version="v1")

        assert store.get_curve("ts1", "v1").total_observations == 5
        assert store.get_curve("ts1").total_observations == 5

    def test_unknown_config_falls_back_to_the_set_aggregate(self, table):
        """Better to use this set's own data than a cross-set prior."""
        store = CurveStore(table)
        store.add_observations("ts1", [(0.3, False)] * 8, config_version="v1")

        fallback = store.get_curve("ts1", "v-never-used")
        assert fallback.total_observations == 8

    def test_feeds_the_global_prior(self, table):
        """A brand-new set should inherit what past sets measured."""
        store = CurveStore(table)
        store.add_observations("ts1", [(0.4, True)] * 6, config_version="v1")
        assert store.get_global_prior().total_observations == 6

    def test_reset_discards_a_curve(self, table):
        store = CurveStore(table)
        store.add_observations("ts1", [(0.4, True)] * 6, config_version="v1")
        store.reset("ts1", "v1")
        # The config-specific curve is gone; the aggregate is untouched, so a
        # read falls back to it rather than reporting nothing.
        item = table.get_item(Key={"PK": "testset#ts1", "SK": curve_sk("v1")}).get(
            "Item"
        )
        assert item is None

    def test_list_curves_reports_one_entry_per_config(self, table):
        store = CurveStore(table)
        store.add_observations("ts1", [(0.4, True)], config_version="v1")
        store.add_observations("ts1", [(0.6, True)], config_version="v2")

        versions = {entry["configVersion"] for entry in store.list_curves("ts1")}
        # v1, v2, plus the aggregate (None).
        assert versions == {"v1", "v2", None}


class TestConcurrentMerging:
    def test_interleaved_writers_all_contribute(self, table):
        """Two reviewers finishing at once must not overwrite each other.

        This is why the store uses per-bin ADD instead of read-modify-write.
        """
        store_a = CurveStore(table)
        store_b = CurveStore(table)

        # Both read the (empty) curve before either writes.
        assert store_a.get_curve("ts1", "v1").total_observations == 0
        assert store_b.get_curve("ts1", "v1").total_observations == 0

        store_a.add_observations("ts1", [(0.2, False)] * 4, config_version="v1")
        store_b.add_observations("ts1", [(0.3, True)] * 6, config_version="v1")

        # 10, not 6 — neither writer's work was lost.
        assert store_a.get_curve("ts1", "v1").total_observations == 10

    def test_repeated_folds_accumulate(self, table):
        store = CurveStore(table)
        for _ in range(5):
            store.add_observations("ts1", [(0.5, True)] * 2, config_version="v1")
        assert store.get_curve("ts1", "v1").total_observations == 10


class TestSticklerBins:
    def test_scoring_bins_fold_in_and_count_as_scoring(self, table):
        """Only a scoring run measures the high-confidence zone."""
        store = CurveStore(table)
        bins = [
            {"range": [0.0, 0.1], "count": 10, "accuracy": 0.1},
            {"range": [0.9, 1.0], "count": 40, "accuracy": 0.95},
        ]
        accepted = store.add_ece_bins("ts1", bins, config_version="v1")
        assert accepted == 50

        curve = store.get_curve("ts1", "v1")
        assert curve.scoring_observations == 50
        assert curve.review_observations == 0
        assert curve.accuracy_at(0.05) < 0.3
        assert curve.accuracy_at(0.95) > 0.8

    def test_empty_bins_write_nothing(self, table):
        store = CurveStore(table)
        assert store.add_ece_bins("ts1", []) == 0
        assert store.get_curve("ts1").total_observations == 0


class TestObservationsFromComparisonResults:
    def test_extracts_confidence_and_match_pairs(self):
        results = [
            {
                "fields": {
                    "vendor": {"confidence": 0.9, "is_match": True},
                    "total": {"confidence": 0.4, "is_match": False},
                    # No confidence — carries no calibration information.
                    "notes": {"is_match": True},
                    # No verdict — can't say whether the model was right.
                    "other": {"confidence": 0.5},
                }
            }
        ]
        observations = observations_from_comparison_results(results)
        assert sorted(observations) == [(0.4, False), (0.9, True)]

    def test_accepts_a_list_of_fields(self):
        """The blob shape varies with which Stickler flags produced it."""
        results = [{"fields": [{"confidence": 0.7, "matched": True}]}]
        assert observations_from_comparison_results(results) == [(0.7, True)]

    def test_tolerates_empty_and_malformed_input(self):
        assert observations_from_comparison_results([]) == []
        assert observations_from_comparison_results(None) == []
        assert observations_from_comparison_results([{}]) == []
        assert observations_from_comparison_results([{"fields": ["junk"]}]) == []


class TestObservationsFromBaselineReview:
    def test_unchanged_field_is_a_correct_observation(self):
        """A reviewer who left a field alone has confirmed the model was right."""
        before = {
            "inference_result": {"vendor": "Acme", "total": "100"},
            "explainability_info": [
                {
                    "vendor": {"confidence": 0.95},
                    "total": {"confidence": 0.42},
                }
            ],
        }
        after = {"inference_result": {"vendor": "Acme", "total": "100"}}
        observations = dict(observations_from_baseline_review(before, after))
        assert observations[0.95] is True
        assert observations[0.42] is True

    def test_changed_field_is_an_incorrect_observation(self):
        before = {
            "inference_result": {"vendor": "Acme", "total": "100"},
            "explainability_info": [
                {
                    "vendor": {"confidence": 0.95},
                    "total": {"confidence": 0.42},
                }
            ],
        }
        # The reviewer corrected the low-confidence field.
        after = {"inference_result": {"vendor": "Acme", "total": "142.50"}}
        observations = dict(observations_from_baseline_review(before, after))
        assert observations[0.95] is True
        assert observations[0.42] is False

    def test_handles_nested_compound_fields(self):
        """Real payloads nest (PayPeriod.StartDate on a payslip)."""
        before = {
            "inference_result": {"PayPeriod": {"StartDate": "1/1", "EndDate": "1/15"}},
            "explainability_info": [
                {
                    "PayPeriod": {
                        "StartDate": {"confidence": 0.6},
                        "EndDate": {"confidence": 0.99},
                    }
                }
            ],
        }
        after = {
            "inference_result": {"PayPeriod": {"StartDate": "2/1", "EndDate": "1/15"}}
        }
        observations = dict(observations_from_baseline_review(before, after))
        assert observations[0.6] is False
        assert observations[0.99] is True

    def test_field_added_by_the_reviewer_yields_no_observation(self):
        """There was no prediction to be right or wrong about."""
        before = {
            "inference_result": {"vendor": "Acme"},
            "explainability_info": [{"vendor": {"confidence": 0.9}}],
        }
        after = {"inference_result": {"vendor": "Acme", "newField": "added"}}
        assert observations_from_baseline_review(before, after) == [(0.9, True)]

    def test_missing_confidence_yields_nothing(self):
        before = {"inference_result": {"vendor": "Acme"}}
        after = {"inference_result": {"vendor": "Corrected"}}
        assert observations_from_baseline_review(before, after) == []

    def test_tolerates_missing_payloads(self):
        assert observations_from_baseline_review(None, {}) == []
        assert observations_from_baseline_review({}, None) == []


class TestEndToEnd:
    def test_review_observations_move_the_estimate(self, table):
        """The self-correcting property: review changes what the curve says.

        Before review the curve takes confidence at face value. After a
        reviewer finds the low-confidence fields were in fact wrong, the curve
        should report a lower accuracy for that band.
        """
        store = CurveStore(table)
        before = store.get_curve("ts1", "v1")
        naive = before.accuracy_at(0.25)

        # A reviewer corrects 40 low-confidence fields.
        store.add_observations("ts1", [(0.25, False)] * 40, config_version="v1")

        after = store.get_curve("ts1", "v1")
        assert after.accuracy_at(0.25) < naive
        assert after.review_observations == 40

    def test_stored_curve_serializes_back_into_the_estimator(self, table):
        from idp_common.evaluation.confidence_curve import estimate_for_target

        store = CurveStore(table)
        store.add_observations(
            "ts1", [(0.3, False)] * 30 + [(0.9, True)] * 30, config_version="v1"
        )
        curve = store.get_curve("ts1", "v1")
        estimate = estimate_for_target(curve, 99.0, 50, [0.3] * 25 + [0.9] * 25)
        assert isinstance(estimate.docs_to_review, int)
        assert estimate.to_dict()["calibration"]["totalObservations"] == 60


class TestCurveIdentity:
    def test_curve_carries_its_keying(self, table):
        """A curve must know which set and config it describes."""
        store = CurveStore(table)
        store.add_observations("ts1", [(0.5, True)], config_version="v7")
        curve = store.get_curve("ts1", "v7")
        assert curve.test_set_id == "ts1"
        assert curve.config_version == "v7"

    def test_config_versions_are_kept_separate(self, table):
        """Confidence means different things across configs; don't mix them."""
        store = CurveStore(table)
        store.add_observations("ts1", [(0.9, False)] * 20, config_version="bad-config")
        store.add_observations("ts1", [(0.9, True)] * 20, config_version="good-config")

        bad = store.get_curve("ts1", "bad-config")
        good = store.get_curve("ts1", "good-config")
        assert bad.accuracy_at(0.9) < good.accuracy_at(0.9)


def test_curve_dict_round_trips_through_storage_shape(table):
    """Bins are stored flat (correct0..correct9) so each can be an atomic ADD."""
    store = CurveStore(table)
    store.add_observations("ts1", [(0.05, True), (0.95, False)], config_version="v1")
    item = table.get_item(Key={"PK": "testset#ts1", "SK": curve_sk("v1")})["Item"]
    assert "correct0" in item and "total0" in item
    assert "correct9" in item and "total9" in item

    restored = store.get_curve("ts1", "v1")
    assert isinstance(restored, ConfidenceCurve)
    assert restored.total_observations == 2
