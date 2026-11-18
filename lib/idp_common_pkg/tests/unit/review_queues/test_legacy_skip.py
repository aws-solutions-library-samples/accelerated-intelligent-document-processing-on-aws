# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Legacy Skip All Reviews (``legacy_skip.skip_legacy_review``), on a moto table."""

from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from idp_common.review_queues import legacy_skip
from idp_common.review_queues.exceptions import ReviewHeldByOther

from ._table import REGION

KEY = "batch/doc.pdf"
TABLE = "tracking"


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
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
        yield ddb.Table(TABLE)


def _document(completed=()):
    return SimpleNamespace(
        hitl_sections_completed=list(completed),
        sections=[SimpleNamespace(section_id="1"), SimpleNamespace(section_id="2")],
    )


def _seed(table, **attrs):
    table.put_item(Item={"PK": f"doc#{KEY}", "SK": "none", **attrs})


def _skip(table, **kw):
    kw.setdefault("require_offered", True)
    return legacy_skip.skip_legacy_review(
        table, lambda _k: _document(), KEY, username="bob", user_email="bob@x", **kw
    )


def _row(table):
    return table.get_item(Key={"PK": f"doc#{KEY}", "SK": "none"})["Item"]


@pytest.mark.unit
class TestSkipLegacyReview:
    def test_skips_every_pending_section_and_records_the_bulk_context(self, table):
        _seed(table, HITLStatus="Review Pending", HITLTriggered=True)
        result = _skip(
            table,
            reason="Duplicate document",
            note="dup",
            job_id="brj-1",
            resubmitted=False,
        )
        row = _row(table)
        assert row["HITLStatus"] == "Review Skipped"
        assert sorted(row["HITLSectionsSkipped"]) == ["1", "2"]
        assert row["HITLCompleted"] is True
        [entry] = row["HITLReviewHistory"]
        assert entry["action"] == "skip_all" and entry["jobId"] == "brj-1"
        assert (entry["source"], entry["resubmitted"]) == ("ui", False)
        assert (entry["reason"], entry["note"]) == ("Duplicate document", "dup")
        assert sorted(result.skipped_sections) == ["1", "2"]

    def test_history_is_appended_not_overwritten(self, table):
        _seed(
            table,
            HITLStatus="Review Pending",
            HITLReviewHistory=[{"action": "complete", "sectionId": "1"}],
        )
        _skip(table)
        assert [h["action"] for h in _row(table)["HITLReviewHistory"]] == [
            "complete",
            "skip_all",
        ]

    def test_a_finished_review_is_refused(self, table):
        _seed(table, HITLStatus="Review Completed")
        with pytest.raises(ValueError, match="^REVIEW_FINISHED: "):
            _skip(table)

    def test_a_review_never_raised_is_refused(self, table):
        _seed(table, HITLStatus="N/A")
        with pytest.raises(ValueError, match="^NO_REVIEW_PENDING: "):
            _skip(table)

    def test_without_require_offered_an_admin_may_skip(self, table):
        _seed(table, HITLStatus="N/A")
        _skip(table, require_offered=False)
        assert _row(table)["HITLStatus"] == "Review Skipped"

    def test_a_review_finished_during_the_skip_is_refused_at_the_write(self, table):
        _seed(table, HITLStatus="Review Pending")

        class _Racing:
            def get_item(self, **kw):
                item = table.get_item(**kw)
                table.update_item(
                    Key={"PK": f"doc#{KEY}", "SK": "none"},
                    UpdateExpression="SET HITLStatus = :s",
                    ExpressionAttributeValues={":s": "Review Completed"},
                )
                return item

            def update_item(self, **kw):
                return table.update_item(**kw)

        with pytest.raises(ValueError, match="^REVIEW_FINISHED: "):
            _skip(_Racing())
        assert _row(table)["HITLStatus"] == "Review Completed"

    def test_require_raised_holds_at_the_write(self, table):
        _seed(table, HITLStatus="Review Pending")

        class _Clearing:
            def get_item(self, **kw):
                item = table.get_item(**kw)
                table.update_item(
                    Key={"PK": f"doc#{KEY}", "SK": "none"},
                    UpdateExpression="REMOVE HITLStatus",
                )
                return item

            def update_item(self, **kw):
                return table.update_item(**kw)

        with pytest.raises(ValueError, match="^REVIEW_FINISHED: "):
            _skip(_Clearing(), require_raised=True)

    def test_the_owner_rule_refuses_another_reviewers_review(self, table):
        _seed(table, HITLStatus="Review In Progress", HITLReviewOwner="carol")
        with pytest.raises(ReviewHeldByOther):
            _skip(table, owner_rule=legacy_skip.OwnerRule(username="bob"))
        _skip(table, owner_rule=legacy_skip.OwnerRule(username="carol"))

    def test_the_first_read_of_the_row_is_consistent(self, table):
        _seed(table, HITLStatus="Review Pending")
        reads = []

        class _Recording:
            def get_item(self, **kw):
                reads.append(kw)
                return table.get_item(**kw)

            def update_item(self, **kw):
                return table.update_item(**kw)

        _skip(_Recording())
        assert reads[0]["ConsistentRead"] is True

    def test_a_missing_document_is_refused(self, table):
        with pytest.raises(ValueError, match="not found"):
            legacy_skip.skip_legacy_review(
                table, lambda _k: None, KEY, require_offered=True
            )
