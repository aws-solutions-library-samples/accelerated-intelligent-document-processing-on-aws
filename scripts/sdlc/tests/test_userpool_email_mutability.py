"""Guards the parameter-gated `email` schema flag on the Cognito User Pool (#835).

Background
----------
Cognito re-applies an identity provider's `AttributeMapping` on EVERY federated
sign-in, and an immutable attribute cannot be rewritten even with an identical
value. With `email` declared `Mutable: false` a federated user can therefore
sign in exactly once. `Mutable: true` is the fix — but a Cognito schema flag is
fixed at pool creation, so flipping it unconditionally (11fd05dbd) wedged every
existing stack's next update in UPDATE_ROLLBACK_FAILED and was reverted
(3bed47097).

The fix is therefore parameter-gated. These tests pin the three properties that
make the gate safe, because each failure mode is invisible to a fresh-deploy
smoke test:

1. The parameter DEFAULTS to the historical value (`"false"`). A default of
   `"true"` would change the resolved schema flag on every existing stack's
   next update — the exact regression the revert fixed.
2. The schema flag is driven by that parameter (through one condition) rather
   than hardcoded either way, and the parameter is discoverable in the
   federation section of the console with a label that says what to do.
3. The compensating control ships with it: a mutable `email` can be rewritten
   by a native user's own token (`email` is in the client's WriteAttributes
   because it is IdP-mapped) and the API keys config scope on the token's email
   claim, so `AttributesRequireVerificationBeforeUpdate: [email]` must be set on
   exactly the pools that opt in — and not touch pools that did not.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TEMPLATE = REPO_ROOT / "template.yaml"
TRANSFORM = REPO_ROOT / "lib/idp_sdk/idp_sdk/_core/template_transform.py"

PARAM = "ExternalIdPEmailMutable"
CONDITION = "ExternalIdPEmailIsMutable"
FEDERATION_GROUP = "External Identity Provider (Federation)"


class _CfnSafeLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form intrinsics.

    Tagged nodes are wrapped as ``{"<tag>": value}`` so a test can tell an
    ``!If`` list apart from a literal list.
    """


def _tagged(loader: Any, tag_suffix: str, node: Any) -> Any:  # noqa: ANN401
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {f"!{tag_suffix}": value}


_CfnSafeLoader.add_multi_constructor("!", _tagged)


@pytest.fixture(scope="module")
def template() -> dict:
    with TEMPLATE.open() as fh:
        loader = _CfnSafeLoader(fh)
        try:
            return loader.get_single_data()
        finally:
            loader.dispose()


@pytest.fixture(scope="module")
def user_pool(template: dict) -> dict:
    pool = template["Resources"]["UserPool"]
    assert pool["Type"] == "AWS::Cognito::UserPool"
    return pool["Properties"]


@pytest.fixture(scope="module")
def email_schema(user_pool: dict) -> dict:
    for attr in user_pool["Schema"]:
        if attr.get("Name") == "email":
            return attr
    pytest.fail("UserPool.Schema has no `email` attribute")


def test_parameter_defaults_to_the_historical_value(template: dict) -> None:
    """Failure mode 1: a default of "true" flips the flag on every existing stack.

    Cognito rejects a schema-flag change on an existing pool, and CloudFormation
    surfaces it as a misleading `Required custom attributes are not supported`
    followed by UPDATE_ROLLBACK_FAILED. The default must stay "false" forever;
    new federated stacks opt in explicitly (the CLI does it for them).
    """
    param = template["Parameters"][PARAM]
    assert param["Default"] == "false", (
        f"{PARAM} must default to 'false'. A default of 'true' changes the "
        "resolved `email` Mutable flag on every EXISTING stack's next update, "
        "which Cognito rejects and which wedges the stack — see 3bed47097."
    )
    assert set(param["AllowedValues"]) == {"true", "false"}
    desc = param["Description"]
    # The description is the only warning a console user sees.
    assert "true" in desc and "CANNOT be changed" in desc, desc
    assert "UPDATE_ROLLBACK_FAILED" in desc, desc


def test_parameter_is_in_the_federation_group_with_a_label(template: dict) -> None:
    """The parameter must be found next to the other IdP settings, not orphaned."""
    interface = template["Metadata"]["AWS::CloudFormation::Interface"]
    groups = {
        g["Label"]["default"]: g["Parameters"] for g in interface["ParameterGroups"]
    }
    assert PARAM in groups[FEDERATION_GROUP], (
        f"{PARAM} must be listed under the '{FEDERATION_GROUP}' parameter group"
    )
    # Exactly one group, or the console shows it twice.
    assert sum(PARAM in params for params in groups.values()) == 1
    label = interface["ParameterLabels"][PARAM]["default"]
    assert "true" in label and "external IdP" in label, label


def test_email_mutable_is_driven_by_the_parameter(
    template: dict, email_schema: dict
) -> None:
    """Failure mode 2: the flag hardcoded either way.

    Hardcoded false re-introduces the one-sign-in lockout for every federated
    deployment; hardcoded true wedges every existing stack. It must be an Fn::If
    on a condition that reads exactly this parameter.
    """
    mutable = email_schema["Mutable"]
    assert isinstance(mutable, dict) and "!If" in mutable, (
        f"UserPool.Schema[email].Mutable must be `!If [{CONDITION}, true, false]`, "
        f"got {mutable!r}"
    )
    cond_name, if_true, if_false = mutable["!If"]
    assert cond_name == CONDITION
    assert if_true is True and if_false is False
    assert email_schema["Required"] is True, (
        "email must stay Required: the app keys per-user settings on it and a "
        "federated user record cannot be created without it"
    )

    condition = template["Conditions"][CONDITION]
    assert condition == {"!Equals": [{"!Ref": PARAM}, "true"]}, condition


def test_verification_before_update_is_gated_on_the_same_condition(
    user_pool: dict,
) -> None:
    """Failure mode 3: mutability without the compensating control, or the
    control leaking onto pools that did not opt in.

    On a mutable pool a native user's own SRP access token can call
    UpdateUserAttributes on `email` (it is in WriteAttributes because it is
    IdP-mapped), and the API resolvers derive allowedConfigVersions from the
    token's email claim. Requiring verification of the NEW address closes that.
    Gating it on the same condition keeps existing (immutable) pools' property
    set byte-identical, so their updates see no change at all.
    """
    settings = user_pool.get("UserAttributeUpdateSettings")
    assert settings is not None, (
        "UserPool must set UserAttributeUpdateSettings when email is mutable"
    )
    assert "!If" in settings, settings
    cond_name, when_mutable, otherwise = settings["!If"]
    assert cond_name == CONDITION
    assert when_mutable == {"AttributesRequireVerificationBeforeUpdate": ["email"]}
    assert otherwise == {"!Ref": "AWS::NoValue"}, (
        "an immutable pool must see NO UserAttributeUpdateSettings property, "
        "so existing stacks are untouched by this change"
    )


def test_headless_transform_strips_the_parameter() -> None:
    """The headless template has no Cognito, so it must drop this parameter
    along with the other ExternalIdP* ones — otherwise a headless deploy that
    passes it fails with a CFN ValidationError, and the CLI's create-time
    defaulting is skipped for headless precisely because of that."""
    source = TRANSFORM.read_text()
    rel = TRANSFORM.relative_to(REPO_ROOT)
    assert re.search(rf'^\s+"{PARAM}",$', source, re.M), (
        f"{PARAM} must be in parameters_to_remove in {rel}"
    )
    # ...and the condition that reads it, or the headless template carries a
    # dangling Ref that CloudFormation rejects outright (E1020).
    assert re.search(rf'^\s+"{CONDITION}",', source, re.M), (
        f"{CONDITION} must be in conditions_to_remove in {rel}"
    )
