# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``bulk_selection.PendingReviewQuery`` against a moto TrackingTable + TypeDateIndex."""

from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from idp_common.dynamodb.client import DynamoDBClient
from idp_common.review_queues.bulk_selection import PendingReviewQuery

from ._table import REGION

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 3, 31, tzinfo=timezone.utc)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName="tracking",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "ItemType", "AttributeType": "S"},
                {"AttributeName": "InitialEventTime", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "TypeDateIndex",
                    "KeySchema": [
                        {"AttributeName": "ItemType", "KeyType": "HASH"},
                        {"AttributeName": "InitialEventTime", "KeyType": "RANGE"},
                    ],
                    "Projection": {
                        "ProjectionType": "INCLUDE",
                        "NonKeyAttributes": [
                            "ObjectKey",
                            "ConfigVersion",
                            "HITLTriggered",
                            "HITLCompleted",
                            "HITLStatus",
                        ],
                    },
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table = ddb.Table("tracking")

        def put(key, when, **attrs):
            table.put_item(
                Item={
                    "PK": f"doc#{key}",
                    "SK": "none",
                    "ObjectKey": key,
                    "ItemType": attrs.pop("ItemType", "document"),
                    "InitialEventTime": when,
                    "ConfigVersion": attrs.pop("ConfigVersion", "claims"),
                    **attrs,
                }
            )

        put("pending.pdf", "2026-03-05T10:00:00.000Z", HITLStatus="Review Pending")
        put("triggered.pdf", "2026-03-06T10:00:00Z", HITLTriggered=True)
        put("queued.pdf", "2026-03-07T10:00:00.000Z", HITLStatus="in_review")
        put("done.pdf", "2026-03-08T10:00:00.000Z", HITLStatus="Review Skipped")
        put(
            "flagged.pdf",
            "2026-03-09T10:00:00.000Z",
            HITLStatus="Review Pending",
            HITLCompleted=True,
        )
        put(
            "other.pdf",
            "2026-03-10T10:00:00.000Z",
            HITLStatus="Review Pending",
            ConfigVersion="x",
        )
        put("before.pdf", "2026-02-10T10:00:00.000Z", HITLStatus="Review Pending")
        put("na.pdf", "2026-03-11T10:00:00.000Z", HITLStatus="N/A")
        put(
            "test.pdf",
            "2026-03-12T10:00:00.000Z",
            HITLStatus="Review Pending",
            ItemType="test-document",
        )
        for i in range(30):
            put(
                f"bulk-{i:02d}.pdf",
                f"2026-03-20T10:{i:02d}:00.000Z",
                HITLStatus="PendingReview",
            )
        yield DynamoDBClient(table_name="tracking", region=REGION)


def _all(query, **kw):
    keys = []
    for page, _cursor in query.pages("claims", T0, T1, **kw):
        keys.extend(page)
    return keys


@pytest.mark.unit
class TestPendingReviewQuery:
    def test_only_the_profiles_pending_rows_in_the_range(self, client):
        keys = set(_all(PendingReviewQuery(client), page_size=1000))
        assert {"pending.pdf", "triggered.pdf", "queued.pdf"} <= keys
        assert keys.isdisjoint(
            {"done.pdf", "flagged.pdf", "other.pdf", "before.pdf", "na.pdf", "test.pdf"}
        )
        assert len(keys) == 33

    def test_small_pages_paginate_without_duplicates(self, client):
        keys = _all(PendingReviewQuery(client), page_size=4)
        assert len(keys) == len(set(keys)) == 33

    def test_resuming_from_a_cursor(self, client):
        query = PendingReviewQuery(client)
        pages = query.pages("claims", T0, T1, page_size=10)
        first, cursor = next(pages)
        rest = []
        for page, _c in query.pages("claims", T0, T1, cursor=cursor, page_size=10):
            rest.extend(page)
        assert set(first).isdisjoint(rest) and len(first) + len(rest) == 33

    def test_collect_caps_and_reports_approximate(self, client):
        keys, approximate = PendingReviewQuery(client).collect(
            "claims", T0, T1, max_docs=5, budget_seconds=10
        )
        assert len(keys) == 5 and approximate is True
        keys, approximate = PendingReviewQuery(client).collect(
            "claims", T0, T1, max_docs=100, budget_seconds=10
        )
        assert len(keys) == 33 and approximate is False
