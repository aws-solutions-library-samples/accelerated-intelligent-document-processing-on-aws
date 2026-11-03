# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A deployment resolves user profiles against the `default` it just saved (#1782).

Settings a deployment adds to `default` reach a user profile with revision history
only through a revision, because documents run the profile's published revision.
`get_merged_configuration` records that revision, and this sweep is what makes it
happen at the upgrade rather than whenever something able to record revisions
next reads the profile. Engine: moto's DynamoDB and S3.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import boto3
import pytest
from moto import mock_aws

TABLE = "deploy-sweep-table"
BUCKET = "deploy-sweep-bucket"


def _load_handler() -> ModuleType:
    sys.modules.setdefault("cfnresponse", SimpleNamespace(send=lambda *a, **k: None))
    path = Path(__file__).resolve().parent / "index.py"
    spec = importlib.util.spec_from_file_location("update_configuration_index", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


index = _load_handler()


class _Context:
    def __init__(self, remaining_ms: int = 25_000):
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_ms


@pytest.fixture
def manager(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "CONFIGURATION_TABLE_NAME": TABLE,
        "CONFIGURATION_BUCKET": BUCKET,
    }.items():
        monkeypatch.setenv(key, value)
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
        from idp_common.config.configuration_manager import ConfigurationManager
        from idp_common.config.models import IDPConfig

        mgr = ConfigurationManager()
        # `default` as an upgrade leaves it: a setting older profiles lack.
        mgr.save_configuration(
            "Config", IDPConfig(summarization={"enabled": False}), version="default"
        )
        yield mgr


def _profile_predating_summarization(mgr, name: str) -> None:
    """A profile and its r1 saved before `summarization` existed."""
    from idp_common.config.configuration_manager import _stored_content_hash
    from idp_common.config.models import IDPConfig

    mgr.save_configuration("Config", IDPConfig(notes=name), version=name)
    key = {"Configuration": f"Config#{name}"}
    head = mgr._decompress_item(mgr.table.get_item(Key=key)["Item"])
    head.pop("summarization")
    mgr.table.put_item(Item=mgr._compress_item(head))
    body = mgr.revisions.get_body(name, 1)
    body.pop("summarization")
    mgr.revisions.put_body(name, 1, body)
    mgr.revisions.update_entry(name, 1, storedHash=_stored_content_hash(head))


@pytest.mark.unit
def test_a_profile_with_history_gets_the_resolution_as_its_published_revision(
    manager,
):
    from idp_common.config import get_config
    from idp_common.config.configuration_manager import (
        RESOLVED_AGAINST_DEFAULT_NOTES,
    )

    _profile_predating_summarization(manager, "claims")

    assert index.resolve_user_profiles_against_default(manager, _Context()) == 1

    assert manager.resolve_published_revision("claims") == 2
    newest = manager.list_revisions("claims")[0]
    assert newest["createdBy"] == "system"
    assert newest["notes"] == RESOLVED_AGAINST_DEFAULT_NOTES
    runs = get_config(as_model=True, version="claims", revision=2)
    assert runs.summarization.enabled is False
    assert manager.head_matches_published("claims") is True


@pytest.mark.unit
def test_a_second_deployment_records_nothing_more(manager):
    _profile_predating_summarization(manager, "claims")
    index.resolve_user_profiles_against_default(manager, _Context())
    index.resolve_user_profiles_against_default(manager, _Context())
    assert manager.resolve_published_revision("claims") == 2


@pytest.mark.unit
def test_default_managed_and_history_free_profiles_are_skipped(manager, monkeypatch):
    from idp_common.config.models import IDPConfig

    manager.save_configuration(
        "Config", IDPConfig(notes="m", managed=True), version="stack-managed"
    )
    read = []
    monkeypatch.setattr(
        manager, "get_merged_configuration", lambda name: read.append(name)
    )
    monkeypatch.setattr(
        manager,
        "list_config_versions",
        lambda: [
            {"versionName": "default", "publishedRevision": 1},
            {"versionName": "stack-managed", "managed": True, "publishedRevision": 1},
            {"versionName": "no-history", "publishedRevision": None},
            {"versionName": "claims", "publishedRevision": 3},
        ],
    )
    assert index.resolve_user_profiles_against_default(manager, _Context()) == 1
    assert read == ["claims"]


@pytest.mark.unit
def test_the_sweep_stops_in_time_for_the_response(manager, monkeypatch):
    read = []
    monkeypatch.setattr(
        manager, "get_merged_configuration", lambda name: read.append(name)
    )
    monkeypatch.setattr(
        manager,
        "list_config_versions",
        lambda: [{"versionName": f"p{i}", "publishedRevision": 1} for i in range(3)],
    )
    low = _Context(remaining_ms=index.SWEEP_RESERVED_MS - 1)
    assert index.resolve_user_profiles_against_default(manager, low) == 0
    assert read == []


@pytest.mark.unit
def test_a_failing_profile_never_fails_the_deployment(manager, monkeypatch):
    def boom(name):
        raise RuntimeError(f"{name} unreadable")

    monkeypatch.setattr(manager, "get_merged_configuration", boom)
    monkeypatch.setattr(
        manager,
        "list_config_versions",
        lambda: [{"versionName": "claims", "publishedRevision": 1}],
    )
    assert index.resolve_user_profiles_against_default(manager, _Context()) == 0
    monkeypatch.setattr(
        manager, "list_config_versions", lambda: (_ for _ in ()).throw(RuntimeError())
    )
    assert index.resolve_user_profiles_against_default(manager, _Context()) == 0


@pytest.mark.unit
def test_the_create_and_update_path_runs_the_sweep_before_reporting_success():
    """Source-level: driving `handler` end to end needs a full stack-shaped event,
    and what matters here is only that the sweep is on the success path and ahead
    of the response, so a deployment cannot report success before it runs."""
    import ast

    tree = ast.parse((Path(__file__).resolve().parent / "index.py").read_text())
    handler = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "handler"
    )
    calls = [
        n
        for n in ast.walk(handler)
        if isinstance(n, ast.Call)
        and isinstance(n.func, (ast.Name, ast.Attribute))
        and getattr(n.func, "id", getattr(n.func, "attr", None))
        in ("resolve_user_profiles_against_default", "send")
    ]
    names = [getattr(c.func, "id", getattr(c.func, "attr", "")) for c in calls]
    sweep = [
        c for c, n in zip(calls, names) if n == "resolve_user_profiles_against_default"
    ]
    assert len(sweep) == 1, names
    successes = [
        c
        for c, n in zip(calls, names)
        if n == "send"
        and any(isinstance(a, ast.Attribute) and a.attr == "SUCCESS" for a in c.args)
    ]
    assert successes, "the handler sends no SUCCESS response"
    assert sweep[0].lineno < min(c.lineno for c in successes)
