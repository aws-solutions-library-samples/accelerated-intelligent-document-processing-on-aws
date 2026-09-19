# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""syncBdaIdp denies a caller whose config-version scope is unresolvable.

`versionName` selects the Configuration Profile — and, through the version
tracking table, the BDA project — that this operation mutates. An Author
restricted to a subset of profiles must not be able to name one outside it, and
that check is only as good as the scope it compares against. Three outcomes:

* **no `email` claim** — no key to look the caller up by, so deny, and issue **no
  query**. Nothing is substituted for the claim: any other identifier matches no
  UsersTable row, and an empty page means "unrestricted".
* **a failed DynamoDB query** — deny. A missing IAM grant or a throttle is not a
  statement that this caller has no restrictions.
* **an empty page** — still unrestricted, deliberately: scoping is opt-in.

The denial uses this resolver's in-band `{"success": false, "error": {"type":
"Unauthorized"}}` shape with HTTP 200, which is what an out-of-scope
`versionName` already returns and what the live RBAC harness reads as a denial —
so the two refusals are indistinguishable to the UI, as they should be.
"""

import importlib
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "sync_bda_idp_resolver_index", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_bda_idp_resolver_index"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()
config_scope = importlib.import_module("idp_common.config_scope")


def _event(claims, version_name="tenant-a"):
    return {
        "info": {"fieldName": "syncBdaIdp"},
        "arguments": {"versionName": version_name, "direction": "bda_to_idp"},
        "identity": {"claims": claims},
    }


def _author_claims(**extra):
    claims = {"cognito:groups": ["Author"], "email": "author@example.com"}
    claims.update(extra)
    return claims


def _is_unauthorized(result):
    return result["success"] is False and result["error"]["type"] == "Unauthorized"


@pytest.fixture
def users_table(monkeypatch):
    """Point the scope lookup at a UsersTable double and clear its cache."""

    def _configure(*, items=None, error=None):
        table = MagicMock()
        if error is not None:
            table.query.side_effect = error
        else:
            table.query.return_value = {"Items": items or []}
        resource = MagicMock()
        resource.Table.return_value = table
        monkeypatch.setattr(index, "_dynamodb", resource)
        monkeypatch.setenv("USERS_TABLE_NAME", "IDP-UsersTable")
        index._user_scope_cache.clear()
        return table

    index._user_scope_cache.clear()
    return _configure


@pytest.fixture(autouse=True)
def no_bda_calls(monkeypatch):
    """Stub the BDA/config work so only the authorization decision is exercised."""
    service = MagicMock()
    service.sync_bda_to_idp.return_value = {"success": True, "processedClasses": []}
    monkeypatch.setattr(index, "BdaBlueprintService", lambda *a, **k: service)
    monkeypatch.setattr(index, "ConfigurationManager", lambda *a, **k: MagicMock())
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    return service


@pytest.mark.unit
class TestScopeFailsClosed:
    def test_no_email_claim_denies_without_querying(self, users_table):
        table = users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])

        result = index.handler(
            _event(
                {
                    "cognito:groups": ["Author"],
                    "sub": "11111111-2222-3333-4444-555555555555",
                    "cognito:username": "author",
                }
            ),
            None,
        )

        assert _is_unauthorized(result)
        table.query.assert_not_called()

    def test_a_dynamodb_failure_denies(self, users_table):
        users_table(error=Exception("AccessDeniedException: dynamodb:Query"))

        assert _is_unauthorized(index.handler(_event(_author_claims()), None))

    def test_an_unwired_users_table_denies(self, monkeypatch):
        monkeypatch.delenv("USERS_TABLE_NAME", raising=False)
        index._user_scope_cache.clear()

        assert _is_unauthorized(index.handler(_event(_author_claims()), None))

    def test_an_out_of_scope_version_is_refused(self, users_table):
        users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])

        result = index.handler(_event(_author_claims(), version_name="tenant-b"), None)

        assert _is_unauthorized(result)

    def test_an_empty_page_is_still_unrestricted(self, users_table):
        """Most users have no scope row; the sync must still run for them."""
        table = users_table(items=[])

        result = index.handler(_event(_author_claims()), None)

        assert not _is_unauthorized(result)
        table.query.assert_called_once()

    def test_an_admin_is_never_looked_up(self, users_table):
        table = users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])

        result = index.handler(
            _event({"cognito:groups": ["Admin"], "email": "admin@example.com"},
                   version_name="tenant-b"),
            None,
        )

        assert not _is_unauthorized(result)
        table.query.assert_not_called()


@pytest.mark.unit
class TestCallerEmailProvenance:
    @pytest.mark.parametrize(
        "claims",
        [
            {"cognito:groups": ["Author"], "sub": "abc-123"},
            {"cognito:groups": ["Author"], "cognito:username": "author"},
            {"cognito:groups": ["Author"], "email": ""},
        ],
    )
    def test_only_an_email_claim_becomes_the_scope_key(self, claims):
        caller = index._get_caller_info({"identity": {"claims": claims}})

        assert caller["email"] == ""

    def test_the_wrapper_raises_the_shared_error_type(self, users_table):
        """One implementation of this rule, not a seventh copy of it."""
        users_table(error=Exception("boom"))

        with pytest.raises(config_scope.ScopeLookupError):
            index._get_user_allowed_config_versions("author@example.com")
