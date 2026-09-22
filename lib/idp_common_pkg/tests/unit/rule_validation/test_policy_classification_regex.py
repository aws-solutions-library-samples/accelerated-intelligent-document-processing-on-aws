# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for `PolicyClassificationService`'s regex matchers and skip results.

Complements `test_policy_classification.py`, which covers `classify_document` and the
loading of policy classes. What is added here is the two regex checks and the three
result builders that run when nothing matches.

This service is a **cost gate**: it decides which policy types a document is evaluated
against, and a document that matches nothing skips rule validation entirely. So the two
failure directions are asymmetric and both matter. Matching too broadly spends a
fact-extraction model call per rule on a policy that does not apply. Matching too
narrowly skips validation and reports `NO_POLICY_MATCH` — which looks like a
deliberate, correct outcome in the UI, not like a missed evaluation.

Two behaviours are asserted because they are easy to get backwards:

**Both checks return every match, not the first.** A document can belong to several
policy types, and stopping at the first would silently drop the rest.

**Page-content matching skips policy types already matched by name.** That is what
keeps a document from being evaluated twice against the same policy — the dedupe lives
in the content check rather than in the caller.

The skip results are asserted on shape as well as content, because the orchestrator's
markdown formatter reads the same keys and a missing `overall_statistics` block would
render a report with no statistics section rather than one showing zeroes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from idp_common.config.schema_constants import (
    X_AWS_IDP_DOCUMENT_NAME_REGEX,
    X_AWS_IDP_PAGE_CONTENT_REGEX,
    X_AWS_IDP_POLICY_TYPE,
)
from idp_common.rule_validation.policy_classification import (
    PolicyClassificationService,
)


def _config(*policies: dict[str, Any]) -> dict[str, Any]:
    return {"policy_classes": list(policies)}


def _policy(
    policy_type: str,
    *,
    name_regex: str | None = None,
    content_regex: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        X_AWS_IDP_POLICY_TYPE: policy_type,
        "rule_properties": {"r1": {"type": "string", "description": "a rule"}},
    }
    if name_regex is not None:
        entry[X_AWS_IDP_DOCUMENT_NAME_REGEX] = name_regex
    if content_regex is not None:
        entry[X_AWS_IDP_PAGE_CONTENT_REGEX] = content_regex
    return entry


def _service(*policies: dict[str, Any]) -> PolicyClassificationService:
    return PolicyClassificationService(config=_config(*policies))


def _document(document_id: str = "lending_package.pdf") -> MagicMock:
    document = MagicMock()
    document.id = document_id
    return document


@pytest.mark.unit
class TestDocumentNameRegex:
    """_check_document_name_regex: filename matching."""

    def test_a_matching_name_returns_its_policy_type(self):
        service = _service(_policy("Lending", name_regex=r"lending"))
        assert service._check_document_name_regex("lending_package.pdf") == ["Lending"]

    def test_a_non_matching_name_returns_nothing(self):
        service = _service(_policy("Lending", name_regex=r"lending"))
        assert service._check_document_name_regex("invoice.pdf") == []

    def test_every_matching_policy_type_is_returned_not_just_the_first(self):
        # A document can belong to several policy types; stopping at the first would
        # silently skip the rules of the others.
        service = _service(
            _policy("Lending", name_regex=r"package"),
            _policy("Insurance", name_regex=r"package"),
        )
        assert sorted(service._check_document_name_regex("a_package.pdf")) == [
            "Insurance",
            "Lending",
        ]

    def test_matching_is_a_search_rather_than_a_full_match(self):
        # The pattern is applied with .search, so a substring is enough. Requiring a
        # full match would make every configured pattern need anchors and wildcards.
        service = _service(_policy("Lending", name_regex=r"lend"))
        assert service._check_document_name_regex("2026_lending_pkg_v2.pdf") == [
            "Lending"
        ]

    def test_an_empty_document_id_matches_nothing(self):
        service = _service(_policy("Lending", name_regex=r".*"))
        assert service._check_document_name_regex("") == []

    def test_policy_types_with_no_name_pattern_are_ignored(self):
        # A policy class configured only for content matching must not be selected by
        # the name check, or every document would match it.
        service = _service(
            _policy("Lending", name_regex=r"lending"),
            _policy("ContentOnly", content_regex=r"anything"),
        )
        assert service._check_document_name_regex("lending.pdf") == ["Lending"]

    def test_no_name_patterns_configured_at_all_returns_nothing(self):
        service = _service(_policy("ContentOnly", content_regex=r"x"))
        assert service._check_document_name_regex("anything.pdf") == []

    def test_an_anchored_pattern_is_honoured(self):
        service = _service(_policy("Lending", name_regex=r"^lending"))
        assert service._check_document_name_regex("lending.pdf") == ["Lending"]
        assert service._check_document_name_regex("re_lending.pdf") == []


@pytest.mark.unit
class TestPageContentRegex:
    """_check_page_content_regex: page-text matching, and its dedupe."""

    def test_matching_page_text_returns_its_policy_type(self):
        service = _service(_policy("Lending", content_regex=r"Loan Amount"))
        assert service._check_page_content_regex(
            "Total Loan Amount: $100", "1", []
        ) == ["Lending"]

    def test_non_matching_page_text_returns_nothing(self):
        service = _service(_policy("Lending", content_regex=r"Loan Amount"))
        assert service._check_page_content_regex("An invoice", "1", []) == []

    def test_empty_page_text_returns_nothing(self):
        service = _service(_policy("Lending", content_regex=r".*"))
        assert service._check_page_content_regex("", "1", []) == []

    def test_a_policy_type_already_matched_by_name_is_skipped(self):
        # This is where the dedupe lives. Without it a document matching by both name
        # and content would be evaluated twice against the same policy, doubling the
        # model calls for its rules.
        service = _service(_policy("Lending", content_regex=r"Loan"))
        assert service._check_page_content_regex("Loan Amount", "1", ["Lending"]) == []

    def test_only_the_already_matched_types_are_skipped(self):
        service = _service(
            _policy("Lending", content_regex=r"Loan"),
            _policy("Insurance", content_regex=r"Loan"),
        )
        assert service._check_page_content_regex("Loan", "1", ["Lending"]) == [
            "Insurance"
        ]

    def test_policy_types_with_no_content_pattern_are_ignored(self):
        service = _service(
            _policy("Lending", content_regex=r"Loan"),
            _policy("NameOnly", name_regex=r"x"),
        )
        assert service._check_page_content_regex("Loan", "1", []) == ["Lending"]

    def test_every_matching_policy_type_is_returned(self):
        service = _service(
            _policy("Lending", content_regex=r"Amount"),
            _policy("Insurance", content_regex=r"Amount"),
        )
        assert sorted(service._check_page_content_regex("Amount: 5", "1", [])) == [
            "Insurance",
            "Lending",
        ]

    def test_matching_is_case_insensitive(self):
        # Both patterns are compiled with re.IGNORECASE. That is the right default for
        # OCR text, whose casing follows the scan rather than the configuration, and it
        # is worth pinning because removing the flag would silently stop matching
        # every document whose heading is upper-cased.
        service = _service(_policy("Lending", content_regex=r"Loan Amount"))
        assert service._check_page_content_regex("loan amount", "1", []) == ["Lending"]
        assert service._check_page_content_regex("LOAN AMOUNT", "1", []) == ["Lending"]

    def test_an_invalid_pattern_makes_its_policy_unmatchable_rather_than_raising(self):
        # A bad pattern is logged and left compiled to None, so the policy class is
        # loaded but can never match. Pinned because the failure is silent: rule
        # validation reports NO_POLICY_MATCH, which reads like a correct outcome
        # rather than a configuration error.
        service = _service(_policy("Broken", content_regex=r"([unclosed"))
        assert service._check_page_content_regex("[unclosed", "1", []) == []
        assert service.get_all_policy_types() == ["Broken"]


@pytest.mark.unit
class TestRegexInventory:
    """get_all_policy_types and has_regex_patterns."""

    def test_all_configured_policy_types_are_listed(self):
        service = _service(_policy("Lending"), _policy("Insurance"))
        assert sorted(service.get_all_policy_types()) == ["Insurance", "Lending"]

    def test_no_policy_classes_gives_an_empty_list(self):
        assert PolicyClassificationService(config={}).get_all_policy_types() == []

    def test_a_name_pattern_counts_as_having_patterns(self):
        assert (
            _service(_policy("Lending", name_regex=r"x")).has_regex_patterns() is True
        )

    def test_a_content_pattern_counts_as_having_patterns(self):
        assert (
            _service(_policy("Lending", content_regex=r"x")).has_regex_patterns()
            is True
        )

    def test_no_patterns_at_all_is_reported_as_false(self):
        # This is the flag the caller uses to decide between regex filtering and
        # evaluating every policy type. Reporting True here would filter a document
        # against patterns that do not exist and skip validation entirely.
        assert _service(_policy("Lending")).has_regex_patterns() is False

    def test_one_policy_with_a_pattern_is_enough(self):
        service = _service(_policy("NoPattern"), _policy("Lending", name_regex=r"x"))
        assert service.has_regex_patterns() is True


@pytest.mark.unit
class TestSkipResults:
    """The two skip results and their markdown rendering."""

    def test_the_no_policy_classes_result_names_its_status_and_document(self):
        result = _service().create_no_policy_classes_result(_document("a.pdf"))
        assert result["document_id"] == "a.pdf"
        assert result["overall_status"] == "NO_POLICY_CLASSES"
        assert "No policy classes configured" in result["message"]

    def test_the_no_match_result_names_the_document_in_its_message(self):
        # The message is what the user reads to understand why nothing was evaluated,
        # so naming the document is what makes it actionable.
        result = _service(_policy("Lending", name_regex=r"x")).create_no_match_result(
            _document("mystery.pdf")
        )
        assert result["overall_status"] == "NO_POLICY_MATCH"
        assert "mystery.pdf" in result["message"]

    @pytest.mark.parametrize(
        "builder", ["create_no_policy_classes_result", "create_no_match_result"]
    )
    def test_both_results_carry_the_keys_the_report_formatter_reads(self, builder):
        # The orchestrator's markdown formatter reads these keys unconditionally; a
        # missing overall_statistics block renders a report with no statistics section
        # rather than one showing zeroes.
        result = getattr(_service(_policy("Lending", name_regex=r"x")), builder)(
            _document()
        )
        assert set(result) >= {
            "document_id",
            "overall_status",
            "total_policy_types",
            "message",
            "rule_summary",
            "overall_statistics",
            "supporting_pages",
            "rule_details",
            "generated_at",
        }

    @pytest.mark.parametrize(
        "builder", ["create_no_policy_classes_result", "create_no_match_result"]
    )
    def test_both_results_report_zero_rules_rather_than_omitting_the_counts(
        self, builder
    ):
        # Zero evaluated is the true statement. Omitting the counts would let a reader
        # infer the rules passed.
        statistics = getattr(_service(_policy("Lending", name_regex=r"x")), builder)(
            _document()
        )["overall_statistics"]
        assert statistics["total_rules"] == 0
        assert statistics["pass_count"] == 0
        assert statistics["fail_count"] == 0
        assert statistics["information_not_found_count"] == 0
        assert statistics["pass_percentage"] == 0.0

    @pytest.mark.parametrize(
        "builder", ["create_no_policy_classes_result", "create_no_match_result"]
    )
    def test_both_results_carry_a_generation_timestamp(self, builder):
        result = getattr(_service(_policy("Lending", name_regex=r"x")), builder)(
            _document()
        )
        assert result["generated_at"]

    def test_the_markdown_reports_the_document_status_and_message(self):
        service = _service()
        result = service.create_no_policy_classes_result(_document("a.pdf"))
        markdown = service.format_skip_result_as_markdown(result)
        assert "# Rule Validation Summary: a.pdf" in markdown
        assert "NO_POLICY_CLASSES" in markdown
        assert "No policy classes configured" in markdown

    def test_the_markdown_shows_explicit_zeroes(self):
        # An empty statistics table would read as "not computed"; zeroes read as
        # "nothing was evaluated", which is what happened.
        service = _service()
        markdown = service.format_skip_result_as_markdown(
            service.create_no_match_result(_document())
        )
        assert "0 (0 / 0 / 0)" in markdown
        assert "0.0%" in markdown

    def test_the_markdown_heading_matches_the_orchestrators_so_the_ui_renders_both(
        self,
    ):
        # The UI shows whichever summary exists; a different top-level heading would
        # make a skipped document look like a different kind of artifact.
        service = _service()
        markdown = service.format_skip_result_as_markdown(
            service.create_no_match_result(_document("a.pdf"))
        )
        assert markdown.startswith("# Rule Validation Summary: ")

    def test_the_markdown_tolerates_a_result_with_nothing_in_it(self):
        markdown = _service().format_skip_result_as_markdown({})
        assert "Rule Validation Summary: Document" in markdown
        assert "SKIPPED" in markdown
