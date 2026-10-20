# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared setup for the ``idp_common.review_queues`` suites."""

import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import pytest
from moto import mock_aws

from ._table import REGION, create_review_table


@pytest.fixture
def review_table(monkeypatch):
    """A moto HITLTable (the deployed definition); yields its name.

    The region is pinned to the one the table is created in: the stores read
    ``AWS_REGION`` when they build their client, so a value left behind by an
    earlier test on the same xdist worker would send them to an empty region.
    """
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        yield create_review_table()
