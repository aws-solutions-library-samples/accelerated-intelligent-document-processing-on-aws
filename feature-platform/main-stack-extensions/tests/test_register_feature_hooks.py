"""Unit tests for the register_feature_hooks Lambda.

Focused on active-version resolution (issue #599). The resolver writes a
feature's hooks INLINE into the active config version's row, so resolving the
wrong version writes them somewhere the dispatcher never reads: registration
reports success and the hooks never fire.

The original implementation resolved the active row with a filtered `Scan`
bounded by `Limit=1`. DynamoDB applies `Limit` to the items it EXAMINES, not the
items matching `FilterExpression`, so it found the active row only when that row
happened to be the very first item examined — i.e. almost never on a table with
more than a handful of versions.
"""

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
    return load_lambda("register_feature_hooks")


def _table():
    return boto3.resource("dynamodb", region_name="us-east-1").Table(_TABLE)


def _seed_versions(active_version: str, filler: int = 40) -> None:
    """Write `filler` inactive Config# rows plus one active row.

    The active row is written LAST and given a name that sorts late, so a caller
    that trusts a single scan page is unlikely to see it. Each filler row
    carries a payload chunk so the rows are not trivially small — an unprojected
    scan burns its 1MB page budget on config bodies, which is what makes this
    fail on real stacks at ~35 versions.
    """
    table = _table()
    with table.batch_writer() as batch:
        for i in range(filler):
            batch.put_item(
                Item={
                    "Configuration": f"Config#v{i:03d}",
                    "IsActive": False,
                    "_config_format": "full",
                    "classes": [{"name": f"Class{i}", "description": "x" * 2000}],
                }
            )
    table.put_item(
        Item={
            "Configuration": f"Config#{active_version}",
            "IsActive": True,
            "_config_format": "full",
            "classes": [{"name": "PA-Administrative"}],
        }
    )


_HOOK = {
    "point": "postRuleValidation",
    "arn": "arn:aws:lambda:us-east-1:123456789012:function:claims-hook",
    "order": 50,
    "onError": "continue",
    "enabled": True,
}


def _register_event(feature_id: str = "claims-pack", hooks=None):
    return make_appsync_event(
        "registerFeatureHooks",
        {"input": {"featureId": feature_id, "hooks": hooks or [_HOOK]}},
    )


def test_hooks_register_into_the_active_version_not_default(
    monkeypatch, configuration_table, load_lambda
):
    """The regression: with 40 inactive versions present, the hook must land in
    the ACTIVE row. Before the fix `Limit=1` resolved to 'default', so the hook
    was written into Config#default and the dispatcher — reading the active
    version — never saw it."""
    mod = _preload(monkeypatch, load_lambda)
    _seed_versions("zz-claims-pack-v0.4.0", filler=40)

    result = mod.handler(_register_event(), None)
    assert result["hookCount"] == 1

    active = _table().get_item(Key={"Configuration": "Config#zz-claims-pack-v0.4.0"})[
        "Item"
    ]
    hooks = active["rule_validation"]["postHook"]
    assert [h["featureId"] for h in hooks] == ["claims-pack"]
    assert hooks[0]["arn"] == _HOOK["arn"]

    # And nothing was written into Config#default.
    assert "Item" not in _table().get_item(Key={"Configuration": "Config#default"})


def test_resolve_active_version_pages_past_the_first_scan_page(
    monkeypatch, configuration_table, load_lambda
):
    """Direct assertion on the resolver, independent of the write path."""
    mod = _preload(monkeypatch, load_lambda)
    _seed_versions("zz-claims-pack-v0.4.0", filler=40)
    assert mod._resolve_active_version(_table()) == "zz-claims-pack-v0.4.0"


def test_unregister_clears_hooks_from_the_active_version(
    monkeypatch, configuration_table, load_lambda
):
    """Unregister resolves the active version the same way; if it resolved
    'default' it would leave the real hooks in place while reporting success."""
    mod = _preload(monkeypatch, load_lambda)
    _seed_versions("zz-claims-pack-v0.4.0", filler=40)
    mod.handler(_register_event(), None)

    assert mod.handler(
        make_appsync_event("unregisterFeatureHooks", {"featureId": "claims-pack"}),
        None,
    )
    active = _table().get_item(Key={"Configuration": "Config#zz-claims-pack-v0.4.0"})[
        "Item"
    ]
    assert active["rule_validation"]["postHook"] == []


def test_other_features_hooks_survive_registration(
    monkeypatch, configuration_table, load_lambda
):
    """Two features registering into a late active row must coexist — this is
    the behavior a resolver that silently fell back to 'default' would appear
    to satisfy while writing to the wrong row."""
    mod = _preload(monkeypatch, load_lambda)
    _seed_versions("zz-claims-pack-v0.4.0", filler=40)
    mod.handler(_register_event("claims-pack"), None)
    mod.handler(_register_event("pii-anonymizer"), None)

    active = _table().get_item(Key={"Configuration": "Config#zz-claims-pack-v0.4.0"})[
        "Item"
    ]
    ids = {h["featureId"] for h in active["rule_validation"]["postHook"]}
    assert ids == {"claims-pack", "pii-anonymizer"}


def test_no_active_version_falls_back_to_default(
    monkeypatch, configuration_table, load_lambda
):
    """A genuine "nothing is active" table still resolves to 'default' — the
    host always seeds Config#default — rather than raising."""
    mod = _preload(monkeypatch, load_lambda)
    table = _table()
    for i in range(15):
        table.put_item(Item={"Configuration": f"Config#v{i:03d}", "IsActive": False})
    assert mod._resolve_active_version(table) == "default"


def test_register_fails_loudly_when_the_resolved_row_is_absent(
    monkeypatch, configuration_table, load_lambda
):
    """No rows at all: resolution yields 'default', which does not exist, so
    registration must raise rather than silently create a stray row."""
    mod = _preload(monkeypatch, load_lambda)
    with pytest.raises(RuntimeError, match="not found"):
        mod.handler(_register_event(), None)


def test_invalid_hook_point_is_rejected(monkeypatch, configuration_table, load_lambda):
    mod = _preload(monkeypatch, load_lambda)
    _seed_versions("zz-claims-pack-v0.4.0", filler=2)
    bad = dict(_HOOK, point="postBananas")
    with pytest.raises(ValueError, match="Invalid hook point"):
        mod.handler(_register_event(hooks=[bad]), None)


def test_compressed_active_row_is_decompressed_before_hooks_are_added(
    monkeypatch, configuration_table, load_lambda
):
    """A compressed active row must round-trip: the resolver rewrites it inline,
    so the pre-existing config body has to survive."""
    import base64
    import gzip

    mod = _preload(monkeypatch, load_lambda)
    _seed_versions("zz-claims-pack-v0.4.0", filler=20)
    payload = {"classes": [{"name": "PA-Administrative"}], "ocr": {"enabled": True}}
    _table().put_item(
        Item={
            "Configuration": "Config#zz-claims-pack-v0.4.0",
            "IsActive": True,
            "_config_storage": "compressed",
            "_compressed_config": base64.b64encode(
                gzip.compress(json.dumps(payload).encode("utf-8"))
            ).decode("utf-8"),
        }
    )

    mod.handler(_register_event(), None)
    active = _table().get_item(Key={"Configuration": "Config#zz-claims-pack-v0.4.0"})[
        "Item"
    ]
    assert active["ocr"]["enabled"] is True
    assert active["classes"] == [{"name": "PA-Administrative"}]
    assert active["rule_validation"]["postHook"][0]["featureId"] == "claims-pack"


# ---------------------------------------------------------------------------
# Flat single-hook points (preprocessing / postprocessing)
# ---------------------------------------------------------------------------

_FLAT_ARN = "arn:aws:lambda:us-east-1:123456789012:function:deliver-hook"


def _flat_hook(point: str, arn: str = _FLAT_ARN, **over):
    h = {"point": point, "arn": arn, "onError": "continue", "enabled": True}
    h.update(over)
    return h


@pytest.mark.parametrize("point", ["preprocessing", "postprocessing"])
def test_flat_point_registers_onto_the_section_itself(
    monkeypatch, configuration_table, load_lambda, point
):
    """`preprocessing`/`postprocessing` are standalone SINGLE-hook sections: the
    ARN goes directly on the section, not into a `postHook` list. Registering
    either previously failed outright ("Invalid hook point"), which is why the
    PII Anonymizer had to bake its ARN into a config preset instead."""
    mod = _preload(monkeypatch, load_lambda)
    _seed_versions("zz-active-v1", filler=2)

    result = mod.handler(_register_event("deliver", [_flat_hook(point)]), None)
    assert result["hookCount"] == 1

    active = _table().get_item(Key={"Configuration": "Config#zz-active-v1"})["Item"]
    section = active[point]
    assert section["arn"] == _FLAT_ARN
    assert section["featureId"] == "deliver"
    assert section["enabled"] is True
    # No list is created — the dispatcher reads the flat fields.
    assert "postHook" not in section


@pytest.mark.parametrize("point", ["preprocessing", "postprocessing"])
def test_flat_point_registration_preserves_preset_args(
    monkeypatch, configuration_table, load_lambda, point
):
    """A feature's config preset typically ships the section's `args` and leaves
    the ARN blank until its stack exists. Registration must fill in the ARN
    without discarding those args."""
    mod = _preload(monkeypatch, load_lambda)
    _seed_versions("zz-active-v1", filler=2)
    table = _table()
    item = table.get_item(Key={"Configuration": "Config#zz-active-v1"})["Item"]
    item[point] = {"args": [{"key": "mode", "value": "deliver_and_notify"}]}
    table.put_item(Item=item)

    mod.handler(_register_event("deliver", [_flat_hook(point)]), None)

    section = table.get_item(Key={"Configuration": "Config#zz-active-v1"})["Item"][
        point
    ]
    assert section["args"] == [{"key": "mode", "value": "deliver_and_notify"}]
    assert section["arn"] == _FLAT_ARN


@pytest.mark.parametrize("point", ["preprocessing", "postprocessing"])
def test_flat_point_unregister_clears_only_its_own_hook(
    monkeypatch, configuration_table, load_lambda, point
):
    """Uninstall disables the section and drops the ARN, but leaves `args` so a
    re-install restores the previous behavior."""
    mod = _preload(monkeypatch, load_lambda)
    _seed_versions("zz-active-v1", filler=2)
    mod.handler(_register_event("deliver", [_flat_hook(point)]), None)

    mod.handler(
        make_appsync_event("unregisterFeatureHooks", {"featureId": "deliver"}), None
    )
    section = _table().get_item(Key={"Configuration": "Config#zz-active-v1"})["Item"][
        point
    ]
    assert section["enabled"] is False
    assert section["arn"] is None
    assert "args" in section


@pytest.mark.parametrize("point", ["preprocessing", "postprocessing"])
def test_flat_point_refuses_to_hijack_another_features_hook(
    monkeypatch, configuration_table, load_lambda, point
):
    """A flat point holds exactly ONE hook. Silently overwriting it would
    disable the owning feature with no signal, so registration fails loudly."""
    mod = _preload(monkeypatch, load_lambda)
    _seed_versions("zz-active-v1", filler=2)
    mod.handler(_register_event("pii-anonymizer", [_flat_hook(point)]), None)

    with pytest.raises(ValueError, match="already holds a hook"):
        mod.handler(
            _register_event("other-feature", [_flat_hook(point, arn=_FLAT_ARN + "2")]),
            None,
        )
    # The original owner survives untouched.
    section = _table().get_item(Key={"Configuration": "Config#zz-active-v1"})["Item"][
        point
    ]
    assert section["featureId"] == "pii-anonymizer"
    assert section["arn"] == _FLAT_ARN


@pytest.mark.parametrize("point", ["preprocessing", "postprocessing"])
def test_flat_point_re_registration_by_the_same_owner_is_idempotent(
    monkeypatch, configuration_table, load_lambda, point
):
    """A stack Update re-invokes registration; the same feature must be able to
    refresh its own ARN."""
    mod = _preload(monkeypatch, load_lambda)
    _seed_versions("zz-active-v1", filler=2)
    mod.handler(_register_event("deliver", [_flat_hook(point)]), None)

    new_arn = _FLAT_ARN + "-v2"
    result = mod.handler(
        _register_event("deliver", [_flat_hook(point, arn=new_arn)]), None
    )
    assert result["hookCount"] == 1
    section = _table().get_item(Key={"Configuration": "Config#zz-active-v1"})["Item"][
        point
    ]
    assert section["arn"] == new_arn
    assert section["featureId"] == "deliver"


def test_flat_and_list_points_register_together(
    monkeypatch, configuration_table, load_lambda
):
    """One feature may own the flat postprocessing hook AND contribute a
    post-step hook; both shapes must be written in a single call."""
    mod = _preload(monkeypatch, load_lambda)
    _seed_versions("zz-active-v1", filler=2)

    result = mod.handler(
        _register_event("deliver", [_flat_hook("postprocessing"), _HOOK]), None
    )
    assert result["hookCount"] == 2
    item = _table().get_item(Key={"Configuration": "Config#zz-active-v1"})["Item"]
    assert item["postprocessing"]["arn"] == _FLAT_ARN
    assert item["rule_validation"]["postHook"][0]["featureId"] == "deliver"


def test_unregister_clears_a_stale_flat_arn_that_would_fail_every_document(
    monkeypatch, configuration_table, load_lambda
):
    """The reason unregister clears a flat section rather than leaving it.

    A flat hook is invoked by ARN. Left behind after the owning feature's stack
    is deleted, that ARN names a Lambda that no longer exists — and the PII
    Anonymizer's shipped preset sets `onError: fail`, so the dispatcher raises
    and the workflow ends in its terminal `PreprocessingHookFailed` state. Every
    subsequent document would fail until an admin hand-edited the config.

    Pins the fail-safe end state: disabled, no ARN, args retained for re-install.
    """
    mod = _preload(monkeypatch, load_lambda)
    _seed_versions("zz-pii-v1", filler=2)
    table = _table()
    item = table.get_item(Key={"Configuration": "Config#zz-pii-v1"})["Item"]
    item["preprocessing"] = {
        "enabled": True,
        "featureId": "pii-anonymizer",
        "arn": "arn:aws:lambda:us-east-1:123456789012:function:PiiHook",
        "onError": "fail",
        "args": [{"key": "mode", "value": "redactcopy_and_stop"}],
    }
    table.put_item(Item=item)

    mod.handler(
        make_appsync_event("unregisterFeatureHooks", {"featureId": "pii-anonymizer"}),
        None,
    )

    pp = table.get_item(Key={"Configuration": "Config#zz-pii-v1"})["Item"][
        "preprocessing"
    ]
    # Both conditions matter: `enabled: False` alone is what the dispatcher
    # checks, and a null arn means even a re-enabled section cannot invoke the
    # deleted function.
    assert pp["enabled"] is False
    assert pp["arn"] is None
    assert pp["args"] == [{"key": "mode", "value": "redactcopy_and_stop"}]


def test_unregister_leaves_another_features_flat_hook_running(
    monkeypatch, configuration_table, load_lambda
):
    """Uninstalling feature A must not disable feature B's flat hook — the clear
    is scoped to the recorded owner."""
    mod = _preload(monkeypatch, load_lambda)
    _seed_versions("zz-active-v1", filler=2)
    mod.handler(_register_event("pii-anonymizer", [_flat_hook("preprocessing")]), None)

    mod.handler(
        make_appsync_event("unregisterFeatureHooks", {"featureId": "other-feature"}),
        None,
    )
    pp = _table().get_item(Key={"Configuration": "Config#zz-active-v1"})["Item"][
        "preprocessing"
    ]
    assert pp["enabled"] is True
    assert pp["arn"] == _FLAT_ARN
    assert pp["featureId"] == "pii-anonymizer"


# --------------------------------------------------------------------------
# Registering at a hook point the active configuration's processing mode cannot
# reach (#982).
#
# `postOcr`, `postClassification` and `postExtraction` are states on the Pipeline
# branch of the state machine only — BDA does OCR, classification and extraction
# in one invocation, so there is no separate step to hook after. A registration
# there under `use_bda: true` used to succeed silently: the config record showed
# the hook, the UI showed the hook, and the execution history showed nothing,
# because the dispatcher was never invoked for that point.
#
# `onError: fail` is refused (it declares a gate that cannot gate); any other
# policy is advisory and is accepted with a warning.
# --------------------------------------------------------------------------

_OCR_POINT_HOOK = {
    "point": "postOcr",
    "arn": "arn:aws:lambda:us-east-1:123456789012:function:redact-hook",
    "order": 50,
    "onError": "fail",
    "enabled": True,
}


def _seed_bda_active(version: str = "zz-bda-v1", use_bda=True) -> None:
    """An ACTIVE config row in BDA mode.

    `use_bda` is parameterised because config values are written STRINGIFIED into
    DynamoDB, so `"true"` is as ordinary as `True` and both must be understood.
    """
    _table().put_item(
        Item={
            "Configuration": f"Config#{version}",
            "IsActive": True,
            "_config_format": "full",
            "use_bda": use_bda,
            "classes": [{"name": "Invoice"}],
        }
    )


def test_gating_hook_at_a_bda_unreachable_point_is_refused(
    monkeypatch, configuration_table, load_lambda
):
    """The fail-open closed: a gate that cannot fire is an error, not a silence.

    Accepting this registration is how an operator ends up with a PII-redaction
    gate that never runs and a document that completes un-redacted, with nothing
    anywhere to say so (#982). Failing the feature install instead is loud,
    immediate and fixable in one field.
    """
    mod = _preload(monkeypatch, load_lambda)
    _seed_bda_active()

    with pytest.raises(ValueError) as excinfo:
        mod.handler(_register_event("pii-redactor", [_OCR_POINT_HOOK]), None)

    message = str(excinfo.value)
    assert "postOcr" in message
    assert "onError='fail'" in message
    # The refusal has to say what to do instead, or it is just an obstacle.
    assert "preprocessing" in message

    # And nothing was written: the refusal happens before the put_item.
    row = _table().get_item(Key={"Configuration": "Config#zz-bda-v1"})["Item"]
    assert "ocr" not in row


def test_refusal_reads_a_stringified_use_bda(
    monkeypatch, configuration_table, load_lambda
):
    """Config rows store their values as strings; `"true"` must still refuse."""
    mod = _preload(monkeypatch, load_lambda)
    _seed_bda_active(use_bda="true")

    with pytest.raises(ValueError, match="postOcr"):
        mod.handler(_register_event("pii-redactor", [_OCR_POINT_HOOK]), None)


def test_advisory_hook_at_a_bda_unreachable_point_is_accepted_with_a_warning(
    monkeypatch, configuration_table, load_lambda
):
    """An observing hook is not a gate, so refusing it would block a legitimate
    "install now, switch to pipeline mode later" sequence. It registers, and the
    warning rides back on the response the feature stack's custom resource logs."""
    mod = _preload(monkeypatch, load_lambda)
    _seed_bda_active()
    advisory = {**_OCR_POINT_HOOK, "onError": "continue"}

    result = mod.handler(_register_event("observer", [advisory]), None)

    assert result["hookCount"] == 1
    assert len(result["warnings"]) == 1
    assert "postOcr" in result["warnings"][0]
    assert "NOT run" in result["warnings"][0]
    row = _table().get_item(Key={"Configuration": "Config#zz-bda-v1"})["Item"]
    assert [h["featureId"] for h in row["ocr"]["postHook"]] == ["observer"]


def test_pipeline_mode_registers_the_same_gating_hook_without_complaint(
    monkeypatch, configuration_table, load_lambda
):
    """The other direction: the point exists in pipeline mode, so nothing is wrong.

    Without this, a check that refused everything would look equally green.
    """
    mod = _preload(monkeypatch, load_lambda)
    _seed_bda_active(use_bda=False)

    result = mod.handler(_register_event("pii-redactor", [_OCR_POINT_HOOK]), None)

    assert result["hookCount"] == 1
    assert "warnings" not in result
    row = _table().get_item(Key={"Configuration": "Config#zz-bda-v1"})["Item"]
    assert [h["featureId"] for h in row["ocr"]["postHook"]] == ["pii-redactor"]


def test_gating_hook_at_a_shared_point_is_accepted_in_bda_mode(
    monkeypatch, configuration_table, load_lambda
):
    """`preprocessing` and the shared tail exist in BOTH modes, so a gate there
    is exactly what the docs tell an extension author to use — it must not be
    caught by a check aimed at the three Pipeline-only points."""
    mod = _preload(monkeypatch, load_lambda)
    _seed_bda_active()

    result = mod.handler(
        _register_event("pii-anonymizer", [_flat_hook("preprocessing")]), None
    )
    assert result["hookCount"] == 1
    assert "warnings" not in result


def test_a_disabled_gating_hook_at_an_unreachable_point_registers(
    monkeypatch, configuration_table, load_lambda
):
    """The least drastic remedy the refusal offers has to actually work.

    The dispatcher skips a disabled entry in every mode, so it gates nothing and
    there is nothing to refuse — and the refusal message tells the author to do
    exactly this when the hook is not wanted under BDA.
    """
    mod = _preload(monkeypatch, load_lambda)
    _seed_bda_active()
    disabled = {**_OCR_POINT_HOOK, "enabled": False}

    result = mod.handler(_register_event("pii-redactor", [disabled]), None)

    assert result["hookCount"] == 1
    assert "warnings" not in result
    row = _table().get_item(Key={"Configuration": "Config#zz-bda-v1"})["Item"]
    assert row["ocr"]["postHook"][0]["enabled"] is False


def test_registration_is_unchanged_when_the_mode_is_not_recorded(
    monkeypatch, configuration_table, load_lambda
):
    """A config row with no `use_bda` at all gives no basis to refuse anything.

    Guessing the mode would refuse valid registrations; the dispatcher's runtime
    audit covers this row once a document actually runs against it.
    """
    mod = _preload(monkeypatch, load_lambda)
    _seed_versions("zz-active-v1", filler=1)

    result = mod.handler(_register_event("pii-redactor", [_OCR_POINT_HOOK]), None)
    assert result["hookCount"] == 1
    assert "warnings" not in result
