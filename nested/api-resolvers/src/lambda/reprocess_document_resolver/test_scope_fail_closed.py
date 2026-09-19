# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""reprocessDocument denies a caller whose config-version scope is unresolvable.

`version` names the Configuration Profile a document will be re-run under, so an
Author restricted to a subset of profiles must not be able to pass one outside it.
That check is only as good as the scope it compares against, and three outcomes
have to be distinguished:

* **no `email` claim** — no key to look the caller up by, so deny, and issue **no
  query**. Nothing is substituted for the claim: any other identifier matches no
  UsersTable row, and an empty page means "unrestricted".
* **a failed DynamoDB query** — deny. A missing IAM grant or a throttle is not a
  statement that this caller has no restrictions.
* **an empty page** — still unrestricted, deliberately: scoping is opt-in.

The denial is a raised `PermissionError`, which `http_api_dispatcher` turns into
HTTP 403; the handler's catch-all re-raises rather than swallowing it.

This file deliberately drives the module with the REAL `idp_common.config_scope`,
because what is under test is that a `ScopeLookupError` from the shared lookup
becomes a refusal here. `test_delete_output_data.py` in this directory stubs the
`idp_common` package in `sys.modules` at import time to test a different thing, so
the loader below undoes that for its own import.
"""

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("INPUT_BUCKET", "test-in")
os.environ.setdefault("OUTPUT_BUCKET", "test-out")
os.environ.setdefault("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/1/test-queue")
# index.py builds a real document service at import; it needs a table name but
# makes no call, so any value does. The Lambda always has this wired.
os.environ.setdefault("TRACKING_TABLE", "test-tracking")


def _drop_stubbed_idp_common():
    """Remove any MagicMock `idp_common` entries a sibling test module installed."""
    for name in list(sys.modules):
        if name == "idp_common" or name.startswith("idp_common."):
            if isinstance(sys.modules[name], MagicMock):
                del sys.modules[name]


def _load_index():
    _drop_stubbed_idp_common()
    spec = importlib.util.spec_from_file_location(
        "reprocess_resolver_index", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["reprocess_resolver_index"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()
config_scope = importlib.import_module("idp_common.config_scope")


def _event(claims, version="tenant-a"):
    return {
        "info": {"fieldName": "reprocessDocument"},
        "arguments": {"objectKeys": ["acme/statement.pdf"], "version": version},
        "identity": {"claims": claims},
    }


def _author_claims(**extra):
    claims = {"cognito:groups": ["Author"], "email": "author@example.com"}
    claims.update(extra)
    return claims


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
def no_side_effects(monkeypatch):
    """Stub the work reprocessing would do, so only authorization is exercised."""
    monkeypatch.setattr(index, "reprocess_document", lambda *a, **k: None)
    monkeypatch.setattr(
        index, "sanitize_event_for_logging", lambda event: {"redacted": True}
    )
    monkeypatch.setattr(index.json, "dumps", json.dumps)


@pytest.mark.unit
class TestScopeFailsClosed:
    def test_no_email_claim_denies_without_querying(self, users_table):
        table = users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])

        with pytest.raises(PermissionError):
            index.handler(
                _event(
                    {
                        "cognito:groups": ["Author"],
                        "sub": "11111111-2222-3333-4444-555555555555",
                        "cognito:username": "author",
                    }
                ),
                None,
            )

        table.query.assert_not_called()

    def test_a_dynamodb_failure_denies(self, users_table):
        users_table(error=Exception("AccessDeniedException: dynamodb:Query"))

        with pytest.raises(PermissionError):
            index.handler(_event(_author_claims()), None)

    def test_an_unwired_users_table_denies(self, monkeypatch):
        monkeypatch.delenv("USERS_TABLE_NAME", raising=False)
        index._user_scope_cache.clear()

        with pytest.raises(PermissionError):
            index.handler(_event(_author_claims()), None)

    def test_an_empty_page_is_still_unrestricted(self, users_table):
        users_table(items=[])

        assert index.handler(_event(_author_claims()), None) is True

    def test_an_in_scope_version_is_accepted(self, users_table):
        users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])

        assert index.handler(_event(_author_claims()), None) is True

    def test_an_out_of_scope_version_is_refused(self, users_table):
        users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])

        with pytest.raises(PermissionError):
            index.handler(_event(_author_claims(), version="tenant-b"), None)


@pytest.mark.unit
class TestTheLookupIsTheSharedOne:
    def test_the_wrapper_delegates_to_idp_common(self, users_table):
        """One implementation of this rule, not a seventh copy of it."""
        users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])

        assert index._get_user_allowed_config_versions("author@example.com") == [
            "tenant-a"
        ]

    def test_the_wrapper_raises_the_shared_error_type(self, users_table):
        users_table(error=Exception("boom"))

        with pytest.raises(config_scope.ScopeLookupError):
            index._get_user_allowed_config_versions("author@example.com")
