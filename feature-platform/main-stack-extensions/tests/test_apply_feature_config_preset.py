"""Unit tests for the apply_feature_config_preset Lambda."""

from __future__ import annotations

import json

import boto3
import pytest
from _helpers import make_appsync_event
from moto import mock_aws

_TABLE = "TestConfigurationTable"


@pytest.fixture
def configuration_table(aws_credentials):
    """A mocked ConfigurationTable (PK: Configuration). Yields the name."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=_TABLE,
            KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "Configuration", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        ).wait_until_exists()
        yield _TABLE


def _preload(monkeypatch, load_lambda):
    monkeypatch.setenv("CONFIGURATION_TABLE", _TABLE)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    return load_lambda("apply_feature_config_preset")


def _get_row(version_name: str):
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    return (
        ddb.Table(_TABLE)
        .get_item(Key={"Configuration": f"Config#{version_name}"})
        .get("Item")
    )


_PRESET = {
    "classes": [{"name": "PA-Administrative"}],
    "rule_validation": {"enabled": True},
    "extraction": {"model": "us.anthropic.claude-sonnet-4-20250514-v1:0"},
}


def _apply_input(**overrides):
    base = {
        "featureId": "sample-health-insurance-review",
        "version": "0.1.0",
        "config": json.dumps(_PRESET),
        "description": "Healthcare claims preset",
    }
    base.update(overrides)
    return base


def test_apply_writes_inactive_version(monkeypatch, configuration_table, load_lambda):
    mod = _preload(monkeypatch, load_lambda)
    event = make_appsync_event("applyFeatureConfigPreset", {"input": _apply_input()})
    result = mod.handler(event, None)

    assert result["featureId"] == "sample-health-insurance-review"
    assert result["configVersionName"] == "sample-health-insurance-review"
    assert result["appliedAt"]

    row = _get_row("sample-health-insurance-review")
    assert row is not None
    assert row["IsActive"] is False
    assert row["Managed"] is False
    assert row["Description"] == "Healthcare claims preset"
    assert row["rule_validation"] == {"enabled": True}
    # Config payload fields are written at top level, not nested.
    assert "classes" in row


def test_apply_does_not_write_full_config_marker(
    monkeypatch, configuration_table, load_lambda
):
    """Regression: the resolver must NOT flag the row `_config_format: full`.

    A configPreset is a SPARSE overlay (here: classes/rule_validation/extraction,
    but no classification/ocr/summarization). idp_common only merges a version
    over system defaults when the row is NOT flagged "full"; a "full" row is
    returned verbatim. Marking a sparse preset "full" skips that merge and the
    classification stage fails at runtime with "No system_prompt found in
    classification configuration". So the stored row must carry no full-config
    marker, leaving it to be merged + auto-migrated on first read.
    """
    mod = _preload(monkeypatch, load_lambda)
    mod.handler(
        make_appsync_event("applyFeatureConfigPreset", {"input": _apply_input()}),
        None,
    )
    row = _get_row("sample-health-insurance-review")
    assert "_config_format" not in row
    # The sparse preset is stored verbatim (no classification section invented).
    assert "classification" not in row


def test_apply_sparse_preset_without_classification_has_no_marker(
    monkeypatch, configuration_table, load_lambda
):
    """The real claims preset shape: only classes + rule_validation. Must be
    stored unmarked so the runtime fills classification/ocr/etc. from defaults."""
    mod = _preload(monkeypatch, load_lambda)
    sparse = {"classes": [{"name": "PA-Administrative"}], "rule_validation": {}}
    mod.handler(
        make_appsync_event(
            "applyFeatureConfigPreset",
            {"input": _apply_input(config=json.dumps(sparse))},
        ),
        None,
    )
    row = _get_row("sample-health-insurance-review")
    assert "_config_format" not in row
    assert "classification" not in row
    assert "classes" in row


def test_apply_accepts_dict_config(monkeypatch, configuration_table, load_lambda):
    """Direct invocations (and some AppSync paths) pass a parsed object."""
    mod = _preload(monkeypatch, load_lambda)
    event = make_appsync_event(
        "applyFeatureConfigPreset", {"input": _apply_input(config=_PRESET)}
    )
    result = mod.handler(event, None)
    assert result["configVersionName"] == "sample-health-insurance-review"
    assert _get_row("sample-health-insurance-review")["rule_validation"] == {
        "enabled": True
    }


def test_apply_is_idempotent_overwrite(monkeypatch, configuration_table, load_lambda):
    mod = _preload(monkeypatch, load_lambda)
    mod.handler(
        make_appsync_event("applyFeatureConfigPreset", {"input": _apply_input()}),
        None,
    )
    created_at = _get_row("sample-health-insurance-review")["CreatedAt"]

    updated = _apply_input(
        config=json.dumps({**_PRESET, "summarization": {"enabled": False}})
    )
    mod.handler(
        make_appsync_event("applyFeatureConfigPreset", {"input": updated}), None
    )

    row = _get_row("sample-health-insurance-review")
    assert row["summarization"] == {"enabled": False}
    assert row["CreatedAt"] == created_at  # preserved across overwrites


def test_apply_preserves_admin_activation(
    monkeypatch, configuration_table, load_lambda
):
    """A stack Update must not flip an admin-activated preset back to inactive."""
    mod = _preload(monkeypatch, load_lambda)
    mod.handler(
        make_appsync_event("applyFeatureConfigPreset", {"input": _apply_input()}),
        None,
    )
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.Table(_TABLE).update_item(
        Key={"Configuration": "Config#sample-health-insurance-review"},
        UpdateExpression="SET IsActive = :t",
        ExpressionAttributeValues={":t": True},
    )

    mod.handler(
        make_appsync_event("applyFeatureConfigPreset", {"input": _apply_input()}),
        None,
    )
    assert _get_row("sample-health-insurance-review")["IsActive"] is True


def test_apply_strips_metadata_fields(monkeypatch, configuration_table, load_lambda):
    mod = _preload(monkeypatch, load_lambda)
    sneaky = {**_PRESET, "IsActive": True, "Managed": True, "_config_storage": "x"}
    mod.handler(
        make_appsync_event(
            "applyFeatureConfigPreset",
            {"input": _apply_input(config=json.dumps(sneaky))},
        ),
        None,
    )
    row = _get_row("sample-health-insurance-review")
    assert row["IsActive"] is False
    assert row["Managed"] is False
    assert "_config_storage" not in row


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"featureId": "Bad Id!"}, "Invalid featureId"),
        ({"version": ""}, "Invalid version"),
        ({"config": "not json"}, "not valid JSON"),
        ({"config": json.dumps(["a", "b"])}, "must be a JSON object"),
        ({"config": json.dumps({})}, "at least one configuration field"),
    ],
)
def test_apply_rejects_invalid_input(
    monkeypatch, configuration_table, load_lambda, overrides, match
):
    mod = _preload(monkeypatch, load_lambda)
    event = make_appsync_event(
        "applyFeatureConfigPreset", {"input": _apply_input(**overrides)}
    )
    with pytest.raises(ValueError, match=match):
        mod.handler(event, None)


def test_remove_deletes_inactive_versions(
    monkeypatch, configuration_table, load_lambda
):
    mod = _preload(monkeypatch, load_lambda)
    for version in ("0.1.0", "0.2.0"):
        mod.handler(
            make_appsync_event(
                "applyFeatureConfigPreset", {"input": _apply_input(version=version)}
            ),
            None,
        )
    # An unrelated feature's preset and the default config must survive.
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.Table(_TABLE).put_item(
        Item={"Configuration": "Config#other-feature-v1.0.0", "IsActive": False}
    )
    ddb.Table(_TABLE).put_item(
        Item={"Configuration": "Config#default", "IsActive": True}
    )

    result = mod.handler(
        make_appsync_event(
            "removeFeatureConfigPreset", {"featureId": "sample-health-insurance-review"}
        ),
        None,
    )
    assert result is True
    assert _get_row("sample-health-insurance-review") is None
    assert _get_row("other-feature-v1.0.0") is not None
    assert _get_row("default") is not None


def test_remove_hands_the_pipeline_back_to_default_then_deletes(
    monkeypatch, configuration_table, load_lambda
):
    """
    An ACTIVE feature profile is not left behind on uninstall.

    Previously it was skipped, on the reasoning that deleting the running
    configuration is dangerous. But the feature's config carries the feature's
    pipeline hooks INLINE, and uninstall deletes those Lambdas — so leaving it
    active means every subsequent document runs against dangling hook ARNs, which
    is worse than the config change. `default` is activated first.
    """
    mod = _preload(monkeypatch, load_lambda)
    mod.handler(
        make_appsync_event("applyFeatureConfigPreset", {"input": _apply_input()}),
        None,
    )
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.Table(_TABLE).put_item(
        Item={"Configuration": "Config#default", "IsActive": False}
    )
    ddb.Table(_TABLE).update_item(
        Key={"Configuration": "Config#sample-health-insurance-review"},
        UpdateExpression="SET IsActive = :t",
        ExpressionAttributeValues={":t": True},
    )

    result = mod.handler(
        make_appsync_event(
            "removeFeatureConfigPreset", {"featureId": "sample-health-insurance-review"}
        ),
        None,
    )
    assert result is True  # still succeeds — uninstall must not fail
    assert _get_row("sample-health-insurance-review") is None, (
        "the feature's profile must not outlive the feature stack: its inline hooks "
        "point at Lambdas this uninstall deleted"
    )
    assert _get_row("default")["IsActive"] is True


def test_remove_keeps_an_active_profile_when_there_is_no_default_to_fall_back_to(
    monkeypatch, configuration_table, load_lambda
):
    """
    The one case where the feature's profile does outlive the stack.

    Deleting the active profile with no `Config#default` to activate would leave
    the deployment with NO active configuration, and document processing fails
    outright. Dangling hooks fail per-document; no configuration fails everything.
    So this refuses, loudly, rather than choosing the larger outage.
    """
    mod = _preload(monkeypatch, load_lambda)
    mod.handler(
        make_appsync_event("applyFeatureConfigPreset", {"input": _apply_input()}),
        None,
    )
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.Table(_TABLE).update_item(
        Key={"Configuration": "Config#sample-health-insurance-review"},
        UpdateExpression="SET IsActive = :t",
        ExpressionAttributeValues={":t": True},
    )

    result = mod.handler(
        make_appsync_event(
            "removeFeatureConfigPreset", {"featureId": "sample-health-insurance-review"}
        ),
        None,
    )
    assert result is True  # uninstall still succeeds
    assert _get_row("sample-health-insurance-review") is not None


def test_remove_sweeps_legacy_per_release_profiles(
    monkeypatch, configuration_table, load_lambda
):
    """
    A stack that installed the feature before #697 has one profile per release.

    Only the version being uninstalled used to be removed, which is how a dev
    stack accumulates twelve orphaned profiles from one uninstalled feature — they
    are `Managed`-flagged in some packs, so the UI will not delete them either.
    """
    mod = _preload(monkeypatch, load_lambda)
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    for legacy in ("0.1.0", "0.2.0", "0.5.3"):
        ddb.Table(_TABLE).put_item(
            Item={
                "Configuration": f"Config#sample-health-insurance-review-v{legacy}",
                "IsActive": False,
                "Managed": True,
            }
        )
    ddb.Table(_TABLE).put_item(
        Item={"Configuration": "Config#default", "IsActive": True}
    )
    # And the current, unsuffixed profile.
    mod.handler(
        make_appsync_event("applyFeatureConfigPreset", {"input": _apply_input()}),
        None,
    )

    mod.handler(
        make_appsync_event(
            "removeFeatureConfigPreset", {"featureId": "sample-health-insurance-review"}
        ),
        None,
    )
    for legacy in ("0.1.0", "0.2.0", "0.5.3"):
        assert _get_row(f"sample-health-insurance-review-v{legacy}") is None, (
            f"legacy per-release profile v{legacy} survived uninstall"
        )
    assert _get_row("sample-health-insurance-review") is None
    assert _get_row("default") is not None


def test_apply_stamps_the_owning_feature(monkeypatch, configuration_table, load_lambda):
    """
    `Config#<featureId>` is indistinguishable by name from a profile an admin
    created, now that the name carries no version. `_feature_id` is the marker
    that says which feature owns it.
    """
    mod = _preload(monkeypatch, load_lambda)
    mod.handler(
        make_appsync_event("applyFeatureConfigPreset", {"input": _apply_input()}),
        None,
    )
    assert (
        _get_row("sample-health-insurance-review")["_feature_id"]
        == "sample-health-insurance-review"
    )


def test_apply_reports_the_revision_when_history_is_available(
    monkeypatch, configuration_table, load_lambda
):
    """
    The response carries `configRevision` so an installer can log which revision
    the upgrade produced. It is None on a stack without revision history (no
    configuration bucket), which is the degraded-but-working path — an install
    must not fail because history could not be recorded.
    """
    mod = _preload(monkeypatch, load_lambda)
    result = mod.handler(
        make_appsync_event("applyFeatureConfigPreset", {"input": _apply_input()}),
        None,
    )
    assert "configRevision" in result


def test_a_second_release_reuses_the_same_profile(
    monkeypatch, configuration_table, load_lambda
):
    """
    The point of #697: upgrading the feature must not mint a second profile.

    A profile is an access-control object — an admin has to add it to every scoped
    user's allowedConfigVersions — so one per release means re-scoping every user
    on every feature release.
    """
    mod = _preload(monkeypatch, load_lambda)
    for version in ("0.1.0", "0.2.0", "0.3.0"):
        mod.handler(
            make_appsync_event(
                "applyFeatureConfigPreset", {"input": _apply_input(version=version)}
            ),
            None,
        )

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    rows = ddb.Table(_TABLE).scan().get("Items") or []
    feature_rows = [
        r["Configuration"]
        for r in rows
        if str(r["Configuration"]).startswith("Config#sample-health-insurance-review")
    ]
    assert feature_rows == ["Config#sample-health-insurance-review"], feature_rows


def test_unknown_field_raises(monkeypatch, configuration_table, load_lambda):
    mod = _preload(monkeypatch, load_lambda)
    with pytest.raises(ValueError, match="Unknown field"):
        mod.handler(make_appsync_event("someOtherField", {}), None)
