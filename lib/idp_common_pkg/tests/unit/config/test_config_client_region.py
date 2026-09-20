# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The region reaches the clients that read and write the configuration.

`idp-cli config-upload --region eu-west-1` used to resolve the
ConfigurationTable's *name* from CloudFormation in eu-west-1 and then read and
write that name in whatever region the ambient credentials resolved to, because
``ConfigurationManager`` built ``boto3.resource("dynamodb")`` with no region and
had no parameter to pass one. A DynamoDB table name is not region-qualified, so
on a multi-region account that is a successful write to a *different* stack's
configuration table, reported to the operator as success. Seven other
``config-*`` commands, ``bootstrap`` and one migration script shared the defect.

These tests assert the region of the client that was actually constructed, which
needs no AWS call and no credentials. ``tests/conftest.py`` pins the ambient
region to us-east-1, so asking for eu-west-1 is the real cross-region case: with
the region dropped, every assertion below sees us-east-1.
"""

from __future__ import annotations

import boto3
import pytest

from idp_common.config import ConfigurationReader
from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.revisions import ConfigRevisionStore

REQUESTED = "eu-west-1"
AMBIENT = "us-east-1"  # what tests/conftest.py sets AWS_REGION to


@pytest.mark.unit
def test_manager_dynamodb_client_uses_requested_region():
    m = ConfigurationManager(table_name="some-config-table", region=REQUESTED)
    assert m.dynamodb.meta.client.meta.region_name == REQUESTED


@pytest.mark.unit
def test_manager_revision_s3_client_uses_requested_region():
    m = ConfigurationManager(table_name="some-config-table", region=REQUESTED)
    # ConfigRevisionStore builds its S3 client lazily on first property access.
    assert m.revisions.s3.meta.region_name == REQUESTED


@pytest.mark.unit
def test_revision_store_s3_client_uses_requested_region():
    store = ConfigRevisionStore(table=None, bucket="b", region=REQUESTED)
    assert store.s3.meta.region_name == REQUESTED


@pytest.mark.unit
def test_configuration_reader_threads_region_to_manager():
    """`config-download`'s non-revision branch goes through the reader, not the
    manager, so the reader needs the same passthrough or that one command stays
    broken after the manager is fixed."""
    r = ConfigurationReader(table_name="some-config-table", region=REQUESTED)
    assert r.manager.dynamodb.meta.client.meta.region_name == REQUESTED


@pytest.mark.unit
def test_region_none_defers_to_boto3_resolution():
    """The documented precedence: an explicit region wins; ``None`` means "let
    boto3 resolve it" (AWS_REGION / AWS_DEFAULT_REGION / profile / IMDS). No
    hardcoded fallback is introduced, so Lambda behaviour is unchanged — the
    runtime always sets AWS_REGION.
    """
    m = ConfigurationManager(table_name="some-config-table")
    assert m.dynamodb.meta.client.meta.region_name == boto3.Session().region_name
    assert m.dynamodb.meta.client.meta.region_name == AMBIENT


@pytest.mark.unit
def test_requested_region_differs_from_ambient():
    """Guards the two tests above against becoming vacuous: if the ambient region
    ever equalled REQUESTED, every assertion here would pass with the region
    dropped and the suite would be asserting nothing."""
    assert boto3.Session().region_name == AMBIENT
    assert REQUESTED != AMBIENT
