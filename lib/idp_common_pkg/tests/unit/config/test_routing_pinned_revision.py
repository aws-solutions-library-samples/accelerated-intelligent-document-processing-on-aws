# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A revision saved before routing tables existed routes with default's table.

A document is processed with the revision it is pinned to. A pinned read resolves
the revision against ``default``, as the read of a profile's current
configuration does (#1955): every key the revision holds is kept as saved, and a
key it lacks is filled from ``default``. So a revision with no ``routing`` key
routes with the table ``default`` holds: the built-in one, unless an Admin
changed it there. The built-in table is also the model default, for a stack
whose ``default`` predates the section too.
"""

import gzip
import json

import boto3
import pytest
from moto import mock_aws

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import CONFIG_TYPE_CONFIG
from idp_common.config.models import IDPConfig
from idp_common.config.revisions import ConfigRevisionStore
from idp_common.config.routing_models import (
    BUILTIN_CONFIDENCE_REASON,
    BUILTIN_EXTRACTION_FAILED_REASON,
    BUILTIN_MISSING_REQUIRED_REASON,
    RoutingConfig,
)

pytestmark = pytest.mark.unit

TABLE = "routing-pinned-config"
BUCKET = "routing-pinned-config-bucket"
PROFILE = "lending"

_CUSTOM_ROUTING = {
    "tables": [
        {
            "rules": [
                {
                    "id": "everything-to-approvals",
                    "conditions": [],
                    "actions": [{"type": "review_queue", "queue": "approvals"}],
                }
            ]
        }
    ]
}


@pytest.fixture
def manager(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
    monkeypatch.setenv("CONFIGURATION_BUCKET", BUCKET)
    with mock_aws():
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield ConfigurationManager()


def _strip_routing_from_revision(profile: str, revision: int) -> dict:
    """Rewrite a stored revision body as code from before this section wrote it."""
    s3 = boto3.client("s3", region_name="us-east-1")
    key = ConfigRevisionStore.body_key(profile, revision)
    body = json.loads(
        gzip.decompress(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    )
    assert "routing" in body, "the fixture expects this release to store the section"
    del body["routing"]
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=gzip.compress(json.dumps(body).encode("utf-8")),
        ContentType="application/json",
        ContentEncoding="gzip",
    )
    return body


@pytest.mark.parametrize("default_predates_routing", [False, True])
def test_a_pre_upgrade_revision_reads_back_with_the_built_in_table(
    manager, default_predates_routing
):
    """``default`` holds the built-in table, or predates the section as well."""
    manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
    if default_predates_routing:
        _strip_routing_from_revision(
            "default", int(manager.resolve_published_revision("default"))
        )
    manager.save_configuration(
        CONFIG_TYPE_CONFIG, IDPConfig(notes="saved before the upgrade"), version=PROFILE
    )
    revision = manager.resolve_published_revision(PROFILE)
    assert revision is not None
    stored = _strip_routing_from_revision(PROFILE, int(revision))
    assert "routing" not in stored

    pinned = manager.get_merged_configuration(PROFILE, revision=int(revision))

    assert pinned.notes == "saved before the upgrade"
    assert pinned.routing == RoutingConfig()
    rules = pinned.routing.tables[0].rules
    assert [rule.id for rule in rules] == [
        "builtin-extraction-failed",
        "builtin-confidence-alerts",
        "builtin-missing-required",
    ]
    # The built-in routing rules of this release: actions and reasons.
    assert [[a.model_dump() for a in rule.actions] for rule in rules] == [
        [{"type": "review_queue", "queue": "default"}],
        [{"type": "review_queue", "queue": "default"}],
        [{"type": "review_queue", "queue": "default"}],
    ]
    assert [rule.reason for rule in rules] == [
        BUILTIN_EXTRACTION_FAILED_REASON,
        BUILTIN_CONFIDENCE_REASON,
        BUILTIN_MISSING_REQUIRED_REASON,
    ]
    assert pinned.routing.tables[0].default_outcome.actions == []


def test_a_pre_upgrade_revision_takes_the_table_default_holds(manager):
    """A key the revision lacks is filled from ``default`` (#1955), so the
    revision routes as its profile's current configuration does."""
    manager.save_configuration(
        CONFIG_TYPE_CONFIG, IDPConfig(routing=_CUSTOM_ROUTING), version="default"
    )
    manager.save_configuration(
        CONFIG_TYPE_CONFIG, IDPConfig(notes="saved before the upgrade"), version=PROFILE
    )
    revision = manager.resolve_published_revision(PROFILE)
    stored = _strip_routing_from_revision(PROFILE, int(revision))
    assert "routing" not in stored

    pinned = manager.get_merged_configuration(PROFILE, revision=int(revision))

    assert pinned.notes == "saved before the upgrade"
    assert [rule.id for rule in pinned.routing.tables[0].rules] == [
        "everything-to-approvals"
    ]
    assert pinned.routing != RoutingConfig()
    # The control: it is default's rule, and the stored body still has none.
    default = manager.get_merged_configuration("default")
    assert default.routing == pinned.routing


def test_a_revision_saved_with_routing_keeps_it(manager):
    manager.save_configuration(CONFIG_TYPE_CONFIG, IDPConfig(), version="default")
    manager.save_configuration(
        CONFIG_TYPE_CONFIG, IDPConfig(routing=_CUSTOM_ROUTING), version=PROFILE
    )
    revision = manager.resolve_published_revision(PROFILE)

    pinned = manager.get_merged_configuration(PROFILE, revision=int(revision))

    assert [rule.id for rule in pinned.routing.tables[0].rules] == [
        "everything-to-approvals"
    ]
