# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared moto environment for the bulk Skip All Reviews suites."""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3

from idp_common.dynamodb.client import DynamoDBClient
from idp_common.review_queues.bulk_jobs import BulkJobStore, BulkSkipService
from idp_common.review_queues.bulk_models import ReviewCaller
from idp_common.review_queues.bulk_selection import PendingReviewQuery
from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.models import ReviewItemRecord
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.review_skip import SkipDeps
from idp_common.review_queues.service import ReviewQueueService
from idp_common.review_queues.store import ReviewStore, item_to_dynamo

from ._table import REGION

REASON = "Duplicate document"
CYCLE = "0" * 21
ADMIN = ReviewCaller(
    reviewer_key="admin@x", email="admin@x", groups=frozenset({"Admin"}), is_admin=True
)
REV = ReviewCaller(
    reviewer_key="rev@x", username="rev", email="rev@x", groups=frozenset({"Reviewer"})
)
SCOPED = ReviewCaller(
    reviewer_key="sc@x",
    username="sc",
    email="sc@x",
    groups=frozenset({"Reviewer"}),
    allowed_versions=["claims"],
)
TRACKING = "tracking"


def create_tracking_table():
    ddb = boto3.resource("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName=TRACKING,
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
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table(TRACKING)


class Env(SimpleNamespace):
    def doc(self, key, when="2026-03-05T10:00:00.000Z", **attrs):
        self.table.put_item(
            Item={
                "PK": f"doc#{key}",
                "SK": "none",
                "ObjectKey": key,
                "ItemType": "document",
                "InitialEventTime": when,
                "ConfigVersion": "claims",
                "ObjectStatus": "COMPLETED",
                **attrs,
            }
        )

    def item(self, key, state="OPEN", owner=None):
        now = int(time.time() * 1000)
        fields = dict(
            doc_key=key,
            cycle_id=CYCLE,
            profile="claims",
            queue="claims",
            state=state,
            enqueued_at="2026-01-01T00:00:00.000Z",
            enqueued_at_ms=now,
            sections=["1"],
            version=1,
        )
        if owner is not None:
            fields.update(
                owner_key=owner.reviewer_key,
                owner_email=owner.email,
                claimed_at_ms=now,
                lease_expires_at_ms=now + 3_600_000,
            )
        self.store.client.put_item(item_to_dynamo(ReviewItemRecord(**fields)))

    def status(self, key):
        return self.table.get_item(Key={"PK": f"doc#{key}", "SK": "none"})["Item"].get(
            "HITLStatus"
        )


def make_env(review_table, **service_kw):
    table = create_tracking_table()
    store = ReviewStore(review_table, region=REGION)
    store.queue_defs.put(QueueDefinition(id="claims", priority=70), create=True)
    review_service = ReviewQueueService(
        store,
        QueueDirectory(store.queue_defs),
        notifier=MagicMock(),
        finalize_sender=MagicMock(),
        s3=MagicMock(),
        output_bucket="out",
    )
    document = SimpleNamespace(
        hitl_sections_completed=[], sections=[SimpleNamespace(section_id="1")]
    )
    resubmitter = MagicMock()

    def deps_factory():
        return SkipDeps(
            tracking_table=table,
            get_document=lambda _k: document,
            review_service=review_service,
            store=store,
            resubmitter=resubmitter,
        )

    tracking_client = DynamoDBClient(table_name=TRACKING, region=REGION)
    jobs = BulkJobStore(DynamoDBClient(table_name=review_table, region=REGION))
    sent = []
    defaults = dict(
        tracking_client=tracking_client,
        job_sender=lambda job_id, token, delay: sent.append((job_id, token, delay)),
        sync_limit=lambda: 5,
        retention_days=30,
    )
    defaults.update(service_kw)
    service = BulkSkipService(
        deps_factory, jobs, PendingReviewQuery(tracking_client), **defaults
    )
    return Env(
        table=table,
        store=store,
        review_service=review_service,
        resubmitter=resubmitter,
        jobs=jobs,
        service=service,
        sent=sent,
    )
