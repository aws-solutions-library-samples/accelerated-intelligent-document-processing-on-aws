# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Document evaluation functionality.

This module provides services and models for evaluating document extraction results
using the Stickler library for structured object comparison.

**Stickler-dependent exports are resolved lazily.** ``EvaluationService`` and the
mapper import ``stickler``, which is only installed via the ``[evaluation]``
extra — deliberately excluded from the shared base Lambda layer because it adds
50MB+. Importing them eagerly here made *every* module in this package
un-importable without that extra, including ``confidence_curve`` and
``curve_store``, which depend on nothing but the standard library and boto3 and
are needed by Lambdas that run on the base layer (the test-set resolver, the HITL
review function). Accessing a stickler-backed name without the extra installed
still raises, with a message naming the extra, rather than failing on an opaque
``ModuleNotFoundError: stickler`` at cold start.
"""

from typing import Any

# Import-safe without the [evaluation] extra: standard library + boto3 only.
from idp_common.evaluation.confidence_curve import (
    CalibrationHealth,
    ConfidenceCurve,
    EstimateConfidence,
    ReviewEstimate,
    estimate_for_target,
)
from idp_common.evaluation.curve_store import CurveStore
from idp_common.evaluation.intervals import (
    AccuracyInterval,
    accuracy_interval,
    accuracy_interval_from_confusion_matrix,
    wilson_interval,
)
from idp_common.evaluation.models import (
    AttributeEvaluationResult,
    DocumentEvaluationResult,
    EvaluationAttribute,
    EvaluationMethod,
    SectionEvaluationResult,
)

# Names that require the [evaluation] extra, mapped to their defining module.
_LAZY_EXPORTS = {
    "EvaluationService": "idp_common.evaluation.service",
    "SticklerConfigMapper": "idp_common.evaluation.stickler_mapper",
    "LLMComparator": "idp_common.evaluation.llm_comparator",
}


def __getattr__(name: str) -> Any:
    """Resolve stickler-backed exports on first access."""
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        import importlib

        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ImportError(
            f"{name} requires the evaluation extra. Install it from the local "
            f'checkout: pip install -e "lib/idp_common_pkg[evaluation]" '
            f"(original error: {e})"
        ) from e
    value = getattr(module, name)
    globals()[name] = value  # Cache so later lookups skip __getattr__.
    return value


__all__ = [
    # Core models and enums
    "EvaluationMethod",
    "EvaluationAttribute",
    "AttributeEvaluationResult",
    "SectionEvaluationResult",
    "DocumentEvaluationResult",
    # Main service (Stickler-based)
    "EvaluationService",
    # Stickler components
    "SticklerConfigMapper",
    "LLMComparator",
    # Confidence→accuracy curve and the review-effort estimator
    "ConfidenceCurve",
    "CalibrationHealth",
    "EstimateConfidence",
    "ReviewEstimate",
    "estimate_for_target",
    "CurveStore",
    # Sampling uncertainty on a measured accuracy
    "AccuracyInterval",
    "accuracy_interval",
    "accuracy_interval_from_confusion_matrix",
    "wilson_interval",
]
