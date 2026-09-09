"""Guards the UserPoolClient attribute write permissions in `template.yaml`.

Background — the two failure modes this suite sits between
----------------------------------------------------------
`UserPoolClient` declares an explicit `WriteAttributes`. Getting that list wrong
breaks in one of two opposite directions, and neither shows up in a fresh-deploy
smoke test:

1. **Too narrow.** Cognito applies an identity provider's `AttributeMapping` *as
   the app client*, so every IdP-mapped attribute must be writable by the client.
   A mapped attribute missing from `WriteAttributes` fails the *federated
   sign-in* — not the deploy — and only for deployments that configured an
   external IdP. Verified behaviour per the `WriteAttributes` API reference: "If
   your app client allows users to sign in through an IdP, this array must
   include all attributes that you have mapped to IdP attributes."

2. **Too wide.** Left unset entirely, Cognito's default lets a user's own access
   token write every mutable attribute — including `custom:idp_groups`, which
   `ExternalIdPGroupMappingFunction` reads to assign Cognito groups. Measured
   against a live pool: with `WriteAttributes` unset a plain user self-wrote that
   attribute; with an explicit list excluding it the same call returned
   `NotAuthorizedException`. So the list must exist, and must not be a blanket
   grant.

The tests below pin both edges: every attribute the SAML/OIDC provider maps is
present, and nothing beyond the mapped set (plus attributes the app itself needs)
is.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

# scripts/sdlc/tests/<this file> -> repo root
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TEMPLATE = REPO_ROOT / "template.yaml"


class _CfnSafeLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form intrinsics."""


def _any_tag(loader: Any, _tag_suffix: str, node: Any) -> Any:  # noqa: ANN401
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_CfnSafeLoader.add_multi_constructor("!", _any_tag)


def _flatten(value: Any) -> list[str]:
    """Collect the attribute names out of a property that may contain Fn::If.

    An `!If [Cond, "custom:idp_groups", !Ref "AWS::NoValue"]` survives the loader
    above as the list `["Cond", "custom:idp_groups", "AWS::NoValue"]`, so pull out
    every string that looks like an attribute name and drop the condition name and
    the NoValue sentinel.
    """
    out: list[str] = []
    if isinstance(value, str):
        if value != "AWS::NoValue":
            out.append(value)
    elif isinstance(value, list):
        for item in value:
            out.extend(_flatten(item))
    elif isinstance(value, dict):
        for item in value.values():
            out.extend(_flatten(item))
    return out


@pytest.fixture(scope="module")
def template() -> dict:
    with TEMPLATE.open() as fh:
        loader = _CfnSafeLoader(fh)
        try:
            return loader.get_single_data()
        finally:
            loader.dispose()


@pytest.fixture(scope="module")
def write_attributes(template: dict) -> set[str]:
    props = template["Resources"]["UserPoolClient"]["Properties"]
    assert "WriteAttributes" in props, (
        "UserPoolClient must declare WriteAttributes explicitly. Unset, Cognito's "
        "default lets a user's own access token write every mutable attribute, "
        "including custom:idp_groups which the pre-token trigger reads to assign "
        "Cognito groups."
    )
    names = set(_flatten(props["WriteAttributes"]))
    # Drop the condition names Fn::If contributes.
    return {n for n in names if n in _ALL_KNOWN_ATTRIBUTES}


@pytest.fixture(scope="module")
def mapped_attributes(template: dict) -> set[str]:
    """The attributes the external identity provider maps onto the user record."""
    provider = None
    for name, res in template["Resources"].items():
        if res.get("Type") == "AWS::Cognito::UserPoolIdentityProvider":
            provider = res
            break
    assert provider is not None, (
        "no AWS::Cognito::UserPoolIdentityProvider found — if the external IdP "
        "resource was renamed or removed, update this test"
    )
    mapping = provider["Properties"]["AttributeMapping"]
    return set(mapping.keys())


# Attribute names that may legitimately appear in Read/WriteAttributes. Anything
# outside this set in the fixtures above is a condition name leaking through the
# Fn::If flattening, not an attribute.
_ALL_KNOWN_ATTRIBUTES = {
    "email",
    "email_verified",
    "preferred_username",
    "given_name",
    "family_name",
    "custom:idp_groups",
}


def test_every_idp_mapped_attribute_is_writable(mapped_attributes, write_attributes):
    """Failure mode 1: a mapped attribute the client cannot write breaks sign-in.

    Cognito writes mapped attributes as the app client on every federated
    sign-in, so omitting one here fails the sign-in rather than the deploy — and
    only for deployments that configured an external IdP, which no default-path
    test exercises.
    """
    missing = mapped_attributes - write_attributes
    assert not missing, (
        f"AttributeMapping maps {sorted(missing)} but UserPoolClient.WriteAttributes "
        "does not include them. Cognito applies AttributeMapping as the app client "
        "and fails the federated sign-in if it cannot write a mapped attribute."
    )


def test_write_attributes_is_not_a_blanket_grant(write_attributes):
    """Failure mode 2: the list must be narrower than Cognito's default.

    The default is every mutable attribute. `preferred_username` and
    `email_verified` are readable but the application never writes them, so they
    must not appear — if a future change needs one, add it deliberately and update
    this test.
    """
    unexpected = write_attributes - {
        "email",
        "given_name",
        "family_name",
        "custom:idp_groups",
    }
    assert not unexpected, (
        f"UserPoolClient.WriteAttributes grants {sorted(unexpected)}, which the "
        "application does not write. Every writable attribute is one an end user "
        "can set on themselves with their own access token."
    )


def test_idp_groups_is_writable_only_alongside_group_mapping(template):
    """`custom:idp_groups` must be gated on the group-mapping condition.

    It is only in the list because it is IdP-mapped. A deployment that does not
    map groups has no reason to let anyone write it, and the pre-token trigger
    that reads it does not exist there either.
    """
    props = template["Resources"]["UserPoolClient"]["Properties"]
    raw = props["WriteAttributes"]
    gated = [
        item
        for item in raw
        if isinstance(item, list) and "custom:idp_groups" in _flatten(item)
    ]
    assert gated, (
        "custom:idp_groups must be conditional in WriteAttributes, not granted "
        "unconditionally"
    )
    assert "ShouldMapExternalIdPGroups" in gated[0], (
        "custom:idp_groups should be gated on ShouldMapExternalIdPGroups (an "
        "external IdP AND a group attribute configured), got: "
        f"{gated[0]!r}"
    )


def test_idp_groups_attribute_is_mutable(template):
    """The trigger's provenance check, not immutability, is what protects it.

    Making the schema attribute immutable would look like a fix but breaks the
    feature: Cognito rewrites the mapped attribute on every federated sign-in, so
    an immutable `custom:idp_groups` fails the second sign-in the same way
    `email` does. This test exists so nobody "hardens" it that way.
    """
    schema = template["Resources"]["UserPool"]["Properties"]["Schema"]
    entry = next((s for s in schema if s.get("Name") == "idp_groups"), None)
    assert entry is not None, "idp_groups schema attribute not found"
    assert entry.get("Mutable") is True, (
        "idp_groups must stay Mutable: true — Cognito rewrites mapped attributes "
        "on every federated sign-in, so an immutable one breaks the second "
        "sign-in. Provenance is enforced in ExternalIdPGroupMappingFunction."
    )
