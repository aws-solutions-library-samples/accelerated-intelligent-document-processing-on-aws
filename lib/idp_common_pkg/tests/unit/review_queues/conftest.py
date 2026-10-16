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

from ._table import create_review_table


@pytest.fixture
def review_table():
    """A moto HITLTable (the deployed definition); yields its name."""
    with mock_aws():
        yield create_review_table()
