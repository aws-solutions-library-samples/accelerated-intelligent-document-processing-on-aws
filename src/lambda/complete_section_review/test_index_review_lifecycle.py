# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the `complete_section_review` Lambda, the resolver behind every
human-in-the-loop (HITL) review action the web UI offers: claim a document, release
it, complete one section's review (optionally saving the reviewer's edits), and skip
all remaining sections.

This function is the only writer of a document's review state, and its failure modes
are not crashes. A document whose `HITLSectionsPending` never empties is stranded
mid-workflow — it stays in the review queue forever and the summarization and
evaluation steps that run after review never fire. A reviewer's edits that are
accepted and not persisted are lost silently, because the UI resets its dirty-state
tracking on a successful response. And a claim that is advisory rather than exclusive
lets two annotators label the same document, each overwriting the other. None of
those raise; they are all "success" responses with the wrong side effects.

Four things shape these tests.

**DynamoDB and S3 are real (moto), not mocked.** Most of what can go wrong here is
in an `UpdateExpression` or an S3 key, and a `MagicMock` table accepts any expression
and any key. The `REMOVE HITLPendingReview` clause that takes a finished document out
of the queue, the `ConditionExpression` that makes `claimReview` exclusive, and the
`{test_set_id}/baseline/{filename}/sections/{section_id}/result.json` key that makes a
correction reusable as ground truth are therefore asserted by reading back what the
service actually stored.

**The two `idp_common` modules that decide an authorization or a numeric outcome are
the real ones.** `idp_common.testset_scope` is the shared enforcement point for
annotator scope — the handler's `_assert_annotator_scope` exists because the Lambda
and the library once disagreed about a caller holding both `Annotator` and
`Reviewer` — and `idp_common.evaluation.curve_store` decides which confidence bin a
reviewed field lands in. Stubbing either would make the test assert its own fixture
data. `create_document_service` *is* patched, because the document loader is the one
dependency whose behaviour is not under test here.

**The document object is a small real class, not a `MagicMock`.** `trigger_reprocessing`
and `complete_section_review` mutate it (`status`, `hitl_sections_pending`,
`hitl_status`), and a `MagicMock` would accept and record any assignment including a
wrong one. A real object lets the test read back the value that the workflow will
actually see.

**"Best effort" is asserted in both directions.** Writing the test-set baseline, the
confidence-curve observation and the reprocessing trigger must never fail a review;
persisting the reviewer's edits to the section output must always fail it rather than
report success. Those two contracts are opposite and easy to invert, so each is
pinned with a failure injected at the boundary.

⚠️ **On a best-effort path, "nothing was written and nothing raised" is not an
assertion.** Every one of those functions ends in a blanket `except` that logs, and
most of them hold two guards in a row, so deleting the first guard leaves the work
below it to raise into the `except` — producing the same empty bucket, the same
absent item and the same absence of an exception as the guard produced. A test
asserting only that outcome passes with the guard it names deleted. So each guard
here is asserted on something only the guard can produce: the call below it not
being made (`recorded`, or a mock asserted `not_called`), or the specific line it
logs (`log_spy`), which differs from the line the `except` logs. Where the two are
genuinely indistinguishable by outcome, that is said in the test's own docstring.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from moto import mock_aws  # noqa: E402

# The real scope and curve modules, imported before any suite in this directory
# installs a MagicMock stub for the `idp_common` package. See the module docstring.
import idp_common.evaluation.curve_store as curve_store  # noqa: E402
import idp_common.testset_scope as testset_scope  # noqa: E402

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

TRACKING_TABLE = "review-tracking"
USERS_TABLE = "review-users"
TEST_SET_BUCKET = "review-test-sets"
OUTPUT_BUCKET = "review-output"
QUEUE_NAME = "review-reprocess"

OBJECT_KEY = "run-7/invoice.pdf"
DOC_PK = f"doc#{OBJECT_KEY}"


class FakeSection:
    """A document section as the resolver reads it: an id and an output URI."""

    def __init__(self, section_id, extraction_result_uri=None):
        self.section_id = section_id
        self.extraction_result_uri = extraction_result_uri


class FakeDocument:
    """A document the resolver can mutate, so the test can read the result back."""

    def __init__(self, sections=(), pending=None, completed=None, hitl_status=None):
        self.sections = list(sections)
        self.hitl_sections_pending = list(pending or [])
        self.hitl_sections_completed = list(completed or [])
        self.hitl_status = hitl_status
        self.input_bucket = None
        self.output_bucket = None
        self.status = None
        self.start_time = "2026-01-01T00:00:00Z"
        self.completion_time = "2026-01-01T00:10:00Z"
        self.workflow_execution_arn = "arn:aws:states:us-east-1:1:execution:x:y"
        self.serialize_calls = []

    def serialize_document(self, bucket, prefix, _logger):
        self.serialize_calls.append((bucket, prefix))
        return {"shape": "compressed", "object_key": OBJECT_KEY}

    def to_dict(self):
        return {"shape": "plain", "object_key": OBJECT_KEY}


class FakeDocumentService:
    """Stands in for `create_document_service(mode="dynamodb")`."""

    def __init__(self, document):
        self.document = document
        self.updated = []

    def get_document(self, _object_key):
        return self.document

    def update_document(self, document):
        self.updated.append(document)


@pytest.fixture
def aws(monkeypatch):
    """A moto-backed account with the tracking table, buckets and queue in place."""
    for name, value in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - dummy moto credential
        "AWS_SESSION_TOKEN": "testing",  # nosec B105 - dummy moto credential
        "TRACKING_TABLE_NAME": TRACKING_TABLE,
        "OUTPUT_BUCKET": OUTPUT_BUCKET,
        "TEST_SET_BUCKET": TEST_SET_BUCKET,
        "USERS_TABLE_NAME": USERS_TABLE,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("QUEUE_URL", raising=False)
    monkeypatch.delenv("WORKING_BUCKET", raising=False)
    monkeypatch.delenv("INPUT_BUCKET", raising=False)
    testset_scope.clear_scope_cache()
    with mock_aws():
        yield
    testset_scope.clear_scope_cache()


@pytest.fixture
def mod(aws):
    """Import the resolver fresh, so its module-level clients are moto-backed."""
    if MODULE_DIR not in sys.path:
        sys.path.insert(0, MODULE_DIR)
    sys.modules.pop("index", None)
    module = importlib.import_module("index")

    module.dynamodb.create_table(
        TableName=TRACKING_TABLE,
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
    module.dynamodb.create_table(
        TableName=USERS_TABLE,
        KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "userId", "AttributeType": "S"},
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
    for bucket in (TEST_SET_BUCKET, OUTPUT_BUCKET):
        module.s3_client.create_bucket(Bucket=bucket)
    yield module
    sys.modules.pop("index", None)


@pytest.fixture
def table(mod):
    return mod.dynamodb.Table(TRACKING_TABLE)


def put_doc(table, **attributes):
    item = {"PK": DOC_PK, "SK": "none"}
    item.update(attributes)
    table.put_item(Item=item)
    return item


def read_doc(table, object_key=OBJECT_KEY):
    return table.get_item(Key={"PK": f"doc#{object_key}", "SK": "none"}).get("Item", {})


def scope_user(table_resource, email, allowed):
    table_resource.put_item(
        Item={"userId": email, "email": email, "allowedTestSets": allowed}
    )


_DEFAULT_ARGS = object()


def event(field_name, groups, *, arguments=_DEFAULT_ARGS, email="rev@example.com"):
    if arguments is _DEFAULT_ARGS:
        arguments = {"objectKey": OBJECT_KEY, "sectionId": "sec-1"}
    return {
        "info": {"fieldName": field_name},
        "arguments": arguments,
        "identity": {
            "username": email.split("@")[0],
            "claims": {"email": email, "cognito:groups": groups},
        },
    }


def s3_json(mod, bucket, key):
    body = mod.s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


def baseline_key(test_set_id, filename, section_id):
    return f"{test_set_id}/baseline/{filename}/sections/{section_id}/result.json"


class LogSpy:
    """What the resolver logged, as one blob of text.

    Several guards below are distinguishable from the no-op they protect only by the
    line they emit: with the guard the function returns quietly, without it the work
    below raises into the blanket `except` and logs something else. Asserting on the
    message is therefore asserting on the branch that ran.
    """

    def __init__(self):
        self.lines = []

    def record(self, message, *_args, **_kwargs):
        self.lines.append(str(message))

    @property
    def text(self):
        return "\n".join(self.lines)


@contextmanager
def log_spy(mod):
    spy = LogSpy()
    with (
        patch.object(mod.logger, "info", spy.record),
        patch.object(mod.logger, "warning", spy.record),
        patch.object(mod.logger, "error", spy.record),
    ):
        yield spy


@contextmanager
def recorded(target, name):
    """Record calls to `target.name` while still making them.

    Yields the list of `(args, kwargs)` actually passed, so a guard can be asserted
    on the call it prevents rather than on an outcome its absence reproduces. A
    plain function is used rather than `patch.object(..., wraps=...)` because on a
    class attribute a `MagicMock` is not a descriptor, so the call would arrive
    without `self`; a function assigned to a class binds normally, and one assigned
    to a module or to an instance is looked up without binding — which makes
    `real(*args)` correct in all three cases.
    """
    real = getattr(target, name)
    calls = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    with patch.object(target, name, spy):
        yield calls


# --------------------------------------------------------------------------
# Authorization: who may reach a review operation at all
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "field_name",
    [
        "completeSectionReview",
        "claimReview",
        "releaseReview",
        "skipAllSectionsReview",
    ],
)
@pytest.mark.parametrize("group", ["Viewer", "Author", "SomeOtherGroup"])
def test_a_caller_outside_the_three_review_groups_reaches_no_operation(
    mod, table, field_name, group
):
    """Defense in depth: the schema directive is not the only gate.

    Asserted on the stored item as well as the exception, because an
    authorization check placed after the write would still raise while having
    already changed the document.
    """
    put_doc(table, HITLStatus="Review Pending", HITLSectionsPending=["sec-1"])
    with pytest.raises(ValueError, match="Admin, Reviewer or Annotator"):
        mod.handler(event(field_name, [group]), None)
    assert read_doc(table)["HITLStatus"] == "Review Pending"
    assert read_doc(table)["HITLSectionsPending"] == ["sec-1"]


@pytest.mark.unit
def test_a_groups_claim_delivered_as_a_bare_string_still_authorizes(mod, table):
    """Cognito sends a single group as a string, not a one-element list.

    Without the normalization, `{"Admin", "Reviewer", "Annotator"} & "Reviewer"`
    intersects a set of words with a set of characters and comes back empty, so a
    legitimate reviewer would be refused every operation.
    """
    put_doc(table, HITLReviewOwner="")
    with patch.object(
        mod,
        "create_document_service",
        return_value=FakeDocumentService(FakeDocument([FakeSection("sec-1")])),
    ):
        mod.handler(
            event("claimReview", "Reviewer", arguments={"objectKey": OBJECT_KEY}), None
        )
    assert read_doc(table)["HITLReviewOwner"] == "rev"


@pytest.mark.unit
def test_an_annotator_may_not_skip_a_document_without_looking_at_it(mod, table):
    """`Annotator` passes the outer gate but skip-all is the set owner's call.

    Skipping marks every section reviewed with no human having read them, which
    would write unverified labels into a golden dataset.
    """
    put_doc(table)
    with pytest.raises(ValueError, match="administrators and reviewers"):
        mod.handler(
            event(
                "skipAllSectionsReview",
                ["Annotator"],
                arguments={"objectKey": OBJECT_KEY},
            ),
            None,
        )
    assert "HITLCompleted" not in read_doc(table)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field_name", "arguments", "message"),
    [
        ("claimReview", {}, "objectKey is required"),
        ("releaseReview", {}, "objectKey is required"),
        ("skipAllSectionsReview", {}, "objectKey is required"),
        ("completeSectionReview", {"objectKey": OBJECT_KEY}, "sectionId are required"),
        ("completeSectionReview", {"sectionId": "sec-1"}, "sectionId are required"),
    ],
)
def test_a_missing_identifier_is_refused_before_any_lookup(
    mod, field_name, arguments, message
):
    with pytest.raises(ValueError, match=message):
        mod.handler(event(field_name, ["Admin"], arguments=arguments), None)


# --------------------------------------------------------------------------
# Annotator test-set scope, through the real idp_common.testset_scope
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_an_annotator_who_also_holds_reviewer_stays_test_set_scoped(mod, table):
    """The regression `_assert_annotator_scope` was written for.

    Exempting `Reviewer` alongside `Admin`/`Author` turned object scope off
    entirely for a caller holding both groups, so the library refused such a
    caller while this Lambda waved them through. Annotators are assigned by hand
    in Cognito, so holding both is an ordinary mistake.
    """
    put_doc(table, TestSetId="ts-other")
    scope_user(mod.dynamodb.Table(USERS_TABLE), "rev@example.com", ["ts-mine"])
    with pytest.raises(ValueError, match="not assigned to test set 'ts-other'"):
        mod.handler(event("completeSectionReview", ["Annotator", "Reviewer"]), None)


@pytest.mark.unit
@pytest.mark.parametrize("unscoped", ["Admin", "Author"])
def test_admin_and_author_are_the_only_groups_exempt_from_test_set_scope(
    mod, table, unscoped
):
    """Exempt means the document's test set is never looked up at all.

    The document belongs to `ts-other` and the caller is scoped to `ts-mine`, so a
    scope check that ran would refuse. Asserting only that nothing was raised would
    therefore be satisfied by the exemption *and* by a check that ran and reached
    the wrong verdict; the tracking-table read is what only the exemption prevents.
    """
    put_doc(table, TestSetId="ts-other")
    scope_user(mod.dynamodb.Table(USERS_TABLE), "rev@example.com", ["ts-mine"])
    with recorded(mod.dynamodb, "Table") as lookups:
        mod._assert_annotator_scope(
            event("completeSectionReview", ["Annotator", unscoped]), OBJECT_KEY
        )
    assert lookups == []


@pytest.mark.unit
def test_an_annotator_scoped_to_the_documents_test_set_is_allowed(mod, table):
    """Allowed because the scope check ran and passed, not because it was skipped.

    Asserted on the library call and the test set it was handed. A handler that had
    stopped consulting scope altogether would raise nothing here too, and that is
    the defect `_assert_annotator_scope` exists to prevent.
    """
    put_doc(table, TestSetId="ts-mine")
    scope_user(mod.dynamodb.Table(USERS_TABLE), "rev@example.com", ["ts-mine", "ts-2"])
    with recorded(testset_scope, "assert_can_access_test_set") as checks:
        mod._assert_annotator_scope(
            event("completeSectionReview", ["Annotator"]), OBJECT_KEY
        )
    assert len(checks) == 1
    assert checks[0][0][1] == "ts-mine"


@pytest.mark.unit
def test_an_annotator_may_not_review_a_production_document(mod, table):
    """A document with no `TestSetId` is production HITL work, not annotation."""
    put_doc(table)  # no TestSetId
    scope_user(mod.dynamodb.Table(USERS_TABLE), "rev@example.com", ["ts-mine"])
    with pytest.raises(ValueError, match="only review test-set documents"):
        mod._assert_annotator_scope(
            event("completeSectionReview", ["Annotator"]), OBJECT_KEY
        )


@pytest.mark.unit
def test_an_annotator_with_no_scope_at_all_is_denied_rather_than_unrestricted(
    mod, table
):
    """Fails closed: a half-created annotator must not inherit every test set."""
    put_doc(table, TestSetId="ts-mine")  # users table left empty
    with pytest.raises(ValueError, match="not assigned to any test set"):
        mod._assert_annotator_scope(
            event("completeSectionReview", ["Annotator"]), OBJECT_KEY
        )


# Scope on the *claim* and *release* routes, not only on the edit route. These
# exercise `handler` rather than `_assert_annotator_scope` directly, because what
# they pin is that the branch makes the call at all.


@pytest.mark.unit
@pytest.mark.parametrize("field_name", ["claimReview", "releaseReview"])
def test_claiming_and_releasing_are_test_set_scoped_for_an_annotator(
    mod, table, field_name
):
    """Both routes write to a document, so both have to be scoped.

    Claiming takes a document out of every other annotator's queue and releasing
    puts someone else's back into it. The caller here is assigned to `ts-mine` and
    the document belongs to `ts-other`, so the refusal has to come from the scope
    check — and the stored item is read back, because a check placed after the
    update would raise having already changed ownership.
    """
    put_doc(table, TestSetId="ts-other", HITLReviewOwner="")
    scope_user(mod.dynamodb.Table(USERS_TABLE), "rev@example.com", ["ts-mine"])
    document = FakeDocument([FakeSection("sec-1")])
    with patch.object(
        mod, "create_document_service", return_value=FakeDocumentService(document)
    ):
        with pytest.raises(ValueError, match="not assigned to test set 'ts-other'"):
            mod.handler(
                event(field_name, ["Annotator"], arguments={"objectKey": OBJECT_KEY}),
                None,
            )
    stored = read_doc(table)
    assert stored["HITLReviewOwner"] == ""
    assert "HITLStatus" not in stored


@pytest.mark.unit
@pytest.mark.parametrize("field_name", ["claimReview", "releaseReview"])
def test_an_annotator_can_neither_claim_nor_release_a_production_document(
    mod, table, field_name
):
    """A document carrying no `TestSetId` is production HITL work, on every route."""
    put_doc(table, HITLReviewOwner="")  # no TestSetId
    scope_user(mod.dynamodb.Table(USERS_TABLE), "rev@example.com", ["ts-mine"])
    document = FakeDocument([FakeSection("sec-1")])
    with patch.object(
        mod, "create_document_service", return_value=FakeDocumentService(document)
    ):
        with pytest.raises(ValueError, match="only review test-set documents"):
            mod.handler(
                event(field_name, ["Annotator"], arguments={"objectKey": OBJECT_KEY}),
                None,
            )
    assert read_doc(table)["HITLReviewOwner"] == ""


@pytest.mark.unit
def test_an_in_scope_annotator_may_claim_a_document_from_their_own_test_set(mod, table):
    """The other direction, so the two refusals above are not a blanket one.

    A scope check that refused every annotator would satisfy both of them and break
    the collaborative labelling queue outright.
    """
    put_doc(table, TestSetId="ts-mine", HITLReviewOwner="")
    scope_user(mod.dynamodb.Table(USERS_TABLE), "rev@example.com", ["ts-mine"])
    document = FakeDocument([FakeSection("sec-1")])
    with patch.object(
        mod, "create_document_service", return_value=FakeDocumentService(document)
    ):
        mod.handler(
            event("claimReview", ["Annotator"], arguments={"objectKey": OBJECT_KEY}),
            None,
        )
    stored = read_doc(table)
    assert stored["HITLReviewOwner"] == "rev"
    assert stored["HITLStatus"] == "InProgress"


def _handler_field_branches():
    """`fieldName` → the function names called in that branch of `handler`.

    Read from the source rather than exercised, because what this answers is a
    question about the shape of the dispatcher: which branches make the call.
    """
    tree = ast.parse((Path(MODULE_DIR) / "index.py").read_text())
    handler = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "handler"
    )
    branches = {}
    for node in handler.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "field_name"
            and isinstance(test.comparators[0], ast.Constant)
        ):
            branches[test.comparators[0].value] = {
                call.func.id
                for call in ast.walk(node)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            }
    return branches


@pytest.mark.unit
def test_the_set_of_handler_branches_that_assert_annotator_scope_is_pinned():
    """The defect class is one branch of the dispatcher missing the call.

    `claimReview` and `releaseReview` had no coverage of annotator scope at all:
    deleting `_assert_annotator_scope` from either left every behavioural test in
    this directory green, because nothing reached those two branches as an
    annotator. The three tests above close that, and this one closes the class — a
    fifth `fieldName` added later without the call fails here rather than shipping.

    `skipAllSectionsReview` is deliberately not in the set: `Annotator` is refused
    that operation outright a few lines earlier, so no scoped caller survives to be
    narrowed. `completeSectionReview` is the dispatcher's fall-through rather than a
    `field_name ==` branch, and is asserted separately below.
    """
    branches = _handler_field_branches()
    assert set(branches) == {"claimReview", "releaseReview", "skipAllSectionsReview"}
    scoped = {
        field for field, calls in branches.items() if "_assert_annotator_scope" in calls
    }
    assert scoped == {"claimReview", "releaseReview"}
    assert "skip_all_sections_review" in branches["skipAllSectionsReview"]


@pytest.mark.unit
def test_the_fall_through_route_to_completion_also_asserts_scope(mod, table):
    """`completeSectionReview` reaches the operation without naming a branch.

    Any unrecognised `fieldName` lands here too, so this is the widest route into a
    document and the one a new operation gets for free.
    """
    put_doc(table, TestSetId="ts-other")
    scope_user(mod.dynamodb.Table(USERS_TABLE), "rev@example.com", ["ts-mine"])
    with patch.object(mod, "complete_section_review") as completion:
        with pytest.raises(ValueError, match="not assigned to test set 'ts-other'"):
            mod.handler(event("completeSectionReview", ["Annotator"]), None)
    completion.assert_not_called()


# --------------------------------------------------------------------------
# complete_section_review: the pending/completed/skipped state machine
# --------------------------------------------------------------------------


def run_complete(mod, document, **kwargs):
    service = FakeDocumentService(document)
    with (
        patch.object(mod, "create_document_service", return_value=service),
        patch.object(mod, "trigger_reprocessing") as reprocess,
    ):
        result = mod.complete_section_review(OBJECT_KEY, **kwargs)
    return service, reprocess, result


@pytest.mark.unit
def test_finishing_the_last_pending_section_completes_the_document_and_reprocesses(
    mod, table
):
    """The transition that releases a document from the review queue.

    `HITLPendingReview` must be *removed*, not set falsy: the UI's queue is driven
    by that attribute's presence, so a document left holding it is stranded in the
    queue with nothing left to review.
    """
    put_doc(table, HITLPendingReview="true")
    document = FakeDocument(
        [FakeSection("sec-1", "s3://out/1.json"), FakeSection("sec-2")],
        pending=["sec-2"],
        completed=["sec-1"],
    )
    service, reprocess, _ = run_complete(mod, document, section_id="sec-2")

    assert document.hitl_status == "Completed"
    assert document.hitl_sections_pending == []
    assert sorted(document.hitl_sections_completed) == ["sec-1", "sec-2"]
    assert service.updated == [document]
    stored = read_doc(table)
    assert stored["HITLCompleted"] is True
    assert "HITLPendingReview" not in stored
    reprocess.assert_called_once_with(OBJECT_KEY)


@pytest.mark.unit
def test_a_document_with_a_skipped_section_finishes_as_skipped_not_completed(
    mod, table
):
    """`Skipped` and `Completed` are different verdicts about the same document.

    A set whose documents all report `Completed` looks fully labelled; one
    reporting `Skipped` says some sections were never read. Collapsing the two
    would silently promote unreviewed extractions to ground truth.
    """
    put_doc(table, HITLSectionsSkipped=["sec-3"])
    document = FakeDocument(
        [FakeSection("sec-1", "s3://out/1.json"), FakeSection("sec-3")],
        pending=["sec-1"],
    )
    _, _, _ = run_complete(mod, document, section_id="sec-1")
    assert document.hitl_status == "Skipped"


@pytest.mark.unit
def test_pending_is_seeded_from_the_remaining_sections_on_a_first_review(mod, table):
    """With no review state stored yet, pending must exclude the section just done.

    Seeding it from *all* section ids instead would leave this section pending
    forever, so the document could never reach `Completed` no matter how many times
    it was reviewed.
    """
    put_doc(table)
    document = FakeDocument(
        [
            FakeSection("sec-1", "s3://out/1.json"),
            FakeSection("sec-2"),
            FakeSection("sec-3"),
            FakeSection(None),  # sections without an id are not review units
        ]
    )
    run_complete(mod, document, section_id="sec-1")
    assert sorted(document.hitl_sections_pending) == ["sec-2", "sec-3"]
    assert document.hitl_sections_completed == ["sec-1"]
    assert document.hitl_status == "InProgress"


@pytest.mark.unit
def test_reviewing_a_section_twice_does_not_resurrect_it_as_pending(mod, table):
    """Completed is a set, so a double submit is idempotent rather than additive."""
    put_doc(table)
    document = FakeDocument(
        [FakeSection("sec-1", "s3://out/1.json")], pending=[], completed=["sec-1"]
    )
    run_complete(mod, document, section_id="sec-1")
    assert document.hitl_sections_completed == ["sec-1"]
    assert document.hitl_status == "Completed"


@pytest.mark.unit
def test_the_review_history_appends_and_names_the_reviewer(mod, table):
    """The audit trail is the only record of who asserted a label was right."""
    put_doc(
        table,
        HITLReviewHistory=[{"sectionId": "sec-0", "reviewedBy": "earlier"}],
        HITLSectionsSkipped=[],
    )
    document = FakeDocument(
        [FakeSection("sec-1", "s3://out/1.json"), FakeSection("sec-2")],
        pending=["sec-1", "sec-2"],
    )
    run_complete(
        mod,
        document,
        section_id="sec-1",
        username="alice",
        user_email="alice@example.com",
    )
    history = read_doc(table)["HITLReviewHistory"]
    assert [h["reviewedBy"] for h in history] == ["earlier", "alice"]
    assert history[-1]["sectionId"] == "sec-1"
    assert history[-1]["reviewedByEmail"] == "alice@example.com"
    assert history[-1]["reviewedAt"].startswith("20")


@pytest.mark.unit
def test_an_anonymous_review_is_recorded_as_unknown_not_as_an_empty_string(mod, table):
    """An empty `reviewedBy` renders as a blank cell; `unknown` is legible."""
    put_doc(table)
    document = FakeDocument([FakeSection("sec-1", "s3://out/1.json")])
    run_complete(mod, document, section_id="sec-1", username="", user_email="")
    entry = read_doc(table)["HITLReviewHistory"][0]
    assert entry["reviewedBy"] == "unknown"
    assert entry["reviewedByEmail"] == ""


@pytest.mark.unit
def test_an_incomplete_document_does_not_gain_the_completed_flag(mod, table):
    """`HITLCompleted` is what downstream reads to decide the review is finished."""
    put_doc(table, HITLPendingReview="true")
    document = FakeDocument(
        [FakeSection("sec-1", "s3://out/1.json"), FakeSection("sec-2")],
        pending=["sec-1", "sec-2"],
    )
    _, reprocess, _ = run_complete(mod, document, section_id="sec-1")
    stored = read_doc(table)
    assert "HITLCompleted" not in stored
    assert stored["HITLPendingReview"] == "true"
    reprocess.assert_not_called()


@pytest.mark.unit
def test_reviewing_a_document_the_loader_cannot_find_raises(mod, table):
    service = FakeDocumentService(None)
    with patch.object(mod, "create_document_service", return_value=service):
        with pytest.raises(ValueError, match="not found"):
            mod.complete_section_review(OBJECT_KEY, "sec-1")


# --------------------------------------------------------------------------
# Persisting the reviewer's edits
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_edited_data_is_written_to_the_section_output_uri_unchanged(mod, table):
    """The UI sends the whole result document, including its confidence payload.

    Any transformation here drops the fields the ground-truth viewer and the
    confidence curve read, so the write is asserted to round-trip verbatim.
    """
    put_doc(table)
    edited = {
        "inference_result": {"total": "42.00", "lines": [{"amount": "1.00"}]},
        "explainability_info": {"total": {"confidence": 0.5}},
        "metadata": {"config_version": "profileA"},
    }
    document = FakeDocument(
        [FakeSection("sec-1", f"s3://{OUTPUT_BUCKET}/sections/sec-1/result.json")]
    )
    run_complete(mod, document, section_id="sec-1", edited_data=json.dumps(edited))
    assert s3_json(mod, OUTPUT_BUCKET, "sections/sec-1/result.json") == edited


@pytest.mark.unit
def test_a_dict_payload_is_accepted_as_well_as_a_json_string(mod):
    mod.save_edited_data_to_s3(
        f"s3://{OUTPUT_BUCKET}/dict.json", {"inference_result": {"a": 1}}
    )
    assert s3_json(mod, OUTPUT_BUCKET, "dict.json") == {"inference_result": {"a": 1}}


@pytest.mark.unit
@pytest.mark.parametrize(
    "uri", ["http://example.com/x.json", "s3://bucket-with-no-key"]
)
def test_an_unusable_output_uri_writes_nothing_and_does_not_raise(mod, uri):
    """Logged and skipped, so one malformed URI cannot wedge the whole review."""
    mod.save_edited_data_to_s3(uri, {"a": 1})
    assert "Contents" not in mod.s3_client.list_objects_v2(Bucket=OUTPUT_BUCKET)


@pytest.mark.unit
def test_a_failed_edit_write_aborts_the_review_instead_of_reporting_success(mod, table):
    """The loudest failure mode this function has to avoid.

    The UI clears its unsaved-changes state on a successful response, so a review
    that reports success with the edits unwritten destroys the reviewer's work
    with no trace. The write must therefore propagate and the document must stay
    unreviewed.
    """
    put_doc(table, HITLPendingReview="true")
    document = FakeDocument(
        [FakeSection("sec-1", "s3://no-such-bucket-anywhere/result.json")]
    )
    service = FakeDocumentService(document)
    with patch.object(mod, "create_document_service", return_value=service):
        with pytest.raises(Exception):  # noqa: B017 - botocore ClientError subclass
            mod.complete_section_review(OBJECT_KEY, "sec-1", edited_data={"a": 1})

    stored = read_doc(table)
    assert stored["HITLPendingReview"] == "true"
    assert "HITLCompleted" not in stored
    assert "HITLReviewHistory" not in stored
    assert service.updated == []


@pytest.mark.unit
def test_edits_for_a_section_the_document_does_not_have_are_refused(mod, table):
    """The matched text has to name *this* guard, not the pair it belongs to.

    Both guards open "Cannot save edited data: section '<id>'", and with the
    not-found one deleted the section's output URI is `None`, so the second raises
    for the same input. A match on the shared prefix therefore passes with the
    guard this test is named for removed; the clause after the section id is what
    separates them.
    """
    put_doc(table)
    document = FakeDocument([FakeSection("sec-2", "s3://out/2.json")])
    service = FakeDocumentService(document)
    with patch.object(mod, "create_document_service", return_value=service):
        with pytest.raises(ValueError, match="section 'sec-1' not found in document"):
            mod.complete_section_review(OBJECT_KEY, "sec-1", edited_data={"a": 1})
    assert service.updated == []


# --------------------------------------------------------------------------
# The test-set baseline: a review as reusable ground truth
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_correction_lands_on_the_baseline_key_derived_from_set_and_filename(
    mod, table
):
    """`{set}/baseline/{filename}/sections/{section}/result.json`.

    The object key is `{test_run_id}/{filename}` but the baseline is keyed by
    filename alone, so a correction made on any run updates the one label. A key
    built from the full object key would scatter a set's ground truth across one
    directory per run and the next labeling pass would read none of it.
    """
    put_doc(table, TestSetId="ts-1")
    mod.write_correction_to_test_set_baseline(
        OBJECT_KEY, "sec-1", {"inference_result": {"total": "9"}}, "alice", "a@e.com"
    )
    saved = s3_json(mod, TEST_SET_BUCKET, baseline_key("ts-1", "invoice.pdf", "sec-1"))
    assert saved["inference_result"] == {"total": "9"}
    assert saved["labelSource"] == "reviewed-human"


@pytest.mark.unit
def test_an_object_key_with_no_run_prefix_uses_the_whole_key_as_the_filename(
    mod, table
):
    table.put_item(Item={"PK": "doc#solo.pdf", "SK": "none", "TestSetId": "ts-1"})
    mod.write_correction_to_test_set_baseline("solo.pdf", "sec-1", {"x": 1})
    assert (
        s3_json(mod, TEST_SET_BUCKET, baseline_key("ts-1", "solo.pdf", "sec-1"))["x"]
        == 1
    )


@pytest.mark.unit
def test_a_document_outside_a_test_set_writes_no_baseline(mod, table):
    put_doc(table)  # no TestSetId
    mod.write_correction_to_test_set_baseline(OBJECT_KEY, "sec-1", {"x": 1})
    assert "Contents" not in mod.s3_client.list_objects_v2(Bucket=TEST_SET_BUCKET)


@pytest.mark.unit
def test_no_baseline_is_written_when_the_test_set_bucket_is_not_configured(mod, table):
    """A deployment without the test-set feature must not fail every review.

    Asserted on the tracking-table read never being made. An empty bucket does not
    distinguish the guard from its absence: without it the function looks the
    document's test set up, builds a key and calls `put_object` with an empty bucket
    name, which raises into the blanket `except` — leaving the bucket exactly as
    empty and nothing raised.
    """
    put_doc(table, TestSetId="ts-1")
    mod.TEST_SET_BUCKET = ""
    with recorded(mod.dynamodb, "Table") as lookups:
        mod.write_correction_to_test_set_baseline(OBJECT_KEY, "sec-1", {"x": 1})
    assert lookups == []
    assert "Contents" not in mod.s3_client.list_objects_v2(Bucket=TEST_SET_BUCKET)


@pytest.mark.unit
def test_a_baseline_write_failure_does_not_fail_the_review(mod, table):
    """Opposite contract to the section-output write.

    The baseline is a derived artifact; losing it costs a future labeling run some
    signal. Failing the review would cost the reviewer their work, so this path
    swallows and logs.
    """
    put_doc(table, TestSetId="ts-1")
    document = FakeDocument(
        [FakeSection("sec-1", f"s3://{OUTPUT_BUCKET}/sections/sec-1/result.json")]
    )
    with patch.object(
        mod.s3_client,
        "put_object",
        side_effect=[None, RuntimeError("baseline bucket denied")],
    ):
        _, _, _ = run_complete(
            mod, document, section_id="sec-1", edited_data={"inference_result": {}}
        )
    assert document.hitl_status == "Completed"
    assert read_doc(table)["HITLCompleted"] is True


# --------------------------------------------------------------------------
# Edit history inside the label
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_stored_edit_history_survives_a_client_that_drops_it(mod, table):
    """The trail is server-owned.

    The UI does not round-trip `_editHistory`, so seeding from the client's copy
    would erase every prior entry on each save and the provenance of a
    repeatedly-corrected label would show only the last reviewer.
    """
    put_doc(table, TestSetId="ts-1")
    key = baseline_key("ts-1", "invoice.pdf", "sec-1")
    mod.s3_client.put_object(
        Bucket=TEST_SET_BUCKET,
        Key=key,
        Body=json.dumps(
            {
                "inference_result": {"total": "1"},
                "_editHistory": [{"editedBy": "first"}, {"editedBy": "second"}],
            }
        ),
    )
    mod.write_correction_to_test_set_baseline(
        OBJECT_KEY, "sec-1", {"inference_result": {"total": "2"}}, "third"
    )
    history = s3_json(mod, TEST_SET_BUCKET, key)["_editHistory"]
    assert [e.get("editedBy") for e in history] == ["first", "second", "third"]


@pytest.mark.unit
def test_a_client_holding_more_history_than_the_server_is_preferred(mod, table):
    """Taking the longer of the two stops a client truncating the trail."""
    put_doc(table, TestSetId="ts-1")
    key = baseline_key("ts-1", "invoice.pdf", "sec-1")
    mod.s3_client.put_object(
        Bucket=TEST_SET_BUCKET,
        Key=key,
        Body=json.dumps({"_editHistory": [{"editedBy": "stored-only"}]}),
    )
    mod.write_correction_to_test_set_baseline(
        OBJECT_KEY,
        "sec-1",
        {"_editHistory": [{"editedBy": "a"}, {"editedBy": "b"}]},
        "c",
    )
    history = s3_json(mod, TEST_SET_BUCKET, key)["_editHistory"]
    assert [e.get("editedBy") for e in history] == ["a", "b", "c"]


@pytest.mark.unit
def test_a_capped_edit_history_drops_the_oldest_entries_and_keeps_this_review(
    mod, table
):
    """Keeping the head instead of the tail would silently stop recording reviews."""
    put_doc(table, TestSetId="ts-1")
    key = baseline_key("ts-1", "invoice.pdf", "sec-1")
    stored = [{"editedBy": f"r{i}"} for i in range(mod.MAX_EDIT_HISTORY_ENTRIES)]
    mod.s3_client.put_object(
        Bucket=TEST_SET_BUCKET, Key=key, Body=json.dumps({"_editHistory": stored})
    )
    mod.write_correction_to_test_set_baseline(OBJECT_KEY, "sec-1", {}, "newest")

    history = s3_json(mod, TEST_SET_BUCKET, key)["_editHistory"]
    assert len(history) == mod.MAX_EDIT_HISTORY_ENTRIES
    assert history[-1]["editedBy"] == "newest"
    assert history[0]["editedBy"] == "r1"  # r0 dropped


@pytest.mark.unit
def test_the_edit_history_entry_names_the_changed_field_paths_and_both_values(mod):
    """The diff is what a later auditor reads to see whether the model was right.

    Flattened with the same helper the confidence curve uses, so "changed" means
    the same thing to the audit trail and to the calibration signal. Nested and
    list-indexed paths are included because a corrected table row is the common
    case.
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
    mod.append_edit_history(previous, saved, "alice", "alice@example.com")

    entry = saved["_editHistory"][-1]
    assert entry["editedBy"] == "alice"
    assert entry["editedByEmail"] == "alice@example.com"
    assert entry["source"] == "annotation-review"
    edits = entry["baselineEdits"]
    assert sorted(edits["changedFields"]) == ["lines[1].amount", "total"]
    assert edits["changeCount"] == 2
    assert edits["diffs"]["total"] == {"originalValue": "100.00", "newValue": "105.00"}
    assert edits["diffs"]["lines[1].amount"] == {
        "originalValue": "2.00",
        "newValue": "9.99",
    }


@pytest.mark.unit
def test_a_review_that_changed_nothing_records_no_baseline_edits(mod):
    """`baselineEdits` absent is how a confirmation is distinguished from a fix."""
    label = {"inference_result": {"total": "1"}}
    saved = {"inference_result": {"total": "1"}}
    mod.append_edit_history(label, saved, "alice", "")
    assert "baselineEdits" not in saved["_editHistory"][-1]


@pytest.mark.unit
def test_a_diff_that_cannot_be_computed_still_records_who_reviewed(mod):
    """Provenance must not fail the save when the flattener cannot be used.

    The diff is taken with a helper from the layer-delivered `idp_common`, so the
    two can be at different versions in a partially-updated deployment. Losing the
    field-level detail is acceptable; losing the reviewer's edit is not, so the
    entry is still written without `baselineEdits`.

    The warning is asserted here, which is the other half of the pair described in
    `test_a_first_review_with_no_prior_label_records_no_diff`: this input is the one
    that must produce the line, that one the one that must not.
    """
    saved = {"inference_result": {"total": "2"}}
    with (
        log_spy(mod) as log,
        patch.object(
            curve_store, "flatten_values", side_effect=TypeError("incompatible layer")
        ),
    ):
        mod.append_edit_history(
            {"inference_result": {"total": "1"}}, saved, "alice", ""
        )
    assert "Could not diff review changes" in log.text
    assert "incompatible layer" in log.text
    assert "baselineEdits" not in saved["_editHistory"][-1]
    assert saved["_editHistory"][-1]["editedBy"] == "alice"


@pytest.mark.unit
def test_a_first_review_with_no_prior_label_records_no_diff(mod):
    """Nothing to diff against, and that is recognised rather than crashed into.

    An absent `baselineEdits` does not distinguish the guard from its removal:
    without it, `previous.get(...)` on `None` raises `AttributeError` into the diff
    helper's own `except`, which returns the same empty diff. Asserting that
    `flatten_values` was not called does not distinguish them either — the attribute
    access raises one expression *before* the call, so it is unmade in both cases.

    What differs is the warning: the `except` logs "Could not diff review changes",
    and the guard does not. The pair of tests holds that line in both directions —
    absent here, present in `test_a_diff_that_cannot_be_computed_still_records_who
    _reviewed` — so neither passes on a change that stops emitting it.
    """
    saved = {"inference_result": {"total": "1"}}
    with log_spy(mod) as log:
        mod.append_edit_history(None, saved, "alice", "")
    assert "Could not diff review changes" not in log.text
    assert "baselineEdits" not in saved["_editHistory"][-1]
    assert len(saved["_editHistory"]) == 1


# --------------------------------------------------------------------------
# Confirming an unchanged review
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_confirming_unchanged_labels_tags_the_baseline_human_reviewed(mod, table):
    """ "The labels are correct" is a verdict, not the absence of one.

    Without this, a confirmed baseline kept its `draft-machine` tag: the test set
    showed "Awaiting review" after every document had been confirmed, a later
    labeling run was free to overwrite the confirmation, and the confidence curve
    never learned that those high-confidence fields were right.
    """
    put_doc(table, TestSetId="ts-1")
    key = baseline_key("ts-1", "invoice.pdf", "sec-1")
    mod.s3_client.put_object(
        Bucket=TEST_SET_BUCKET,
        Key=key,
        Body=json.dumps(
            {"inference_result": {"total": "1"}, "labelSource": "draft-machine"}
        ),
    )
    document = FakeDocument([FakeSection("sec-1", "s3://out/1.json")])
    run_complete(mod, document, section_id="sec-1", username="alice", edited_data=None)

    saved = s3_json(mod, TEST_SET_BUCKET, key)
    assert saved["labelSource"] == "reviewed-human"
    assert saved["inference_result"] == {"total": "1"}  # values untouched
    assert saved["_editHistory"][-1]["editedBy"] == "alice"


@pytest.mark.unit
def test_confirming_an_already_confirmed_baseline_adds_no_second_entry(mod, table):
    """Re-opening a finished document must not inflate its revision history."""
    put_doc(table, TestSetId="ts-1")
    key = baseline_key("ts-1", "invoice.pdf", "sec-1")
    mod.s3_client.put_object(
        Bucket=TEST_SET_BUCKET,
        Key=key,
        Body=json.dumps(
            {
                "labelSource": "reviewed-human",
                "_editHistory": [{"editedBy": "alice"}],
            }
        ),
    )
    mod.confirm_test_set_baseline_reviewed(OBJECT_KEY, "sec-1", "bob")
    assert s3_json(mod, TEST_SET_BUCKET, key)["_editHistory"] == [{"editedBy": "alice"}]


@pytest.mark.unit
def test_confirming_a_section_with_no_baseline_yet_writes_nothing(mod, table):
    """Nothing has been drafted, so there is no label to assert is correct.

    The logged line is the assertion. Without the `isinstance(existing, dict)`
    guard, the `None` the read returned is asked for `labelSource`, which raises
    into the blanket `except` and logs a *failure* instead — same empty bucket,
    nothing raised, different branch.
    """
    put_doc(table, TestSetId="ts-1")
    with log_spy(mod) as log:
        mod.confirm_test_set_baseline_reviewed(OBJECT_KEY, "sec-1", "alice")
    assert "No baseline to confirm" in log.text
    assert "Failed to confirm" not in log.text
    assert "Contents" not in mod.s3_client.list_objects_v2(Bucket=TEST_SET_BUCKET)


@pytest.mark.unit
def test_a_tracking_table_failure_during_confirmation_does_not_fail_the_review(mod):
    """The confirmation path reads DynamoDB to find the owning test set.

    A throttle or a missing table there must be logged and dropped, not raised: the
    section's own output has already been accepted at this point. Asserted on the
    logged failure, which is what shows the injected error was actually raised and
    caught rather than the function having found nothing to do before reaching it.
    """
    with log_spy(mod) as log:
        with patch.object(
            mod.dynamodb, "Table", side_effect=RuntimeError("table unavailable")
        ):
            mod.confirm_test_set_baseline_reviewed(OBJECT_KEY, "sec-1", "alice")
    assert "Failed to confirm test-set baseline" in log.text
    assert "table unavailable" in log.text


@pytest.mark.unit
def test_confirmation_stops_at_a_document_that_belongs_to_no_test_set(mod, table):
    """There is no baseline to confirm, so the S3 read is never attempted.

    Asserted on `_read_json` not being called. Without the `TestSetId` guard the
    function reads a key built from `None`, gets nothing back and returns on the
    next guard — the same empty bucket and the same absence of an exception.
    """
    put_doc(table)  # no TestSetId
    with recorded(mod, "_read_json") as reads:
        mod.confirm_test_set_baseline_reviewed(OBJECT_KEY, "sec-1", "alice")
    assert reads == []
    assert "Contents" not in mod.s3_client.list_objects_v2(Bucket=TEST_SET_BUCKET)


@pytest.mark.unit
def test_confirmation_is_skipped_when_no_test_set_bucket_is_configured(mod, table):
    """Same shape as the correction path: the tracking-table read is never made."""
    put_doc(table, TestSetId="ts-1")
    mod.TEST_SET_BUCKET = ""
    with recorded(mod.dynamodb, "Table") as lookups:
        mod.confirm_test_set_baseline_reviewed(OBJECT_KEY, "sec-1", "alice")
    assert lookups == []
    assert "Contents" not in mod.s3_client.list_objects_v2(Bucket=TEST_SET_BUCKET)


# --------------------------------------------------------------------------
# Confidence-curve observations, through the real CurveStore
# --------------------------------------------------------------------------


CURVE_PREVIOUS = {
    "inference_result": {"total": "100.00", "date": "2026-01-01"},
    "explainability_info": {
        "total": {"confidence": 0.95},
        "date": {"confidence": 0.30},
    },
    "metadata": {"config_version": "profileA", "confidence_fingerprint": "fp1"},
}
CURVE_SAVED = {"inference_result": {"total": "100.00", "date": "2026-02-02"}}


def curve_item(table, test_set_id, sk):
    return table.get_item(
        Key={"PK": curve_store.test_set_pk(test_set_id), "SK": sk}
    ).get("Item")


@pytest.mark.unit
def test_a_review_records_which_confidence_was_earned_and_which_was_not(mod, table):
    """The calibration signal, asserted per bin rather than "the store was called".

    A field the reviewer left alone was predicted correctly; one they changed was
    not. Here `total` was claimed at 0.95 and kept (bin 9, correct) and `date` at
    0.30 and changed (bin 2, incorrect). Inverting that sense would teach the
    review-effort estimator to trust exactly the predictions a human rejected.
    """
    mod.record_curve_observations("ts-1", CURVE_PREVIOUS, CURVE_SAVED)

    item = curve_item(table, "ts-1", "curve#_aggregate")
    assert item is not None
    assert item["total9"] == Decimal("1") and item["correct9"] == Decimal("1")
    assert item["total2"] == Decimal("1") and item["correct2"] == Decimal("0")
    assert item["reviewObservations"] == Decimal("2")
    assert item["ItemType"] == "confidence_curve"


@pytest.mark.unit
def test_observations_are_keyed_by_the_config_and_fingerprint_that_drafted_the_label(
    mod, table
):
    """Keyed from the *replaced* label's metadata, not the saved one.

    A curve measured under one extraction model must not be inherited by another,
    so the revision fingerprint on the drafted label decides the sort key. Reading
    the config version from the reviewer's payload instead — which carries no
    metadata — would pool every revision into one curve and make a model swap
    invisible to the estimator.
    """
    mod.record_curve_observations("ts-1", CURVE_PREVIOUS, CURVE_SAVED)

    assert curve_item(table, "ts-1", "curve#profileA@fp1") is not None
    assert curve_item(table, "ts-1", "curve#profileA") is not None
    assert curve_item(table, "ts-1", "curve#_aggregate") is not None
    prior = table.get_item(
        Key={"PK": curve_store.GLOBAL_PRIOR_PK, "SK": "curve#_aggregate"}
    ).get("Item")
    assert prior is not None, "a new test set should inherit the global prior"


@pytest.mark.unit
def test_a_label_without_a_fingerprint_writes_no_revision_scoped_curve(mod, table):
    previous = dict(CURVE_PREVIOUS, metadata={"config_version": "profileA"})
    mod.record_curve_observations("ts-1", previous, CURVE_SAVED)
    assert curve_item(table, "ts-1", "curve#profileA") is not None
    assert curve_item(table, "ts-1", "curve#profileA@fp1") is None


@pytest.mark.unit
def test_no_curve_is_recorded_when_there_was_no_prior_prediction(mod, table):
    """With nothing predicted there is no verdict for the reviewer to deliver.

    Asserted on the observation builder not being reached. Without the guard it is
    called with `None`, raises into the blanket `except`, and leaves exactly the
    same absent curve item.
    """
    with recorded(curve_store, "observations_from_baseline_review") as built:
        mod.record_curve_observations("ts-1", None, CURVE_SAVED)
    assert built == []
    assert curve_item(table, "ts-1", "curve#_aggregate") is None


@pytest.mark.unit
def test_a_label_with_no_recorded_confidences_records_nothing(mod, table):
    """No claimed confidence means no `(confidence, correct)` pair to learn from.

    Asserted on the store not being written to. `add_observations` with an empty
    list writes nothing either, so an absent curve item cannot tell the guard from
    the call it prevents.
    """
    previous = {"inference_result": {"total": "1"}, "metadata": {}}
    with recorded(curve_store.CurveStore, "add_observations") as added:
        mod.record_curve_observations(
            "ts-1", previous, {"inference_result": {"total": "2"}}
        )
    assert added == []
    assert curve_item(table, "ts-1", "curve#_aggregate") is None


@pytest.mark.unit
def test_a_curve_write_failure_does_not_fail_the_review(mod, table):
    """The curve is an optimization; a reviewer's save outranks it.

    Asserted on *which* handler swallowed it. A finished review does not
    distinguish the two: had this function re-raised, the baseline writer's own
    blanket `except` one frame up would have caught it and the review would have
    completed just the same — with the correction reported as failed rather than the
    curve. Both log lines are therefore checked, one present and one absent.
    """
    put_doc(table, TestSetId="ts-1")
    key = baseline_key("ts-1", "invoice.pdf", "sec-1")
    mod.s3_client.put_object(
        Bucket=TEST_SET_BUCKET, Key=key, Body=json.dumps(CURVE_PREVIOUS)
    )
    document = FakeDocument(
        [FakeSection("sec-1", f"s3://{OUTPUT_BUCKET}/sections/sec-1/result.json")]
    )
    with (
        log_spy(mod) as log,
        patch.object(
            curve_store.CurveStore,
            "add_observations",
            side_effect=RuntimeError("no table"),
        ),
    ):
        run_complete(mod, document, section_id="sec-1", edited_data=CURVE_SAVED)
    assert read_doc(table)["HITLCompleted"] is True
    assert "Could not record confidence-curve observations" in log.text
    assert "Failed to write correction to test-set baseline" not in log.text


# --------------------------------------------------------------------------
# claimReview: exclusive ownership
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_claiming_an_unowned_document_records_the_owner_and_starts_the_review(
    mod, table
):
    put_doc(table, HITLStatus="Review Pending")
    document = FakeDocument([FakeSection("sec-1")])
    with patch.object(
        mod, "create_document_service", return_value=FakeDocumentService(document)
    ):
        mod.claim_review(OBJECT_KEY, "alice", "alice@example.com")
    stored = read_doc(table)
    assert stored["HITLReviewOwner"] == "alice"
    assert stored["HITLReviewOwnerEmail"] == "alice@example.com"
    assert stored["HITLStatus"] == "InProgress"


@pytest.mark.unit
def test_a_document_already_owned_by_someone_else_cannot_be_claimed(mod, table):
    put_doc(table, HITLReviewOwner="bob")
    document = FakeDocument([FakeSection("sec-1")])
    with patch.object(
        mod, "create_document_service", return_value=FakeDocumentService(document)
    ):
        with pytest.raises(ValueError, match="already claimed by bob"):
            mod.claim_review(OBJECT_KEY, "alice")
    assert read_doc(table)["HITLReviewOwner"] == "bob"


@pytest.mark.unit
def test_reclaiming_your_own_document_is_allowed(mod, table):
    """A reviewer returning to a document they own must not be locked out."""
    put_doc(table, HITLReviewOwner="alice", HITLStatus="Review Pending")
    document = FakeDocument([FakeSection("sec-1")])
    with patch.object(
        mod, "create_document_service", return_value=FakeDocumentService(document)
    ):
        mod.claim_review(OBJECT_KEY, "alice")
    assert read_doc(table)["HITLStatus"] == "InProgress"


@pytest.mark.unit
def test_an_empty_owner_attribute_does_not_block_a_claim(mod, table):
    """A released document stores `""` rather than dropping the attribute."""
    put_doc(table, HITLReviewOwner="")
    document = FakeDocument([FakeSection("sec-1")])
    with patch.object(
        mod, "create_document_service", return_value=FakeDocumentService(document)
    ):
        mod.claim_review(OBJECT_KEY, "alice")
    assert read_doc(table)["HITLReviewOwner"] == "alice"


@pytest.mark.unit
def test_losing_the_claim_race_names_the_winner_in_the_phrase_the_ui_matches(
    mod, table
):
    """The `ConditionExpression` is the exclusion, and it phrases the refusal.

    Two annotators clicking Claim in the same moment both read an unowned document,
    so nothing read *before* the write can exclude either of them — and a read
    before the write cannot refuse anything either, because it answers from an
    eventually consistent view in which a document released a moment ago still
    looks owned. The condition on the update is therefore the only ownership test,
    and the loser's message comes from its recovery path. That message must name
    the actual winner and keep the "already claimed" phrasing, because the UI
    matches on it to skip ahead to the next document rather than dead-ending on an
    error toast.

    The stored owner is the whole staging: `alice`'s conditional update arrives at
    an item `bob` already owns, which is exactly what a loser's write meets. Three
    assertions, each answering to a different mutation — that the claim is refused
    at all (weaken the `ConditionExpression` and alice's write lands), that the
    refusal came *through* the `ConditionalCheckFailedException` handler rather
    than from anywhere else (that handler logs a line nothing else in this function
    logs), and that the loser left the winner's ownership untouched.
    """
    put_doc(table, HITLReviewOwner="bob", HITLStatus="InProgress")
    document = FakeDocument([FakeSection("sec-1")])
    with (
        patch.object(
            mod, "create_document_service", return_value=FakeDocumentService(document)
        ),
        log_spy(mod) as log,
    ):
        with pytest.raises(ValueError, match="already claimed by bob"):
            mod.claim_review(OBJECT_KEY, "alice", "alice@example.com")

    assert "Claim race lost" in log.text, (
        "the refusal must be the condition's: any other route would mean something "
        "other than the atomic write decided ownership"
    )
    stored = read_doc(table)
    assert stored["HITLReviewOwner"] == "bob", "the loser must not overwrite"
    assert "HITLReviewOwnerEmail" not in stored, "no part of the loser's write landed"


@pytest.mark.unit
def test_a_document_claimed_between_entry_and_the_write_is_refused(mod, table):
    """The race staged as a transition, which is the case only the condition covers.

    The test above enters on an already-owned document, so *anything* that looks at
    ownership refuses it — including a read-then-check, which is what this function
    used to hold and what must not come back. Here the document is unowned when
    `claim_review` is entered and `bob` wins in the window before alice's update is
    sent, so a check that reads before writing passes and lets alice overwrite him.
    Only a test that does the same thing DynamoDB does, evaluating the condition
    against the item as it stands at the write, can tell the two apart.

    Nothing is blinded and no read is counted: `bob`'s claim is planted through the
    same table the resolver is using, immediately before the guarded write, so the
    item genuinely changes under the caller.
    """
    put_doc(table, HITLStatus="Review Pending")
    real_table = mod.dynamodb.Table(TRACKING_TABLE)

    class BobWinsJustBeforeTheWrite:
        """The real table, with bob's claim landing ahead of each update."""

        def __getattr__(self, name):
            return getattr(real_table, name)

        def update_item(self, **kwargs):
            real_table.update_item(
                Key={"PK": DOC_PK, "SK": "none"},
                UpdateExpression="SET HITLReviewOwner = :o",
                ExpressionAttributeValues={":o": "bob"},
            )
            return real_table.update_item(**kwargs)

    document = FakeDocument([FakeSection("sec-1")])
    with (
        patch.object(
            mod, "create_document_service", return_value=FakeDocumentService(document)
        ),
        patch.object(mod.dynamodb, "Table", return_value=BobWinsJustBeforeTheWrite()),
        log_spy(mod) as log,
    ):
        with pytest.raises(ValueError, match="already claimed by bob"):
            mod.claim_review(OBJECT_KEY, "alice", "alice@example.com")

    assert "Claim race lost" in log.text
    stored = read_doc(table)
    assert stored["HITLReviewOwner"] == "bob", "the winner must keep the document"
    assert "HITLReviewOwnerEmail" not in stored, "no part of the loser's write landed"


@pytest.mark.unit
def test_claiming_a_missing_document_raises(mod, table):
    with patch.object(
        mod, "create_document_service", return_value=FakeDocumentService(None)
    ):
        with pytest.raises(ValueError, match="not found"):
            mod.claim_review(OBJECT_KEY, "alice")


# --------------------------------------------------------------------------
# releaseReview
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_releasing_a_review_returns_the_document_to_the_queue(mod, table):
    """`HITLReviewOwner` must be removed and `HITLPendingReview` set.

    Leaving the owner attribute behind would keep the document invisible to every
    other reviewer while nobody is working on it.
    """
    put_doc(
        table,
        HITLReviewOwner="alice",
        HITLReviewOwnerEmail="alice@example.com",
        HITLStatus="InProgress",
    )
    document = FakeDocument([FakeSection("sec-1")])
    with patch.object(
        mod, "create_document_service", return_value=FakeDocumentService(document)
    ):
        mod.release_review(OBJECT_KEY, "alice")
    stored = read_doc(table)
    assert "HITLReviewOwner" not in stored
    assert "HITLReviewOwnerEmail" not in stored
    assert stored["HITLStatus"] == "Review Pending"
    assert stored["HITLPendingReview"] == "true"


@pytest.mark.unit
def test_a_third_party_cannot_release_someone_elses_review(mod, table):
    put_doc(table, HITLReviewOwner="bob", HITLStatus="InProgress")
    document = FakeDocument([FakeSection("sec-1")])
    with patch.object(
        mod, "create_document_service", return_value=FakeDocumentService(document)
    ):
        with pytest.raises(ValueError, match="owner or an admin"):
            mod.release_review(OBJECT_KEY, "alice", is_admin=False)
    assert read_doc(table)["HITLReviewOwner"] == "bob"


@pytest.mark.unit
def test_an_admin_can_release_a_review_abandoned_by_its_owner(mod, table):
    """The recovery path for a reviewer who claimed a document and left."""
    put_doc(table, HITLReviewOwner="bob", HITLStatus="InProgress")
    document = FakeDocument([FakeSection("sec-1")])
    with patch.object(
        mod, "create_document_service", return_value=FakeDocumentService(document)
    ):
        mod.release_review(OBJECT_KEY, "admin", is_admin=True)
    assert "HITLReviewOwner" not in read_doc(table)


@pytest.mark.unit
def test_releasing_a_missing_document_raises(mod, table):
    with patch.object(
        mod, "create_document_service", return_value=FakeDocumentService(None)
    ):
        with pytest.raises(ValueError, match="not found"):
            mod.release_review(OBJECT_KEY, "alice")


# --------------------------------------------------------------------------
# skipAllSectionsReview
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_skip_all_skips_only_the_sections_nobody_has_reviewed(mod, table):
    """A completed section keeps its `Completed` status; only the rest are skipped.

    Folding already-reviewed sections into the skipped list would make a
    part-reviewed document indistinguishable from an untouched one in the audit
    record.
    """
    put_doc(table, HITLSectionsSkipped=["sec-4"], HITLPendingReview="true")
    document = FakeDocument(
        [
            FakeSection("sec-1"),
            FakeSection("sec-2"),
            FakeSection("sec-3"),
            FakeSection("sec-4"),
            FakeSection(None),
        ],
        completed=["sec-1"],
    )
    with (
        patch.object(
            mod, "create_document_service", return_value=FakeDocumentService(document)
        ),
        patch.object(mod, "trigger_reprocessing") as reprocess,
    ):
        mod.skip_all_sections_review(OBJECT_KEY, "admin", "admin@example.com")

    stored = read_doc(table)
    assert sorted(stored["HITLSectionsSkipped"]) == ["sec-2", "sec-3", "sec-4"]
    assert stored["HITLSectionsPending"] == []
    assert stored["HITLStatus"] == "Review Skipped"
    assert stored["HITLCompleted"] is True
    assert "HITLPendingReview" not in stored
    assert stored["HITLReviewedBy"] == "admin"
    entry = stored["HITLReviewHistory"][-1]
    assert entry["action"] == "skip_all"
    assert entry["sectionId"] == "ALL_SKIPPED"
    # The history entry records what *this* action skipped, which is narrower than
    # the cumulative list: sec-4 was already skipped before this call, so attributing
    # it to this admin would misreport who made that decision.
    assert sorted(entry["skippedSections"]) == ["sec-2", "sec-3"]
    reprocess.assert_called_once_with(OBJECT_KEY)


@pytest.mark.unit
def test_skip_all_on_a_missing_document_raises(mod, table):
    with patch.object(
        mod, "create_document_service", return_value=FakeDocumentService(None)
    ):
        with pytest.raises(ValueError, match="not found"):
            mod.skip_all_sections_review(OBJECT_KEY, "admin")


# --------------------------------------------------------------------------
# trigger_reprocessing
# --------------------------------------------------------------------------


@pytest.fixture
def queue(mod, monkeypatch):
    url = mod.sqs_client.create_queue(QueueName=QUEUE_NAME)["QueueUrl"]
    monkeypatch.setenv("QUEUE_URL", url)
    monkeypatch.setenv("INPUT_BUCKET", "in-bucket")
    monkeypatch.setenv("OUTPUT_BUCKET", "out-bucket")
    return url


def drain(mod, url):
    received = mod.sqs_client.receive_message(QueueUrl=url, MaxNumberOfMessages=10)
    return [json.loads(m["Body"]) for m in received.get("Messages", [])]


@pytest.mark.unit
def test_reprocessing_requeues_the_document_with_its_run_state_cleared(
    mod, queue, monkeypatch
):
    """Summarization and evaluation re-run only if the document looks fresh.

    A stale `workflow_execution_arn` or `completion_time` makes the queue
    processor treat the document as already finished, so the post-review
    summarization silently never happens. The compressed envelope is used when a
    working bucket is configured, because a full document can exceed the SQS
    256 KB message limit.
    """
    monkeypatch.setenv("WORKING_BUCKET", "work-bucket")
    document = FakeDocument([FakeSection("sec-1")])
    with patch.object(
        mod, "create_document_service", return_value=FakeDocumentService(document)
    ):
        mod.trigger_reprocessing(OBJECT_KEY)

    assert document.status == mod.Status.QUEUED
    assert document.start_time is None
    assert document.completion_time is None
    assert document.workflow_execution_arn is None
    assert document.input_bucket == "in-bucket"
    assert document.output_bucket == "out-bucket"
    assert document.serialize_calls == [("work-bucket", "hitl_complete")]
    assert drain(mod, queue) == [{"shape": "compressed", "object_key": OBJECT_KEY}]


@pytest.mark.unit
def test_without_a_working_bucket_the_whole_document_is_sent_inline(mod, queue):
    document = FakeDocument([FakeSection("sec-1")])
    with patch.object(
        mod, "create_document_service", return_value=FakeDocumentService(document)
    ):
        mod.trigger_reprocessing(OBJECT_KEY)
    assert document.serialize_calls == []
    assert drain(mod, queue) == [{"shape": "plain", "object_key": OBJECT_KEY}]


@pytest.mark.unit
def test_an_unconfigured_queue_url_is_logged_rather_than_raised(mod):
    """A deployment without the reprocessing queue must still finish reviews.

    Asserted on the warning and on the send never being attempted. With the guard
    neutralised the send is made with no queue URL, raises into the blanket
    `except`, and leaves the document in exactly the state checked below.
    """
    document = FakeDocument([FakeSection("sec-1")])
    with (
        log_spy(mod) as log,
        patch.object(mod.sqs_client, "send_message") as send,
        patch.object(
            mod, "create_document_service", return_value=FakeDocumentService(document)
        ),
    ):
        mod.trigger_reprocessing(OBJECT_KEY)  # no QUEUE_URL set
    send.assert_not_called()
    assert "QUEUE_URL not configured" in log.text
    assert document.status == mod.Status.QUEUED


@pytest.mark.unit
def test_a_document_that_cannot_be_reloaded_sends_nothing_and_does_not_raise(
    mod, queue
):
    """The named cause is the assertion.

    Without the guard the `None` document is assigned to, which raises into the
    blanket `except` and leaves the queue just as empty — so the specific "not found
    for reprocessing" line is the only thing that separates a recognised absence
    from a swallowed `AttributeError`.
    """
    with (
        log_spy(mod) as log,
        patch.object(
            mod, "create_document_service", return_value=FakeDocumentService(None)
        ),
    ):
        mod.trigger_reprocessing(OBJECT_KEY)
    assert f"Document {OBJECT_KEY} not found for reprocessing" in log.text
    assert "Failed to trigger reprocessing" not in log.text
    assert drain(mod, queue) == []


@pytest.mark.unit
def test_a_send_failure_does_not_undo_the_completed_review(mod, table, queue):
    """Reprocessing runs after the review is committed.

    Raising here would surface as a failed save to a reviewer whose work was in
    fact stored, and a retry would append a duplicate history entry.
    """
    put_doc(table)
    document = FakeDocument([FakeSection("sec-1", "s3://out/1.json")])
    with (
        patch.object(
            mod, "create_document_service", return_value=FakeDocumentService(document)
        ),
        patch.object(
            mod.sqs_client, "send_message", side_effect=RuntimeError("queue gone")
        ),
    ):
        result = mod.complete_section_review(OBJECT_KEY, "sec-1")
    assert result["HITLCompleted"] is True
    assert read_doc(table)["HITLCompleted"] is True


# --------------------------------------------------------------------------
# The response the UI renders
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_dynamodb_numbers_are_returned_as_json_serializable_ints_and_floats(mod, table):
    """DynamoDB hands back `Decimal`, which `json.dumps` refuses.

    An unconverted value fails the whole resolver response, and a blanket
    `float()` would render a 5-page document as `5.0` in the UI. Whole numbers
    must come back as `int`.
    """
    put_doc(
        table,
        PageCount=Decimal("5"),
        Sections=[{"Id": "sec-1", "Confidence": Decimal("0.875")}],
        Pages=[{"Index": Decimal("1")}],
        HITLTriggered=True,
    )
    result = mod.build_document_response(OBJECT_KEY)

    assert result["PageCount"] == 5
    assert isinstance(result["PageCount"], int)
    assert result["Sections"][0]["Confidence"] == 0.875
    assert isinstance(result["Sections"][0]["Confidence"], float)
    assert isinstance(result["Pages"][0]["Index"], int)
    json.dumps(result)  # would raise on a surviving Decimal


@pytest.mark.unit
def test_every_response_field_is_present_with_a_typed_default(mod, table):
    """An absent key reaches the UI as `undefined`, not as an empty list.

    The review screen iterates `HITLSectionsPending` and `HITLReviewHistory` and
    reads `HITLCompleted` as a boolean, so the defaults have to be typed rather
    than merely present.
    """
    put_doc(table)
    result = mod.build_document_response(OBJECT_KEY)

    for key in (
        "HITLSectionsPending",
        "HITLSectionsCompleted",
        "HITLSectionsSkipped",
        "HITLReviewHistory",
        "Sections",
        "Pages",
    ):
        assert result[key] == [], key
    assert result["HITLCompleted"] is False
    assert result["HITLTriggered"] is False
    assert result["PageCount"] == 0
    assert result["ObjectKey"] == OBJECT_KEY
    assert result["HITLStatus"] == ""


@pytest.mark.unit
def test_a_dynamodb_string_set_is_flattened_to_a_list(mod):
    """Sets are not JSON, so the converter has to unwrap them too."""
    converted = mod._convert_decimals({"tags": {"b", "a"}, "nums": (Decimal("1"),)})
    assert sorted(converted["tags"]) == ["a", "b"]
    assert converted["nums"] == [1]


# --------------------------------------------------------------------------
# Handler dispatch
# --------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field_name", "target"),
    [
        ("claimReview", "claim_review"),
        ("releaseReview", "release_review"),
        ("skipAllSectionsReview", "skip_all_sections_review"),
        ("completeSectionReview", "complete_section_review"),
        ("", "complete_section_review"),  # unknown field falls through to completion
    ],
)
def test_the_handler_routes_each_field_name_to_its_operation(mod, field_name, target):
    with patch.object(mod, target, return_value={"ok": True}) as op:
        result = mod.handler(event(field_name, ["Admin"]), None)
    assert result == {"ok": True}
    assert op.call_count == 1


@pytest.mark.unit
def test_release_is_told_whether_the_caller_is_an_admin(mod):
    """`is_admin` is derived from the group claim, not from a client argument.

    A caller able to assert its own admin status could release any reviewer's
    document.
    """
    with patch.object(mod, "release_review", return_value={}) as release:
        mod.handler(
            event("releaseReview", ["Admin"], arguments={"objectKey": OBJECT_KEY}), None
        )
    assert release.call_args.args[3] is True

    with patch.object(mod, "release_review", return_value={}) as release:
        mod.handler(
            event("releaseReview", ["Reviewer"], arguments={"objectKey": OBJECT_KEY}),
            None,
        )
    assert release.call_args.args[3] is False


@pytest.mark.unit
def test_the_handler_forwards_the_caller_identity_to_the_operation(mod):
    """The audit trail is only as good as the identity that reaches it."""
    with patch.object(mod, "complete_section_review", return_value={}) as op:
        mod.handler(
            {
                "info": {"fieldName": "completeSectionReview"},
                "arguments": {
                    "objectKey": OBJECT_KEY,
                    "sectionId": "sec-1",
                    "editedData": '{"a": 1}',
                },
                "identity": {
                    "username": "alice",
                    "claims": {
                        "email": "alice@example.com",
                        "cognito:groups": ["Reviewer"],
                    },
                },
            },
            None,
        )
    assert op.call_args.args == (
        OBJECT_KEY,
        "sec-1",
        '{"a": 1}',
        "alice",
        "alice@example.com",
    )
