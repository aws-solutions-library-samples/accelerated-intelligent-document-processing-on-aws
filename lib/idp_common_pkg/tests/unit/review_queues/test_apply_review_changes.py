# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Applying a completed review to a document (``apply_review.apply_review_changes``).

Against moto: the review table with the real store, and the output bucket with
the real ``idp_common.s3`` helpers, so what is asserted is the objects a later
step reads.
"""

import json
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError

import idp_common.s3 as s3_module
from idp_common.config.routing_models import RoutingTable
from idp_common.review_queues import keys
from idp_common.review_queues.apply_review import (
    MAX_CHANGED_PATHS,
    ApplyReviewResult,
    apply_review_changes,
    extracted_review_record,
    merge_corrections,
    review_record,
)
from idp_common.review_queues.exceptions import ReviewConflict
from idp_common.review_queues.models import DraftRecord, ReviewItemRecord
from idp_common.review_queues.routing_inputs import build_inputs
from idp_common.review_queues.service import BASE_KEY_FORMAT
from idp_common.review_queues.store import ReviewStore, item_to_dynamo

from ._table import REGION

pytestmark = pytest.mark.unit

BUCKET = "apply-review-output"
KEY = "claims/packet.pdf"
CYCLE = "0123456789abcdef01234"
ARN = "arn:aws:states:us-east-1:111122223333:execution:sm:finalize-run"
DISPOSED_AT = "2026-11-16T09:30:00.000Z"
REVIEWER = "reviewer@example.com"

MACHINE = {
    "inference_result": {"Vendor": "ACME", "Total": 100},
    "explainability_info": [
        {"Total": {"confidence": 0.4, "confidence_threshold": 0.8}}
    ],
    "metadata": {"model": "m"},
}


@pytest.fixture
def env(review_table, monkeypatch):
    """The store, the bucket, and the S3 module pointed at moto."""
    monkeypatch.setattr(s3_module, "_s3_client", None)
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(Bucket=BUCKET)
    store = ReviewStore(review_table, region=REGION)
    return SimpleNamespace(store=store, s3=client)


def _result_key(section_id):
    return f"{KEY}/sections/{section_id}/result.json"


def _draft_key(section_id):
    return f"{KEY}/review/drafts/{CYCLE}/{section_id}.json"


def _base_key(section_id):
    return BASE_KEY_FORMAT.format(doc=KEY, cycle=CYCLE, section=section_id)


def _put(env, key, body):
    env.s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(body).encode("utf-8"))


def _bytes(env, key):
    return env.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def _stored(env, section_id):
    return json.loads(_bytes(env, _result_key(section_id)))


def _section(env, section_id, *, result=MACHINE, excluded=False, alerts=1):
    if result is not None:
        _put(env, _result_key(section_id), result)
    return SimpleNamespace(
        section_id=section_id,
        classification="Invoice",
        extraction_result_uri=(
            f"s3://{BUCKET}/{_result_key(section_id)}" if result is not None else None
        ),
        confidence_threshold_alerts=[
            {"attribute_name": "Total", "confidence": 0.4, "confidence_threshold": 0.8}
        ]
        * alerts,
        confidence_alert_total=40 if alerts else None,
        missing_required_fields=[{"attribute_name": "Date"}],
        confidence=None,
        blueprint_unscored=False,
        excluded=excluded,
    )


def _document(*sections):
    return SimpleNamespace(
        input_key=KEY,
        sections=list(sections),
        num_pages=len(sections),
        output_bucket=BUCKET,
        confidence_alert_count=80,
        missing_required_field_count=2,
        rule_validation_result=None,
        config_version="lending",
        config_revision=4,
    )


def _item(env, **overrides):
    fields = dict(
        doc_key=KEY,
        cycle_id=CYCLE,
        profile="lending",
        queue="default",
        state="FINALIZING",
        version=5,
        enqueued_at="2026-11-16T09:00:00.000Z",
        enqueued_at_ms=1,
        sections=["1", "2"],
        disposition="complete",
        disposed_by=REVIEWER,
        disposed_at=DISPOSED_AT,
        finalize_token="rvw-" + "ab" * 16,
        finalize_execution_name=ARN.rsplit(":", 1)[-1],
    )
    fields.update(overrides)
    record = ReviewItemRecord(**fields)
    env.store.client.put_item(item_to_dynamo(record))
    return record


def _draft(env, section_id, corrected, changed):
    """A reviewer's corrected result for one section, and its draft record."""
    _put(env, _draft_key(section_id), corrected)
    draft = DraftRecord(
        doc_key=KEY,
        cycle_id=CYCLE,
        section_id=section_id,
        S3Key=_draft_key(section_id),
        revision=1,
        updated_by=REVIEWER,
        changed_fields=list(changed),
    )
    env.store.client.put_item(
        {
            **draft.to_attributes(),
            "PK": keys.doc_pk(KEY),
            "SK": keys.draft_sk(CYCLE, section_id),
        }
    )
    return draft


def _apply(env, document):
    return apply_review_changes(
        document,
        store=env.store,
        s3=s3_module,
        output_bucket=BUCKET,
        execution_arn=ARN,
    )


CORRECTED = {
    "inference_result": {"Vendor": "ACME Corp", "Total": 120},
    "explainability_info": [
        {"Total": {"confidence": 0.4, "confidence_threshold": 0.8}}
    ],
    "metadata": {"model": "m"},
}


class TestCorrections:
    def test_corrections_land_on_the_section_result(self, env):
        _item(env)
        _draft(env, "1", CORRECTED, ["Vendor", "Total"])
        document = _document(_section(env, "1"), _section(env, "2"))

        result = _apply(env, document)

        assert result == ApplyReviewResult(
            applied_sections=1,
            confirmed_sections=2,
            extracted_sections=0,
            cycle_id=CYCLE,
        )
        stored = _stored(env, "1")
        assert stored["inference_result"] == {"Vendor": "ACME Corp", "Total": 120}
        # The rest of the result is kept.
        assert stored["explainability_info"] == CORRECTED["explainability_info"]
        assert stored["metadata"] == {"model": "m"}
        # The section the reviewer left alone keeps the machine's values.
        assert _stored(env, "2")["inference_result"] == MACHINE["inference_result"]

    def test_the_record_names_what_the_reviewer_changed(self, env):
        _item(env)
        _draft(env, "1", CORRECTED, ["Vendor", "Total"])

        _apply(env, _document(_section(env, "1")))

        assert _stored(env, "1")["review"] == {
            "v": 1,
            "cycle_id": CYCLE,
            "reviewed_by": REVIEWER,
            "reviewed_at": DISPOSED_AT,
            "confirmed": True,
            "changed_paths": ["Vendor", "Total"],
            "changed_paths_truncated": False,
        }


class TestEverySectionIsConfirmed:
    def test_a_section_without_a_draft_gets_the_record_too(self, env):
        """Completing a review confirms the sections the reviewer did not touch."""
        _item(env)
        _draft(env, "1", CORRECTED, ["Total"])
        document = _document(_section(env, "1"), _section(env, "2"), _section(env, "3"))

        result = _apply(env, document)

        assert result.confirmed_sections == 3
        for section_id in ("1", "2", "3"):
            assert _stored(env, section_id)["review"]["confirmed"] is True
        assert _stored(env, "2")["review"]["changed_paths"] == []
        assert _stored(env, "3")["review"]["changed_paths"] == []

    def test_an_excluded_section_is_untouched(self, env):
        _item(env)
        excluded = _section(env, "2", excluded=True)
        before = _bytes(env, _result_key("2"))
        document = _document(_section(env, "1"), excluded)

        result = _apply(env, document)

        assert result.confirmed_sections == 1
        assert _bytes(env, _result_key("2")) == before
        # Its alerts are not this review's to clear.
        assert excluded.confidence_threshold_alerts
        assert excluded.confidence_alert_total == 40
        assert excluded.missing_required_fields

    def test_a_section_with_no_result_has_nothing_to_record(self, env):
        _item(env)
        document = _document(_section(env, "1"), _section(env, "2", result=None))

        result = _apply(env, document)

        assert result.confirmed_sections == 1
        assert document.sections[1].confidence_threshold_alerts == []

    def test_the_document_reads_as_reviewed_afterwards(self, env):
        _item(env)
        document = _document(_section(env, "1"), _section(env, "2"))
        config = SimpleNamespace(classes=[], hitl=SimpleNamespace(enabled=True))
        before = build_inputs(
            document, config, RoutingTable(), mode="pipeline", s3=s3_module
        ).values

        _apply(env, document)
        after = build_inputs(
            document, config, RoutingTable(), mode="pipeline", s3=s3_module
        ).values

        assert (before["reviewed"], before["confidence_alert_count"]) == (False, 80)
        assert (after["reviewed"], after["confidence_alert_count"]) == (True, 0)


class TestTheDocument:
    def test_the_alerts_and_missing_fields_are_cleared(self, env):
        _item(env)
        document = _document(_section(env, "1"), _section(env, "2"))

        _apply(env, document)

        for section in document.sections:
            assert section.confidence_threshold_alerts == []
            assert section.confidence_alert_total is None
            assert section.missing_required_fields == []
        assert document.confidence_alert_count == 0
        assert document.missing_required_field_count == 0


class TestChangedPaths:
    def test_more_than_the_limit_are_cut_and_flagged(self, env):
        _item(env)
        paths = [f"Lines[{i}].Amount" for i in range(MAX_CHANGED_PATHS + 50)]
        _draft(env, "1", CORRECTED, paths)

        _apply(env, _document(_section(env, "1")))

        record = _stored(env, "1")["review"]
        assert record["changed_paths"] == paths[:MAX_CHANGED_PATHS]
        assert record["changed_paths_truncated"] is True

    def test_a_draft_that_was_already_cut_stays_flagged(self):
        item = ReviewItemRecord(
            doc_key=KEY,
            cycle_id=CYCLE,
            profile="p",
            queue="default",
            state="FINALIZING",
            enqueued_at="t",
            enqueued_at_ms=1,
        )
        draft = DraftRecord(
            section_id="1",
            S3Key="k",
            revision=1,
            changed_fields=["A"],
            changed_fields_truncated=True,
        )

        assert review_record(item, draft)["changed_paths_truncated"] is True

    def test_a_path_too_long_to_keep_is_left_out_and_flagged(self):
        item = ReviewItemRecord(
            doc_key=KEY,
            cycle_id=CYCLE,
            profile="p",
            queue="default",
            state="FINALIZING",
            enqueued_at="t",
            enqueued_at_ms=1,
        )
        draft = DraftRecord(
            section_id="1", S3Key="k", revision=1, changed_fields=["A", "x" * 300, "B"]
        )

        record = review_record(item, draft)

        assert record["changed_paths"] == ["A", "B"]
        assert record["changed_paths_truncated"] is True


class TestRunningItAgain:
    def test_a_second_run_gives_the_same_bytes(self, env):
        _item(env)
        _draft(env, "1", CORRECTED, ["Total"])
        document = _document(_section(env, "1"), _section(env, "2"))

        first = _apply(env, document)
        after_first = {s: _bytes(env, _result_key(s)) for s in ("1", "2")}
        second = _apply(env, document)
        after_second = {s: _bytes(env, _result_key(s)) for s in ("1", "2")}

        assert first == second
        assert after_first == after_second

    def test_the_finalize_start_is_recorded_once(self, env):
        _item(env)
        document = _document(_section(env, "1"))

        _apply(env, document)
        started = env.store.get_item(KEY).finalize_started_at
        _apply(env, document)

        assert started
        assert env.store.get_item(KEY).finalize_started_at == started


class TestARunThatDoesNotFollowACompletedReview:
    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"state": "OPEN", "disposition": None}, id="still-open"),
            pytest.param({"disposition": "rejected"}, id="rejected"),
            pytest.param({"disposition": "skipped"}, id="skipped"),
            pytest.param({"finalize_execution_name": "another-run"}, id="another-run"),
            pytest.param({"state": "COMPLETE"}, id="already-complete"),
        ],
    )
    def test_it_is_refused_and_nothing_is_written(self, env, overrides):
        _item(env, **overrides)
        section = _section(env, "1")
        before = _bytes(env, _result_key("1"))

        with pytest.raises(ReviewConflict) as refused:
            _apply(env, _document(section))

        assert refused.value.code == "NOT_A_COMPLETED_REVIEW"
        assert _bytes(env, _result_key("1")) == before
        assert section.confidence_threshold_alerts

    def test_a_document_with_no_item_is_refused(self, env):
        with pytest.raises(ReviewConflict, match="NOT_A_COMPLETED_REVIEW"):
            _apply(env, _document(_section(env, "1")))


# A section the run after the review extracted again. SEEN is the result the
# reviewer started from (the base copy), DRAFTED their corrected copy of it, and
# EXTRACTED the result of the new extraction.
SEEN = {
    "inference_result": {
        "Vendor": "ACME",
        "Total": 100,
        "Notes": "void",
        "Lines": [{"Amount": 1}, {"Amount": 2}],
    },
    "explainability_info": [{"Total": {"confidence": 0.4}}],
    "metadata": {"model": "m"},
}
DRAFTED = {
    "inference_result": {
        # Corrected.
        "Vendor": "ACME Corp",
        # Left as it was.
        "Total": 100,
        # "Notes" removed.
        # The second row corrected.
        "Lines": [{"Amount": 1}, {"Amount": 20}],
        # Added: the required field extraction had left out.
        "Date": "2026-11-16",
    },
    "explainability_info": [{"Total": {"confidence": 0.4}}],
    "metadata": {"model": "m"},
}
EXTRACTED = {
    "inference_result": {
        "Vendor": "ACME Inc",
        "Total": 105,
        "Notes": "void again",
        # One row: the row the reviewer corrected is not there any more.
        "Lines": [{"Amount": 1}],
    },
    "explainability_info": [{"Total": {"confidence": 0.9}}],
    "metadata": {"model": "m2"},
}


def _alert(name):
    return {"attribute_name": name, "confidence": 0.4, "confidence_threshold": 0.8}


def _extracted_section(env, section_id="1", *, alerts=("Vendor", "Total"), draft=True):
    """A section with a new result; with ``draft``, its base copy and its draft."""
    section = _section(env, section_id, result=EXTRACTED)
    section.confidence_threshold_alerts = [_alert(name) for name in alerts]
    section.confidence_alert_total = 40
    section.missing_required_fields = [
        {"attribute_name": "Date"},
        {"attribute_name": "Currency"},
    ]
    if draft:
        _put(env, _base_key(section_id), SEEN)
        _draft(env, section_id, DRAFTED, ["Vendor"])
    return section


def _extraction_item(env, *sections):
    """The item of a completed review whose run started at extraction."""
    return _item(env, restart_step="extraction", restart_sections=list(sections))


class TestASectionExtractedAgain:
    """The run after the review extracted the section: its result is new."""

    def test_a_corrected_value_is_set_on_the_new_result(self, env):
        _extraction_item(env, "1")

        result = _apply(env, _document(_extracted_section(env)))

        stored = _stored(env, "1")
        assert stored["inference_result"]["Vendor"] == "ACME Corp"
        # A value the reviewer added.
        assert stored["inference_result"]["Date"] == "2026-11-16"
        assert result == ApplyReviewResult(
            applied_sections=1,
            confirmed_sections=0,
            extracted_sections=1,
            cycle_id=CYCLE,
        )

    def test_the_draft_is_not_copied_over_the_new_result(self, env):
        _extraction_item(env, "1")

        _apply(env, _document(_extracted_section(env)))

        stored = _stored(env, "1")
        # A value the reviewer left as it was keeps what the new extraction read.
        assert stored["inference_result"]["Total"] == 105
        assert stored["explainability_info"] == EXTRACTED["explainability_info"]
        assert stored["metadata"] == {"model": "m2"}

    def test_a_list_row_that_is_gone_is_not_applied(self, env):
        _extraction_item(env, "1")

        _apply(env, _document(_extracted_section(env)))

        stored = _stored(env, "1")
        assert stored["inference_result"]["Lines"] == [{"Amount": 1}]
        assert "Lines[1].Amount" in stored["review"]["unapplied_paths"]

    def test_a_value_the_reviewer_removed_is_not_removed(self, env):
        _extraction_item(env, "1")

        _apply(env, _document(_extracted_section(env)))

        stored = _stored(env, "1")
        assert stored["inference_result"]["Notes"] == "void again"
        assert "Notes" in stored["review"]["unapplied_paths"]

    def test_the_record_says_the_section_was_extracted_again(self, env):
        _extraction_item(env, "1")

        _apply(env, _document(_extracted_section(env)))

        assert _stored(env, "1")["review"] == {
            "v": 1,
            "cycle_id": CYCLE,
            "reviewed_by": REVIEWER,
            "reviewed_at": DISPOSED_AT,
            "confirmed": False,
            "extracted_again": True,
            "changed_paths": ["Date", "Vendor"],
            "changed_paths_truncated": False,
            "unapplied_paths": ["Lines[1].Amount", "Notes"],
        }

    def test_alerts_on_corrected_paths_are_removed_and_the_others_kept(self, env):
        _extraction_item(env, "1")
        section = _extracted_section(env)

        _apply(env, _document(section))

        assert section.confidence_threshold_alerts == [_alert("Total")]
        # "Date" was set by the reviewer; "Currency" is still missing.
        assert section.missing_required_fields == [{"attribute_name": "Currency"}]
        # The list is what is left to count.
        assert section.confidence_alert_total is None

    @pytest.mark.parametrize(
        ("alert", "kept"),
        [
            pytest.param("Vendor", False, id="the-corrected-path"),
            pytest.param("Vendor.Name", False, id="under-it"),
            pytest.param("Vendor[0]", False, id="a-row-of-it"),
            pytest.param("VendorId", True, id="a-longer-name"),
            pytest.param("Lines", True, id="over-a-path-that-was-not-applied"),
            pytest.param("Notes", True, id="a-removal-that-was-not-applied"),
            pytest.param("Total", True, id="another-field"),
        ],
    )
    def test_which_alerts_a_correction_answers(self, env, alert, kept):
        _extraction_item(env, "1")
        section = _extracted_section(env, alerts=(alert,))

        _apply(env, _document(section))

        assert section.confidence_threshold_alerts == ([_alert(alert)] if kept else [])

    def test_a_correction_under_an_alerted_path_answers_the_alert(self, env):
        """An alert on a group is answered by a correction of one of its fields."""
        _extraction_item(env, "1")
        section = _section(
            env, "1", result={"inference_result": {"Address": {"City": "Bonn"}}}
        )
        section.confidence_threshold_alerts = [_alert("Address"), _alert("Addressee")]
        _put(env, _base_key("1"), {"inference_result": {"Address": {"City": "Bonn"}}})
        _draft(env, "1", {"inference_result": {"Address": {"City": "Berlin"}}}, [])

        _apply(env, _document(section))

        assert _stored(env, "1")["inference_result"] == {"Address": {"City": "Berlin"}}
        assert section.confidence_threshold_alerts == [_alert("Addressee")]

    def test_the_document_counts_are_those_of_its_sections(self, env):
        _extraction_item(env, "1")
        document = _document(_extracted_section(env), _section(env, "2"))

        _apply(env, document)

        # Section 1 keeps one alert and one missing field; section 2 is cleared.
        assert document.confidence_alert_count == 1
        assert document.missing_required_field_count == 1

    def test_without_a_draft_it_keeps_its_alerts(self, env):
        _extraction_item(env, "1")
        section = _extracted_section(env, draft=False)
        before = _stored(env, "1")

        result = _apply(env, _document(section))

        assert section.confidence_threshold_alerts == [
            _alert("Vendor"),
            _alert("Total"),
        ]
        assert [m["attribute_name"] for m in section.missing_required_fields] == [
            "Date",
            "Currency",
        ]
        stored = _stored(env, "1")
        assert stored["review"] == {
            "v": 1,
            "cycle_id": CYCLE,
            "reviewed_by": REVIEWER,
            "reviewed_at": DISPOSED_AT,
            "confirmed": False,
            "extracted_again": True,
            "changed_paths": [],
            "changed_paths_truncated": False,
            "unapplied_paths": [],
        }
        # Nothing else of the result changed.
        assert {k: v for k, v in stored.items() if k != "review"} == before
        assert (result.applied_sections, result.extracted_sections) == (0, 1)

    def test_the_other_sections_are_confirmed_as_before(self, env):
        _extraction_item(env, "1")
        other = _section(env, "2")
        _draft(env, "2", CORRECTED, ["Vendor", "Total"])

        result = _apply(env, _document(_extracted_section(env), other))

        stored = _stored(env, "2")
        # Its draft is copied onto its result, and the record confirms it.
        assert stored["inference_result"] == CORRECTED["inference_result"]
        assert stored["review"]["confirmed"] is True
        assert stored["review"]["changed_paths"] == ["Vendor", "Total"]
        assert "extracted_again" not in stored["review"]
        assert other.confidence_threshold_alerts == []
        assert other.missing_required_fields == []
        assert result == ApplyReviewResult(
            applied_sections=2,
            confirmed_sections=1,
            extracted_sections=1,
            cycle_id=CYCLE,
        )

    def test_no_named_section_means_every_processed_section(self, env):
        _extraction_item(env)
        excluded = _section(env, "3", excluded=True)
        before = _bytes(env, _result_key("3"))
        document = _document(
            _extracted_section(env), _extracted_section(env, "2", draft=False), excluded
        )

        result = _apply(env, document)

        assert result.extracted_sections == 2
        assert result.confirmed_sections == 0
        for section_id in ("1", "2"):
            assert _stored(env, section_id)["review"]["extracted_again"] is True
        assert _bytes(env, _result_key("3")) == before

    def test_without_a_base_copy_every_value_of_the_draft_is_a_correction(self, env):
        _extraction_item(env, "1")
        section = _extracted_section(env)
        env.s3.delete_object(Bucket=BUCKET, Key=_base_key("1"))

        _apply(env, _document(section))

        stored = _stored(env, "1")
        assert stored["inference_result"] == {
            "Vendor": "ACME Corp",
            "Total": 100,
            "Notes": "void again",
            "Lines": [{"Amount": 1}],
            "Date": "2026-11-16",
        }
        assert stored["review"]["changed_paths"] == [
            "Date",
            "Lines[0].Amount",
            "Total",
            "Vendor",
        ]
        assert stored["review"]["unapplied_paths"] == ["Lines[1].Amount"]

    def test_a_base_copy_that_cannot_be_read_fails_the_step(self, env, monkeypatch):
        """Only a missing copy reads as empty: another failure is not a correction."""
        _extraction_item(env, "1")
        section = _extracted_section(env)
        before = _bytes(env, _result_key("1"))
        read = s3_module.get_json_content

        def denied(uri):
            if uri.endswith(_base_key("1")):
                raise ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetObject"
                )
            return read(uri)

        monkeypatch.setattr(s3_module, "get_json_content", denied)

        with pytest.raises(ClientError):
            _apply(env, _document(section))

        assert _bytes(env, _result_key("1")) == before

    def test_a_second_run_writes_the_same_objects(self, env):
        _extraction_item(env, "1")
        other = _section(env, "2")
        _draft(env, "2", CORRECTED, ["Total"])
        document = _document(_extracted_section(env), other)
        watched = [_result_key("1"), _result_key("2"), _base_key("1"), _draft_key("1")]

        first = _apply(env, document)
        after_first = {key: _bytes(env, key) for key in watched}
        counts = (
            document.confidence_alert_count,
            document.missing_required_field_count,
        )
        second = _apply(env, document)

        assert first == second
        assert {key: _bytes(env, key) for key in watched} == after_first
        assert json.loads(after_first[_base_key("1")]) == SEEN
        assert json.loads(after_first[_draft_key("1")]) == DRAFTED
        assert (
            document.confidence_alert_count,
            document.missing_required_field_count,
        ) == counts

    def test_the_document_does_not_read_as_reviewed_afterwards(self, env):
        """Routing counts the section again: its values are not confirmed."""
        _extraction_item(env, "1")
        document = _document(_extracted_section(env), _section(env, "2"))
        config = SimpleNamespace(classes=[], hitl=SimpleNamespace(enabled=True))

        _apply(env, document)
        after = build_inputs(
            document, config, RoutingTable(), mode="pipeline", s3=s3_module
        ).values

        assert after["reviewed"] is False
        # The alert on "Total" of section 1. Section 2 is confirmed: it counts none.
        assert after["confidence_alert_count"] == 1

    def test_an_item_that_restarts_at_apply_review_changes_extracts_nothing(self, env):
        _item(env, restart_step="apply_review_changes", restart_sections=[])
        _draft(env, "1", CORRECTED, ["Vendor", "Total"])

        result = _apply(env, _document(_section(env, "1")))

        assert result.extracted_sections == 0
        assert _stored(env, "1")["review"]["confirmed"] is True
        assert _stored(env, "1")["inference_result"] == CORRECTED["inference_result"]


class TestMergeCorrections:
    def test_only_the_values_the_reviewer_changed_are_set(self):
        new = {"inference_result": {"A": 1, "B": 2}}

        applied, unapplied = merge_corrections(
            {"inference_result": {"A": "a", "B": "b"}},
            {"inference_result": {"A": "a", "B": "corrected"}},
            new,
        )

        assert (applied, unapplied) == (["B"], [])
        assert new == {"inference_result": {"A": 1, "B": "corrected"}}

    def test_a_result_without_values_takes_no_correction(self):
        new = {"metadata": {}}

        applied, unapplied = merge_corrections(
            {"inference_result": {"A": "a"}}, {"inference_result": {"A": "b"}}, new
        )

        assert (applied, unapplied) == ([], ["A"])
        assert new == {"metadata": {}}

    def test_a_key_the_path_grammar_cannot_spell_is_not_applied(self):
        new = {"inference_result": {"": "x", "A": 1}}

        applied, unapplied = merge_corrections(
            {"inference_result": {"": "x"}}, {"inference_result": {"": "y"}}, new
        )

        assert (applied, unapplied) == ([], [""])
        assert new == {"inference_result": {"": "x", "A": 1}}

    def test_merging_again_changes_nothing(self):
        base = {"inference_result": {"A": "a", "Rows": [{"N": 1}, {"N": 2}]}}
        draft = {"inference_result": {"A": "b", "Rows": [{"N": 1}, {"N": 3}]}}
        new = {"inference_result": {"A": "z", "Rows": [{"N": 9}]}}

        first = merge_corrections(base, draft, new)
        once = json.dumps(new, sort_keys=True)
        second = merge_corrections(base, draft, new)

        assert first == second == (["A"], ["Rows[1].N"])
        assert json.dumps(new, sort_keys=True) == once


class TestTheExtractedRecord:
    def _record(self, applied, unapplied):
        item = ReviewItemRecord(
            doc_key=KEY,
            cycle_id=CYCLE,
            profile="p",
            queue="default",
            state="FINALIZING",
            enqueued_at="t",
            enqueued_at_ms=1,
        )
        return extracted_review_record(item, applied, unapplied)

    def test_the_applied_paths_are_cut_and_flagged(self):
        paths = [f"Lines[{i}].Amount" for i in range(MAX_CHANGED_PATHS + 5)]

        record = self._record(paths, [])

        assert record["changed_paths"] == paths[:MAX_CHANGED_PATHS]
        assert record["changed_paths_truncated"] is True

    def test_the_unapplied_paths_are_cut_the_same_way(self):
        paths = [f"Lines[{i}].Amount" for i in range(MAX_CHANGED_PATHS + 5)]

        record = self._record(["A"], [*paths, "x" * 300])

        assert record["changed_paths"] == ["A"]
        assert record["unapplied_paths"] == paths[:MAX_CHANGED_PATHS]
        assert record["changed_paths_truncated"] is True

    def test_short_lists_are_not_flagged(self):
        assert self._record(["A"], ["B"])["changed_paths_truncated"] is False
