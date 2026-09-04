# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Migrate evaluation baselines to the multi-instance shape (#715).

Turning on ``x-aws-idp-multi-instance`` for a class changes the shape of that
class's ``inference_result`` from

    {"CheckNumber": "77310468", "NetPay": 4104.59}

to

    {"instances": [{"CheckNumber": "77310468", "NetPay": 4104.59}, …]}

Evaluation compares a prediction against a stored baseline **of the same shape**.
A wrapped prediction against a flat baseline scores every field as
missing-on-one-side, so the class's accuracy collapses to ~0 with no error — the
one way this feature can break a working deployment. Baselines therefore have to
be migrated, and this module is the mechanical half of that.

**It migrates SHAPE, not CONTENT.** Wrapping a one-record baseline yields
``instances`` of length 1, which is correct ground truth only if the document
really does contain one document of that class. The whole reason a user turns the
flag on is that some documents contain several — and those records were never in
the baseline, because the old pipeline could not extract them. Adding them is
authoring work no tool can do. The report this module produces names those
documents so the work is visible rather than discovered later as a mysterious
recall drop.

Pure: no boto3, no S3. ``scripts/migrate_multi_instance_baselines.py`` is the
operational wrapper that walks the baseline bucket.
"""

from __future__ import annotations

import logging
from typing import Any

from idp_common.schema.multi_instance import INSTANCES_KEY, unwrap_instances

logger = logging.getLogger(__name__)

INFERENCE_RESULT_KEY = "inference_result"


def wrap_baseline_result(
    result_json: Any,
) -> tuple[dict[str, Any], bool]:
    """Wrap one baseline ``result.json``'s ``inference_result``.

    Returns ``(migrated_json, changed)``. **Idempotent**: an
    already-wrapped baseline is returned unchanged with ``changed=False``, so the
    migration can be re-run safely (and must be, because a partially-applied run
    is the normal outcome of an interrupted bulk job).

    ``changed=False`` is also returned for anything that is not a recognisable
    baseline result — a missing or non-dict ``inference_result``, or an empty one.
    An empty baseline carries no ground truth to preserve, and inventing
    ``{"instances": [{}]}`` for it would assert "this document contains exactly
    one record with no values", which scores worse than the honest empty.

    Never mutates the input.
    """
    if not isinstance(result_json, dict):
        return result_json, False

    inference_result = result_json.get(INFERENCE_RESULT_KEY)
    if not isinstance(inference_result, dict) or not inference_result:
        return result_json, False

    if unwrap_instances(inference_result) is not None:
        # Already wrapped.
        return result_json, False

    migrated = dict(result_json)
    migrated[INFERENCE_RESULT_KEY] = {INSTANCES_KEY: [inference_result]}
    return migrated, True


def unwrap_baseline_result(result_json: Any) -> tuple[dict[str, Any], bool]:
    """Reverse :func:`wrap_baseline_result` for a SINGLE-instance baseline.

    The rollback path: turning the flag back off needs the baselines flat again.
    Refuses (``changed=False``) when the wrapper holds more than one record,
    because flattening would silently discard ground truth the user authored —
    exactly the data loss this whole feature exists to stop. Those documents are
    reported so they can be handled deliberately.
    """
    if not isinstance(result_json, dict):
        return result_json, False

    records = unwrap_instances(result_json.get(INFERENCE_RESULT_KEY))
    if records is None or len(records) != 1:
        return result_json, False

    migrated = dict(result_json)
    migrated[INFERENCE_RESULT_KEY] = records[0]
    return migrated, True


def baseline_instance_count(result_json: Any) -> int | None:
    """How many records a baseline asserts, or None when it is not wrapped."""
    if not isinstance(result_json, dict):
        return None
    records = unwrap_instances(result_json.get(INFERENCE_RESULT_KEY))
    return None if records is None else len(records)


def multi_instance_class_labels(classes: Any) -> set[str]:
    """Lower-cased labels of every class flagged for Synthesize mode.

    Used to decide which baseline sections need migrating; a section whose class
    is not flagged must be left byte-identical.
    """
    from idp_common.config.schema_constants import ID_FIELD, X_AWS_IDP_DOCUMENT_TYPE
    from idp_common.schema.multi_instance import is_multi_instance

    labels: set[str] = set()
    for class_obj in classes or []:
        if not isinstance(class_obj, dict) or not is_multi_instance(class_obj):
            continue
        for key in (ID_FIELD, X_AWS_IDP_DOCUMENT_TYPE):
            value = class_obj.get(key)
            if isinstance(value, str) and value.strip():
                labels.add(value.strip().lower())
    return labels


def section_class_label(result_json: Any) -> str | None:
    """The class of a baseline section, from its ``document_class.type``."""
    if not isinstance(result_json, dict):
        return None
    document_class = result_json.get("document_class")
    if not isinstance(document_class, dict):
        return None
    value = document_class.get("type")
    return value if isinstance(value, str) and value else None
