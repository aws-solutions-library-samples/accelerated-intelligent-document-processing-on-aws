# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cross-Lambda contract for the evaluation pipeline.

The evaluation service (``EvaluationService`` in ``service.py``) writes
per-doc ``results.json`` files that the ``test_execution_aggregation_function``
Lambda reads. Both sides live in different packages / deploy artifacts; the
shape of what they exchange (``stickler_comparison_result`` blob, the
``compare_with`` flag set that produced it, and the S3 key template) is a
de-facto API. This module makes the contract explicit so a shape change fails
loudly at read time rather than as wrong dashboard numbers downstream.

Bump ``STICKLER_RESULT_VERSION`` on any change that alters the raw blob shape
Stickler emits (a stickler-eval upgrade, a new ``compare_with`` flag, a
different accumulator). The aggregation Lambda can key off it to reject
mismatched blobs (or migrate) instead of silently doubling counters.
"""

from typing import Any, Dict

# S3 key template for per-document evaluation output. Both the evaluation
# service and the aggregation Lambda import this rather than each pinning
# their own copy of the string.
EVALUATION_RESULTS_KEY_TEMPLATE = "{document_input_key}/evaluation/results.json"


def evaluation_results_key(document_input_key: str) -> str:
    """Return the S3 key under which per-doc results.json is stored.

    Args:
        document_input_key: The document's ``input_key`` field (the S3 key of
            the source document within its input bucket).
    """
    return EVALUATION_RESULTS_KEY_TEMPLATE.format(document_input_key=document_input_key)


# Flag set passed to ``expected_instance.compare_with(...)`` for each section.
# Change this (add/remove a flag) → the raw blob's shape changes → bump
# STICKLER_RESULT_VERSION.
def compare_with_flags() -> Dict[str, Any]:
    """The exact keyword arguments the evaluation service passes to
    ``StructuredModel.compare_with`` for every section. Kept in one place so
    the aggregation Lambda can assert the same flags are in force when it
    validates old ``results.json`` payloads.
    """
    # Avoid importing stickler here — this dict is data, not a call site.
    return {
        "document_field_comparisons": True,
        "document_non_matches": True,
        "include_confusion_matrix": True,
        "add_derived_metrics": True,
        "add_confidence_metrics": True,
    }


# Version stamp for the ``stickler_comparison_result`` blob shape AND the
# derived ``_stickler_counts`` semantics. Bump on ANY change that alters what
# appears at ``result["fields"][name]``, ``result["confusion_matrix"]``,
# ``result["confidence_metrics"]``, the top-level keys the aggregation Lambda
# relies on, OR the meaning of the values in ``_stickler_counts`` on
# ``SectionEvaluationResult.metrics``. Numeric MAJOR.MINOR string so
# lexicographic and numeric ordering agree.
#
# Version history:
#   1.0 — initial (v0.6.3 stickler cleanup): counts sourced from
#         ``cm["aggregate"]`` (leaf-level of matched items) and later
#         ``cm["overall"]`` (item-level after Hungarian pairing). Both
#         missed at least one failure mode on list-heavy documents (see #625).
#   2.0 — leaf-level from row-level ``field_comparisons``: every threshold-
#         gated leaf verdict Stickler emits contributes to section and
#         document counts. Fixes both the parent-vs-children contradiction
#         and the section-metric inflation on list-heavy documents.
STICKLER_RESULT_VERSION = "2.0"
