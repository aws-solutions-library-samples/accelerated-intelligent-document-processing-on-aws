# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key as DDBKey
from idp_common.docs_service import create_document_service
from idp_common.config_scope import scope_allows
from idp_common.document_versions import delete_current_output_objects

# Import IDP Common modules
from idp_common.models import Document, Status
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
sqs_client = boto3.client("sqs")
s3_client = boto3.client("s3")
cloudwatch = boto3.client("cloudwatch")
# Namespace for the ``StaleOutputPurgeFailed`` metric emitted on a
# partial-purge failure (parity with queue_sender's #719 metric).
# Set to ``AWS::StackName`` by the template — falls back to ``IDP`` for
# local test contexts where the env var isn't wired.
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "IDP")
_dynamodb = boto3.resource("dynamodb")


# ----- Caller-scope enforcement for multi-user RBAC deployments ----------
# See docs/rbac.md. When a customer assigns a non-admin user
# `allowedConfigVersions` in UsersTable (EmailIndex), reprocessDocument
# MUST NOT accept a `version` argument outside that scope. Mirrors the
# same check performed in sync_bda_idp_resolver and configuration_resolver.
_user_scope_cache: dict = {}
_USER_SCOPE_CACHE_TTL = 60  # seconds


def _get_caller_info(event):
    """Extract caller's email and groups from AppSync event identity."""
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    username = claims.get("cognito:username") or claims.get("sub") or ""
    email = claims.get("email") or identity.get("username") or username
    return {
        "email": email,
        "username": username,
        "groups": groups,
        "is_admin": "Admin" in groups,
    }


def _get_user_allowed_config_versions(caller_email):
    """Look up the caller's `allowedConfigVersions` from UsersTable (TTL-cached)."""
    users_table_name = os.environ.get("USERS_TABLE_NAME", "")
    if not users_table_name or not caller_email:
        return None
    now = time.time()
    cached = _user_scope_cache.get(caller_email)
    if cached and (now - cached["timestamp"]) < _USER_SCOPE_CACHE_TTL:
        return cached["scope"]
    try:
        users_table = _dynamodb.Table(users_table_name)
        resp = users_table.query(
            IndexName="EmailIndex",
            KeyConditionExpression=DDBKey("email").eq(caller_email),
        )
        items = resp.get("Items", [])
        if items:
            scope = items[0].get("allowedConfigVersions")
            result = list(scope) if scope and len(scope) > 0 else None
        else:
            result = None
    except Exception as e:
        logger.warning(f"Failed to look up user scope for {caller_email}: {e}")
        result = None
    _user_scope_cache[caller_email] = {"scope": result, "timestamp": now}
    return result

# Initialize document service (same as queue_sender - defaults to AppSync)
document_service = create_document_service()

# Environment variables
queue_url = os.environ.get("QUEUE_URL")
input_bucket = os.environ.get("INPUT_BUCKET")
output_bucket = os.environ.get("OUTPUT_BUCKET")
retentionDays = int(os.environ.get("DATA_RETENTION_IN_DAYS", "365"))


def _delete_output_data(input_key):
    """Delete previous processing output from the S3 output bucket.

    During full document reprocessing, stale OCR results left in S3 can be
    picked up by the OCR function's retry-safe recovery mechanism
    (``discover_existing_ocr_pages``), causing it to skip OCR instead of
    re-running it with the current configuration.  Deleting the previous
    output ensures OCR (and all downstream steps) execute from scratch.

    This is only called for *full* document reprocessing (the "Reprocess"
    button in the UI).  Step-level reprocessing (classification, extraction)
    goes through a different code path that preserves OCR data intentionally.

    ⚠️ Broad-purge scope note: this calls
    ``delete_current_output_objects`` with the default
    ``subprefixes=None`` (broad purge of ``<key>/*`` preserving only
    ``<key>/runs/``). On deployments where object keys are nested
    (``foo`` reprocessed while a separate document lives at
    ``foo/bar.pdf``), the broad purge deletes the ENTIRE nested
    document — pages/, sections/, summary/, evaluation/, AND runs/ —
    because the preserved-prefix filter only protects THIS document's
    runs/. This is accepted for reprocess (deliberate admin "start
    over" action) but a caller that cannot tolerate nested-doc loss
    should pass a narrower ``subprefixes`` (see queue_sender for the
    ``("pages/",)`` example).
    """
    try:
        deleted = delete_current_output_objects(s3_client, output_bucket, input_key)
        if deleted:
            logger.info(
                f"Deleted {deleted} objects from s3://{output_bucket}/{input_key}/"
            )
        else:
            logger.info(f"No previous output data found for {input_key}")
    except Exception as e:
        # Non-fatal: OCR will still run but may recover stale partial data.
        # Logged at ERROR AND emitted as a CloudWatch metric so an operator
        # can alarm on it without depending on log-scraping — parity with
        # queue_sender's ``StaleOutputPurgeFailed`` metric for the same
        # symptom on the ingest path. A partial purge means reprocess is
        # NOT actually "starting over" cleanly (OCR's retry-safe recovery
        # only needs one surviving complete 4-file page to resurrect
        # stale text). Metric emit is fire-and-forget so telemetry can't
        # affect the reprocess action.
        logger.error(f"Failed to delete previous output data for {input_key}: {e}")
        try:
            cloudwatch.put_metric_data(
                Namespace=METRIC_NAMESPACE,
                MetricData=[
                    {
                        "MetricName": "StaleOutputPurgeFailed",
                        "Value": 1,
                        "Unit": "Count",
                    }
                ],
            )
        except Exception:
            pass  # telemetry must not affect the reprocess flow


def handler(event, context):
    # Log a redacted copy of the event — never the raw event, which contains
    # AppSync identity.claims (Cognito sub, email, groups) and may include
    # attacker-influenced fields (object keys derived from filenames).
    logger.info(
        f"Reprocess resolver invoked with event: "
        f"{json.dumps(sanitize_event_for_logging(event))}"
    )

    try:
        # Validate environment variables
        if not input_bucket:
            raise Exception("INPUT_BUCKET environment variable is not set")
        if not output_bucket:
            raise Exception("OUTPUT_BUCKET environment variable is not set")
        if not queue_url:
            raise Exception("QUEUE_URL environment variable is not set")

        # Extract arguments from GraphQL event
        args = event.get("arguments", {})
        object_keys = args.get("objectKeys", [])
        version = args.get("version")  # Optional version parameter
        revision = args.get("revision")  # Optional revision of that version

        if not object_keys:
            logger.error("objectKeys is required but not provided")
            return False

        # Defense-in-depth RBAC: reprocessDocument is an Admin+Author operation.
        # The schema enforces this via @aws_cognito_user_pools(cognito_groups),
        # but we also gate it server-side so a Viewer can never reach it even if
        # the schema directive is missing/misconfigured.
        caller = _get_caller_info(event)
        if not ({"Admin", "Author"}.intersection(caller["groups"])):
            logger.warning(
                "Forbidden: caller %s (groups=%s) attempted reprocessDocument",
                caller["email"],
                caller["groups"],
            )
            raise PermissionError(
                "Unauthorized: reprocessDocument requires Admin or Author group"
            )

        # RBAC: scope-enforce the `version` argument for non-admins. Authors
        # with `allowedConfigVersions` restricted to a subset of versions
        # must not be able to reprocess documents against a version outside
        # their scope. Admins are unrestricted. If no scope is set, all
        # callers are unrestricted (preserves pre-fix behavior for single-
        # user / pre-RBAC deployments).
        if version:
            if not caller["is_admin"]:
                allowed_versions = _get_user_allowed_config_versions(caller["email"])
                if not scope_allows(allowed_versions, version):
                    logger.warning(
                        "Rejecting reprocessDocument: caller %s is scoped to %s "
                        "but requested version=%r",
                        caller["email"],
                        sorted(allowed_versions),
                        version,
                    )
                    # Raise so AppSync propagates a GraphQL error to the client.
                    raise PermissionError(
                        f"Access denied: version '{version}' is not in your "
                        "allowed scope"
                    )

        logger.info(
            f"Reprocessing {len(object_keys)} documents"
            + (f" with version: {version}" if version else "")
        )

        # Process each document
        success_count = 0
        for object_key in object_keys:
            try:
                reprocess_document(object_key, version, revision)
                success_count += 1
            except Exception as e:
                logger.error(
                    f"Error reprocessing document {object_key}: {str(e)}", exc_info=True
                )
                # Continue with other documents even if one fails

        logger.info(
            f"Successfully queued {success_count}/{len(object_keys)} documents for reprocessing"
        )
        return True

    except Exception as e:
        logger.error(f"Error in reprocess handler: {str(e)}", exc_info=True)
        raise e


def reprocess_document(object_key, version=None, revision=None):
    """
    Reprocess a document by creating a fresh Document object and queueing it.
    This exactly mirrors the queue_sender pattern for consistency and avoids
    S3 copy operations that can trigger duplicate events for large files.

    Args:
        object_key: S3 object key of the document to reprocess
        version: Optional Configuration Profile to use for reprocessing
        revision: Optional revision of that profile. Omit to reprocess under the
            profile's current configuration.
    """
    logger.info(
        f"Reprocessing document: {object_key}"
        + (f" with version: {version}" if version else "")
        + (f" r{revision}" if revision is not None else "")
    )

    # Verify file exists in S3
    try:
        s3_client.head_object(Bucket=input_bucket, Key=object_key)
    except Exception as e:
        raise ValueError(
            f"Document {object_key} not found in S3 bucket {input_bucket}: {str(e)}"
        )

    # Delete previous output data from S3 so the OCR retry-safe recovery
    # mechanism doesn't reinstall stale results from the previous run.
    _delete_output_data(object_key)

    # Create a fresh Document object (same as queue_sender does)
    current_time = datetime.now(timezone.utc).isoformat()

    document = Document(
        id=object_key,  # Document ID is the object key
        input_bucket=input_bucket,
        input_key=object_key,
        output_bucket=output_bucket,
        status=Status.QUEUED,
        queued_time=current_time,
        initial_event_time=current_time,
        pages={},
        sections=[],
        config_version=version,  # Set the configuration version if provided
        config_revision=revision,
    )

    logger.info(f"Created fresh document object for reprocessing: {object_key}")

    # Calculate expiry date (same as queue_sender)
    expires_after = int(
        (datetime.now(timezone.utc) + timedelta(days=retentionDays)).timestamp()
    )

    # Create document in DynamoDB via document service (same as queue_sender - uses AppSync by default)
    logger.info(f"Creating document via document service: {document.input_key}")
    created_key = document_service.create_document(
        document, expires_after=expires_after
    )
    logger.info(f"Document created with key: {created_key}")

    # Send serialized document to SQS queue (same as queue_sender)
    doc_json = document.to_json()
    message = {
        "QueueUrl": queue_url,
        "MessageBody": doc_json,
        "MessageAttributes": {
            "EventType": {"StringValue": "DocumentReprocessed", "DataType": "String"},
            "ObjectKey": {"StringValue": object_key, "DataType": "String"},
        },
    }
    logger.info(f"Sending document to SQS queue: {object_key}")
    response = sqs_client.send_message(**message)
    logger.info(f"SQS response: {response}")

    logger.info(f"Successfully reprocessed document: {object_key}")
    return response.get("MessageId")
