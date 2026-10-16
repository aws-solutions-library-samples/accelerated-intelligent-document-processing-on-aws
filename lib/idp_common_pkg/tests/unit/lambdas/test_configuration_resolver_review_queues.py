# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The configuration resolver's Admin-only check on `hitl.review_queues` (FR-3, FR-4).

Runs the real resolver over the real ConfigurationManager on moto tables.
"""

import json
import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import CONFIG_TYPE_CONFIG, DEFAULT_VERSION
from idp_common.config.models import IDPConfig
from tests.unit.lambdas._lambda_loader import load_lambda

CONFIG_TABLE = "cfg-rq"
USERS_TABLE = "users-rq"
BUCKET = "cfg-rq-bucket"

ENV = {
    "CONFIGURATION_TABLE_NAME": CONFIG_TABLE,
    "CONFIGURATION_BUCKET": BUCKET,
    "USERS_TABLE_NAME": USERS_TABLE,
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "us-east-1",
}

QUEUES = [
    {"name": "default", "member_groups": ["QueueReviewer"]},
    {"name": "escalation", "member_groups": ["Admin"]},
]


def _event(field, args, groups, email="caller@example.com"):
    return {
        "info": {"fieldName": field},
        "arguments": args,
        "identity": {
            "claims": {
                "email": email,
                "sub": "sub-" + email,
                "cognito:username": email,
                "cognito:groups": groups,
            }
        },
    }


def _update(config, groups, version="lending"):
    return _event(
        "updateConfiguration",
        {"versionName": version, "customConfig": json.dumps(config)},
        groups,
    )


@pytest.mark.unit
@mock_aws
class TestConfigurationResolverReviewQueues:
    def setup_method(self, method):
        self._env = patch.dict(os.environ, ENV)
        self._env.start()
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=CONFIG_TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        users = ddb.create_table(
            TableName=USERS_TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
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
        users.put_item(
            Item={
                "PK": "USER#v",
                "SK": "USER#v",
                "email": "viewer@example.com",
                "persona": "Viewer",
            }
        )
        users.put_item(
            Item={
                "PK": "USER#q",
                "SK": "USER#q",
                "email": "qr@example.com",
                "persona": "QueueReviewer",
            }
        )
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        manager = ConfigurationManager()
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="default"), version=DEFAULT_VERSION
        )
        manager.save_configuration(
            CONFIG_TYPE_CONFIG, IDPConfig(notes="lending"), version="lending"
        )
        self.resolver = load_lambda(
            "configuration_resolver_rq_index",
            "nested/api-resolvers/src/lambda/configuration_resolver",
        )

    def teardown_method(self, method):
        self._env.stop()

    def _queues(self, version="lending"):
        cfg = ConfigurationManager()._get_full_config_for_version(version)
        return cfg.hitl.review_queues.queues

    def test_author_change_to_queues_is_permission_error(self):
        config = {"hitl": {"review_queues": {"queues": QUEUES + [{"name": "claims"}]}}}
        with pytest.raises(Exception) as exc:
            self.resolver.handler(_update(config, ["Author"]), None)
        assert type(exc.value) is PermissionError
        assert len(self._queues()) == 2

    def test_author_unrelated_change_succeeds_without_warnings(self):
        result = self.resolver.handler(_update({"notes": "edited"}, ["Author"]), None)
        assert result["success"] is True
        assert result["warnings"] == []

    def test_admin_adding_unknown_email_gets_a_warning(self):
        config = {
            "hitl": {
                "review_queues": {
                    "queues": [
                        {
                            **QUEUES[0],
                            "member_emails": ["ghost@example.com", "qr@example.com"],
                        },
                        QUEUES[1],
                    ]
                }
            }
        }
        result = self.resolver.handler(_update(config, ["Admin"]), None)
        assert result["success"] is True
        assert len(result["warnings"]) == 1
        assert "ghost@example.com" in result["warnings"][0]

    def test_admin_adding_a_viewer_gets_a_warning(self):
        config = {
            "hitl": {
                "review_queues": {
                    "queues": [
                        {**QUEUES[0], "member_emails": ["viewer@example.com"]},
                        QUEUES[1],
                    ]
                }
            }
        }
        result = self.resolver.handler(_update(config, ["Admin"]), None)
        assert len(result["warnings"]) == 1
        assert "viewer@example.com" in result["warnings"][0]

    def test_author_restore_that_changes_queues_is_permission_error(self):
        admin = _update(
            {"hitl": {"review_queues": {"queues": QUEUES + [{"name": "claims"}]}}},
            ["Admin"],
        )
        assert self.resolver.handler(admin, None)["success"] is True
        revisions = ConfigurationManager().list_revisions("lending")
        oldest = min(int(r["revision"]) for r in revisions)
        with pytest.raises(Exception) as exc:
            self.resolver.handler(
                _event(
                    "restoreConfigProfileRevision",
                    {"profileName": "lending", "revision": oldest},
                    ["Author"],
                ),
                None,
            )
        assert type(exc.value) is PermissionError

    def test_cross_field_invalid_is_returned_as_data(self):
        config = {"hitl": {"review_queues": {"default_queue": "nope"}}}
        result = self.resolver.handler(_update(config, ["Admin"]), None)
        assert result["success"] is False
        assert result["error"]["type"] == "ValidationError"
        assert "default_queue" in result["error"]["message"]
