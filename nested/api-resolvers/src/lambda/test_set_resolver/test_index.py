# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for test_set_resolver — getTestSetDocuments.

The resolver builds its S3 and DynamoDB clients at import time, so each test
imports the module inside moto with the env vars already set (same pattern as
get_file_contents_resolver/test_index.py).
"""

import importlib

import boto3
import pytest
from moto import mock_aws

from idp_common.review_queues.queue_defs import DEFAULT_SKIP_REASONS

TEST_SET_BUCKET = "test-set-bucket"
TRACKING_TABLE = "tracking-table"
TEST_SET_ID = "my-set"


def _event(field, arguments, groups=("Admin",)):
    return {
        "info": {"fieldName": field},
        "arguments": arguments,
        "identity": {"claims": {"cognito:groups": list(groups)}},
    }


def _seed_document(s3, name, sections=(1,)):
    s3.put_object(
        Bucket=TEST_SET_BUCKET,
        Key=f"{TEST_SET_ID}/input/{name}",
        Body=b"%PDF-1.4 fake",
    )
    for n in sections:
        s3.put_object(
            Bucket=TEST_SET_BUCKET,
            Key=f"{TEST_SET_ID}/baseline/{name}/sections/{n}/result.json",
            Body=b'{"inference_result": {}}',
        )


@pytest.fixture
def resolver(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("TEST_SET_BUCKET", TEST_SET_BUCKET)
    monkeypatch.setenv("TRACKING_TABLE", TRACKING_TABLE)
    monkeypatch.setenv("INPUT_BUCKET", "input-bucket")
    monkeypatch.setenv("TEST_SET_COPY_QUEUE_URL", "https://sqs/queue")
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
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
        ddb.put_item(
            TableName=TRACKING_TABLE,
            Item={
                "PK": {"S": f"testset#{TEST_SET_ID}"},
                "SK": {"S": "metadata"},
                "id": {"S": TEST_SET_ID},
                "name": {"S": "my set"},
                "status": {"S": "COMPLETED"},
                "createdAt": {"S": "2026-01-01T00:00:00Z"},
            },
        )

        # Import after mock + env are in place so the module-level clients
        # bind to moto.
        import index

        importlib.reload(index)
        yield index, s3


@pytest.mark.unit
def test_lists_documents_with_sections(resolver):
    index, s3 = resolver
    _seed_document(s3, "doc-a.pdf", sections=(1, 2, 10))
    _seed_document(s3, "doc-b.png")

    result = index.handler(
        _event("getTestSetDocuments", {"testSetId": TEST_SET_ID}), None
    )

    assert result["nextToken"] is None
    docs = {d["objectKey"]: d for d in result["documents"]}
    assert set(docs) == {"doc-a.pdf", "doc-b.png"}
    assert docs["doc-a.pdf"]["inputKey"] == f"{TEST_SET_ID}/input/doc-a.pdf"
    assert docs["doc-a.pdf"]["size"] > 0
    assert docs["doc-a.pdf"]["lastModified"]
    # Numeric sort: 1, 2, 10 (not 1, 10, 2)
    assert [s["sectionId"] for s in docs["doc-a.pdf"]["sections"]] == ["1", "2", "10"]
    assert (
        docs["doc-b.png"]["sections"][0]["baselineKey"]
        == f"{TEST_SET_ID}/baseline/doc-b.png/sections/1/result.json"
    )


@pytest.mark.unit
def test_handles_nested_input_names(resolver):
    index, s3 = resolver
    _seed_document(s3, "folder/nested doc.pdf")

    result = index.handler(
        _event("getTestSetDocuments", {"testSetId": TEST_SET_ID}), None
    )

    (doc,) = result["documents"]
    assert doc["objectKey"] == "folder/nested doc.pdf"
    assert doc["sections"] == [
        {
            "sectionId": "1",
            "baselineKey": f"{TEST_SET_ID}/baseline/folder/nested doc.pdf/sections/1/result.json",
        }
    ]


@pytest.mark.unit
def test_pagination_round_trip(resolver):
    index, s3 = resolver
    for i in range(5):
        _seed_document(s3, f"doc-{i}.pdf")

    first = index.handler(
        _event("getTestSetDocuments", {"testSetId": TEST_SET_ID, "limit": 2}), None
    )
    assert len(first["documents"]) == 2
    assert first["nextToken"]

    second = index.handler(
        _event(
            "getTestSetDocuments",
            {"testSetId": TEST_SET_ID, "limit": 3, "nextToken": first["nextToken"]},
        ),
        None,
    )
    assert len(second["documents"]) == 3
    assert second["nextToken"] is None
    all_names = {d["objectKey"] for d in first["documents"] + second["documents"]}
    assert all_names == {f"doc-{i}.pdf" for i in range(5)}


@pytest.mark.unit
def test_object_key_filter_returns_single_document(resolver):
    index, s3 = resolver
    _seed_document(s3, "doc-a.pdf", sections=(1, 2))
    _seed_document(s3, "doc-a.pdf.bak")  # same-prefix sibling must be excluded
    _seed_document(s3, "doc-b.pdf")

    result = index.handler(
        _event(
            "getTestSetDocuments",
            {"testSetId": TEST_SET_ID, "objectKey": "doc-a.pdf"},
        ),
        None,
    )
    (doc,) = result["documents"]
    assert doc["objectKey"] == "doc-a.pdf"
    assert [s["sectionId"] for s in doc["sections"]] == ["1", "2"]


@pytest.mark.unit
def test_object_key_traversal_rejected(resolver):
    index, _ = resolver
    with pytest.raises(Exception, match="Invalid object key"):
        index.handler(
            _event(
                "getTestSetDocuments",
                {"testSetId": TEST_SET_ID, "objectKey": "../other/secret.pdf"},
            ),
            None,
        )


@pytest.mark.unit
def test_missing_baseline_yields_empty_sections(resolver):
    index, s3 = resolver
    s3.put_object(
        Bucket=TEST_SET_BUCKET,
        Key=f"{TEST_SET_ID}/input/orphan.pdf",
        Body=b"x",
    )

    result = index.handler(
        _event("getTestSetDocuments", {"testSetId": TEST_SET_ID}), None
    )
    (doc,) = result["documents"]
    assert doc["sections"] == []


@pytest.mark.unit
def test_unknown_test_set_rejected(resolver):
    index, _ = resolver
    with pytest.raises(Exception, match="not found"):
        index.handler(_event("getTestSetDocuments", {"testSetId": "no-such-set"}), None)


@pytest.mark.unit
@pytest.mark.parametrize("bad_id", ["../other", "a/b", "set#1", ""])
def test_invalid_test_set_id_rejected(resolver, bad_id):
    index, _ = resolver
    with pytest.raises(Exception, match="Invalid test set id"):
        index.handler(_event("getTestSetDocuments", {"testSetId": bad_id}), None)


@pytest.mark.unit
@pytest.mark.parametrize("groups", [(), ("Viewer",), ("Reviewer",)])
def test_rbac_denies_non_admin_author(resolver, groups):
    index, _ = resolver
    with pytest.raises(Exception, match="Unauthorized"):
        index.handler(
            _event("getTestSetDocuments", {"testSetId": TEST_SET_ID}, groups=groups),
            None,
        )


# --------------------------------------------------------------------------
# Test Studio queues: settings, listing, claim, skip and My Work › Tasks
# --------------------------------------------------------------------------

HITL_TABLE = "hitl-table"
ANNOTATOR = "ann@example.com"


def _caller(groups, email):
    return {"claims": {"cognito:groups": list(groups), "email": email}}


@pytest.fixture
def queues(resolver, monkeypatch):
    """The resolver plus the HITL table holding Test Studio document items."""
    index, s3 = resolver
    monkeypatch.setenv("HITL_TABLE_NAME", HITL_TABLE)
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    hitl = ddb.create_table(
        TableName=HITL_TABLE,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": attr, "AttributeType": "S"}
            for attr in ("PK", "SK", "OwnerPK", "OwnerSK")
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "OwnerIndex",
                "KeySchema": [
                    {"AttributeName": "OwnerPK", "KeyType": "HASH"},
                    {"AttributeName": "OwnerSK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    tracking = ddb.Table(TRACKING_TABLE)
    tracking.update_item(
        Key={"PK": f"testset#{TEST_SET_ID}", "SK": "metadata"},
        UpdateExpression="SET QueueEligibleAnnotators = :a, TimeLimitMinutes = :t",
        ExpressionAttributeValues={":a": [ANNOTATOR], ":t": 60},
    )
    return index, hitl, tracking


def _put_doc_item(hitl, object_key, **attributes):
    hitl.put_item(
        Item={
            "PK": f"TSDOC#{TEST_SET_ID}",
            "SK": f"D#{object_key}",
            "TestSetId": TEST_SET_ID,
            "ObjectKey": object_key,
            **attributes,
        }
    )


def _metadata(tracking):
    return tracking.get_item(Key={"PK": f"testset#{TEST_SET_ID}", "SK": "metadata"})[
        "Item"
    ]


@pytest.mark.unit
def test_an_admin_updates_the_queue_settings(queues):
    index, _, tracking = queues
    result = index.handler(
        _event(
            "updateTestSetQueue",
            {
                "input": {
                    "testSetId": TEST_SET_ID,
                    "description": "  Invoices first  ",
                    "priority": 70,
                    "timeLimitMinutes": 120,
                    "checklist": ["Check totals", "  ", "Check dates"],
                }
            },
        ),
        None,
    )
    assert result == {
        "testSetId": TEST_SET_ID,
        "description": "Invoices first",
        "priority": 70,
        "claimLeaseMinutes": 120,
        "eligibleAnnotators": [ANNOTATOR],
        "checklist": ["Check totals", "Check dates"],
        # Never saved for this set, so the review queues' defaults.
        "skipReasons": list(DEFAULT_SKIP_REASONS),
    }
    meta = _metadata(tracking)
    assert meta["QueuePriority"] == 70
    assert meta["TimeLimitMinutes"] == 120


@pytest.mark.unit
@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"priority": 101}, "priority must be 0-100"),
        ({"eligibleAnnotators": ["not-an-email"]}, "must contain email addresses"),
        ({"checklist": [f"item {i}" for i in range(21)]}, "at most 20 items"),
        ({"skipReasons": [f"r{i}" for i in range(21)]}, "1 to 20 reasons"),
        ({}, "Nothing to update"),
    ],
)
def test_invalid_queue_settings_are_refused_before_any_write(queues, data, message):
    index, _, tracking = queues
    before = _metadata(tracking)
    with pytest.raises(ValueError, match=message):
        index.update_test_set_queue({"input": {"testSetId": TEST_SET_ID, **data}})
    assert _metadata(tracking) == before


@pytest.mark.unit
def test_queue_settings_for_an_unknown_set_are_not_found(queues):
    index, _, _ = queues
    with pytest.raises(index.ResourceNotFound):
        index.update_test_set_queue({"input": {"testSetId": "nope", "priority": 1}})


@pytest.mark.unit
def test_only_an_admin_may_change_queue_settings(queues):
    index, _, _ = queues
    with pytest.raises(Exception, match="requires Admin group"):
        index.handler(
            _event(
                "updateTestSetQueue",
                {"input": {"testSetId": TEST_SET_ID, "priority": 1}},
                groups=("Author",),
            ),
            None,
        )


@pytest.mark.unit
def test_an_annotator_lists_only_the_queues_that_name_them(queues):
    index, _, tracking = queues
    tracking.put_item(
        Item={
            "PK": "testset#other-set",
            "SK": "metadata",
            "id": "other-set",
            "status": "COPYING",
            "createdAt": "2026-01-02T00:00:00Z",
        }
    )
    tracking.update_item(
        Key={"PK": f"testset#{TEST_SET_ID}", "SK": "metadata"},
        UpdateExpression="SET SummaryVersion = :v, SummaryCount = :c, "
        "QueuePendingDocs = :p",
        ExpressionAttributeValues={":v": index.SUMMARY_VERSION, ":c": 3, ":p": 2},
    )

    as_annotator = {
        "identity": _caller(["Annotator"], ANNOTATOR.upper()),
        "info": {"fieldName": "listTestSetQueues"},
        "arguments": {},
    }
    listed = index.handler(as_annotator, None)
    assert [q["id"] for q in listed] == [TEST_SET_ID]
    queue = listed[0]
    assert queue["summaryStatus"] == index.SUMMARY_READY
    assert (queue["pendingDocs"], queue["name"]) == (2, "my set")
    assert queue["eligibleAnnotators"] == [ANNOTATOR]

    as_admin = {**as_annotator, "identity": _caller(["Admin"], "admin@example.com")}
    by_id = {q["id"]: q for q in index.handler(as_admin, None)}
    assert set(by_id) == {TEST_SET_ID, "other-set"}
    # A set still copying gets no summary request; its name falls back to its id.
    assert by_id["other-set"]["summaryStatus"] == index.SUMMARY_BUILDING
    assert by_id["other-set"]["name"] == "other-set"


@pytest.mark.unit
def test_an_annotator_listed_on_the_queue_claims_an_open_document(queues):
    index, hitl, tracking = queues
    _put_doc_item(hitl, "a.pdf")
    claimed = index.claim_test_set_document(
        {"testSetId": TEST_SET_ID, "objectKey": "a.pdf"},
        {"identity": _caller(["Annotator"], ANNOTATOR)},
    )
    assert claimed["assignedTo"] == ANNOTATOR
    assert claimed["objectKey"] == "a.pdf"
    item = hitl.get_item(Key={"PK": f"TSDOC#{TEST_SET_ID}", "SK": "D#a.pdf"})["Item"]
    assert item["AssignedBy"] == ANNOTATOR
    assert _metadata(tracking)["QueueAssignedDocs"] == 1

    # A second claim of the same document is refused, naming who holds it.
    with pytest.raises(ValueError, match="ALREADY_CLAIMED"):
        index.claim_test_set_document(
            {"testSetId": TEST_SET_ID, "objectKey": "a.pdf"},
            {"identity": _caller(["Annotator"], ANNOTATOR)},
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("identity", "message"),
    [
        (_caller(["Admin"], "admin@example.com"), "only Annotators"),
        (_caller(["Annotator"], "other@example.com"), "aren't listed on this queue"),
    ],
)
def test_a_claim_is_refused_to_anyone_not_listed_on_the_queue(
    queues, identity, message
):
    index, hitl, _ = queues
    _put_doc_item(hitl, "a.pdf")
    with pytest.raises(PermissionError, match=f"^NOT_ELIGIBLE: .*{message}"):
        index.claim_test_set_document(
            {"testSetId": TEST_SET_ID, "objectKey": "a.pdf"}, {"identity": identity}
        )
    item = hitl.get_item(Key={"PK": f"TSDOC#{TEST_SET_ID}", "SK": "D#a.pdf"})["Item"]
    assert "Assignee" not in item


@pytest.mark.unit
@pytest.mark.parametrize(
    "args",
    [
        {"testSetId": "bad id!", "objectKey": "a.pdf"},
        {"testSetId": TEST_SET_ID, "objectKey": "../a.pdf"},
        {"testSetId": TEST_SET_ID, "objectKey": "/a.pdf"},
        {"testSetId": TEST_SET_ID},
    ],
)
def test_claim_and_skip_refuse_malformed_identifiers(queues, args):
    index, _, _ = queues
    event = {"identity": _caller(["Annotator"], ANNOTATOR)}
    with pytest.raises(ValueError, match="^Invalid"):
        index.claim_test_set_document(args, event)
    with pytest.raises(ValueError, match="^Invalid"):
        index.skip_test_set_document(args, event)


@pytest.mark.unit
def test_skipping_hands_the_document_back_to_the_queue(queues):
    index, hitl, tracking = queues
    _put_doc_item(hitl, "a.pdf")
    event = {"identity": _caller(["Annotator"], ANNOTATOR)}
    index.claim_test_set_document(
        {"testSetId": TEST_SET_ID, "objectKey": "a.pdf"}, event
    )

    result = index.skip_test_set_document(
        {
            "testSetId": TEST_SET_ID,
            "objectKey": "a.pdf",
            "reason": "Out of time",
            "note": "  back tomorrow  ",
        },
        event,
    )

    assert result == {"testSetId": TEST_SET_ID, "objectKey": "a.pdf", "skipped": True}
    item = hitl.get_item(Key={"PK": f"TSDOC#{TEST_SET_ID}", "SK": "D#a.pdf"})["Item"]
    assert "Assignee" not in item and "OwnerPK" not in item
    # The skip is recorded with the document: who, why, and the note.
    [skip] = item["SkipHistory"]
    assert (skip["by"], skip["reason"], skip["note"]) == (
        ANNOTATOR.lower(),
        "Out of time",
        "back tomorrow",
    )
    meta = _metadata(tracking)
    assert (meta["QueueAssignedDocs"], meta["QueuePendingDocs"]) == (0, 1)


@pytest.mark.unit
@pytest.mark.parametrize("reason", [None, "", "Lunch"])
def test_a_skip_needs_a_reason_from_the_sets_list(queues, reason):
    """Refused before the assignment ends."""
    index, hitl, _ = queues
    _put_doc_item(hitl, "b.pdf")
    event = {"identity": _caller(["Annotator"], ANNOTATOR)}
    index.claim_test_set_document(
        {"testSetId": TEST_SET_ID, "objectKey": "b.pdf"}, event
    )
    with pytest.raises(ValueError, match="REASON_REQUIRED"):
        index.skip_test_set_document(
            {"testSetId": TEST_SET_ID, "objectKey": "b.pdf", "reason": reason}, event
        )
    item = hitl.get_item(Key={"PK": f"TSDOC#{TEST_SET_ID}", "SK": "D#b.pdf"})["Item"]
    assert "Assignee" in item and "SkipHistory" not in item


@pytest.mark.unit
def test_a_set_with_its_own_skip_reasons_checks_against_them(queues):
    index, hitl, _ = queues
    index.update_test_set_queue(
        {"input": {"testSetId": TEST_SET_ID, "skipReasons": ["Needs a tax expert"]}}
    )
    _put_doc_item(hitl, "c.pdf")
    event = {"identity": _caller(["Annotator"], ANNOTATOR)}
    index.claim_test_set_document(
        {"testSetId": TEST_SET_ID, "objectKey": "c.pdf"}, event
    )
    with pytest.raises(ValueError, match="REASON_REQUIRED"):
        index.skip_test_set_document(
            {"testSetId": TEST_SET_ID, "objectKey": "c.pdf", "reason": "Out of time"},
            event,
        )
    index.skip_test_set_document(
        {
            "testSetId": TEST_SET_ID,
            "objectKey": "c.pdf",
            "reason": "Needs a tax expert",
        },
        event,
    )


@pytest.mark.unit
def test_my_tasks_lists_only_sets_the_caller_may_still_open(queues, monkeypatch):
    index, hitl, _ = queues
    _put_doc_item(hitl, "a.pdf", Classes=["Invoice"], AlertCount=2)
    hitl.put_item(
        Item={
            "PK": "TSDOC#revoked-set",
            "SK": "D#b.pdf",
            "TestSetId": "revoked-set",
            "ObjectKey": "b.pdf",
        }
    )
    store = index._doc_store()
    for test_set_id, key in ((TEST_SET_ID, "a.pdf"), ("revoked-set", "b.pdf")):
        store.assign(test_set_id, key, ANNOTATOR, assigned_by="admin", time_limit=60)

    checked = []

    def scope(event, test_set_id):
        checked.append(test_set_id)
        if test_set_id == "revoked-set":
            raise index.TestSetAccessDenied("no longer in scope")

    monkeypatch.setattr(index, "assert_can_access_test_set", scope)
    tasks = index.list_my_test_set_tasks(
        {"identity": _caller(["Annotator"], ANNOTATOR)}
    )

    assert sorted(checked) == sorted(["revoked-set", TEST_SET_ID])
    assert [(t["testSetId"], t["objectKey"]) for t in tasks] == [(TEST_SET_ID, "a.pdf")]
    assert tasks[0]["assignedTo"] == ANNOTATOR
    assert (tasks[0]["documentClasses"], tasks[0]["alertCount"]) == (["Invoice"], 2)


@pytest.mark.unit
def test_my_tasks_is_empty_for_a_caller_with_no_email(queues):
    index, _, _ = queues
    assert index.list_my_test_set_tasks({"identity": {"claims": {}}}) == []
