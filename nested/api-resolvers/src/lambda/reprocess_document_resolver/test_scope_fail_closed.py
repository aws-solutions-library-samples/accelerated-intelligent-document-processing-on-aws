# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""reprocessDocument enforces config-version scope in both directions, fail-closed.

Two things must be in the caller's scope, and the second is what
`TestTheDocumentsOwnScopeIsChecked` covers: the `version` argument, which names the
Configuration Profile the documents will be re-run *under*; and the profile each
named document was *last processed under*, so the control does not stand down when
the caller simply omits the argument.

Both are only as good as the scope they compare against, and three outcomes have to
be distinguished:

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

# Captured before the autouse fixture below stubs it, for the few tests that drive
# the real tracking-table read rather than a fixed answer.
_REAL_DOCUMENT_CONFIG_VERSION = index._document_config_version


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
    """Stub the work reprocessing would do, so only authorization is exercised.

    The tracking-table read defaults to a document stamped `tenant-a`, so tests
    about the `version` argument are not also tripped by the document check.
    """
    monkeypatch.setattr(index, "reprocess_document", lambda *a, **k: None)
    monkeypatch.setattr(
        index, "sanitize_event_for_logging", lambda event: {"redacted": True}
    )
    monkeypatch.setattr(index.json, "dumps", json.dumps)
    monkeypatch.setattr(index, "_document_config_version", lambda key: "tenant-a")


@pytest.fixture
def document_version(monkeypatch):
    """Set the ConfigVersion the tracking table reports for every target."""

    def _configure(version):
        monkeypatch.setattr(index, "_document_config_version", lambda key: version)

    return _configure


@pytest.fixture
def real_document_lookup(monkeypatch):
    """The real tracking-table read, with the autouse stub lifted.

    Returned rather than only installed, so a test that depends on it says so by
    calling it. A fixture whose whole effect is a monkeypatch reads as unused, and
    then nothing distinguishes "this test drives the real function" from "this test
    forgot to ask for it and is asserting against the stub".
    """
    monkeypatch.setattr(
        index, "_document_config_version", _REAL_DOCUMENT_CONFIG_VERSION
    )
    return index._document_config_version


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
class TestTheDocumentsOwnScopeIsChecked:
    """The check must not depend on the caller volunteering a `version`.

    `version` names the profile the documents will be re-run *under*. Gating the
    whole control on it meant a scoped Author could reprocess any `objectKey` in
    the deployment by omitting the argument — including the documents the
    document-list resolvers already hide from them.
    """

    def test_a_document_outside_scope_is_refused_with_no_version_argument(
        self, users_table, document_version
    ):
        users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])
        document_version("tenant-b")

        with pytest.raises(PermissionError):
            index.handler(_event(_author_claims(), version=None), None)

    def test_an_in_scope_document_is_accepted_with_no_version_argument(
        self, users_table, document_version
    ):
        users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])
        document_version("tenant-a")

        assert index.handler(_event(_author_claims(), version=None), None) is True

    def test_an_unstamped_document_is_refused(self, users_table, document_version):
        """Fails closed: no profile name means it cannot be proven in scope."""
        users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])
        document_version(None)

        with pytest.raises(PermissionError):
            index.handler(_event(_author_claims(), version=None), None)

    def test_nothing_is_queued_when_one_document_of_a_batch_is_refused(
        self, users_table, monkeypatch
    ):
        """A batch is refused whole, not part-processed."""
        users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])
        versions = {"ok.pdf": "tenant-a", "nope.pdf": "tenant-b"}
        monkeypatch.setattr(index, "_document_config_version", versions.get)
        queued = []
        monkeypatch.setattr(
            index, "reprocess_document", lambda key, *a, **k: queued.append(key)
        )
        event = _event(_author_claims(), version=None)
        event["arguments"]["objectKeys"] = ["ok.pdf", "nope.pdf"]

        with pytest.raises(PermissionError):
            index.handler(event, None)

        assert queued == []

    def test_an_unreadable_tracking_row_refuses(
        self, users_table, monkeypatch, real_document_lookup
    ):
        """A failed GetItem cannot establish the profile, so it denies."""
        users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])
        service = MagicMock()
        service.get_document.side_effect = Exception("ProvisionedThroughputExceeded")
        monkeypatch.setattr(index, "document_service", service)

        # The read swallows its own error and reports "no version", which is what
        # the scope check then refuses. Asserted first so this test cannot pass on
        # the stubbed lookup.
        assert real_document_lookup("a.pdf") is None

        with pytest.raises(PermissionError):
            index.handler(_event(_author_claims(), version=None), None)

    def test_an_unrestricted_caller_is_not_document_scoped(
        self, users_table, document_version
    ):
        users_table(items=[])
        document_version("tenant-b")

        assert index.handler(_event(_author_claims(), version=None), None) is True

    def test_an_admin_is_not_document_scoped(self, users_table, document_version):
        users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])
        document_version("tenant-b")
        claims = {"cognito:groups": ["Admin"], "email": "admin@example.com"}

        assert index.handler(_event(claims, version=None), None) is True

    def test_the_document_version_comes_from_the_tracking_row(
        self, monkeypatch, real_document_lookup
    ):
        service = MagicMock()
        service.get_document.return_value = MagicMock(config_version="tenant-a")
        monkeypatch.setattr(index, "document_service", service)

        assert real_document_lookup("a.pdf") == "tenant-a"
        service.get_document.assert_called_once_with("a.pdf")

    def test_a_missing_tracking_row_yields_no_version(
        self, monkeypatch, real_document_lookup
    ):
        service = MagicMock()
        service.get_document.return_value = None
        monkeypatch.setattr(index, "document_service", service)

        assert real_document_lookup("a.pdf") is None


@pytest.mark.unit
class TestTheForwardDirectionStaysInScope:
    """Omitting `version` must not move a document OUT of the caller's scope.

    An unpinned reprocess reaches `queue_processor` with no `config_version`, which
    resolves the **globally active** profile — a value nothing scope-checks. So a
    caller scoped to `tenant-b`, reprocessing their own in-scope document with no
    `version`, would have it re-run under whatever is active and the tracking row
    stamped accordingly. A scoped caller's reprocess is therefore pinned to the
    document's own profile, which the backward check has just verified.
    """

    def _queued(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            index,
            "reprocess_document",
            lambda key, version=None, revision=None: calls.append((key, version)),
        )
        return calls

    def test_a_scoped_caller_is_pinned_to_the_documents_own_profile(
        self, users_table, document_version, monkeypatch
    ):
        users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])
        document_version("tenant-a")
        calls = self._queued(monkeypatch)

        index.handler(_event(_author_claims(), version=None), None)

        assert calls == [("acme/statement.pdf", "tenant-a")]

    def test_an_explicit_version_still_wins(
        self, users_table, document_version, monkeypatch
    ):
        """It has already been scope-checked, so the caller's choice stands."""
        users_table(items=[{"allowedConfigVersions": ["tenant-a", "tenant-a2"]}])
        document_version("tenant-a")
        calls = self._queued(monkeypatch)

        index.handler(_event(_author_claims(), version="tenant-a2"), None)

        assert calls == [("acme/statement.pdf", "tenant-a2")]

    def test_an_unscoped_caller_is_not_pinned(
        self, users_table, document_version, monkeypatch
    ):
        """Unchanged behaviour: no pin, so the active profile is resolved as before."""
        users_table(items=[])
        document_version("tenant-a")
        calls = self._queued(monkeypatch)

        index.handler(_event(_author_claims(), version=None), None)

        assert calls == [("acme/statement.pdf", None)]

    def test_an_admin_is_not_pinned(self, users_table, document_version, monkeypatch):
        users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])
        document_version("tenant-a")
        calls = self._queued(monkeypatch)

        index.handler(
            _event(
                {"cognito:groups": ["Admin"], "email": "admin@example.com"},
                version=None,
            ),
            None,
        )

        assert calls == [("acme/statement.pdf", None)]

    def test_each_document_is_pinned_to_its_own_profile(
        self, users_table, monkeypatch
    ):
        """A batch spanning two in-scope profiles keeps each document where it is."""
        users_table(items=[{"allowedConfigVersions": ["tenant-*"]}])
        versions = {"a.pdf": "tenant-a", "b.pdf": "tenant-b"}
        monkeypatch.setattr(index, "_document_config_version", versions.get)
        calls = self._queued(monkeypatch)
        event = _event(_author_claims(), version=None)
        event["arguments"]["objectKeys"] = ["a.pdf", "b.pdf"]

        index.handler(event, None)

        assert calls == [("a.pdf", "tenant-a"), ("b.pdf", "tenant-b")]


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
