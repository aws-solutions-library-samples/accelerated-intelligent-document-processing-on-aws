# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Stickler backend for IDP evaluation.

Single import boundary: this is the ONLY package under
``idp_common.evaluation`` allowed to ``import stickler``. Everything else
(``service.py``, ``models.py``, tests, downstream Lambdas) stays
backend-agnostic and consumes this package's public surface. A future
Stickler upgrade is a one-package review, not a cross-cutting hunt.

Public surface:
- ``SticklerConfigMapper`` — IDP → Stickler schema-extension translation.
- ``LLMComparator`` — IDP's Bedrock-backed comparator, registered with
  Stickler under the distinct name ``IDPLLMComparator``.
- ``register_idp_comparators()`` — idempotent registration with the global
  registry (invoked once by ``EvaluationService.__init__``).
- ``get_stickler_model(...)`` — build a ``StructuredModel`` subclass from an
  IDP config schema (goes through ``JsonSchemaFieldConverter`` +
  ``pydantic.create_model`` + the nullable-fields shim).
- ``transform_stickler_result(...)`` — convert Stickler's raw
  ``compare_with`` dict into IDP dataclasses. Encodes R3: no re-scoring, all
  verdicts / counts / derived metrics come straight from Stickler's
  ``confusion_matrix.fields`` and ``.aggregate``.
- ``load_sections_for_doc_split(...)`` and ``compute_graded_packet_metrics(...)``
  — the thin adapters over ``stickler.doc_split.*``.
"""

# Re-exported so ``service.py`` and any other backend consumers don't need a
# direct ``import stickler`` (single-boundary rule).
from stickler import StructuredModel

from idp_common.evaluation.stickler_backend.comparators import (
    LLMComparator,
    register_idp_comparators,
)
from idp_common.evaluation.stickler_backend.doc_split import (
    DocSplitClassificationMetrics,
    attach_page_confidence,
    compute_graded_packet_metrics,
    load_sections_for_doc_split,
)
from idp_common.evaluation.stickler_backend.mapper import SticklerConfigMapper
from idp_common.evaluation.stickler_backend.model_factory import get_stickler_model
from idp_common.evaluation.stickler_backend.results import transform_stickler_result

__all__ = [
    "DocSplitClassificationMetrics",
    "LLMComparator",
    "SticklerConfigMapper",
    "StructuredModel",
    "attach_page_confidence",
    "compute_graded_packet_metrics",
    "get_stickler_model",
    "load_sections_for_doc_split",
    "register_idp_comparators",
    "transform_stickler_result",
]
