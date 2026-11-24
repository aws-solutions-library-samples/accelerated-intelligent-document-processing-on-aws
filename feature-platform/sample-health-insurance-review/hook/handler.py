# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""postRuleValidation pipeline hook — derives a claim status per document.

The host's pipeline-hooks dispatcher (patterns/unified/src/
pipeline_hooks_function) invokes this Lambda after policy rule validation, in
every run that reaches that step, with the payload:

    {
      "hookPoint": "postRuleValidation",
      "featureId": "sample-health-insurance-review",
      "document": { ... },          # usually a compressed reference, see below
      "executionArn": "arn:...:execution:...",
      "runKind": "new_upload",      # what started the run; "unplanned" if absent
      "startStep": "beginning",     # the step the run started at
      "reviewed": false             # true when the run validates reviewed values
    }

A document that is held for review reaches the step twice: in the run that
places it in a review queue (`reviewed` false), and again in the run that
follows the completed review (`runKind` "review_completed"). The second write
replaces the first, so the row describes the document after the review. That
second run carries `reviewed` true when it applies the reviewer's corrections
to the existing results and validates the corrected values. When it extracts
sections again (the reviewer changed the pages of a section, or a section has
no result), it carries `reviewed` false, because the newly extracted values are
not reviewer-confirmed. A document whose review is rejected never has the
second run: it keeps the row written before the review, with `reviewed` false.

`document` is almost always a *compressed reference* — the host serializes
Document state to its Working bucket and passes
`{"compressed": true, "s3_uri": "s3://<working>/compressed_documents/..."}`
(see idp_common.models.Document.serialize_document). We resolve it with a
small inline helper instead of depending on idp_common, so this sample stays
copyable by third-party feature authors whose stacks can't reference
repo-relative package paths.

Status derivation is DETERMINISTIC (no LLM) from the consolidated summary's
`overall_statistics.recommendation_counts`:

    all rules Pass                          -> CLEAN_CLAIM
    any rule Fail                           -> REVIEW_REQUIRED
    no Fail, but not all Pass (Information
    Not Found / Unknown dominate)           -> INSUFFICIENT_DOCUMENTATION

The result row is written to this feature's own ClaimsStatus DynamoDB table
(PutItem keyed by documentId — naturally idempotent if the dispatcher or
Step Functions retries). The hook NEVER raises for missing/partial results:
documents that skipped rule validation simply produce `{"skipped": true}`,
and the hook is registered with onError=continue regardless — a sample
feature must never break the host pipeline.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_CLAIMS_TABLE = os.environ.get("CLAIMS_TABLE_NAME", "")
_WORKING_BUCKET = os.environ.get("WORKING_BUCKET", "")
_OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "")

_STATUS_CLEAN = "CLEAN_CLAIM"
_STATUS_REVIEW = "REVIEW_REQUIRED"
_STATUS_INSUFFICIENT = "INSUFFICIENT_DOCUMENTATION"

_s3 = boto3.client("s3")
_dynamodb = boto3.resource("dynamodb")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_s3_json(uri: str) -> Optional[Dict[str, Any]]:
    parsed = urlparse(uri)
    try:
        resp = _s3.get_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))
        body = json.loads(resp["Body"].read().decode("utf-8"))
        return body if isinstance(body, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read %s: %s", uri, exc)
        return None


def _load_document(raw: Any) -> Optional[Dict[str, Any]]:
    """Resolve the hook payload's document to a plain dict.

    Inline equivalent of idp_common.models.Document.load_document: a
    compressed reference is `{"compressed": true, "s3_uri": ...}` pointing
    at the full Document JSON in the host's Working bucket.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("compressed") is True:
        uri = raw.get("s3_uri")
        if not uri:
            logger.warning("Compressed document reference without s3_uri")
            return None
        return _read_s3_json(uri)
    return raw


def _summary_json_uri(document: Dict[str, Any]) -> Optional[str]:
    """Locate the consolidated rule-validation summary JSON for this doc.

    `rule_validation_result.output_uri` points at the MARKDOWN summary (the
    host stores the .md URI because that's what its own UI displays); the
    JSON sits next to it. Fall back to the conventional output path when the
    result block is absent (e.g. an older document shape).
    """
    rv = document.get("rule_validation_result") or {}
    output_uri = rv.get("output_uri") or ""
    if output_uri.endswith(".md"):
        return output_uri[: -len(".md")] + ".json"
    if output_uri.endswith(".json"):
        return output_uri

    input_key = document.get("input_key") or document.get("id")
    output_bucket = document.get("output_bucket") or _OUTPUT_BUCKET
    if not input_key or not output_bucket:
        return None
    return (
        f"s3://{output_bucket}/{input_key}"
        f"/rule_validation/consolidated/consolidated_summary.json"
    )


def derive_status(recommendation_counts: Dict[str, Any]) -> Tuple[str, Dict[str, int]]:
    """Deterministic claim status from the summary's recommendation counts.

    Returns (status, normalized_counts). Counts outside the three known
    recommendations (e.g. "Unknown") are treated like Information Not Found:
    they block CLEAN_CLAIM but do not force REVIEW_REQUIRED.
    """
    counts = {
        str(k): int(v)
        for k, v in (recommendation_counts or {}).items()
        if isinstance(v, (int, float)) and int(v) > 0
    }
    total = sum(counts.values())
    pass_count = counts.get("Pass", 0)
    fail_count = counts.get("Fail", 0)

    if fail_count > 0:
        return _STATUS_REVIEW, counts
    if total > 0 and pass_count == total:
        return _STATUS_CLEAN, counts
    return _STATUS_INSUFFICIENT, counts


def _skip(document_id: Optional[str], reason: str) -> Dict[str, Any]:
    logger.info("Skipping claim status for %s: %s", document_id, reason)
    return {"skipped": True, "documentId": document_id, "reason": reason}


def lambda_handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    logger.info(
        "sample-health-insurance-review hook invoked: hookPoint=%s executionArn=%s",
        event.get("hookPoint"),
        event.get("executionArn"),
    )

    document = _load_document(event.get("document"))
    if not document:
        return _skip(None, "document payload missing or unresolvable")
    document_id = document.get("id") or document.get("input_key")
    if not document_id:
        return _skip(None, "document has no id/input_key")

    summary_uri = _summary_json_uri(document)
    if not summary_uri:
        return _skip(document_id, "no rule validation output location resolvable")
    summary = _read_s3_json(summary_uri)
    if not summary:
        return _skip(document_id, f"consolidated summary not found at {summary_uri}")

    stats = summary.get("overall_statistics") or {}
    status, counts = derive_status(stats.get("recommendation_counts") or {})
    matched_policy_types = (document.get("rule_validation_result") or {}).get(
        "matched_policy_types"
    ) or list((summary.get("rule_summary") or {}).keys())

    if not _CLAIMS_TABLE:
        return _skip(document_id, "CLAIMS_TABLE_NAME env var is not set")
    table = _dynamodb.Table(_CLAIMS_TABLE)
    item = {
        "documentId": document_id,
        "status": status,
        "passCount": counts.get("Pass", 0),
        "failCount": counts.get("Fail", 0),
        "notFoundCount": counts.get("Information Not Found", 0),
        "totalRules": int(stats.get("total_rules", sum(counts.values()))),
        "recommendationCounts": counts,
        "policyTypes": matched_policy_types or [],
        "summaryJsonUri": summary_uri,
        "summaryMdUri": summary_uri[: -len(".json")] + ".md",
        "executionArn": event.get("executionArn") or "",
        # Whether a reviewer had completed a review when this status was derived,
        # and what kind of run derived it (a dispatcher that sends neither is an
        # older host: not reviewed, and "unplanned").
        "reviewed": bool(event.get("reviewed", False)),
        "runKind": event.get("runKind") or "unplanned",
        "updatedAt": _now(),
    }
    table.put_item(Item=item)
    logger.info("Claim %s -> %s (counts=%s)", document_id, status, counts)

    # Returned to the dispatcher, which records it in the hook results list
    # visible in the Step Functions execution history.
    return {"documentId": document_id, "status": status, "counts": counts}
