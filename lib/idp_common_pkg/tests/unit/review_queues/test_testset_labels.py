# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test Studio labels: the reviewed-label writer and section drafts.

S3, HITLTable and the tracking table are moto-backed, and the confidence curve is
the real ``CurveStore``, so what is asserted is what was stored. The edit-history,
cap and curve cases moved here from ``complete_section_review``'s suite with the
code they test, and keep its assertions.
"""

import json
import logging
from decimal import Decimal
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

import idp_common.evaluation.curve_store as curve_store
from idp_common.review_queues import keys, testset_docs
from idp_common.review_queues.exceptions import (
    ReviewConflict,
    ReviewItemNotFound,
    ReviewValidationError,
)
from idp_common.review_queues.testset_labels import (
    MAX_EDIT_HISTORY_ENTRIES,
    MAX_SECTION_LABEL_BYTES,
    DraftMove,
    LabelEditor,
    TestSetLabels,
    append_edit_history,
    baseline_key,
)

from ._table import REGION, create_review_table

BUCKET = "test-sets"
TRACKING = "tracking"
SET, DOC = "ts-1", "in/a b.pdf"
LOGGER = "idp_common.review_queues.testset_labels"
ALICE = LabelEditor(name="alice", email="alice@example.com")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        ddb = boto3.resource("dynamodb", region_name=REGION)
        doc_table = ddb.Table(create_review_table())
        curve_table = ddb.create_table(
            TableName=TRACKING,
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
        labels = TestSetLabels(s3, BUCKET, doc_table=doc_table, curve_table=curve_table)
        yield labels, s3, doc_table, curve_table


def put_label(s3, label, section_id="1"):
    s3.put_object(
        Bucket=BUCKET,
        Key=baseline_key(SET, DOC, section_id),
        Body=json.dumps(label).encode(),
    )


def get_json(s3, key):
    return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())


def put_row(doc_table, **attributes):
    """The document's TSDOC item, as getAnnotationQueue syncs it."""
    testset_docs.TestSetDocStore(doc_table).upsert_summary(
        SET, {"objectKey": DOC, "labelSource": "draft-machine", **attributes}
    )


def raw_row(doc_table):
    return doc_table.get_item(Key=keys.tsdoc_key(SET, DOC)).get("Item")


def draft_objects(s3):
    listed = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{SET}/drafts/")
    return sorted(o["Key"] for o in listed.get("Contents", []))


# -------------------------------------------------------------- edit history


@pytest.mark.unit
class TestEditHistory:
    def test_a_capped_edit_history_drops_the_oldest_entries_and_keeps_this_review(
        self, env
    ):
        """Keeping the head instead of the tail would silently stop recording reviews."""
        labels, s3, _, _ = env
        stored = [{"editedBy": f"r{i}"} for i in range(MAX_EDIT_HISTORY_ENTRIES)]
        put_label(s3, {"_editHistory": stored})
        labels.write_reviewed(
            SET,
            DOC,
            "1",
            {},
            editor=LabelEditor(name="newest"),
            source="annotation-review",
            require_existing=False,
            skip_if_reviewed=False,
        )
        history = get_json(s3, baseline_key(SET, DOC, "1"))["_editHistory"]
        assert len(history) == MAX_EDIT_HISTORY_ENTRIES
        assert history[-1]["editedBy"] == "newest"
        assert history[0]["editedBy"] == "r1"  # r0 dropped

    def test_the_entry_names_the_changed_field_paths_and_both_values(self):
        """The diff is what a later auditor reads to see whether the model was right.

        Flattened with the same helper the confidence curve uses, so "changed"
        means the same thing to the audit trail and to the calibration signal.
        """
        previous = {
            "inference_result": {
                "total": "100.00",
                "vendor": {"name": "ACME"},
                "lines": [{"amount": "1.00"}, {"amount": "2.00"}],
            }
        }
        saved = {
            "inference_result": {
                "total": "105.00",
                "vendor": {"name": "ACME"},
                "lines": [{"amount": "1.00"}, {"amount": "9.99"}],
            }
        }
        append_edit_history(previous, saved, ALICE, "annotation-review")

        entry = saved["_editHistory"][-1]
        assert entry["editedBy"] == "alice"
        assert entry["editedByEmail"] == "alice@example.com"
        assert entry["source"] == "annotation-review"
        edits = entry["baselineEdits"]
        assert sorted(edits["changedFields"]) == ["lines[1].amount", "total"]
        assert edits["changeCount"] == 2
        assert edits["diffs"]["total"] == {
            "originalValue": "100.00",
            "newValue": "105.00",
        }
        assert edits["diffs"]["lines[1].amount"] == {
            "originalValue": "2.00",
            "newValue": "9.99",
        }

    def test_a_review_that_changed_nothing_records_no_baseline_edits(self):
        """`baselineEdits` absent is how a confirmation is distinguished from a fix."""
        saved = {"inference_result": {"total": "1"}}
        append_edit_history({"inference_result": {"total": "1"}}, saved, ALICE, "x")
        assert "baselineEdits" not in saved["_editHistory"][-1]

    def test_a_diff_that_cannot_be_computed_still_records_who_reviewed(self, caplog):
        """Provenance must not fail the save when the flattener cannot be used."""
        saved = {"inference_result": {"total": "2"}}
        with (
            caplog.at_level(logging.WARNING, logger=LOGGER),
            patch.object(
                curve_store,
                "flatten_values",
                side_effect=TypeError("incompatible layer"),
            ),
        ):
            append_edit_history({"inference_result": {"total": "1"}}, saved, ALICE, "x")
        assert "Could not diff review changes" in caplog.text
        assert "incompatible layer" in caplog.text
        assert "baselineEdits" not in saved["_editHistory"][-1]
        assert saved["_editHistory"][-1]["editedBy"] == "alice"

    def test_a_first_review_with_no_prior_label_records_no_diff(self, caplog):
        """Nothing to diff against, recognised rather than crashed into: the
        warning the diff's own `except` logs is absent."""
        saved = {"inference_result": {"total": "1"}}
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            append_edit_history(None, saved, ALICE, "x")
        assert "Could not diff review changes" not in caplog.text
        assert "baselineEdits" not in saved["_editHistory"][-1]
        assert len(saved["_editHistory"]) == 1

    def test_the_stored_trail_wins_over_a_shorter_one_the_client_sent(self, env):
        labels, s3, _, _ = env
        put_label(s3, {"_editHistory": [{"editedBy": "a"}, {"editedBy": "b"}]})
        labels.write_reviewed(
            SET,
            DOC,
            "1",
            {"_editHistory": []},
            editor=LabelEditor(name="c"),
            source="x",
            require_existing=True,
            skip_if_reviewed=False,
        )
        history = get_json(s3, baseline_key(SET, DOC, "1"))["_editHistory"]
        assert [e["editedBy"] for e in history] == ["a", "b", "c"]


# ---------------------------------------------------------- confidence curve


CURVE_PREVIOUS = {
    "inference_result": {"total": "100.00", "date": "2026-01-01"},
    "explainability_info": {
        "total": {"confidence": 0.95},
        "date": {"confidence": 0.30},
    },
    "metadata": {"config_version": "profileA", "confidence_fingerprint": "fp1"},
}
CURVE_SAVED = {"inference_result": {"total": "100.00", "date": "2026-02-02"}}


def curve_item(table, sk, test_set_id=SET):
    return table.get_item(
        Key={"PK": curve_store.test_set_pk(test_set_id), "SK": sk}
    ).get("Item")


@pytest.mark.unit
class TestCurveObservations:
    def test_a_review_records_which_confidence_was_earned_and_which_was_not(self, env):
        """A field left alone was predicted correctly; a changed one was not."""
        labels, _, _, curve = env
        labels.record_curve_observations(SET, CURVE_PREVIOUS, CURVE_SAVED)
        item = curve_item(curve, "curve#_aggregate")
        assert item is not None
        assert item["total9"] == Decimal("1") and item["correct9"] == Decimal("1")
        assert item["total2"] == Decimal("1") and item["correct2"] == Decimal("0")
        assert item["reviewObservations"] == Decimal("2")
        assert item["ItemType"] == "confidence_curve"

    def test_observations_are_keyed_by_the_replaced_labels_config_and_fingerprint(
        self, env
    ):
        labels, _, _, curve = env
        labels.record_curve_observations(SET, CURVE_PREVIOUS, CURVE_SAVED)
        assert curve_item(curve, "curve#profileA@fp1") is not None
        assert curve_item(curve, "curve#profileA") is not None
        assert curve_item(curve, "curve#_aggregate") is not None
        prior = curve.get_item(
            Key={"PK": curve_store.GLOBAL_PRIOR_PK, "SK": "curve#_aggregate"}
        ).get("Item")
        assert prior is not None, "a new test set should inherit the global prior"

    def test_a_label_without_a_fingerprint_writes_no_revision_scoped_curve(self, env):
        labels, _, _, curve = env
        previous = dict(CURVE_PREVIOUS, metadata={"config_version": "profileA"})
        labels.record_curve_observations(SET, previous, CURVE_SAVED)
        assert curve_item(curve, "curve#profileA") is not None
        assert curve_item(curve, "curve#profileA@fp1") is None

    def test_no_curve_is_recorded_when_there_was_no_prior_prediction(self, env):
        labels, _, _, curve = env
        with patch.object(
            curve_store,
            "observations_from_baseline_review",
            wraps=curve_store.observations_from_baseline_review,
        ) as built:
            assert labels.record_curve_observations(SET, None, CURVE_SAVED) == 0
        built.assert_not_called()
        assert curve_item(curve, "curve#_aggregate") is None

    def test_a_label_with_no_recorded_confidences_records_nothing(self, env):
        labels, _, _, curve = env
        with patch.object(
            curve_store.CurveStore, "add_observations", autospec=True
        ) as added:
            labels.record_curve_observations(
                SET,
                {"inference_result": {"total": "1"}, "metadata": {}},
                {"inference_result": {"total": "2"}},
            )
        added.assert_not_called()
        assert curve_item(curve, "curve#_aggregate") is None

    def test_a_curve_write_failure_does_not_fail_the_write(self, env, caplog):
        labels, s3, _, _ = env
        put_label(s3, CURVE_PREVIOUS)
        with (
            caplog.at_level(logging.WARNING, logger=LOGGER),
            patch.object(
                curve_store.CurveStore,
                "add_observations",
                side_effect=RuntimeError("no table"),
            ),
        ):
            written = labels.write_reviewed(
                SET,
                DOC,
                "1",
                CURVE_SAVED,
                editor=ALICE,
                source="x",
                require_existing=True,
                skip_if_reviewed=False,
            )
        assert written is not None
        assert "Could not record confidence-curve observations" in caplog.text
        assert get_json(s3, written.key)["labelSource"] == "reviewed-human"


# ----------------------------------------------------------- reviewed writes


def write(labels, data, **kw):
    args = dict(
        editor=ALICE,
        source="test-set-annotation",
        require_existing=True,
        skip_if_reviewed=False,
    )
    args.update(kw)
    return labels.write_reviewed(SET, DOC, "1", data, **args)


@pytest.mark.unit
class TestWriteReviewed:
    def test_stamps_the_label_reviewed_with_who_why_and_the_diffs(self, env):
        labels, s3, _, _ = env
        put_label(
            s3,
            {"inference_result": {"total": "1"}, "labelSource": "draft-machine"},
        )
        written = write(labels, {"inference_result": {"total": "2"}})
        stored = get_json(s3, baseline_key(SET, DOC, "1"))
        assert stored["labelSource"] == "reviewed-human"
        entry = stored["_editHistory"][-1]
        assert entry["editedBy"] == "alice"
        assert entry["editedByEmail"] == "alice@example.com"
        assert entry["source"] == "test-set-annotation"
        assert entry["baselineEdits"]["diffs"]["total"] == {
            "originalValue": "1",
            "newValue": "2",
        }
        assert written.was_reviewed is False
        assert written.previous["labelSource"] == "draft-machine"

    def test_confirming_rewrites_the_stored_label_as_reviewed(self, env):
        labels, s3, _, _ = env
        put_label(
            s3,
            {"inference_result": {"total": "1"}, "labelSource": "draft-machine"},
        )
        write(labels, None, source="test-set-confirmation")
        stored = get_json(s3, baseline_key(SET, DOC, "1"))
        assert stored["inference_result"] == {"total": "1"}
        assert stored["labelSource"] == "reviewed-human"
        assert "baselineEdits" not in stored["_editHistory"][-1]

    def test_a_confirmation_of_a_reviewed_label_writes_nothing_when_asked_to_skip(
        self, env
    ):
        labels, s3, _, _ = env
        put_label(s3, {"labelSource": "reviewed-human", "_editHistory": [{"a": 1}]})
        with patch.object(s3, "put_object", wraps=s3.put_object) as puts:
            assert write(labels, None, skip_if_reviewed=True) is None
        puts.assert_not_called()

    def test_a_missing_label_is_not_found_when_one_is_required(self, env):
        labels, _, _, _ = env
        with pytest.raises(ReviewItemNotFound) as excinfo:
            write(labels, {"inference_result": {}})
        assert excinfo.value.code == "NO_LABEL"

    def test_deletes_the_sections_draft(self, env):
        labels, s3, doc_table, _ = env
        put_label(s3, {"inference_result": {"total": "1"}})
        put_row(doc_table)
        labels.save_draft(
            SET, DOC, "1", {"inference_result": {"total": "9"}}, 0, editor=ALICE
        )
        assert draft_objects(s3)
        write(labels, {"inference_result": {"total": "9"}})
        assert keys.tsdoc_draft_attr("1") not in raw_row(doc_table)
        assert draft_objects(s3) == []

    def test_a_draft_delete_failure_is_logged_and_the_write_still_returns(
        self, env, caplog
    ):
        labels, s3, doc_table, _ = env
        put_label(s3, {"inference_result": {"total": "1"}})
        put_row(doc_table)
        with (
            caplog.at_level(logging.WARNING, logger=LOGGER),
            patch.object(labels, "delete_draft", side_effect=RuntimeError("boom")),
        ):
            written = write(labels, {"inference_result": {"total": "2"}})
        assert written is not None
        assert "Could not delete the draft" in caplog.text


# ------------------------------------------------------------------- drafts


def save(labels, data, base=0, section_id="1"):
    return labels.save_draft(SET, DOC, section_id, data, base, editor=ALICE)


@pytest.mark.unit
class TestDrafts:
    def test_a_draft_changes_neither_the_label_nor_the_summary_nor_the_curve(self, env):
        labels, s3, doc_table, curve = env
        label = {"inference_result": {"total": "1"}, "labelSource": "draft-machine"}
        put_label(s3, label)
        put_row(doc_table, reviewedSections=0)
        before = raw_row(doc_table)
        draft = save(
            labels, {"inference_result": {"total": "2"}, "labelSource": "draft-machine"}
        )

        assert get_json(s3, baseline_key(SET, DOC, "1")) == label
        after = raw_row(doc_table)
        for attr in ("Reviewed", "LabelSource", "ReviewedSections", "UpdatedAt"):
            assert after.get(attr) == before.get(attr), attr
        stored = get_json(s3, draft.s3_key)
        assert stored == {
            "inference_result": {"total": "2"},
            "labelSource": "draft-machine",
        }
        assert draft.s3_key.startswith(f"{SET}/drafts/{DOC}/sections/1/")
        assert curve.scan()["Items"] == []

    def test_a_second_save_moves_to_the_next_revision_and_drops_the_first(self, env):
        labels, s3, doc_table, _ = env
        put_label(s3, {"inference_result": {}})
        put_row(doc_table)
        first = save(labels, {"n": 1})
        second = save(labels, {"n": 2}, base=1)
        assert (first.revision, second.revision) == (1, 2)
        assert draft_objects(s3) == [second.s3_key]
        assert [d.revision for d in labels.drafts(SET, DOC)] == [2]

    def test_a_stale_base_fails_and_leaves_no_object_behind(self, env):
        labels, s3, doc_table, _ = env
        put_label(s3, {"inference_result": {}})
        put_row(doc_table)
        kept = save(labels, {"n": 1})
        with pytest.raises(ReviewConflict) as excinfo:
            save(labels, {"n": "late"}, base=0)
        assert excinfo.value.code == "STALE_DRAFT"
        assert draft_objects(s3) == [kept.s3_key]

    def test_a_document_not_in_the_set_is_refused(self, env):
        labels, s3, _, _ = env
        put_label(s3, {"inference_result": {}})
        with pytest.raises(ReviewValidationError) as excinfo:
            save(labels, {"n": 1})
        assert excinfo.value.code == "NOT_IN_SET"

    def test_bad_arguments_are_refused(self, env):
        labels, s3, doc_table, _ = env
        put_label(s3, {"inference_result": {}})
        put_row(doc_table)
        with pytest.raises(ReviewValidationError) as bad_id:
            save(labels, {"n": 1}, section_id="../x")
        assert bad_id.value.code == "BAD_ARGUMENT"
        with pytest.raises(ReviewValidationError) as not_object:
            labels.save_draft(SET, DOC, "1", ["x"], 0, editor=ALICE)  # type: ignore[arg-type]
        assert not_object.value.code == "BAD_ARGUMENT"
        with pytest.raises(ReviewValidationError) as too_large:
            save(labels, {"blob": "x" * MAX_SECTION_LABEL_BYTES})
        assert too_large.value.code == "DRAFT_TOO_LARGE"
        with pytest.raises(ReviewItemNotFound) as no_label:
            save(labels, {"n": 1}, section_id="2")
        assert no_label.value.code == "NO_LABEL"

    def test_the_document_view_lists_its_drafts(self, env):
        labels, s3, doc_table, _ = env
        put_label(s3, {"inference_result": {}})
        put_row(doc_table)
        draft = save(labels, {"n": 1})
        view = testset_docs.TestSetDocStore(doc_table).get(SET, DOC)
        assert view["sectionDrafts"] == [draft.model_dump()]

    def test_moving_drafts_follows_a_swap_and_rewrites_their_split_and_class(self, env):
        labels, s3, doc_table, _ = env
        for sid in ("1", "2", "3"):
            put_label(s3, {"inference_result": {}}, section_id=sid)
        put_row(doc_table)
        save(
            labels,
            {"n": "one", "split_document": {"page_indices": [0]}},
            section_id="1",
        )
        save(labels, {"n": "two", "document_class": {"type": "W2"}}, section_id="2")
        save(labels, {"n": "three"}, section_id="3")

        labels.move_drafts(
            SET,
            DOC,
            [
                DraftMove(
                    from_id="1",
                    to_id="2",
                    page_indices=[3, 4],
                    document_class="Invoice",
                ),
                DraftMove(
                    from_id="2", to_id="1", page_indices=[0], document_class=None
                ),
            ],
            removed=["3"],
        )
        by_id = {d.section_id: d for d in labels.drafts(SET, DOC)}
        assert sorted(by_id) == ["1", "2"]
        moved_one = get_json(s3, by_id["2"].s3_key)
        assert moved_one["n"] == "one"
        assert moved_one["split_document"]["page_indices"] == [3, 4]
        assert moved_one["document_class"]["type"] == "Invoice"
        moved_two = get_json(s3, by_id["1"].s3_key)
        assert moved_two["n"] == "two"
        assert moved_two["split_document"]["page_indices"] == [0]
        assert moved_two["document_class"]["type"] == "W2"
        # The removed section's draft, and the superseded objects, are gone.
        assert draft_objects(s3) == sorted(d.s3_key for d in by_id.values())

    def test_deleting_a_documents_drafts_removes_the_attributes_and_the_objects(
        self, env
    ):
        labels, s3, doc_table, _ = env
        for sid in ("1", "2"):
            put_label(s3, {"inference_result": {}}, section_id=sid)
        put_row(doc_table)
        save(labels, {"n": 1}, section_id="1")
        save(labels, {"n": 2}, section_id="2")
        labels.delete_document_drafts(SET, DOC)
        assert labels.drafts(SET, DOC) == []
        assert draft_objects(s3) == []
