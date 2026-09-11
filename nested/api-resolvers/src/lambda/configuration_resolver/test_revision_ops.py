# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Configuration Profile revision operations in the configuration resolver.

The security properties under test:

- **Group gate.** Every revision operation re-checks the caller's Cognito group
  server-side, so a missing schema directive cannot make it reachable.
- **Profile-level scope.** Scope is checked at the PROFILE for all five
  operations. A revision is content inside a profile, never its own RBAC object,
  so there is exactly one check to get right — and it must reject an
  out-of-scope profile before any work happens.
- **Reserved names.** A profile may not be named after a sentinel record.
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


def _event(field, args=None, groups=("Admin",), email="admin@example.com"):
    return {
        "info": {"fieldName": field},
        "arguments": args or {},
        "identity": {"claims": {"cognito:groups": list(groups), "email": email}},
    }


@pytest.fixture
def manager(monkeypatch):
    """Stub ConfigurationManager so the handler runs without AWS."""
    fake = MagicMock()
    fake.list_revisions.return_value = [
        {"revision": 2, "createdBy": "a@example.com", "published": True},
        {"revision": 1, "createdBy": "system", "published": False},
    ]
    fake.get_revision.return_value = {"notes": "hello"}
    fake.restore_revision.return_value = 3
    fake.label_revision.return_value = True
    fake.delete_revision.return_value = True
    monkeypatch.setattr(index, "ConfigurationManager", lambda *a, **k: fake)
    # Default to an unscoped caller unless a test says otherwise.
    monkeypatch.setattr(index, "_get_user_allowed_config_versions", lambda email: None)
    return fake


@pytest.mark.unit
class TestHappyPath:
    def test_list_returns_revisions(self, manager):
        result = index.handler(_event("listConfigProfileRevisions", {"profileName": "lending"}), None)
        assert result["success"] is True
        assert [r["revision"] for r in result["revisions"]] == [2, 1]
        manager.list_revisions.assert_called_once_with("lending")

    def test_get_returns_the_configuration_as_json(self, manager):
        result = index.handler(
            _event("getConfigProfileRevision", {"profileName": "lending", "revision": 1}), None
        )
        assert result["success"] is True
        assert '"notes": "hello"' in result["config"] or '"notes":"hello"' in result["config"]

    def test_restore_reports_the_new_revision(self, manager):
        result = index.handler(
            _event(
                "restoreConfigProfileRevision",
                {"profileName": "lending", "revision": 1},
                groups=("Author",),
                email="author@example.com",
            ),
            None,
        )
        assert result["success"] is True
        assert result["revision"] == 3
        manager.restore_revision.assert_called_once_with(
            "lending", 1, created_by="author@example.com"
        )

    def test_label_passes_label_and_notes_through(self, manager):
        result = index.handler(
            _event(
                "labelConfigProfileRevision",
                {"profileName": "lending", "revision": 1, "label": "known good", "notes": "n"},
                groups=("Author",),
            ),
            None,
        )
        assert result["success"] is True
        manager.label_revision.assert_called_once_with(
            "lending", 1, label="known good", notes="n"
        )

    def test_delete_succeeds_for_admin(self, manager):
        result = index.handler(
            _event("deleteConfigProfileRevision", {"profileName": "lending", "revision": 1}), None
        )
        assert result["success"] is True


@pytest.mark.unit
class TestGroupEnforcement:
    @pytest.mark.parametrize(
        "field,args",
        [
            ("listConfigProfileRevisions", {"profileName": "p"}),
            ("getConfigProfileRevision", {"profileName": "p", "revision": 1}),
            ("restoreConfigProfileRevision", {"profileName": "p", "revision": 1}),
            ("labelConfigProfileRevision", {"profileName": "p", "revision": 1}),
            ("deleteConfigProfileRevision", {"profileName": "p", "revision": 1}),
        ],
    )
    def test_reviewer_is_refused_every_revision_operation(self, manager, field, args):
        with pytest.raises(Exception, match="Unauthorized"):
            index.handler(_event(field, args, groups=("Reviewer",)), None)

    @pytest.mark.parametrize(
        "field", ["restoreConfigProfileRevision", "labelConfigProfileRevision"]
    )
    def test_viewer_cannot_write(self, manager, field):
        with pytest.raises(Exception, match="Unauthorized"):
            index.handler(
                _event(field, {"profileName": "p", "revision": 1}, groups=("Viewer",)), None
            )

    def test_author_cannot_delete_a_revision(self, manager):
        """Deleting history is Admin-only, mirroring deleteDocumentVersion."""
        with pytest.raises(Exception, match="Unauthorized"):
            index.handler(
                _event(
                    "deleteConfigProfileRevision",
                    {"profileName": "p", "revision": 1},
                    groups=("Author",),
                ),
                None,
            )

    def test_viewer_can_read_history(self, manager):
        result = index.handler(
            _event("listConfigProfileRevisions", {"profileName": "p"}, groups=("Viewer",)), None
        )
        assert result["success"] is True


@pytest.mark.unit
class TestProfileScope:
    @pytest.fixture
    def scoped(self, manager, monkeypatch):
        monkeypatch.setattr(
            index, "_get_user_allowed_config_versions", lambda email: ["lending"]
        )
        return manager

    @pytest.mark.parametrize(
        "field,args",
        [
            ("listConfigProfileRevisions", {"profileName": "claims"}),
            ("getConfigProfileRevision", {"profileName": "claims", "revision": 1}),
            ("restoreConfigProfileRevision", {"profileName": "claims", "revision": 1}),
            ("labelConfigProfileRevision", {"profileName": "claims", "revision": 1}),
        ],
    )
    def test_out_of_scope_profile_is_denied(self, scoped, field, args):
        result = index.handler(_event(field, args, groups=("Author",)), None)
        assert result["success"] is False
        assert result["error"]["type"] == "Unauthorized"
        scoped.list_revisions.assert_not_called()
        scoped.restore_revision.assert_not_called()

    def test_in_scope_profile_is_allowed(self, scoped):
        result = index.handler(
            _event(
                "listConfigProfileRevisions", {"profileName": "lending"}, groups=("Author",)
            ),
            None,
        )
        assert result["success"] is True

    def test_scope_denial_precedes_argument_validation(self, scoped):
        """An out-of-scope caller must not be able to probe argument handling."""
        result = index.handler(
            _event(
                "getConfigProfileRevision",
                {"profileName": "claims", "revision": "not-a-number"},
                groups=("Author",),
            ),
            None,
        )
        assert result["error"]["type"] == "Unauthorized"

    def test_a_glob_scope_entry_matches_a_lineage(self, manager, monkeypatch):
        monkeypatch.setattr(
            index, "_get_user_allowed_config_versions", lambda email: ["usecaseA_*"]
        )
        allowed = index.handler(
            _event(
                "listConfigProfileRevisions", {"profileName": "usecaseA_v2"}, groups=("Author",)
            ),
            None,
        )
        denied = index.handler(
            _event(
                "listConfigProfileRevisions", {"profileName": "usecaseB_v1"}, groups=("Author",)
            ),
            None,
        )
        assert allowed["success"] is True
        assert denied["success"] is False

    def test_admin_scope_is_ignored(self, manager, monkeypatch):
        """Admins are always unrestricted; the scope lookup is skipped for them."""

        def explode(email):
            raise AssertionError("admin scope must not be looked up")

        monkeypatch.setattr(index, "_get_user_allowed_config_versions", explode)
        result = index.handler(
            _event("listConfigProfileRevisions", {"profileName": "anything"}), None
        )
        assert result["success"] is True


@pytest.mark.unit
class TestArgumentValidation:
    def test_missing_profile_name_is_rejected(self, manager):
        result = index.handler(_event("listConfigProfileRevisions", {}), None)
        assert result["success"] is False
        assert result["error"]["type"] == "ValidationError"

    def test_non_numeric_revision_is_rejected(self, manager):
        result = index.handler(
            _event("getConfigProfileRevision", {"profileName": "p", "revision": "abc"}), None
        )
        assert result["success"] is False
        assert result["error"]["type"] == "ValidationError"

    def test_missing_revision_body_is_reported_as_not_found(self, manager):
        manager.get_revision.return_value = None
        result = index.handler(
            _event("getConfigProfileRevision", {"profileName": "p", "revision": 9}), None
        )
        assert result["success"] is False
        assert result["error"]["type"] == "NotFound"

    def test_restoring_a_pruned_revision_returns_a_validation_error(self, manager):
        manager.restore_revision.side_effect = ValueError("no longer available")
        result = index.handler(
            _event("restoreConfigProfileRevision", {"profileName": "p", "revision": 9}), None
        )
        assert result["success"] is False
        assert result["error"]["type"] == "ValidationError"

    def test_deleting_the_published_revision_returns_a_validation_error(self, manager):
        manager.delete_revision.side_effect = ValueError("current configuration")
        result = index.handler(
            _event("deleteConfigProfileRevision", {"profileName": "p", "revision": 2}), None
        )
        assert result["success"] is False
        assert result["error"]["type"] == "ValidationError"


@pytest.mark.unit
class TestReservedNames:
    def test_reserved_profile_name_is_not_a_valid_version_name(self):
        assert index.validate_version_name("__active") is False
        assert index.validate_version_name("lending") is True

    def test_reserved_profile_name_is_refused_by_revision_operations(self, manager):
        result = index.handler(
            _event("listConfigProfileRevisions", {"profileName": "__active"}), None
        )
        assert result["success"] is False
        assert result["error"]["type"] == "ValidationError"
