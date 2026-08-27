#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Out-of-band verification. NO AGENTS, NO MODEL CALLS.

DESIGN RULE 2: the agent must never grade itself. An agent asked "did you succeed?"
will report a green banner it half-remembers as proof. So the worker only states what it
*claims*, and this module confirms it against the system of record — DynamoDB, S3, or
the REST API — using deterministic Python.

Every verifier returns:
  {method, confirmed: bool|None, evidence, detail}
`confirmed=None` means UNVERIFIABLE (no oracle could be applied), which is a third
outcome distinct from pass and fail, and must never be reported as success.
"""

from __future__ import annotations

import gzip
import json
import re
from typing import Any

import boto3


def _table(ctx: dict, region: str, name_contains: str, exclude: tuple[str, ...] = ()) -> str | None:
    """Find a stack table by substring. The stack's logical names are not exposed in
    ctx, so match on the physical name prefix."""
    ddb = boto3.client("dynamodb", region_name=region)
    stack = ctx["stack"]
    paginator = ddb.get_paginator("list_tables")
    for page in paginator.paginate():
        for t in page["TableNames"]:
            if not t.startswith(stack):
                continue
            low = t.lower()
            if name_contains.lower() not in low:
                continue
            if any(x.lower() in low for x in exclude):
                continue
            return t
    return None


def _configured_classes(ctx: dict, region: str) -> list[str]:
    """Ground-truth document classes from the active configuration.

    The config is gzip-compressed in DynamoDB (`_compressed_config`, with
    `_config_storage='compressed'`), and each class is a JSON Schema object whose name
    lives in `x-aws-idp-document-type` (falling back to `$id`) — NOT a `name` field.
    """
    tbl = _table(ctx, region, "configuration")
    if not tbl:
        return []
    ddb = boto3.client("dynamodb", region_name=region)
    item = ddb.get_item(TableName=tbl, Key={"Configuration": {"S": "Config#default"}}).get("Item")
    if not item:
        return []
    blob = item.get("_compressed_config", {}).get("B")
    if blob:
        cfg = json.loads(gzip.decompress(blob))
    else:
        raw = item.get("Configuration", {}).get("S")
        cfg = json.loads(raw) if raw else {}
    out = []
    for c in cfg.get("classes", []) or []:
        n = c.get("x-aws-idp-document-type") or c.get("$id") or c.get("name")
        if n:
            out.append(str(n))
    return out


def _document_statuses(ctx: dict, region: str) -> list[tuple[str, str]]:
    tbl = _table(ctx, region, "trackingtable", exclude=("bootstrap", "discovery"))
    if not tbl:
        return []
    ddb = boto3.client("dynamodb", region_name=region)
    out: list[tuple[str, str]] = []
    for page in ddb.get_paginator("scan").paginate(TableName=tbl, Limit=100):
        for it in page.get("Items", []):
            key = it.get("ObjectKey", {}).get("S") or it.get("PK", {}).get("S") or ""
            status = it.get("ObjectStatus", {}).get("S") or ""
            if key and status:
                out.append((key, status))
        if len(out) >= 100:
            break
    return out


def _mentions(haystack: str, needle: str) -> bool:
    """Loose match: the agent may reformat a class name ('US drivers licenses' for
    'US-drivers-licenses'). Compare on alphanumerics only so punctuation and case
    differences don't read as a miss."""
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
    return norm(needle) in norm(haystack)


def verify_claim(spec: dict, report: dict | None, ctx: dict, region: str) -> dict[str, Any]:
    method = (spec.get("verify") or {}).get("method")
    if report is None:
        return {
            "method": method,
            "confirmed": None,
            "evidence": "worker produced no report",
            "detail": "agent errored before reporting",
        }

    # A precondition block is not a failure of the feature, and must not be scored as one.
    if report.get("blocked_by_precondition"):
        return {
            "method": method,
            "confirmed": None,
            "evidence": "worker reported blocked_by_precondition",
            "detail": "required prior state was missing; not a usability verdict",
        }

    haystack = " ".join(
        [str(report.get("claimed_action") or ""), str(report.get("narrative") or "")]
    )

    if method == "ddb_config_classes":
        truth = _configured_classes(ctx, region)
        if not truth:
            return {"method": method, "confirmed": None,
                    "evidence": "could not read configured classes", "detail": ""}
        found = [c for c in truth if _mentions(haystack, c)]
        recall = len(found) / len(truth)
        need = float((spec.get("verify") or {}).get("min_recall", 0.6))
        return {
            "method": method,
            "confirmed": recall >= need,
            "evidence": f"named {len(found)}/{len(truth)} configured classes (recall {recall:.2f}, need {need:.2f})",
            "detail": {"ground_truth": truth, "named_by_agent": found,
                       "missed": [c for c in truth if c not in found]},
        }

    if method == "ddb_document_status":
        truth = _document_statuses(ctx, region)
        if not truth:
            return {"method": method, "confirmed": None,
                    "evidence": "no documents in tracking table — nothing to verify",
                    "detail": "precondition: deployment has no processed documents"}
        statuses = sorted({s for _, s in truth})
        found = [s for s in statuses if _mentions(haystack, s)]
        need = float((spec.get("verify") or {}).get("min_recall", 0.5))
        recall = len(found) / len(statuses)
        return {
            "method": method,
            "confirmed": recall >= need,
            "evidence": f"named {len(found)}/{len(statuses)} distinct statuses present (recall {recall:.2f})",
            "detail": {"ground_truth_statuses": statuses, "named_by_agent": found,
                       "document_count": len(truth)},
        }

    return {"method": method, "confirmed": None,
            "evidence": f"no verifier implemented for method={method!r}", "detail": ""}
