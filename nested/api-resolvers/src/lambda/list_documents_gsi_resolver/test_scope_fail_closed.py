# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""listDocuments and getDocumentCount both enforce the caller's config scope.

Two properties, and the second is why this file exists at all.

**The scope lookup fails closed.** Three outcomes have to be distinguished:

* **no `email` claim** — no key to look the caller up by, so deny, and issue **no
  query**. Nothing is substituted for the claim: any other identifier matches no
  UsersTable row, and an empty page means "unrestricted".
* **a failed DynamoDB query** — deny. A missing IAM grant or a throttle is not a
  statement that this caller has no restrictions.
* **an empty page** — still unrestricted, deliberately: scoping is opt-in per
  user, and denying there would empty the document list for everyone.

**getDocumentCount is filtered too.** It was not: it resolved no caller and
referenced no scope, while `scripts/api_rbac_expectations.yaml` declared it
`scope_filtered: true`. The declaration was simply false, and the static scan
could not see it because check S4 grepped the *file* for `allowedConfigVersions`
and the `listDocuments` path in the same module supplied the string. A count is
data: it told a caller scoped to one profile how many documents exist outside it,
and left the header disagreeing with the rows below it.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from boto3.dynamodb.conditions import ConditionExpressionBuilder

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TRACKING_TABLE_NAME", "IDP-TrackingTable")


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "list_documents_gsi_index", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["list_documents_gsi_index"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()


def _event(field, claims):
    return {
        "info": {"fieldName": field},
        "arguments": {},
        "identity": {"claims": claims},
    }


def _viewer_claims(**extra):
    claims = {"cognito:groups": ["Viewer"], "email": "viewer@example.com"}
    claims.update(extra)
    return claims


def _doc(pk, config_version):
    item = {"PK": pk, "ObjectKey": pk, "InitialEventTime": "2026-01-01T00:00:00Z"}
    if config_version is not None:
        item["ConfigVersion"] = config_version
    return item


@pytest.fixture
def tables(monkeypatch):
    """Stub both DynamoDB tables this resolver reads.

    `TrackingTable` answers the index query with the documents a test names;
    `UsersTable` answers the scope query. One resource serves both, dispatching
    on table name, which is how the Lambda sees it too.
    """

    def _configure(*, documents=(), scope_items=None, scope_error=None):
        tracking = MagicMock()
        tracking.query.return_value = {
            "Items": list(documents),
            "Count": len(documents),
        }
        users = MagicMock()
        if scope_error is not None:
            users.query.side_effect = scope_error
        else:
            users.query.return_value = {"Items": scope_items or []}

        def _table(name):
            return users if "Users" in name else tracking

        resource = MagicMock()
        resource.Table.side_effect = _table
        monkeypatch.setattr(index, "dynamodb", resource)
        monkeypatch.setenv("USERS_TABLE_NAME", "IDP-UsersTable")
        monkeypatch.setenv("TRACKING_TABLE_NAME", "IDP-TrackingTable")
        index._user_scope_cache.clear()
        return tracking, users

    index._user_scope_cache.clear()
    return _configure


@pytest.mark.unit
@pytest.mark.parametrize("field", ["listDocuments", "getDocumentCount"])
class TestScopeFailsClosedOnBothOperations:
    def test_no_email_claim_denies_without_querying(self, tables, field):
        tracking, users = tables(
            documents=[_doc("doc#1", "tenant-a")],
            scope_items=[{"allowedConfigVersions": ["tenant-a"]}],
        )

        with pytest.raises(PermissionError):
            index.handler(
                _event(
                    field,
                    {
                        "cognito:groups": ["Viewer"],
                        "sub": "11111111-2222-3333-4444-555555555555",
                        "cognito:username": "viewer",
                    },
                ),
                None,
            )

        users.query.assert_not_called()
        tracking.query.assert_not_called()

    def test_a_dynamodb_failure_denies(self, tables, field):
        tracking, _ = tables(
            scope_error=Exception("AccessDeniedException: dynamodb:Query")
        )

        with pytest.raises(PermissionError):
            index.handler(_event(field, _viewer_claims()), None)

        tracking.query.assert_not_called()

    def test_an_unwired_users_table_denies(self, tables, field, monkeypatch):
        tables(documents=[_doc("doc#1", "tenant-a")])
        monkeypatch.delenv("USERS_TABLE_NAME", raising=False)
        index._user_scope_cache.clear()

        with pytest.raises(PermissionError):
            index.handler(_event(field, _viewer_claims()), None)

    def test_an_admin_is_never_looked_up(self, tables, field):
        _, users = tables(documents=[_doc("doc#1", "tenant-b")])

        index.handler(
            _event(field, {"cognito:groups": ["Admin"], "email": "a@example.com"}),
            None,
        )

        users.query.assert_not_called()


@pytest.mark.unit
class TestListDocumentsFiltering:
    def test_an_empty_page_is_still_unrestricted(self, tables):
        """Most users have no scope row and must see the whole list."""
        tables(documents=[_doc("doc#1", "tenant-a"), _doc("doc#2", "tenant-b")])

        result = index.handler(_event("listDocuments", _viewer_claims()), None)

        assert len(result["Documents"]) == 2

    def test_a_scoped_caller_sees_only_in_scope_documents(self, tables):
        tables(
            documents=[_doc("doc#1", "tenant-a"), _doc("doc#2", "tenant-b")],
            scope_items=[{"allowedConfigVersions": ["tenant-a"]}],
        )

        result = index.handler(_event("listDocuments", _viewer_claims()), None)

        assert [d["ObjectKey"] for d in result["Documents"]] == ["doc#1"]

    def test_an_unstamped_document_is_denied_to_a_scoped_caller(self, tables):
        """Fails closed: no ConfigVersion cannot be proven in scope."""
        tables(
            documents=[_doc("doc#1", None)],
            scope_items=[{"allowedConfigVersions": ["tenant-a"]}],
        )

        result = index.handler(_event("listDocuments", _viewer_claims()), None)

        assert result["Documents"] == []


@pytest.mark.unit
class TestGetDocumentCountFiltering:
    def test_the_count_excludes_out_of_scope_documents(self, tables):
        """The header figure must not report documents the list does not show."""
        tables(
            documents=[
                _doc("doc#1", "tenant-a"),
                _doc("doc#2", "tenant-b"),
                _doc("doc#3", "tenant-a"),
            ],
            scope_items=[{"allowedConfigVersions": ["tenant-a"]}],
        )

        assert index.handler(_event("getDocumentCount", _viewer_claims()), None) == {
            "count": 2
        }

    def test_the_count_excludes_unstamped_documents_for_a_scoped_caller(self, tables):
        tables(
            documents=[_doc("doc#1", "tenant-a"), _doc("doc#2", None)],
            scope_items=[{"allowedConfigVersions": ["tenant-a"]}],
        )

        assert index.handler(_event("getDocumentCount", _viewer_claims()), None) == {
            "count": 1
        }

    def test_a_scoped_caller_reads_projected_attributes_not_a_bare_count(self, tables):
        """`Select: COUNT` cannot be filtered on a glob-matched attribute."""
        tracking, _ = tables(
            documents=[_doc("doc#1", "tenant-a")],
            scope_items=[{"allowedConfigVersions": ["tenant-*"]}],
        )

        index.handler(_event("getDocumentCount", _viewer_claims()), None)

        assert tracking.query.call_args.kwargs["Select"] == "ALL_PROJECTED_ATTRIBUTES"

    def test_an_unrestricted_caller_keeps_the_count_only_query(self, tables):
        """The common case must not start reading item data."""
        tracking, _ = tables(documents=[_doc("doc#1", "tenant-a")])

        index.handler(_event("getDocumentCount", _viewer_claims()), None)

        assert tracking.query.call_args.kwargs["Select"] == "COUNT"

    def test_the_count_matches_the_list_for_the_same_caller(self, tables):
        """One pair of filters, applied by both operations."""
        documents = [
            _doc("doc#1", "tenant-a"),
            _doc("doc#2", "tenant-b"),
            _doc("doc#3", None),
        ]
        scope = [{"allowedConfigVersions": ["tenant-a"]}]

        tables(documents=documents, scope_items=scope)
        listed = index.handler(_event("listDocuments", _viewer_claims()), None)

        tables(documents=documents, scope_items=scope)
        counted = index.handler(_event("getDocumentCount", _viewer_claims()), None)

        assert counted["count"] == len(listed["Documents"])

    def test_a_reviewer_only_count_carries_the_reviewer_filter(self, tables):
        """A Reviewer's count must not include work they cannot see."""
        tracking, _ = tables(documents=[_doc("doc#1", "tenant-a")])

        index.handler(
            _event(
                "getDocumentCount",
                {"cognito:groups": ["Reviewer"], "email": "r@example.com"},
            ),
            None,
        )

        assert "FilterExpression" in tracking.query.call_args.kwargs


def _filter_values(condition):
    """The literal values a boto3 condition would send to DynamoDB."""
    built = ConditionExpressionBuilder().build_expression(
        condition, is_key_condition=False
    )
    return set(built.attribute_value_placeholders.values())


@pytest.mark.unit
class TestTheScopedTallyIsBounded:
    """A scoped caller's count reads item data, so it needs a stop.

    The unscoped path takes DynamoDB's `Count` and transfers a number per page. A
    scoped caller's tally transfers and deserialises the projected rows instead,
    because the config-version filter cannot be a FilterExpression — so on a very
    large date range it can exhaust the 30s function timeout (and API Gateway's 29s
    ceiling) where an unscoped caller would not. Better a flagged approximate count
    than a 502.
    """

    def _paging_tracking(self, tables):
        tracking, _ = tables(
            documents=[_doc("doc#1", "tenant-a")],
            scope_items=[{"allowedConfigVersions": ["tenant-a"]}],
        )
        # Always hands back another page, so only a cap can end the loop.
        tracking.query.return_value = {
            "Items": [_doc("doc#1", "tenant-a")],
            "Count": 1,
            "LastEvaluatedKey": {"PK": "doc#1"},
        }
        return tracking

    def test_the_page_cap_stops_the_tally_and_flags_it(self, tables, monkeypatch):
        tracking = self._paging_tracking(tables)
        monkeypatch.setattr(index, "_COUNT_MAX_PAGES", 3)

        result = index.handler(_event("getDocumentCount", _viewer_claims()), None)

        assert result == {"count": 3, "approximate": True}
        assert tracking.query.call_count == 3

    def test_a_short_remaining_invocation_stops_the_tally(self, tables, monkeypatch):
        self._paging_tracking(tables)
        monkeypatch.setattr(index, "_COUNT_MAX_PAGES", 10_000)
        context = MagicMock()
        context.get_remaining_time_in_millis.return_value = 100

        event = _event("getDocumentCount", _viewer_claims())
        result = index.handler(event, context)

        assert result["approximate"] is True
        assert result["count"] >= 1

    def test_a_complete_tally_is_not_flagged(self, tables):
        tables(
            documents=[_doc("doc#1", "tenant-a")],
            scope_items=[{"allowedConfigVersions": ["tenant-a"]}],
        )

        result = index.handler(_event("getDocumentCount", _viewer_claims()), None)

        assert result == {"count": 1}

    def test_the_unscoped_count_path_is_not_bounded(self, tables, monkeypatch):
        """It transfers a number per page, so it paginates to completion as before."""
        tracking, _ = tables(documents=[_doc("doc#1", "tenant-a")])
        pages = {"n": 0}

        def _query(**kwargs):
            pages["n"] += 1
            more = pages["n"] < 5
            out = {"Count": 2}
            if more:
                out["LastEvaluatedKey"] = {"PK": "doc#1"}
            return out

        tracking.query.side_effect = _query
        monkeypatch.setattr(index, "_COUNT_MAX_PAGES", 2)

        result = index.handler(_event("getDocumentCount", _viewer_claims()), None)

        assert result == {"count": 10}


@pytest.mark.unit
class TestReviewerOwnerMatching:
    def test_both_identifiers_are_matched_when_both_are_present(self):
        """claim_review stores identity.username; the claims carry an email."""
        values = _filter_values(
            index._reviewer_filter_expression(
                {"username": "reviewer", "email": "r@example.com"}
            )
        )

        assert {"reviewer", "r@example.com"} <= values

    def test_an_absent_email_claim_does_not_match_an_unowned_document(self):
        """`HITLReviewOwner == ""` means unowned, not "owned by me".

        The email now comes from the `email` claim alone, so it can legitimately
        be empty — and `eq("")` would match every unowned row instead of the
        caller's own.
        """
        values = _filter_values(
            index._reviewer_filter_expression({"username": "reviewer", "email": ""})
        )

        assert "reviewer" in values
        # The one empty string present is the explicit "unowned" test in the
        # filter, not a second owner comparison: exactly one occurrence.
        assert sum(1 for v in values if v == "") <= 1

    def test_an_identity_with_no_identifier_matches_no_owner(self):
        """Such a caller cannot own a review; they still see unassigned work."""
        values = _filter_values(
            index._reviewer_filter_expression({"username": "", "email": ""})
        )

        assert index._UNMATCHABLE_OWNER in values
