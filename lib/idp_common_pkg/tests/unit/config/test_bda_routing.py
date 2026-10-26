# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The BDA routing rule, and the strict reads the processChanges resolver makes.

``bda_routing`` states the queue processor's rule: an unpinned document is processed
under the active profile, and goes down the BDA branch when that profile sets
``use_bda`` and a BDA project is linked to it. The resolver suites drive it through a
real ``ConfigurationManager`` on moto (``tests/unit/lambdas/test_process_changes_bda_mode.py``);
this file pins the rule on its own and the two reads ``raise_errors`` makes strict.

``BdaRouting.bda`` is pinned directly because ``bda_routing`` only ever builds a
routing with a project when BDA was requested, so no input through it can tell
``requested and project`` from ``project`` alone.
"""

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from idp_common.config.configuration_manager import (
    BdaRouting,
    ConfigurationManager,
    bda_routing,
)
from idp_common.config.constants import ACTIVE_POINTER_KEY, CONFIG_TYPE_CONFIG


class _Manager:
    """The three reads the rule makes, recording what each was asked."""

    def __init__(self, use_bda, project, active="active-profile"):
        self.config = (
            type("Config", (), {"use_bda": use_bda})() if use_bda is not None else None
        )
        self.project = project
        self.active = active
        self.asked = []

    def resolve_active_version(self, raise_errors=False):
        self.asked.append(("active", raise_errors))
        return self.active

    def get_merged_configuration(self, version):
        self.asked.append(("config", version))
        return self.config

    def get_bda_project_arn(self, version, raise_errors=False):
        self.asked.append(("project", version, raise_errors))
        return self.project


@pytest.mark.unit
@pytest.mark.parametrize(
    "use_bda, project, bda",
    [
        (True, "arn:p", True),
        (True, None, False),  # asks for BDA with no project: the pipeline
        (True, "", False),
        (False, "arn:p", False),
        (None, "arn:p", False),  # no configuration at all
    ],
)
def test_the_routing_rule(use_bda, project, bda):
    manager = _Manager(use_bda, project)
    routing = bda_routing(manager, "tenant-a")
    assert routing.bda is bda
    assert routing.requested is bool(use_bda)
    assert routing.version == "tenant-a"
    # The project is read for the same profile, and only when BDA is requested.
    expected = [("config", "tenant-a")]
    if use_bda:
        expected.append(("project", "tenant-a", False))
    assert manager.asked == expected


@pytest.mark.unit
@pytest.mark.parametrize("pinned", [None, ""])
def test_an_unpinned_document_takes_the_active_profile(pinned):
    # The queue processor pins the active profile before it routes.
    manager = _Manager(True, "arn:p", active="bda-profile")
    routing = bda_routing(manager, pinned, raise_errors=True)
    assert routing.version == "bda-profile"
    assert manager.asked == [
        ("active", True),
        ("config", "bda-profile"),
        ("project", "bda-profile", True),
    ]


@pytest.mark.unit
def test_a_project_without_a_request_is_not_bda():
    assert BdaRouting("v", requested=False, project_arn="arn:p").bda is False
    assert BdaRouting("v", requested=True, project_arn="arn:p").bda is True


TABLE = "bda-routing-config"


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("CONFIGURATION_BUCKET", raising=False)
    with mock_aws():
        boto3.resource("dynamodb").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ConfigurationManager(table_name=TABLE)


class _FailingTable:
    """A table whose reads fail the way a throttled or denied read does."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def _fail(self, op):
        return ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, op
        )

    def get_item(self, **_):
        raise self._fail("GetItem")

    def scan(self, **_):
        raise self._fail("Scan")


@pytest.mark.unit
class TestStrictReads:
    def test_a_failed_project_read_raises_only_when_asked(self, manager):
        manager.table = _FailingTable(manager.table)
        assert manager.get_bda_project_arn("p") is None  # the lenient default
        with pytest.raises(ClientError):
            manager.get_bda_project_arn("p", raise_errors=True)

    def test_a_failed_pointer_read_falls_back_to_the_scan_even_when_strict(
        self, manager
    ):
        manager.table.put_item(
            Item={"Configuration": f"{CONFIG_TYPE_CONFIG}#lending", "IsActive": True}
        )
        real = manager.table

        class _PointerFails(_FailingTable):
            def scan(self, **kwargs):
                return real.scan(**kwargs)

        manager.table = _PointerFails(real)
        assert manager.resolve_active_version(raise_errors=True) == "lending"

    def test_a_failed_active_profile_read_raises_only_when_asked(self, manager):
        manager.table = _FailingTable(manager.table)
        assert manager.resolve_active_version() == "default"  # the lenient default
        with pytest.raises(ClientError):
            manager.resolve_active_version(raise_errors=True)

    def test_a_failed_fallback_scan_raises_when_asked(self, manager):
        # No pointer item, so resolution falls back to the scan, which fails.
        real = manager.table

        class _ScanFails(_FailingTable):
            def get_item(self, **kwargs):
                return real.get_item(**kwargs)

        manager.table = _ScanFails(real)
        assert manager.resolve_active_version() == "default"
        with pytest.raises(ClientError):
            manager.resolve_active_version(raise_errors=True)

    def test_the_strict_reads_answer_as_the_lenient_ones_when_reads_succeed(
        self, manager
    ):
        manager.table.put_item(
            Item={"Configuration": ACTIVE_POINTER_KEY, "ActiveVersion": "lending"}
        )
        manager.table.put_item(
            Item={"Configuration": f"{CONFIG_TYPE_CONFIG}#p", "BdaProjectArn": "arn:p"}
        )
        assert manager.resolve_active_version(raise_errors=True) == "lending"
        assert manager.get_bda_project_arn("p", raise_errors=True) == "arn:p"
        assert manager.get_bda_project_arn("absent", raise_errors=True) is None
