# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The leaf values of a section result, and the fields a draft changed.

A light module: standard library only, so a step that compares a reviewer's
draft with a section result can import it without the review-queue service.

A leaf path is written in the grammar of :mod:`idp_common.field_paths`
(``Vendor.Name``, ``Lines[2].Amount``), so a path this module returns can be
read with ``extract_path`` and written with ``set_path``.
"""

from __future__ import annotations

from typing import Any

__all__ = ["changed_fields", "leaf_values"]


def leaf_values(value: Any, prefix: str = "") -> dict[str, Any]:
    """Every leaf of ``value`` by its path. An empty dict or list has no leaves."""
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            out.update(leaf_values(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.update(leaf_values(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = value
    return out


def changed_fields(base: Any, new: Any) -> list[str]:
    """Leaf paths under ``inference_result`` whose value differs (D71)."""
    a = leaf_values(
        (base or {}).get("inference_result", {}) if isinstance(base, dict) else {}
    )
    b = leaf_values(
        (new or {}).get("inference_result", {}) if isinstance(new, dict) else {}
    )
    return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))
