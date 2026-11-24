# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared setup for the handler suites that need a review-queue deployment."""

from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

import idp_common.s3 as s3_module
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.store import ReviewStore
from tests.unit.lambdas._review_env import BUCKET, REGION
from tests.unit.review_queues._table import create_review_table


@pytest.fixture
def review_env(monkeypatch):
    """moto: the HITL table with an enabled Default queue, and the output bucket.

    The store is the real one, and ``idp_common.s3`` is pointed at moto, so a
    handler run here writes the items and objects a later step would read. Yields
    ``store``, ``s3`` (a client) and ``bucket``.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # nosec B105 - moto dummy
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")  # nosec B105 - moto dummy
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_REGION", REGION)
    # Documents travel inline (no compressed wrapper).
    monkeypatch.setenv("WORKING_BUCKET", "")
    monkeypatch.setenv("OUTPUT_BUCKET", BUCKET)
    monkeypatch.setenv("DB_NAME", "")
    with mock_aws():
        monkeypatch.setattr(s3_module, "_s3_client", None)
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        store = ReviewStore(create_review_table(region=REGION), region=REGION)
        store.queue_defs.put(QueueDefinition(id="default", name="Default"), create=True)
        yield SimpleNamespace(store=store, s3=client, bucket=BUCKET)
