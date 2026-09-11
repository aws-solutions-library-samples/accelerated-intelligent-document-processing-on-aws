# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Configuration-profile scope matching (``allowedConfigVersions``).

This is a security boundary: it decides which profiles a user can read/edit and
which documents they can see. The fail-closed cases below are the ones that
matter — before this module existed, the document-list resolvers admitted any
document whose ConfigVersion was absent.
"""

import pytest

from idp_common.config_scope import (
    filter_profiles,
    is_pattern,
    normalize_scope,
    scope_allows,
)


@pytest.mark.unit
class TestUnrestricted:
    @pytest.mark.parametrize("scope", [None, [], (), set(), ""])
    def test_absent_scope_allows_everything(self, scope):
        """Scoping is opt-in per user; most users have none."""
        assert scope_allows(scope, "anything") is True

    def test_absent_scope_allows_an_unnamed_target(self):
        assert scope_allows(None, None) is True
        assert scope_allows(None, "") is True

    def test_blank_entries_do_not_create_a_scope(self):
        """A stray empty string must not become a rule that matches nothing."""
        assert normalize_scope(["", "  "]) is None
        assert scope_allows(["", "  "], "lending") is True


@pytest.mark.unit
class TestExactMatching:
    def test_named_profile_in_scope_is_allowed(self):
        assert scope_allows(["lending", "claims"], "claims") is True

    def test_named_profile_outside_scope_is_denied(self):
        assert scope_allows(["lending"], "claims") is False

    def test_matching_is_case_sensitive(self):
        """Profile names are case-sensitive keys in DynamoDB; scope must agree."""
        assert scope_allows(["Lending"], "lending") is False

    def test_partial_names_do_not_match(self):
        assert scope_allows(["lending"], "lending-2") is False
        assert scope_allows(["lending-2"], "lending") is False


@pytest.mark.unit
class TestFailsClosed:
    @pytest.mark.parametrize("target", [None, "", 0, False])
    def test_unnamed_target_is_denied_when_scoped(self, target):
        """
        A document with no ConfigVersion cannot be proven in scope. Admitting it
        leaked every document processed before config-version stamping to every
        scoped user.
        """
        assert scope_allows(["lending"], target) is False

    def test_unusable_scope_type_is_treated_as_unrestricted_not_as_a_match(self):
        # A malformed attribute should not silently become a scope that matches
        # one arbitrary thing; it degrades to the documented default.
        assert normalize_scope(42) is None
        assert scope_allows(42, "lending") is True


@pytest.mark.unit
class TestPatterns:
    def test_trailing_wildcard_matches_a_lineage(self):
        """The reason patterns exist: usecaseA_v1, usecaseA_v2, … under one grant."""
        scope = ["usecaseA_*"]
        assert scope_allows(scope, "usecaseA_v1") is True
        assert scope_allows(scope, "usecaseA_v2") is True
        assert scope_allows(scope, "usecaseB_v1") is False

    def test_single_character_wildcard(self):
        assert scope_allows(["uc?-prod"], "uc1-prod") is True
        assert scope_allows(["uc?-prod"], "uc12-prod") is False

    def test_character_class(self):
        assert scope_allows(["uc[12]"], "uc1") is True
        assert scope_allows(["uc[12]"], "uc3") is False

    def test_bare_wildcard_is_effectively_unrestricted(self):
        assert scope_allows(["*"], "anything") is True

    def test_a_pattern_still_denies_an_unnamed_target(self):
        assert scope_allows(["*"], None) is False

    def test_pattern_detection(self):
        assert is_pattern("lending-*") is True
        assert is_pattern("uc?") is True
        assert is_pattern("uc[12]") is True
        assert is_pattern("lending") is False

    def test_a_literal_name_is_not_treated_as_a_regex(self):
        """Dots are legal in profile names (semver-style) and must be literal."""
        assert scope_allows(["v0.1.6"], "v0X1X6") is False
        assert scope_allows(["v0.1.6"], "v0.1.6") is True


@pytest.mark.unit
class TestFilterProfiles:
    def test_filters_to_the_permitted_names(self):
        names = ["lending-1", "lending-2", "claims"]
        assert filter_profiles(["lending-*"], names) == ["lending-1", "lending-2"]

    def test_unrestricted_scope_passes_everything_through(self):
        names = ["a", "b"]
        assert filter_profiles(None, names) == names

    def test_a_string_scope_is_accepted_as_a_single_entry(self):
        assert filter_profiles("claims", ["claims", "lending"]) == ["claims"]
