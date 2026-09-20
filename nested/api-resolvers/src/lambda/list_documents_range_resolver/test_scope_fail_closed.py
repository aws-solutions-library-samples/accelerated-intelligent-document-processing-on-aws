# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""listDocumentsByDateRange enforces the caller's config-version scope.

The three outcomes this resolver has to distinguish:

* **no `email` claim** — no key to look the caller up by, so deny, and issue **no
  query**. Nothing is substituted for the claim: any other identifier matches no
  UsersTable row, and an empty page means "unrestricted".
* **a failed DynamoDB query** — deny. A missing IAM grant or a throttle is not a
  statement that this caller has no restrictions.
* **an empty page** — still unrestricted, deliberately: scoping is opt-in per
  user, and denying there would empty the document list for everyone.

The scope is resolved before any shard is read, so a request that will be refused
does not first iterate the range's partitions.

The second property here is about the reviewer-only view rather than the scope:
`HITLReviewOwner == ""` means *unowned*, and the caller's email may now legitimately
be empty, so the two empties must not compare equal and hand a Reviewer somebody
else's unowned completed work.
"""

import importlib
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TRACKING_TABLE_NAME", "IDP-TrackingTable")


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "list_documents_range_index", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["list_documents_range_index"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()
config_scope = importlib.import_module("config_scope")


def _event(claims):
    return {
        "info": {"fieldName": "listDocumentsByDateRange"},
        "arguments": {
            "startDateTime": "2026-01-01T00:00:00Z",
            "endDateTime": "2026-01-01T01:00:00Z",
        },
        "identity": {"claims": claims},
    }


def _viewer_claims(**extra):
    claims = {"cognito:groups": ["Viewer"], "email": "viewer@example.com"}
    claims.update(extra)
    return claims


def _doc(key, config_version):
    doc = {"ObjectKey": key, "InitialEventTime": "2026-01-01T00:30:00Z"}
    if config_version is not None:
        doc["ConfigVersion"] = config_version
    return doc


@pytest.fixture
def documents(monkeypatch):
    """Serve the list entries and document bodies a test names."""

    def _configure(docs):
        entries = [
            {"PK": f"list#{d['ObjectKey']}", "SK": "e", "ObjectKey": d["ObjectKey"]}
            for d in docs
        ]
        monkeypatch.setattr(
            index, "_query_shard", lambda *a, **k: entries if a[2] == 0 else []
        )
        monkeypatch.setattr(
            index,
            "_batch_get_documents",
            lambda table_name, keys: {d["ObjectKey"]: dict(d) for d in docs},
        )

    return _configure


@pytest.fixture
def users_table(monkeypatch):
    """Point the scope lookup at a UsersTable double and clear its cache."""

    def _configure(*, items=None, error=None, store=None):
        """A UsersTable double covering BOTH key spaces the lookup reads.

        ``items`` is the ``EmailIndex`` query page. ``store`` maps a raw ``PK``
        string to the item stored under it, which is how the ``sub`` join is
        modelled: a ``SUB#<sub>`` pointer carrying a ``userId``, and the
        ``USER#<userId>`` row it names. Modelling both matters — a double that
        answers only ``query`` leaves ``get_item`` returning a truthy Mock, so a
        test can pass while the code reads a row that does not exist.
        """
        table = MagicMock()
        if error is not None:
            table.query.side_effect = error
            table.get_item.side_effect = error
        else:
            table.query.return_value = {"Items": items or []}
            _store = dict(store or {})
            table.get_item.side_effect = lambda Key: (
                {"Item": _store[Key["PK"]]} if Key["PK"] in _store else {}
            )
        resource = MagicMock()
        resource.Table.return_value = table
        monkeypatch.setattr(index, "dynamodb", resource)
        monkeypatch.setenv("USERS_TABLE_NAME", "IDP-UsersTable")
        monkeypatch.setenv("TRACKING_TABLE_NAME", "IDP-TrackingTable")
        index._user_scope_cache.clear()
        return table

    index._user_scope_cache.clear()
    return _configure


@pytest.mark.unit
class TestScopeFailsClosed:
    def test_no_email_claim_denies_without_querying(self, users_table, documents):
        documents([_doc("a.pdf", "tenant-a")])
        table = users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])

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

    def test_a_dynamodb_failure_denies(self, users_table, documents):
        documents([_doc("a.pdf", "tenant-a")])
        users_table(error=Exception("AccessDeniedException: dynamodb:Query"))

        with pytest.raises(PermissionError):
            index.handler(_event(_viewer_claims()), None)

    def test_an_unwired_users_table_denies(self, users_table, documents, monkeypatch):
        documents([_doc("a.pdf", "tenant-a")])
        users_table(items=[])
        monkeypatch.delenv("USERS_TABLE_NAME", raising=False)
        index._user_scope_cache.clear()

        with pytest.raises(PermissionError):
            index.handler(_event(_viewer_claims()), None)

    def test_the_denial_precedes_any_shard_read(self, users_table, monkeypatch):
        """No point iterating the range's partitions for a refused request."""
        users_table(error=Exception("boom"))
        shard_reads = []
        monkeypatch.setattr(
            index, "_query_shard", lambda *a, **k: shard_reads.append(a) or []
        )

        with pytest.raises(PermissionError):
            index.handler(_event(_viewer_claims()), None)

        assert shard_reads == []

    def test_an_empty_page_is_still_unrestricted(self, users_table, documents):
        documents([_doc("a.pdf", "tenant-a"), _doc("b.pdf", "tenant-b")])
        users_table(items=[])

        result = index.handler(_event(_viewer_claims()), None)

        assert len(result["Documents"]) == 2

    def test_a_scoped_caller_sees_only_in_scope_documents(self, users_table, documents):
        documents([_doc("a.pdf", "tenant-a"), _doc("b.pdf", "tenant-b")])
        users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])

        result = index.handler(_event(_viewer_claims()), None)

        assert [d["ObjectKey"] for d in result["Documents"]] == ["a.pdf"]

    def test_an_unstamped_document_is_denied_to_a_scoped_caller(
        self, users_table, documents
    ):
        documents([_doc("a.pdf", None)])
        users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])

        result = index.handler(_event(_viewer_claims()), None)

        assert result["Documents"] == []

    def test_an_admin_is_never_looked_up(self, users_table, documents):
        documents([_doc("a.pdf", "tenant-b")])
        table = users_table(items=[{"allowedConfigVersions": ["tenant-a"]}])

        result = index.handler(
            _event({"cognito:groups": ["Admin"], "email": "a@example.com"}), None
        )

        assert len(result["Documents"]) == 1
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
    def test_only_an_email_claim_becomes_the_scope_key(self, claims):
        caller = index._get_caller_identity({"identity": {"claims": claims}})

        assert caller["email"] == ""

    def test_the_wrapper_raises_the_shared_error_type(self, users_table):
        """One implementation of this rule, not a seventh copy of it."""
        users_table(error=Exception("boom"))

        with pytest.raises(config_scope.ScopeLookupError):
            index._get_user_allowed_config_versions("viewer@example.com")


@pytest.mark.unit
class TestReviewerOwnerMatching:
    def _reviewer(self, email):
        return {
            "username": "reviewer",
            "email": email,
            "groups": ["Reviewer"],
            "is_admin": False,
            "is_author": False,
            "is_reviewer": True,
            "is_viewer": False,
        }

    def test_an_unowned_completed_review_is_not_mine_when_i_have_no_email(self):
        """An empty owner must not compare equal to an empty caller email."""
        doc = {
            "HITLTriggered": True,
            "HITLCompleted": True,
            "HITLReviewOwner": "",
            "ConfigVersion": "tenant-a",
        }

        included = index._should_include_document(
            doc, self._reviewer(""), True, None
        )

        assert included is False

    def test_my_own_completed_review_is_still_mine(self):
        doc = {
            "HITLTriggered": True,
            "HITLCompleted": True,
            "HITLReviewOwner": "reviewer",
            "ConfigVersion": "tenant-a",
        }

        assert (
            index._should_include_document(doc, self._reviewer(""), True, None) is True
        )

    def test_a_review_owned_by_my_email_is_mine(self):
        doc = {
            "HITLTriggered": True,
            "HITLCompleted": True,
            "HITLReviewOwner": "r@example.com",
            "ConfigVersion": "tenant-a",
        }

        assert (
            index._should_include_document(
                doc, self._reviewer("r@example.com"), True, None
            )
            is True
        )
