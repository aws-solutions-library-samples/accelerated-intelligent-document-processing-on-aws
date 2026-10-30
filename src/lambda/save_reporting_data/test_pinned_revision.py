# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The report's configuration is the document's pinned revision (#1619).

The handler used to read the profile head, so a save made while a document was
in flight changed the classes its report was built from, and an unpinned read
could attempt a configuration write-back this role cannot make. The first half
of this file pins the branches with ``get_config`` replaced; the second drives
the real configuration reader against moto with a revision history whose class
list differs from the head's.
"""

import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import boto3
import index
import pytest
import yaml
from botocore.exceptions import ClientError
from moto import mock_aws

_TABLE = "reporting-pin-config"
_BUCKET = "reporting-pin-config-bucket"
_PROFILE = "reporting-profile"


def _event(revision):
    document = {"id": "doc.pdf", "input_key": "doc.pdf", "config_version": _PROFILE}
    if revision is not None:
        document["config_revision"] = revision
    return {
        "document": document,
        "reporting_bucket": "reporting",
        "data_to_save": ["evaluation_results"],
    }


def _run(revision):
    """Invoke the handler; return the configuration handed to the reporter."""
    with patch("index.SaveReportingData") as reporter:
        reporter.return_value.save.return_value = ["saved"]
        response = index.handler(_event(revision), {})
    assert response["statusCode"] == 200, response
    return reporter.call_args.args[2]


@pytest.mark.unit
@patch.dict(os.environ, {"CONFIGURATION_TABLE_NAME": _TABLE, "STACK_NAME": "s"})
@patch("index.ConfigurationManager")
class TestPinnedRead:
    def test_a_pinned_document_reads_its_revision(self, manager):
        manager.return_value.get_merged_pricing.return_value = None
        pinned = MagicMock()
        with patch("index.get_config", return_value=pinned) as get_config:
            assert _run(7) is pinned
        get_config.assert_called_once_with(
            table_name=_TABLE, as_model=True, version=_PROFILE, revision=7
        )

    def test_an_unpinned_document_reads_the_head_as_before(self, manager):
        manager.return_value.get_merged_pricing.return_value = None
        head = MagicMock()
        with patch("index.get_config", return_value=head) as get_config:
            assert _run(None) is head
        get_config.assert_called_once_with(
            table_name=_TABLE, as_model=True, version=_PROFILE
        )

    @pytest.mark.parametrize(
        "error",
        [
            ValueError("r7 is gone"),
            PermissionError("AccessDenied on config_revisions/"),
            ClientError(
                {"Error": {"Code": "SlowDown", "Message": "slow"}}, "GetObject"
            ),
            # Any exception at all: an `except` narrowed to any list of library
            # types (BotoCoreError, OSError, ...) would let this one through.
            type("_AnyFailure", (Exception,), {})("not a library type"),
        ],
        ids=["revision-gone", "access-denied", "s3-error", "any-exception"],
    )
    def test_a_failed_pinned_read_falls_back_to_the_head(self, manager, error):
        manager.return_value.get_merged_pricing.return_value = None
        head = MagicMock()
        with patch("index.get_config", side_effect=[error, head]) as get_config:
            assert _run(7) is head
        assert get_config.call_args_list == [
            call(table_name=_TABLE, as_model=True, version=_PROFILE, revision=7),
            call(table_name=_TABLE, as_model=True, version=_PROFILE),
        ]

    def test_the_fallback_warning_carries_the_traceback(self, manager, caplog):
        manager.return_value.get_merged_pricing.return_value = None
        error = ValueError("r7 is gone")
        with patch("index.get_config", side_effect=[error, MagicMock()]):
            with caplog.at_level(logging.WARNING):
                _run(7)
        [record] = [
            r for r in caplog.records if "Building the report from" in r.getMessage()
        ]
        assert record.levelno == logging.WARNING
        assert record.exc_info is not None and record.exc_info[1] is error

    def test_both_reads_failing_still_saves_without_a_config(self, manager):
        with patch("index.get_config", side_effect=RuntimeError("ddb down")):
            assert _run(7) is None


def _preset():
    for parent in Path(__file__).resolve().parents:
        path = parent / "config_library/unified/lending-package-sample/config.yaml"
        if path.is_file():
            break
    else:
        raise RuntimeError("config_library not found above this file")
    from idp_common.config.merge_utils import merge_config_with_defaults

    body = yaml.safe_load(path.read_text())
    body.pop("description", None)
    body.pop("pricing", None)
    return merge_config_with_defaults(body, pattern="pattern-2", validate=False)


@pytest.mark.unit
def test_the_report_gets_the_pinned_revisions_classes_not_the_heads(monkeypatch):
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", _TABLE)
    monkeypatch.setenv("CONFIGURATION_BUCKET", _BUCKET)
    monkeypatch.setenv("STACK_NAME", "s")
    from idp_common.config.configuration_manager import ConfigurationManager

    with mock_aws():
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=_TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=_BUCKET)
        manager = ConfigurationManager(table_name=_TABLE)
        manager.save_configuration("Config", _preset(), version="default")
        first = _preset()
        manager.save_configuration("Config", first, version=_PROFILE)
        pinned = manager.resolve_published_revision(_PROFILE)
        second = _preset()
        second["classes"] = second["classes"][:1]
        manager.save_configuration("Config", second, version=_PROFILE)
        assert manager.resolve_published_revision(_PROFILE) != pinned
        assert len(first["classes"]) > 1

        config = _run(int(pinned))

    assert len(config.classes) == len(first["classes"])
