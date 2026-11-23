# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ConfBench Test Set feature API.

Route                     | Purpose
------------------------- | ---------------------------------------------------
GET    /variants          | Noise-variant catalog + size tiers (drives the picker)
GET    /jobs              | Recent ingest jobs for this feature
GET    /jobs/{jobId}      | One job's live progress
POST   /ingest            | Start an ingest job (Admin only)
DELETE /dataset/{testSetId} | Remove an ingested test set and its S3 objects (Admin only)

The HTTP API is fronted by a Cognito JWT authorizer pointing at the main
stack's User Pool, so every request here is already authenticated. The two
mutating routes additionally require Admin group membership: an ingest moves up
to 32.71 GB into the host's TestSet bucket and the delete is destructive, so
neither should be available to a read-only viewer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import boto3
import variants as variant_catalog
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_JOB_TABLE = os.environ["JOB_TABLE"]
_TESTSET_BUCKET = os.environ["TESTSET_BUCKET"]
_HOST_TRACKING_TABLE = os.environ["HOST_TRACKING_TABLE"]
_STATE_MACHINE_ARN = os.environ["INGEST_STATE_MACHINE_ARN"]

_ddb = boto3.resource("dynamodb")
_s3 = boto3.client("s3")
_sfn = boto3.client("stepfunctions")

# Only test sets this extension owns may be deleted through it — never an
# arbitrary caller-supplied prefix in the shared TestSet bucket.
_OWNED_TEST_SET_IDS = frozenset(
    [str(spec["testSetId"]) for spec in variant_catalog.TIERS.values()]
    + [variant_catalog.CUSTOM_TEST_SET_ID]
)

_JOB_ID_RE = re.compile(r"^[0-9a-f-]{36}$")

#: The ``source`` the ingest stamps on its host test-set record (see
#: ``ingest/planner.py``). A record with any other source is a set somebody else
#: created in Test Studio under one of these fixed ids.
_DATASET_SOURCE = f"huggingface:{variant_catalog.HF_REPO_ID}"


def _foreign_test_set(test_set_id: str) -> bool:
    """Whether the host holds a record for this id that ConfBench did not write."""
    item = (
        _ddb.Table(_HOST_TRACKING_TABLE)
        .get_item(Key={"PK": f"testset#{test_set_id}", "SK": "metadata"})
        .get("Item")
    )
    return bool(item) and item.get("source") != _DATASET_SOURCE


def _id_in_use(test_set_id: str) -> Dict[str, Any]:
    return _response(
        409,
        {
            "error": f"Test set {test_set_id!r} already exists in Test Studio and "
            "was not created by ConfBench. Delete or rename it there first."
        },
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _response(status: int, body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=_json_default),
    }


def _json_default(value: Any) -> Any:
    """json.dumps fallback: DynamoDB hands back Decimal for every number.

    Integral Decimals become ints so counts render as `75`, not `75.0`.
    Anything else falls back to its string form rather than raising — a
    progress response is not worth a 500.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return str(value)


def _caller_groups(event: Dict[str, Any]) -> List[str]:
    """Cognito groups from the HTTP API JWT authorizer claims.

    The `cognito:groups` claim arrives as a string like "[Admin Author]"
    (API Gateway stringifies the list) or occasionally a real list — handle both.
    """
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    )
    raw = claims.get("cognito:groups")
    if isinstance(raw, list):
        return [str(g) for g in raw]
    if isinstance(raw, str):
        return raw.strip("[]").split()
    return []


def _require_admin(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return an error response if the caller is not an Admin, else None."""
    if "Admin" not in set(_caller_groups(event)):
        return _response(
            403,
            {
                "error": "This operation requires the Admin role.",
            },
        )
    return None


# ---------------------------------------------------------------------------
# GET /variants
# ---------------------------------------------------------------------------
def _get_variants() -> Dict[str, Any]:
    payload = variant_catalog.catalog()
    # Annotate which tiers already have data on disk, so the UI can show
    # "installed" state instead of offering a redundant re-ingest.
    existing: Dict[str, int] = {}
    for test_set_id in sorted(_OWNED_TEST_SET_IDS):
        count = _count_objects(f"{test_set_id}/input/")
        if count:
            existing[test_set_id] = count
    payload["deployed"] = existing
    return payload


def _count_objects(prefix: str) -> int:
    """Paginated object count under a prefix."""
    total = 0
    token: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"Bucket": _TESTSET_BUCKET, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = _s3.list_objects_v2(**kwargs)
        total += resp.get("KeyCount", 0)
        token = resp.get("NextContinuationToken")
        if not resp.get("IsTruncated"):
            return total


# ---------------------------------------------------------------------------
# POST /ingest
# ---------------------------------------------------------------------------
def _start_ingest(body: Dict[str, Any]) -> Dict[str, Any]:
    tier = body.get("tier")
    selected = body.get("variants")
    if tier and tier not in variant_catalog.TIERS:
        return _response(
            400,
            {
                "error": f"Unknown tier {tier!r}. "
                f"Valid tiers: {', '.join(variant_catalog.TIERS)}"
            },
        )
    try:
        test_set_id, resolved = variant_catalog.resolve_selection(tier, selected)
    except (KeyError, ValueError) as exc:
        return _response(400, {"error": str(exc)})

    files, nbytes = variant_catalog.totals(resolved)

    # Refuse a second concurrent job for the same test set — two ingests writing
    # the same keys would double-count progress and race on the row index.
    active = _active_job_for(test_set_id)
    if active:
        return _response(
            409,
            {
                "error": f"An ingest for {test_set_id} is already "
                f"{active.get('jobStatus')}.",
                "jobId": active.get("jobId"),
            },
        )

    # Early, friendly answer only; the ingest's own conditional claim is what
    # actually keeps a set created after this read.
    if _foreign_test_set(test_set_id):
        return _id_in_use(test_set_id)

    job_id = str(uuid.uuid4())
    item = {
        "jobId": job_id,
        "testSetId": test_set_id,
        "tier": tier or "custom",
        "variants": resolved,
        "jobStatus": "STARTING",
        "plannedFiles": files,
        "expectedBytes": nbytes,
        "filesUploaded": 0,
        "filesSkipped": 0,
        "filesFailed": 0,
        "createdAt": _now(),
        "updatedAt": _now(),
    }
    _ddb.Table(_JOB_TABLE).put_item(Item=item)

    execution = _sfn.start_execution(
        stateMachineArn=_STATE_MACHINE_ARN,
        name=f"confbench-{job_id}",
        input=json.dumps(
            {"jobId": job_id, "testSetId": test_set_id, "variants": resolved}
        ),
    )
    _ddb.Table(_JOB_TABLE).update_item(
        Key={"jobId": job_id},
        UpdateExpression="SET executionArn = :a, updatedAt = :t",
        ExpressionAttributeValues={":a": execution["executionArn"], ":t": _now()},
    )
    logger.info(
        "Started ConfBench ingest job %s for %s (%d files, %.2f GB, variants: %s)",
        job_id,
        test_set_id,
        files,
        nbytes / 1e9,
        ",".join(resolved),
    )
    return _response(
        202,
        {
            "jobId": job_id,
            "testSetId": test_set_id,
            "variants": resolved,
            "plannedFiles": files,
            "expectedBytes": nbytes,
            "status": "STARTING",
        },
    )


def _active_job_for(test_set_id: str) -> Optional[Dict[str, Any]]:
    """Most recent non-terminal job for a test set, if any."""
    table = _ddb.Table(_JOB_TABLE)
    resp = table.query(
        IndexName="ByTestSet",
        KeyConditionExpression=Key("testSetId").eq(test_set_id),
        ScanIndexForward=False,
        Limit=5,
    )
    for item in resp.get("Items", []):
        if item.get("jobStatus") in ("STARTING", "RUNNING"):
            return dict(item)
    return None


# ---------------------------------------------------------------------------
# GET /jobs, GET /jobs/{id}
# ---------------------------------------------------------------------------
def _get_job(job_id: str) -> Dict[str, Any]:
    if not _JOB_ID_RE.match(job_id):
        return _response(400, {"error": "Malformed job id"})
    item = _ddb.Table(_JOB_TABLE).get_item(Key={"jobId": job_id}).get("Item")
    if not item:
        return _response(404, {"error": f"No such job {job_id}"})
    return _response(200, {"job": dict(item)})


def _list_jobs() -> Dict[str, Any]:
    # Job volume is inherently tiny (one row per admin-initiated ingest), so a
    # bounded scan is appropriate and cheaper than maintaining another GSI.
    resp = _ddb.Table(_JOB_TABLE).scan(Limit=100)
    jobs = sorted(
        (dict(i) for i in resp.get("Items", [])),
        key=lambda j: str(j.get("createdAt") or ""),
        reverse=True,
    )
    return _response(200, {"jobs": jobs})


# ---------------------------------------------------------------------------
# DELETE /dataset/{testSetId}
# ---------------------------------------------------------------------------
def _delete_dataset(test_set_id: str) -> Dict[str, Any]:
    if test_set_id not in _OWNED_TEST_SET_IDS:
        return _response(
            400,
            {
                "error": f"{test_set_id!r} is not a ConfBench test set. "
                f"Deletable ids: {', '.join(sorted(_OWNED_TEST_SET_IDS))}"
            },
        )
    if _active_job_for(test_set_id):
        return _response(
            409,
            {"error": f"An ingest for {test_set_id} is still running."},
        )

    # The id being on the list is not enough: a user can create a set with it in
    # Test Studio, and this route must not delete that set's files.
    if _foreign_test_set(test_set_id):
        return _id_in_use(test_set_id)

    try:
        deleted = _delete_prefix(f"{test_set_id}/")
    except _ObjectsNotDeleted as exc:
        # The record stays: without it Test Studio would register the leftover
        # folder as a user upload, and the next ingest would find its id taken.
        logger.error("Delete of %s stopped partway: %s", test_set_id, exc)
        return _response(
            500,
            {
                "error": f"Deleting {test_set_id} stopped partway: {exc}. Some files "
                "remain; delete it again."
            },
        )
    try:
        # Conditional, so a record somebody else wrote since the check is kept.
        _ddb.Table(_HOST_TRACKING_TABLE).delete_item(
            Key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
            ConditionExpression="attribute_not_exists(PK) OR #source = :source",
            ExpressionAttributeNames={"#source": "source"},
            ExpressionAttributeValues={":source": _DATASET_SOURCE},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not delete host test-set record: %s", exc)

    logger.info("Deleted ConfBench test set %s (%d objects)", test_set_id, deleted)
    return _response(
        200, {"testSetId": test_set_id, "objectsDeleted": deleted, "status": "DELETED"}
    )


class _ObjectsNotDeleted(RuntimeError):
    """DeleteObjects reported keys it did not delete."""


def _delete_prefix(prefix: str) -> int:
    """Delete every object under a prefix, paginating both list and delete.

    A full ConfBench set is ~2,700 objects (1,346 PDFs + 1,346 baselines), well
    past list_objects_v2's 1,000-key page — so pagination here is required, not
    defensive. delete_objects also caps at 1,000 keys per call.
    """
    deleted = 0
    token: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"Bucket": _TESTSET_BUCKET, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = _s3.list_objects_v2(**kwargs)
        batch = [
            {"Key": key}
            for key in (obj.get("Key") for obj in (resp.get("Contents") or []))
            if key
        ]
        for i in range(0, len(batch), 1000):
            chunk = batch[i : i + 1000]
            result = _s3.delete_objects(
                Bucket=_TESTSET_BUCKET, Delete={"Objects": chunk}
            )
            # A 200 can still carry per-key failures; only Errors says so.
            errors = result.get("Errors") or []
            deleted += len(chunk) - len(errors)
            if errors:
                raise _ObjectsNotDeleted(
                    f"{len(errors)} object(s) could not be deleted (first: "
                    f"{errors[0].get('Key')}: {errors[0].get('Code')})"
                )
        token = resp.get("NextContinuationToken")
        if not resp.get("IsTruncated"):
            return deleted


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def _route(event: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    method = (
        event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
    )
    path = event.get("rawPath", "/") or "/"
    # HTTP API stage prefixes are not part of rawPath for $default stages, but
    # strip a leading stage segment defensively if one appears.
    segments = [s for s in path.split("/") if s]
    return method, path, segments


def lambda_handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    method, path, segments = _route(event)
    logger.info("confbench-testset API %s %s", method, path)

    try:
        if method == "GET":
            if segments[:1] == ["variants"]:
                return _response(200, _get_variants())
            if segments[:1] == ["jobs"]:
                if len(segments) >= 2:
                    return _get_job(segments[1])
                return _list_jobs()
            return _response(404, {"error": f"unknown path {path}"})

        if method == "POST" and segments[:1] == ["ingest"]:
            denied = _require_admin(event)
            if denied:
                return denied
            try:
                body = json.loads(event.get("body") or "{}")
            except json.JSONDecodeError as exc:
                return _response(400, {"error": f"Body is not valid JSON: {exc}"})
            if not isinstance(body, dict):
                return _response(400, {"error": "Body must be a JSON object"})
            return _start_ingest(body)

        if method == "DELETE" and segments[:1] == ["dataset"]:
            denied = _require_admin(event)
            if denied:
                return denied
            if len(segments) < 2:
                return _response(400, {"error": "Provide a test set id to delete"})
            return _delete_dataset(segments[1])

        return _response(404, {"error": f"unknown path {method} {path}"})

    except Exception as exc:  # noqa: BLE001
        logger.exception("confbench-testset API failed")
        return _response(500, {"error": str(exc)})
