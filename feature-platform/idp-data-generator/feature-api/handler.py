# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Test Set Generator feature API.

Fronted by the host's Cognito JWT authorizer (template.yaml FeatureApi), so this
handler only does application logic. It is the endpoint the host's Quick Start
tools discover (via listInstalledFeatures.featureApiEndpoint) and POST jobs to,
and the surface the feature UI page calls.

Routes
------
POST /generate
    Body (one of):
      - { prompt: str, className?: str, docCount?: int, threshold?: int,
          augment?: bool } — the processor authors a schema from the prompt.
      - { schema: <json-schema obj>, configVersion: str, docCount?: int,
          threshold?: int, augment?: bool } — preauthored schema.
    Enqueues a generation job. Returns { jobId }.

POST /generate-from-config
    Body: { versionName: str, className: str, docCount?: int, threshold?: int,
            augment?: bool }
    Reads the class schema from an existing config version and enqueues. Returns
    { jobId }. (The processor resolves the class -> generator schema; see
    bootstrap-processor.)

GET /jobs
    Returns in-flight jobs (PENDING/IN_PROGRESS) so the UI can surface jobs it
    did not itself start (e.g. started from Quick Start).

GET /jobs/{jobId}
    Returns the BootstrapTrackingTable row for a job (status, message, etc.).

POST /estimate-cost
    Body: { docCount: int, threshold?: int }. Returns { estimate: {...} } with a
    rough cost/time band for the run.

POST /suggest-scenario
    Body: { className?: str, versionName?: str, prompt?: str }. Returns
    { suggestions: [str, ...] } — short scenario themes proposed via Bedrock.

GET /config
    Returns lightweight UI config (featureId, version) for the feature page.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_QUEUE_URL = os.environ.get("BOOTSTRAP_QUEUE_URL", "")
_TRACKING_TABLE = os.environ.get("BOOTSTRAP_TRACKING_TABLE", "")
_HOST_TRACKING_TABLE = os.environ.get("HOST_TRACKING_TABLE", "")
_CONFIG_TABLE = os.environ.get("CONFIGURATION_TABLE_NAME", "")
_FEATURE_VERSION = os.environ.get("FEATURE_VERSION", "")
_SUGGEST_MODEL_ID = os.environ.get(
    "SUGGEST_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

_sqs = boto3.client("sqs")
_dynamodb = boto3.resource("dynamodb")
_bedrock = boto3.client("bedrock-runtime")


def _resp(status: int, body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _enqueue(message: Dict[str, Any]) -> str:
    job_id = message["jobId"]
    _sqs.send_message(QueueUrl=_QUEUE_URL, MessageBody=json.dumps(message))
    return job_id


MAX_DOC_COUNT = 100
MIN_THRESHOLD = 1
MAX_THRESHOLD = 10


class _BadRequest(Exception):
    """Raised for invalid request fields; surfaced as a 400."""


class _Forbidden(Exception):
    """Raised when the caller lacks the required Cognito group; surfaced as 403."""


_WRITE_GROUPS = ("Admin", "Author")


def _caller_groups(event: Dict[str, Any]) -> list:
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("jwt", {})
        .get("claims", {})
    ) or {}
    raw = claims.get("cognito:groups") or []
    if isinstance(raw, str):
        raw = [g for g in raw.strip("[]").replace(",", " ").split() if g]
    return list(raw)


def _require_write_group(event: Dict[str, Any]) -> None:
    if not any(g in _WRITE_GROUPS for g in _caller_groups(event)):
        raise _Forbidden("generating test sets requires the Admin or Author group")


def _int_field(body: Dict[str, Any], name: str, default: int, lo: int, hi: int) -> int:
    raw = body.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _BadRequest(f"{name} must be an integer")
    if value < lo or value > hi:
        raise _BadRequest(f"{name} must be between {lo} and {hi}")
    return value


_NAME_RE = re.compile(r"^[a-zA-Z0-9\s_-]+$")


def _test_set_dest(body: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the generated-docs destination test set from the request.

    Either testSetId (append to an existing set) or testSetName (create a new
    one). The id is derived from the name with the host's rule so it matches the
    console (name.replace(' ', '-').lower()). Returns testSetId/testSetName/append
    fields to merge into the SQS message.
    """
    existing_id = (body.get("testSetId") or "").strip()
    if existing_id:
        if len(existing_id) > 50 or not _NAME_RE.match(existing_id):
            raise _BadRequest("testSetId contains invalid characters")
        return {"testSetId": existing_id, "testSetName": existing_id, "append": True}
    name = (body.get("testSetName") or "").strip()
    if not name:
        raise _BadRequest("testSetName or testSetId is required")
    if len(name) > 50 or not _NAME_RE.match(name):
        raise _BadRequest(
            "testSetName may contain only letters, numbers, spaces, hyphens, and "
            "underscores (max 50 characters)"
        )
    new_id = name.replace(" ", "-").lower()
    if _test_set_exists(new_id):
        raise _BadRequest(
            f"A test set named '{name}' already exists. Choose a different name, "
            "or add to the existing test set."
        )
    return {"testSetId": new_id, "testSetName": name, "append": False}


def _test_set_exists(test_set_id: str) -> bool:
    """Backstop for the UI's collision check: is there already a host test-set
    record with this id? Best-effort — if the lookup fails, don't block."""
    if not _HOST_TRACKING_TABLE:
        return False
    try:
        item = (
            _dynamodb.Table(_HOST_TRACKING_TABLE)
            .get_item(Key={"PK": f"testset#{test_set_id}", "SK": "metadata"})
            .get("Item")
        )
        return item is not None
    except Exception:  # noqa: BLE001 — best-effort, don't block generation
        logger.warning(
            "Could not check test set existence for %s", test_set_id, exc_info=True
        )
        return False


def _handle_generate(body: Dict[str, Any]) -> Dict[str, Any]:
    # Two shapes: a natural-language prompt (the processor authors a schema), or
    # a preauthored schema + target version. Require one or the other.
    prompt = (body.get("prompt") or "").strip()
    schema = body.get("schema")
    if not prompt and not schema:
        return _resp(400, {"error": "either prompt or schema is required"})

    message = {
        "jobId": uuid.uuid4().hex,
        "prompt": prompt,
        "className": body.get("className"),
        "docCount": _int_field(body, "docCount", 3, 1, MAX_DOC_COUNT),
        "threshold": _int_field(body, "threshold", 7, MIN_THRESHOLD, MAX_THRESHOLD),
        "augment": bool(body.get("augment", False)),
        "scenario": (body.get("scenario") or "").strip(),
        "generateDocs": True,
        **_test_set_dest(body),
    }
    if schema:
        # Preauthored path — the processor uses the schema as-is and writes it
        # into targetVersion (defaults to a bootstrap-<class> version otherwise).
        message["preauthoredSchema"] = schema
        message["targetVersion"] = body.get("configVersion")
    return _resp(202, {"jobId": _enqueue(message)})


def _handle_generate_from_config(body: Dict[str, Any]) -> Dict[str, Any]:
    version_name = body.get("versionName")
    class_name = body.get("className")
    if not version_name or not class_name:
        return _resp(400, {"error": "versionName and className are required"})
    message = {
        "jobId": uuid.uuid4().hex,
        "prompt": "",
        "targetVersion": version_name,
        "className": class_name,
        "configVersion": version_name,
        "docCount": _int_field(body, "docCount", 3, 1, MAX_DOC_COUNT),
        "threshold": _int_field(body, "threshold", 7, MIN_THRESHOLD, MAX_THRESHOLD),
        "augment": bool(body.get("augment", False)),
        "scenario": (body.get("scenario") or "").strip(),
        "generateDocs": True,
        # The processor reads the class from this version and builds the
        # generator schema (schema_bridge.config_class_to_generator_schema).
        "fromExistingConfig": True,
        **_test_set_dest(body),
    }
    return _resp(202, {"jobId": _enqueue(message)})


def _estimate_cost(count: int, threshold: int) -> Dict[str, Any]:
    per_doc_usd = 1.75 if threshold <= 7 else 4.0
    per_doc_min = 7.0 if threshold <= 7 else 12.0
    return {
        "documents": count,
        "estimated_usd_low": round(per_doc_usd * count, 2),
        "estimated_usd_high": round(per_doc_usd * count * 2.5, 2),
        "estimated_minutes_low": round(per_doc_min * count / max(1, min(count, 3)), 1),
        "estimated_minutes_high": round(per_doc_min * count, 1),
        "note": "Estimates; actual cost depends on document complexity and retries.",
    }


def _handle_estimate_cost(body: Dict[str, Any]) -> Dict[str, Any]:
    count = _int_field(body, "docCount", 3, 1, MAX_DOC_COUNT)
    threshold = _int_field(body, "threshold", 7, MIN_THRESHOLD, MAX_THRESHOLD)
    return _resp(200, {"estimate": _estimate_cost(count, threshold)})


def _handle_suggest_scenario(body: Dict[str, Any]) -> Dict[str, Any]:
    class_name = (body.get("className") or "").strip()
    version_name = (body.get("versionName") or "").strip()
    prompt = (body.get("prompt") or "").strip()
    subject = class_name or prompt or "documents"
    ask = (
        "You help create diverse synthetic test documents. Propose 3 short, "
        "distinct scenario themes (each under 12 words) for generating varied "
        f"examples of: {subject}."
    )
    if version_name:
        ask += f" These belong to configuration version '{version_name}'."
    ask += (
        " A scenario is a high-level theme (e.g. 'small-business owners in "
        "retail', 'travel-heavy expense reports'). Return ONLY a JSON array of "
        "3 strings, no prose."
    )
    resp = _bedrock.invoke_model(
        modelId=_SUGGEST_MODEL_ID,
        body=json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": ask}],
            }
        ),
    )
    payload = json.loads(resp["body"].read())
    text = "".join(
        block.get("text", "")
        for block in payload.get("content", [])
        if isinstance(block, dict)
    ).strip()
    suggestions = _parse_suggestions(text)
    return _resp(200, {"suggestions": suggestions})


def _parse_suggestions(text: str) -> list:
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            arr = json.loads(text[start : end + 1])
            return [str(s).strip() for s in arr if str(s).strip()][:3]
        except json.JSONDecodeError:
            pass
    lines = [line.strip("-*• ").strip() for line in text.splitlines() if line.strip()]
    return [line for line in lines if line][:3]


# How long a running job may go without a heartbeat before it is presumed dead. The
# runtime pulses once a minute, so this is generous — it only has to exceed a pause in
# heartbeat writes, not a pause in progress (a long augmentation stage completes no
# documents for an hour but still pulses).
STALE_HEARTBEAT_MINUTES = int(os.environ.get("STALE_HEARTBEAT_MINUTES", "15"))


def _log_cleanup_failure(what: str, exc: BaseException) -> None:
    """Log a failed cleanup write at a level that matches what it means.

    A refused condition is the expected outcome of losing a race — the record moved on
    without us — so it stays quiet. Anything else is a real fault, and AccessDenied
    especially: a missing IAM grant is permanent, affects every call, and shipped once
    already precisely because it hid at info level among condition failures.
    """
    code = ""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
    if code == "ConditionalCheckFailedException":
        logger.info("%s: the record moved on first", what)
        return
    logger.warning("%s: %s", what, exc, exc_info=True)


def _release_host_test_set(test_set_id: Optional[str], error: str) -> None:
    """Move a host test-set record off GENERATING after its job was reaped.

    Conditional on GENERATING, so a set that finished — or one the host reaper already
    handled — is never touched. Best-effort: this is cleanup, not the reap itself.
    """
    if not (_HOST_TRACKING_TABLE and test_set_id):
        return
    try:
        _dynamodb.Table(_HOST_TRACKING_TABLE).update_item(
            Key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
            UpdateExpression="SET #s = :failed, #e = :error",
            ConditionExpression="#s = :generating",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={
                ":failed": "FAILED",
                ":generating": "GENERATING",
                ":error": error,
            },
        )
        logger.warning("Released host test set %s after reaping its job", test_set_id)
    except Exception as e:  # noqa: BLE001 — cleanup must not fail the list
        _log_cleanup_failure(f"Did not release host test set {test_set_id}", e)


def _reap_dead_jobs(table, jobs):
    """Fail jobs whose runtime stopped heartbeating.

    The runtime's own watchdog cannot cover this: it runs *inside* the container, so a
    container killed mid-stage (augmentation is the memory-hungry one) leaves the job
    IN_PROGRESS with nothing alive to fail it. Observed on a dev stack — the UI showed
    "generating" for 68 minutes after the runtime had gone silent, and would have shown
    it indefinitely, across reloads, because the state is a database record rather than
    anything client-side.

    Jobs with no heartbeat at all are left alone: they predate heartbeating, and
    presuming those dead would fail live work.
    """
    now = datetime.now(timezone.utc)
    for job in jobs:
        if job.get("status") != "IN_PROGRESS":
            continue
        beat = job.get("heartbeatAt")
        if not beat:
            continue
        try:
            last = datetime.fromisoformat(str(beat).replace("Z", "+00:00"))
        except ValueError:
            continue
        idle_min = (now - last).total_seconds() / 60
        if idle_min < STALE_HEARTBEAT_MINUTES:
            continue
        message = (
            f"Generation runtime stopped responding {int(idle_min)} minutes ago "
            "(no heartbeat); the container was most likely terminated mid-run"
        )
        try:
            table.update_item(
                Key={"jobId": job["jobId"]},
                UpdateExpression=(
                    "SET #s = :failed, statusMessage = :m, errorMessage = :m"
                ),
                # Only ever moves a still-running job, so a status the runtime wrote
                # in the meantime wins.
                ConditionExpression="#s = :running",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":failed": "FAILED",
                    ":running": "IN_PROGRESS",
                    ":m": message,
                },
            )
            # Only after the write lands, so a job that reported in between the read
            # and the write is not described to the caller as failed.
            job["status"] = "FAILED"
            job["statusMessage"] = message
            job["errorMessage"] = message
            logger.warning("Reaped dead job %s: %s", job["jobId"], message)
            # The job list is not what the user sees spinning: Test Studio renders the
            # host test-set record's GENERATING. The host has its own reaper, but its
            # window has to sit above the runtime's ~8h ceiling, so on its own the
            # spinner outlives the failed job by hours. Clearing it here — where a
            # heartbeat has already proven the runtime dead — is the difference between
            # 15 minutes and half a day.
            _release_host_test_set(job.get("testSetId"), message)
        except Exception as e:  # noqa: BLE001 — reaping must not fail the list
            _log_cleanup_failure(f"Could not reap job {job.get('jobId')}", e)


def _handle_list_active_jobs() -> Dict[str, Any]:
    if not _TRACKING_TABLE:
        return _resp(500, {"error": "tracking table not configured"})
    table = _dynamodb.Table(_TRACKING_TABLE)
    jobs = []
    kwargs = {"FilterExpression": Attr("status").is_in(["PENDING", "IN_PROGRESS"])}
    while True:
        page = table.scan(**kwargs)
        jobs.extend(page.get("Items", []))
        key = page.get("LastEvaluatedKey")
        if not key:
            break
        kwargs["ExclusiveStartKey"] = key
    # The UI polls this, so it is the natural place to notice a runtime that died.
    _reap_dead_jobs(table, jobs)
    return _resp(200, {"jobs": jobs})


def _handle_get_job(job_id: str) -> Dict[str, Any]:
    if not _TRACKING_TABLE:
        return _resp(500, {"error": "tracking table not configured"})
    table = _dynamodb.Table(_TRACKING_TABLE)
    item = table.get_item(Key={"jobId": job_id}).get("Item")
    if not item:
        return _resp(404, {"error": "job not found", "jobId": job_id})
    return _resp(200, {"job": item})


def lambda_handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    )
    raw_path = event.get("rawPath") or event.get("path") or "/"
    # Strip the API Gateway stage prefix if present.
    path = raw_path.rstrip("/") or "/"
    logger.info("%s %s", method, path)

    try:
        if method == "POST" and path.endswith("/generate"):
            _require_write_group(event)
            return _handle_generate(json.loads(event.get("body") or "{}"))
        if method == "POST" and path.endswith("/generate-from-config"):
            _require_write_group(event)
            return _handle_generate_from_config(json.loads(event.get("body") or "{}"))
        if method == "POST" and path.endswith("/estimate-cost"):
            return _handle_estimate_cost(json.loads(event.get("body") or "{}"))
        if method == "POST" and path.endswith("/suggest-scenario"):
            return _handle_suggest_scenario(json.loads(event.get("body") or "{}"))
        if method == "GET" and "/jobs/" in path:
            return _handle_get_job(path.rsplit("/jobs/", 1)[-1])
        if method == "GET" and path.endswith("/jobs"):
            return _handle_list_active_jobs()
        if method == "GET" and path.endswith("/config"):
            return _resp(
                200, {"featureId": "idp-data-generator", "version": _FEATURE_VERSION}
            )
        return _resp(404, {"error": f"no route for {method} {path}"})
    except json.JSONDecodeError:
        return _resp(400, {"error": "invalid JSON body"})
    except _BadRequest as exc:
        return _resp(400, {"error": str(exc)})
    except _Forbidden as exc:
        return _resp(403, {"error": str(exc)})
    except Exception:  # noqa: BLE001
        logger.exception("feature-api error")
        return _resp(500, {"error": "internal error"})
