# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Circuit Breaker Manager Lambda

Handles circuit breaker state transitions triggered by:
1. CloudWatch Alarm state changes (via SNS)
2. Scheduled health checks (via EventBridge)

DynamoDB Schema (in ConcurrencyTable):
{
    "counter_id": "circuit_breaker",
    "state": "CLOSED" | "OPEN" | "HALF_OPEN",
    "opened_at": "ISO8601 timestamp",
    "last_checked_at": "ISO8601 timestamp",
    "failure_count": number,
    "recovery_attempts": number,
    "last_error": "error message"
}
"""

import json
import logging
import os
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")
sns = boto3.client("sns")
lambda_client = boto3.client("lambda")
cloudwatch = boto3.client("cloudwatch")

CONCURRENCY_TABLE = os.environ["CONCURRENCY_TABLE"]
ALERTS_TOPIC_ARN = os.environ["ALERTS_TOPIC_ARN"]
RECOVERY_TIMEOUT = int(os.environ.get("RECOVERY_TIMEOUT_SECONDS", "300"))
ERROR_HANDLER_ARN = os.environ.get("ERROR_HANDLER_ARN", "")
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "GENAIDP")
APPSYNC_API_URL = os.environ.get("APPSYNC_API_URL", "")

CIRCUIT_BREAKER_ID = "circuit_breaker"
STATE_CLOSED = "CLOSED"
STATE_OPEN = "OPEN"
STATE_HALF_OPEN = "HALF_OPEN"

concurrency_table = dynamodb.Table(CONCURRENCY_TABLE)


def get_circuit_breaker_state() -> dict:
    """Get current circuit breaker state from DynamoDB."""
    try:
        response = concurrency_table.get_item(Key={"counter_id": CIRCUIT_BREAKER_ID})
        item = response.get("Item")
        if not item:
            return {
                "counter_id": CIRCUIT_BREAKER_ID,
                "state": STATE_CLOSED,
                "opened_at": None,
                "last_checked_at": None,
                "failure_count": 0,
                "recovery_attempts": 0,
            }
        return item
    except ClientError as e:
        logger.error(f"Error reading circuit breaker state: {e}")
        raise


def update_circuit_breaker_state(
    new_state: str,
    failure_count: int | None = None,
    recovery_attempts: int | None = None,
    last_error: str | None = None,
    clear_last_error: bool = False,
    expected_state: str | None = None,
) -> bool:
    """Update circuit breaker state in DynamoDB.

    Only attributes passed explicitly are written; `failure_count` and
    `recovery_attempts` are preserved when omitted so that HALF_OPEN transitions
    don't clobber counters accumulated during an outage.

    When ``expected_state`` is supplied, the update is conditional on the
    current persisted state matching it. Concurrent writers losing the race
    get ConditionalCheckFailedException swallowed and return False.

    Returns:
        True if the update was applied, False if the conditional check failed.
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    set_exprs = ["#state = :state", "last_checked_at = :ts"]
    remove_exprs: list[str] = []
    expr_values: dict = {":state": new_state, ":ts": timestamp}
    expr_names = {"#state": "state"}

    if failure_count is not None:
        set_exprs.append("failure_count = :fc")
        expr_values[":fc"] = failure_count
    if recovery_attempts is not None:
        set_exprs.append("recovery_attempts = :ra")
        expr_values[":ra"] = recovery_attempts
    if new_state == STATE_OPEN:
        set_exprs.append("opened_at = :opened")
        expr_values[":opened"] = timestamp
    elif new_state == STATE_CLOSED:
        # Clear the outage timestamp so the UI details panel doesn't show a
        # stale "Opened at" after the breaker has fully recovered.
        remove_exprs.append("opened_at")
    if last_error:
        set_exprs.append("last_error = :err")
        expr_values[":err"] = last_error
    if clear_last_error:
        remove_exprs.append("last_error")

    update_expr = "SET " + ", ".join(set_exprs)
    if remove_exprs:
        update_expr += " REMOVE " + ", ".join(remove_exprs)

    kwargs: dict = {
        "Key": {"counter_id": CIRCUIT_BREAKER_ID},
        "UpdateExpression": update_expr,
        "ExpressionAttributeValues": expr_values,
        "ExpressionAttributeNames": expr_names,
    }

    if expected_state is not None:
        # attribute_not_exists handles the first-ever write when the item
        # hasn't been created yet and the caller expects CLOSED.
        if expected_state == STATE_CLOSED:
            kwargs["ConditionExpression"] = (
                "attribute_not_exists(#state) OR #state = :expected"
            )
        else:
            kwargs["ConditionExpression"] = "#state = :expected"
        expr_values[":expected"] = expected_state

    try:
        concurrency_table.update_item(**kwargs)
        logger.info(f"Circuit breaker state updated to {new_state}")
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info(
                f"Conditional update to {new_state} skipped - "
                f"expected state {expected_state} no longer current"
            )
            return False
        logger.error(f"Error updating circuit breaker state: {e}")
        raise


def publish_notification(state: str, reason: str) -> None:
    """Publish circuit breaker state change to AlertsTopic."""
    message = {
        "circuit_breaker_state": state,
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    subject = f"Circuit Breaker {state}: Bedrock Service"

    try:
        sns.publish(
            TopicArn=ALERTS_TOPIC_ARN, Subject=subject, Message=json.dumps(message, indent=2)
        )
        logger.info(f"Published notification: {subject}")
    except ClientError as e:
        logger.error(f"Error publishing notification: {e}")


def invoke_error_handler(state: str, context: dict) -> None:
    """Invoke optional custom error handler Lambda."""
    if not ERROR_HANDLER_ARN:
        return

    payload = {
        "circuit_breaker_state": state,
        "context": context,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        lambda_client.invoke(
            FunctionName=ERROR_HANDLER_ARN,
            InvocationType="Event",
            Payload=json.dumps(payload),
        )
        logger.info(f"Invoked error handler: {ERROR_HANDLER_ARN}")
    except ClientError as e:
        logger.error(f"Error invoking error handler: {e}")


def put_metric(name: str, value: float) -> None:
    """Publish circuit breaker metric to CloudWatch."""
    try:
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[{"MetricName": name, "Value": value, "Unit": "Count"}],
        )
    except Exception as e:
        logger.warning(f"Failed to publish metric {name}: {e}")


_PUBLISH_MUTATION = """
mutation PublishCircuitBreakerStatus($status: AWSJSON!) {
    publishCircuitBreakerStatus(status: $status) {
        enabled
        state
        openedAt
        lastCheckedAt
        failureCount
        recoveryAttempts
        lastError
    }
}
"""


def publish_to_appsync(state: dict) -> None:
    """Fan out circuit breaker state to UI subscribers via AppSync.

    Fire-and-forget: never raises. State changes originate from multiple sources
    (alarms, scheduled health checks, manual admin actions) and subscribers
    should receive every transition. A mutation failure must not cause the
    DynamoDB update that preceded it to be retried.
    """
    if not APPSYNC_API_URL:
        return

    status_payload = {
        "enabled": True,
        "state": state.get("state"),
        "openedAt": state.get("opened_at"),
        "lastCheckedAt": state.get("last_checked_at"),
        "failureCount": int(state.get("failure_count", 0) or 0),
        "recoveryAttempts": int(state.get("recovery_attempts", 0) or 0),
        "lastError": state.get("last_error"),
    }
    payload = json.dumps(
        {
            "query": _PUBLISH_MUTATION,
            "variables": {"status": json.dumps(status_payload)},
        }
    )

    try:
        session = boto3.Session()
        credentials = session.get_credentials().get_frozen_credentials()
        region = session.region_name or os.environ.get("AWS_REGION", "us-east-1")

        request = AWSRequest(
            method="POST",
            url=APPSYNC_API_URL,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        SigV4Auth(credentials, "appsync", region).add_auth(request)

        urllib_request = Request(
            APPSYNC_API_URL,
            data=payload.encode("utf-8"),
            headers=dict(request.headers),
            method="POST",
        )
        with urlopen(urllib_request, timeout=10) as response:  # noqa: S310  # nosec B310 - APPSYNC_API_URL is a deployment-set env var (https), not user input
            body = json.loads(response.read().decode("utf-8"))
        if "errors" in body:
            logger.warning(f"AppSync publish errors: {body['errors']}")
    except URLError as e:
        logger.warning(f"AppSync publish failed: {e}")
    except Exception as e:
        logger.warning(f"Unexpected error publishing to AppSync: {e}")


def handle_alarm_event(alarm_state: str, alarm_reason: str) -> None:
    """Handle CloudWatch Alarm state change."""
    current = get_circuit_breaker_state()
    current_state = current.get("state", STATE_CLOSED)

    if alarm_state == "ALARM":
        if current_state in [STATE_CLOSED, STATE_HALF_OPEN]:
            failure_count = current.get("failure_count", 0) + 1
            applied = update_circuit_breaker_state(
                STATE_OPEN,
                failure_count=failure_count,
                last_error=alarm_reason,
                expected_state=current_state,
            )
            if not applied:
                return
            publish_notification(
                STATE_OPEN, f"Bedrock service outage detected: {alarm_reason}"
            )
            invoke_error_handler(STATE_OPEN, {"alarm_reason": alarm_reason})
            put_metric("CircuitBreakerOpened", 1)
            publish_to_appsync(get_circuit_breaker_state())
            logger.info(f"Circuit breaker OPENED due to: {alarm_reason}")

    elif alarm_state == "OK":
        if current_state == STATE_OPEN:
            applied = update_circuit_breaker_state(
                STATE_HALF_OPEN, expected_state=STATE_OPEN
            )
            if not applied:
                return
            publish_notification(STATE_HALF_OPEN, "Alarm cleared, testing recovery")
            put_metric("CircuitBreakerHalfOpen", 1)
            publish_to_appsync(get_circuit_breaker_state())
            logger.info("Circuit breaker transitioned to HALF_OPEN")


def handle_health_check() -> None:
    """Handle periodic health check for recovery."""
    current = get_circuit_breaker_state()
    current_state = current.get("state", STATE_CLOSED)

    if current_state == STATE_CLOSED:
        logger.debug("Circuit breaker is CLOSED, no action needed")
        return

    if current_state == STATE_OPEN:
        opened_at = current.get("opened_at")
        if opened_at:
            opened_time = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - opened_time).total_seconds()

            if elapsed >= RECOVERY_TIMEOUT:
                recovery_attempts = current.get("recovery_attempts", 0) + 1
                applied = update_circuit_breaker_state(
                    STATE_HALF_OPEN,
                    recovery_attempts=recovery_attempts,
                    expected_state=STATE_OPEN,
                )
                if not applied:
                    return
                publish_notification(
                    STATE_HALF_OPEN,
                    f"Recovery timeout elapsed ({RECOVERY_TIMEOUT}s), testing service",
                )
                put_metric("CircuitBreakerHalfOpen", 1)
                publish_to_appsync(get_circuit_breaker_state())
                logger.info(
                    f"Circuit breaker transitioned to HALF_OPEN after {elapsed}s"
                )
            else:
                logger.info(
                    f"Recovery timeout not yet elapsed: {elapsed}s / {RECOVERY_TIMEOUT}s"
                )

    elif current_state == STATE_HALF_OPEN:
        logger.info("Circuit breaker in HALF_OPEN, allowing probe traffic")


def _handle_action(event: dict) -> dict:
    """Dispatch manual actions: get_state, reset, manual_open|close|probe."""
    action = event["action"]

    if action == "get_state":
        return {"statusCode": 200, "body": get_circuit_breaker_state()}

    if action == "broadcast":
        # Re-publish current DDB state to AppSync subscribers. Used by writers
        # (e.g. workflow_tracker closing HALF_OPEN → CLOSED) that mutate state
        # themselves but don't want to duplicate the SigV4 publish helper.
        publish_to_appsync(get_circuit_breaker_state())
        return {"statusCode": 200, "body": get_circuit_breaker_state()}

    if action == "reset":
        update_circuit_breaker_state(
            STATE_CLOSED,
            failure_count=0,
            recovery_attempts=0,
            clear_last_error=True,
        )
        publish_notification(STATE_CLOSED, "Manual reset")
        put_metric("CircuitBreakerClosed", 1)
        publish_to_appsync(get_circuit_breaker_state())
        return {"statusCode": 200, "body": "Circuit breaker reset"}

    reason = event.get("reason", "").strip() or "(no reason provided)"
    user = event.get("user", "unknown")

    if action == "manual_open":
        # CLOSED|HALF_OPEN → OPEN. Race-safe via expected_state.
        # failure_count is deliberately NOT incremented: manual pauses are
        # operator actions, not Bedrock failures.
        current = get_circuit_breaker_state()
        current_state = current.get("state", STATE_CLOSED)
        if current_state == STATE_OPEN:
            return {"statusCode": 200, "body": get_circuit_breaker_state()}
        applied = update_circuit_breaker_state(
            STATE_OPEN,
            last_error=f"Manual pause by {user}: {reason}",
            expected_state=current_state,
        )
        if applied:
            publish_notification(
                STATE_OPEN, f"Manual pause by {user}: {reason}"
            )
            put_metric("CircuitBreakerOpened", 1)
            publish_to_appsync(get_circuit_breaker_state())
        return {"statusCode": 200, "body": get_circuit_breaker_state()}

    if action == "manual_close":
        # Unconditional: admins explicitly override from any prior state.
        update_circuit_breaker_state(
            STATE_CLOSED,
            failure_count=0,
            recovery_attempts=0,
            clear_last_error=True,
        )
        publish_notification(
            STATE_CLOSED, f"Manual override by {user}: {reason}"
        )
        put_metric("CircuitBreakerClosed", 1)
        publish_to_appsync(get_circuit_breaker_state())
        return {"statusCode": 200, "body": get_circuit_breaker_state()}

    if action == "manual_probe":
        # OPEN → HALF_OPEN. No-op if not currently OPEN.
        current = get_circuit_breaker_state()
        current_state = current.get("state", STATE_CLOSED)
        if current_state != STATE_OPEN:
            return {"statusCode": 200, "body": get_circuit_breaker_state()}
        recovery_attempts = current.get("recovery_attempts", 0) + 1
        applied = update_circuit_breaker_state(
            STATE_HALF_OPEN,
            recovery_attempts=recovery_attempts,
            expected_state=STATE_OPEN,
        )
        if applied:
            publish_notification(
                STATE_HALF_OPEN, f"Manual probe by {user}: {reason}"
            )
            put_metric("CircuitBreakerHalfOpen", 1)
            publish_to_appsync(get_circuit_breaker_state())
        return {"statusCode": 200, "body": get_circuit_breaker_state()}

    return {"statusCode": 400, "body": f"Unknown action: {action}"}


def handler(event: dict, context) -> dict:
    """Lambda handler for circuit breaker management."""
    logger.info(f"Circuit breaker event: {json.dumps(event)}")

    try:
        if "Records" in event:
            for record in event["Records"]:
                if record.get("EventSource") == "aws:sns":
                    message = json.loads(record["Sns"]["Message"])
                    alarm_state = message.get("NewStateValue", "")
                    alarm_reason = message.get("NewStateReason", "")
                    handle_alarm_event(alarm_state, alarm_reason)

        elif event.get("source") == "scheduled" and event.get("action") == "health_check":
            handle_health_check()

        elif "action" in event:
            return _handle_action(event)

        return {"statusCode": 200, "body": "Processed"}

    except Exception as e:
        logger.error(f"Error in circuit breaker handler: {e}", exc_info=True)
        raise
