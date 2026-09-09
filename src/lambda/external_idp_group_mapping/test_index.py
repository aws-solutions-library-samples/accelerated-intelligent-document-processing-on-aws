# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the external IdP group mapping Lambda function.

Covers:
- Group claim parsing (JSON array, comma-separated, single value, edge cases)
- Bidirectional group sync (add to target groups, remove from stale groups)
- Error handling (Cognito API failures, missing claims)
- Token override injection
- Provenance: only a user federated through the configured provider may be
  granted groups from custom:idp_groups (a self-set value on a native user
  grants nothing)
- Freshness: only a fresh sign-in is honoured, never a token refresh
"""
import json
import os
import pytest
from unittest.mock import MagicMock, patch, call


# The Cognito identity provider whose assertions may grant groups.
IDP_NAME = "MyOkta"

# Set env vars BEFORE importing the module so GROUP_MAPPING is populated at module load
ENV_VARS = {
    "ADMIN_GROUP_NAME": "IdP-Admins",
    "AUTHOR_GROUP_NAME": "IdP-Authors",
    "REVIEWER_GROUP_NAME": "IdP-Reviewers",
    "VIEWER_GROUP_NAME": "IdP-Viewers",
    "EXTERNAL_IDP_NAME": IDP_NAME,
    "LOG_LEVEL": "DEBUG",
}


def _make_event(
    username="testuser",
    user_pool_id="us-east-1_abc123",
    idp_groups="",
    extra_attrs=None,
    trigger_source="TokenGeneration_HostedAuth",
):
    """Build a minimal Cognito PreTokenGeneration trigger event."""
    attrs = {"custom:idp_groups": idp_groups}
    if extra_attrs:
        attrs.update(extra_attrs)
    return {
        "userPoolId": user_pool_id,
        "userName": username,
        "triggerSource": trigger_source,
        "request": {"userAttributes": attrs},
    }


def _admin_get_user_response(provider_name=IDP_NAME):
    """An AdminGetUser response for a user federated through `provider_name`.

    Pass provider_name=None for a native (non-federated) user, who has no
    `identities` attribute at all.

    The `identities` value below is the shape Cognito actually returns — captured
    from a live pool with a SAML provider and `AdminLinkProviderForUser`. Note
    `primary` and `dateCreated` come back as *strings*, not a boolean and an int;
    the handler reads neither, and this fixture keeps the real shape so that
    stays true.
    """
    attributes = [{"Name": "email", "Value": "testuser@example.com"}]
    if provider_name is not None:
        attributes.append(
            {
                "Name": "identities",
                "Value": json.dumps(
                    [
                        {
                            "dateCreated": "1788903903014",
                            "userId": "testuser@example.com",
                            "providerName": provider_name,
                            "providerType": "SAML",
                            "issuer": None,
                            "primary": "false",
                        }
                    ]
                ),
            }
        )
    return {"Username": "testuser", "UserAttributes": attributes}



def _override_groups(result):
    """The groups the handler asked Cognito to put in the token, or None.

    Reads whichever response key is present. The two names are not
    interchangeable — Cognito reads `claimsOverrideDetails` for a V1_0 trigger and
    `claimsAndScopeOverrideDetails` for V2_0/V3_0 — so the handler emits both and
    tests assert on both (see TestTokenOverrideShape).
    """
    response = result.get("response") or {}
    details = (
        response.get("claimsOverrideDetails")
        or response.get("claimsAndScopeOverrideDetails")
    )
    if not details:
        return None
    return (details.get("groupOverrideDetails") or {}).get("groupsToOverride")


# ============================================================
# Group parsing tests
# ============================================================

class TestParseIdpGroups:
    """Tests for parse_idp_groups handling various claim formats."""

    @pytest.fixture(autouse=True)
    def _load_module(self):
        with patch.dict(os.environ, ENV_VARS, clear=False):
            import importlib
            import index as mod
            importlib.reload(mod)
            self.parse = mod.parse_idp_groups

    def test_json_array(self):
        assert self.parse('["IdP-Admins", "IdP-Authors"]') == ["IdP-Admins", "IdP-Authors"]

    def test_json_array_single(self):
        assert self.parse('["IdP-Admins"]') == ["IdP-Admins"]

    def test_comma_separated(self):
        assert self.parse("IdP-Admins, IdP-Authors") == ["IdP-Admins", "IdP-Authors"]

    def test_single_value(self):
        assert self.parse("IdP-Admins") == ["IdP-Admins"]

    def test_empty_string(self):
        assert self.parse("") == []

    def test_none(self):
        assert self.parse(None) == []

    def test_whitespace_only(self):
        assert self.parse("   ") == []

    def test_json_array_with_extra_whitespace(self):
        result = self.parse('  ["IdP-Admins" , "IdP-Authors"]  ')
        assert result == ["IdP-Admins", "IdP-Authors"]

    def test_malformed_json_array_fallback(self):
        """Malformed JSON that starts with [ should fall back to split parsing."""
        result = self.parse("[IdP-Admins, IdP-Authors]")
        assert "IdP-Admins" in result
        assert "IdP-Authors" in result

    def test_comma_separated_with_spaces(self):
        assert self.parse("  IdP-Admins ,  IdP-Authors , IdP-Viewers  ") == [
            "IdP-Admins", "IdP-Authors", "IdP-Viewers"
        ]


# ============================================================
# Handler tests
# ============================================================

@pytest.mark.unit
class TestHandler:
    """Tests for the Lambda handler function."""

    @pytest.fixture(autouse=True)
    def _load_module(self):
        """Reload module with env vars and mock the cognito client.

        The mock user is federated through the configured provider by default, so
        these tests exercise the group-sync behaviour. Provenance and freshness
        are covered by TestProvenanceAndFreshness.
        """
        with patch.dict(os.environ, ENV_VARS, clear=False):
            import importlib
            import index as mod
            importlib.reload(mod)
            self.mod = mod
            self.handler = mod.handler
            self.mock_cognito = MagicMock()
            self.mock_cognito.admin_get_user.return_value = _admin_get_user_response()
            mod.cognito = self.mock_cognito

    def test_no_idp_groups_claim_returns_event_unchanged(self):
        """When custom:idp_groups is empty, handler returns event without calling Cognito."""
        event = _make_event(idp_groups="")
        result = self.handler(event, None)
        assert result is event
        self.mock_cognito.admin_list_groups_for_user.assert_not_called()

    def test_no_matching_groups_returns_event_unchanged(self):
        """When IdP groups don't match any mapping, handler returns event without syncing."""
        event = _make_event(idp_groups="UnknownGroup")
        result = self.handler(event, None)
        assert result is event
        self.mock_cognito.admin_list_groups_for_user.assert_not_called()

    def test_adds_user_to_mapped_groups(self):
        """User with IdP-Admins should be added to Cognito Admin group."""
        self.mock_cognito.admin_list_groups_for_user.return_value = {"Groups": []}
        event = _make_event(idp_groups="IdP-Admins")

        result = self.handler(event, None)

        self.mock_cognito.admin_add_user_to_group.assert_called_once_with(
            UserPoolId="us-east-1_abc123", Username="testuser", GroupName="Admin"
        )
        # Verify token override
        override = result["response"]["claimsAndScopeOverrideDetails"]["groupOverrideDetails"]
        assert "Admin" in override["groupsToOverride"]

    def test_multiple_groups_added(self):
        """User with multiple IdP groups should be added to all matching Cognito groups."""
        self.mock_cognito.admin_list_groups_for_user.return_value = {"Groups": []}
        event = _make_event(idp_groups="IdP-Admins, IdP-Reviewers")

        result = self.handler(event, None)

        add_calls = self.mock_cognito.admin_add_user_to_group.call_args_list
        added_groups = {c.kwargs["GroupName"] for c in add_calls}
        assert added_groups == {"Admin", "Reviewer"}

    def test_skips_already_assigned_groups(self):
        """User already in Admin should not be re-added."""
        self.mock_cognito.admin_list_groups_for_user.return_value = {
            "Groups": [{"GroupName": "Admin"}]
        }
        event = _make_event(idp_groups="IdP-Admins")

        self.handler(event, None)

        self.mock_cognito.admin_add_user_to_group.assert_not_called()

    def test_removes_stale_managed_groups(self):
        """User currently in Admin+Author but IdP only says Author → remove from Admin."""
        self.mock_cognito.admin_list_groups_for_user.return_value = {
            "Groups": [{"GroupName": "Admin"}, {"GroupName": "Author"}]
        }
        event = _make_event(idp_groups="IdP-Authors")

        self.handler(event, None)

        self.mock_cognito.admin_remove_user_from_group.assert_called_once_with(
            UserPoolId="us-east-1_abc123", Username="testuser", GroupName="Admin"
        )
        self.mock_cognito.admin_add_user_to_group.assert_not_called()

    def test_does_not_remove_unmanaged_groups(self):
        """Groups not in the mapping (e.g., 'CustomGroup') should not be removed."""
        self.mock_cognito.admin_list_groups_for_user.return_value = {
            "Groups": [{"GroupName": "CustomGroup"}, {"GroupName": "Author"}]
        }
        event = _make_event(idp_groups="IdP-Authors")

        self.handler(event, None)

        self.mock_cognito.admin_remove_user_from_group.assert_not_called()

    def test_token_override_contains_all_target_groups(self):
        """The groupsToOverride in the response should list all target groups."""
        self.mock_cognito.admin_list_groups_for_user.return_value = {"Groups": []}
        event = _make_event(idp_groups='["IdP-Admins", "IdP-Viewers"]')

        result = self.handler(event, None)

        override_groups = set(
            result["response"]["claimsAndScopeOverrideDetails"]["groupOverrideDetails"]["groupsToOverride"]
        )
        assert override_groups == {"Admin", "Viewer"}

    def test_list_groups_api_failure_returns_event(self):
        """If admin_list_groups_for_user fails, handler returns event gracefully."""
        self.mock_cognito.admin_list_groups_for_user.side_effect = Exception("AccessDenied")
        event = _make_event(idp_groups="IdP-Admins")

        result = self.handler(event, None)

        assert result is event
        self.mock_cognito.admin_add_user_to_group.assert_not_called()

    def test_add_group_api_failure_continues(self):
        """If adding to one group fails, the handler should continue with others."""
        self.mock_cognito.admin_list_groups_for_user.return_value = {"Groups": []}
        self.mock_cognito.admin_add_user_to_group.side_effect = [
            Exception("Throttled"),  # first call fails
            None,  # second call succeeds
        ]
        event = _make_event(idp_groups="IdP-Admins, IdP-Authors")

        result = self.handler(event, None)

        # Should still have attempted both adds
        assert self.mock_cognito.admin_add_user_to_group.call_count == 2
        # Token override should still contain both target groups
        override_groups = set(
            result["response"]["claimsAndScopeOverrideDetails"]["groupOverrideDetails"]["groupsToOverride"]
        )
        assert override_groups == {"Admin", "Author"}

    def test_remove_group_api_failure_continues(self):
        """If removing from a group fails, handler should continue gracefully."""
        self.mock_cognito.admin_list_groups_for_user.return_value = {
            "Groups": [{"GroupName": "Admin"}, {"GroupName": "Author"}]
        }
        self.mock_cognito.admin_remove_user_from_group.side_effect = Exception("Throttled")
        event = _make_event(idp_groups="IdP-Authors")

        result = self.handler(event, None)

        # Should still return event with token override
        assert "claimsAndScopeOverrideDetails" in result["response"]

    def test_json_array_groups_claim(self):
        """Groups sent as a JSON array should be parsed correctly."""
        self.mock_cognito.admin_list_groups_for_user.return_value = {"Groups": []}
        event = _make_event(idp_groups=json.dumps(["IdP-Admins", "IdP-Reviewers"]))

        result = self.handler(event, None)

        add_calls = self.mock_cognito.admin_add_user_to_group.call_args_list
        added_groups = {c.kwargs["GroupName"] for c in add_calls}
        assert added_groups == {"Admin", "Reviewer"}

    def test_missing_user_attributes_key(self):
        """Event with no custom:idp_groups attribute should return unchanged."""
        event = {
            "userPoolId": "us-east-1_abc123",
            "userName": "testuser",
            "triggerSource": "TokenGeneration_HostedAuth",
            "request": {"userAttributes": {}},
        }
        result = self.handler(event, None)
        assert result is event


# ============================================================
# Provenance and freshness tests
# ============================================================

@pytest.mark.unit
class TestProvenanceAndFreshness:
    """The attribute only grants groups when the IdP put it there.

    `custom:idp_groups` is mutable and — when IdP-mapped — writable by the app
    client, so a user's own access token can set it via UpdateUserAttributes.
    These tests pin the two checks that stop a self-set value from granting
    groups.
    """

    @pytest.fixture(autouse=True)
    def _load_module(self):
        with patch.dict(os.environ, ENV_VARS, clear=False):
            import importlib
            import index as mod
            importlib.reload(mod)
            self.mod = mod
            self.handler = mod.handler
            self.mock_cognito = MagicMock()
            self.mock_cognito.admin_list_groups_for_user.return_value = {"Groups": []}
            mod.cognito = self.mock_cognito

    def _assert_nothing_granted(self, result, event):
        """No group membership change and no token override."""
        assert result is event
        self.mock_cognito.admin_add_user_to_group.assert_not_called()
        self.mock_cognito.admin_remove_user_from_group.assert_not_called()
        assert "claimsAndScopeOverrideDetails" not in result.get("response", {})

    def test_native_user_self_set_attribute_grants_nothing(self):
        """A native user who sets custom:idp_groups on themselves gains no groups.

        Without the provenance check, any value a user put in the attribute would
        be mapped, so a Viewer naming the admin IdP group would be added to the
        Admin group. This is the case the check exists for.
        """
        self.mock_cognito.admin_get_user.return_value = _admin_get_user_response(
            provider_name=None
        )
        event = _make_event(idp_groups="IdP-Admins")

        result = self.handler(event, None)

        self._assert_nothing_granted(result, event)

    def test_user_federated_via_other_provider_grants_nothing(self):
        """Being federated through some *other* provider is not enough."""
        self.mock_cognito.admin_get_user.return_value = _admin_get_user_response(
            provider_name="SomeOtherIdP"
        )
        event = _make_event(idp_groups="IdP-Admins")

        result = self.handler(event, None)

        self._assert_nothing_granted(result, event)

    def test_federated_user_is_granted_groups(self):
        """The positive case: federated through the configured provider."""
        self.mock_cognito.admin_get_user.return_value = _admin_get_user_response()
        event = _make_event(idp_groups="IdP-Admins")

        result = self.handler(event, None)

        self.mock_cognito.admin_add_user_to_group.assert_called_once_with(
            UserPoolId="us-east-1_abc123", Username="testuser", GroupName="Admin"
        )
        override = result["response"]["claimsAndScopeOverrideDetails"]["groupOverrideDetails"]
        assert override["groupsToOverride"] == ["Admin"]

    def test_token_refresh_grants_nothing(self):
        """A refresh re-reads stored state, which the user may have written.

        Skipping the refresh closes the path where a federated user self-sets the
        attribute after sign-in and then refreshes to pick up the new groups.
        """
        self.mock_cognito.admin_get_user.return_value = _admin_get_user_response()
        event = _make_event(
            idp_groups="IdP-Admins", trigger_source="TokenGeneration_RefreshTokens"
        )

        result = self.handler(event, None)

        self._assert_nothing_granted(result, event)
        self.mock_cognito.admin_get_user.assert_not_called()

    def test_unknown_trigger_source_grants_nothing(self):
        """Anything not on the fresh-sign-in list is treated as untrusted."""
        self.mock_cognito.admin_get_user.return_value = _admin_get_user_response()
        event = _make_event(idp_groups="IdP-Admins", trigger_source="TokenGeneration_Future")

        result = self.handler(event, None)

        self._assert_nothing_granted(result, event)

    @pytest.mark.parametrize(
        "trigger_source",
        [
            "TokenGeneration_HostedAuth",
            "TokenGeneration_Authentication",
            "TokenGeneration_NewPasswordChallenge",
            "TokenGeneration_AuthenticateDevice",
        ],
    )
    def test_fresh_sign_in_sources_are_honoured(self, trigger_source):
        """Every non-refresh sign-in source maps groups."""
        self.mock_cognito.admin_get_user.return_value = _admin_get_user_response()
        event = _make_event(idp_groups="IdP-Authors", trigger_source=trigger_source)

        result = self.handler(event, None)

        self.mock_cognito.admin_add_user_to_group.assert_called_once_with(
            UserPoolId="us-east-1_abc123", Username="testuser", GroupName="Author"
        )
        assert "claimsAndScopeOverrideDetails" in result["response"]

    def test_admin_get_user_failure_fails_closed(self):
        """An unreadable user is not a trusted user."""
        self.mock_cognito.admin_get_user.side_effect = Exception("AccessDenied")
        event = _make_event(idp_groups="IdP-Admins")

        result = self.handler(event, None)

        self._assert_nothing_granted(result, event)

    def test_unparseable_identities_fails_closed(self):
        """A malformed identities value grants nothing rather than being ignored."""
        self.mock_cognito.admin_get_user.return_value = {
            "UserAttributes": [{"Name": "identities", "Value": "not-json"}]
        }
        event = _make_event(idp_groups="IdP-Admins")

        result = self.handler(event, None)

        self._assert_nothing_granted(result, event)

    def test_empty_identities_list_fails_closed(self):
        """An empty identities array names no provider, so it matches none."""
        self.mock_cognito.admin_get_user.return_value = {
            "UserAttributes": [{"Name": "identities", "Value": "[]"}]
        }
        event = _make_event(idp_groups="IdP-Admins")

        result = self.handler(event, None)

        self._assert_nothing_granted(result, event)

    def test_no_configured_provider_grants_nothing(self):
        """With EXTERNAL_IDP_NAME unset no provider is trusted, so nothing is granted."""
        env = dict(ENV_VARS)
        env["EXTERNAL_IDP_NAME"] = ""
        with patch.dict(os.environ, env, clear=False):
            import importlib
            import index as mod
            importlib.reload(mod)
            mock_cognito = MagicMock()
            mock_cognito.admin_get_user.return_value = _admin_get_user_response()
            mod.cognito = mock_cognito

            event = _make_event(idp_groups="IdP-Admins")
            result = mod.handler(event, None)

            assert result is event
            mock_cognito.admin_add_user_to_group.assert_not_called()
            mock_cognito.admin_get_user.assert_not_called()

    def test_provenance_is_not_read_from_the_event(self):
        """An `identities` attribute in the event does not substitute for AdminGetUser.

        The event's userAttributes mirror stored attributes; provenance must come
        from the AdminGetUser read so the decision rests on one server-side source.
        """
        self.mock_cognito.admin_get_user.return_value = _admin_get_user_response(
            provider_name=None
        )
        event = _make_event(
            idp_groups="IdP-Admins",
            extra_attrs={
                "identities": json.dumps([{"providerName": IDP_NAME}]),
            },
        )

        result = self.handler(event, None)

        self._assert_nothing_granted(result, event)


# ============================================================
# Deployed-copy (template.yaml InlineCode) tests
# ============================================================

@pytest.mark.unit
class TestDeployedInlineCopy:
    """The handler that deploys is the InlineCode in template.yaml, not this file.

    ExternalIdPGroupMappingFunction carries an inline copy of the same logic (a
    Lambda-layer-free trigger), so a fix applied only to index.py would not ship.
    These tests load that inline copy and re-run the provenance and freshness
    assertions against it, which fails if the two drift apart.
    """

    @pytest.fixture(autouse=True)
    def _load_inline_module(self):
        import types
        import yaml

        template_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "template.yaml"
        )
        if not os.path.exists(template_path):
            pytest.skip("template.yaml not reachable from this test's location")

        # CloudFormation short-form tags (!Ref, !Sub, ...) are not valid YAML tags,
        # so collapse them to plain values — only InlineCode matters here.
        class _CfnLoader(yaml.SafeLoader):
            pass

        def _passthrough(loader, _tag_suffix, node):
            if isinstance(node, yaml.ScalarNode):
                return loader.construct_scalar(node)
            if isinstance(node, yaml.SequenceNode):
                return loader.construct_sequence(node)
            return loader.construct_mapping(node)

        _CfnLoader.add_multi_constructor("!", _passthrough)

        with open(template_path) as f:
            template = yaml.load(f, Loader=_CfnLoader)

        code = template["Resources"]["ExternalIdPGroupMappingFunction"]["Properties"][
            "InlineCode"
        ]

        env = dict(ENV_VARS)
        env.setdefault("AWS_DEFAULT_REGION", "us-east-1")
        with patch.dict(os.environ, env, clear=False):
            mod = types.ModuleType("inline_external_idp_group_mapping")
            exec(compile(code, "template.yaml:InlineCode", "exec"), mod.__dict__)

        self.mod = mod
        self.handler = mod.handler
        self.mock_cognito = MagicMock()
        self.mock_cognito.admin_list_groups_for_user.return_value = {"Groups": []}
        mod.cognito = self.mock_cognito

    def test_inline_copy_grants_nothing_to_a_native_user(self):
        self.mock_cognito.admin_get_user.return_value = _admin_get_user_response(
            provider_name=None
        )
        event = _make_event(idp_groups="IdP-Admins")

        result = self.handler(event, None)

        assert result is event
        self.mock_cognito.admin_add_user_to_group.assert_not_called()
        assert "claimsAndScopeOverrideDetails" not in result.get("response", {})

    def test_inline_copy_grants_nothing_on_refresh(self):
        self.mock_cognito.admin_get_user.return_value = _admin_get_user_response()
        event = _make_event(
            idp_groups="IdP-Admins", trigger_source="TokenGeneration_RefreshTokens"
        )

        result = self.handler(event, None)

        assert result is event
        self.mock_cognito.admin_add_user_to_group.assert_not_called()

    def test_inline_copy_grants_nothing_via_another_provider(self):
        self.mock_cognito.admin_get_user.return_value = _admin_get_user_response(
            provider_name="SomeOtherIdP"
        )
        event = _make_event(idp_groups="IdP-Admins")

        result = self.handler(event, None)

        assert result is event
        self.mock_cognito.admin_add_user_to_group.assert_not_called()

    def test_inline_copy_grants_groups_to_a_federated_user(self):
        self.mock_cognito.admin_get_user.return_value = _admin_get_user_response()
        event = _make_event(idp_groups="IdP-Admins")

        result = self.handler(event, None)

        self.mock_cognito.admin_add_user_to_group.assert_called_once_with(
            UserPoolId="us-east-1_abc123", Username="testuser", GroupName="Admin"
        )
        override = result["response"]["claimsAndScopeOverrideDetails"]["groupOverrideDetails"]
        assert override["groupsToOverride"] == ["Admin"]

    def test_inline_copy_reads_the_provider_name_from_the_environment(self):
        """The trusted provider must come from EXTERNAL_IDP_NAME, not be hardcoded."""
        assert self.mod.EXTERNAL_IDP_NAME == IDP_NAME



# ============================================================
# Token override response shape
# ============================================================

@pytest.mark.unit
class TestTokenOverrideShape:
    """Cognito reads a different response key per trigger event version.

    `claimsOverrideDetails` is the V1_0 name; `claimsAndScopeOverrideDetails` is
    V2_0/V3_0. template.yaml registers the trigger with `PreTokenGeneration:`,
    which is V1_0, so emitting only the V2 name meant Cognito silently ignored the
    override and a first sign-in produced a token with no group claim — confirmed
    against a deployed pool. The handler emits both; these tests keep it that way.
    """

    @pytest.fixture(autouse=True)
    def _load_module(self):
        with patch.dict(os.environ, ENV_VARS, clear=False):
            import importlib
            import index as mod
            importlib.reload(mod)
            self.handler = mod.handler
            self.mock_cognito = MagicMock()
            self.mock_cognito.admin_get_user.return_value = _admin_get_user_response()
            self.mock_cognito.admin_list_groups_for_user.return_value = {"Groups": []}
            mod.cognito = self.mock_cognito

    def test_emits_the_v1_key(self):
        result = self.handler(_make_event(idp_groups="IdP-Admins"), None)
        details = result["response"]["claimsOverrideDetails"]
        assert details["groupOverrideDetails"]["groupsToOverride"] == ["Admin"]

    def test_emits_the_v2_key(self):
        result = self.handler(_make_event(idp_groups="IdP-Admins"), None)
        details = result["response"]["claimsAndScopeOverrideDetails"]
        assert details["groupOverrideDetails"]["groupsToOverride"] == ["Admin"]

    def test_both_keys_agree(self):
        result = self.handler(_make_event(idp_groups="IdP-Admins, IdP-Viewers"), None)
        v1 = result["response"]["claimsOverrideDetails"]
        v2 = result["response"]["claimsAndScopeOverrideDetails"]
        assert v1 == v2

    def test_neither_key_appears_when_nothing_is_granted(self):
        """A skipped sign-in must not emit an empty override under either name."""
        self.mock_cognito.admin_get_user.return_value = _admin_get_user_response(
            provider_name=None
        )
        result = self.handler(_make_event(idp_groups="IdP-Admins"), None)
        response = result.get("response") or {}
        assert "claimsOverrideDetails" not in response
        assert "claimsAndScopeOverrideDetails" not in response

# ============================================================
# Module-level GROUP_MAPPING tests
# ============================================================

class TestGroupMapping:
    """Tests for GROUP_MAPPING initialization from environment variables."""

    def test_partial_env_vars(self):
        """Only configured env vars should appear in GROUP_MAPPING."""
        partial_env = {"ADMIN_GROUP_NAME": "MyAdmins", "LOG_LEVEL": "INFO"}
        with patch.dict(os.environ, partial_env, clear=True):
            import importlib
            import index as mod
            importlib.reload(mod)
            assert mod.GROUP_MAPPING == {"MyAdmins": "Admin"}
            assert mod.COGNITO_GROUPS == {"Admin"}

    def test_empty_env_vars(self):
        """No group env vars → empty mapping."""
        with patch.dict(os.environ, {"LOG_LEVEL": "INFO"}, clear=True):
            import importlib
            import index as mod
            importlib.reload(mod)
            assert mod.GROUP_MAPPING == {}
            assert mod.COGNITO_GROUPS == set()

    def test_whitespace_env_vars_ignored(self):
        """Env vars with only whitespace should be ignored."""
        env = {"ADMIN_GROUP_NAME": "  ", "AUTHOR_GROUP_NAME": "Authors", "LOG_LEVEL": "INFO"}
        with patch.dict(os.environ, env, clear=True):
            import importlib
            import index as mod
            importlib.reload(mod)
            assert "  " not in mod.GROUP_MAPPING
            assert mod.GROUP_MAPPING == {"Authors": "Author"}
