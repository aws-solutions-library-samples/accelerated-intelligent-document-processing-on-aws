# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

sqs = boto3.client("sqs")
athena = boto3.client("athena")


lambda_client = boto3.client("lambda")


def _invoke_mlflow_logger(test_run_id, metrics, config=None):
    """Asynchronously invoke the MLflow logger function if configured."""
    mlflow_logger_arn = os.environ.get("MLFLOW_LOGGER_FUNCTION_ARN")
    if not mlflow_logger_arn:
        return

    try:
        payload = {
            "experiment_name": test_run_id,
            "metrics": metrics,
            "params": {
                "test_run_id": test_run_id,
            },
            "tags": {
                "source": "test_results_resolver",
            },
        }

        if config:
            payload["config"] = config

        lambda_client.invoke(
            FunctionName=mlflow_logger_arn,
            InvocationType="Event",  # async, fire-and-forget
            Payload=json.dumps(payload, cls=DecimalEncoder),
        )
        logger.info(f"Invoked MLflow logger for test run: {test_run_id}")
    except Exception as e:
        logger.warning(f"Failed to invoke MLflow logger for {test_run_id}: {e}")


# Custom JSON encoder to handle Decimal objects from DynamoDB
class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super(DecimalEncoder, self).default(obj)


logger = logging.getLogger()


def _as_int(value):
    """DynamoDB returns numbers as Decimal; the GraphQL Int field needs an int."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# SQL-injection defense for Athena queries
# -----------------------------------------
# The Athena f-string queries in this file (marked `# nosec B608`) interpolate
# values that Athena does NOT support as bind parameters. Two distinct contexts
# exist and they need different defenses — conflating them was the bug behind
# issue #619:
#
# 1. IDENTIFIER context (the database name in `FROM "{database}"."table"`).
#    Defended by a strict allow-list, `_validate_sql_input()`:
#      - Pattern: ^[a-zA-Z0-9_\-./]+$  (identifiers, UUID fragments, S3 paths)
#      - No quotes (' "), no whitespace, no semicolons, no parentheses, no `*`
#        — nothing that can close the surrounding double-quoted identifier or
#        terminate the statement. (Hyphens and dots ARE allowed, since real
#        database names need them: `idp1-reporting-db`. That admits the `--`
#        digraph, which is harmless here because the value only ever appears
#        inside double quotes, where it is identifier text rather than the start
#        of a comment.)
#    Identifiers in this solution are infrastructure-controlled (a CloudFormation
#    -supplied env var), so rejecting anything outside the grammar costs nothing.
#
# 2. STRING-LITERAL context (test_run_id in `WHERE document_id LIKE '...'`).
#    test_run_id is derived from a *user-chosen test set name*, so it legally
#    contains spaces, parentheses, apostrophes and the like. Applying the
#    identifier allow-list here rejected perfectly valid runs (e.g.
#    "ConfBench (light noise)-20260813-132501") and broke their metrics
#    aggregation outright. The correct defense for a literal is escaping, not
#    an allow-list: `_sql_literal()` doubles every single quote, which is the
#    complete escape for a Trino/Presto single-quoted literal (backslash is
#    NOT an escape character there, so doubling `'` cannot be bypassed).
#    `_sql_like_prefix()` additionally neutralises the LIKE wildcards `%` and
#    `_` so a name containing them matches literally rather than broadening
#    the prefix match; its queries must pair it with `ESCAPE '\'`.
#
# Bandit's B608 flags string-built SQL generally; it cannot see that every
# interpolated value goes through one of these two helpers. Each `# nosec B608`
# annotation in this file is justified by the preceding `_validate_sql_input()`
# / `_sql_literal()` / `_sql_like_prefix()` calls.
_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-./]+$")

# Escape character paired with every LIKE built by `_sql_like_prefix()`.
_LIKE_ESCAPE = "\\"


def _validate_sql_input(value, name):
    """Validate that a value is safe for use in Athena SQL *identifier* context.

    See the module-level comment above for the rationale and threat model. Use
    `_sql_literal()` instead for values interpolated into a quoted string
    literal — those may legitimately contain characters rejected here.
    """
    if not value or not _SAFE_ID_PATTERN.match(value):
        raise ValueError(f"{name} contains invalid characters: {value}")
    return value


def _sql_literal(value, name):
    """Escape a value for interpolation inside an Athena single-quoted literal.

    Doubling `'` is the complete escape for a Trino/Presto string literal —
    backslash is not an escape character there, so there is no way to smuggle a
    quote past this. NUL is rejected outright since it cannot appear in a
    literal at all.
    """
    if not value:
        raise ValueError(f"{name} must not be empty")
    if "\x00" in value:
        raise ValueError(f"{name} contains invalid characters: {value!r}")
    return value.replace("'", "''")


def _sql_like_prefix(value, name):
    """Escape a value for use as a literal prefix in a `LIKE ... ESCAPE '\\'`.

    Neutralises the LIKE wildcards (`%`, `_`) as well as the escape character
    itself, so a test set name containing them matches literally instead of
    silently widening the prefix. The caller MUST append `ESCAPE '\\'` to the
    LIKE, otherwise the emitted backslashes are matched as data.
    """
    escaped = (
        value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )
    return _sql_literal(escaped, name)


dynamodb = boto3.resource("dynamodb")


def _caller_groups(event):
    """Extract the caller's Cognito groups from the (normalized) identity."""
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return list(groups)


def handler(event, context):
    """Handle both GraphQL resolver and SQS events"""

    # Check if this is an SQS event
    if "Records" in event:
        return handle_cache_update_request(event, context)

    # Otherwise handle as GraphQL resolver
    field_name = event["info"]["fieldName"]

    # Defense-in-depth RBAC: all Test Studio read ops are Admin/Author only in
    # the AppSync schema (@aws_cognito_user_pools(cognito_groups:["Admin",
    # "Author"])). The REST dispatcher's Cognito authorizer only authenticates,
    # so re-enforce the group check here (dispatcher maps PermissionError->403).
    # Direct Lambda invocations (no 'identity' — SDK/CLI/CI automation) are
    # gated by IAM (lambda:InvokeFunction on this ARN), not Cognito groups —
    # same carve-out as abort_test_runs.
    is_api_invoke = event.get("identity") is not None
    groups = _caller_groups(event)
    if is_api_invoke and not ({"Admin", "Author"}.intersection(groups)):
        logger.warning(
            "Forbidden: caller (groups=%s) attempted %s (requires Admin/Author)",
            groups,
            field_name,
        )
        raise PermissionError(
            f"Unauthorized: {field_name} requires Admin or Author group"
        )

    if field_name == "getTestRuns":
        args = event.get("arguments", {})
        start_date_time = args.get("startDateTime")
        end_date_time = args.get("endDateTime")
        time_period_hours = args.get("timePeriodHours", 2)

        if start_date_time and end_date_time:
            start_iso, end_iso = start_date_time, end_date_time
        else:
            end_iso = datetime.utcnow().isoformat() + "Z"
            start_iso = (
                datetime.utcnow() - timedelta(hours=time_period_hours)
            ).isoformat() + "Z"

        logger.info(f"Processing getTestRuns request: {start_iso} → {end_iso}")
        return get_test_runs(start_iso, end_iso)
    elif field_name == "getTestRun":
        test_run_id = event["arguments"]["testRunId"]
        logger.info(f"Processing getTestRun request for test run: {test_run_id}")
        return get_test_results(test_run_id)
    elif field_name == "getTestRunStatus":
        test_run_id = event["arguments"]["testRunId"]
        logger.info(f"Processing getTestRunStatus request for test run: {test_run_id}")
        return get_test_run_status(test_run_id)
    elif field_name == "compareTestRuns":
        test_run_ids = event["arguments"]["testRunIds"]
        logger.info(f"Processing compareTestRuns request for test runs: {test_run_ids}")
        return compare_test_runs(test_run_ids)

    raise ValueError(f"Unknown field: {field_name}")


# Statuses that mean "processing is over" AND that aggregate metrics are
# expected for. ABORTED is terminal too but is deliberately absent: an aborted
# run is not eligible for aggregation, so it must keep reporting ABORTED rather
# than being masked as EVALUATING.
_METRICS_ELIGIBLE_STATUSES = ("COMPLETE", "PARTIAL_COMPLETE")


def _awaiting_metrics(item, status):
    """Is this run finished but still missing its cached aggregate metrics?

    The single definition of the condition behind the ``EVALUATING`` badge. Three
    resolvers surface that badge — ``_build_test_run_list`` (the Executions
    list), ``get_test_run_status`` (the per-row poll) and ``get_test_results``
    (the results page) — and they previously each spelled the rule out inline.
    Issue #619 was diagnosed through the resulting confusion: the badge said
    EVALUATING while the file counts said every document was processed and none
    was evaluating, because "terminal but no metrics" and "actually evaluating"
    render identically. Keep the rule here so the three sites cannot drift.
    """
    if _is_draft_labeling_run(item):
        return False
    return status in _METRICS_ELIGIBLE_STATUSES and not item.get("testRunResult")


def _is_draft_labeling_run(item):
    """True for a run that CREATES a baseline rather than being scored against one.

    Such a run has no baseline by construction — that is what it is producing — so
    the copier skips baseline staging and evaluation never runs. It therefore has
    no aggregate metrics to wait for, ever.

    Treating one as "awaiting metrics" cost twice: it displayed EVALUATING
    indefinitely (observed on a dev stack: a run COMPLETE with 100/100 documents
    processed and CompletedAt set, still badged EVALUATING three days later), and
    every view of it enqueued a full aggregation — re-reading every document's
    results.json from S3 — to compute extraction metrics that are structurally
    empty.

    Prefers the persisted ``Purpose``. Falls back to the Context string only for
    runs created before Purpose was written, which is why the fallback is exact
    rather than a substring match.
    """
    purpose = item.get("Purpose")
    if purpose:
        return purpose == "draft-labeling"
    return item.get("Context") == "Draft labeling run"


def _display_status(item, status):
    """Map a true run status to the status reported to the UI.

    A finished run with no cached metrics reports ``EVALUATING``, because the
    aggregation genuinely is still outstanding — but note the caller is
    responsible for making sure something will actually *do* that aggregation.
    ``get_test_run_status`` and ``get_test_results`` pair this with
    ``_queue_cache_update``; ``_build_test_run_list`` deliberately does not (see
    its own comment), since it renders many runs at once.
    """
    return "EVALUATING" if _awaiting_metrics(item, status) else status


# How long one enqueued cache update suppresses further enqueues for the same
# run. Aggregation re-reads every document's results.json from S3, so it can take
# minutes on a large run; this window keeps concurrent readers (and repeat visits
# to the results page) from stacking up redundant duplicate work, while still
# letting a run whose aggregation genuinely failed retry on a later view.
_CACHE_UPDATE_THROTTLE_SECONDS = 300


def _cache_update_recently_queued(
    item, throttle_seconds=_CACHE_UPDATE_THROTTLE_SECONDS
):
    """Cheap in-memory read of the throttle window from an already-fetched item.

    ``_claim_cache_update_slot`` remains the authoritative guard — only its
    conditional write is atomic against concurrent invocations. This is purely an
    optimisation for the common case: ``get_test_run_status`` is polled every 5
    seconds per in-progress run by the UI, and for a run that is stuck awaiting
    metrics every one of those polls would otherwise issue a conditional
    ``update_item`` that is almost always rejected — and a rejected conditional
    write still consumes write capacity. Callers that already hold the metadata
    item can skip that write entirely.

    Returns False whenever the answer is not clearly "yes" (no timestamp,
    unparseable timestamp), so an unreadable value degrades to attempting the
    authoritative claim rather than silently suppressing recovery.
    """
    queued_at = item.get("CacheUpdateQueuedAt")
    if not queued_at or not throttle_seconds:
        return False
    try:
        parsed = datetime.fromisoformat(queued_at)
    except (TypeError, ValueError):
        logger.warning(
            f"Unparseable CacheUpdateQueuedAt {queued_at!r}; "
            "falling through to the conditional claim"
        )
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - parsed).total_seconds()
    return 0 <= age < throttle_seconds


def _claim_cache_update_slot(test_run_id, throttle_seconds):
    """Atomically claim the right to enqueue a cache update for this run.

    Returns True if the caller won the claim, False if another invocation
    already enqueued one inside the throttle window. Implemented as a
    conditional write on ``CacheUpdateQueuedAt`` so that concurrent Lambda
    invocations — three of them raced during the issue #619 incident, each
    firing its own redundant aggregation — collapse to a single enqueue.

    Fails *open* (returns True) on any unexpected DynamoDB error: a broken
    throttle must never be able to block a legitimate recompute.
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=throttle_seconds)).isoformat()
    try:
        table = dynamodb.Table(os.environ["TRACKING_TABLE"])  # type: ignore[attr-defined]
        table.update_item(
            Key={"PK": f"testrun#{test_run_id}", "SK": "metadata"},
            UpdateExpression="SET CacheUpdateQueuedAt = :now",
            ConditionExpression=(
                "attribute_not_exists(CacheUpdateQueuedAt) "
                "OR CacheUpdateQueuedAt < :cutoff"
            ),
            ExpressionAttributeValues={":now": now.isoformat(), ":cutoff": cutoff},
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        logger.warning(
            f"Cache-update throttle check failed for {test_run_id}, "
            f"enqueueing anyway: {e}"
        )
        return True
    except Exception as e:
        logger.warning(
            f"Cache-update throttle check failed for {test_run_id}, "
            f"enqueueing anyway: {e}"
        )
        return True


def _queue_cache_update(test_run_id, throttle_seconds=_CACHE_UPDATE_THROTTLE_SECONDS):
    """Enqueue an async metrics (re-)aggregation for a test run.

    Best-effort: a failure to enqueue must never fail the read that triggered
    it — the caller still serves whatever cached metrics it has, and the next
    view retries. Consumed by handle_cache_update_request in this same Lambda.

    Enqueues are throttled per run (see ``_claim_cache_update_slot``); pass
    ``throttle_seconds=0`` to force one through.
    """
    try:
        queue_url = os.environ.get("TEST_RESULT_CACHE_UPDATE_QUEUE_URL")
        if not queue_url:
            logger.warning(
                f"TEST_RESULT_CACHE_UPDATE_QUEUE_URL not set; cannot queue "
                f"cache update for {test_run_id}"
            )
            return False
        if throttle_seconds and not _claim_cache_update_slot(
            test_run_id, throttle_seconds
        ):
            logger.info(
                f"Cache update for test run {test_run_id} already enqueued within "
                f"the last {throttle_seconds}s; skipping duplicate"
            )
            return False
        sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps({"testRunId": test_run_id}),
        )
        logger.info(f"Queued cache update for test run: {test_run_id}")
        return True
    except Exception as e:
        logger.warning(f"Failed to queue cache update for {test_run_id}: {e}")
        return False


def handle_cache_update_request(event, context):
    """Process SQS messages to calculate and cache test result metrics"""

    for record in event["Records"]:
        try:
            message = json.loads(record["body"])
            test_run_id = message["testRunId"]

            logger.info(f"Processing cache update for test run: {test_run_id}")

            # Calculate metrics
            aggregated_metrics = _aggregate_test_run_metrics(test_run_id)

            # Cache the metrics (including new confidence_metrics from Stickler v0.4.0+)
            metrics_to_cache = {
                "overallAccuracy": aggregated_metrics.get("overall_accuracy"),
                "weightedOverallScores": aggregated_metrics.get(
                    "weighted_overall_scores", {}
                ),
                "averageConfidence": aggregated_metrics.get("average_confidence"),
                "confidenceMetrics": aggregated_metrics.get("confidence_metrics"),
                "accuracyBreakdown": aggregated_metrics.get("accuracy_breakdown", {}),
                "confusionMatrix": aggregated_metrics.get("confusion_matrix", {}),
                "fieldMetrics": aggregated_metrics.get("field_metrics", {}),
                "splitClassificationMetrics": aggregated_metrics.get(
                    "split_classification_metrics", {}
                ),
                "gradedPacketMetrics": aggregated_metrics.get(
                    "graded_packet_metrics", {}
                ),
                # Per-section classification mismatches. Absent on the Athena
                # fallback path, which has the accuracy percentages but not the
                # per-document detail — hence the default rather than a KeyError.
                "classificationErrors": aggregated_metrics.get(
                    "classification_errors", {}
                ),
                # Documents whose sections all had no extractable schema were
                # dropped from ``weighted_overall_scores`` upstream; expose the
                # count so the UI can render "N excluded" next to the
                # histogram instead of leaving the drop from filesCount
                # unexplained.
                "excludedDocumentCount": aggregated_metrics.get(
                    "excluded_document_count", 0
                ),
                "totalCost": aggregated_metrics.get("total_cost", 0),
                "costBreakdown": aggregated_metrics.get("cost_breakdown", {}),
            }

            table = dynamodb.Table(os.environ["TRACKING_TABLE"])  # type: ignore[attr-defined]
            table.update_item(
                Key={"PK": f"testrun#{test_run_id}", "SK": "metadata"},
                UpdateExpression="SET testRunResult = :metrics",
                ExpressionAttributeValues={
                    ":metrics": float_to_decimal(metrics_to_cache)
                },
            )

            logger.info(f"Successfully cached metrics for test run: {test_run_id}")

        except Exception as e:
            logger.error(
                f"Failed to process cache update for {record.get('body', 'unknown')}: {e}"
            )
            # Don't raise - let other messages in batch continue processing


def float_to_decimal(obj):
    """Convert float values to Decimal for DynamoDB storage"""
    if isinstance(obj, float):
        return Decimal(str(obj))
    elif isinstance(obj, dict):
        return {k: float_to_decimal(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [float_to_decimal(v) for v in obj]
    return obj


def compare_test_runs(test_run_ids):
    """Compare multiple test runs"""
    logger.info(f"Comparing test runs: {test_run_ids}")

    if not test_run_ids or len(test_run_ids) < 2:
        logger.warning(
            f"Insufficient test runs for comparison: {len(test_run_ids) if test_run_ids else 0}"
        )
        return {"metrics": [], "configs": []}

    # Get results for each test run
    results = []
    configs = []

    for test_run_id in test_run_ids:
        logger.info(f"Getting results for test run: {test_run_id}")
        test_result = get_test_results(test_run_id)
        if test_result:
            logger.info(f"Found results for {test_run_id}: {test_result.keys()}")
            results.append(test_result)
            config = _get_test_run_config(test_run_id)
            configs.append({"testRunId": test_run_id, "config": config})
        else:
            logger.warning(f"No results found for test run: {test_run_id}")

    logger.info(f"Total results found: {len(results)}")

    if len(results) < 2:
        logger.warning(f"Insufficient results for comparison: {len(results)}")
        return {"metrics": [], "configs": []}

    metrics_comparison = {result["testRunId"]: result for result in results}
    configs_comparison = _build_config_comparison(configs)

    logger.info(f"Configs data: {configs}")
    logger.info(f"Config comparison result: {configs_comparison}")

    comparison_result = {"metrics": metrics_comparison, "configs": configs_comparison}

    logger.info(f"Final comparison result: {comparison_result}")

    return comparison_result


def _format_datetime(dt_str):
    """Format datetime string for GraphQL AWSDateTime type"""
    if not dt_str:
        return None
    # Add Z suffix if not present
    return dt_str + "Z" if not dt_str.endswith("Z") else dt_str


def _count_completed_documents(table, test_run_id, files):
    """
    Count how many documents completed evaluation successfully.

    Uses batch_get_item for efficiency instead of sequential get_item calls.

    Args:
        table: DynamoDB table resource
        test_run_id: Test run identifier
        files: List of file names in the test run

    Returns:
        int: Number of documents with EvaluationStatus='COMPLETED'
    """
    if not files:
        return 0

    completed_count = 0
    table_name = table.table_name
    dynamodb_client = boto3.client("dynamodb")

    # Build object keys
    object_keys = [f"{test_run_id}/{file_name}" for file_name in files]

    # DynamoDB batch_get_item supports up to 100 keys per batch
    batch_size = 100
    for i in range(0, len(object_keys), batch_size):
        batch = object_keys[i : i + batch_size]
        keys = [{"PK": {"S": f"doc#{key}"}, "SK": {"S": "none"}} for key in batch]

        try:
            response = dynamodb_client.batch_get_item(
                RequestItems={table_name: {"Keys": keys}}
            )

            # Count completed evaluations
            for item in response.get("Responses", {}).get(table_name, []):
                eval_status = item.get("EvaluationStatus", {}).get("S", "").upper()
                if eval_status == "COMPLETED":
                    completed_count += 1

            # Handle unprocessed keys (throttling, etc.)
            unprocessed = response.get("UnprocessedKeys", {}).get(table_name, {})
            if unprocessed:
                logger.warning(
                    f"Batch get had {len(unprocessed.get('Keys', []))} unprocessed keys"
                )

        except Exception as e:
            logger.error(f"Batch get failed for batch starting at index {i}: {str(e)}")

    return completed_count


def get_test_results(test_run_id):
    """Get detailed test results for a specific test run"""
    table = dynamodb.Table(os.environ["TRACKING_TABLE"])  # type: ignore[attr-defined]  # type: ignore[attr-defined]

    # Get test run metadata
    response = table.get_item(Key={"PK": f"testrun#{test_run_id}", "SK": "metadata"})

    if "Item" not in response:
        raise ValueError(f"Test run {test_run_id} not found")

    metadata = response["Item"]
    current_status = metadata.get("Status")

    # Update status if not completed
    if current_status not in ["COMPLETE", "PARTIAL_COMPLETE", "ABORTED"]:
        status_result = get_test_run_status(test_run_id)
        if status_result:
            current_status = status_result["status"]
            # Refresh metadata after status update
            response = table.get_item(
                Key={"PK": f"testrun#{test_run_id}", "SK": "metadata"}
            )
            if "Item" in response:
                metadata = response["Item"]

    # Raise error if status is still not complete
    if current_status not in ["COMPLETE", "PARTIAL_COMPLETE", "ABORTED"]:
        raise ValueError(
            f"Test run {test_run_id} is not complete. Current status: {current_status}"
        )

    # Check if cached results exist and are complete
    cached_metrics = metadata.get("testRunResult")
    if cached_metrics is not None:
        logger.info(f"Retrieved cached metrics for test run: {test_run_id}")

        # Check if cached data is missing keys added by a later release (or was
        # written in a superseded shape) and needs re-aggregation.
        #
        # This is a presence check, so any key added here is by definition
        # absent from every cache written before that key existed — i.e. every
        # historical test run goes stale exactly once when a new key lands.
        # Re-aggregation therefore has to be *additive*: enqueue the recompute
        # and still serve the cached values we do have. Returning nothing
        # instead would resolve getTestRun to null (UI: "No test results
        # found") and silently drop the run from compareTestRuns, permanently
        # — nothing else re-enqueues a cache update for a run whose
        # testRunResult is present but stale.
        #
        # Convergence: handle_cache_update_request always writes every key in
        # metrics_to_cache (defaulting to {}), so one pass through the queue
        # satisfies this guard for good — no re-enqueue loop, even for runs
        # whose aggregation legitimately yields no graded metrics.
        cached_scores = cached_metrics.get("weightedOverallScores")
        if (
            "splitClassificationMetrics" not in cached_metrics
            or "confusionMatrix" not in cached_metrics
            or "fieldMetrics" not in cached_metrics
            or "gradedPacketMetrics" not in cached_metrics
            or "excludedDocumentCount" not in cached_metrics
            or "classificationErrors" not in cached_metrics
            or isinstance(cached_scores, list)
        ):
            logger.info(
                f"Cached metrics incomplete or outdated, queueing re-aggregation "
                f"for test run: {test_run_id} (serving cached values meanwhile)"
            )
            # Recompute off the request path. Aggregation re-reads every
            # document's results.json from S3 and can take minutes on a large
            # run — far longer than the API's read timeout — so doing it
            # inline here would turn a stale cache into a timed-out query.
            # Skip when one is already in flight, using the metadata in hand so
            # revisiting the page doesn't cost a rejected conditional write.
            if not _cache_update_recently_queued(metadata):
                _queue_cache_update(test_run_id)

        # For ABORTED status, count completed files on first call and persist to DB
        completed_files_count = metadata.get("CompletedFiles", 0)
        completed_files_counted = metadata.get("CompletedFilesCounted", False)

        # Only re-count if we haven't counted before (tracked by CompletedFilesCounted flag)
        if current_status == "ABORTED" and not completed_files_counted:
            files = metadata.get("Files", [])
            if files:
                completed_files_count = _count_completed_documents(
                    table, test_run_id, files
                )
                logger.info(
                    f"Counted {completed_files_count} completed documents for aborted test run {test_run_id}"
                )

                # Persist the count and flag to database
                try:
                    table.update_item(
                        Key={"PK": f"testrun#{test_run_id}", "SK": "metadata"},
                        UpdateExpression="SET CompletedFiles = :completed_files, CompletedFilesCounted = :counted",
                        ExpressionAttributeValues={
                            ":completed_files": completed_files_count,
                            ":counted": True,
                        },
                    )
                    logger.info(
                        f"Updated CompletedFiles to {completed_files_count} and set CompletedFilesCounted=True for test run {test_run_id}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to update CompletedFiles for {test_run_id}: {str(e)}"
                    )

        # Use cached metrics but get dynamic fields from current metadata.
        # Keys absent from an older cache default to {} / 0, which every
        # consumer already treats as "no data for this panel".
        return {
            "testRunId": test_run_id,
            "testSetId": metadata.get("TestSetId"),
            "testSetName": metadata.get("TestSetName"),
            "status": current_status,
            "filesCount": metadata.get("FilesCount", 0),
            "completedFiles": completed_files_count,
            "failedFiles": metadata.get("FailedFiles", 0),
            "overallAccuracy": cached_metrics.get("overallAccuracy"),
            "weightedOverallScores": cached_metrics.get("weightedOverallScores", {}),
            "averageConfidence": cached_metrics.get("averageConfidence"),
            "confidenceMetrics": cached_metrics.get("confidenceMetrics"),
            "accuracyBreakdown": cached_metrics.get("accuracyBreakdown", {}),
            "confusionMatrix": cached_metrics.get("confusionMatrix", {}),
            "fieldMetrics": cached_metrics.get("fieldMetrics", {}),
            "splitClassificationMetrics": cached_metrics.get(
                "splitClassificationMetrics", {}
            ),
            "gradedPacketMetrics": cached_metrics.get("gradedPacketMetrics", {}),
            "classificationErrors": cached_metrics.get("classificationErrors", {}),
            "excludedDocumentCount": cached_metrics.get("excludedDocumentCount", 0),
            "totalCost": cached_metrics.get("totalCost", 0),
            "costBreakdown": cached_metrics.get("costBreakdown", {}),
            "createdAt": _format_datetime(metadata.get("CreatedAt")),
            "completedAt": _format_datetime(metadata.get("CompletedAt")),
            "context": metadata.get("Context"),
            "isDraftLabeling": _is_draft_labeling_run(metadata),
            "configVersion": metadata.get("ConfigVersion"),
            "configRevision": _as_int(metadata.get("ConfigRevision")),
            "testSetVersion": metadata.get("TestSetVersion"),
            "testSetDraftVersion": _as_int(metadata.get("TestSetDraftVersion")),
            "config": _get_test_run_config(test_run_id),
        }
    else:
        # No aggregate metrics have been cached yet. This happens when all
        # files finished processing but the evaluation aggregation step hasn't
        # written testRunResult (still running, or it timed out / failed on a
        # large run). Don't raise — that surfaces as an opaque error and the UI
        # spins on "Loading..." forever. Return a structured partial TestRun so
        # the UI can render the in-progress status instead.
        if current_status == "ABORTED":
            logger.info(
                f"Test run {test_run_id} aborted; aggregate metrics not yet available"
            )
        else:
            logger.info(
                f"Test run {test_run_id} processing complete; "
                "aggregate metrics not yet available (evaluation in progress)"
            )
            # Self-heal: re-enqueue the aggregation. Nothing else will — the
            # enqueue in get_test_run_status only fires on a status *transition*,
            # and the stale-cache re-enqueue above only fires when testRunResult
            # already exists. So a run that reached COMPLETE while its one
            # aggregation attempt failed used to stay metric-less forever, which
            # the UI renders as a permanent EVALUATING badge alongside a fully
            # processed file count (issue #619). Throttled, so repeat visits to
            # the results page don't stack up duplicate aggregations — checked
            # against the metadata already in hand to avoid a write per visit.
            if not _cache_update_recently_queued(metadata):
                _queue_cache_update(test_run_id)

        return {
            "testRunId": test_run_id,
            "testSetId": metadata.get("TestSetId"),
            "testSetName": metadata.get("TestSetName"),
            "status": current_status,
            "filesCount": metadata.get("FilesCount", 0),
            "completedFiles": metadata.get("CompletedFiles", 0),
            "failedFiles": metadata.get("FailedFiles", 0),
            "createdAt": _format_datetime(metadata.get("CreatedAt")),
            "completedAt": _format_datetime(metadata.get("CompletedAt")),
            "context": metadata.get("Context"),
            "isDraftLabeling": _is_draft_labeling_run(metadata),
            "configVersion": metadata.get("ConfigVersion"),
            "configRevision": _as_int(metadata.get("ConfigRevision")),
            "testSetVersion": metadata.get("TestSetVersion"),
            "testSetDraftVersion": _as_int(metadata.get("TestSetDraftVersion")),
        }


def _query_test_runs_from_gsi(table, start_iso, end_iso):
    """Query test runs from TypeDateIndex GSI instead of scanning the full table.

    Uses GSI to find testrun keys efficiently, then BatchGetItem for full records
    (GSI projection doesn't include all fields like Context, ConfigVersion, etc.).
    Falls back to scan if GSI query returns no results (backfill may not be complete).
    """
    from boto3.dynamodb.conditions import Key

    gsi_items = []
    query_kwargs = {
        "IndexName": "TypeDateIndex",
        "KeyConditionExpression": Key("ItemType").eq("testrun")
        & Key("InitialEventTime").between(start_iso, end_iso),
        "ScanIndexForward": False,  # Newest first
        "ProjectionExpression": "PK, SK",
    }

    try:
        while True:
            response = table.query(**query_kwargs)
            gsi_items.extend(response.get("Items", []))

            if "LastEvaluatedKey" not in response:
                break
            query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        logger.info(f"GSI query returned {len(gsi_items)} test run keys")

        # If GSI returned results, fetch full records via BatchGetItem
        if gsi_items:
            items = []
            keys = [{"PK": item["PK"], "SK": item["SK"]} for item in gsi_items]
            table_name = table.table_name
            # DynamoDB BatchGetItem supports max 100 keys per call
            for i in range(0, len(keys), 100):
                batch_keys = keys[i : i + 100]
                batch_response = boto3.resource("dynamodb").batch_get_item(
                    RequestItems={table_name: {"Keys": batch_keys}}
                )
                items.extend(batch_response.get("Responses", {}).get(table_name, []))
            logger.info(f"BatchGetItem returned {len(items)} full test run records")
            return items

        # Fallback: GSI may not have ItemType yet (backfill pending).
        # Try scan with CreatedAt filter as fallback.
        logger.info(
            "GSI returned 0 results, falling back to scan (backfill may be pending)"
        )
    except Exception as e:
        logger.warning(f"GSI query failed, falling back to scan: {e}")

    # Fallback scan
    items = []
    scan_kwargs = {
        "FilterExpression": "begins_with(PK, :pk) AND SK = :sk AND CreatedAt >= :start AND CreatedAt <= :end",
        "ExpressionAttributeValues": {
            ":pk": "testrun#",
            ":sk": "metadata",
            ":start": start_iso,
            ":end": end_iso,
        },
    }

    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))

        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    logger.info(f"Fallback scan returned {len(items)} test runs")
    return items


def _build_test_run_list(items):
    """Build sorted test run list from raw DynamoDB items."""
    test_runs = []

    for item in items:
        # Show EVALUATING for completed tests without metrics, but keep ABORTED
        # as-is. Display only: unlike the two single-run resolvers, this does NOT
        # enqueue the missing aggregation, because one list render covers an
        # arbitrary number of runs and fanning out an enqueue per stuck row would
        # turn a page load into a burst of multi-minute Lambda invocations. The
        # per-row poll (getTestRunStatus) drives recovery instead, and the UI
        # renders TestRunnerStatus for every row — so a run visible here is
        # already being healed by the resolver that can afford to do it.
        display_status = _display_status(item, item.get("Status"))

        test_runs.append(
            {
                "testRunId": item["TestRunId"],
                "testSetId": item.get("TestSetId"),
                "testSetName": item.get("TestSetName"),
                "status": display_status,
                "filesCount": item.get("FilesCount", 0),
                "completedFiles": item.get("CompletedFiles", 0),
                "failedFiles": item.get("FailedFiles", 0),
                "createdAt": _format_datetime(item.get("CreatedAt")),
                "completedAt": _format_datetime(item.get("CompletedAt")),
                "context": item.get("Context"),
                "isDraftLabeling": _is_draft_labeling_run(item),
                "configVersion": item.get("ConfigVersion"),
                "configRevision": _as_int(item.get("ConfigRevision")),
                "testSetVersion": item.get("TestSetVersion"),
                "testSetDraftVersion": _as_int(item.get("TestSetDraftVersion")),
            }
        )

    test_runs.sort(
        key=lambda r: r.get("createdAt") or "1970-01-01T00:00:00Z", reverse=True
    )
    return test_runs


def get_test_runs(start_iso, end_iso):
    """Get list of test runs within a date range"""
    table = dynamodb.Table(os.environ["TRACKING_TABLE"])  # type: ignore[attr-defined]

    logger.info(f"Fetching test runs between: {start_iso} and {end_iso}")
    items = _query_test_runs_from_gsi(table, start_iso, end_iso)
    logger.info(f"Test runs found: {len(items)}")

    return _build_test_run_list(items)


def _calculate_completed_at(test_run_id, files, table):
    """Calculate completedAt timestamp from document CompletionTime"""
    latest_completion_time = None

    for file_key in files:
        doc_response = table.get_item(
            Key={"PK": f"doc#{test_run_id}/{file_key}", "SK": "none"}
        )
        if "Item" in doc_response:
            doc_item = doc_response["Item"]
            completion_time = doc_item.get("CompletionTime")
            if completion_time:
                completion_time = completion_time.replace("+00:00", "Z")
                if (
                    not latest_completion_time
                    or completion_time > latest_completion_time
                ):
                    latest_completion_time = completion_time

    return latest_completion_time


def get_test_run_status(test_run_id):
    """Get lightweight status for specific test run - checks both document and evaluation status"""
    table = dynamodb.Table(os.environ["TRACKING_TABLE"])  # type: ignore[attr-defined]

    try:
        logger.info(f"Getting test run status for: {test_run_id}")

        # Get test run metadata
        response = table.get_item(
            Key={"PK": f"testrun#{test_run_id}", "SK": "metadata"}
        )

        if "Item" not in response:
            logger.warning(f"Test run metadata not found for: {test_run_id}")
            return None

        item = response["Item"]
        files = item.get("Files", [])
        files_count = item.get("FilesCount", 0)
        logger.info(f"Test run {test_run_id}: Found {files_count} files")

        # If test run was manually aborted, return ABORTED status without recalculation
        stored_status = item.get("Status", "RUNNING")
        if stored_status == "ABORTED":
            logger.info(f"Test run {test_run_id} is ABORTED, returning stored status")
            return {
                "testRunId": test_run_id,
                "status": "ABORTED",
                "progress": 100,
                "completedFiles": item.get("CompletedFiles", 0),
                "filesCount": files_count,
                "evaluatingFiles": 0,
                "failedFiles": item.get("FailedFiles", 0),
            }

        # Always check actual document status from tracking table
        completed_files = 0
        processing_failed_files = 0  # Only count processing failures found during scan
        evaluating_files = 0
        queued_files = 0

        for file_key in files:
            logger.info(f"Checking file: {file_key} for test run: {test_run_id}")
            doc_response = table.get_item(
                Key={"PK": f"doc#{test_run_id}/{file_key}", "SK": "none"}
            )
            if "Item" in doc_response:
                doc_status = doc_response["Item"].get("ObjectStatus", "QUEUED")
                eval_status = doc_response["Item"].get("EvaluationStatus")
                logger.info(
                    f"File {file_key}: ObjectStatus={doc_status}, EvaluationStatus={eval_status}"
                )

                if doc_status == "COMPLETED":
                    # Check if evaluation is also complete
                    if eval_status == "COMPLETED":
                        completed_files += 1
                        logger.info(f"File {file_key}: counted as completed")
                    elif eval_status == "RUNNING":
                        evaluating_files += 1
                        logger.info(f"File {file_key}: counted as evaluating")
                    elif eval_status is None:
                        # Document completed but evaluation not started yet
                        evaluating_files += 1
                        logger.info(
                            f"File {file_key}: counted as evaluating (eval not started)"
                        )
                    elif eval_status == "FAILED":
                        # Evaluation failed - count as failed
                        processing_failed_files += 1
                        logger.info(f"File {file_key}: counted as failed (eval failed)")
                    elif eval_status == "NO_BASELINE":
                        # No baseline data available - count as completed
                        completed_files += 1
                        logger.info(
                            f"File {file_key}: counted as completed (no baseline data)"
                        )
                    else:
                        # Unknown evaluation status - count as evaluating
                        evaluating_files += 1
                        logger.info(
                            f"File {file_key}: counted as evaluating (unknown eval status: {eval_status})"
                        )
                elif doc_status == "FAILED":
                    processing_failed_files += 1
                    logger.info(f"File {file_key}: counted as failed")
                elif doc_status == "ABORTED":
                    # Count aborted documents as processing failures
                    processing_failed_files += 1
                    logger.info(f"File {file_key}: counted as failed (aborted)")
                elif doc_status == "QUEUED":
                    queued_files += 1
                    logger.info(f"File {file_key}: counted as queued")
                else:
                    logger.info(
                        f"File {file_key}: still processing (status: {doc_status})"
                    )
            else:
                logger.warning(f"Document not found: doc#{test_run_id}/{file_key}")
                # Count missing documents as queued (not yet created)
                queued_files += 1

        # Calculate total failed files
        baseline_failed_files = item.get(
            "BaselineFailedFiles", 0
        )  # Set by copier, never updated
        total_failed_files = (
            baseline_failed_files + processing_failed_files
        )  # Recalculated each call

        logger.info(
            f"Test run {test_run_id} counts: completed={completed_files}, processing_failed={processing_failed_files}, baseline_failed={baseline_failed_files}, total_failed={total_failed_files}, evaluating={evaluating_files}, queued={queued_files}, total={files_count}"
        )

        # Determine overall test run status based on document and evaluation states
        if (
            completed_files == files_count
            and files_count > 0
            and total_failed_files == 0
        ):
            overall_status = "COMPLETE"
        elif (
            total_failed_files > 0
            and (completed_files + total_failed_files + evaluating_files) == files_count
        ):
            overall_status = "PARTIAL_COMPLETE"
        elif evaluating_files > 0:
            overall_status = "EVALUATING"
        elif queued_files == files_count:
            overall_status = "QUEUED"  # All files are still queued
        elif (
            completed_files + total_failed_files + evaluating_files + queued_files
            < files_count
        ):
            overall_status = "RUNNING"  # Some files are actively processing
        else:
            overall_status = item.get("Status", "RUNNING")

        # Auto-update database metadata if calculated status differs from stored status
        stored_status = item.get("Status", "RUNNING")
        if overall_status != stored_status:
            # Calculate completedAt from document completion times if status is complete
            calculated_completed_at = item.get("CompletedAt")
            if (
                overall_status in ["COMPLETE", "PARTIAL_COMPLETE"]
                and not calculated_completed_at
            ):
                calculated_completed_at = _calculate_completed_at(
                    test_run_id, files, table
                )

            logger.info(
                f"Auto-updating test run {test_run_id} status from {stored_status} to {overall_status}"
            )
            try:
                table.update_item(
                    Key={"PK": f"testrun#{test_run_id}", "SK": "metadata"},
                    UpdateExpression="SET #status = :status, #completedAt = :completedAt, CompletedFiles = :completedFiles, FailedFiles = :failedFiles",
                    ExpressionAttributeNames={
                        "#status": "Status",
                        "#completedAt": "CompletedAt",
                    },
                    ExpressionAttributeValues={
                        ":status": overall_status,
                        ":completedAt": calculated_completed_at,
                        ":completedFiles": completed_files,
                        ":failedFiles": total_failed_files,
                    },
                )
                logger.info(
                    f"Successfully updated test run {test_run_id} status to {overall_status}"
                )

                # Queue metric calculation for completed test runs
                if _awaiting_metrics(item, overall_status):
                    _queue_cache_update(test_run_id)

            except Exception as e:
                logger.error(
                    f"Failed to auto-update test run {test_run_id} status: {e}"
                )

        # Report EVALUATING to caller until cached metrics are available
        display_status = _display_status(item, overall_status)
        if _awaiting_metrics(item, overall_status):
            # Self-heal when the status did NOT change on this call — i.e. the
            # transition enqueue above didn't fire, because the run reached its
            # terminal status on an earlier call whose aggregation then failed.
            # Without this the run reports EVALUATING forever while showing every
            # file processed and zero evaluating (issue #619).
            #
            # The UI polls this resolver every 5 seconds per in-progress run, so
            # the throttle window is read from the item already in hand rather
            # than by attempting the conditional write on every poll — the write
            # would be rejected almost every time, and a rejected conditional
            # write still costs write capacity. _queue_cache_update re-checks
            # atomically, so this shortcut can only skip work, never duplicate it.
            if overall_status == stored_status and not _cache_update_recently_queued(
                item
            ):
                _queue_cache_update(test_run_id)

        progress = (
            ((completed_files + total_failed_files) / files_count * 100)
            if files_count > 0
            else 0
        )

        result = {
            "testRunId": test_run_id,
            "status": display_status,
            "filesCount": files_count,
            "completedFiles": completed_files,
            "failedFiles": total_failed_files,
            "evaluatingFiles": evaluating_files,
            "progress": progress,
        }

        logger.info(f"Test run {test_run_id} final result: {result}")
        return result

    except Exception as e:
        logger.error(f"Error getting test run status for {test_run_id}: {e}")
        return None


def _athena_supplements(test_run_id):
    """Fetch the Athena-only extras that supplement a successful Stickler run.

    Returns ``(evaluation_metrics, cost_data)``, each degrading to its documented
    "no data" shape if Athena is unavailable, the reporting tables haven't been
    created yet, or the query fails for any other reason.

    Deliberately swallows every exception. These two values are *additive* —
    split-classification metrics and cost — on top of accuracy/confidence
    metrics that Stickler has already computed successfully. Letting an Athena
    error escape here is what caused issue #619: a run whose Stickler
    aggregation produced perfectly good numbers ended up caching nothing at all,
    which left the UI reporting EVALUATING forever.
    """
    try:
        evaluation_metrics = _get_evaluation_metrics_from_athena(test_run_id)
    except Exception as e:
        logger.warning(
            f"Athena split-classification metrics unavailable for {test_run_id}; "
            f"continuing with Stickler metrics only: {e}"
        )
        evaluation_metrics = {}

    try:
        cost_data = _get_cost_data_from_athena(test_run_id)
    except Exception as e:
        logger.warning(
            f"Athena cost data unavailable for {test_run_id}; reporting zero cost: {e}"
        )
        cost_data = {"total_cost": 0, "cost_breakdown": {}}

    return evaluation_metrics, cost_data


def _aggregate_test_run_metrics(test_run_id):
    """Aggregate metrics using Stickler bulk evaluator (with Athena fallback)"""

    # Fetch config for MLflow logging (best-effort, don't block on failure)
    test_run_config = None
    try:
        test_run_config = _get_test_run_config(test_run_id)
    except Exception as e:
        logger.warning(f"Failed to fetch config for MLflow logging: {e}")

    # Try Stickler-based aggregation via Lambda function
    test_execution_aggregation_arn = os.environ.get(
        "TEST_EXECUTION_AGGREGATION_FUNCTION_ARN"
    )

    if test_execution_aggregation_arn:
        try:
            # Invoke the test execution aggregation function
            response = lambda_client.invoke(
                FunctionName=test_execution_aggregation_arn,
                InvocationType="RequestResponse",
                Payload=json.dumps({"test_run_id": test_run_id}),
            )

            # Parse response
            payload = json.loads(response["Payload"].read())

            if payload.get("statusCode") == 200:
                stickler_metrics = json.loads(payload["body"])

                # If we got valid results, use them and get split metrics and confidence from Athena
                if stickler_metrics.get("document_count", 0) > 0:
                    logger.info(
                        f"Using Stickler aggregation for test run {test_run_id}"
                    )

                    # Get split metrics + cost from Athena. Best-effort: these
                    # only *supplement* the Stickler numbers, so a failure here
                    # must never discard them (issue #619).
                    athena_metrics, cost_data = _athena_supplements(test_run_id)

                    # Prefer Stickler confidence over Athena (Stickler v0.4.0+ has better calibration)
                    stickler_avg_confidence = stickler_metrics.get("average_confidence")
                    athena_avg_confidence = athena_metrics.get("average_confidence")

                    # Use Stickler confidence if available, fallback to Athena
                    avg_confidence = (
                        stickler_avg_confidence
                        if stickler_avg_confidence is not None
                        else athena_avg_confidence
                    )

                    # Merge Stickler metrics with Athena split metrics
                    merged_metrics = {
                        **stickler_metrics,
                        "average_confidence": avg_confidence,
                        "split_classification_metrics": athena_metrics.get(
                            "split_classification_metrics", {}
                        ),
                        "total_cost": cost_data.get("total_cost", 0),
                        "cost_breakdown": cost_data.get("cost_breakdown", {}),
                    }

                    logger.info(
                        f"Confidence source for {test_run_id}: "
                        f"{'Stickler' if stickler_avg_confidence is not None else 'Athena'} "
                        f"(value: {avg_confidence})"
                    )
                    _invoke_mlflow_logger(
                        test_run_id, merged_metrics, config=test_run_config
                    )
                    return merged_metrics
                else:
                    logger.warning(
                        f"Test execution aggregation returned empty metrics (document_count=0) for {test_run_id}, falling back to Athena"
                    )
            else:
                logger.warning(f"Test execution aggregation returned error: {payload}")

        except Exception as e:
            logger.error(
                f"Test execution aggregation Lambda failed for {test_run_id}, falling back to Athena: {e}"
            )
    else:
        logger.info(
            "TEST_EXECUTION_AGGREGATION_FUNCTION_ARN not set, using Athena aggregation"
        )

    # Fallback to Athena-based aggregation
    logger.info(f"Using Athena aggregation for test run {test_run_id}")
    evaluation_metrics = _get_evaluation_metrics_from_athena(test_run_id)
    cost_data = _get_cost_data_from_athena(test_run_id)

    athena_result = {
        "overall_accuracy": evaluation_metrics.get("overall_accuracy"),
        "weighted_overall_scores": evaluation_metrics.get(
            "weighted_overall_scores", {}
        ),
        "average_confidence": evaluation_metrics.get("average_confidence"),
        "accuracy_breakdown": evaluation_metrics.get("accuracy_breakdown", {}),
        "split_classification_metrics": evaluation_metrics.get(
            "split_classification_metrics", {}
        ),
        "total_cost": cost_data.get("total_cost", 0),
        "cost_breakdown": cost_data.get("cost_breakdown", {}),
    }
    _invoke_mlflow_logger(test_run_id, athena_result, config=test_run_config)
    return athena_result


def _get_test_run_config(test_run_id):
    """Get test run configuration from metadata record"""
    table = dynamodb.Table(os.environ["TRACKING_TABLE"])  # type: ignore[attr-defined]
    response = table.get_item(Key={"PK": f"testrun#{test_run_id}", "SK": "metadata"})

    config = response.get("Item", {}).get("Config", {})

    # Convert DynamoDB Decimal objects to regular Python types for JSON serialization
    def convert_decimals(obj):
        if isinstance(obj, dict):
            return {k: convert_decimals(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [convert_decimals(v) for v in obj]
        elif hasattr(obj, "__class__") and obj.__class__.__name__ == "Decimal":
            # Convert Decimal to float or int
            if obj % 1 == 0:
                return int(obj)
            else:
                return float(obj)
        else:
            return obj

    return convert_decimals(config)


def _build_config_comparison(configs):
    """Build configuration differences - compare actual Config structure"""
    if not configs or len(configs) < 2:
        return None

    def get_nested_value(dictionary, path):
        """Get nested value from dictionary using dot notation path"""
        keys = path.split(".")
        current = dictionary
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            elif isinstance(current, list) and key.isdigit():
                # Handle array index access
                index = int(key)
                if 0 <= index < len(current):
                    current = current[index]
                else:
                    return None
            else:
                return None
        return current

    def get_all_paths(dictionary, prefix=""):
        """Get all nested paths from dictionary"""
        paths = []
        ignored_fields = {
            "UpdatedAt",
            "Description",
            "CreatedAt",
            "IsActive",
            "Configuration",
            "version_name",
            "classes",
        }

        for key, value in dictionary.items():
            # Skip ignored metadata fields
            if key in ignored_fields:
                continue

            current_path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                paths.extend(get_all_paths(value, current_path))
            elif isinstance(value, list):
                # Handle arrays by creating indexed paths for each element
                for i, item in enumerate(value):
                    item_path = f"{current_path}.{i}"
                    if isinstance(item, dict):
                        paths.extend(get_all_paths(item, item_path))
                    else:
                        paths.append(item_path)
            else:
                paths.append(current_path)
        return paths

    # Get all possible configuration paths from all configs
    all_paths = set()

    for config_item in configs:
        config = config_item["config"]
        actual_config = config.get("Config", {})
        all_paths.update(get_all_paths(actual_config))

    # Sort paths for consistent ordering with configuration UI
    sorted_paths = sorted(all_paths)

    differences = []
    for path in sorted_paths:
        values = {}
        has_differences = False
        first_value = None

        # Get values for each test run
        for config_item in configs:
            test_run_id = config_item["testRunId"]
            config = config_item["config"]
            actual_config = config.get("Config", {})

            value = get_nested_value(actual_config, path)

            # Always include the value, even if None (missing field)
            if value is None:
                str_value = "<missing>"
            elif isinstance(value, str):
                str_value = value.strip()
            else:
                str_value = str(value).strip()

            values[test_run_id] = str_value

            # Check for differences using normalized values
            if first_value is None:
                first_value = str_value
            elif first_value != str_value:
                has_differences = True

        # Include if there are differences (including missing vs present)
        if has_differences:
            differences.append({"setting": path, "values": values})

    return differences


def _execute_athena_query(query, database):
    """Execute Athena query and return results."""
    try:
        # Get query result location from environment
        result_location = os.environ.get("ATHENA_OUTPUT_LOCATION")

        # Start query execution
        response = athena.start_query_execution(
            QueryString=query,
            QueryExecutionContext={"Database": database},
            ResultConfiguration={"OutputLocation": result_location},
        )

        query_execution_id = response["QueryExecutionId"]

        # Wait for query to complete. Round-25: with the 2-day bounded
        # date window on the cost query (see _get_cost_data_from_athena),
        # queries complete in seconds. 60s poll timeout is generous.
        # (Round-24 briefly widened this to 180s to compensate for the
        # round-7 unbounded date filter; the date-filter revert makes
        # that unnecessary.)
        max_attempts = 30
        final_result = None
        for attempt in range(max_attempts):
            result = athena.get_query_execution(QueryExecutionId=query_execution_id)
            status = result["QueryExecution"]["Status"]["State"]

            if status == "SUCCEEDED":
                final_result = result
                break
            elif status in ["FAILED", "CANCELLED"]:
                error = result["QueryExecution"]["Status"].get(
                    "StateChangeReason", "Unknown error"
                )
                logger.error(f"Athena query failed: {error}")
                return []

            time.sleep(2)
        else:
            # Round-23 review fix (#1416): stop the orphan Athena query
            # before returning — otherwise it keeps scanning bytes and
            # billing indefinitely (up to the workgroup DML timeout,
            # typically 30 min for Athena engine v2/v3). Best-effort;
            # never raise from this fallback path.
            try:
                athena.stop_query_execution(QueryExecutionId=query_execution_id)
                logger.warning(
                    f"Athena query {query_execution_id} timed out after "
                    f"{max_attempts * 2} seconds — stop_query_execution issued."
                )
            except Exception as stop_err:
                logger.warning(
                    f"stop_query_execution({query_execution_id}) failed: {stop_err}"
                )
            return []

        # Round-21 review fix: emit the AthenaBytesScanned control-plane
        # cost metric so this resolver's Athena spend shows up in
        # ``control_plane_hourly`` under component=``test-results``.
        # Before this, the 149 daily invocations of this resolver ran
        # queries whose DataScannedInBytes was NEVER published to
        # CloudWatch — the rollup Lambda saw invocations+duration but
        # ``est_athena_cost=0``. Matches the pattern the analytics-agent
        # ``athena_tool.py`` uses (round-5 review fix wired it there).
        try:
            bytes_scanned = (
                (final_result or {})
                .get("QueryExecution", {})
                .get("Statistics", {})
                .get("DataScannedInBytes")
            )
            if bytes_scanned is not None:
                from idp_common.metrics import emit_control_plane_cost_metric

                emit_control_plane_cost_metric(
                    component="test-results",
                    athena_bytes=int(bytes_scanned),
                )
        except Exception as e:  # nosec — telemetry must not break the resolver
            # WARN, not silent, so a future packaging/import regression is
            # visible in the log instead of returning invisible zeros in
            # control_plane_hourly.
            logger.warning(f"Failed to emit test-results Athena cost metric: {e!r}")

        # Get query results
        results = []
        paginator = athena.get_paginator("get_query_results")

        for page in paginator.paginate(QueryExecutionId=query_execution_id):
            for row in page["ResultSet"]["Rows"][1:]:  # Skip header row
                row_data = {}
                for i, col in enumerate(
                    page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
                ):
                    col_name = col["Name"]
                    value = row["Data"][i].get("VarCharValue")
                    if value is not None:
                        # Try to convert numeric values
                        try:
                            if "." in value:
                                row_data[col_name] = float(value)
                            else:
                                row_data[col_name] = int(value)
                        except ValueError:
                            row_data[col_name] = value
                    else:
                        row_data[col_name] = None
                results.append(row_data)

        return results

    except Exception as e:
        logger.error(f"Error executing Athena query: {e}")
        return []


def _get_evaluation_metrics_from_athena(test_run_id):
    """Get split classification metrics and confidence from Athena"""
    database = os.environ.get("ATHENA_DATABASE")
    if not database:
        logger.warning("ATHENA_DATABASE environment variable not set")
        return {}

    # test_run_id lands in a string-literal context, so it is escaped (it may
    # legitimately contain spaces/parens/quotes from the test set name); the
    # database name is an identifier and keeps the strict allow-list.
    like_prefix = _sql_like_prefix(test_run_id, "test_run_id")
    _validate_sql_input(database, "database")

    # Get only split classification metrics from Athena
    # Other metrics (accuracy, precision, recall, etc.) come from Stickler aggregation
    query = f"""
    SELECT
        SUM(CAST(total_pages AS INT)) as total_pages,
        SUM(CAST(total_splits AS INT)) as total_splits,
        SUM(CAST(correctly_classified_pages AS INT)) as correctly_classified_pages,
        SUM(CAST(correctly_split_without_order AS INT)) as correctly_split_without_order,
        SUM(CAST(correctly_split_with_order AS INT)) as correctly_split_with_order
    FROM "{database}"."document_evaluations"
    WHERE document_id LIKE '{like_prefix}%' ESCAPE '{_LIKE_ESCAPE}'
    """  # nosec B608 - escaped by _sql_like_prefix() / _validate_sql_input()

    results = _execute_athena_query(query, database)

    if not results or not results[0]:
        return {}

    result = results[0]

    # Get confidence data from attribute_evaluations table
    confidence_query = f"""
    SELECT AVG(CAST(confidence AS DOUBLE)) as avg_confidence
    FROM "{database}"."attribute_evaluations"
    WHERE document_id LIKE '{like_prefix}%' ESCAPE '{_LIKE_ESCAPE}'
      AND confidence IS NOT NULL AND confidence != ''
    """  # nosec B608 - escaped by _sql_like_prefix() / _validate_sql_input()

    confidence_results = _execute_athena_query(confidence_query, database)
    avg_confidence = (
        confidence_results[0]["avg_confidence"]
        if confidence_results and confidence_results[0]["avg_confidence"] is not None
        else None
    )

    # Calculate split accuracies from summed counts
    # Athena returns None for SUM() on empty result sets, so default to 0
    total_pages = (
        result.get("total_pages") if result.get("total_pages") is not None else 0
    )
    total_splits = (
        result.get("total_splits") if result.get("total_splits") is not None else 0
    )
    correctly_classified_pages = (
        result.get("correctly_classified_pages")
        if result.get("correctly_classified_pages") is not None
        else 0
    )
    correctly_split_without_order = (
        result.get("correctly_split_without_order")
        if result.get("correctly_split_without_order") is not None
        else 0
    )
    correctly_split_with_order = (
        result.get("correctly_split_with_order")
        if result.get("correctly_split_with_order") is not None
        else 0
    )

    page_level_accuracy = (
        correctly_classified_pages / total_pages if total_pages > 0 else None
    )
    split_accuracy_without_order = (
        correctly_split_without_order / total_splits if total_splits > 0 else None
    )
    split_accuracy_with_order = (
        correctly_split_with_order / total_splits if total_splits > 0 else None
    )

    return {
        "average_confidence": avg_confidence,
        "split_classification_metrics": {
            "page_level_accuracy": page_level_accuracy,
            "split_accuracy_without_order": split_accuracy_without_order,
            "split_accuracy_with_order": split_accuracy_with_order,
            "total_pages": total_pages,
            "total_splits": total_splits,
            "correctly_classified_pages": correctly_classified_pages,
            "correctly_split_without_order": correctly_split_without_order,
            "correctly_split_with_order": correctly_split_with_order,
        },
    }


# Hard ceiling on how many `metering` date partitions the cost query may span.
# The window is normally derived from the run's own lifecycle (see
# `_cost_query_date_filter`), so this only bites on a pathological run — one
# abandoned mid-flight, or whose `CompletedAt` is far in the future because of a
# clock problem. Without it, such a run would scan months of raw metering,
# re-triggering the HIVE_S3_THROTTLING / poll-timeout failure that emptied the
# UI's cost section before.
#
# Not exposed as a CFN parameter — it's a support escape hatch, settable on the
# deployed function without a stack update if a customer legitimately has runs
# spanning more than a week. Parsed defensively so a bad value degrades to the
# default instead of failing Lambda init for every request.
def _cost_query_max_partition_days():
    raw = os.environ.get("COST_QUERY_MAX_PARTITION_DAYS", "7")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            f"COST_QUERY_MAX_PARTITION_DAYS={raw!r} is not an integer; "
            f"using the default of 7 date partitions"
        )
        return 7
    if value < 1:
        logger.warning(
            f"COST_QUERY_MAX_PARTITION_DAYS={value} is below the 1-day floor; using 1"
        )
        return 1
    return value


_COST_QUERY_MAX_PARTITION_DAYS = _cost_query_max_partition_days()


def _lookup_test_run_completed_at(test_run_id):
    """Return the run's `CompletedAt` (ISO string) or None.

    Single-key `get_item` on the run's metadata row. Best-effort: any failure
    returns None and the caller falls back to the fixed 2-day window.
    """
    try:
        table = dynamodb.Table(os.environ["TRACKING_TABLE"])  # type: ignore[attr-defined]
        item = table.get_item(
            Key={"PK": f"testrun#{test_run_id}", "SK": "metadata"},
            ProjectionExpression="CompletedAt",
        ).get("Item")
        value = (item or {}).get("CompletedAt")
        # Only a real ISO string is usable. Anything else (absent, None, or a
        # non-string left by a stubbed client in tests) falls back rather than
        # reaching the parser and logging a spurious warning.
        return value if isinstance(value, str) else None
    except Exception as e:
        logger.warning(
            f"Could not read CompletedAt for {test_run_id} ({e}); cost query "
            f"falls back to the fixed 2-day partition window"
        )
        return None


def _cost_query_date_filter(test_run_id, completed_at=None):
    """Build the `AND date IN (...)` partition filter for the cost query.

    The selective predicate on the cost query is `document_id LIKE '<run>/%'`;
    this filter exists only to bound how much of the `metering` lake Athena has
    to open. Getting it wrong in either direction has bitten us:

    - Too wide (an unbounded upper edge, round-7) scanned days of raw metering,
      hit `HIVE_S3_THROTTLING`, timed out the resolver's poll loop, and left
      Test Studio's "Estimated cost" section empty for every run.
    - Too narrow (a fixed `run_date`, `run_date+1`, round-25) is correct only
      while `metering.date` means *queue* time. Since the Phase-1 partitioning
      change it means **completion** time, so any run whose documents finish
      more than ~24h after the date embedded in its ID — HITL review, a
      throttled or very large batch — silently drops out of the total.

    So bound it by the run's actual lifecycle instead of guessing: from the
    run's start date (parsed from `test_run_id`) through the day its last
    document completed, plus one day. The `+1` covers a metering row written
    just after a `CompletionTime` that sat close to midnight UTC. For a run that
    completes the same day — the overwhelming majority — this yields exactly the
    same two partitions as before, so the common case costs nothing extra.

    Falls back to `run_date`, `run_date+1` when `CompletedAt` is missing or
    unparseable (a still-running run, or a row predating the attribute).

    Returns "" when `test_run_id` carries no parseable date, which leaves the
    query unpruned — pre-existing behavior, unchanged.
    """
    date_match = re.search(r"-(\d{4})(\d{2})(\d{2})-", test_run_id)
    if not date_match:
        return ""

    year, month, day = date_match.groups()
    run_date = datetime(int(year), int(month), int(day), tzinfo=timezone.utc)

    end_date = run_date + timedelta(days=1)
    reason = "no CompletedAt; fixed 2-day fallback window"
    if completed_at:
        try:
            parsed = datetime.fromisoformat(str(completed_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            # +1 day: a document completing at 23:58 has its metering row
            # written moments later, possibly in the next date partition.
            candidate = parsed.astimezone(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)
            # A CompletedAt *before* the run's start date means the ID's date
            # and the tracking row disagree; trust the wider of the two rather
            # than emitting an empty range.
            end_date = max(end_date, candidate)
            reason = f"derived from CompletedAt={completed_at}"
        except (ValueError, TypeError) as e:
            logger.warning(
                f"Unparseable CompletedAt={completed_at!r} for {test_run_id} "
                f"({e}); using the fixed 2-day fallback window"
            )

    span_days = (end_date - run_date).days
    if span_days > _COST_QUERY_MAX_PARTITION_DAYS:
        end_date = run_date + timedelta(days=_COST_QUERY_MAX_PARTITION_DAYS)
        logger.warning(
            f"Cost query window for {test_run_id} clamped from {span_days} to "
            f"{_COST_QUERY_MAX_PARTITION_DAYS} date partitions "
            f"(COST_QUERY_MAX_PARTITION_DAYS). Documents completing after "
            f"{end_date.strftime('%Y-%m-%d')} are NOT counted in the reported "
            f"cost — it is an under-estimate for this run."
        )
        reason = f"clamped to {_COST_QUERY_MAX_PARTITION_DAYS} days"

    dates = []
    cursor = run_date
    while cursor <= end_date:
        dates.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)

    rendered = ", ".join(f"'{d}'" for d in dates)
    logger.info(
        f"Using date filter for cost query: date IN ({rendered}) — {reason}. "
        f"metering.date is COMPLETION time, so the window must reach the run's "
        f"last document completion, not just its start date."
    )
    return f"AND date IN ({rendered})"


def _get_cost_data_from_athena(test_run_id, completed_at=None):
    """Get cost data from Athena metering table.

    ``completed_at`` is the run's `CompletedAt` timestamp, used to size the
    partition window (see `_cost_query_date_filter`). Looked up from the
    tracking table when not supplied.
    """
    database = os.environ.get("ATHENA_DATABASE")
    if not database:
        logger.warning("ATHENA_DATABASE environment variable not set")
        return {"total_cost": 0, "cost_breakdown": {}}

    # See _get_evaluation_metrics_from_athena: literal context for test_run_id,
    # identifier context for the database name.
    like_prefix = _sql_like_prefix(test_run_id, "test_run_id")
    _validate_sql_input(database, "database")

    if completed_at is None:
        completed_at = _lookup_test_run_completed_at(test_run_id)
    date_filter = _cost_query_date_filter(test_run_id, completed_at)

    query = f"""
    SELECT
        context,
        service_api,
        unit,
        SUM(CAST(value AS DOUBLE)) as total_value,
        AVG(CAST(unit_cost AS DOUBLE)) as unit_cost,
        SUM(CAST(estimated_cost AS DOUBLE)) as total_estimated_cost
    FROM "{database}"."metering"
    WHERE document_id LIKE '{like_prefix}/%' ESCAPE '{_LIKE_ESCAPE}'
    {date_filter}
    GROUP BY context, service_api, unit
    """  # nosec B608 - escaped by _sql_like_prefix() / _validate_sql_input()

    results = _execute_athena_query(query, database)

    if not results:
        return {"total_cost": 0, "cost_breakdown": {}}

    cost_breakdown = {}
    total_cost = 0

    for result in results:
        context = result["context"]
        service_api = result["service_api"]
        unit = result["unit"]
        total_value = result["total_value"]
        unit_cost = result["unit_cost"]
        estimated_cost = (
            result["total_estimated_cost"]
            if result["total_estimated_cost"] is not None
            else 0
        )

        if context not in cost_breakdown:
            cost_breakdown[context] = {}

        key = f"{service_api}_{unit}"
        cost_breakdown[context][key] = {
            "unit": unit,
            "value": total_value,
            "unit_cost": unit_cost,
            "estimated_cost": estimated_cost,
        }

        total_cost += estimated_cost

    return {"total_cost": total_cost, "cost_breakdown": cost_breakdown}
