# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Models for document evaluation.

This module provides data models for evaluation results and comparison methods.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from idp_common.evaluation.contract import _is_match_true


class EvaluationMethod(Enum):
    """Evaluation method types for different field comparison approaches."""

    EXACT = "EXACT"  # Exact string match after stripping punctuation and whitespace
    NUMERIC_EXACT = (
        "NUMERIC_EXACT"  # Exact numeric match, ±tolerance via evaluation-threshold
    )
    SEMANTIC = "SEMANTIC"  # Semantic similarity comparison using embeddings
    HUNGARIAN = "HUNGARIAN"  # Bipartite matching for lists of values
    FUZZY = "FUZZY"  # Fuzzy string matching
    LEVENSHTEIN = "LEVENSHTEIN"  # Levenshtein-distance-based string comparison
    DATE = "DATE"  # Semantic date comparison (format-insensitive, ranges)
    LLM = "LLM"  # LLM-based comparison using Bedrock models


@dataclass
class EvaluationAttribute:
    """Configuration for a single attribute to be evaluated."""

    name: str
    description: str
    evaluation_method: EvaluationMethod = EvaluationMethod.EXACT
    evaluation_threshold: float = 0.8  # Used for SEMANTIC, and FUZZY methods
    comparator_type: Optional[str] = None  # Used for HUNGARIAN method


@dataclass
class AttributeEvaluationResult:
    """Result of evaluation for a single attribute."""

    name: str
    expected: Any
    actual: Any
    matched: bool
    score: float = 1.0  # Score between 0 and 1 for fuzzy matching methods
    reason: Optional[str] = None  # Explanation from LLM evaluation
    error_details: Optional[str] = None
    evaluation_method: str = "EXACT"
    evaluation_threshold: Optional[float] = None
    comparator_type: Optional[str] = None  # Used for HUNGARIAN methods
    confidence: Optional[float] = (
        None  # Confidence score from assessment for actual values
    )
    confidence_threshold: Optional[float] = (
        None  # Confidence threshold from assessment for actual values
    )
    weight: Optional[float] = (
        None  # Field importance weight from Stickler (business criticality)
    )
    field_comparison_details: Optional[List[Dict[str, Any]]] = (
        None  # Detailed field-by-field comparison from sticker-eval v0.1.4+
    )


@dataclass
class SectionEvaluationResult:
    """Result of evaluation for a document section."""

    section_id: str
    document_class: str
    attributes: List[AttributeEvaluationResult]
    # ``Dict[str, Any]`` because the runtime dict carries nested dicts
    # (``_stickler_counts``), booleans (``evaluation_failed``), and
    # strings (``failure_type``) alongside the derived-metric floats.
    metrics: Dict[str, Any] = field(default_factory=dict)
    stickler_comparison_result: Optional[Dict[str, Any]] = (
        None  # Raw Stickler result for bulk aggregation
    )

    def get_attribute_results(self) -> Dict[str, AttributeEvaluationResult]:
        """Get results indexed by attribute name."""
        return {attr.name: attr for attr in self.attributes}


@dataclass
class DocSplitMetrics:
    """Document split classification accuracy metrics."""

    page_level_accuracy: float
    split_accuracy_without_order: float
    split_accuracy_with_order: float
    total_pages: int
    total_splits: int
    correctly_classified_pages: int
    correctly_split_without_order: int
    correctly_split_with_order: int
    page_details: List[Dict[str, Any]] = field(default_factory=list)
    section_details_without_order: List[Dict[str, Any]] = field(default_factory=list)
    section_details_with_order: List[Dict[str, Any]] = field(default_factory=list)
    predicted_sections: List[Dict[str, Any]] = field(
        default_factory=list
    )  # All predicted sections for unmatched display
    errors: List[str] = field(default_factory=list)
    # Graded packet metrics (R14, from stickler.doc_split.packet_evaluation_metrics.
    # evaluate_packet — V-measure / Rand index / ordering score). Optional
    # because the exact-match counters above already have meaning on their own
    # and older results.json files won't carry these fields. All values are
    # in [0.0, 1.0] where 1.0 is perfect.
    final_score: Optional[float] = None
    clustering_score: Optional[float] = None
    v_measure: Optional[float] = None
    rand_index: Optional[float] = None
    avg_ordering_score: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "page_level_accuracy": self.page_level_accuracy,
            "split_accuracy_without_order": self.split_accuracy_without_order,
            "split_accuracy_with_order": self.split_accuracy_with_order,
            "total_pages": self.total_pages,
            "total_splits": self.total_splits,
            "correctly_classified_pages": self.correctly_classified_pages,
            "correctly_split_without_order": self.correctly_split_without_order,
            "correctly_split_with_order": self.correctly_split_with_order,
            "page_details": self.page_details,
            "section_details_without_order": self.section_details_without_order,
            "section_details_with_order": self.section_details_with_order,
            "predicted_sections": self.predicted_sections,
            "errors": self.errors,
            "final_score": self.final_score,
            "clustering_score": self.clustering_score,
            "v_measure": self.v_measure,
            "rand_index": self.rand_index,
            "avg_ordering_score": self.avg_ordering_score,
        }


@dataclass
class DocumentEvaluationResult:
    """Comprehensive evaluation result for a document."""

    document_id: str
    section_results: List[SectionEvaluationResult]
    overall_metrics: Dict[str, float] = field(default_factory=dict)
    execution_time: float = 0.0
    output_uri: Optional[str] = None
    doc_split_metrics: Optional[DocSplitMetrics] = None

    # Sections whose class was marked x-aws-idp-exclude-from-processing=true.
    # These sections are NOT included in accuracy metrics but ARE listed in
    # the markdown report with an "Excluded" annotation, so users can see
    # what was intentionally skipped.
    excluded_sections: List[Dict[str, Any]] = field(default_factory=list)

    def _format_nested_comparisons(
        self, field_comparisons: List[Dict[str, Any]], indent_level: int = 0
    ) -> str:
        """
        Format nested field comparison details as HTML.

        Shows both expected and actual paths to reveal Hungarian matching pairs
        where list indices may differ.

        Args:
            field_comparisons: List of field comparison dictionaries from Stickler
            indent_level: Current indentation level for nested fields

        Returns:
            HTML string with formatted comparison details
        """
        if not field_comparisons:
            return "<p><em>No detailed comparisons available</em></p>"

        html_parts = []
        html_parts.append(
            '<table class="nested-comparison-table" style="width: 100%; border-collapse: collapse; font-size: 0.9em; margin: 10px 0;">'
        )
        html_parts.append(
            "<thead><tr style='background: #f0f0f0;'>"
            "<th style='padding: 8px; border: 1px solid #ddd; text-align: left;'>Expected Path</th>"
            "<th style='padding: 8px; border: 1px solid #ddd; text-align: left;'>Expected Value</th>"
            "<th style='padding: 8px; border: 1px solid #ddd; text-align: left;'>Actual Path</th>"
            "<th style='padding: 8px; border: 1px solid #ddd; text-align: left;'>Actual Value</th>"
            "<th style='padding: 8px; border: 1px solid #ddd; text-align: center;'>Match</th>"
            "<th style='padding: 8px; border: 1px solid #ddd; text-align: center;'>Score</th>"
            "<th style='padding: 8px; border: 1px solid #ddd; text-align: center;'>Weight</th>"
            "<th style='padding: 8px; border: 1px solid #ddd; text-align: left;'>Method</th>"
            "<th style='padding: 8px; border: 1px solid #ddd; text-align: left;'>Reason</th>"
            "</tr></thead><tbody>"
        )

        for fc in field_comparisons:
            expected_key = fc.get("expected_key", "N/A")
            actual_key = fc.get("actual_key", "N/A")
            expected_val = str(fc.get("expected_value", ""))[:100]
            actual_val = str(fc.get("actual_value", ""))[:100]
            # Use the SHARED predicate so the drilldown's green/red painting
            # agrees with section counts and per-attribute verdict on the
            # same row. Raw ``bool(fc.get("match", False))`` would paint the
            # string ``"false"`` or a numeric score like ``0.5`` GREEN
            # while the row was counted as a failure — the exact parent-
            # vs-drilldown contradiction #625 exists to eliminate.
            match = _is_match_true(fc.get("match"))
            score = fc.get("score", 0.0)
            weight = fc.get("weight")
            # Per-field comparison method (annotated in service.py); fall back to
            # empty when comparing results produced before this was surfaced.
            method = fc.get("evaluation_method", "")
            reason = fc.get("reason", "")

            # Escape HTML special characters
            expected_val = (
                expected_val.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            actual_val = (
                actual_val.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            reason_escaped = (
                str(reason)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                if reason
                else ""
            )
            method_escaped = (
                str(method)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                if method
                else ""
            )
            weight_str = f"{weight:.2f}" if weight is not None else "1.00"

            # Style based on match status
            row_style = "background: #d4edda;" if match else "background: #f8d7da;"
            match_symbol = "✅" if match else "❌"

            html_parts.append(
                f"<tr style='{row_style}'>"
                f"<td style='padding: 8px; border: 1px solid #ddd;'><code>{expected_key}</code></td>"
                f"<td style='padding: 8px; border: 1px solid #ddd;'>{expected_val}</td>"
                f"<td style='padding: 8px; border: 1px solid #ddd;'><code>{actual_key}</code></td>"
                f"<td style='padding: 8px; border: 1px solid #ddd;'>{actual_val}</td>"
                f"<td style='padding: 8px; border: 1px solid #ddd; text-align: center;'>{match_symbol}</td>"
                f"<td style='padding: 8px; border: 1px solid #ddd; text-align: center;'>{score:.3f}</td>"
                f"<td style='padding: 8px; border: 1px solid #ddd; text-align: center;'>{weight_str}</td>"
                f"<td style='padding: 8px; border: 1px solid #ddd; font-size: 0.85em;'>{method_escaped}</td>"
                f"<td style='padding: 8px; border: 1px solid #ddd; font-size: 0.85em;'>{reason_escaped}</td>"
                "</tr>"
            )

        html_parts.append("</tbody></table>")
        return "".join(html_parts)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        result = {
            "document_id": self.document_id,
            "overall_metrics": self.overall_metrics,
            "execution_time": self.execution_time,
            "output_uri": self.output_uri,
            "section_results": [
                {
                    "section_id": sr.section_id,
                    "document_class": sr.document_class,
                    "metrics": sr.metrics,
                    "stickler_comparison_result": sr.stickler_comparison_result,  # Include for bulk aggregation
                    "attributes": [
                        {
                            "name": ar.name,
                            "expected": ar.expected,
                            "actual": ar.actual,
                            "matched": ar.matched,
                            "score": ar.score,
                            "reason": ar.reason,
                            "error_details": ar.error_details,
                            "evaluation_method": ar.evaluation_method,
                            "evaluation_threshold": ar.evaluation_threshold,
                            "comparator_type": ar.comparator_type,
                            "confidence": ar.confidence,
                            "confidence_threshold": ar.confidence_threshold,
                            "weight": ar.weight,
                            "field_comparison_details": ar.field_comparison_details,
                        }
                        for ar in sr.attributes
                    ],
                }
                for sr in self.section_results
            ],
        }

        # Add doc_split_metrics if available
        if self.doc_split_metrics:
            result["doc_split_metrics"] = self.doc_split_metrics.to_dict()

        # Add excluded sections (not evaluated, but surfaced for transparency)
        if self.excluded_sections:
            result["excluded_sections"] = list(self.excluded_sections)

        # NOTE: ``stickler_result_version`` is NOT stamped here. Stamping in
        # ``to_dict()`` would silently upgrade the version on any round-trip
        # (load a historical v1.0 ``results.json``, wrap it in a
        # ``DocumentEvaluationResult``, re-serialize) — defeating the
        # drift-detection soft gate the stamp exists to enable. The single
        # writer path in ``service.py`` stamps at write time; new callers
        # must do the same explicitly rather than inheriting it as a
        # side-effect of ``to_dict()``.

        return result

    @staticmethod
    def _failure_remediation(sr: "SectionEvaluationResult") -> List[str]:
        """Return "How to fix" lines appropriate to *this* section's failure.

        Keyed on ``metrics["failure_type"]``, which ``evaluate_section`` sets
        alongside ``evaluation_failed``. Results written before that field
        existed carry no type; those get no remediation list rather than a
        guessed one — the failure reason above already states the cause, and
        advice for the wrong cause is worse than no advice.
        """
        failure_type = sr.metrics.get("failure_type")
        steps: List[str] = []

        if failure_type == "missing_schema_configuration":
            steps = [
                f"1. Add a configuration for '{sr.document_class}' in your `evaluation` config YAML",
                "2. Ensure the document class name matches exactly (case-insensitive)",
                "3. Or provide baseline/expected data when calling evaluate_document() to enable auto-generation",
            ]
        elif failure_type == "empty_nested_object":
            steps = [
                "1. Add at least one property to the nested object named above, or remove it from the schema",
                "2. Re-run the evaluation",
            ]
        elif failure_type == "extraction_parsing_failed":
            steps = [
                "1. Open the section's extraction output — the model's response was not parseable JSON",
                "2. Check whether the response was truncated (raise `max_tokens`) or wrapped in commentary",
                "3. Re-run extraction for this document, then re-evaluate",
            ]
        elif failure_type == "baseline_data_validation_error":
            steps = [
                "1. Compare the baseline (expected) values against the class schema — the types disagree",
                "2. Fix the baseline data (or widen the schema field type) and re-run the evaluation",
            ]
        elif failure_type == "schema_configuration_error":
            steps = [
                f"1. Review the `evaluation` schema for '{sr.document_class}' against the error above",
                "2. Re-run the evaluation once the schema is valid",
            ]

        if not steps:
            return []
        return ["**How to fix:**", *steps, ""]

    def to_markdown(self) -> str:
        """Convert evaluation results to clean, portable markdown format."""
        sections = []

        # Add main title
        sections.append("# Evaluation Report")
        sections.append("")

        # Add marker for React UI to detect evaluation reports with enhanced data
        sections.append("<!-- eval-report-v2 -->")
        sections.append("")

        # Get overall stats for visual summary
        total_attributes = 0
        matched_attributes = 0
        for sr in self.section_results:
            for attr in sr.attributes:
                total_attributes += 1
                if attr.matched:
                    matched_attributes += 1

        match_rate = (
            matched_attributes / total_attributes if total_attributes > 0 else 0
        )
        precision = self.overall_metrics.get("precision", 0)
        recall = self.overall_metrics.get("recall", 0)
        f1_score = self.overall_metrics.get("f1_score", 0)

        # Create unified summary with both extraction and doc split metrics
        sections.append("## Summary")
        sections.append("")

        # Add doc split metrics FIRST if available
        if self.doc_split_metrics:

            def get_indicator(accuracy: float) -> str:
                if accuracy >= 0.9:
                    return "🟢"
                elif accuracy >= 0.7:
                    return "🟡"
                elif accuracy >= 0.5:
                    return "🟠"
                else:
                    return "🔴"

            page_acc = self.doc_split_metrics.page_level_accuracy
            split_no_ord_acc = self.doc_split_metrics.split_accuracy_without_order
            split_ord_acc = self.doc_split_metrics.split_accuracy_with_order

            # Create progress bars
            page_percent = int(page_acc * 100)
            page_progress = f"[{'█' * (page_percent // 5)}{'░' * (20 - page_percent // 5)}] {page_percent}%"

            split_no_ord_percent = int(split_no_ord_acc * 100)
            split_no_ord_progress = f"[{'█' * (split_no_ord_percent // 5)}{'░' * (20 - split_no_ord_percent // 5)}] {split_no_ord_percent}%"

            split_ord_percent = int(split_ord_acc * 100)
            split_ord_progress = f"[{'█' * (split_ord_percent // 5)}{'░' * (20 - split_ord_percent // 5)}] {split_ord_percent}%"

            sections.append("**Document Split Classification:**")
            sections.append(
                f"- **Page Level Accuracy**: {get_indicator(page_acc)} "
                f"{self.doc_split_metrics.correctly_classified_pages}/{self.doc_split_metrics.total_pages} pages "
                f"{page_progress}"
            )
            sections.append(
                f"- **Split Accuracy (Without Order)**: {get_indicator(split_no_ord_acc)} "
                f"{self.doc_split_metrics.correctly_split_without_order}/{self.doc_split_metrics.total_splits} sections "
                f"{split_no_ord_progress}"
            )
            sections.append(
                f"- **Split Accuracy (With Order)**: {get_indicator(split_ord_acc)} "
                f"{self.doc_split_metrics.correctly_split_with_order}/{self.doc_split_metrics.total_splits} sections "
                f"{split_ord_progress}"
            )
            sections.append("")

        # Then add extraction metrics
        sections.append("**Document Extraction:**")

        # Match rate indicator
        if match_rate >= 0.9:
            match_indicator = "🟢"
        elif match_rate >= 0.7:
            match_indicator = "🟡"
        elif match_rate >= 0.5:
            match_indicator = "🟠"
        else:
            match_indicator = "🔴"

        # F1 score indicator
        if f1_score >= 0.9:
            f1_indicator = "🟢"
        elif f1_score >= 0.7:
            f1_indicator = "🟡"
        elif f1_score >= 0.5:
            f1_indicator = "🟠"
        else:
            f1_indicator = "🔴"

        # Create a visual progress bar for match rate
        match_percent = int(match_rate * 100)
        progress_bar = f"[{'█' * (match_percent // 5)}{'░' * (20 - match_percent // 5)}] {match_percent}%"

        sections.append(
            f"- **Match Rate**: {match_indicator} {matched_attributes}/{total_attributes} attributes matched {progress_bar}"
        )
        sections.append(
            f"- **Precision**: {precision:.2f} | **Recall**: {recall:.2f} | **F1 Score**: {f1_indicator} {f1_score:.2f}"
        )

        # Add weighted overall score if available. When every section was a
        # scoring no-op (no extractable schema), the weighted score is None —
        # render as "N/A — Excluded" instead of a misleading 0.0000. The
        # ``None`` sentinel is authoritative: service.py sets
        # ``evaluation_excluded`` iff the score is None.
        weighted_score = self.overall_metrics.get("weighted_overall_score")
        if weighted_score is None:
            sections.append(
                "- **Weighted Overall Score**: ⚪ N/A — Excluded "
                "(no extractable schema for any section)"
            )
        else:
            if weighted_score >= 0.9:
                weighted_indicator = "🟢"
            elif weighted_score >= 0.7:
                weighted_indicator = "🟡"
            elif weighted_score >= 0.5:
                weighted_indicator = "🟠"
            else:
                weighted_indicator = "🔴"

            sections.append(
                f"- **Weighted Overall Score**: {weighted_indicator} {weighted_score:.4f} (Stickler's field-weighted aggregate)"
            )

        sections.append("")

        # Report excluded sections (intentionally skipped from evaluation).
        # These are surfaced here so users understand why section counts in
        # the Overall Metrics table may be lower than the number of sections
        # in the source document.
        if self.excluded_sections:
            sections.append("## Excluded Sections (Not Evaluated)")
            sections.append("")
            sections.append(
                "The following sections were skipped during evaluation and do "
                "**not** contribute to the accuracy metrics above. Sections are "
                "excluded either because their class was marked "
                "`x-aws-idp-exclude-from-processing: true` (whole-pipeline skip) "
                "or because the class has no extractable attributes defined in "
                "the evaluation schema (evaluation-only skip)."
            )
            sections.append("")
            sections.append("| Section | Classification | Exclusion Reason | Pages |")
            sections.append("| ------- | -------------- | ---------------- | ----- |")
            for es in self.excluded_sections:
                sid = es.get("section_id", "")
                cls = es.get("classification", "")
                reason = es.get("exclusion_reason") or "excluded"
                pages = ", ".join(str(p) for p in es.get("page_ids") or [])
                sections.append(f"| {sid} | {cls} | {reason} | {pages} |")
            sections.append("")

        # Add overall metrics with two separate tables
        sections.append("## Overall Metrics")
        sections.append("")

        # Helper function for rating
        def get_rating_for_metric(metric_name: str, value: float) -> str:
            if metric_name in [
                "precision",
                "recall",
                "f1_score",
                "accuracy",
                "weighted_overall_score",
                "page_level_accuracy",
                "split_accuracy_without_order",
                "split_accuracy_with_order",
            ]:
                if value >= 0.9:
                    return "🟢 Excellent"
                elif value >= 0.7:
                    return "🟡 Good"
                elif value >= 0.5:
                    return "🟠 Fair"
                else:
                    return "🔴 Poor"
            elif metric_name in ["false_alarm_rate", "false_discovery_rate"]:
                # For error metrics, lower is better
                if value <= 0.1:
                    return "🟢 Excellent"
                elif value <= 0.3:
                    return "🟡 Good"
                elif value <= 0.5:
                    return "🟠 Fair"
                else:
                    return "🔴 Poor"
            else:
                return ""  # No rating for other metrics

        # Add doc split metrics table first if available
        if self.doc_split_metrics:
            sections.append("### Document Split Classification Metrics")
            doc_split_table = (
                "| Metric | Value | Rating |\n| ------ | :----: | :----: |\n"
            )
            doc_split_table += f"| page_level_accuracy | {self.doc_split_metrics.page_level_accuracy:.4f} | {get_rating_for_metric('page_level_accuracy', self.doc_split_metrics.page_level_accuracy)} |\n"
            doc_split_table += f"| split_accuracy_without_order | {self.doc_split_metrics.split_accuracy_without_order:.4f} | {get_rating_for_metric('split_accuracy_without_order', self.doc_split_metrics.split_accuracy_without_order)} |\n"
            doc_split_table += f"| split_accuracy_with_order | {self.doc_split_metrics.split_accuracy_with_order:.4f} | {get_rating_for_metric('split_accuracy_with_order', self.doc_split_metrics.split_accuracy_with_order)} |\n"
            sections.append(doc_split_table)
            sections.append("")

        # Add extraction metrics table. ``overall_metrics`` can carry
        # non-numeric entries — ``weighted_overall_score`` is None when every
        # section was excluded, and companion flags like ``evaluation_excluded``
        # / ``exclusion_reason`` / ``skipped_section_count`` document *why*.
        # Render those as-is instead of trying to ``{value:.4f}`` them.
        sections.append("### Document Extraction Metrics")
        extraction_table = "| Metric | Value | Rating |\n| ------ | :----: | :----: |\n"
        for metric, value in self.overall_metrics.items():
            if value is None:
                extraction_table += f"| {metric} | N/A | ⚪ Excluded |\n"
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                indicator = get_rating_for_metric(metric, value)
                extraction_table += f"| {metric} | {value:.4f} | {indicator} |\n"
            else:
                extraction_table += f"| {metric} | {value} |  |\n"
        sections.append(extraction_table)
        sections.append("")

        # Add doc split analysis tables if available (right after Overall Metrics)
        if self.doc_split_metrics:
            # Combined Section split analysis
            sections.append("## 📑 Section Split Analysis")
            sections.append("")

            if (
                self.doc_split_metrics.section_details_without_order
                or self.doc_split_metrics.section_details_with_order
            ):
                sections.append(
                    "| Section Match | Page Order Match | Section ID | Expected Class | Expected Pages | Pred Class | Pred Pages | Matched Section |"
                )
                sections.append(
                    "| :-----------: | :--------------: | ---------- | -------------- | -------------- | ---------- | ---------- | --------------- |"
                )

                # Track matched predicted section IDs
                matched_pred_section_ids = set()

                # Get list of all predicted sections from service.py
                # We need to identify unmatched predicted sections
                # We'll collect matched IDs first, then show unmatched ones at the end

                # Combine data from both metrics for ground truth sections
                for idx, section_with_order in enumerate(
                    self.doc_split_metrics.section_details_with_order
                ):
                    # Get corresponding section from split_no_order
                    section_no_order = (
                        self.doc_split_metrics.section_details_without_order[idx]
                        if idx
                        < len(self.doc_split_metrics.section_details_without_order)
                        else None
                    )

                    # Section Match status (from split_no_order)
                    section_match = (
                        "✅"
                        if section_no_order and section_no_order["matched"]
                        else "❌"
                    )

                    # Page Order Match status (from split_with_order)
                    page_order_match = (
                        "✅" if section_with_order.get("order_matched", False) else "❌"
                    )

                    gt_pages = str(section_with_order["ground_truth_pages"])
                    pred_pages = (
                        str(section_with_order["predicted_pages"])
                        if section_with_order["predicted_pages"]
                        else "N/A"
                    )
                    matched_id = (
                        section_with_order["matched_section_id"]
                        if section_with_order["matched_section_id"]
                        else "N/A"
                    )

                    # Track matched predicted section IDs
                    if section_with_order["matched_section_id"]:
                        matched_pred_section_ids.add(
                            section_with_order["matched_section_id"]
                        )

                    sections.append(
                        f"| {section_match} | {page_order_match} | {section_with_order['section_id']} | "
                        f"{section_with_order['ground_truth_class']} | {gt_pages} | {section_with_order['predicted_class']} | "
                        f"{pred_pages} | {matched_id} |"
                    )

                # Add unmatched predicted sections
                for pred_section in self.doc_split_metrics.predicted_sections:
                    if pred_section["section_id"] not in matched_pred_section_ids:
                        pred_pages = str(pred_section["page_indices"])
                        sections.append(
                            f"| ❌ | ❌ | N/A | No Match | N/A | "
                            f"{pred_section['document_class']} | {pred_pages} | {pred_section['section_id']} |"
                        )
            else:
                sections.append("*No section data available*")

            sections.append("")

            # Add errors if any
            if self.doc_split_metrics.errors:
                sections.append("### ⚠️ Doc Split Errors")
                sections.append("")
                for error in self.doc_split_metrics.errors:
                    sections.append(f"- {error}")
                sections.append("")

        # Add header for extraction attribute evaluation section
        sections.append("## Extraction Attribute Evaluation")
        sections.append("")

        # Add section results
        for sr in self.section_results:
            sections.append(f"### Section: {sr.section_id} ({sr.document_class})")

            # Check if this section had an evaluation failure
            if sr.metrics.get("evaluation_failed", False):
                failure_reason = (
                    sr.attributes[0].reason
                    if sr.attributes and sr.attributes[0].reason
                    else ""
                )
                sections.append("")
                sections.append("⚠️ **EVALUATION FAILED**")
                sections.append("")
                # State the actual cause. This block used to assert that no
                # configuration was found for the class, and print the matching
                # "how to fix" list, for *every* failure — so a parsing error or
                # a baseline type mismatch was described as a missing config,
                # sending users to fix something that was not wrong.
                sections.append(
                    failure_reason
                    or f"This section could not be evaluated for document class: **{sr.document_class}**"
                )
                sections.append("")
                sections.append("**Impact:**")
                sections.append(
                    "- This section contributes **zero accuracy** to all metrics (precision, recall, F1, etc.)"
                )
                sections.append(
                    "- The failure is reflected in document-level aggregate metrics"
                )
                sections.append("")

                for line in self._failure_remediation(sr):
                    sections.append(line)

                # Still show the metrics (all zeros) for transparency
                sections.append("### Metrics (Failure State)")
                sections.append("")
                # Say what the zeros are. They are placeholders for a section
                # that was never scored, not measurements of a bad extraction —
                # read as accuracy they look alarming next to a healthy
                # document-level score.
                sections.append(
                    "> These zeros mean *not scored*, not *scored zero* — this section "
                    "was never evaluated, so there is nothing measured to report. They "
                    "do count against the document-level aggregates above."
                )
                sections.append("")
                metrics_table = (
                    "| Metric | Value | Rating |\n| ------ | :----: | :----: |\n"
                )
                for metric, value in sr.metrics.items():
                    if metric in ("evaluation_failed", "failure_type"):
                        continue  # Skip internal flags in the table
                    formatted_value = (
                        f"{value:.4f}"
                        if isinstance(value, (int, float))
                        else str(value)
                    )
                    metrics_table += f"| {metric} | {formatted_value} | ❌ Failed |\n"
                sections.append(metrics_table)
                sections.append("")
                continue  # Skip attribute display for failed sections

            # Note any fields excluded from scoring due to validation errors, so
            # reduced coverage is explicit rather than hidden in the attribute table.
            skipped_count = sr.metrics.get("skipped_field_count", 0)
            if skipped_count:
                sections.append(
                    f"> ⚠️ **{skipped_count} field(s) were excluded from scoring** "
                    f"because they could not be validated against the schema. "
                    f"The remaining fields were evaluated normally. See the "
                    f"`__SKIPPED__` rows below for details."
                )
                sections.append("")

            # Section metrics with enhanced formatting (normal case)
            sections.append("#### Metrics")
            metrics_table = (
                "| Metric | Value | Rating |\n| ------ | :----: | :----: |\n"
            )
            for metric, value in sr.metrics.items():
                # Skip the evaluation_failed flag and the skipped-field count
                # (rendered as a note above, not a scored metric)
                if metric in ("evaluation_failed", "skipped_field_count"):
                    continue
                # Section metrics can carry non-numeric internal state (e.g.
                # ``_stickler_counts``, a nested dict of raw Stickler tp/fa/fd/
                # fp/tn/fn used for the document-level rollup). Skip anything
                # that can't be formatted as ``{:.4f}`` so a new internal
                # metric doesn't crash ``to_markdown``.
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue

                # Add a visual indicator based on metric value
                if metric in [
                    "precision",
                    "recall",
                    "f1_score",
                    "accuracy",
                    "weighted_overall_score",
                ]:
                    if value >= 0.9:
                        indicator = "🟢 Excellent"
                    elif value >= 0.7:
                        indicator = "🟡 Good"
                    elif value >= 0.5:
                        indicator = "🟠 Fair"
                    else:
                        indicator = "🔴 Poor"
                elif metric in ["false_alarm_rate", "false_discovery_rate"]:
                    # For error metrics, lower is better
                    if value <= 0.1:
                        indicator = "🟢 Excellent"
                    elif value <= 0.3:
                        indicator = "🟡 Good"
                    elif value <= 0.5:
                        indicator = "🟠 Fair"
                    else:
                        indicator = "🔴 Poor"
                else:
                    indicator = ""  # No rating for other metrics

                metrics_table += f"| {metric} | {value:.4f} | {indicator} |\n"
            sections.append(metrics_table)
            sections.append("")

            # Attribute results with enhanced nested details
            sections.append("#### Attributes")

            # Use HTML table for better control over row classes and expandable details
            sections.append('<table style="width: 100%; border-collapse: collapse;">')
            sections.append(
                '<thead><tr style="background: #f0f0f0;">'
                '<th style="padding: 8px; border: 1px solid #ddd; text-align: center;">Status</th>'
                '<th style="padding: 8px; border: 1px solid #ddd;">Attribute</th>'
                '<th style="padding: 8px; border: 1px solid #ddd;">Expected</th>'
                '<th style="padding: 8px; border: 1px solid #ddd;">Actual</th>'
                '<th style="padding: 8px; border: 1px solid #ddd; text-align: center;">Confidence</th>'
                '<th style="padding: 8px; border: 1px solid #ddd; text-align: center;">Conf. Threshold</th>'
                '<th style="padding: 8px; border: 1px solid #ddd; text-align: center;">Score</th>'
                '<th style="padding: 8px; border: 1px solid #ddd; text-align: center;">Weight</th>'
                '<th style="padding: 8px; border: 1px solid #ddd;">Method</th>'
                '<th style="padding: 8px; border: 1px solid #ddd;">Reason</th>'
                "</tr></thead><tbody>"
            )

            for ar in sr.attributes:
                # Determine row class for filtering
                row_class = "matched-row" if ar.matched else "unmatched-row"

                # Status symbol
                status_symbol = "✅" if ar.matched else "❌"

                # Truncate long values for display in table
                expected_display = str(ar.expected)[:100]
                if len(str(ar.expected)) > 100:
                    expected_display += "..."
                actual_display = str(ar.actual)[:100]
                if len(str(ar.actual)) > 100:
                    actual_display += "..."

                # Escape HTML in values
                expected_display = (
                    expected_display.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .replace("\n", " ")
                )
                actual_display = (
                    actual_display.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .replace("\n", " ")
                )
                reason = (
                    str(ar.reason)
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .replace("\n", " ")
                    if ar.reason
                    else ""
                )

                # Format confidence values
                confidence_str = (
                    f"{ar.confidence:.2f}" if ar.confidence is not None else "N/A"
                )
                threshold_str = (
                    f"{ar.confidence_threshold:.2f}"
                    if ar.confidence_threshold is not None
                    else "N/A"
                )

                # Format weight value
                weight_str = f"{ar.weight:.2f}" if ar.weight is not None else "1.00"

                # Check if aggregate score (nested object or array)
                has_nested_details = (
                    ar.field_comparison_details and len(ar.field_comparison_details) > 1
                )
                if has_nested_details:
                    score_display = f'<span class="aggregate-score">{ar.score:.2f}</span> <em>(aggregate)</em>'
                else:
                    score_display = f"{ar.score:.2f}"

                # Create table row
                sections.append(f'<tr class="{row_class}">')
                sections.append(
                    f'<td style="padding: 8px; border: 1px solid #ddd; text-align: center;">{status_symbol}</td>'
                )
                sections.append(
                    f'<td style="padding: 8px; border: 1px solid #ddd;"><strong>{ar.name}</strong></td>'
                )
                sections.append(
                    f'<td style="padding: 8px; border: 1px solid #ddd;">{expected_display}</td>'
                )
                sections.append(
                    f'<td style="padding: 8px; border: 1px solid #ddd;">{actual_display}</td>'
                )
                sections.append(
                    f'<td style="padding: 8px; border: 1px solid #ddd; text-align: center;">{confidence_str}</td>'
                )
                sections.append(
                    f'<td style="padding: 8px; border: 1px solid #ddd; text-align: center;">{threshold_str}</td>'
                )
                sections.append(
                    f'<td style="padding: 8px; border: 1px solid #ddd; text-align: center;">{score_display}</td>'
                )
                sections.append(
                    f'<td style="padding: 8px; border: 1px solid #ddd; text-align: center;">{weight_str}</td>'
                )
                sections.append(
                    f'<td style="padding: 8px; border: 1px solid #ddd;">{ar.evaluation_method}</td>'
                )
                sections.append(
                    f'<td style="padding: 8px; border: 1px solid #ddd;">{reason}</td>'
                )
                sections.append("</tr>")

                # Add expandable nested details if available
                if has_nested_details:
                    sections.append(f'<tr class="{row_class}">')
                    sections.append(
                        '<td colspan="10" style="padding: 0; border: 1px solid #ddd;">'
                    )
                    sections.append(
                        f'<details><summary style="padding: 8px;"><strong>🔍 View {len(ar.field_comparison_details)} Nested Field Comparisons</strong></summary>'
                    )
                    sections.append('<div style="padding: 10px;">')
                    sections.append(
                        self._format_nested_comparisons(ar.field_comparison_details)
                    )
                    sections.append("</div></details>")
                    sections.append("</td></tr>")

            sections.append("</tbody></table>")
            sections.append("")

        # Add execution time
        sections.append(f"Execution time: {self.execution_time:.2f} seconds")

        # Add evaluation methods explanation
        sections.append("")
        sections.append("## Evaluation Methods Used")
        sections.append("")
        sections.append(
            "This evaluation uses Stickler-based comparison with the following methods:"
        )
        sections.append("")
        sections.append("### Field-Level Comparison Methods")
        sections.append("")
        sections.append("1. **EXACT** - Exact string match (case-sensitive)")
        sections.append(
            "   - Use for: IDs, codes, exact text requiring character-for-character match"
        )
        sections.append("")
        sections.append(
            "2. **NUMERIC_EXACT** - Numeric comparison with configurable tolerance"
        )
        sections.append("   - Tolerance specified via `x-aws-idp-evaluation-threshold`")
        sections.append("   - Use for: Monetary amounts, percentages, numeric values")
        sections.append("")
        sections.append("3. **FUZZY** - Fuzzy string matching using similarity metrics")
        sections.append(
            "   - Threshold specified via `x-aws-idp-evaluation-threshold` (0.0-1.0)"
        )
        sections.append("   - Use for: Names, addresses, text with minor variations")
        sections.append("")
        sections.append(
            "4. **LEVENSHTEIN** - Levenshtein distance-based string comparison"
        )
        sections.append("   - Configurable threshold for acceptable edit distance")
        sections.append(
            "   - Use for: Similar to FUZZY but using specific edit distance algorithm"
        )
        sections.append("")
        sections.append("5. **SEMANTIC** - Semantic similarity using embedding models")
        sections.append("   - Threshold specified via `x-aws-idp-evaluation-threshold`")
        sections.append(
            "   - Use for: Text where meaning matters more than exact wording"
        )
        sections.append("")
        sections.append("6. **DATE** - Format-insensitive date / date-range comparison")
        sections.append(
            "   - Compares dates by resolved value, not surface form"
            " (e.g. `2024-01-05` == `January 5, 2024`)"
        )
        sections.append(
            "   - Optional per-field config via `x-aws-idp-evaluation-method-config`:"
            " `dayfirst`, `range_mode`, `tolerance`"
        )
        sections.append("   - Use for: Dates in mixed formats, date ranges, birthdates")
        sections.append("")
        sections.append(
            "7. **LLM** - Advanced semantic evaluation using **AWS Bedrock LLMs**"
        )
        sections.append("   - Configured via `evaluation.llm_method` section:")
        sections.append("     - `model`: Bedrock model ID (e.g., Claude Haiku, Sonnet)")
        sections.append(
            "     - `task_prompt`: Custom prompt template with context placeholders"
        )
        sections.append("     - `system_prompt`: System instructions for the LLM")
        sections.append(
            "     - `temperature`, `top_k`, `top_p`, `max_tokens`: LLM generation parameters"
        )
        sections.append("   - Provides contextual evaluation with reasoning")
        sections.append(
            "   - Use for: Complex nested objects, structured data, semantic understanding"
        )
        sections.append("")
        sections.append("### Array-Level Matching")
        sections.append("")
        sections.append(
            "8. **HUNGARIAN** - Bipartite graph matching for arrays of structured objects"
        )
        sections.append(
            "   - Finds optimal 1:1 mapping between expected and actual lists"
        )
        sections.append(
            "   - Each matched item pair is then compared using field-level methods"
        )
        sections.append(
            '   - Configured with `x-aws-idp-evaluation-method: "HUNGARIAN"` on array properties'
        )
        sections.append("")
        sections.append(
            "9. **LLM for Arrays** - Semantic evaluation of entire list structures"
        )
        sections.append("   - Evaluates whether lists semantically match as a whole")
        sections.append(
            '   - Configured with `x-aws-idp-evaluation-method: "LLM"` on array properties'
        )
        sections.append("")
        sections.append("### Field Weighting")
        sections.append("")
        sections.append(
            "Fields can be assigned importance weights using `x-aws-idp-evaluation-weight` in the schema:"
        )
        sections.append("- **Default weight**: 1.0 (standard importance)")
        sections.append(
            "- **Higher weights** (e.g., 2.0, 3.0): Critical fields that matter more for overall quality"
        )
        sections.append(
            "- **Lower weights** (e.g., 0.5): Less important optional fields"
        )
        sections.append(
            "- **Impact**: Used in Stickler's weighted_overall_score calculation"
        )
        sections.append(
            "- **Display**: Shown in the Weight column of attribute results"
        )
        sections.append("")
        sections.append("**Example field weights:**")
        sections.append("- Account Number (weight: 3.0) - Critical identifier")
        sections.append("- Phone Number (weight: 1.0) - Standard field")
        sections.append("- Notes (weight: 0.5) - Optional supplementary info")
        sections.append("")
        sections.append(
            "The **Weighted Overall Score** aggregates individual field scores weighted by importance:"
        )
        sections.append("- **Formula**: `Σ(weight_i × score_i) / Σ(weight_i)`")
        sections.append("- **Section-level**: Weighted score for that specific section")
        sections.append("- **Document-level**: Average of all section weighted scores")
        sections.append("")
        sections.append("---")
        sections.append("")
        sections.append(
            "**Note**: Each attribute specifies its evaluation method via `x-aws-idp-evaluation-method` in the schema."
        )

        # Add Metrics Explanation at the very end
        if self.doc_split_metrics:
            sections.append("")
            sections.append("## 📖 Metrics Explanation")
            sections.append("")
            sections.append("### Page Level Accuracy")
            sections.append(
                "- Evaluates classification accuracy for **individual pages**"
            )
            sections.append(
                "- Each page index is checked: does its predicted `document_class` match expected?"
            )
            sections.append("- Does not consider page grouping into sections")
            sections.append("")
            sections.append("### Document Split Accuracy (Without Page Order)")
            sections.append(
                "- Evaluates whether pages are **correctly grouped into sections**"
            )
            sections.append(
                "- For each expected section, checks if predicted sections contain:"
            )
            sections.append("  - Same set of page indices (order doesn't matter)")
            sections.append("  - Same `document_class`")
            sections.append(
                "- Both conditions must be met for a section to be marked correct"
            )
            sections.append("")
            sections.append("### Document Split Accuracy (With Page Order)")
            sections.append("- Same as above but **page order must match exactly**")
            sections.append(
                "- The `page_indices` list must be identical (same pages, same order)"
            )
            sections.append("- Most strict evaluation metric")
            sections.append("")

        return "\n".join(sections)
