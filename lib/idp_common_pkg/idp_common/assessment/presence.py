# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Absent extracted values: kept out of confidence alerts, counted when required.

The code lives in :mod:`idp_common.field_values`, a light module that review
routing can import without the assessment extra. This module re-exports its
public names, so ``idp_common.assessment.presence`` keeps working for every
existing caller. The private helpers are imported from ``field_values`` itself.
"""

from idp_common.field_values import (
    absent_value_paths,
    collect_missing_required_fields,
    exclude_absent_value_alerts,
    has_extracted_value,
    iter_assessed_leaf_values,
)

__all__ = [
    "absent_value_paths",
    "collect_missing_required_fields",
    "exclude_absent_value_alerts",
    "has_extracted_value",
    "iter_assessed_leaf_values",
]
