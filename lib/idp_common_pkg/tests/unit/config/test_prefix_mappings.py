# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Configuration prefix mappings: S3 prefix -> Configuration Profile.

The precedence matrix is table-driven rather than written out case by case, because
the rule has six branches crossed with three conflict modes and an exemption, and a
hand-written subset is how one of those branches ends up untested. The resolver takes
no boto3 client and reads no environment, which is what makes that possible.

Beyond the matrix, the cases here are the ones where getting it wrong is silent:
- a leading '/' producing a key no mapping matches (a one-character bypass of reject);
- metadata that AGREES with the mapping being recorded as a conflict (which would fail
  every correctly-stamped SDK upload under reject mode);
- a revision number carried across a profile change (per-profile numbering, so it
  reads a configuration nobody asked for);
- a read failure in the store turning a put into "the list is now this one entry".
"""

from decimal import Decimal

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from idp_common.config.prefix_mappings import (
    MAX_MAPPINGS,
    PREFIX_MAP_INDEX_KEY,
    SOURCE_ACTIVE_PROFILE,
    SOURCE_INTERNAL_PRODUCER,
    SOURCE_METADATA,
    SOURCE_PREFIX_MAPPING,
    SOURCE_REJECTED,
    ConfigAssignment,
    PrefixMappingConflict,
    PrefixMappingStore,
    canonical_key,
    find_match,
    match_kind,
    prefix_rejection_reason,
    resolve_config_assignment,
    sort_entries,
)

TABLE = "test-config-table"


def _make_table():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    return ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "Configuration", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _mapping(prefix, profile, **kwargs):
    entry = {"prefix": prefix, "configProfile": profile}
    entry.update(kwargs)
    return entry


def _resolve(key, **kwargs):
    kwargs.setdefault("active_profile", lambda: "active")
    return resolve_config_assignment(key, **kwargs)


# ---------------------------------------------------------------------------
# Key and prefix canonicalization
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("acme/invoices/x.pdf", "acme/invoices/x.pdf"),
        ("/acme/invoices/x.pdf", "acme/invoices/x.pdf"),
        ("acme//invoices/x.pdf", "acme/invoices/x.pdf"),
        ("//acme/invoices//x.pdf", "acme/invoices/x.pdf"),
        ("acme/invoices/", "acme/invoices/"),
        ("/acme/invoices/", "acme/invoices/"),
        ("/", ""),
        ("", ""),
    ],
)
def test_canonical_key_collapses_the_forms_s3_treats_as_distinct(raw, expected):
    assert canonical_key(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        "finance/x.pdf",
        "/finance/x.pdf",
        "finance//x.pdf",
        "finance/./x.pdf",
        "./finance/x.pdf",
    ],
)
def test_no_one_character_spelling_evades_an_exact_key_mapping(key):
    """S3 serves the same object for all of these and treats each key as distinct.

    An exact-key mapping is the shape a `reject` rule uses to protect one object, so
    a spelling that resolves to the object while matching no mapping is a bypass.
    """
    assert find_match(key, [_mapping("finance/x.pdf", "reg")]) is not None


@pytest.mark.unit
def test_a_parent_segment_is_matched_literally_rather_than_resolved():
    """`..` is left alone, deliberately.

    S3 keys are opaque strings with no parent directory, so `a/b/../c` is a real,
    distinct object. Resolving it the way a filesystem would would make this claim a
    key S3 serves from somewhere else, so such an object simply has no mapping
    unless one names it literally -- which `prefix_rejection_reason` refuses, so in
    practice it has none.
    """
    assert canonical_key("a/b/../c.pdf") == "a/b/../c.pdf"
    assert find_match("a/b/../c.pdf", [_mapping("a/b/", "p")]) is not None
    assert find_match("a/b/../c.pdf", [_mapping("a/c/", "p")]) is None


@pytest.mark.unit
def test_a_leading_slash_does_not_bypass_a_mapping():
    """The one-character bypass: S3 accepts '/finance/x.pdf' as a distinct key."""
    mappings = [_mapping("finance/", "lending")]
    assert find_match("finance/x.pdf", mappings) is not None
    assert find_match("/finance/x.pdf", mappings) is not None
    assert find_match("finance//x.pdf", mappings) is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    "prefix",
    ["", "   ", "/", "//", "/finance/", "acme//invoices/", "../etc/", "a/./b/", " a/"],
)
def test_unusable_prefixes_are_refused_with_prose(prefix):
    reason = prefix_rejection_reason(prefix)
    assert reason, f"{prefix!r} should have been refused"
    # The message reaches the admin verbatim, so it has to read as a sentence.
    assert reason.endswith(".")


@pytest.mark.unit
@pytest.mark.parametrize(
    "prefix", ["acme/", "acme/invoices/", "acme/invoices/jan.pdf", "a"]
)
def test_usable_prefixes_are_accepted(prefix):
    assert prefix_rejection_reason(prefix) is None


@pytest.mark.unit
def test_the_trailing_slash_is_the_mode_selector():
    assert match_kind("acme/invoices/") == "prefix"
    assert match_kind("acme/invoices/jan.pdf") == "exact"


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_matching_is_case_sensitive_because_s3_keys_are():
    mappings = [_mapping("Invoices/", "lending")]
    assert find_match("Invoices/x.pdf", mappings) is not None
    assert find_match("invoices/x.pdf", mappings) is None


@pytest.mark.unit
def test_the_longest_prefix_wins():
    mappings = [
        _mapping("acme/", "broad"),
        _mapping("acme/invoices/", "specific"),
        _mapping("acme/invoices/2026/", "most-specific"),
    ]
    assert find_match("acme/invoices/2026/x.pdf", mappings)["configProfile"] == (
        "most-specific"
    )
    assert find_match("acme/invoices/x.pdf", mappings)["configProfile"] == "specific"
    assert find_match("acme/other/x.pdf", mappings)["configProfile"] == "broad"
    assert find_match("other/x.pdf", mappings) is None


@pytest.mark.unit
def test_a_prefix_only_matches_at_a_segment_boundary_the_admin_declared():
    """`acme/inv/` must not capture `acme/invoices/`, and `acme/inv` is exact."""
    assert find_match("acme/invoices/x.pdf", [_mapping("acme/inv/", "p")]) is None
    assert find_match("acme/invoices", [_mapping("acme/inv", "p")]) is None


@pytest.mark.unit
def test_an_exact_entry_outranks_every_prefix_entry_however_long():
    mappings = [
        _mapping("acme/invoices/2026/q1/", "long-prefix"),
        _mapping("acme/invoices/2026/q1/jan.pdf", "exact"),
    ]
    assert find_match("acme/invoices/2026/q1/jan.pdf", mappings)["configProfile"] == (
        "exact"
    )
    assert find_match("acme/invoices/2026/q1/feb.pdf", mappings)["configProfile"] == (
        "long-prefix"
    )


@pytest.mark.unit
def test_a_disabled_entry_never_matches_but_a_shorter_enabled_one_still_can():
    mappings = [
        _mapping("acme/", "broad"),
        _mapping("acme/invoices/", "specific", enabled=False),
    ]
    assert find_match("acme/invoices/x.pdf", mappings)["configProfile"] == "broad"


@pytest.mark.unit
def test_an_entry_with_no_profile_is_skipped_rather_than_assigning_nothing():
    mappings = [_mapping("acme/", "broad"), _mapping("acme/invoices/", "")]
    assert find_match("acme/invoices/x.pdf", mappings)["configProfile"] == "broad"


@pytest.mark.unit
def test_sort_order_is_the_order_resolution_evaluates():
    entries = sort_entries(
        [
            _mapping("a/", "p"),
            _mapping("a/b/c/", "p"),
            _mapping("a/b/", "p"),
            _mapping("a/b/exact.pdf", "p"),
        ]
    )
    assert [e["prefix"] for e in entries] == ["a/b/exact.pdf", "a/b/c/", "a/b/", "a/"]


# ---------------------------------------------------------------------------
# The precedence matrix
# ---------------------------------------------------------------------------

# (internal producer, metadata profile, a mapping matches, conflict mode)
#   -> (resolved profile, source, conflict, rejected)
PRECEDENCE_MATRIX = [
    # An internal producer routes itself; the mapping is never consulted.
    (
        "test-studio",
        "companion",
        True,
        "mapping",
        "companion",
        SOURCE_INTERNAL_PRODUCER,
        False,
        False,
    ),
    (
        "test-studio",
        "companion",
        True,
        "reject",
        "companion",
        SOURCE_INTERNAL_PRODUCER,
        False,
        False,
    ),
    (
        "test-studio",
        "companion",
        False,
        "mapping",
        "companion",
        SOURCE_INTERNAL_PRODUCER,
        False,
        False,
    ),
    # Nothing matched: today's behaviour, byte for byte.
    (None, None, False, "mapping", "active", SOURCE_ACTIVE_PROFILE, False, False),
    (None, "chosen", False, "mapping", "chosen", SOURCE_METADATA, False, False),
    # A mapping matched and the upload asked for nothing.
    (None, None, True, "mapping", "mapped", SOURCE_PREFIX_MAPPING, False, False),
    (None, None, True, "metadata", "mapped", SOURCE_PREFIX_MAPPING, False, False),
    (None, None, True, "reject", "mapped", SOURCE_PREFIX_MAPPING, False, False),
    # Both, and they disagree.
    (None, "chosen", True, "mapping", "mapped", SOURCE_PREFIX_MAPPING, True, False),
    (None, "chosen", True, "metadata", "chosen", SOURCE_METADATA, True, False),
    (None, "chosen", True, "reject", None, SOURCE_REJECTED, True, True),
    # Both, and they AGREE, which by design is NOT recorded as a conflict.
    (None, "mapped", True, "mapping", "mapped", SOURCE_PREFIX_MAPPING, False, False),
    (None, "mapped", True, "metadata", "mapped", SOURCE_PREFIX_MAPPING, False, False),
    (None, "mapped", True, "reject", "mapped", SOURCE_PREFIX_MAPPING, False, False),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    "submission_source,metadata_profile,matches,precedence,"
    "expected_profile,expected_source,expected_conflict,expected_rejected",
    PRECEDENCE_MATRIX,
)
def test_precedence_matrix(
    submission_source,
    metadata_profile,
    matches,
    precedence,
    expected_profile,
    expected_source,
    expected_conflict,
    expected_rejected,
):
    mappings = (
        [_mapping("acme/", "mapped", metadataPrecedence=precedence)] if matches else []
    )
    assignment = _resolve(
        "acme/x.pdf" if matches else "elsewhere/x.pdf",
        metadata_profile=metadata_profile,
        submission_source=submission_source,
        mappings=mappings,
    )
    assert assignment.profile == expected_profile
    assert assignment.source == expected_source
    assert assignment.conflict is expected_conflict
    assert assignment.rejected is expected_rejected
    # Every outcome explains itself; the reason reaches a log line, a tracking row and
    # an upload warning, so an empty one is a defect rather than cosmetic.
    assert assignment.reason


@pytest.mark.unit
def test_an_internal_producer_with_no_profile_says_why_the_mapping_did_not_apply():
    """The reason reaches an operator asking exactly that question.

    `_active()`'s wording ("no prefix mapping matched") is false here: a mapping
    may well match, and was deliberately not consulted.
    """
    assignment = _resolve(
        "acme/x.pdf",
        submission_source="test-studio",
        mappings=[_mapping("acme/", "mapped")],
    )
    assert assignment.profile == "active"
    assert "test-studio" in assignment.reason
    assert "do not apply to internal submissions" in assignment.reason
    assert "No prefix mapping matched" not in assignment.reason


@pytest.mark.unit
def test_an_agreeing_revision_is_not_a_conflict_either():
    """Profile AND revision must both agree, or `reject` fails correct SDK uploads."""
    mappings = [
        _mapping("acme/", "lending", configRevision=7, metadataPrecedence="reject")
    ]
    agree = _resolve(
        "acme/x.pdf", metadata_profile="lending", metadata_revision=7, mappings=mappings
    )
    assert agree.rejected is False
    assert agree.conflict is False
    assert agree.revision == 7

    disagree = _resolve(
        "acme/x.pdf", metadata_profile="lending", metadata_revision=3, mappings=mappings
    )
    assert disagree.rejected is True


@pytest.mark.unit
def test_an_unpinned_mapping_agrees_with_metadata_naming_the_published_revision():
    mappings = [_mapping("acme/", "lending", metadataPrecedence="reject")]
    assignment = _resolve(
        "acme/x.pdf",
        metadata_profile="lending",
        metadata_revision=9,
        mappings=mappings,
        published_revision=lambda _p: 9,
    )
    assert assignment.rejected is False
    assert assignment.revision == 9


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_unpinned_mapping_follows_the_published_revision():
    assignment = _resolve(
        "acme/x.pdf",
        mappings=[_mapping("acme/", "lending")],
        published_revision=lambda profile: {"lending": 12}.get(profile),
    )
    assert (assignment.profile, assignment.revision) == ("lending", 12)


@pytest.mark.unit
def test_a_pinned_mapping_ignores_the_published_revision():
    assignment = _resolve(
        "acme/x.pdf",
        mappings=[_mapping("acme/", "lending", configRevision=3)],
        published_revision=lambda _profile: 12,
    )
    assert assignment.revision == 3


@pytest.mark.unit
def test_the_metadata_revision_is_never_carried_onto_a_different_profile():
    """Revision numbers are per profile, so r7 of 'chosen' means nothing for 'mapped'.

    queue_processor will not correct this either: its backfill only fires when the
    revision is absent, so a carried-over number is what extraction would read.
    """
    assignment = _resolve(
        "acme/x.pdf",
        metadata_profile="chosen",
        metadata_revision=7,
        mappings=[_mapping("acme/", "mapped")],
        published_revision=lambda profile: {"mapped": 2}.get(profile),
    )
    assert assignment.profile == "mapped"
    assert assignment.revision == 2

    unpublished = _resolve(
        "acme/x.pdf",
        metadata_profile="chosen",
        metadata_revision=7,
        mappings=[_mapping("acme/", "mapped")],
    )
    assert unpublished.profile == "mapped"
    assert unpublished.revision is None


@pytest.mark.unit
def test_a_decimal_revision_from_dynamodb_compares_equal_to_an_int():
    assignment = _resolve(
        "acme/x.pdf",
        mappings=[_mapping("acme/", "lending", configRevision=Decimal("4"))],
    )
    assert assignment.revision == 4
    assert isinstance(assignment.revision, int)


# ---------------------------------------------------------------------------
# Degraded paths -- none of these may strand a document
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_mapping_naming_a_deleted_profile_falls_through_and_is_flagged():
    assignment = _resolve(
        "acme/x.pdf",
        mappings=[_mapping("acme/", "deleted")],
        profile_exists=lambda profile: profile != "deleted",
    )
    assert assignment.unresolvable is True
    assert assignment.profile == "active"
    assert assignment.source == SOURCE_ACTIVE_PROFILE
    # The prefix is still recorded: an operator needs to know WHICH mapping is stale.
    assert assignment.mapping_prefix == "acme/"


@pytest.mark.unit
def test_a_deleted_profile_falls_back_to_the_uploads_own_choice_when_it_made_one():
    assignment = _resolve(
        "acme/x.pdf",
        metadata_profile="chosen",
        mappings=[_mapping("acme/", "deleted")],
        profile_exists=lambda profile: profile != "deleted",
    )
    assert (assignment.profile, assignment.source) == ("chosen", SOURCE_METADATA)
    assert assignment.unresolvable is True


@pytest.mark.unit
def test_a_raising_lookup_does_not_propagate_out_of_resolution():
    def boom(*_args):
        raise RuntimeError("DynamoDB said no")

    assignment = _resolve(
        "acme/x.pdf",
        mappings=[_mapping("acme/", "lending")],
        published_revision=boom,
        profile_exists=boom,
    )
    assert assignment.profile == "lending"
    assert assignment.revision is None


@pytest.mark.unit
def test_no_active_profile_is_a_normal_state_not_a_failure():
    assignment = resolve_config_assignment("x.pdf", active_profile=lambda: None)
    assert assignment.profile is None
    assert assignment.source == SOURCE_ACTIVE_PROFILE
    assert assignment.reason


@pytest.mark.unit
def test_a_raising_active_profile_lookup_degrades_to_no_pin():
    def boom():
        raise RuntimeError("no")

    assert resolve_config_assignment("x.pdf", active_profile=boom).profile is None


# ---------------------------------------------------------------------------
# Scope -- evaluated on the RESOLVED profile
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scope_is_checked_on_the_resolved_profile_not_the_requested_one():
    """The prefix route and the metadata route are one check, or neither is covered."""
    via_prefix = _resolve(
        "finance/x.pdf",
        mappings=[_mapping("finance/", "finance-prod")],
        allowed_profiles=["teamA"],
    )
    assert via_prefix.scope_denied is True

    via_metadata = _resolve(
        "elsewhere/x.pdf", metadata_profile="finance-prod", allowed_profiles=["teamA"]
    )
    assert via_metadata.scope_denied is True


@pytest.mark.unit
def test_an_in_scope_resolution_is_allowed_and_globs_are_honoured():
    assignment = _resolve(
        "teamA/x.pdf",
        mappings=[_mapping("teamA/", "teamA-prod")],
        allowed_profiles=["teamA-*"],
    )
    assert assignment.scope_denied is False
    assert assignment.profile == "teamA-prod"


@pytest.mark.unit
def test_an_unset_scope_means_unrestricted():
    """queue_sender has no caller, so it passes None -- which must not deny."""
    assignment = _resolve(
        "finance/x.pdf",
        mappings=[_mapping("finance/", "finance-prod")],
        allowed_profiles=None,
    )
    assert assignment.scope_denied is False


@pytest.mark.unit
def test_a_scope_denial_does_not_name_the_profile_it_refused():
    """Profile names are RBAC objects; a refusal must not enumerate them."""
    assignment = _resolve(
        "finance/x.pdf",
        mappings=[_mapping("finance/", "finance-prod")],
        allowed_profiles=["teamA"],
    )
    assert "finance-prod" not in assignment.reason


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
def test_put_get_list_and_delete_round_trip():
    store = PrefixMappingStore(_make_table())
    assert store.list() == []

    store.put("acme/", "lending", config_revision=7, actor="admin@example.com")
    store.put("acme/invoices/", "invoices", description="More specific")

    entries = store.list()
    assert [e["prefix"] for e in entries] == ["acme/invoices/", "acme/"]
    assert store.get("acme/")["configProfile"] == "lending"
    assert store.get("acme/")["configRevision"] == 7
    assert store.get("acme/")["createdBy"] == "admin@example.com"
    assert store.get("missing/") is None

    assert store.delete("acme/") is True
    assert store.delete("acme/") is False
    assert [e["prefix"] for e in store.list()] == ["acme/invoices/"]


@pytest.mark.unit
@mock_aws
def test_put_is_an_upsert_that_preserves_creation_provenance():
    store = PrefixMappingStore(_make_table())
    store.put("acme/", "first", actor="creator@example.com")
    created_at = store.get("acme/")["createdAt"]

    store.put("acme/", "second", actor="editor@example.com")
    entry = store.get("acme/")

    assert len(store.list()) == 1
    assert entry["configProfile"] == "second"
    assert entry["createdBy"] == "creator@example.com"
    assert entry["createdAt"] == created_at
    assert entry["updatedBy"] == "editor@example.com"


@pytest.mark.unit
@mock_aws
def test_the_aggregate_item_does_not_look_like_a_configuration_profile():
    """It shares the table with profiles, so its key must not match 'Config#'."""
    store = PrefixMappingStore(_make_table())
    store.put("acme/", "lending")
    assert not PREFIX_MAP_INDEX_KEY.startswith("Config#")
    scanned = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE).scan()
    keys = [i["Configuration"] for i in scanned["Items"]]
    assert keys == [PREFIX_MAP_INDEX_KEY]


@pytest.mark.unit
@mock_aws
def test_put_refuses_an_unusable_prefix_and_an_unknown_conflict_mode():
    store = PrefixMappingStore(_make_table())
    with pytest.raises(ValueError, match="root mapping"):
        store.put("/", "lending")
    with pytest.raises(ValueError, match="must not start with"):
        store.put("/acme/", "lending")
    with pytest.raises(ValueError, match="Configuration Profile is required"):
        store.put("acme/", "")
    with pytest.raises(ValueError, match="Unknown conflict mode"):
        store.put("acme/", "lending", metadata_precedence="whatever")
    assert store.list() == []


@pytest.mark.unit
@mock_aws
def test_the_mapping_count_is_capped_at_write_time():
    """The aggregate item is on the ingest path, so overflow would be an outage."""
    store = PrefixMappingStore(_make_table())
    for n in range(MAX_MAPPINGS):
        store.put(f"p{n}/", "lending")
    with pytest.raises(ValueError, match=str(MAX_MAPPINGS)):
        store.put("one-too-many/", "lending")
    assert len(store.list()) == MAX_MAPPINGS


@pytest.mark.unit
@pytest.mark.parametrize(
    "prefix",
    [
        "a" * 513 + "/",
        "\u00e9" * 300 + "/",  # bytes, not characters
    ],
)
def test_an_over_long_prefix_is_refused(prefix):
    """MAX_MAPPINGS bounds the entry COUNT only.

    Every per-entry field needs a length limit as well, or the item-size
    arithmetic does not close -- and what going over costs is an opaque
    ValidationException on the admin write, so each bound is refused individually
    with a message naming it.
    """
    reason = prefix_rejection_reason(prefix)
    assert reason and "512 bytes" in reason


@pytest.mark.unit
@mock_aws
def test_an_over_long_actor_is_truncated_rather_than_refused():
    """An actor string comes from the caller's token, not from something they
    typed, so refusing their write over its length would be unactionable."""
    store = PrefixMappingStore(_make_table())
    store.put("acme/", "lending", actor="a" * 400)
    assert len(store.get("acme/")["createdBy"]) == 256


@pytest.mark.unit
@mock_aws
def test_an_over_long_profile_name_is_refused():
    store = PrefixMappingStore(_make_table())
    with pytest.raises(ValueError, match="at most"):
        store.put("acme/", "p" * 129)


@pytest.mark.unit
@mock_aws
def test_the_cap_and_the_length_bounds_together_hold_the_item_limit():
    """The worst case the write path permits, measured rather than asserted.

    MAX_MAPPINGS entries each at the maximum prefix, profile and description
    length. If this ever exceeds 400 KB, ingest starts failing to READ the item
    and no admin action caused it on that request.
    """
    entries = [
        {
            # Every field at its maximum, INCLUDING the two actor strings -- they
            # are a token claim rather than typed input, so they are truncated
            # rather than refused, and leaving them unbounded is what put this
            # sum over the limit.
            "prefix": ("p" * 508 + f"{n:04d}" + "/"),
            "configProfile": "q" * 128,
            "configRevision": 999999,
            "metadataPrecedence": "reject",
            "enabled": True,
            "description": "D" * 500,
            "createdAt": "2026-10-10T00:00:00.000000Z",
            "createdBy": "a" * 256,
            "updatedAt": "2026-10-10T00:00:00.000000Z",
            "updatedBy": "b" * 256,
        }
        for n in range(MAX_MAPPINGS)
    ]
    size = len(str(sort_entries(entries)).encode("utf-8"))
    assert size < 400 * 1024, (
        f"the worst case the write path permits serializes to {size:,} bytes, "
        f"against DynamoDB's 400 KB item limit. Lower MAX_MAPPINGS or the length "
        f"bounds — the overflow surfaces on the INGEST read."
    )


@pytest.mark.unit
@mock_aws
def test_a_full_mapping_set_stays_well_inside_the_dynamodb_item_limit():
    store = PrefixMappingStore(_make_table())
    for n in range(MAX_MAPPINGS):
        store.put(
            f"tenant-{n:03d}/business-unit/invoices/incoming/",
            f"profile-for-tenant-{n:03d}",
            config_revision=n,
            description="D" * 500,
        )
    item = (
        boto3.resource("dynamodb", region_name="us-east-1")
        .Table(TABLE)
        .get_item(Key=PrefixMappingStore.index_key())["Item"]
    )
    size = len(str(item).encode("utf-8"))
    assert size < 400 * 1024, f"aggregate item is {size:,} bytes"


@pytest.mark.unit
@mock_aws
def test_profiles_in_use_backs_the_delete_a_profile_guard():
    store = PrefixMappingStore(_make_table())
    store.put("a/", "lending")
    store.put("b/", "lending")
    store.put("c/", "rvl-cdip", enabled=False)
    # A disabled mapping still counts: deleting its profile would drop the revision
    # bodies it names, so re-enabling it later would silently resolve to nothing.
    assert store.profiles_in_use() == ["lending", "rvl-cdip"]


@pytest.mark.unit
@mock_aws
def test_a_read_failure_must_not_turn_a_put_into_a_wipe():
    """The defect this store exists to avoid inheriting from ConfigRevisionStore.

    That one logs a ClientError and returns {}. Harmless there, because every
    mutation it feeds aborts when its target revision is absent. Here a put appends
    unconditionally, so a swallowed throttle would write a one-entry list over
    everything and report success.
    """
    table = _make_table()
    store = PrefixMappingStore(table)
    store.put("keep-me/", "lending")
    store.put("keep-me-too/", "lending")

    def throttled(**_kwargs):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem"
        )

    table.get_item = throttled
    with pytest.raises(ClientError):
        store.put("new/", "lending")

    fresh = PrefixMappingStore(
        boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
    )
    assert sorted(e["prefix"] for e in fresh.list()) == ["keep-me-too/", "keep-me/"]


@pytest.mark.unit
@mock_aws
def test_a_losing_concurrent_write_raises_rather_than_reporting_success():
    """A mapping the admin believes they created, which does not exist, is the worst
    outcome available here -- so the conflict is an exception, not a return value."""
    table = _make_table()
    store = PrefixMappingStore(table)
    store.put("existing/", "lending")

    real_update = table.update_item

    def always_conflicts(**_kwargs):
        # Simulate another writer winning the conditional check, every time.
        raise ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
        )

    table.update_item = always_conflicts
    with pytest.raises(PrefixMappingConflict):
        store.put("new/", "lending")

    table.update_item = real_update
    assert [e["prefix"] for e in store.list()] == ["existing/"]


@pytest.mark.unit
@mock_aws
def test_a_conflict_on_the_first_attempt_is_retried_once_and_succeeds():
    table = _make_table()
    store = PrefixMappingStore(table)
    store.put("existing/", "lending")

    real_update = table.update_item
    calls = {"n": 0}

    def fails_once(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
            )
        return real_update(**kwargs)

    table.update_item = fails_once
    store.put("new/", "lending")
    table.update_item = real_update

    assert sorted(e["prefix"] for e in store.list()) == ["existing/", "new/"]
    assert calls["n"] == 2


@pytest.mark.unit
@mock_aws
def test_a_non_conflict_client_error_propagates_rather_than_retrying():
    table = _make_table()
    store = PrefixMappingStore(table)

    def access_denied(**_kwargs):
        raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "UpdateItem")

    table.update_item = access_denied
    with pytest.raises(ClientError):
        store.put("acme/", "lending")


@pytest.mark.unit
@mock_aws
def test_store_entries_round_trip_through_dynamodb_types():
    """DynamoDB returns numbers as Decimal; a revision that never compares equal to an
    int would make every agreeing upload look like a conflict."""
    store = PrefixMappingStore(_make_table())
    store.put("acme/", "lending", config_revision=7)
    entry = store.list()[0]
    assert entry["configRevision"] == 7
    assert isinstance(entry["configRevision"], int)

    assignment = resolve_config_assignment(
        "acme/x.pdf",
        metadata_profile="lending",
        metadata_revision=7,
        mappings=store.list(),
        active_profile=lambda: "active",
    )
    assert assignment.conflict is False


@pytest.mark.unit
def test_the_assignment_is_frozen():
    """It is recorded on a document and read by four consumers; none may edit it."""
    with pytest.raises(Exception):
        ConfigAssignment().profile = "something-else"  # pyright: ignore[reportAttributeAccessIssue]
