# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The configuration resolver denies a caller whose scope cannot be resolved.

Three outcomes, and the whole point is the difference between the first two and
the third:

* **no ``email`` claim** — there is no key to look the caller up by, so the
  request is denied and **no query is issued**. Nothing is substituted for the
  claim: every other identifier a claims set may carry is not an email address
  for all callers, so querying an email-keyed index with one matches no row, and
  an empty page means "unrestricted".
* **a failed DynamoDB query** — denied. A missing IAM grant or a throttle must
  not read as "this caller has no restriction".
* **an empty page** — still **unrestricted**, deliberately. Scoping is opt-in per
  user; denying here would lock every ordinary user out of the UI.

The denial is a raised ``PermissionError``, which ``http_api_dispatcher`` turns
into HTTP 403. Returning the resolver's in-band error payload instead would
answer 200 on the operations that return a list rather than a success envelope.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("CONFIGURATION_TABLE_NAME", "test-config-table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "config_resolver_index", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["config_resolver_index"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()

# A read a non-admin Viewer is entitled to, so the group gate passes and the
# scope lookup is what decides the request.
_FIELD = "getConfigVersions"


def _event(claims):
    return {
        "info": {"fieldName": _FIELD},
        "arguments": {},
        "identity": {"claims": claims},
    }


def _viewer_claims(**extra):
    claims = {"cognito:groups": ["Viewer"], "email": "viewer@example.com"}
    claims.update(extra)
    return claims


@pytest.fixture
def manager(monkeypatch):
    """Stub ConfigurationManager so the handler runs without AWS."""
    fake = MagicMock()
    fake.list_versions.return_value = []
    monkeypatch.setattr(index, "ConfigurationManager", lambda *a, **k: fake)
    return fake


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


@pytest.mark.unit
class TestScopeFailsClosed:
    def test_no_email_claim_denies_without_querying(self, manager, users_table):
        """Every Cognito identifier except an email — deny, and do not query."""
        table = users_table(items=[{"allowedConfigVersions": ["lending"]}])

        with pytest.raises(PermissionError):
            index.handler(
                _event(
                    {
                        "cognito:groups": ["Viewer"],
                        "sub": "11111111-2222-3333-4444-555555555555",
                        "cognito:username": "viewer",
                    }
                ),
                None,
            )

        table.query.assert_not_called()

    def test_a_dynamodb_failure_denies(self, manager, users_table):
        users_table(error=Exception("AccessDeniedException: dynamodb:Query"))

        with pytest.raises(PermissionError):
            index.handler(_event(_viewer_claims()), None)

    def test_an_unwired_users_table_denies(self, manager, monkeypatch):
        """The parent template wires it unconditionally; empty means drift."""
        monkeypatch.delenv("USERS_TABLE_NAME", raising=False)
        index._user_scope_cache.clear()

        with pytest.raises(PermissionError):
            index.handler(_event(_viewer_claims()), None)

    def test_an_empty_page_is_still_unrestricted(self, manager, users_table):
        """Most users have no scope row and must keep working."""
        users_table(items=[])

        result = index.handler(_event(_viewer_claims()), None)

        assert result["success"] is True

    def test_an_admin_is_never_looked_up(self, manager, users_table):
        table = users_table(items=[{"allowedConfigVersions": ["lending"]}])

        index.handler(
            _event({"cognito:groups": ["Admin"], "email": "admin@example.com"}), None
        )

        table.query.assert_not_called()


@pytest.mark.unit
class TestCallerEmailProvenance:
    @pytest.mark.parametrize(
        "claims",
        [
            {"cognito:groups": ["Viewer"], "sub": "abc-123"},
            {"cognito:groups": ["Viewer"], "cognito:username": "viewer"},
            {"cognito:groups": ["Viewer"], "email": ""},
        ],
    )
    def test_no_identifier_but_an_email_becomes_the_scope_key(self, claims):
        """A substituted identifier is not a cheaper way to satisfy the lookup."""
        caller = index._get_caller_info({"identity": {"claims": claims}})

        assert caller["email"] == ""

    def test_the_email_claim_is_the_scope_key(self):
        caller = index._get_caller_info(
            {
                "identity": {
                    "claims": {"email": "a@example.com", "cognito:username": "other"},
                    "username": "different@example.com",
                }
            }
        )

        assert caller["email"] == "a@example.com"
