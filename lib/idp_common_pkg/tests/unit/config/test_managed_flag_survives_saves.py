# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A save of a stack-managed profile must not write it back as unmanaged (#1613).

`managed` is stored as the profile head's `Managed` attribute rather than in the
compressed body, and `get_raw_configuration` drops every head attribute. Every
caller that reads a profile raw, edits it and saves it therefore passed a body with
no `managed`, the model filled in its `False` default, and the save wrote
`Managed=False` over the stack's `True`: the profile then listed as a user profile
and became deletable in the web UI. The ordinary editor update did the same, because
it builds the profile it applies a delta to from the same raw read.

The rule `save_configuration` now applies is that a save can make a profile
stack-managed (the stack's own presets carry `managed: true`) but never clears the
flag on a profile already stored as managed. The callers below are each driven
through their real save path, against a profile seeded from the real
`config_library/managed_config` presets the way the stack's configuration custom
resource seeds it (`merge_config_with_defaults`, then `save_configuration`).
Engine: moto's DynamoDB and S3; nothing here depends on expression semantics.
"""

from __future__ import annotations

import copy
import importlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List
from unittest.mock import MagicMock

import boto3
import pytest
import yaml
from moto import mock_aws
from pydantic import ValidationError

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import CONFIG_TYPE_CONFIG, DEFAULT_VERSION
from idp_common.config.merge_utils import merge_config_with_defaults

_REPO_ROOT = Path(__file__).resolve().parents[5]
_LIBRARY = _REPO_ROOT / "config_library"
_DEFAULT_PRESET = _LIBRARY / "unified" / "lending-package-sample" / "config.yaml"
_MANAGED_DIR = _LIBRARY / "managed_config"
_UPDATE_CONFIGURATION_DIR = _REPO_ROOT / "src" / "lambda" / "update_configuration"
_DISCOVERY_UPLOAD_DIR = (
    _REPO_ROOT
    / "nested"
    / "api-resolvers"
    / "src"
    / "lambda"
    / "discovery_upload_resolver"
)

TABLE = "managed-flag-table"
BUCKET = "managed-flag-bucket"


def _managed_presets() -> List[str]:
    names = sorted(
        p.name for p in _MANAGED_DIR.iterdir() if (p / "config.yaml").is_file()
    )
    # An empty derivation collects as a skip, not a failure, and would take every
    # parametrised case below with it.
    assert names, f"no managed presets found under {_MANAGED_DIR}"
    return names


MANAGED_PRESETS = _managed_presets()


def _stack_shaped(preset: Path) -> Dict[str, Any]:
    """What `update_configuration`'s handler saves for a preset."""
    body = yaml.safe_load(preset.read_text())
    body.pop("description", None)
    body.pop("pricing", None)
    pattern = "pattern-1" if str(body.get("use_bda")).lower() == "true" else "pattern-2"
    return merge_config_with_defaults(body, pattern=pattern, validate=False)


def _env(monkeypatch) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", TABLE)
    monkeypatch.setenv("CONFIGURATION_BUCKET", BUCKET)


def _create_resources() -> None:
    boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "Configuration", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)


def _seed_default(mgr: ConfigurationManager) -> None:
    mgr.save_configuration(
        CONFIG_TYPE_CONFIG,
        _stack_shaped(_DEFAULT_PRESET),
        version=DEFAULT_VERSION,
        created_by="stack-deployment",
    )


@pytest.fixture
def manager(monkeypatch):
    _env(monkeypatch)
    with mock_aws():
        _create_resources()
        mgr = ConfigurationManager()
        assert mgr.revisions.enabled, "the stack seeds with a revision bucket"
        _seed_default(mgr)
        yield mgr


def _seed_managed(mgr: ConfigurationManager, name: str) -> None:
    body = _stack_shaped(_MANAGED_DIR / name / "config.yaml")
    assert body.get("managed") is True, f"{name} is not a managed preset"
    mgr.save_configuration(
        CONFIG_TYPE_CONFIG,
        body,
        version=name,
        description=f"Managed config for {name}",
        created_by="stack-deployment",
    )
    assert _head(mgr, name)["Managed"] is True, "the seed must be managed"


def _head(mgr: ConfigurationManager, name: str) -> Dict[str, Any]:
    return mgr.table.get_item(Key={"Configuration": f"{CONFIG_TYPE_CONFIG}#{name}"})[
        "Item"
    ]


def _listed_managed(mgr: ConfigurationManager, name: str) -> bool:
    return {v["versionName"]: v["managed"] for v in mgr.list_config_versions()}[name]


def _class(class_id: str) -> Dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": class_id,
        "x-aws-idp-document-type": class_id,
        "type": "object",
        "description": "A class added by the save under test",
        "properties": {"Field": {"type": "string", "description": "a field"}},
    }


# ---------------------------------------------------------------------------
# One entry per caller that reads a profile and saves it. Each runs the caller's
# own code down to `save_configuration`; where the caller's surroundings need an
# AWS service other than DynamoDB and S3, only those surroundings are stubbed.
# ---------------------------------------------------------------------------


def _via_classes_discovery(mgr: ConfigurationManager, name: str) -> None:
    from idp_common.discovery.classes_discovery import ClassesDiscovery

    discovery = object.__new__(ClassesDiscovery)
    discovery.config_manager = mgr
    discovery.version = name
    discovery._merge_and_save_class(_class("DiscoveredClass"))


def _via_blueprint_optimizer(mgr: ConfigurationManager, name: str) -> None:
    from idp_common.bda.blueprint_optimizer import BlueprintOptimizer

    optimizer = object.__new__(BlueprintOptimizer)
    optimizer.config_manager = mgr
    optimizer.blueprint_creator = MagicMock()  # the BDA calls, not the save
    optimizer.blueprint_service = SimpleNamespace(
        transform_bda_blueprint_to_idp_class_schema=lambda _schema: _class(
            "OptimizedClass"
        )
    )
    optimizer._apply_optimized_schema(
        "arn:aws:bedrock:us-east-1:000000000000:blueprint/b",
        "arn:aws:bedrock:us-east-1:000000000000:data-automation-project/p",
        {},
        _class("OptimizedClass"),
        name,
    )


def _via_synthesis_bootstrap(mgr: ConfigurationManager, name: str) -> None:
    from idp_common.synthesis.bootstrap import merge_class_into_version

    merge_class_into_version(_class("BootstrapClass"), name, config_manager=mgr)


def _via_quick_start_bootstrap_tool(mgr: ConfigurationManager, name: str) -> None:
    from idp_common.agents.quick_start.tools.bootstrap_tools import (
        create_config_version_impl,
    )

    result = json.loads(
        create_config_version_impl(json.dumps(_class("QuickStartClass")), name)
    )
    assert result["config_version"] == name


def _via_discovery_upload_resolver(mgr: ConfigurationManager, name: str) -> None:
    sys.path.insert(0, str(_DISCOVERY_UPLOAD_DIR))
    sys.modules.pop("index", None)
    try:
        module = importlib.import_module("index")
        assert Path(module.__file__).parent == _DISCOVERY_UPLOAD_DIR
        assert mgr.get_raw_configuration(CONFIG_TYPE_CONFIG, name).get("classes"), (
            "the replace-mode clear is a no-op on a profile with no classes"
        )
        module._clear_version_schema(name, "classes")
    finally:
        sys.path.remove(str(_DISCOVERY_UPLOAD_DIR))
        sys.modules.pop("index", None)
    assert mgr.get_raw_configuration(CONFIG_TYPE_CONFIG, name)["classes"] == []


def _via_bda_blueprint_service_class_write(
    mgr: ConfigurationManager, name: str
) -> None:
    # Every class write in `BdaBlueprintService` is this one call; the service's
    # BDA project calls around it are not on the save path.
    classes = mgr.get_raw_configuration(CONFIG_TYPE_CONFIG, name).get("classes", [])
    mgr.handle_update_custom_configuration(
        custom_config={"classes": classes + [_class("BdaClass")]}, version=name
    )


def _via_editor_update(mgr: ConfigurationManager, name: str) -> None:
    mgr.handle_update_custom_configuration(
        {"summarization": {"enabled": False}},
        version=name,
        description=f"Managed config for {name}",
    )


def _via_save_raw_configuration(mgr: ConfigurationManager, name: str) -> None:
    raw = mgr.get_raw_configuration(CONFIG_TYPE_CONFIG, name)
    mgr.save_raw_configuration(CONFIG_TYPE_CONFIG, raw, version=name)


def _via_reset_to_default(mgr: ConfigurationManager, name: str) -> None:
    # Copies `default`, which is stored with `Managed=False`, into the profile.
    mgr.handle_update_custom_configuration({"resetToDefault": True}, version=name)


def _via_explicit_false_in_body(mgr: ConfigurationManager, name: str) -> None:
    # `idp-cli config-upload` / the SDK force `managed: false` into every upload.
    raw = mgr.get_raw_configuration(CONFIG_TYPE_CONFIG, name)
    raw["managed"] = False
    mgr.handle_update_custom_configuration(raw, version=name)


def _via_save_configuration_with_model_false(
    mgr: ConfigurationManager, name: str
) -> None:
    raw = mgr.get_raw_configuration(CONFIG_TYPE_CONFIG, name)
    raw["managed"] = False
    mgr.save_configuration(CONFIG_TYPE_CONFIG, raw, version=name)


SAVE_PATHS: Dict[str, Callable[[ConfigurationManager, str], None]] = {
    "classes_discovery": _via_classes_discovery,
    "blueprint_optimizer": _via_blueprint_optimizer,
    "synthesis_bootstrap": _via_synthesis_bootstrap,
    "quick_start_bootstrap_tool": _via_quick_start_bootstrap_tool,
    "discovery_upload_resolver": _via_discovery_upload_resolver,
    "bda_blueprint_service_class_write": _via_bda_blueprint_service_class_write,
    "editor_update": _via_editor_update,
    "save_raw_configuration": _via_save_raw_configuration,
    "reset_to_default": _via_reset_to_default,
    "explicit_false_in_body": _via_explicit_false_in_body,
    "save_configuration_with_managed_false": _via_save_configuration_with_model_false,
}


@pytest.mark.unit
@pytest.mark.parametrize("name", MANAGED_PRESETS)
@pytest.mark.parametrize("path", sorted(SAVE_PATHS))
def test_a_save_keeps_a_managed_profile_managed(manager, path, name):
    _seed_managed(manager, name)
    latest_before = _head(manager, name)["LatestRevision"]

    SAVE_PATHS[path](manager, name)

    head = _head(manager, name)
    assert head["UpdatedAt"], "the save under test must have written the head"
    assert head["Managed"] is True
    assert _listed_managed(manager, name) is True
    assert manager.get_configuration(CONFIG_TYPE_CONFIG, name).managed is True
    # The revision a changing save cuts records the profile as it is stored.
    latest = head["LatestRevision"]
    if latest != latest_before:
        body = manager.get_revision(name, int(latest))
        assert body is not None
        assert body["managed"] is True


@pytest.mark.unit
@pytest.mark.parametrize("name", MANAGED_PRESETS)
def test_every_save_path_writes_the_profile(manager, name):
    """Guard against a vacuous pass above: each path really reaches a write."""
    _seed_managed(manager, name)
    for path, save in sorted(SAVE_PATHS.items()):
        before = _head(manager, name)["UpdatedAt"]
        save(manager, name)
        assert _head(manager, name)["UpdatedAt"] != before, path


@pytest.mark.unit
@pytest.mark.parametrize("name", MANAGED_PRESETS)
def test_save_as_a_new_name_is_not_managed(manager, name):
    """The editor's Save as: the copy of a managed profile is a user profile.

    The UI sends the source profile's raw read merged with the form, so the body
    is built that way here; the second case sends `managed: true` explicitly, as a
    client that sent the validated read back would.
    """
    _seed_managed(manager, name)
    for copy_name, extra in (
        (f"{name}-copy", {}),
        (f"{name}-copy-explicit", {"managed": True}),
    ):
        body = copy.deepcopy(manager.get_raw_configuration(CONFIG_TYPE_CONFIG, name))
        body.update(extra)
        body["saveAsVersion"] = True
        manager.handle_update_custom_configuration(
            body, version=copy_name, description="my copy"
        )

        assert _head(manager, copy_name)["Managed"] is False, copy_name
        assert _listed_managed(manager, copy_name) is False, copy_name
        assert _head(manager, name)["Managed"] is True
        assert _listed_managed(manager, name) is True


@pytest.mark.unit
def test_an_editor_save_cannot_make_a_user_profile_managed(manager):
    manager.handle_update_custom_configuration(
        {"saveAsVersion": True}, version="mine", description="mine"
    )
    assert _head(manager, "mine")["Managed"] is False

    manager.handle_update_custom_configuration(
        {"managed": True, "summarization": {"enabled": False}}, version="mine"
    )
    assert _head(manager, "mine")["Managed"] is False
    assert _listed_managed(manager, "mine") is False

    manager.handle_update_custom_configuration(
        {"managed": True, "saveAsDefault": True}, version="mine"
    )
    assert _head(manager, DEFAULT_VERSION)["Managed"] is False
    assert _head(manager, "mine")["Managed"] is False


@pytest.mark.unit
def test_the_stack_can_still_make_an_existing_profile_managed(manager):
    """A preset that becomes managed takes over a same-named user profile."""
    name = MANAGED_PRESETS[0]
    body = _stack_shaped(_MANAGED_DIR / name / "config.yaml")
    body["managed"] = False
    manager.save_configuration(CONFIG_TYPE_CONFIG, body, version=name)
    assert _head(manager, name)["Managed"] is False

    _seed_managed(manager, name)
    assert _listed_managed(manager, name) is True


@pytest.mark.unit
def test_an_unmanaged_profile_stays_unmanaged_on_save(manager):
    manager.save_configuration(
        CONFIG_TYPE_CONFIG, _stack_shaped(_DEFAULT_PRESET), version="plain"
    )
    _via_editor_update(manager, "plain")
    _via_classes_discovery(manager, "plain")
    assert _head(manager, "plain")["Managed"] is False
    assert _listed_managed(manager, "plain") is False


@pytest.mark.unit
@pytest.mark.parametrize("name", MANAGED_PRESETS)
def test_a_body_saved_without_managed_never_puts_it_in_the_body(manager, name):
    import gzip

    _seed_managed(manager, name)
    _via_classes_discovery(manager, name)
    head = _head(manager, name)
    body = json.loads(gzip.decompress(bytes(head["_compressed_config"].value)))
    assert "managed" not in body, "managed belongs on the head, not in the body"


# ---------------------------------------------------------------------------
# The configuration custom resource's bypass writer (taken on the update that
# migrates a stack from the legacy format).
# ---------------------------------------------------------------------------


@pytest.fixture
def update_configuration_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "cfnresponse", MagicMock())
    monkeypatch.syspath_prepend(str(_UPDATE_CONFIGURATION_DIR))
    sys.modules.pop("index", None)
    module = importlib.import_module("index")
    assert Path(module.__file__).parent == _UPDATE_CONFIGURATION_DIR
    yield module
    sys.modules.pop("index", None)


@pytest.mark.unit
@pytest.mark.parametrize("name", MANAGED_PRESETS)
def test_the_bypass_writer_stores_managed_on_the_head(
    manager, update_configuration_module, name
):
    import gzip

    body = _stack_shaped(_MANAGED_DIR / name / "config.yaml")
    assert body["managed"] is True
    update_configuration_module.save_configuration_bypass_manager(
        "Config", copy.deepcopy(body), version=name
    )

    head = _head(manager, name)
    assert head["Managed"] is True
    stored = json.loads(gzip.decompress(bytes(head["_compressed_config"].value)))
    assert "managed" not in stored, "managed belongs on the head, not in the body"
    assert _listed_managed(manager, name) is True
    assert manager.get_configuration(CONFIG_TYPE_CONFIG, name).managed is True

    # A later bypass write of a body without `managed` keeps the stored flag, the
    # same rule `save_configuration` applies.
    body.pop("managed")
    update_configuration_module.save_configuration_bypass_manager(
        "Config", body, version=name
    )
    assert _head(manager, name)["Managed"] is True
    assert _listed_managed(manager, name) is True


@pytest.mark.unit
def test_the_bypass_writer_stores_an_unmanaged_profile_as_unmanaged(
    manager, update_configuration_module
):
    update_configuration_module.save_configuration_bypass_manager(
        "Config", _stack_shaped(_DEFAULT_PRESET), version="plain"
    )
    assert _head(manager, "plain")["Managed"] is False
    assert _listed_managed(manager, "plain") is False
    assert manager.get_configuration(CONFIG_TYPE_CONFIG, "plain").managed is False


@pytest.mark.unit
def test_the_bypass_writer_keeps_an_unmanaged_profile_unmanaged_on_rewrite(
    manager, update_configuration_module
):
    """Only a stored `True` is kept: an existing head alone is not "managed"."""
    body = _stack_shaped(_DEFAULT_PRESET)
    body.pop("managed", None)
    for _ in range(2):
        update_configuration_module.save_configuration_bypass_manager(
            "Config", copy.deepcopy(body), version="plain"
        )
        assert _head(manager, "plain")["Managed"] is False
    assert _listed_managed(manager, "plain") is False


def _rewrite_head(mgr: ConfigurationManager, name: str, **changes: Any) -> None:
    """Rewrite a stored head the way an older writer left it."""
    item = mgr._decompress_item(_head(mgr, name))
    for key, value in changes.items():
        if value is None:
            item.pop(key, None)
        else:
            item[key] = value
    mgr.table.put_item(Item=mgr._compress_item(item))


@pytest.mark.unit
@pytest.mark.parametrize("stored", ["False", "false"])
def test_a_string_valued_unmanaged_head_is_not_made_managed(manager, stored):
    manager.save_configuration(
        CONFIG_TYPE_CONFIG, _stack_shaped(_DEFAULT_PRESET), version="plain"
    )
    _rewrite_head(manager, "plain", Managed=stored)
    assert manager.get_configuration(CONFIG_TYPE_CONFIG, "plain").managed is False

    _via_save_raw_configuration(manager, "plain")

    assert _head(manager, "plain")["Managed"] is False


@pytest.mark.unit
@pytest.mark.parametrize("name", MANAGED_PRESETS)
def test_a_head_managed_only_by_a_body_key_stays_managed_and_gains_the_attribute(
    manager, update_configuration_module, name
):
    """The shape the bypass writer used to leave: `managed: true` in the body
    and no `Managed` attribute. The validated read calls it managed, so a save
    keeps it so, and writes the attribute the listing reads.

    A raw read carries that body key, so a raw-read-then-save passes either way;
    the reset to `default` is the case that tells the two readings apart, since
    its body says `managed: false`."""
    for save in (
        _via_save_raw_configuration,
        _via_reset_to_default,
        lambda mgr, n: update_configuration_module.save_configuration_bypass_manager(
            "Config", _stack_shaped(_DEFAULT_PRESET), version=n
        ),
    ):
        _seed_managed(manager, name)
        _rewrite_head(manager, name, Managed=None, managed=True)
        assert "Managed" not in _head(manager, name)
        assert manager.get_configuration(CONFIG_TYPE_CONFIG, name).managed is True

        save(manager, name)

        assert _head(manager, name)["Managed"] is True
        assert _listed_managed(manager, name) is True


@pytest.mark.unit
@pytest.mark.parametrize("name", MANAGED_PRESETS)
def test_a_stack_save_without_managed_does_not_unmanage(manager, name):
    """The explicit-unmanage rule holds for the stack's own saves as well."""
    _seed_managed(manager, name)
    body = _stack_shaped(_MANAGED_DIR / name / "config.yaml")
    body["managed"] = False
    manager.save_configuration(
        CONFIG_TYPE_CONFIG, body, version=name, created_by="stack-deployment"
    )
    assert _head(manager, name)["Managed"] is True
    assert _listed_managed(manager, name) is True


# Stored values for the flag, spelled every way a DynamoDB writer could leave
# one. The expected result is not listed beside them: it is whatever the
# validated read makes of the value, because agreeing with that read is the rule.
_STORED_FLAG_VALUES = [
    True,
    False,
    "true",
    "True",
    "TRUE",
    "false",
    "False",
    "1",
    "0",
    "yes",
    "no",
    "on",
    "off",
    Decimal(1),
    Decimal(0),
]
# Values the validated read cannot parse. Such a profile cannot be read at all, so
# no reading of it is "managed", and the bypass writer takes the body's value.
_UNREADABLE_FLAG_VALUES = ["maybe", "", Decimal(2)]


_FLAG_LOCATIONS = {
    "attribute": {"Managed": True},
    # The shape the bypass writer used to leave: a body key and no attribute.
    "body_key": {"Managed": None, "managed": True},
}


def _store_flag(mgr: ConfigurationManager, name: str, where: str, value: Any) -> None:
    _rewrite_head(
        mgr,
        name,
        **{k: (value if v else None) for k, v in _FLAG_LOCATIONS[where].items()},
    )


@pytest.mark.unit
@pytest.mark.parametrize("where", sorted(_FLAG_LOCATIONS))
@pytest.mark.parametrize("stored", _STORED_FLAG_VALUES, ids=repr)
def test_both_writers_read_a_stored_flag_as_the_validated_read_does(
    manager, update_configuration_module, stored, where
):
    """`save_configuration` and the bypass writer must agree with the read."""
    body = _stack_shaped(_DEFAULT_PRESET)
    body.pop("managed", None)
    expected_values = set()
    for save in (
        lambda n: manager.save_configuration(
            CONFIG_TYPE_CONFIG, copy.deepcopy(body), version=n
        ),
        lambda n: update_configuration_module.save_configuration_bypass_manager(
            "Config", copy.deepcopy(body), version=n
        ),
    ):
        manager.save_configuration(CONFIG_TYPE_CONFIG, copy.deepcopy(body), version="p")
        _store_flag(manager, "p", where, stored)
        expected = manager.get_configuration(CONFIG_TYPE_CONFIG, "p").managed
        expected_values.add(expected)

        save("p")

        assert _head(manager, "p")["Managed"] is expected
    assert len(expected_values) == 1


@pytest.mark.unit
def test_the_stored_flag_values_exercise_both_outcomes(manager):
    """Guard: the list above must contain values read both ways."""
    body = _stack_shaped(_DEFAULT_PRESET)
    seen = set()
    for stored in _STORED_FLAG_VALUES:
        manager.save_configuration(CONFIG_TYPE_CONFIG, copy.deepcopy(body), version="p")
        _rewrite_head(manager, "p", Managed=stored)
        seen.add(manager.get_configuration(CONFIG_TYPE_CONFIG, "p").managed)
    assert seen == {True, False}


@pytest.mark.unit
@pytest.mark.parametrize("stored", _UNREADABLE_FLAG_VALUES, ids=repr)
def test_the_bypass_writer_does_not_read_an_unparseable_flag_as_managed(
    manager, update_configuration_module, stored
):
    body = _stack_shaped(_DEFAULT_PRESET)
    body.pop("managed", None)
    manager.save_configuration(CONFIG_TYPE_CONFIG, copy.deepcopy(body), version="p")
    _rewrite_head(manager, "p", Managed=stored)
    with pytest.raises(ValidationError):
        manager.get_configuration(CONFIG_TYPE_CONFIG, "p")

    update_configuration_module.save_configuration_bypass_manager(
        "Config", copy.deepcopy(body), version="p"
    )

    assert _head(manager, "p")["Managed"] is False
