# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The run plan Process Changes queues with a document's saved edits.

Process Changes edits a document's sections and pages and queues a run. The run
plan says where that run starts, decided from the document as edited: at
classification when a page's class was reset, at extraction for the sections that
have no result, and otherwise at policy rule validation. A document that is not
ready for that start runs from the beginning, as every such run did before run
plans.

``getProcessChangesPlan`` reads that start and the steps of the run before the
caller confirms, and Process Changes returns them with its answer.

Drives the real resolver ``handler`` against moto, with the caller-checks suite's
tables, buckets and queue. The message is read back from the queue.

Engine: moto, not DynamoDB.
"""

import json
from unittest.mock import Mock, patch

import boto3
import pytest

from idp_common.api_adapter import ResourceNotFound
from idp_common.models import Document
from idp_common.run_plan.mode import BDA, PIPELINE, ProcessingMode
from idp_common.run_plan.models import RUN_PLAN_ATTRIBUTE, RunKind, decode
from idp_common.run_plan.steps import display_order

from .test_process_changes_caller_checks import (
    OBJECT_KEY,
    OUTPUT_BUCKET,
    WORKING_BUCKET,
    _call,
    _owned,
    _seed,
    process_changes_env,
)

pytestmark = pytest.mark.unit

POLICY_SUMMARY_KEY = (
    f"{OBJECT_KEY}/rule_validation/consolidated/consolidated_summary.md"
)
POLICY_SECTION_KEY = f"{OBJECT_KEY}/rule_validation/sections/section_1_responses.json"

NO_CHANGES = {"modifiedSections": [], "modifiedPages": []}


@pytest.fixture
def env():
    with process_changes_env() as built:
        yield built


def _page(page_id):
    return {
        "Id": page_id,
        "Class": "invoice",
        "ImageUri": f"s3://{OUTPUT_BUCKET}/{OBJECT_KEY}/pages/{page_id}/image.jpg",
        "TextUri": f"s3://{OUTPUT_BUCKET}/{OBJECT_KEY}/pages/{page_id}/result.json",
    }


def _section(section_id, page_id, with_result=True):
    section = {"Id": section_id, "PageIds": [page_id], "Class": "invoice"}
    section["OutputJSONUri"] = (
        f"s3://{OUTPUT_BUCKET}/{OBJECT_KEY}/sections/{section_id}/result.json"
        if with_result
        else ""
    )
    return section


def _seed_processed(env, sections=("1", "2"), **attributes):
    """A fully processed document: every page has text and an image, every
    section a result, and the tail stages have run."""
    s3 = boto3.client("s3")
    for key in (POLICY_SUMMARY_KEY, POLICY_SECTION_KEY):
        s3.put_object(Bucket=OUTPUT_BUCKET, Key=key, Body=b"{}")
    for section_id in sections:
        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key=f"{OBJECT_KEY}/sections/{section_id}/result.json",
            Body=b'{"a": 1}',
        )
    values = {
        "Pages": [_page(index) for index, _ in enumerate(sections, start=1)],
        "Sections": [
            _section(section_id, index)
            for index, section_id in enumerate(sections, start=1)
        ],
        "RuleValidationResult": json.dumps(
            {
                "request_id": "r",
                "output_uri": f"s3://{OUTPUT_BUCKET}/{POLICY_SUMMARY_KEY}",
            }
        ),
        "RuleValidationResultUri": f"s3://{OUTPUT_BUCKET}/{POLICY_SUMMARY_KEY}",
        "SummaryReportUri": f"s3://{OUTPUT_BUCKET}/{OBJECT_KEY}/summary/summary.md",
        "EvaluationReportUri": f"s3://{OUTPUT_BUCKET}/{OBJECT_KEY}/evaluation/report.md",
        "EvaluationStatus": "COMPLETED",
    }
    values.update(attributes)
    _seed(env, **values)


def _admin(env, changes):
    result = _call(
        env,
        groups=("Admin",),
        username="admin",
        email="admin@example.com",
        changes=changes,
    )
    assert result["success"] is True, result
    return result


def _message(env):
    """The one queued message: (the document it carries, its attributes)."""
    received = boto3.client("sqs").receive_message(
        QueueUrl=env.queue_url, MessageAttributeNames=["All"], MaxNumberOfMessages=10
    )
    (message,) = received["Messages"]
    attributes = {
        name: value["StringValue"]
        for name, value in message["MessageAttributes"].items()
    }
    body = json.loads(message["Body"])
    return Document.load_document(body, WORKING_BUCKET), attributes, body


def _plan(env):
    _, attributes, _ = _message(env)
    return decode(attributes[RUN_PLAN_ATTRIBUTE])


def _keys(prefix):
    listed = boto3.client("s3").list_objects_v2(Bucket=OUTPUT_BUCKET, Prefix=prefix)
    return {item["Key"] for item in listed.get("Contents", [])}


def _reclassify(section_id, page_id):
    return {
        "modifiedSections": [
            {
                "sectionId": section_id,
                "classification": "receipt",
                "pageIds": [str(page_id)],
            }
        ],
        "modifiedPages": [],
    }


class TestTheMessage:
    def test_it_carries_the_event_the_key_and_a_saved_edits_plan(self, env):
        _seed_processed(env)

        result = _admin(env, _reclassify("1", 1))

        document, attributes, body = _message(env)
        assert set(attributes) == {"EventType", "ObjectKey", RUN_PLAN_ATTRIBUTE}
        assert attributes["EventType"] == "DocumentRunRequested"
        assert attributes["ObjectKey"] == OBJECT_KEY
        assert decode(attributes[RUN_PLAN_ATTRIBUTE]).kind == RunKind.SAVED_EDITS
        # The document goes through the working bucket, as before.
        assert body["compressed"] is True
        assert "_process_changes_state.json" in body["s3_uri"]
        assert document.input_key == OBJECT_KEY
        assert result["processingJobId"]


class TestWhereTheRunStarts:
    def test_a_changed_section_starts_at_extraction_for_that_section(self, env):
        _seed_processed(env)

        _admin(env, _reclassify("1", 1))

        plan = _plan(env)
        assert (plan.start, plan.sections, plan.reviewed) == (
            "extraction",
            ["1"],
            False,
        )

    def test_every_section_changed_is_a_plain_extraction_start(self, env):
        _seed_processed(env, sections=("1",))

        _admin(env, _reclassify("1", 1))

        plan = _plan(env)
        assert (plan.start, plan.sections) == ("extraction", [])

    def test_a_new_section_is_extracted(self, env):
        _seed_processed(env)

        _admin(
            env,
            {
                "modifiedSections": [
                    {
                        "sectionId": "3",
                        "classification": "receipt",
                        "pageIds": ["2"],
                        "isNew": True,
                    }
                ],
                "modifiedPages": [],
            },
        )

        plan = _plan(env)
        assert (plan.start, plan.sections) == ("extraction", ["3"])

    def test_a_section_that_already_lacked_a_result_is_extracted_too(self, env):
        """A document that never finished: the run extracts what is missing as
        well as what was changed, as a run from the beginning would."""
        _seed_processed(env, sections=("1", "2", "3"))
        row = env.row()
        row["Sections"][2]["OutputJSONUri"] = ""
        env.tracking.put_item(Item=row)

        _admin(env, _reclassify("1", 1))

        assert _plan(env).sections == ["1", "3"]

    def test_a_page_text_edit_extracts_the_pages_section_again(self, env):
        _seed_processed(env)

        _admin(
            env,
            {
                "modifiedSections": [],
                "modifiedPages": [{"pageId": 2, "textModified": True}],
            },
        )

        plan = _plan(env)
        assert (plan.start, plan.sections) == ("extraction", ["2"])

    def test_a_page_class_reset_starts_at_classification(self, env):
        _seed_processed(env)

        _admin(
            env,
            {
                "modifiedSections": [],
                "modifiedPages": [{"pageId": 1, "classReset": True}],
            },
        )

        document, attributes, _ = _message(env)
        plan = decode(attributes[RUN_PLAN_ATTRIBUTE])
        assert (plan.start, plan.sections) == ("classification", [])
        # As the resolver left it: the page has no class and its section is gone.
        assert document.pages["1"].classification is None
        assert [s.section_id for s in document.sections] == ["2"]

    def test_a_class_reset_wins_over_a_changed_section(self, env):
        _seed_processed(env)

        _admin(
            env,
            {
                **_reclassify("2", 2),
                "modifiedPages": [{"pageId": 1, "classReset": True}],
            },
        )

        assert _plan(env).start == "classification"

    def test_a_deleted_section_leaves_nothing_to_extract(self, env):
        _seed_processed(env)

        _admin(
            env,
            {
                "modifiedSections": [
                    {
                        "sectionId": "2",
                        "classification": "invoice",
                        "pageIds": ["2"],
                        "isDeleted": True,
                    }
                ],
                "modifiedPages": [],
            },
        )

        plan = _plan(env)
        assert (plan.start, plan.sections) == ("policy_rule_validation", [])

    def test_no_changes_start_at_policy_rule_validation(self, env):
        _seed_processed(env)

        _admin(env, NO_CHANGES)

        plan = _plan(env)
        assert (plan.start, plan.sections, plan.skip) == (
            "policy_rule_validation",
            [],
            [],
        )

    def test_section_ids_a_plan_cannot_name_are_left_to_the_steps_own_check(self, env):
        """More than a plan can hold: every section is offered to extraction, and
        the step skips each one that has its result."""
        long_ids = ("a" * 70, "b" * 70)
        _seed_processed(env, sections=long_ids)

        _admin(env, _reclassify(long_ids[0], 1))

        plan = _plan(env)
        assert (plan.start, plan.sections) == ("extraction", [])


class TestADocumentThatIsNotReady:
    def test_it_runs_from_the_beginning(self, env):
        """Pages without text and images: no later step can start, so the run
        starts at the beginning, where each step's own check skips what exists."""
        _seed(
            env, SummaryReportUri="s3://pc-output-bucket/packet.pdf/summary/summary.md"
        )

        _admin(env, NO_CHANGES)

        plan = _plan(env)
        assert (plan.kind, plan.start, plan.sections, plan.reviewed) == (
            RunKind.SAVED_EDITS,
            "beginning",
            [],
            False,
        )
        # Nothing is cleared for it, as before run plans.
        assert env.row()["SummaryReportUri"].endswith("summary.md")

    def test_a_page_with_no_class_that_was_not_reset_runs_from_the_beginning(self, env):
        _seed_processed(env)
        row = env.row()
        row["Pages"][1]["Class"] = ""
        env.tracking.put_item(Item=row)

        _admin(env, _reclassify("1", 1))

        assert _plan(env).start == "beginning"


class TestWhatIsCleared:
    def test_a_policy_rule_validation_start_clears_the_tail(self, env):
        """The run validates the policy rules, summarises and evaluates again, so
        their results are cleared on the queued document, in S3 and on the row."""
        _seed_processed(env)

        _admin(env, NO_CHANGES)

        document, attributes, _ = _message(env)
        assert decode(attributes[RUN_PLAN_ATTRIBUTE]).start == "policy_rule_validation"
        assert document.rule_validation_result is None
        assert document.summary_report_uri is None
        assert document.evaluation_report_uri is None
        assert document.evaluation_status is None
        # Extraction results are kept, on the document and in S3.
        assert all(s.extraction_result_uri for s in document.sections)
        assert len(_keys(f"{OBJECT_KEY}/sections/")) == 2
        assert _keys(f"{OBJECT_KEY}/rule_validation/") == set()
        row = env.row()
        for attribute in (
            "RuleValidationResult",
            "RuleValidationResultUri",
            "SummaryReportUri",
            "EvaluationReportUri",
            "EvaluationStatus",
        ):
            assert attribute not in row, attribute
        assert row["ObjectStatus"] == "QUEUED"

    def test_an_extraction_start_clears_the_policy_result(self, env):
        _seed_processed(env)

        _admin(env, _reclassify("1", 1))

        assert _keys(f"{OBJECT_KEY}/rule_validation/") == set()
        row = env.row()
        assert "RuleValidationResult" not in row
        assert "RuleValidationResultUri" not in row
        # The changed section's result is gone; the other section keeps its own.
        assert _keys(f"{OBJECT_KEY}/sections/") == {
            f"{OBJECT_KEY}/sections/2/result.json"
        }


class TestReviewed:
    def test_a_finished_review_stands_when_nothing_is_extracted_again(self, env):
        _seed_processed(env, **_owned(HITLStatus="Review Completed"))

        _admin(env, NO_CHANGES)

        assert _plan(env).reviewed is True

    def test_a_review_completed_by_this_request_counts(self, env):
        """Process Changes on an open legacy review completes it. The run that
        follows starts after extraction, so the review stands."""
        _seed_processed(env, **_owned())

        result = _call(env, changes=NO_CHANGES)

        assert result["success"] is True, result
        assert env.row()["HITLStatus"] == "Review Completed"
        plan = _plan(env)
        assert (plan.start, plan.reviewed) == ("policy_rule_validation", True)

    def test_a_run_that_extracts_again_is_not_marked_reviewed(self, env):
        _seed_processed(env, **_owned(HITLStatus="Review Completed"))

        _admin(env, _reclassify("1", 1))

        assert _plan(env).reviewed is False

    def test_a_document_with_no_review_is_not_marked_reviewed(self, env):
        _seed_processed(env)

        _admin(env, NO_CHANGES)

        assert _plan(env).reviewed is False


# ------------------------------------------------------------- the plan read

_ADMIN = {"groups": ("Admin",), "username": "admin", "email": "admin@example.com"}


def _read(env, groups=("Reviewer",), username="rev", email="rev@example.com"):
    """``getProcessChangesPlan``, by default as the Reviewer ``_call`` calls as."""
    return env.mod.handler(
        {
            "info": {"fieldName": "getProcessChangesPlan"},
            "identity": {
                "username": username,
                "claims": {
                    "cognito:username": username,
                    "email": email,
                    "cognito:groups": list(groups),
                },
            },
            "arguments": {"objectKey": OBJECT_KEY},
        },
        None,
    )


def _state(env):
    """Everything Process Changes writes: the tracking rows, the objects in the
    output and working buckets, and the queue."""
    s3 = boto3.client("s3")
    objects = {
        (bucket, item["Key"]): item["ETag"]
        for bucket in (OUTPUT_BUCKET, WORKING_BUCKET)
        for item in s3.list_objects_v2(Bucket=bucket).get("Contents", [])
    }
    return env.tracking.scan()["Items"], objects, env.queued()


def _states(steps):
    return {step["id"]: step["state"] for step in steps}


def _raised(call, env, **caller):
    """The refusal a call raises, as the dispatcher reads it: (class name, text)."""
    with pytest.raises((PermissionError, ValueError)) as refused:
        call(env, **caller)
    return type(refused.value).__name__, str(refused.value)


class TestThePlanRead:
    """``getProcessChangesPlan``: where Process Changes would start for the
    document as saved, and the steps of that run, before the caller confirms."""

    @pytest.mark.parametrize(
        "prepare, start, sections",
        [
            pytest.param(_seed_processed, "policy_rule_validation", []),
            pytest.param(
                lambda env: _seed_processed(
                    env,
                    sections=("1", "2", "3"),
                    Sections=[
                        _section("1", 1),
                        _section("2", 2),
                        _section("3", 3, with_result=False),
                    ],
                ),
                "extraction",
                ["3"],
            ),
            pytest.param(_seed, "beginning", []),
        ],
        ids=["nothing-to-extract", "a-section-with-no-result", "not-ready"],
    )
    def test_it_returns_the_start_the_call_then_uses(
        self, env, prepare, start, sections
    ):
        prepare(env)

        read = _read(env, **_ADMIN)
        result = _admin(env, NO_CHANGES)

        plan = _plan(env)
        assert (plan.start, plan.sections) == (start, sections)
        assert read["startStep"] == plan.start
        assert read["sections"] == [
            {"sectionId": section_id, "cause": "no_result"}
            for section_id in plan.sections
        ]
        # The answer of the call carries the same start and the same steps.
        assert result["startStep"] == read["startStep"]
        assert result["steps"] == read["steps"]

    def test_the_answer_is_a_planned_run_in_camel_case(self, env):
        _seed_processed(env)

        read = _read(env, **_ADMIN)

        assert set(read) == {"mode", "startStep", "steps", "sections"}
        assert read["mode"] == PIPELINE
        assert all(set(step) == {"id", "label", "state"} for step in read["steps"])
        # Every step of the mode, in order: what is before the start is kept, and
        # the run does the rest.
        ids = [step["id"] for step in read["steps"]]
        assert ids == list(display_order(PIPELINE))
        first = ids.index("policy_rule_validation")
        assert [step["state"] for step in read["steps"]] == ["skipped"] * first + [
            "run"
        ] * (len(ids) - first)
        labels = {step["id"]: step["label"] for step in read["steps"]}
        assert labels["policy_rule_validation"] == "Policy Rule Validation"
        assert labels["review_routing"] == "Review Routing"

    def test_the_call_answers_with_the_start_and_the_steps_of_its_run(self, env):
        _seed_processed(env)

        result = _admin(env, _reclassify("1", 1))

        assert result["startStep"] == _plan(env).start == "extraction"
        states = _states(result["steps"])
        assert all(set(step) == {"id", "label", "state"} for step in result["steps"])
        assert (states["classification"], states["extraction"]) == ("skipped", "run")

    def test_a_step_the_configuration_switches_off_is_listed_as_not_enabled(self, env):
        _seed_processed(env)
        mode = ProcessingMode(
            PIPELINE, None, {"summarization": {"enabled": False}}, False
        )

        with patch.object(env.mod, "_processing_mode", return_value=mode):
            read = _read(env, **_ADMIN)
            result = _admin(env, NO_CHANGES)

        states = _states(read["steps"])
        assert (states["summarization"], states["evaluation"]) == (
            "not_enabled",
            "run",
        )
        assert result["steps"] == read["steps"]

    def test_the_steps_are_those_of_the_documents_processing_mode(self, env):
        _seed_processed(env)
        mode = ProcessingMode(BDA, "arn:aws:bedrock:us-east-1:1:project/p", None, False)

        with patch.object(env.mod, "_processing_mode", return_value=mode):
            read = _read(env, **_ADMIN)

        assert read["mode"] == BDA
        assert [step["id"] for step in read["steps"]] == list(display_order(BDA))

    def test_the_owner_of_an_open_review_is_served(self, env):
        _seed_processed(env, **_owned())

        assert _read(env)["startStep"] == "policy_rule_validation"

    def test_it_writes_nothing(self, env):
        """Process Changes on this document completes the review, clears the
        policy result, queues a run and marks the row queued. The read does none
        of it, and does not reach S3 or the queue at all."""
        _seed_processed(env, **_owned())
        before = _state(env)

        with (
            patch.object(env.mod, "s3_client", Mock(spec=[])),
            patch.object(env.mod, "sqs_client", Mock(spec=[])),
        ):
            _read(env)
            _read(env, **_ADMIN)

        assert _state(env) == before
        # The snapshot sees each kind of write: the call that follows changes the
        # row, the objects and the queue.
        assert _call(env, changes=NO_CHANGES)["success"] is True
        assert all(now != then for now, then in zip(_state(env), before))

    @pytest.mark.parametrize(
        "seeded, caller, refusal, text",
        [
            pytest.param(
                _owned(HITLReviewOwner="alice"),
                {},
                "ResourceAccessDenied",
                "review owner",
                id="a-review-owned-by-another",
            ),
            pytest.param(
                {}, {}, "ResourceAccessDenied", "review owner", id="no-review"
            ),
            pytest.param(
                _owned(HITLStatus="Review Completed"),
                {},
                "ValueError",
                "already finished",
                id="a-finished-review",
            ),
            pytest.param(
                {**_owned(), "ObjectStatus": "RUNNING"},
                {},
                "ValueError",
                "Wait for processing to finish",
                id="a-document-in-flight",
            ),
            pytest.param(
                {**_owned(), "ObjectStatus": "RUNNING"},
                _ADMIN,
                "ValueError",
                "RUN_IN_PROGRESS: ",
                id="an-admin-on-a-document-in-flight",
            ),
            pytest.param(
                {"ObjectStatus": "QUARANTINED"},
                _ADMIN,
                "ValueError",
                "MALWARE_GATE: ",
                id="an-admin-on-a-document-the-malware-gate-holds",
            ),
            pytest.param(
                {**_owned(), "TestSetId": "ts-1"},
                {},
                "ResourceAccessDenied",
                "test-set documents are worked by Annotators",
                id="a-reviewer-on-a-test-set-document",
            ),
            pytest.param(
                {"HITLStatus": "in_review"},
                _ADMIN,
                "ValueError",
                "QUEUE_MANAGED: ",
                id="a-document-in-a-review-queue",
            ),
            pytest.param(
                _owned(),
                {"groups": ("Viewer",)},
                "PermissionError",
                "requires one of Admin or Reviewer group",
                id="a-caller-outside-the-groups",
            ),
        ],
    )
    def test_it_refuses_what_process_changes_refuses(
        self, env, seeded, caller, refusal, text
    ):
        _seed(env, **seeded)
        before = _state(env)

        read = _raised(_read, env, **caller)
        call = _raised(_call, env, **caller)

        # The class is what the dispatcher maps to a status: the same for both.
        assert read[0] == call[0] == refusal
        assert text in read[1] and text in call[1]
        assert _state(env) == before

    def test_a_scoped_caller_is_refused_a_document_outside_their_scope(self, env):
        env.scope("rev@example.com", ["tenant-b"])
        _seed(env, **_owned())

        read = _raised(_read, env)
        call = _raised(_call, env)

        assert read[0] == call[0] == "ResourceAccessDenied"

    def test_a_scoped_caller_is_not_told_whether_a_document_exists(self, env):
        """The scope is checked before the document is read, so a document that
        is out of scope and one that does not exist are refused alike."""
        env.scope("rev@example.com", ["tenant-b"])

        missing = _raised(_read, env)
        _seed(env, **_owned())
        out_of_scope = _raised(_read, env)

        assert missing == out_of_scope
        assert missing[0] == "ResourceAccessDenied"

    def test_a_document_that_does_not_exist_is_not_found(self, env):
        with pytest.raises(ResourceNotFound, match=OBJECT_KEY) as missing:
            _read(env, **_ADMIN)

        # 404 through the dispatcher, which reads the class name.
        assert type(missing.value).__name__ == "ResourceNotFound"

    def test_the_group_refusal_names_the_read(self, env):
        _seed_processed(env)

        with pytest.raises(PermissionError, match="getProcessChangesPlan"):
            _read(env, groups=("Author", "Annotator", "Viewer"))

    def test_a_fault_is_not_answered_as_a_failed_write(self, env):
        """Process Changes answers a write that failed with a 200 body that says
        so. A read has no such body: its fault leaves as a fault."""
        _seed_processed(env)

        with patch.object(
            env.mod, "create_document_service", side_effect=RuntimeError("throttled")
        ):
            with pytest.raises(RuntimeError, match="throttled"):
                _read(env, **_ADMIN)
