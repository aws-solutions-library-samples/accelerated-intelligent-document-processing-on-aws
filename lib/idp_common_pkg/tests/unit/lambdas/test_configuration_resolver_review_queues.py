# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The configuration resolver's Admin-only check on `hitl.review_queues` (FR-3, FR-4).

Runs the real resolver over the real ConfigurationManager on moto tables. The
block names a system-wide queue; an Admin naming one that HITL › Queue
Management does not define is warned, not refused.
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
from tests.unit.review_queues._table import create_review_table

CONFIG_TABLE = "cfg-rq"
USERS_TABLE = "users-rq"
BUCKET = "cfg-rq-bucket"
HITL_TABLE = "hitl-rq"

ENV = {
    "CONFIGURATION_TABLE_NAME": CONFIG_TABLE,
    "CONFIGURATION_BUCKET": BUCKET,
    "USERS_TABLE_NAME": USERS_TABLE,
    "HITL_TABLE_NAME": HITL_TABLE,
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "us-east-1",
}


def _routing(queue, enabled=True):
    """A ``hitl.review_queues`` delta naming ``queue`` (defined or not)."""
    return {"hitl": {"review_queues": {"enabled": enabled, "queue": queue}}}


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
        ddb.create_table(
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
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        # Empty: the first read seeds the Default and Escalation queues.
        create_review_table(HITL_TABLE)
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

    def _routing_of(self, version="lending"):
        cfg = ConfigurationManager()._get_full_config_for_version(version)
        rq = cfg.hitl.review_queues
        return (rq.enabled, rq.queue)

    def test_author_change_to_review_queues_is_permission_error(self):
        with pytest.raises(Exception) as exc:
            self.resolver.handler(_update(_routing("claims"), ["Author"]), None)
        assert type(exc.value) is PermissionError
        assert self._routing_of() == (False, "default")

    def test_author_unrelated_change_succeeds_without_warnings(self):
        result = self.resolver.handler(_update({"notes": "edited"}, ["Author"]), None)
        assert result["success"] is True
        assert result["warnings"] == []

    def test_admin_choosing_an_undefined_queue_gets_a_warning(self):
        result = self.resolver.handler(_update(_routing("claims"), ["Admin"]), None)
        assert result["success"] is True
        assert self._routing_of() == (True, "claims")
        assert len(result["warnings"]) == 1
        assert "'claims'" in result["warnings"][0]

    def test_admin_choosing_a_defined_queue_gets_no_warning(self):
        result = self.resolver.handler(_update(_routing("Escalation"), ["Admin"]), None)
        assert result["success"] is True
        assert result["warnings"] == []

    def test_unreadable_queue_definitions_never_fail_the_save(self, monkeypatch):
        monkeypatch.delenv("HITL_TABLE_NAME")
        result = self.resolver.handler(_update(_routing("claims"), ["Admin"]), None)
        assert result["success"] is True
        assert result["warnings"] == []
        assert self._routing_of() == (True, "claims")

    def test_author_restore_that_changes_review_queues_is_permission_error(self):
        admin = _update(_routing("escalation"), ["Admin"])
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
        assert self._routing_of() == (True, "escalation")

    def test_invalid_queue_id_is_returned_as_data(self):
        result = self.resolver.handler(_update(_routing("bad/name"), ["Admin"]), None)
        assert result["success"] is False
        assert result["error"]["type"] == "ValidationError"
        fields = [e["field"] for e in result["error"]["validationErrors"]]
        assert any("queue" in f for f in fields)

    def test_cross_field_refusal_is_returned_as_data(self, monkeypatch):
        """The block has no cross-field rule today; force one to pin the mapping."""
        from idp_common.config.models import HITLReviewQueuesConfig

        monkeypatch.setattr(
            HITLReviewQueuesConfig,
            "cross_field_problems",
            lambda self: ["queue nope is reserved"] if self.queue == "nope" else [],
        )
        result = self.resolver.handler(_update(_routing("nope"), ["Admin"]), None)
        assert result["success"] is False
        assert result["error"]["type"] == "ValidationError"
        assert "queue nope is reserved" in result["error"]["message"]
