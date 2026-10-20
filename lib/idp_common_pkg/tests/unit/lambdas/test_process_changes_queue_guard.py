# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""processChanges and the review queues (FR-25, D30)."""

import importlib.util
import os
from unittest.mock import patch

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "../../../../..")

_ENV = {
    "AWS_REGION": "us-east-1",
    "INPUT_BUCKET": "input-bucket",
    "OUTPUT_BUCKET": "output-bucket",
    "QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/1/q",
    "WORKING_BUCKET": "working-bucket",
}


@pytest.fixture
def resolver():
    with patch.dict(os.environ, _ENV):
        with patch("boto3.client"), patch("boto3.resource"):
            spec = importlib.util.spec_from_file_location(
                "process_changes_queue_guard_index",
                os.path.join(
                    _REPO_ROOT,
                    "nested/api-resolvers/src/lambda/process_changes_resolver/index.py",
                ),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod


def _event(groups):
    return {
        "info": {"fieldName": "processChanges"},
        "identity": {"claims": {"cognito:groups": list(groups)}},
        "arguments": {"objectKey": "packet.pdf", "modifiedSections": []},
    }


@pytest.mark.unit
class TestUnknownGroupRefused:
    def test_a_caller_in_no_review_group_is_refused(self, resolver):
        with pytest.raises(PermissionError, match="processChanges"):
            resolver.handler(_event(["SomeOtherGroup"]), None)
