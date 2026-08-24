# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for PolicyClassificationService.

Covers match, no-match, and error paths of the regex-based classifier
introduced by the Policy Discovery feature. Page-content regex tests stub out
S3 text fetches via monkeypatching so no network is required.
"""

# ruff: noqa: E402, I001

import pytest
from unittest.mock import patch

from idp_common.config.models import IDPConfig
from idp_common.models import Document, Page
from idp_common.rule_validation.policy_classification import PolicyClassificationService


def _config_with_policies(*classes):
    """Build an IDPConfig whose policy_classes holds the given raw dicts."""
    cfg = IDPConfig()
    cfg.policy_classes = list(classes)
    return cfg


def _doc(doc_id="doc.pdf", pages_text=None):
    """Build a Document with the given id and optional {page_id: text} pages.

    Page text is not loaded from S3 in tests; the test monkeypatches
    s3.get_text_content instead. We populate parsed_text_uri with a dummy so
    _run_regex_checks doesn't skip the page.
    """
    doc = Document(id=doc_id)
    pages_text = pages_text or {}
    for page_id in pages_text:
        doc.pages[page_id] = Page(
            page_id=page_id, parsed_text_uri=f"s3://bucket/{page_id}.txt"
        )
    return doc


@pytest.mark.unit
class TestPolicyClassificationNoClasses:
    def test_empty_config_returns_empty_match(self):
        service = PolicyClassificationService(config=IDPConfig())
        result = service.classify_document(_doc("anything.pdf"))
        assert result.matched_policy_types == []


@pytest.mark.unit
class TestPolicyClassificationSingleClass:
    """Single-class mode: the class always matches regardless of regex."""

    def test_no_regex_trivial_match(self):
        cfg = _config_with_policies({"x-aws-idp-policy-type": "only_policy"})
        service = PolicyClassificationService(config=cfg)
        result = service.classify_document(_doc("abc.pdf"))
        assert result.matched_policy_types == ["only_policy"]

    def test_regex_matches(self):
        cfg = _config_with_policies(
            {
                "x-aws-idp-policy-type": "only_policy",
                "x-aws-idp-document-name-regex": r"(?i).*medicare.*",
            }
        )
        service = PolicyClassificationService(config=cfg)
        result = service.classify_document(_doc("medicare_pa.pdf"))
        assert result.matched_policy_types == ["only_policy"]

    def test_regex_fails_but_single_class_still_matches(self):
        # Per classify_document: single class + regex mismatch still falls
        # through to the "single policy class" default, returning it.
        cfg = _config_with_policies(
            {
                "x-aws-idp-policy-type": "only_policy",
                "x-aws-idp-document-name-regex": r"WILL_NOT_MATCH",
            }
        )
        service = PolicyClassificationService(config=cfg)
        result = service.classify_document(_doc("unrelated.pdf"))
        assert result.matched_policy_types == ["only_policy"]


@pytest.mark.unit
class TestPolicyClassificationMultipleClasses:
    """Multi-class mode: regex is required to disambiguate."""

    def test_no_regex_configured_returns_empty(self):
        cfg = _config_with_policies(
            {"x-aws-idp-policy-type": "policy_a"},
            {"x-aws-idp-policy-type": "policy_b"},
        )
        service = PolicyClassificationService(config=cfg)
        result = service.classify_document(_doc("whatever.pdf"))
        assert result.matched_policy_types == []

    def test_document_name_regex_matches_one(self):
        cfg = _config_with_policies(
            {
                "x-aws-idp-policy-type": "medicare",
                "x-aws-idp-document-name-regex": r"(?i).*medicare.*",
            },
            {
                "x-aws-idp-policy-type": "invoice",
                "x-aws-idp-document-name-regex": r"(?i).*invoice.*",
            },
        )
        service = PolicyClassificationService(config=cfg)
        result = service.classify_document(_doc("medicare_pa_packet.pdf"))
        assert result.matched_policy_types == ["medicare"]

    def test_document_name_regex_matches_multiple(self):
        cfg = _config_with_policies(
            {
                "x-aws-idp-policy-type": "policy_a",
                "x-aws-idp-document-name-regex": r"(?i).*shared.*",
            },
            {
                "x-aws-idp-policy-type": "policy_b",
                "x-aws-idp-document-name-regex": r"(?i).*shared.*",
            },
        )
        service = PolicyClassificationService(config=cfg)
        result = service.classify_document(_doc("shared_doc.pdf"))
        assert sorted(result.matched_policy_types) == ["policy_a", "policy_b"]

    def test_no_name_regex_match_returns_empty(self):
        cfg = _config_with_policies(
            {
                "x-aws-idp-policy-type": "medicare",
                "x-aws-idp-document-name-regex": r"(?i).*medicare.*",
            },
            {
                "x-aws-idp-policy-type": "invoice",
                "x-aws-idp-document-name-regex": r"(?i).*invoice.*",
            },
        )
        service = PolicyClassificationService(config=cfg)
        result = service.classify_document(_doc("unrelated.pdf"))
        assert result.matched_policy_types == []

    def test_page_content_regex_matches(self):
        cfg = _config_with_policies(
            {
                "x-aws-idp-policy-type": "medicare",
                "x-aws-idp-document-page-content-regex": r"(?i)medicare number",
            },
            {
                "x-aws-idp-policy-type": "invoice",
                "x-aws-idp-document-page-content-regex": r"(?i)invoice number",
            },
        )
        service = PolicyClassificationService(config=cfg)
        doc = _doc("unknown.pdf", pages_text={"1": "page 1 text"})

        with patch(
            "idp_common.rule_validation.policy_classification.s3.get_text_content",
            return_value="Claim submitted with Medicare Number 12345",
        ):
            result = service.classify_document(doc)

        assert result.matched_policy_types == ["medicare"]
        assert result.matched_page_ids == {"medicare": "1"}

    def test_page_content_regex_read_failure_swallowed(self):
        """An exception reading page text should not crash classification."""
        cfg = _config_with_policies(
            {
                "x-aws-idp-policy-type": "medicare",
                "x-aws-idp-document-name-regex": r"(?i).*medicare.*",
                "x-aws-idp-document-page-content-regex": r"(?i)medicare number",
            },
            {
                "x-aws-idp-policy-type": "other",
                "x-aws-idp-document-name-regex": r"(?i).*unrelated.*",
            },
        )
        service = PolicyClassificationService(config=cfg)
        doc = _doc("medicare_x.pdf", pages_text={"1": "p1"})

        with patch(
            "idp_common.rule_validation.policy_classification.s3.get_text_content",
            side_effect=RuntimeError("boom"),
        ):
            # Name regex still matches even if page read fails
            result = service.classify_document(doc)

        assert result.matched_policy_types == ["medicare"]

    def test_invalid_regex_pattern_does_not_crash(self):
        # A malformed regex in the config should be logged and skipped, not
        # raise a re.error that tanks the whole classifier.
        cfg = _config_with_policies(
            {
                "x-aws-idp-policy-type": "bad",
                "x-aws-idp-document-name-regex": "(unclosed",
            },
            {
                "x-aws-idp-policy-type": "good",
                "x-aws-idp-document-name-regex": r"(?i).*medicare.*",
            },
        )
        service = PolicyClassificationService(config=cfg)
        result = service.classify_document(_doc("medicare_x.pdf"))
        assert result.matched_policy_types == ["good"]


@pytest.mark.unit
class TestLegacyRuleTypeDiscriminator:
    """A class keyed on the legacy discriminator must still be matched (#600).

    Raw rules-discovery output and pre-v0.5.9 configs carry
    `x-aws-idp-rule-type`. Reading only `x-aws-idp-policy-type` meant such a
    class was skipped here with nothing logged, so rule validation silently
    produced no output.
    """

    def test_legacy_rule_type_is_honored(self):
        cfg = _config_with_policies(
            {
                "x-aws-idp-rule-type": "legacy_policy",
                "rule_properties": {"r": {"type": "string", "description": "Q?"}},
            }
        )
        service = PolicyClassificationService(config=cfg)
        result = service.classify_document(_doc("anything.pdf"))
        assert result.matched_policy_types == ["legacy_policy"]

    def test_current_key_wins_when_both_present(self):
        cfg = _config_with_policies(
            {
                "x-aws-idp-policy-type": "current",
                "x-aws-idp-rule-type": "legacy",
                "rule_properties": {"r": {"type": "string", "description": "Q?"}},
            }
        )
        service = PolicyClassificationService(config=cfg)
        result = service.classify_document(_doc("anything.pdf"))
        assert result.matched_policy_types == ["current"]

    def test_class_with_neither_key_is_still_skipped(self):
        cfg = _config_with_policies({"rule_properties": {"r": {"description": "Q?"}}})
        service = PolicyClassificationService(config=cfg)
        assert service.policy_classes == []


@pytest.mark.unit
class TestSecondDiscoveredClassRegression:
    """Two regex-less discovered classes match nothing — the documented trap (#601).

    This asserts the CURRENT fail-closed behavior rather than the behavior the
    issue's suggested test wanted, because failing open would run every rule
    against every document. The fix for #601 is that the discovery job now warns
    at save time (see RulesDiscovery._warn_if_no_regex); this test pins the
    runtime contract that warning describes, so the two cannot drift.
    """

    def test_two_regexless_classes_match_nothing(self):
        cfg = _config_with_policies(
            {"x-aws-idp-policy-type": "policy_a", "rule_properties": {}},
            {"x-aws-idp-policy-type": "policy_b", "rule_properties": {}},
        )
        service = PolicyClassificationService(config=cfg)
        result = service.classify_document(_doc("some_doc.pdf"))
        assert result.matched_policy_types == []

    def test_one_class_without_regex_is_fine_when_another_has_one(self):
        """A regex on any class re-enables matching for the classes that have one."""
        cfg = _config_with_policies(
            {
                "x-aws-idp-policy-type": "policy_a",
                "x-aws-idp-document-name-regex": r"(?i).*claim.*",
                "rule_properties": {},
            },
            {"x-aws-idp-policy-type": "policy_b", "rule_properties": {}},
        )
        service = PolicyClassificationService(config=cfg)
        result = service.classify_document(_doc("claim_1.pdf"))
        assert result.matched_policy_types == ["policy_a"]


@pytest.mark.unit
class TestServiceSideLegacyLookup:
    """The classifier and the question lookup must agree on the discriminator.

    If PolicyClassificationService honors the legacy key but the service's
    question lookup does not, a class classifies and then yields ZERO rule
    questions — the same silent no-op, one stage later.
    """

    def test_policy_types_and_questions_both_read_legacy_key(self):
        from idp_common.rule_validation.service import RuleValidationService

        config = {
            "policy_classes": [
                {
                    "x-aws-idp-rule-type": "legacy_policy",
                    "rule_properties": {
                        "r1": {"type": "string", "description": "Question one?"}
                    },
                }
            ]
        }
        service = RuleValidationService.__new__(RuleValidationService)

        assert service._get_policy_types(config) == ["legacy_policy"]
        metadata = service._get_rule_metadata(config, "legacy_policy")
        assert len(metadata) == 1
        assert metadata[0]["description"] == "Question one?"
