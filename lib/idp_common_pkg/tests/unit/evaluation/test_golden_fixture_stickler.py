# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Golden-fixture regression tests for the Stickler evaluation pipeline.

These tests run against the real installed ``stickler-eval`` (no mocking of
``stickler.*``). They gate every scoring-behavior change: if the diff against
the golden JSON is intentional, regenerate via
``python scripts/regenerate_evaluation_goldens.py`` and review the diff.

Fixtures live in ``fixtures/*.input.json`` alongside this file. Section
evaluations write goldens under ``fixtures/section_goldens/``; the doc-split
fixture writes under ``fixtures/doc_split_goldens/``.
"""

import json
import warnings
from pathlib import Path
from typing import Any, Dict

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SECTION_GOLDEN_DIR = FIXTURE_DIR / "section_goldens"
DOC_SPLIT_GOLDEN_DIR = FIXTURE_DIR / "doc_split_goldens"


@pytest.fixture(autouse=True)
def _suppress_stickler_single_doc_warning():  # pyright: ignore[reportUnusedFunction]
    """Stickler emits a UserWarning about single-doc confidence metrics on every
    ``compare_with`` call — expected here, not a signal."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Single-document confidence metrics",
            category=UserWarning,
        )
        yield


def _section_result_to_dict(result) -> Dict[str, Any]:
    """Convert SectionEvaluationResult to a stable dict for golden comparison.

    Kept in sync with ``scripts/regenerate_evaluation_goldens.py`` — both call
    ``_convert_numpy_types`` and serialize the same fields in the same order.
    """
    from idp_common.evaluation.service import _convert_numpy_types

    return {
        "section_id": result.section_id,
        "document_class": result.document_class,
        "metrics": _convert_numpy_types(result.metrics),
        "attributes": [
            {
                "name": a.name,
                "expected": _convert_numpy_types(a.expected),
                "actual": _convert_numpy_types(a.actual),
                "matched": a.matched,
                "score": a.score,
                "reason": a.reason,
                "error_details": a.error_details,
                "evaluation_method": a.evaluation_method,
                "evaluation_threshold": a.evaluation_threshold,
                "comparator_type": a.comparator_type,
                "confidence": a.confidence,
                "confidence_threshold": a.confidence_threshold,
                "weight": a.weight,
                "field_comparison_details": _convert_numpy_types(
                    a.field_comparison_details
                ),
            }
            for a in result.attributes
        ],
        "stickler_comparison_result": _convert_numpy_types(
            result.stickler_comparison_result
        ),
    }


def _section_input_ids():
    return sorted(
        p.stem.rsplit(".", 1)[0]
        for p in FIXTURE_DIR.glob("*.input.json")
        if p.stem.rsplit(".", 1)[0] != "doc_split_metrics"
    )


@pytest.mark.unit
@pytest.mark.parametrize("fixture_name", _section_input_ids())
def test_section_evaluation_matches_golden(fixture_name):
    """Section evaluation must produce output byte-identical to the pinned golden.

    Regenerate goldens deliberately when a scoring change is intended:
        python scripts/regenerate_evaluation_goldens.py
    """
    from idp_common.evaluation.service import EvaluationService
    from idp_common.models import Section

    input_path = FIXTURE_DIR / f"{fixture_name}.input.json"
    golden_path = SECTION_GOLDEN_DIR / f"{fixture_name}.golden.json"

    assert golden_path.exists(), (
        f"Golden missing for {fixture_name}. Run "
        "`python scripts/regenerate_evaluation_goldens.py`."
    )

    spec = json.loads(input_path.read_text())
    svc = EvaluationService(region="us-east-1", config=spec["config"], max_workers=1)
    section = Section(
        section_id=spec["section_id"],
        classification=spec["classification"],
        page_ids=spec["page_ids"],
    )
    result = svc.evaluate_section(section, spec["expected"], spec["actual"])
    actual = _section_result_to_dict(result)
    expected = json.loads(golden_path.read_text())

    assert actual == expected, (
        f"Golden drift for {fixture_name}. If intentional, regenerate via "
        "`python scripts/regenerate_evaluation_goldens.py` and review the diff."
    )


@pytest.mark.unit
def test_doc_split_metrics_match_golden():
    """Upstream ``stickler.doc_split`` output must match the pinned golden.

    This test consumes ``DocSplitClassificationMetrics`` from stickler directly
    (not via the IDP fork) so the R8 fork deletion is a no-op for goldens.
    """
    from stickler.doc_split.doc_split_classification_metrics import (
        DocSplitClassificationMetrics,
    )

    from idp_common.evaluation.service import _convert_numpy_types

    input_path = FIXTURE_DIR / "doc_split_metrics.input.json"
    golden_path = DOC_SPLIT_GOLDEN_DIR / "doc_split_metrics.golden.json"
    assert golden_path.exists(), (
        "Golden missing for doc_split_metrics. Run "
        "`python scripts/regenerate_evaluation_goldens.py`."
    )

    spec = json.loads(input_path.read_text())
    calc = DocSplitClassificationMetrics()

    for section in spec["ground_truth_sections"]:
        section_id = section.get("section_id", f"gt_{len(calc.sections_gt)}")
        doc_class = calc._get_document_class(section)
        page_indices = calc._get_page_indices(section)
        calc.sections_gt.append(
            {
                "section_id": section_id,
                "document_class": doc_class,
                "page_indices": page_indices,
            }
        )
        for page_idx in page_indices:
            calc.page_classifications_gt[page_idx] = doc_class

    for section in spec["predicted_sections"]:
        section_id = section.get("section_id", f"pred_{len(calc.sections_pred)}")
        doc_class = calc._get_document_class(section)
        page_indices = calc._get_page_indices(section)
        calc.sections_pred.append(
            {
                "section_id": section_id,
                "document_class": doc_class,
                "page_indices": page_indices,
            }
        )
        for page_idx in page_indices:
            calc.page_classifications_pred[page_idx] = doc_class

    actual = _convert_numpy_types(calc.calculate_all_metrics())
    expected = json.loads(golden_path.read_text())
    assert actual == expected, (
        "Doc-split golden drift. If intentional, regenerate via "
        "`python scripts/regenerate_evaluation_goldens.py`."
    )


@pytest.mark.unit
@pytest.mark.parametrize("fixture_name", _section_input_ids())
def test_section_attribute_matched_agrees_with_drilldown_rows(fixture_name):
    """Consistency invariant: every attribute's ``matched`` must equal
    "no red drilldown row anywhere under this attribute's subtree." This is
    the property that eliminates the parent-vs-children contradiction
    (issue #625) — parent ✓ can never coexist with a ✗ leaf visible in the
    same section report.

    Reads Stickler's row-level ``field_comparisons`` (the same rows the UI
    drilldown displays) and asserts the derived attribute verdict matches
    the "all rows match=True" property. Two Stickler rollup nodes that had
    been consulted historically — ``cm.overall`` (item-level, hides leaves
    inside kept items) and ``cm.aggregate`` (leaf-level of matched items,
    drops rejected items) — each miss one direction of failure, so neither
    can be the invariant source.
    """
    from idp_common.evaluation.service import EvaluationService
    from idp_common.models import Section

    input_path = FIXTURE_DIR / f"{fixture_name}.input.json"
    spec = json.loads(input_path.read_text())
    svc = EvaluationService(region="us-east-1", config=spec["config"], max_workers=1)
    section = Section(
        section_id=spec["section_id"],
        classification=spec["classification"],
        page_ids=spec["page_ids"],
    )
    # Import the SAME root-extractor production uses — a duplicate copy here
    # can't catch a bug in that extractor (finding 8 from #625 adversarial
    # review).
    from idp_common.evaluation.contract import row_root_attribute

    result = svc.evaluate_section(section, spec["expected"], spec["actual"])
    field_comparisons = (result.stickler_comparison_result or {}).get(
        "field_comparisons"
    ) or []

    def _rows_under(attr_name: str):
        for fc in field_comparisons:
            if row_root_attribute(fc) == attr_name:
                yield fc

    for attr in result.attributes:
        my_rows = list(_rows_under(attr.name))
        if my_rows:
            # Use the SAME predicate production uses so a future refactor
            # of ``_is_match_true`` is caught by these tests too.
            from idp_common.evaluation.contract import _is_match_true

            expected_matched = all(_is_match_true(fc.get("match")) for fc in my_rows)
        else:
            # No rows for this attribute — production ``results.py`` falls
            # back to ``score >= threshold`` (list fields against the
            # Hungarian ``match_threshold``, scalars against the field's
            # ``applied_threshold``), so there is no drilldown to derive an
            # expectation from. Skip; the golden byte-comparison already
            # pins that rare case.
            continue
        assert attr.matched == expected_matched, (
            f"{fixture_name}.{attr.name}: IDP matched={attr.matched} vs "
            f"drilldown expected={expected_matched} "
            f"(red rows: {[fc.get('expected_key') or fc.get('field_path') for fc in my_rows if fc.get('match') is False]})"
        )


@pytest.mark.unit
def test_graded_packet_metrics_computed_from_section_dicts():
    """R14: ``compute_graded_packet_metrics`` builds page-level rows from the
    same section dicts the exact-match calculator uses and returns
    ``final_score`` / ``clustering_score`` / ``v_measure`` / ``rand_index`` /
    ``avg_ordering_score``. Exercised against the doc-split fixture to keep
    one input driving all doc-split-related tests."""
    from stickler.doc_split.doc_split_classification_metrics import (
        DocSplitClassificationMetrics,
    )

    from idp_common.evaluation.stickler_backend import compute_graded_packet_metrics

    spec = json.loads((FIXTURE_DIR / "doc_split_metrics.input.json").read_text())
    calc = DocSplitClassificationMetrics()
    calc.load_sections(
        ground_truth_sections=spec["ground_truth_sections"],
        predicted_sections=spec["predicted_sections"],
    )
    graded = compute_graded_packet_metrics(calc.sections_gt, calc.sections_pred)
    assert graded is not None
    for key in (
        "final_score",
        "clustering_score",
        "v_measure",
        "rand_index",
        "avg_ordering_score",
    ):
        v = graded.get(key)
        assert isinstance(v, (int, float))
        assert 0.0 <= v <= 1.0


@pytest.mark.unit
@pytest.mark.parametrize("fixture_name", _section_input_ids())
def test_document_evaluation_result_renders_to_markdown(fixture_name):
    """``DocumentEvaluationResult.to_markdown()`` must not raise on section
    metrics that carry non-numeric internal state (e.g. ``_stickler_counts``,
    a nested dict added in R3 for the document-level rollup).

    Regression guard for a production failure — after Phase 4 landed, the
    aggregation path crashed in the markdown renderer with
    ``TypeError: unsupported format string passed to dict.__format__``
    because ``metrics.items()`` iterated over the nested-dict counts and
    fed them to ``{value:.4f}``. Any future internal metric that isn't a
    numeric scalar should be silently skipped by ``to_markdown`` rather
    than fail the whole document evaluation.
    """
    from idp_common.evaluation.models import DocumentEvaluationResult
    from idp_common.evaluation.service import EvaluationService
    from idp_common.models import Section

    input_path = FIXTURE_DIR / f"{fixture_name}.input.json"
    spec = json.loads(input_path.read_text())
    svc = EvaluationService(region="us-east-1", config=spec["config"], max_workers=1)
    section = Section(
        section_id=spec["section_id"],
        classification=spec["classification"],
        page_ids=spec["page_ids"],
    )
    section_result = svc.evaluate_section(section, spec["expected"], spec["actual"])

    doc_result = DocumentEvaluationResult(
        document_id=f"test-{fixture_name}",
        section_results=[section_result],
        overall_metrics={"precision": 1.0, "recall": 1.0, "f1_score": 1.0},
    )

    # Must not raise. Result must be a non-empty markdown string.
    md = doc_result.to_markdown()
    assert isinstance(md, str)
    assert len(md) > 0
    # And it must NOT leak the raw dict repr of _stickler_counts into the
    # rendered output (which would happen if some code path str()'d the
    # dict instead of skipping it).
    assert "_stickler_counts" not in md


@pytest.mark.unit
def test_section_goldens_and_inputs_are_paired():
    """Every ``*.input.json`` must have a matching golden — no orphan fixtures."""
    input_names = {
        p.stem.rsplit(".", 1)[0]
        for p in FIXTURE_DIR.glob("*.input.json")
        if p.stem.rsplit(".", 1)[0] != "doc_split_metrics"
    }
    golden_names = {
        p.stem.rsplit(".", 1)[0] for p in SECTION_GOLDEN_DIR.glob("*.golden.json")
    }
    assert input_names == golden_names, (
        f"Input/golden mismatch. Inputs only: {input_names - golden_names}; "
        f"Goldens only: {golden_names - input_names}. Regenerate: "
        "`python scripts/regenerate_evaluation_goldens.py`."
    )
