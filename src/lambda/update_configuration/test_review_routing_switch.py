# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review Routing (`hitl.enabled`) is on in a new stack and carried on every update.

The model default and the system default of the switch are off, so a profile that
does not say otherwise does not hold documents for review. A new stack is the
exception: the deployment writes the switch on into the profiles it creates, when
the stack has a review table and the supplied configuration does not set it. Every
later deployment carries the value each stored profile has, so an upgrade never
turns the switch on and never resets one a user turned off. A headless stack has
no review table (`HITLTableName` is empty) and stays off.

These tests drive `handler` with CloudFormation-shaped events. Engine: moto's
DynamoDB and S3, with the system defaults of this checkout.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from unittest.mock import MagicMock

import boto3
import pytest
import yaml
from moto import mock_aws

# The handler, already loaded with ``cfnresponse`` stubbed. pytest puts this
# directory on ``sys.path`` before importing either module, so the sibling import
# is reliable.
from test_model_mappings import index

TABLE = "review-routing-switch-table"
BUCKET = "review-routing-switch-bucket"
REVIEW_TABLE = "review-routing-switch-review-items"

#: Stack-managed profiles, as the `UpdateDefaultConfig` resource names them.
MANAGED = ("fake-w2", "docsplit")


class _Context:
    def get_remaining_time_in_millis(self) -> int:
        return 25_000


@pytest.fixture
def manager(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
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
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        # The handler's own client was created at import, outside the mock.
        monkeypatch.setattr(index, "s3_client", s3)
        from idp_common.config.configuration_manager import ConfigurationManager

        yield ConfigurationManager()


def _deploy(
    monkeypatch: pytest.MonkeyPatch,
    request_type: str,
    *,
    review_table: Optional[str] = REVIEW_TABLE,
    default: Optional[dict] = None,
    managed: Optional[dict] = None,
) -> None:
    """Run one deployment through `handler` and require it to report success.

    `default` is the stack's preset, passed inline. Each entry of `managed` is
    uploaded and passed as an S3 URI, which is how the template supplies the
    stack-managed profiles. `review_table=None` leaves the property out.
    """
    properties: dict[str, Any] = {
        "ServiceToken": "service-token",
        "Default": default if default is not None else {"notes": "the stack's preset"},
        "PreviousIDPPattern": "",
    }
    if review_table is not None:
        properties[index.REVIEW_TABLE_PROPERTY] = review_table
    for name, config in (managed or {}).items():
        key = f"config_library/managed_config/{name}/config.yaml"
        index.s3_client.put_object(
            Bucket=BUCKET, Key=key, Body=yaml.safe_dump(config).encode("utf-8")
        )
        properties[name] = f"s3://{BUCKET}/{key}"

    # The stub installed by `test_model_mappings` has only `send`.
    monkeypatch.setattr(index.cfnresponse, "SUCCESS", "SUCCESS", raising=False)
    monkeypatch.setattr(index.cfnresponse, "FAILED", "FAILED", raising=False)
    send = MagicMock()
    monkeypatch.setattr(index.cfnresponse, "send", send)
    index.handler(
        {
            "RequestType": request_type,
            "StackId": "stack-id",
            "LogicalResourceId": "UpdateDefaultConfig",
            "ResourceProperties": properties,
        },
        _Context(),
    )
    # The handler reports a failure to CloudFormation instead of raising.
    assert send.call_args.args[2] == "SUCCESS", send.call_args


def _managed(*names: str, **settings: Any) -> dict:
    return {
        name: {"notes": f"{name} preset", "managed": True, **settings} for name in names
    }


def _store(manager, name: str, enabled: bool, *, managed: bool = False) -> None:
    """A profile as an earlier deployment or a user left it."""
    from idp_common.config.models import IDPConfig

    manager.save_configuration(
        "Config",
        IDPConfig(notes=name, managed=managed, hitl={"enabled": enabled}),
        version=name,
    )


def _switch(manager, name: str) -> bool:
    stored = manager.get_configuration("Config", name)
    assert stored is not None, f"profile {name} was not saved"
    return stored.hitl.enabled


@pytest.mark.unit
def test_a_new_stack_turns_review_routing_on_in_every_profile_it_creates(
    manager, monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    resolved: list[Any] = []
    resolve_content = index.resolve_content
    monkeypatch.setattr(
        index,
        "resolve_content",
        lambda content: resolved.append(content) or resolve_content(content),
    )

    _deploy(monkeypatch, "Create", managed=_managed(*MANAGED))

    assert {name: _switch(manager, name) for name in ("default", *MANAGED)} == {
        "default": True,
        "fake-w2": True,
        "docsplit": True,
    }
    for name in ("default", *MANAGED):
        assert (
            f"Review Routing for profile {name}: True (new stack with a review table)"
            in caplog.text
        )
    # The property names a table, not a configuration: every other property was
    # read as a profile and this one was not.
    assert len(resolved) == 1 + len(MANAGED), resolved
    assert REVIEW_TABLE not in resolved


@pytest.mark.unit
@pytest.mark.parametrize("review_table", ["", "  ", None])
def test_a_headless_stack_leaves_it_off(manager, monkeypatch, caplog, review_table):
    """Headless removes the review table, so the nested stack's parameter takes
    its default, an empty string. A stack with nowhere to hold a document must
    not hold one."""
    caplog.set_level(logging.INFO)

    _deploy(
        monkeypatch, "Create", review_table=review_table, managed=_managed("fake-w2")
    )

    assert _switch(manager, "default") is False
    assert _switch(manager, "fake-w2") is False
    assert (
        "Review Routing for profile default: False (left at the system default)"
        in caplog.text
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "request_type, review_table, stored, supplied",
    [
        # Without the supplied value each of these would end the other way.
        pytest.param("Create", REVIEW_TABLE, None, False, id="false-on-a-new-stack"),
        pytest.param("Create", "", None, True, id="true-on-a-headless-stack"),
        pytest.param("Update", REVIEW_TABLE, True, False, id="false-over-stored-true"),
        pytest.param("Update", REVIEW_TABLE, False, True, id="true-over-stored-false"),
    ],
)
def test_a_supplied_configuration_that_sets_it_wins(
    manager, monkeypatch, caplog, request_type, review_table, stored, supplied
):
    caplog.set_level(logging.INFO)
    if stored is not None:
        _store(manager, "default", stored)
        _store(manager, "fake-w2", stored, managed=True)

    _deploy(
        monkeypatch,
        request_type,
        review_table=review_table,
        default={"notes": "preset", "hitl": {"enabled": supplied}},
        managed=_managed("fake-w2", hitl={"enabled": supplied}),
    )

    assert _switch(manager, "default") is supplied
    assert _switch(manager, "fake-w2") is supplied
    assert (
        f"Review Routing for profile default: {supplied} "
        "(set by the supplied configuration)" in caplog.text
    )


@pytest.mark.unit
def test_a_supplied_configuration_decides_for_its_own_profile_only(
    manager, monkeypatch
):
    _deploy(
        monkeypatch,
        "Create",
        default={"notes": "preset", "hitl": {"enabled": False}},
        managed=_managed("fake-w2"),
    )

    assert _switch(manager, "default") is False
    assert _switch(manager, "fake-w2") is True


@pytest.mark.unit
@pytest.mark.parametrize("stored", [False, True])
def test_an_update_keeps_the_stored_value(manager, monkeypatch, caplog, stored):
    """Each profile keeps its own value. `default` is stored the other way, so a
    profile that took the default's value instead of its own would show here."""
    caplog.set_level(logging.INFO)
    _store(manager, "default", not stored)
    _store(manager, "fake-w2", stored, managed=True)

    _deploy(monkeypatch, "Update", managed=_managed("fake-w2"))

    assert _switch(manager, "fake-w2") is stored
    assert _switch(manager, "default") is (not stored)
    assert (
        f"Review Routing for profile fake-w2: {stored} "
        "(carried from the stored profile)" in caplog.text
    )
    # The deployment saved the profile: this is the value it wrote, not one it
    # left untouched.
    newest = manager.list_revisions("fake-w2")[0]
    assert newest["notes"] == "Updated by stack deployment"


@pytest.mark.unit
def test_an_update_never_turns_it_on_for_an_upgraded_stack(manager, monkeypatch):
    """A stack deployed by a release without the switch-on: every profile is off,
    and `default` is stored with no `hitl` block at all. It has a review table,
    and the update still leaves every profile off, a new one included."""
    _store(manager, "default", False)
    _store(manager, "fake-w2", False, managed=True)
    key = {"Configuration": "Config#default"}
    head = manager._decompress_item(manager.table.get_item(Key=key)["Item"])
    head.pop("hitl")
    manager.table.put_item(Item=manager._compress_item(head))

    _deploy(monkeypatch, "Update", managed=_managed(*MANAGED))

    assert {name: _switch(manager, name) for name in ("default", *MANAGED)} == {
        "default": False,
        "fake-w2": False,
        "docsplit": False,
    }


@pytest.mark.unit
@pytest.mark.parametrize("stored_default", [True, False])
def test_a_managed_profile_first_created_by_an_update_takes_the_stored_default(
    manager, monkeypatch, caplog, stored_default
):
    caplog.set_level(logging.INFO)
    _store(manager, "default", stored_default)

    _deploy(monkeypatch, "Update", managed=_managed("docsplit"))

    assert _switch(manager, "docsplit") is stored_default
    assert (
        f"Review Routing for profile docsplit: {stored_default} "
        "(taken from the stored default profile)" in caplog.text
    )
    newest = manager.list_revisions("docsplit")[0]
    assert newest["notes"] == "Created by stack deployment"


@pytest.mark.unit
@pytest.mark.parametrize(
    "stored_default, review_table",
    [
        # Each would end the other way if this Create counted as a new stack.
        pytest.param(False, REVIEW_TABLE, id="off-with-a-review-table"),
        pytest.param(True, "", id="on-without-one"),
    ],
)
def test_a_create_on_a_table_that_already_has_a_default_carries_it(
    manager, monkeypatch, stored_default, review_table
):
    """CloudFormation sends Create when it replaces the custom resource of a
    stack that already runs. The table has a `default`, so it is not a new stack."""
    _store(manager, "default", stored_default)

    _deploy(
        monkeypatch, "Create", review_table=review_table, managed=_managed("docsplit")
    )

    assert _switch(manager, "default") is stored_default
    assert _switch(manager, "docsplit") is stored_default


@pytest.mark.unit
def test_a_pinned_revision_keeps_its_value(manager, monkeypatch):
    """Documents in flight run the revision they were pinned to. A deployment
    records a new revision and rewrites none, and a revision written before the
    setting existed still reads as off, because the model default stays off."""
    from idp_common.config import get_config

    _store(manager, "default", True)  # r1
    _store(manager, "default", False)  # r2: a user turned it off
    _store(manager, "fake-w2", True, managed=True)  # r1
    for name in ("default", "fake-w2"):
        assert manager.revisions.mark_pinned(name, 1) is True
    # fake-w2 r1 as a release that predates `hitl` wrote it.
    predating = manager.revisions.get_body("fake-w2", 1)
    predating.pop("hitl")
    manager.revisions.put_body("fake-w2", 1, predating)

    _deploy(monkeypatch, "Update", managed=_managed("fake-w2"))

    assert manager.resolve_published_revision("default") == 3
    assert [
        get_config(as_model=True, version="default", revision=n).hitl.enabled
        for n in (1, 2, 3)
    ] == [True, False, False]
    assert manager.resolve_published_revision("fake-w2") == 2
    assert "hitl" not in manager.revisions.get_body("fake-w2", 1)
    assert (
        get_config(as_model=True, version="fake-w2", revision=1).hitl.enabled is False
    )
    assert get_config(as_model=True, version="fake-w2", revision=2).hitl.enabled is True


# --- the pieces --------------------------------------------------------------

#: (supplied, stored, first_deployment, review_table, stored_default) -> written.
#: None means the configuration is left as it is.
SEEDING_CASES = [
    # 1. The supplied configuration sets it: left, whatever else is true.
    pytest.param(False, True, True, True, True, None, id="supplied-false"),
    pytest.param(True, False, False, False, False, None, id="supplied-true"),
    # 2. The profile is stored: its value, even on a new stack with a table.
    pytest.param(None, False, True, True, True, False, id="stored-false"),
    pytest.param(None, True, False, False, False, True, id="stored-true"),
    # 3. A new stack with a review table: on.
    pytest.param(None, None, True, True, None, True, id="new-stack"),
    pytest.param(None, None, True, True, False, True, id="new-stack-before-default"),
    # 4. Otherwise the stored default's value.
    pytest.param(None, None, False, True, True, True, id="update-default-on"),
    pytest.param(None, None, False, True, False, False, id="update-default-off"),
    pytest.param(None, None, False, False, True, True, id="headless-default-on"),
    # 5. Nothing applies: left at the system default.
    pytest.param(None, None, True, False, None, None, id="headless-new-stack"),
    pytest.param(None, None, False, True, None, None, id="update-no-default"),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    "supplied, stored, first_deployment, review_table, stored_default, written",
    SEEDING_CASES,
)
@pytest.mark.parametrize(
    "merged",
    [
        pytest.param(
            {"hitl": {"enabled": False, "confidence_threshold": "0.8"}}, id="merged-off"
        ),
        pytest.param(
            {"hitl": {"enabled": True, "confidence_threshold": "0.8"}}, id="merged-on"
        ),
        pytest.param({}, id="no-hitl-block"),
    ],
)
def test_seed_review_routing(
    merged, supplied, stored, first_deployment, review_table, stored_default, written
):
    import copy

    config = copy.deepcopy(merged)

    result = index.seed_review_routing(
        config,
        supplied=supplied,
        stored=stored,
        stored_default=stored_default,
        first_deployment=first_deployment,
        review_table=review_table,
    )

    assert result is written
    if written is None:
        assert config == merged
    else:
        assert config == {
            **merged,
            "hitl": {**merged.get("hitl", {}), "enabled": written},
        }


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw, supplied",
    [
        ({"hitl": {"enabled": True}}, True),
        ({"hitl": {"enabled": False}}, False),
        # A YAML or UI round-trip can deliver the flag as a string.
        ({"hitl": {"enabled": "false"}}, False),
        ({"hitl": {"enabled": "true"}}, True),
        # A file written for an earlier format sets it under `assessment`.
        ({"assessment": {"hitl_enabled": False}}, False),
        ({"assessment": {"hitl_enabled": True}}, True),
        ({"hitl": {"confidence_threshold": "0.7"}}, None),
        ({"hitl": {"enabled": None}}, None),
        ({"hitl": None}, None),
        ({"notes": "no hitl block"}, None),
        ({}, None),
        ("not a configuration", None),
        (None, None),
    ],
)
def test_supplied_review_routing(raw, supplied):
    import copy

    before = copy.deepcopy(raw)

    assert index.supplied_review_routing(raw) is supplied
    assert raw == before, "reading the supplied configuration changed it"


@pytest.mark.unit
def test_stored_review_routing(manager):
    from idp_common.config.models import IDPConfig

    assert index.stored_review_routing(manager, "default") is None  # not stored
    _store(manager, "default", True)
    _store(manager, "claims", False)
    assert index.stored_review_routing(manager, "default") is True
    assert index.stored_review_routing(manager, "claims") is False

    # A dict, a model, and what is not a bool.
    assert index._review_routing_of({"hitl": {"enabled": True}}) is True
    assert index._review_routing_of(IDPConfig()) is False
    assert index._review_routing_of({"hitl": {"enabled": "true"}}) is None
    assert index._review_routing_of({"hitl": {}}) is None
    assert index._review_routing_of(MagicMock()) is None
    assert index._review_routing_of(None) is None


@pytest.mark.unit
def test_an_unreadable_stored_profile_reads_as_absent(caplog):
    manager = MagicMock()
    manager.get_configuration.side_effect = RuntimeError("table unreachable")

    assert index.stored_review_routing(manager, "default") is None
    assert (
        "Could not read Review Routing of the stored profile 'default'" in caplog.text
    )
