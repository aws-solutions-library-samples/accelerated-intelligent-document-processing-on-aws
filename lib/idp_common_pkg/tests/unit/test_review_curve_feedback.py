# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the self-correcting loop: HITL review → confidence curve.

The review-effort estimator's numbers improve as a team reviews, implemented by
``complete_section_review`` recording, for each reviewed field, whether the model's
prediction survived the human's edit. These tests cover that hand-off, including
the ordering constraint that makes it possible: the previous label must be read
*before* the corrected one overwrites it, or the evidence of what the model
predicted is gone.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

pytestmark = pytest.mark.unit


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "src" / "lambda" / "complete_section_review").is_dir():
            return parent
    raise RuntimeError("Could not locate src/lambda/complete_section_review")


def _load_review_module():
    path = _repo_root() / "src" / "lambda" / "complete_section_review" / "index.py"
    spec = importlib.util.spec_from_file_location("complete_section_review", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["complete_section_review"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def review_env():
    """The review Lambda wired to a real (moto) table and bucket."""
    env = {
        "TRACKING_TABLE_NAME": "tracking",
        # build_document_response goes through idp_common's DynamoDBClient, which
        # reads TRACKING_TABLE rather than the Lambda's TRACKING_TABLE_NAME.
        "TRACKING_TABLE": "tracking",
        "TEST_SET_BUCKET": "test-set-bucket",
        "OUTPUT_BUCKET": "output-bucket",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
    }
    with mock_aws(), patch.dict(os.environ, env):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
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
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-set-bucket")

        module = _load_review_module()
        # The module binds its clients at import time from the ambient session,
        # which under moto is the mocked one — but rebind explicitly so the test
        # doesn't depend on import ordering.
        module.dynamodb = ddb
        module.s3_client = s3
        module.TRACKING_TABLE_NAME = "tracking"
        module.TEST_SET_BUCKET = "test-set-bucket"
        yield module, table, s3


DRAFTED_LABEL = {
    "inference_result": {"vendor": "Acme", "total": "100"},
    "explainability_info": [
        {
            "vendor": {"confidence": 0.97},
            "total": {"confidence": 0.35},
        }
    ],
    "labelSource": "draft-machine",
    "metadata": {"config_version": "v2"},
}


def _seed_review_doc(table, s3, object_key="run1/a.pdf", test_set_id="ts1"):
    table.put_item(
        Item={
            "PK": f"doc#{object_key}",
            "SK": "none",
            "TestSetId": test_set_id,
        }
    )
    s3.put_object(
        Bucket="test-set-bucket",
        Key=f"{test_set_id}/baseline/a.pdf/sections/1/result.json",
        Body=json.dumps(DRAFTED_LABEL).encode(),
    )


class TestCurveFeedback:
    def test_correcting_a_field_teaches_the_curve_it_was_wrong(self, review_env):
        """The core of the self-correcting loop.

        The reviewer changes the 0.35-confidence field and leaves the
        0.97-confidence one alone, so the curve should learn that the low band is
        unreliable and the high band is fine.
        """
        module, table, s3 = review_env
        _seed_review_doc(table, s3)

        corrected = {
            "inference_result": {"vendor": "Acme", "total": "142.50"},
        }
        module.write_correction_to_test_set_baseline(
            "run1/a.pdf", "1", json.dumps(corrected)
        )

        from idp_common.evaluation.curve_store import CurveStore

        curve = CurveStore(table).get_curve("ts1", "v2")
        assert curve.review_observations == 2
        # Low-confidence band: observed wrong.
        assert curve.accuracy_at(0.35) < 0.5
        # High-confidence band: observed right.
        assert curve.accuracy_at(0.97) > 0.5

    def test_the_previous_label_is_read_before_it_is_overwritten(self, review_env):
        """Ordering constraint: read the draft first or the evidence is lost.

        If the corrected label were written before the old one was read, every
        field would compare equal to itself and the curve would learn that the
        model is always right — the exact opposite of the truth.
        """
        module, table, s3 = review_env
        _seed_review_doc(table, s3)

        corrected = {"inference_result": {"vendor": "Acme", "total": "142.50"}}
        module.write_correction_to_test_set_baseline(
            "run1/a.pdf", "1", json.dumps(corrected)
        )

        from idp_common.evaluation.curve_store import CurveStore

        curve = CurveStore(table).get_curve("ts1", "v2")
        # One correct (vendor) and one incorrect (total) — not two correct.
        total = curve.total_observations
        correct = sum(curve.correct)
        assert total == 2
        assert correct == 1, f"expected 1 correct of 2, got {correct}"

    def test_review_marks_the_label_human_owned(self, review_env):
        """Otherwise a later draft-labeling run would overwrite the correction."""
        module, table, s3 = review_env
        _seed_review_doc(table, s3)

        module.write_correction_to_test_set_baseline(
            "run1/a.pdf", "1", json.dumps({"inference_result": {"vendor": "Fixed"}})
        )

        stored = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/a.pdf/sections/1/result.json",
            )["Body"].read()
        )
        assert stored["labelSource"] == "reviewed-human"

    def test_curve_is_keyed_by_the_config_that_produced_the_labels(self, review_env):
        """Confidence semantics differ across configs; don't pollute another curve."""
        module, table, s3 = review_env
        _seed_review_doc(table, s3)

        module.write_correction_to_test_set_baseline(
            "run1/a.pdf",
            "1",
            json.dumps({"inference_result": {"vendor": "Acme", "total": "1"}}),
        )

        from idp_common.evaluation.curve_store import CurveStore

        store = CurveStore(table)
        assert store.get_curve("ts1", "v2").review_observations == 2
        # A different config version has no observations of its own, so it falls
        # back to the set aggregate rather than claiming v2's data as its own.
        assert store.get_curve("ts1", "v9").config_version == "v9"

    def test_a_document_outside_a_test_set_records_nothing(self, review_env):
        """Ordinary HITL review has no test set to attribute a curve to."""
        module, table, s3 = review_env
        table.put_item(Item={"PK": "doc#plain/a.pdf", "SK": "none"})

        module.write_correction_to_test_set_baseline(
            "plain/a.pdf", "1", json.dumps({"inference_result": {"x": 1}})
        )
        # No curve item was created for any set.
        # filtered-scan-ok: a test fixture holding a handful of items, asserting
        # emptiness — there is no second page to miss.
        scanned = table.scan(
            FilterExpression="begins_with(SK, :sk)",
            ExpressionAttributeValues={":sk": "curve#"},
        )
        assert scanned.get("Items") == []

    def test_curve_failure_never_breaks_the_review(self, review_env):
        """The curve is an optimization; a reviewer's save must still succeed."""
        module, table, s3 = review_env
        _seed_review_doc(table, s3)

        with patch.object(
            module, "record_curve_observations", side_effect=RuntimeError("boom")
        ):
            # The exception is caught by write_correction's own handler, so the
            # call returns rather than propagating to the reviewer.
            module.write_correction_to_test_set_baseline(
                "run1/a.pdf", "1", json.dumps({"inference_result": {"vendor": "x"}})
            )

    def test_no_previous_label_records_nothing(self, review_env):
        """A first-time label has no prediction to judge."""
        module, table, s3 = review_env
        table.put_item(Item={"PK": "doc#run1/a.pdf", "SK": "none", "TestSetId": "ts1"})
        # No existing baseline object.
        module.write_correction_to_test_set_baseline(
            "run1/a.pdf", "1", json.dumps({"inference_result": {"vendor": "x"}})
        )

        from idp_common.evaluation.curve_store import CurveStore

        assert CurveStore(table).get_curve("ts1").total_observations == 0


class TestClaimExclusivity:
    """The collaborative queue depends on a claim being exclusive, not advisory."""

    def _seed(self, table, s3):
        _seed_review_doc(table, s3)

    def test_second_claimant_loses(self, review_env):
        """Regression: claim_review read the owner then wrote unconditionally, so
        two annotators clicking Claim concurrently both succeeded and both edited
        the same document. The write is now conditional."""
        module, table, s3 = review_env
        self._seed(table, s3)

        module.claim_review("run1/a.pdf", "alice", "alice@example.com")

        # Bob's read-then-write would previously have overwritten Alice's claim.
        with pytest.raises(ValueError, match="already claimed"):
            module.claim_review("run1/a.pdf", "bob", "bob@example.com")

        item = table.get_item(Key={"PK": "doc#run1/a.pdf", "SK": "none"})["Item"]
        assert item["HITLReviewOwner"] == "alice"

    def test_message_names_the_winner_so_the_ui_can_skip_on(self, review_env):
        """AnnotationWorkspace matches on 'already claimed' to advance to the next
        document rather than dead-ending, so the phrasing is load-bearing."""
        module, table, s3 = review_env
        self._seed(table, s3)
        module.claim_review("run1/a.pdf", "alice", "alice@example.com")

        with pytest.raises(ValueError) as exc:
            module.claim_review("run1/a.pdf", "bob", "bob@example.com")
        assert "already claimed" in str(exc.value)
        assert "alice" in str(exc.value)

    def test_reclaiming_your_own_document_still_works(self, review_env):
        """Re-opening a document you already hold must not be treated as a race."""
        module, table, s3 = review_env
        self._seed(table, s3)
        module.claim_review("run1/a.pdf", "alice", "alice@example.com")
        module.claim_review("run1/a.pdf", "alice", "alice@example.com")

        item = table.get_item(Key={"PK": "doc#run1/a.pdf", "SK": "none"})["Item"]
        assert item["HITLReviewOwner"] == "alice"

    def test_a_released_document_can_be_claimed_again(self, review_env):
        """Release blanks the owner; the condition must accept an empty string as
        unclaimed, not just a missing attribute."""
        module, table, s3 = review_env
        self._seed(table, s3)
        module.claim_review("run1/a.pdf", "alice", "alice@example.com")
        module.release_review("run1/a.pdf", "alice", "alice@example.com")
        module.claim_review("run1/a.pdf", "bob", "bob@example.com")

        item = table.get_item(Key={"PK": "doc#run1/a.pdf", "SK": "none"})["Item"]
        assert item["HITLReviewOwner"] == "bob"


class TestConfirmWithoutEdits:
    """Confirming labels unchanged is a review verdict, not the absence of one."""

    def test_confirm_marks_the_baseline_reviewed(self, review_env):
        """Regression: the test set showed "Awaiting review" after every document
        had been confirmed.

        write_correction_to_test_set_baseline only ran when editedData was
        supplied, so "labels are correct — mark reviewed" updated the pipeline
        document but left the baseline tagged draft-machine — which is what the
        test set's Review state column reads.
        """
        module, table, s3 = review_env
        _seed_review_doc(table, s3)

        module.confirm_test_set_baseline_reviewed(
            "run1/a.pdf", "1", "confirmer", "confirmer@example.com"
        )

        stored = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/a.pdf/sections/1/result.json",
            )["Body"].read()
        )
        assert stored["labelSource"] == "reviewed-human"
        # Values are untouched — confirming asserts they were already right.
        assert stored["inference_result"] == DRAFTED_LABEL["inference_result"]
        assert stored["_editHistory"][-1]["editedBy"] == "confirmer"

    def test_confirm_teaches_the_curve_the_model_was_right(self, review_env):
        """The high-confidence evidence that editing never produces.

        A reviewer who changes nothing has confirmed every field, including the
        low-confidence ones — exactly the observations the calibration curve needs
        to learn that a low score was nonetheless correct.
        """
        module, table, s3 = review_env
        _seed_review_doc(table, s3)

        module.confirm_test_set_baseline_reviewed("run1/a.pdf", "1", "confirmer")

        from idp_common.evaluation.curve_store import CurveStore

        curve = CurveStore(table).get_curve("ts1", "v2")
        assert curve.review_observations == 2
        assert sum(curve.correct) == 2, "confirming means every field was correct"

    def test_confirming_twice_records_once(self, review_env):
        """Re-confirming must not inflate the curve with duplicate observations."""
        module, table, s3 = review_env
        _seed_review_doc(table, s3)

        module.confirm_test_set_baseline_reviewed("run1/a.pdf", "1", "first")
        module.confirm_test_set_baseline_reviewed("run1/a.pdf", "1", "second")

        from idp_common.evaluation.curve_store import CurveStore

        curve = CurveStore(table).get_curve("ts1", "v2")
        assert curve.review_observations == 2, "second confirm must be a no-op"
        stored = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/a.pdf/sections/1/result.json",
            )["Body"].read()
        )
        assert len(stored["_editHistory"]) == 1

    def test_confirm_outside_a_test_set_is_a_no_op(self, review_env):
        """Ordinary HITL review has no baseline to confirm."""
        module, table, s3 = review_env
        table.put_item(Item={"PK": "doc#plain/a.pdf", "SK": "none"})
        module.confirm_test_set_baseline_reviewed("plain/a.pdf", "1", "someone")
        # filtered-scan-ok: as above — a two-item fixture, asserting emptiness.
        scanned = table.scan(
            FilterExpression="begins_with(SK, :sk)",
            ExpressionAttributeValues={":sk": "curve#"},
        )
        assert scanned.get("Items") == []


class TestHandlerConfirmPath:
    """The end-to-end path the "labels are correct" button actually takes."""

    def test_complete_section_review_without_edited_data_tags_the_baseline(
        self, review_env
    ):
        """The UI sends completeSectionReview with NO editedData when confirming.

        Pins the wiring, not just the helper: the else-branch must be reached from
        complete_section_review itself, or the button silently leaves the baseline
        a draft again.
        """
        module, table, s3 = review_env
        _seed_review_doc(table, s3)

        with patch.object(module, "create_document_service") as mock_service:
            doc = MagicMock()
            doc.sections = [MagicMock(section_id="1", extraction_result_uri=None)]
            doc.hitl_sections_pending = ["1"]
            doc.hitl_sections_completed = []
            mock_service.return_value.get_document.return_value = doc

            module.complete_section_review(
                "run1/a.pdf", "1", None, "confirmer", "confirmer@example.com"
            )

        stored = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/a.pdf/sections/1/result.json",
            )["Body"].read()
        )
        assert stored["labelSource"] == "reviewed-human"


class TestRevisionHistory:
    """The audit trail has to land where the viewer can read it.

    The ground-truth viewer renders provenance from ``_editHistory`` inside the label
    JSON, so the DynamoDB HITLReviewHistory record alone leaves no visible trail.
    """

    def test_review_records_who_changed_what_in_the_label(self, review_env):
        module, table, s3 = review_env
        _seed_review_doc(table, s3)

        module.write_correction_to_test_set_baseline(
            "run1/a.pdf",
            "1",
            json.dumps({"inference_result": {"vendor": "Acme", "total": "142.50"}}),
            "annotator1",
            "annotator1@example.com",
        )

        stored = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/a.pdf/sections/1/result.json",
            )["Body"].read()
        )
        history = stored["_editHistory"]
        assert len(history) == 1
        entry = history[0]
        assert entry["editedBy"] == "annotator1"
        assert entry["editedByEmail"] == "annotator1@example.com"
        assert entry["source"] == "annotation-review"
        # The diff is what makes the trail useful: a field that keeps being
        # corrected points at a config gap.
        diffs = entry["baselineEdits"]["diffs"]
        assert diffs["total"] == {"originalValue": "100", "newValue": "142.50"}
        assert "vendor" not in diffs, "an untouched field is not a change"

    def test_history_accumulates_across_reviews(self, review_env):
        """A second reviewer's edit must not erase the first one's."""
        module, table, s3 = review_env
        _seed_review_doc(table, s3)

        module.write_correction_to_test_set_baseline(
            "run1/a.pdf",
            "1",
            json.dumps({"inference_result": {"vendor": "Acme", "total": "142.50"}}),
            "first",
        )
        module.write_correction_to_test_set_baseline(
            "run1/a.pdf",
            "1",
            json.dumps({"inference_result": {"vendor": "Acme Inc", "total": "142.50"}}),
            "second",
        )

        stored = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/a.pdf/sections/1/result.json",
            )["Body"].read()
        )
        assert [e["editedBy"] for e in stored["_editHistory"]] == ["first", "second"]

    def test_a_client_that_drops_history_cannot_erase_it(self, review_env):
        """The trail belongs to the server, not to whatever the client posts.

        Regression: appending the entry to the *incoming* body lets any client that
        does not round-trip _editHistory wipe every prior reviewer's record.
        """
        module, table, s3 = review_env
        _seed_review_doc(table, s3)
        # A label with existing history, as it sits in S3.
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=json.dumps(
                {**DRAFTED_LABEL, "_editHistory": [{"editedBy": "earlier-reviewer"}]}
            ).encode(),
        )

        # The client posts only the fields, with no history at all.
        module.write_correction_to_test_set_baseline(
            "run1/a.pdf",
            "1",
            json.dumps({"inference_result": {"vendor": "Acme", "total": "9"}}),
            "later-reviewer",
        )

        stored = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/a.pdf/sections/1/result.json",
            )["Body"].read()
        )
        assert [e["editedBy"] for e in stored["_editHistory"]] == [
            "earlier-reviewer",
            "later-reviewer",
        ]

    def test_history_is_capped(self, review_env):
        """A label reviewed many times must not grow without bound."""
        module, table, s3 = review_env
        _seed_review_doc(table, s3)

        saved = {"inference_result": {"vendor": "Acme"}}
        saved["_editHistory"] = [
            {"editedBy": f"old{i}"} for i in range(module.MAX_EDIT_HISTORY_ENTRIES + 10)
        ]
        module.append_edit_history(DRAFTED_LABEL, saved, "newest", "")

        assert len(saved["_editHistory"]) == module.MAX_EDIT_HISTORY_ENTRIES
        # The most recent entries are the ones anyone reads, so the cap drops the
        # oldest rather than refusing the newest.
        assert saved["_editHistory"][-1]["editedBy"] == "newest"

    def test_a_review_that_changed_nothing_still_records_the_signoff(self, review_env):
        """ "Labels are correct — mark reviewed" is itself the auditable event."""
        module, table, s3 = review_env
        _seed_review_doc(table, s3)

        module.write_correction_to_test_set_baseline(
            "run1/a.pdf",
            "1",
            json.dumps({"inference_result": {"vendor": "Acme", "total": "100"}}),
            "confirmer",
        )

        stored = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/a.pdf/sections/1/result.json",
            )["Body"].read()
        )
        entry = stored["_editHistory"][0]
        assert entry["editedBy"] == "confirmer"
        assert "baselineEdits" not in entry, "no diff, because nothing changed"


class TestRecordCurveObservations:
    def test_missing_previous_is_a_no_op(self, review_env):
        module, table, _ = review_env
        module.record_curve_observations("ts1", None, {"inference_result": {}})
        from idp_common.evaluation.curve_store import CurveStore

        assert CurveStore(table).get_curve("ts1").total_observations == 0

    def test_swallows_store_errors(self, review_env):
        """A DynamoDB hiccup on the curve must not surface to the reviewer."""
        module, _, _ = review_env
        with patch(
            "idp_common.evaluation.curve_store.CurveStore.add_observations",
            side_effect=RuntimeError("throttled"),
        ):
            module.record_curve_observations(
                "ts1", DRAFTED_LABEL, {"inference_result": {"vendor": "Acme"}}
            )


class TestAnnotatorReviewScope:
    """An Annotator may only review documents in their assigned test set.

    Group membership gets them to the operation; these tests cover the second gate,
    which stops an annotator onboarded for one labeling effort from reviewing
    production documents or another test set.
    """

    @pytest.fixture
    def scoped_env(self, review_env):
        module, table, s3 = review_env
        # A users table so scope can be resolved.
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        users = ddb.create_table(
            TableName="users",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "email", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "EmailIndex",
                    "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        users.put_item(
            Item={
                "PK": "USER#ann",
                "SK": "USER#ann",
                "userId": "ann",
                "email": "ann@example.com",
                "persona": "Annotator",
                "allowedTestSets": ["ts1"],
            }
        )
        from idp_common import testset_scope

        testset_scope.clear_scope_cache()
        with patch.dict(os.environ, {"USERS_TABLE_NAME": "users"}):
            yield module, table, s3
        testset_scope.clear_scope_cache()

    def _annotator_event(self, field, object_key):
        return {
            "info": {"fieldName": field},
            "arguments": {"objectKey": object_key},
            "identity": {
                "claims": {
                    "cognito:groups": ["Annotator"],
                    "email": "ann@example.com",
                },
                "username": "ann@example.com",
            },
        }

    def test_annotator_can_claim_a_document_in_their_set(self, scoped_env):
        module, table, s3 = scoped_env
        _seed_review_doc(table, s3, object_key="run1/a.pdf", test_set_id="ts1")

        # Reaches claim_review (which then fails on the document lookup, proving
        # authorization passed rather than the operation being refused).
        try:
            module.handler(self._annotator_event("claimReview", "run1/a.pdf"), None)
        except Exception as e:
            assert "Unauthorized" not in str(e), e

    def test_annotator_cannot_claim_a_document_in_another_set(self, scoped_env):
        """The escalation attempt that matters."""
        module, table, s3 = scoped_env
        _seed_review_doc(table, s3, object_key="run2/b.pdf", test_set_id="ts-other")

        with pytest.raises(ValueError, match="Unauthorized"):
            module.handler(self._annotator_event("claimReview", "run2/b.pdf"), None)

    def test_annotator_cannot_review_a_production_document(self, scoped_env):
        """A document with no TestSetId is production HITL work, not annotation."""
        module, table, s3 = scoped_env
        table.put_item(Item={"PK": "doc#prod/x.pdf", "SK": "none"})

        with pytest.raises(ValueError, match="only review test-set documents"):
            module.handler(self._annotator_event("claimReview", "prod/x.pdf"), None)

    def test_annotator_cannot_skip_all_sections(self, scoped_env):
        """Skipping accepts labels unseen — a set-owner call, not an annotator's."""
        module, table, s3 = scoped_env
        _seed_review_doc(table, s3, object_key="run1/a.pdf", test_set_id="ts1")

        with pytest.raises(ValueError, match="administrators and reviewers"):
            module.handler(
                self._annotator_event("skipAllSectionsReview", "run1/a.pdf"), None
            )

    def test_viewer_still_cannot_reach_review_operations(self, scoped_env):
        module, table, s3 = scoped_env
        event = self._annotator_event("claimReview", "run1/a.pdf")
        event["identity"]["claims"]["cognito:groups"] = ["Viewer"]

        with pytest.raises(ValueError, match="Unauthorized"):
            module.handler(event, None)

    def test_reviewer_is_unaffected_by_test_set_scope(self, scoped_env):
        """Production reviewers keep working exactly as before."""
        module, table, s3 = scoped_env
        table.put_item(Item={"PK": "doc#prod/x.pdf", "SK": "none"})
        event = self._annotator_event("claimReview", "prod/x.pdf")
        event["identity"]["claims"]["cognito:groups"] = ["Reviewer"]

        try:
            module.handler(event, None)
        except Exception as e:
            assert "Unauthorized" not in str(e), e

    def test_annotator_who_is_also_a_reviewer_stays_scoped(self, scoped_env):
        """Holding both groups must intersect the two gates, not disable one.

        Exempting Reviewer turned object scope off entirely for a double-assigned
        user, and disagreed with testset_scope, which refuses that caller. Annotators
        are assigned by hand in Cognito (no external-IdP mapping for the role), so a
        double assignment is an easy mistake.
        """
        module, table, s3 = scoped_env
        _seed_review_doc(table, s3, object_key="run2/b.pdf", test_set_id="ts-other")
        event = self._annotator_event("claimReview", "run2/b.pdf")
        event["identity"]["claims"]["cognito:groups"] = ["Annotator", "Reviewer"]

        with pytest.raises(ValueError, match="Unauthorized"):
            module.handler(event, None)

    def test_annotator_who_is_also_an_author_is_unscoped(self, scoped_env):
        """Author owns test sets, so it is exempt in both layers."""
        module, table, s3 = scoped_env
        _seed_review_doc(table, s3, object_key="run2/b.pdf", test_set_id="ts-other")
        event = self._annotator_event("claimReview", "run2/b.pdf")
        event["identity"]["claims"]["cognito:groups"] = ["Annotator", "Author"]

        try:
            module.handler(event, None)
        except Exception as e:
            assert "Unauthorized" not in str(e), e
