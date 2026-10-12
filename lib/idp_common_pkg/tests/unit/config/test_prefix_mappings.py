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
    description_rejection_reason,
    find_match,
    match_kind,
    normalize_entry,
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


# Every row of PRECEDENCE_MATRIX uses a mapping stored as `acme/` -- with the
# trailing slash, so prefix mode. That left the mapping SHAPE untested, and it is
# the one dimension an administrator chooses by punctuation rather than by a
# control: an entry stored without a trailing slash is an EXACT OBJECT KEY, so
# `bob1` governs an object literally named `bob1` and governs nothing at all under
# `bob1/`. A deployment whose three mappings were all stored that way resolved
# every upload by metadata with `mapping_prefix=None` -- indistinguishable, from
# the document list, from "the mapping lost the conflict", and in fact "no mapping
# was ever found". The conflict modes were never reached.
#
# So the matrix is re-run over both shapes. The folder-prefix half must behave
# exactly as PRECEDENCE_MATRIX says; the exact-key half must match NOTHING for a
# key with a filename under it, whatever the conflict mode, which is the property
# that makes the two shapes different rather than two spellings of one thing.
FOLDER_KEY = "acme/x.pdf"


@pytest.mark.unit
@pytest.mark.parametrize(
    "metadata_profile,precedence,expected_profile,expected_source,"
    "expected_conflict,expected_rejected",
    [
        # (no upload tag) x (each conflict mode)
        (None, "mapping", "mapped", SOURCE_PREFIX_MAPPING, False, False),
        (None, "metadata", "mapped", SOURCE_PREFIX_MAPPING, False, False),
        (None, "reject", "mapped", SOURCE_PREFIX_MAPPING, False, False),
        # (upload tag that conflicts) x (each conflict mode)
        ("chosen", "mapping", "mapped", SOURCE_PREFIX_MAPPING, True, False),
        ("chosen", "metadata", "chosen", SOURCE_METADATA, True, False),
        ("chosen", "reject", None, SOURCE_REJECTED, True, True),
        # (upload tag that agrees) x (each conflict mode)
        ("mapped", "mapping", "mapped", SOURCE_PREFIX_MAPPING, False, False),
        ("mapped", "metadata", "mapped", SOURCE_PREFIX_MAPPING, False, False),
        ("mapped", "reject", "mapped", SOURCE_PREFIX_MAPPING, False, False),
    ],
)
def test_a_folder_prefix_mapping_governs_documents_under_it(
    metadata_profile,
    precedence,
    expected_profile,
    expected_source,
    expected_conflict,
    expected_rejected,
):
    """The shape an administrator means when they type a folder name."""
    assignment = _resolve(
        FOLDER_KEY,
        metadata_profile=metadata_profile,
        mappings=[_mapping("acme/", "mapped", metadataPrecedence=precedence)],
    )
    assert assignment.profile == expected_profile
    assert assignment.source == expected_source
    assert assignment.conflict is expected_conflict
    assert assignment.rejected is expected_rejected
    # Recorded on every outcome, refusals included: it is what distinguishes "a
    # mapping decided this" from "no mapping was found", which are otherwise the
    # same two fields on the document.
    assert assignment.mapping_prefix == "acme/"


@pytest.mark.unit
@pytest.mark.parametrize("precedence", ["mapping", "metadata", "reject"])
@pytest.mark.parametrize("metadata_profile", [None, "chosen", "mapped"])
def test_an_exact_key_mapping_governs_nothing_underneath_it(
    precedence, metadata_profile
):
    """`acme` does NOT govern `acme/x.pdf`, in any conflict mode.

    This is the live failure mode written down: typing a folder name with no
    trailing slash stores an exact-object-key mapping, which never matches an
    uploaded document, so the resolution falls through to the upload's own tag and
    the conflict mode is never consulted. The resolver is behaving correctly here
    -- the fix belongs in the form that let the mapping be written -- but the
    behaviour has to be pinned, because "no mapping matched" and "the mapping
    deferred to the upload" are the same two fields on the document and only the
    absent `mapping_prefix` tells them apart.
    """
    assignment = _resolve(
        FOLDER_KEY,
        metadata_profile=metadata_profile,
        mappings=[_mapping("acme", "mapped", metadataPrecedence=precedence)],
    )
    assert assignment.mapping_prefix is None, (
        "an exact-key mapping matched a key BENEATH it, which would make the "
        "trailing slash meaningless and every folder mapping a catch-all"
    )
    assert assignment.rejected is False, (
        "`reject` fired for a mapping that does not govern this key -- an "
        "exact-key mapping must not fail documents under it"
    )
    assert assignment.conflict is False
    # Falls through to exactly what the deployment did before mappings existed.
    if metadata_profile:
        assert assignment.profile == metadata_profile
        assert assignment.source == SOURCE_METADATA
    else:
        assert assignment.profile == "active"
        assert assignment.source == SOURCE_ACTIVE_PROFILE


@pytest.mark.unit
@pytest.mark.parametrize("precedence", ["mapping", "metadata", "reject"])
@pytest.mark.parametrize("metadata_profile", [None, "chosen", "mapped"])
def test_an_exact_key_mapping_governs_its_own_object(precedence, metadata_profile):
    """And the same entry DOES govern the one object it names, so the mode works."""
    assignment = _resolve(
        "acme",
        metadata_profile=metadata_profile,
        mappings=[_mapping("acme", "mapped", metadataPrecedence=precedence)],
    )
    assert assignment.mapping_prefix == "acme"


# ---------------------------------------------------------------------------
# The same matrix, end to end through the STORE
# ---------------------------------------------------------------------------
#
# Everything above resolves against mapping dicts written inline. That leaves one
# join untested, and it is the join that failed in a real deployment: the entry an
# administrator SAVES, read back the way ingest reads it, against the key the
# upload panel actually creates.
#
# Three mappings saved as `bob1`/`bob2`/`bob3` (one per conflict mode) resolved
# every upload by its own tag with no mapping applied, because a saved prefix with
# no trailing slash is an exact object key and the uploads landed at
# `bob1/<file>.pdf`. Each layer was individually correct and the composition was
# not, so this class is deliberately composed of the real parts: `put()` as the API
# writes, `list()` as ingest reads, `resolve_config_assignment` as both callers call
# it. It is the regression test for that deployment, and it is the matrix the
# feature is actually judged on.
#
# UPLOAD_TAGS covers "tagged vs untagged" in the three forms that behave
# differently -- no tag at all, a tag naming a DIFFERENT profile, and a tag naming
# the SAME profile the mapping assigns (which is deliberately not a conflict).

UPLOAD_TAGS = [
    pytest.param(None, id="untagged"),
    pytest.param("chosen", id="tagged-conflicting"),
    pytest.param("mapped", id="tagged-agreeing"),
]

# What a FOLDER-prefix mapping must do, keyed (tag, mode).
FOLDER_EXPECTATIONS = {
    (None, "mapping"): ("mapped", SOURCE_PREFIX_MAPPING, False, False),
    (None, "metadata"): ("mapped", SOURCE_PREFIX_MAPPING, False, False),
    (None, "reject"): ("mapped", SOURCE_PREFIX_MAPPING, False, False),
    ("chosen", "mapping"): ("mapped", SOURCE_PREFIX_MAPPING, True, False),
    ("chosen", "metadata"): ("chosen", SOURCE_METADATA, True, False),
    ("chosen", "reject"): (None, SOURCE_REJECTED, True, True),
    ("mapped", "mapping"): ("mapped", SOURCE_PREFIX_MAPPING, False, False),
    ("mapped", "metadata"): ("mapped", SOURCE_PREFIX_MAPPING, False, False),
    ("mapped", "reject"): ("mapped", SOURCE_PREFIX_MAPPING, False, False),
}


@pytest.mark.unit
class TestTheSavedMappingMatrix:
    """put() -> list() -> resolve, over every tag x conflict-mode x shape."""

    UPLOADED_KEY = "bob1/lending_package.pdf"

    @staticmethod
    def _saved(prefix, precedence):
        """One mapping, written and read back through the real store."""
        store = PrefixMappingStore(_make_table())
        store.put(
            prefix,
            "mapped",
            metadata_precedence=precedence,
            actor="admin@example.com",
        )
        return store.list()

    @pytest.mark.parametrize("precedence", ["mapping", "metadata", "reject"])
    @pytest.mark.parametrize("tag", UPLOAD_TAGS)
    @mock_aws
    def test_a_folder_prefix_mapping_saved_by_the_api(self, tag, precedence):
        """`bob1/` governs `bob1/<file>`, and every conflict mode is reached."""
        mappings = self._saved("bob1/", precedence)
        assert mappings[0]["matchKind"] == "prefix"

        assignment = _resolve(
            self.UPLOADED_KEY, metadata_profile=tag, mappings=mappings
        )
        profile, source, conflict, rejected = FOLDER_EXPECTATIONS[(tag, precedence)]
        assert assignment.profile == profile
        assert assignment.source == source
        assert assignment.conflict is conflict
        assert assignment.rejected is rejected
        assert assignment.mapping_prefix == "bob1/"

    @pytest.mark.parametrize("precedence", ["mapping", "metadata", "reject"])
    @pytest.mark.parametrize("tag", UPLOAD_TAGS)
    @mock_aws
    def test_an_exact_key_mapping_saved_by_the_api_governs_nothing_under_it(
        self, tag, precedence
    ):
        """`bob1` governs only `bob1`, so NO conflict mode is reached.

        This is the deployment's observed behaviour, pinned: the resolution falls
        through to the upload's own tag in all nine combinations, and
        `mapping_prefix` stays None -- which is the only field distinguishing this
        from "the mapping deferred to the upload".
        """
        mappings = self._saved("bob1", precedence)
        assert mappings[0]["matchKind"] == "exact"

        assignment = _resolve(
            self.UPLOADED_KEY, metadata_profile=tag, mappings=mappings
        )
        assert assignment.mapping_prefix is None
        assert assignment.conflict is False
        assert assignment.rejected is False
        assert assignment.profile == (tag or "active")
        assert assignment.source == (SOURCE_METADATA if tag else SOURCE_ACTIVE_PROFILE)

    @pytest.mark.parametrize("precedence", ["mapping", "metadata", "reject"])
    @pytest.mark.parametrize("tag", UPLOAD_TAGS)
    @mock_aws
    def test_an_exact_key_mapping_saved_by_the_api_governs_its_own_object(
        self, tag, precedence
    ):
        """And it does govern the single object it names, in every mode."""
        mappings = self._saved("bob1", precedence)
        assignment = _resolve("bob1", metadata_profile=tag, mappings=mappings)
        profile, source, conflict, rejected = FOLDER_EXPECTATIONS[(tag, precedence)]
        assert assignment.profile == profile
        assert assignment.source == source
        assert assignment.conflict is conflict
        assert assignment.rejected is rejected
        assert assignment.mapping_prefix == "bob1"

    @mock_aws
    def test_three_mappings_one_per_mode_each_govern_their_own_folder(self):
        """The deployment's actual setup, spelled correctly.

        One folder per conflict mode, all three saved into one index, all three
        uploads carrying a tag that conflicts -- which is the scenario where the
        three modes must visibly differ from each other.
        """
        store = PrefixMappingStore(_make_table())
        for n, precedence in enumerate(["mapping", "metadata", "reject"], start=1):
            store.put(
                f"bob{n}/",
                f"lending{n}",
                metadata_precedence=precedence,
                actor="admin@example.com",
            )
        mappings = store.list()
        assert len(mappings) == 3

        outcomes = {
            key: _resolve(key, metadata_profile="fake-w2", mappings=mappings)
            for key in (
                "bob1/lending_package.pdf",
                "bob2/lending_package.pdf",
                "bob3/lending_package.pdf",
            )
        }

        # mapping wins
        first = outcomes["bob1/lending_package.pdf"]
        assert (first.profile, first.source, first.conflict) == (
            "lending1",
            SOURCE_PREFIX_MAPPING,
            True,
        )
        # upload metadata wins
        second = outcomes["bob2/lending_package.pdf"]
        assert (second.profile, second.source, second.conflict) == (
            "fake-w2",
            SOURCE_METADATA,
            True,
        )
        assert second.mapping_prefix == "bob2/", (
            "the prefix must be recorded even though the mapping lost, or the "
            "document cannot explain why metadata won"
        )
        # refused
        third = outcomes["bob3/lending_package.pdf"]
        assert third.rejected is True
        assert third.source == SOURCE_REJECTED
        assert "lending3" in third.reason and "fake-w2" in third.reason


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


@pytest.mark.unit
def test_a_same_profile_rejection_names_both_effective_revisions():
    """The reason has to show the operator what actually differs.

    When the profiles match, the revisions are what conflict, and the upload's
    revision may be implicit -- it named none and inherits the published one. The
    reason interpolates the EFFECTIVE revision for that reason: interpolating the
    raw value prints the same profile on both sides with nothing distinguishing
    them, which reads as a bug in the resolver rather than a conflict to fix.
    """
    assignment = _resolve(
        "acme/x.pdf",
        metadata_profile="lending",
        metadata_revision=None,
        mappings=[
            _mapping("acme/", "lending", configRevision=3, metadataPrecedence="reject")
        ],
        published_revision=lambda _p: 9,
    )
    assert assignment.rejected is True
    assert "'lending' r3" in assignment.reason
    assert "'lending' r9" in assignment.reason


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
def test_the_no_mapping_path_resolves_the_revision_at_ingest():
    """The one way a deployment with ZERO mappings behaves differently.

    The profile is what it always was, but the revision is now pinned here rather
    than backfilled when `queue_processor` dequeues the document. That is the
    difference an operator planning a promotion while a backlog drains needs to
    know about, so it is asserted rather than only described -- and it is asserted
    with no mappings at all, because that is the configuration it applies to and
    the reason it cannot be called a mapping-only change.
    """
    assignment = _resolve(
        "unmapped/x.pdf", mappings=[], published_revision=lambda _p: 9
    )
    assert assignment.source == SOURCE_ACTIVE_PROFILE
    assert assignment.profile == "active"
    assert assignment.revision == 9, (
        "the active-profile path must resolve the published revision at ingest; "
        "leaving it None defers the pin to dequeue, which splits a queue spanning "
        "a promotion across two configurations"
    )


@pytest.mark.unit
def test_an_upload_that_names_a_profile_keeps_its_revision_unresolved():
    """The deliberate other half of that asymmetry.

    Resolving here would pin a revision the uploader did not choose, so this path
    passes the revision through as given and `queue_processor` still backfills it.
    """
    assignment = _resolve(
        "unmapped/x.pdf",
        metadata_profile="chosen",
        mappings=[],
        published_revision=lambda _p: 9,
    )
    assert assignment.source == SOURCE_METADATA
    assert assignment.revision is None


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
@pytest.mark.parametrize(
    "precedence,metadata_profile",
    [
        # Each row produces a DIFFERENT status-flag combination on the underlying
        # assignment, which is what makes the flags a disclosure channel: `reject`
        # sets source="rejected", a disagreement sets conflict, and the mapping
        # naming a profile that does not exist sets unresolvable.
        ("mapping", None),
        ("mapping", "teamA-prod"),
        ("metadata", "teamA-prod"),
        ("reject", "teamA-prod"),
    ],
)
def test_a_scope_denial_discloses_nothing_about_the_mapping(
    precedence, metadata_profile
):
    """An out-of-scope caller must not be able to tell a governed prefix from a
    bare one, and none of the three status flags names a profile -- which is why
    they are easy to leave in and why each is a channel on its own.

    `source` is "rejected" exactly when a reject-mode mapping matched, `conflict`
    only when a mapping disagreed with the caller's own metadata, `unresolvable`
    only when a mapping named a missing profile. Probing a key and reading any of
    them walks out the routing policy `listConfigPrefixMappings` is Admin-only to
    protect. Pinned at the RESOLVER rather than only at the API handler, because
    the handler's field-by-field nulling is defence in depth and a caller added
    later would inherit the disclosure by doing nothing wrong.
    """
    governed = _resolve(
        "finance/x.pdf",
        metadata_profile=metadata_profile,
        mappings=[_mapping("finance/", "finance-prod", metadataPrecedence=precedence)],
        allowed_profiles=["teamA"],
        profile_exists=lambda _p: False,
    )
    assert governed.scope_denied is True

    # Byte-identical to the answer for a prefix no mapping governs at all, apart
    # from the fields that describe the CALLER's own request.
    assert governed.profile is None
    assert governed.revision is None
    assert governed.mapping_prefix is None
    assert governed.rejected is False
    assert governed.conflict is False
    assert governed.unresolvable is False
    assert governed.source == SOURCE_ACTIVE_PROFILE, (
        "source still carries the decision a mapping drove; for `reject` it reads "
        "'rejected', which tells an out-of-scope caller a mapping governs the key"
    )
    assert "finance" not in governed.reason


@pytest.mark.unit
def test_a_denial_for_a_governed_prefix_is_indistinguishable_from_an_ungoverned_one():
    """The property the field list above exists to produce, asserted directly."""
    governed = _resolve(
        "finance/x.pdf",
        metadata_profile="other-prod",
        mappings=[_mapping("finance/", "finance-prod", metadataPrecedence="reject")],
        allowed_profiles=["teamA"],
    )
    ungoverned = _resolve(
        "finance/x.pdf", metadata_profile="other-prod", allowed_profiles=["teamA"]
    )
    assert governed == ungoverned, (
        "a caller can tell a reject-mode mapping from no mapping by comparing the "
        "two denials, which is one probe per prefix against the routing policy"
    )


@pytest.mark.unit
@pytest.mark.parametrize("precedence", ["mapping", "metadata", "reject"])
def test_a_requested_profile_is_scope_checked_even_when_a_mapping_overrides_it(
    precedence,
):
    """The route a guard on the RESOLVED profile alone does not cover.

    Under the default `mapping` precedence the caller's requested profile never
    reaches `assignment.profile`, so a check on that value alone never asks about
    it -- while the upload resolver deliberately leaves the request unrewritten, so
    it is still stamped on the object. If the mapping read then fails OPEN at
    ingest, rule 2 applies that stamp and the document processes under a profile
    this caller was never checked against. Reached through a transient failure
    rather than a missing check, which is why it has to be refused up front.
    """
    assignment = _resolve(
        "tenant-a-docs/x.pdf",
        metadata_profile="finance-prod",
        mappings=[
            _mapping("tenant-a-docs/", "tenant-a", metadataPrecedence=precedence)
        ],
        allowed_profiles=["tenant-a"],
    )
    assert assignment.scope_denied is True, (
        "a scope-restricted caller named a profile outside their scope and was "
        "allowed, because a mapping to an IN-scope profile won the precedence "
        "contest and the request itself was never checked"
    )
    # And nothing about the mapping leaks on the way out.
    assert assignment.profile is None
    assert assignment.mapping_prefix is None


@pytest.mark.unit
def test_naming_an_in_scope_profile_under_a_mapping_is_still_allowed():
    """The check above must not refuse the legitimate shape it sits next to."""
    assignment = _resolve(
        "tenant-a-docs/x.pdf",
        metadata_profile="tenant-a",
        mappings=[_mapping("tenant-a-docs/", "tenant-a")],
        allowed_profiles=["tenant-a"],
    )
    assert assignment.scope_denied is False
    assert assignment.profile == "tenant-a"


@pytest.mark.unit
def test_a_mapping_cannot_deny_an_internal_submission():
    """Rule 1 says mappings are not consulted for an internal producer, so a
    mapping must not be able to refuse one either -- including through the scope
    guard, which would otherwise deny on a mapping the document had explicitly
    bypassed. Unreachable today (the only caller passing a submission source
    passes no scope), but it is the precedence contract rather than an accident.

    The producer's OWN profile is still checked, which is why this uses an
    in-scope companion: the exemption is from the mapping, not from the scope.
    """
    assignment = _resolve(
        "tenant-a-docs/x.pdf",
        metadata_profile="tenant-a",
        submission_source="pii-anonymizer",
        mappings=[_mapping("tenant-a-docs/", "finance-prod")],
        allowed_profiles=["tenant-a"],
    )
    assert assignment.scope_denied is False, (
        "a mapping the internal-producer branch never consults was still able to "
        "deny the submission through the scope guard"
    )
    assert assignment.profile == "tenant-a"
    assert assignment.source == SOURCE_INTERNAL_PRODUCER


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
def test_multibyte_fields_do_not_escape_the_item_arithmetic():
    """The bounds are in BYTES, which is the unit the arithmetic above is in.

    UTF-8 runs to four bytes per character, so a character-counted description and
    actor pair would let the same nominally-at-the-limit entry be up to four times
    larger — ~940 KB at MAX_MAPPINGS, over the 400 KB item limit every individual
    bound would still read as satisfying. The description is refused at the API and
    both are truncated on normalization; this measures the result of both.
    """
    assert description_rejection_reason("あ" * 200) is not None, (
        "600 bytes of CJK is over the 500-byte description bound and must be "
        "refused; a character count would admit it."
    )

    entries = [
        normalize_entry(
            {
                "prefix": ("p" * 508 + f"{n:04d}" + "/"),
                "configProfile": "q" * 128,
                "configRevision": 999999,
                "metadataPrecedence": "reject",
                "enabled": True,
                # Four bytes per character, at four times the character bound.
                "description": "🧾" * 500,
                "createdAt": "2026-10-10T00:00:00.000000Z",
                "createdBy": "あ" * 256,
                "updatedAt": "2026-10-10T00:00:00.000000Z",
                "updatedBy": "あ" * 256,
            }
        )
        for n in range(MAX_MAPPINGS)
    ]
    for entry in entries:
        assert len(entry["description"].encode("utf-8")) <= 500
        assert len(entry["createdBy"].encode("utf-8")) <= 256
        assert len(entry["updatedBy"].encode("utf-8")) <= 256

    size = len(str(sort_entries(entries)).encode("utf-8"))
    assert size < 400 * 1024, (
        f"MAX_MAPPINGS multibyte entries serialize to {size:,} bytes against "
        f"DynamoDB's 400 KB item limit. The per-entry bounds must be counted in "
        f"UTF-8 bytes, not characters."
    )


@pytest.mark.unit
def test_an_over_length_description_is_refused_with_a_message_naming_the_limit():
    """Refused rather than truncated, because the admin typed it.

    A silently shortened description reports `success: true` for a mapping whose
    stored form is not what was entered. The actor strings are the deliberate
    exception and are covered by the normalization assertions above.
    """
    assert description_rejection_reason(None) is None
    assert description_rejection_reason("") is None
    assert description_rejection_reason("D" * 500) is None

    reason = description_rejection_reason("D" * 501)
    assert reason is not None
    assert "500 bytes" in reason
    assert "501" in reason


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
def test_delete_answers_about_the_winning_pass_not_the_first_one():
    """`delete` returns "no such mapping remains", not "this call removed it".

    `_rewrite` calls the mutation once per attempt and retries under optimistic
    concurrency, so a concurrent delete of the same prefix can win between two
    attempts. The flag therefore has to reflect the LAST attempt: latched on the
    first, this reports a removal the caller did not perform. The end state is the
    same either way, which is exactly why the wrong answer would never be noticed
    in practice -- so it is asserted over the mutation itself rather than left to a
    race that is hard to provoke.
    """
    store = PrefixMappingStore(_make_table())
    attempts = []

    def two_attempts(mutate):
        """Attempt one sees the entry; by the retry somebody else removed it."""
        attempts.append(mutate([{"prefix": "a/", "configProfile": "lending"}]))
        attempts.append(mutate([]))
        return True

    store._rewrite = two_attempts
    assert store.delete("a/") is False, (
        "delete reported that it removed a mapping the winning attempt found "
        "already gone"
    )
    # The mutation itself behaved correctly on both attempts: it removed the entry
    # on the first and declined to write on the second.
    assert attempts[0] == []
    assert attempts[1] is None


@pytest.mark.unit
@mock_aws
def test_delete_still_reports_an_ordinary_removal():
    """The common path, so the test above cannot pass by breaking it."""
    store = PrefixMappingStore(_make_table())
    store.put("a/", "lending")
    assert store.delete("a/") is True
    assert store.delete("a/") is False


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
