# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Config prefix mapping operations in the configuration resolver.

The properties under test:

- **CRUD is Admin-only, server-side.** A mapping ASSIGNS a Configuration Profile,
  and a document's profile is the document-visibility partition for every scoped
  user — so writing one decides who can see the documents landing under that
  prefix. A missing schema directive must not make it reachable.
- **A mapping that could never resolve is refused at write time.** Discovering a
  typo at ingest means the operator who notices is not the one who made it.
- **A pinned revision is protected from retention, and the pin is taken AFTER the
  mapping is written.** `prune()` spares the published, labelled and
  test-run-pinned revisions and nothing else, so a revision-pinned mapping is a
  fourth referent it does not know about. `PrefixMappingStore.delete` never
  unpins — a test run may have pinned the same revision and nothing records which
  referent asked — so a pin taken *before* a put that then fails is permanent,
  with no mapping referencing it and nothing that can ever release it. Pinning
  second makes the only possible failure the recoverable one: the mapping is
  saved and the admin is told the revision is unprotected.
- **The dry run does not leak profile names.** It is the one prefix-mapping
  operation a non-Admin may call, and the filter is over *every profile the
  answer could disclose* rather than the one it selected — a rejection resolves
  to no profile while naming the mapped one, and metadata precedence resolves to
  the caller's own while explaining it beat the mapping's. An out-of-scope caller
  learns that the destination is out of scope and nothing else, not even the
  mapping prefix.
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
        "config_resolver_index_prefix", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["config_resolver_index_prefix"] = module
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
def store():
    """A stand-in for PrefixMappingStore, recording what the resolver asked for."""
    fake = MagicMock()
    fake.list.return_value = []
    fake.put.return_value = {"prefix": "acme/", "configProfile": "lending"}
    fake.delete.return_value = True
    return fake


@pytest.fixture
def manager(monkeypatch, store):
    # spec'd against the REAL ConfigurationManager, not a bare MagicMock. A bare
    # mock accepts any attribute with any signature, so it cannot see a call that
    # would raise on a live stack -- which is how
    # `get_raw_configuration(profile)` shipped past these tests when the real
    # method takes `(config_type, version)`. `spec=` makes a wrong name or a wrong
    # arity a test failure here instead of a 500 in the deployed resolver.
    from idp_common.config.configuration_manager import ConfigurationManager

    fake = MagicMock(spec=ConfigurationManager)
    # A profile that exists, with a retained revision and a published one.
    fake.get_raw_configuration.return_value = {"notes": "exists"}
    fake.get_revision.return_value = {"notes": "a revision body"}
    fake.mark_revision_pinned.return_value = True
    fake.resolve_active_version.return_value = "active"
    fake.resolve_published_revision.return_value = 4
    # A MagicMock attribute is truthy, so without this every deleteConfigVersion
    # test stops at the stack-managed guard before reaching the one under test.
    fake.get_configuration.return_value.managed = False
    # `table` is set in __init__, so a class-level spec does not carry it. The
    # default is a profile head that EXISTS, since most cases need the put to
    # proceed past the existence probe.
    fake.table = MagicMock()
    fake.table.get_item.return_value = {"Item": {"Configuration": "Config#lending"}}
    monkeypatch.setattr(index, "ConfigurationManager", lambda *a, **k: fake)
    monkeypatch.setattr(index, "_prefix_mapping_store", lambda _m: store)
    monkeypatch.setattr(
        index, "_get_user_allowed_config_versions", lambda email, sub="": None
    )
    return fake


# ---------------------------------------------------------------------------
# The group gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "field,args",
    [
        ("listConfigPrefixMappings", {}),
        ("putConfigPrefixMapping", {"prefix": "acme/", "configProfile": "lending"}),
        ("deleteConfigPrefixMapping", {"prefix": "acme/"}),
    ],
)
@pytest.mark.parametrize("group", ["Author", "Viewer", "Reviewer", "Annotator"])
def test_mapping_crud_is_admin_only(manager, field, args, group):
    """Not even an Author: a mapping assigns an access-control object."""
    with pytest.raises(Exception, match="Unauthorized"):
        index.handler(_event(field, args, groups=(group,)), None)


@pytest.mark.unit
@pytest.mark.parametrize("group", ["Admin", "Author", "Viewer"])
def test_the_dry_run_is_readable_by_anyone_who_can_upload_or_review(manager, group):
    result = index.handler(
        _event(
            "resolveConfigPrefixMapping",
            {"objectKey": "acme/x.pdf"},
            groups=(group,),
        ),
        None,
    )
    assert result["success"] is True


@pytest.mark.unit
@pytest.mark.parametrize("group", ["Reviewer", "Annotator"])
def test_the_dry_run_is_refused_to_roles_that_cannot_upload(manager, group):
    with pytest.raises(Exception, match="Unauthorized"):
        index.handler(
            _event(
                "resolveConfigPrefixMapping",
                {"objectKey": "acme/x.pdf"},
                groups=(group,),
            ),
            None,
        )


# ---------------------------------------------------------------------------
# list / delete
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_returns_the_mappings(manager, store):
    store.list.return_value = [
        {"prefix": "acme/invoices/", "configProfile": "invoices"},
        {"prefix": "acme/", "configProfile": "lending"},
    ]
    result = index.handler(_event("listConfigPrefixMappings"), None)
    assert result["success"] is True
    assert [m["prefix"] for m in result["mappings"]] == ["acme/invoices/", "acme/"]


@pytest.mark.unit
def test_delete_reports_a_missing_mapping_rather_than_succeeding(manager, store):
    store.delete.return_value = False
    result = index.handler(
        _event("deleteConfigPrefixMapping", {"prefix": "nope/"}), None
    )
    assert result["success"] is False
    assert result["error"]["type"] == "NotFound"


@pytest.mark.unit
def test_delete_succeeds(manager, store):
    result = index.handler(
        _event("deleteConfigPrefixMapping", {"prefix": "acme/"}), None
    )
    assert result["success"] is True
    store.delete.assert_called_once_with("acme/")


# ---------------------------------------------------------------------------
# put: refusing a mapping that could never resolve
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_put_creates_a_mapping(manager, store):
    result = index.handler(
        _event(
            "putConfigPrefixMapping",
            {
                "prefix": "acme/",
                "configProfile": "lending",
                "metadataPrecedence": "reject",
                "description": "Regulated intake",
            },
        ),
        None,
    )
    assert result["success"] is True
    kwargs = store.put.call_args.kwargs
    assert store.put.call_args.args == ("acme/", "lending")
    assert kwargs["metadata_precedence"] == "reject"
    assert kwargs["actor"] == "admin@example.com"


@pytest.mark.unit
@pytest.mark.parametrize("prefix", ["", "/", "/acme/", "acme//x/", "../etc/"])
def test_put_refuses_an_unusable_prefix(manager, store, prefix):
    result = index.handler(
        _event(
            "putConfigPrefixMapping",
            {"prefix": prefix, "configProfile": "lending"},
        ),
        None,
    )
    assert result["success"] is False
    assert result["error"]["type"] == "ValidationError"
    store.put.assert_not_called()


@pytest.mark.unit
def test_put_refuses_a_profile_that_does_not_exist(manager, store):
    """Refusing here beats discovering it at ingest, where the operator who notices
    is not the one who made the typo."""
    # The probe is a projected GetItem on the profile head, so "absent" is an
    # empty response rather than a None configuration.
    manager.table.get_item.return_value = {}
    result = index.handler(
        _event(
            "putConfigPrefixMapping",
            {"prefix": "acme/", "configProfile": "typo"},
        ),
        None,
    )
    assert result["success"] is False
    assert result["error"]["type"] == "NotFound"
    store.put.assert_not_called()


@pytest.mark.unit
def test_put_refuses_a_revision_that_is_not_retained(manager, store):
    manager.get_revision.return_value = None
    result = index.handler(
        _event(
            "putConfigPrefixMapping",
            {"prefix": "acme/", "configProfile": "lending", "configRevision": 99},
        ),
        None,
    )
    assert result["success"] is False
    assert result["error"]["type"] == "NotFound"
    store.put.assert_not_called()


@pytest.mark.unit
def test_put_refuses_a_non_numeric_revision(manager, store):
    result = index.handler(
        _event(
            "putConfigPrefixMapping",
            {"prefix": "acme/", "configProfile": "lending", "configRevision": "seven"},
        ),
        None,
    )
    assert result["success"] is False
    assert result["error"]["type"] == "ValidationError"


@pytest.mark.unit
def test_put_refuses_an_unknown_conflict_mode(manager, store):
    result = index.handler(
        _event(
            "putConfigPrefixMapping",
            {
                "prefix": "acme/",
                "configProfile": "lending",
                "metadataPrecedence": "whatever",
            },
        ),
        None,
    )
    assert result["success"] is False
    assert result["error"]["type"] == "ValidationError"
    store.put.assert_not_called()


# ---------------------------------------------------------------------------
# put: the retention pin
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pinning_a_revision_protects_it_from_retention(manager, store):
    index.handler(
        _event(
            "putConfigPrefixMapping",
            {"prefix": "acme/", "configProfile": "lending", "configRevision": 3},
        ),
        None,
    )
    manager.mark_revision_pinned.assert_called_once_with("lending", 3)


@pytest.mark.unit
def test_a_failed_pin_is_reported_rather_than_passing_silently(manager, store):
    """The revision body would otherwise stay prunable under a mapping naming it --
    a time bomb that fires whenever retention next runs.

    The mapping IS created: the pin is taken after the put, because
    `PrefixMappingStore.delete` never unpins, so a pin taken first and then
    orphaned by a failed put can never be released. So the honest outcome here is a
    saved mapping plus an error telling the admin the revision is unprotected.
    """
    manager.mark_revision_pinned.return_value = False
    result = index.handler(
        _event(
            "putConfigPrefixMapping",
            {"prefix": "acme/", "configProfile": "lending", "configRevision": 3},
        ),
        None,
    )
    assert result["success"] is False
    assert "could not be protected from retention" in result["error"]["message"]
    store.put.assert_called_once()


@pytest.mark.unit
def test_an_unpinned_mapping_takes_no_pin(manager, store):
    """It follows the published revision, which retention already protects."""
    index.handler(
        _event(
            "putConfigPrefixMapping", {"prefix": "acme/", "configProfile": "lending"}
        ),
        None,
    )
    manager.mark_revision_pinned.assert_not_called()
    assert store.put.call_args.kwargs["config_revision"] is None


@pytest.mark.unit
def test_a_concurrent_write_is_reported_as_a_conflict_not_a_success(manager, store):
    from idp_common.config.prefix_mappings import PrefixMappingConflict

    store.put.side_effect = PrefixMappingConflict("someone else got there first")
    result = index.handler(
        _event(
            "putConfigPrefixMapping", {"prefix": "acme/", "configProfile": "lending"}
        ),
        None,
    )
    assert result["success"] is False
    assert result["error"]["type"] == "Conflict"


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_dry_run_reports_the_mapping_that_would_apply(manager, store):
    store.list.return_value = [
        {"prefix": "acme/", "configProfile": "broad"},
        {"prefix": "acme/invoices/", "configProfile": "lending", "configRevision": 7},
    ]
    result = index.handler(
        _event("resolveConfigPrefixMapping", {"objectKey": "acme/invoices/x.pdf"}),
        None,
    )
    assignment = result["assignment"]
    assert assignment["configProfile"] == "lending"
    assert assignment["configRevision"] == 7
    assert assignment["mappingPrefix"] == "acme/invoices/"
    assert assignment["source"] == "prefix-mapping"
    assert assignment["outOfScope"] is False
    assert assignment["reason"]


@pytest.mark.unit
def test_the_dry_run_reports_the_active_profile_when_nothing_matches(manager, store):
    result = index.handler(
        _event("resolveConfigPrefixMapping", {"objectKey": "unmapped/x.pdf"}), None
    )
    assignment = result["assignment"]
    assert assignment["configProfile"] == "active"
    assert assignment["source"] == "active-profile"
    assert assignment["mappingPrefix"] is None


@pytest.mark.unit
def test_the_dry_run_reports_a_conflict_and_a_rejection(manager, store):
    store.list.return_value = [
        {
            "prefix": "regulated/",
            "configProfile": "regulated",
            "metadataPrecedence": "reject",
        }
    ]
    result = index.handler(
        _event(
            "resolveConfigPrefixMapping",
            {"objectKey": "regulated/x.pdf", "metadataProfile": "something-else"},
        ),
        None,
    )
    assignment = result["assignment"]
    assert assignment["rejected"] is True
    assert assignment["conflict"] is True


@pytest.mark.unit
def test_the_dry_run_does_not_name_a_profile_outside_the_callers_scope(
    manager, store, monkeypatch
):
    """The enumeration-oracle case. getConfigVersions is scope-filtered precisely so
    a scoped caller cannot learn the names of profiles outside their scope; a dry run
    that returned the name would hand them back one key at a time."""
    store.list.return_value = [{"prefix": "finance/", "configProfile": "finance-prod"}]
    monkeypatch.setattr(
        index, "_get_user_allowed_config_versions", lambda email, sub="": ["teamA"]
    )
    result = index.handler(
        _event(
            "resolveConfigPrefixMapping",
            {"objectKey": "finance/x.pdf"},
            groups=("Viewer",),
            email="viewer@example.com",
        ),
        None,
    )
    assignment = result["assignment"]
    assert assignment["outOfScope"] is True
    assert assignment["configProfile"] is None
    assert assignment["configRevision"] is None
    assert "finance-prod" not in assignment["reason"]
    assert "teamA" not in assignment["reason"]
    # The prefix is withheld too. It looks like the caller's own input and is not:
    # they supplied a KEY, so returning the mapping that governs it tells them the
    # boundary sits at `finance/` rather than at `finance/x.pdf`, and one probe at a
    # time that walks out the routing policy `listConfigPrefixMappings` is
    # Admin-only to protect. They still learn the actionable part.
    assert assignment["mappingPrefix"] is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "precedence,metadata_profile",
    [
        # A rejection resolves to NO profile, so a scope guard predicated on the
        # resolved profile skips it entirely -- while the reason names the mapped
        # profile and its pinned revision, which is the whole secret.
        ("reject", "teamA"),
        # Metadata precedence resolves to the caller's OWN profile, in scope by
        # construction, while the reason explains that it beat the mapping's --
        # naming it.
        ("metadata", "teamA"),
    ],
)
def test_no_branch_of_the_dry_run_names_an_out_of_scope_profile(
    manager, store, monkeypatch, precedence, metadata_profile
):
    """The two branches where the disclosed profile is not the resolved one.

    Checking only the resolved profile leaves both of these handing a scoped caller
    the name of a profile outside their scope, one key at a time, through an
    operation Author and Viewer can both call.
    """
    store.list.return_value = [
        {
            "prefix": "finance/",
            "configProfile": "finance-prod-secret",
            "configRevision": 7,
            "metadataPrecedence": precedence,
        }
    ]
    monkeypatch.setattr(
        index, "_get_user_allowed_config_versions", lambda email, sub="": ["teamA"]
    )
    result = index.handler(
        _event(
            "resolveConfigPrefixMapping",
            {"objectKey": "finance/x.pdf", "metadataProfile": metadata_profile},
            groups=("Viewer",),
            email="viewer@example.com",
        ),
        None,
    )
    assignment = result["assignment"]
    assert assignment["outOfScope"] is True
    assert assignment["configProfile"] is None
    assert assignment["configRevision"] is None
    assert assignment["mappingPrefix"] is None
    serialized = str(assignment)
    assert "finance-prod-secret" not in serialized
    assert "r7" not in serialized


@pytest.mark.unit
def test_the_dry_run_serves_an_in_scope_caller_normally(manager, store, monkeypatch):
    store.list.return_value = [{"prefix": "teamA/", "configProfile": "teamA-prod"}]
    monkeypatch.setattr(
        index, "_get_user_allowed_config_versions", lambda email, sub="": ["teamA-*"]
    )
    result = index.handler(
        _event(
            "resolveConfigPrefixMapping",
            {"objectKey": "teamA/x.pdf"},
            groups=("Author",),
            email="author@example.com",
        ),
        None,
    )
    assert result["assignment"]["configProfile"] == "teamA-prod"
    assert result["assignment"]["outOfScope"] is False


@pytest.mark.unit
def test_the_dry_run_requires_an_object_key(manager, store):
    result = index.handler(_event("resolveConfigPrefixMapping", {}), None)
    assert result["success"] is False
    assert result["error"]["type"] == "ValidationError"


@pytest.mark.unit
class TestAProfileCannotOutliveAMappingThatNamesIt:
    """Deleting a profile a mapping names would leave the mapping resolving to
    nothing.

    `ConfigRevisionStore.delete_profile` drops every revision body regardless of
    whether a mapping pinned one, so the mapping stays listed as configured while
    every document arriving at its prefix silently processes under the default
    configuration -- and a reprocess stamps the phantom name onto the tracking row.
    Refusing beats cascading: a mapping is an operator's routing decision and
    deleting it on their behalf is not this operation's call.
    """

    def test_delete_is_refused_while_a_mapping_names_the_profile(self, manager, store):
        store.list.return_value = [
            {"prefix": "acme/", "configProfile": "lending", "enabled": True},
            {"prefix": "other/", "configProfile": "unrelated", "enabled": True},
        ]
        result = index.handler(
            _event("deleteConfigVersion", {"versionName": "lending"}), None
        )
        assert result["success"] is False
        assert "acme/" in result["error"]["message"]

    def test_a_disabled_mapping_still_blocks_the_delete(self, manager, store):
        """Re-enabling it later must not resolve to a profile that is gone."""
        store.list.return_value = [
            {"prefix": "acme/", "configProfile": "lending", "enabled": False}
        ]
        result = index.handler(
            _event("deleteConfigVersion", {"versionName": "lending"}), None
        )
        assert result["success"] is False

    def test_delete_proceeds_when_no_mapping_names_the_profile(self, manager, store):
        store.list.return_value = [
            {"prefix": "other/", "configProfile": "unrelated", "enabled": True}
        ]
        result = index.handler(
            _event("deleteConfigVersion", {"versionName": "lending"}), None
        )
        # Reaches the real delete path rather than being refused by this guard.
        assert "configuration prefix mapping" not in str(
            result.get("error", {}).get("message", "")
        )

    def test_an_unreadable_mapping_set_refuses_the_delete(self, manager, store):
        """Fails CLOSED here, unlike the ingest path. This is an irreversible admin
        delete, so "cannot tell whether a mapping depends on it" must not read as
        "nothing does"."""
        store.list.side_effect = RuntimeError("throttled")
        result = index.handler(
            _event("deleteConfigVersion", {"versionName": "lending"}), None
        )
        assert result["success"] is False
        assert "Could not confirm" in result["error"]["message"]


@pytest.mark.unit
class TestTheRetentionPinIsTakenAfterTheMappingExists:
    def test_a_failed_put_leaves_no_orphan_pin(self, manager, store):
        """`PrefixMappingStore.delete` never unpins -- a test run may have pinned the
        same revision and nothing records which referent asked -- so a pin taken
        before a put that then fails is permanent, with no mapping referencing it
        and nothing that can release it."""
        from idp_common.config.prefix_mappings import PrefixMappingConflict

        store.put.side_effect = PrefixMappingConflict("someone else won")
        result = index.handler(
            _event(
                "putConfigPrefixMapping",
                {"prefix": "acme/", "configProfile": "lending", "configRevision": 3},
            ),
            None,
        )
        assert result["success"] is False
        manager.mark_revision_pinned.assert_not_called()

    def test_a_successful_put_takes_the_pin(self, manager, store):
        index.handler(
            _event(
                "putConfigPrefixMapping",
                {"prefix": "acme/", "configProfile": "lending", "configRevision": 3},
            ),
            None,
        )
        manager.mark_revision_pinned.assert_called_once_with("lending", 3)

    def test_an_explicit_null_enabled_does_not_disable_the_mapping(
        self, manager, store
    ):
        """Absent and null both mean "not specified", and the default is enabled."""
        index.handler(
            _event(
                "putConfigPrefixMapping",
                {"prefix": "acme/", "configProfile": "lending", "enabled": None},
            ),
            None,
        )
        assert store.put.call_args.kwargs["enabled"] is True
