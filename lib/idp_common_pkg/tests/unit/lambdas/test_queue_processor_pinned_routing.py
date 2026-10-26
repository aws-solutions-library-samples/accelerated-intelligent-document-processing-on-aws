# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The queue processor routes a document by its pinned revision (#1619).

``start_workflow`` chooses the BDA or pipeline branch from ``use_bda``. It used
to read that from the profile head, so a save made while a document was queued
could send it down a branch its own pinned revision does not select. These tests
drive the real queue processor and the real ``ConfigurationManager`` against
moto's DynamoDB and S3, with a profile whose revision history holds a ``use_bda``
value that differs from the head's, and assert the routing flags in the input
handed to Step Functions.

The environment is the one ``template.yaml`` gives ``QueueProcessor``, which now
includes ``CONFIGURATION_BUCKET``. One test removes it again to show what the
pinned read does without it -- the configuration a pinned read was first tried
under, where every BDA document went to pipeline -- and that it now falls back
to the head instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import boto3
import pytest
import yaml
from botocore.exceptions import ClientError
from moto import mock_aws

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.models import Document
from tests.unit.lambdas._lambda_loader import REPO_ROOT, load_lambda

pytestmark = pytest.mark.unit

_REGION = "us-east-1"
_CONFIG_TABLE = "pinned-routing-config"
_CONFIG_BUCKET = "pinned-routing-config-bucket"
_PROFILE = "routing-profile"
_BDA_ARN = (
    "arn:aws:bedrock:us-east-1:123456789012:data-automation-project/pinned-routing"
)
_SM_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:pinned-routing"


def _preset() -> dict:
    path = REPO_ROOT / "config_library/unified/lending-package-sample/config.yaml"
    body = yaml.safe_load(Path(path).read_text())
    body.pop("description", None)
    body.pop("pricing", None)
    return merge_config_with_defaults(body, pattern="pattern-2", validate=False)


@pytest.fixture
def env(monkeypatch):
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setenv("CONFIG_TABLE", _CONFIG_TABLE)
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", _CONFIG_TABLE)
    monkeypatch.setenv("CONFIGURATION_BUCKET", _CONFIG_BUCKET)
    monkeypatch.setenv("CONCURRENCY_TABLE", "pinned-routing-concurrency")
    monkeypatch.setenv("TRACKING_TABLE", "pinned-routing-tracking")
    monkeypatch.setenv("STATE_MACHINE_ARN", _SM_ARN)
    monkeypatch.delenv("WORKING_BUCKET", raising=False)
    with mock_aws():
        boto3.resource("dynamodb", region_name=_REGION).create_table(
            TableName=_CONFIG_TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_CONFIG_BUCKET)
        processor = load_lambda(
            "pinned_routing_queue_processor_index", "src/lambda/queue_processor"
        )
        starts = []

        class _Sfn:
            def start_execution(self, **kwargs):
                starts.append(json.loads(kwargs["input"])["document"])
                return {"executionArn": f"{_SM_ARN}:e{len(starts)}"}

        monkeypatch.setattr(processor, "sfn", _Sfn())
        # The claim is #1612's subject, not this file's.
        monkeypatch.setattr(processor, "claim_start", lambda key: True)
        yield {"processor": processor, "starts": starts, "monkeypatch": monkeypatch}


def _profile_with_history(pinned_use_bda: bool, head_use_bda: bool) -> int:
    """Save the profile twice; return the first revision, which the head is not."""
    manager = ConfigurationManager(table_name=_CONFIG_TABLE)
    manager.save_configuration("Config", _preset(), version="default")
    first = dict(_preset(), use_bda=pinned_use_bda)
    manager.save_configuration("Config", first, version=_PROFILE)
    pinned = manager.resolve_published_revision(_PROFILE)
    second = dict(_preset(), use_bda=head_use_bda)
    # A change beyond use_bda, so a second revision is cut even when the two
    # use_bda values are equal.
    second["extraction"] = {**second["extraction"], "temperature": 0.37}
    manager.save_configuration("Config", second, version=_PROFILE)
    head = manager.resolve_published_revision(_PROFILE)
    assert pinned is not None and head is not None and head != pinned, (pinned, head)
    manager.set_bda_project_arn(_PROFILE, _BDA_ARN)
    return int(pinned)


def _route(env, revision):
    document = Document(
        id="routing.pdf",
        input_bucket="input",
        input_key="routing.pdf",
        config_version=_PROFILE,
        config_revision=revision,
    )
    env["processor"].start_workflow(document)
    (started,) = env["starts"]
    return started


@pytest.mark.parametrize(
    ("pinned_use_bda", "head_use_bda"),
    [
        pytest.param(True, False, id="pinned-bda-head-pipeline"),
        pytest.param(False, True, id="pinned-pipeline-head-bda"),
    ],
)
def test_routing_follows_the_pinned_revision_not_the_head(
    env, pinned_use_bda, head_use_bda
):
    pinned = _profile_with_history(pinned_use_bda, head_use_bda)

    started = _route(env, pinned)

    assert started["use_bda"] is pinned_use_bda, started
    assert ("bda_project_arn" in started) is pinned_use_bda, started


def test_a_bda_document_routes_to_bda_when_its_pin_is_the_head(env):
    """The ordinary case: nothing was saved while the document was queued."""
    manager = ConfigurationManager(table_name=_CONFIG_TABLE)
    manager.save_configuration("Config", _preset(), version="default")
    manager.save_configuration(
        "Config", dict(_preset(), use_bda=True), version=_PROFILE
    )
    manager.set_bda_project_arn(_PROFILE, _BDA_ARN)
    pinned = manager.resolve_published_revision(_PROFILE)
    assert pinned is not None

    started = _route(env, int(pinned))

    assert started["use_bda"] is True
    assert started["bda_project_arn"] == _BDA_ARN


def test_a_profile_with_no_history_routes_from_the_head(env):
    """No revision history: no pin is made, and the head routes, as before."""
    env["monkeypatch"].delenv("CONFIGURATION_BUCKET")
    manager = ConfigurationManager(table_name=_CONFIG_TABLE)
    manager.save_configuration("Config", _preset(), version="default")
    manager.save_configuration(
        "Config", dict(_preset(), use_bda=True), version=_PROFILE
    )
    manager.set_bda_project_arn(_PROFILE, _BDA_ARN)
    assert manager.resolve_published_revision(_PROFILE) is None

    started = _route(env, None)

    assert started["use_bda"] is True
    assert started["bda_project_arn"] == _BDA_ARN
    assert started.get("config_revision") is None, started


def test_without_the_bucket_a_pinned_bda_document_is_not_sent_to_pipeline(env):
    """The pinned read cannot work without ``CONFIGURATION_BUCKET``.

    That is the environment the first attempt at this pin ran under, and every
    BDA document routed to pipeline. The failure now falls back to the head.
    """
    pinned = _profile_with_history(pinned_use_bda=True, head_use_bda=True)
    env["monkeypatch"].delenv("CONFIGURATION_BUCKET")

    started = _route(env, pinned)

    assert started["use_bda"] is True
    assert started["bda_project_arn"] == _BDA_ARN


def test_an_unreadable_revision_falls_back_to_the_head_not_to_pipeline(env):
    """An S3 AccessDenied on the body (a missing grant) routes from the head."""
    pinned = _profile_with_history(pinned_use_bda=True, head_use_bda=True)
    reads = []
    real_client = boto3.client

    class _DeniedS3:
        def get_object(self, **kwargs):
            reads.append(kwargs["Key"])
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject"
            )

    def client(service, *args, **kwargs):
        if service == "s3":
            return _DeniedS3()
        return real_client(service, *args, **kwargs)

    env["monkeypatch"].setattr(boto3, "client", client)

    started = _route(env, pinned)

    assert reads == [f"config_revisions/{_PROFILE}/{pinned:06d}.json.gz"], reads
    assert started["use_bda"] is True
    assert started["bda_project_arn"] == _BDA_ARN
