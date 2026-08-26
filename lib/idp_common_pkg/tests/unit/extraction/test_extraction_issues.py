# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Extraction-completeness ProcessingIssues (ExtractionService._build_extraction_issues).

An extraction that PARSES but under-produces (e.g. an empty large-table list on
Simple mode) must NOT be reported as a clean success — it emits an
``extraction_incomplete`` warning, and when Simple mode under-produced it
recommends Advanced (agentic) extraction.
"""

from __future__ import annotations

from idp_common.config.models import IDPConfig
from idp_common.extraction.service import ExtractionService


def _svc(*, agentic: bool):
    cfg = IDPConfig(**{"extraction": {"agentic": {"enabled": agentic}}})
    svc = ExtractionService(config=cfg)
    # A schema with one array field (the large table) + a scalar.
    svc._class_schema = {
        "type": "object",
        "properties": {
            "PortfolioDetail": {"type": "array", "items": {"type": "object"}},
            "AccountNumber": {"type": "string"},
        },
    }
    svc._pending_extraction_model = "us.anthropic.claude-sonnet-5"
    return svc


def test_empty_list_field_flags_extraction_incomplete_simple_recommends_advanced():
    svc = _svc(agentic=False)
    issues = svc._build_extraction_issues(
        extracted_fields={"PortfolioDetail": [], "AccountNumber": "X"},
        metadata={},
        section_id="2",
    )
    codes = [i.code for i in issues]
    assert "extraction_incomplete" in codes
    inc = next(i for i in issues if i.code == "extraction_incomplete")
    assert inc.severity == "warning"
    assert "PortfolioDetail" in inc.message
    # Simple mode → recommends Advanced (agentic) extraction.
    assert "agentic" in inc.message.lower() or "advanced" in inc.message.lower()


def test_empty_list_field_advanced_does_not_recommend_advanced():
    svc = _svc(agentic=True)
    issues = svc._build_extraction_issues(
        extracted_fields={"PortfolioDetail": [], "AccountNumber": "X"},
        metadata={},
        section_id="2",
    )
    inc = next(i for i in issues if i.code == "extraction_incomplete")
    # Already agentic → no "switch to advanced" recommendation.
    assert "recommended" not in inc.message.lower()


def test_populated_list_produces_no_extraction_incomplete():
    svc = _svc(agentic=False)
    issues = svc._build_extraction_issues(
        extracted_fields={"PortfolioDetail": [{"a": 1}], "AccountNumber": "X"},
        metadata={},
        section_id="1",
    )
    assert not any(i.code == "extraction_incomplete" for i in issues)


def test_below_threshold_population_flags_sparse():
    svc = _svc(agentic=True)
    issues = svc._build_extraction_issues(
        extracted_fields={"PortfolioDetail": [{"a": 1}], "AccountNumber": "X"},
        metadata={
            "population_check": {
                "fields_populated": 1,
                "fields_defined": 10,
                "population_ratio": 0.1,
                "threshold": 0.5,
                "below_threshold": True,
                "empty_fields": ["x", "y"],
            }
        },
        section_id="1",
    )
    assert any(i.code == "extraction_sparse" and i.severity == "info" for i in issues)


# ---------------------------------------------------------------------------
# Silent-truncation detection.
#
# Two gaps closed here, both measured live on a v0.6.5 stack:
#
#  * An ABSENT declared list produced no issue at all. Only an *empty* list was
#    checked (`isinstance(..., list)` guard), so a response that simply omitted
#    the list was reported as a clean success. Observed on an 800-row transaction
#    document: `Transactions` was not in the response, status was COMPLETED, and
#    scalar accuracy was 1.000 — nothing anywhere said the table was gone.
#  * A list SHORTER than its declared `minItems` was checked only on the agentic
#    path. Simple mode is where the completeness cliff actually is (recall 1.000
#    through ~800 rows, then 0.199 @1,200 and 0.009 @3,200), and a truncated run
#    is *cheaper* than a complete one, so neither status nor cost reveals it.
# ---------------------------------------------------------------------------


def _svc_with_min_items(*, agentic: bool, min_items):
    cfg = IDPConfig(**{"extraction": {"agentic": {"enabled": agentic}}})
    svc = ExtractionService(config=cfg)
    svc._class_schema = {
        "type": "object",
        "properties": {
            "Transactions": {
                "type": "array",
                "items": {"type": "object"},
                "minItems": min_items,
            },
            "AccountNumber": {"type": "string"},
        },
    }
    svc._pending_extraction_model = "us.anthropic.claude-sonnet-5"
    return svc


def test_absent_list_field_is_flagged_not_just_empty_one():
    """The response omitted the list entirely — previously silent."""
    svc = _svc(agentic=False)
    issues = svc._build_extraction_issues(
        extracted_fields={"AccountNumber": "000123456789"},  # no PortfolioDetail key
        metadata={},
        section_id="1",
    )
    codes = [i.code for i in issues]
    assert "extraction_incomplete" in codes
    issue = next(i for i in issues if i.code == "extraction_incomplete")
    assert "PortfolioDetail" in issue.message


def test_null_list_field_is_flagged():
    svc = _svc(agentic=False)
    issues = svc._build_extraction_issues(
        extracted_fields={"PortfolioDetail": None, "AccountNumber": "x"},
        metadata={},
        section_id="1",
    )
    assert "extraction_incomplete" in [i.code for i in issues]


def test_short_list_violating_min_items_is_flagged_in_simple_mode():
    svc = _svc_with_min_items(agentic=False, min_items=100)
    issues = svc._build_extraction_issues(
        extracted_fields={"Transactions": [{"a": 1}] * 10, "AccountNumber": "x"},
        metadata={},
        section_id="1",
    )
    codes = [i.code for i in issues]
    assert "extraction_list_truncated" in codes
    issue = next(i for i in issues if i.code == "extraction_list_truncated")
    assert issue.severity == "warning"
    assert "10 of at least 100" in issue.message
    # Simple mode should point at the mode that shards large lists.
    assert "agentic" in issue.message.lower()
    assert issue.details["short_lists"] == [
        {"field": "Transactions", "rows": 10, "min_items": 100}
    ]


def test_short_list_flagged_in_agentic_mode_too_without_the_recommendation():
    svc = _svc_with_min_items(agentic=True, min_items=100)
    issues = svc._build_extraction_issues(
        extracted_fields={"Transactions": [{"a": 1}] * 10},
        metadata={},
        section_id="1",
    )
    issue = next(i for i in issues if i.code == "extraction_list_truncated")
    assert "recommended for documents this size" not in issue.message


def test_complete_list_is_not_flagged():
    svc = _svc_with_min_items(agentic=False, min_items=10)
    issues = svc._build_extraction_issues(
        extracted_fields={"Transactions": [{"a": 1}] * 10, "AccountNumber": "x"},
        metadata={},
        section_id="1",
    )
    assert "extraction_list_truncated" not in [i.code for i in issues]


def test_min_items_as_string_after_config_round_trip():
    svc = _svc_with_min_items(agentic=False, min_items="50")
    issues = svc._build_extraction_issues(
        extracted_fields={"Transactions": [{"a": 1}] * 5},
        metadata={},
        section_id="1",
    )
    assert "extraction_list_truncated" in [i.code for i in issues]


def test_no_min_items_means_no_truncation_claim():
    """Without a declared floor there is no unambiguous signal — stay quiet."""
    svc = _svc_with_min_items(agentic=False, min_items=None)
    issues = svc._build_extraction_issues(
        extracted_fields={"Transactions": [{"a": 1}] * 3},
        metadata={},
        section_id="1",
    )
    assert "extraction_list_truncated" not in [i.code for i in issues]


def test_unparseable_min_items_is_ignored_rather_than_raising():
    svc = _svc_with_min_items(agentic=False, min_items="lots")
    issues = svc._build_extraction_issues(
        extracted_fields={"Transactions": [{"a": 1}]},
        metadata={},
        section_id="1",
    )
    assert "extraction_list_truncated" not in [i.code for i in issues]


def test_empty_list_reports_incomplete_not_truncated():
    """An empty list is already covered by (1); don't double-report it."""
    svc = _svc_with_min_items(agentic=False, min_items=100)
    issues = svc._build_extraction_issues(
        extracted_fields={"Transactions": []},
        metadata={},
        section_id="1",
    )
    codes = [i.code for i in issues]
    assert "extraction_incomplete" in codes
    assert "extraction_list_truncated" not in codes
