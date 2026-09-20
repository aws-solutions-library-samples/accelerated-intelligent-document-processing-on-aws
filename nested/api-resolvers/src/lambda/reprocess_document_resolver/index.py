# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from idp_common.docs_service import create_document_service
from idp_common.config_scope import (
    ScopeLookupError,
    caller_email_from_claims,
    resolve_allowed_config_versions,
    scope_allows,
)
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
# `allowedConfigVersions` in UsersTable (EmailIndex), reprocessDocument must keep the
# request inside that scope in BOTH directions:
#
#   * **Backward** — the profile each named document was *last processed under*.
#     Checking only the `version` argument made the whole control depend on the
#     caller volunteering it: omit `version` and any `objectKey` in the deployment
#     was reprocessable, including the documents the list resolvers already hide from
#     a scoped caller.
#   * **Forward** — the profile the documents will be re-run *under*. An explicit
#     `version` argument is matched against the scope, as sync_bda_idp_resolver and
#     configuration_resolver do. When it is omitted, a scoped caller's reprocess is
#     **pinned** to the document's own (already-verified) profile, because an
#     unpinned document reaches queue_processor with no `config_version` and is
#     resolved to the *globally active* profile — a value nothing scope-checks, which
#     would move the caller's own document out of their scope and stamp the tracking
#     row accordingly. See `_version_for_document`.
#
# Both directions fail closed. A document whose `ConfigVersion` cannot be read — no
# tracking row, an unstamped row, or a failed GetItem — is refused, because a document
# with no profile name cannot be proven in scope. That is the same rule the list
# resolvers apply, so a scoped caller cannot see such a document to reprocess it.
# Admins and unscoped callers are unaffected in either direction.
_user_scope_cache: dict = {}
_USER_SCOPE_CACHE_TTL = 60  # seconds


def _get_caller_info(event):
    """Extract caller's email and groups from the resolver event identity.

    ``email`` is the config-version scope lookup key and comes from the ``email``
    claim alone — see ``caller_email_from_claims``. ``username`` keeps its
    fallback chain: it is not a scope key.
    """
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    username = claims.get("cognito:username") or claims.get("sub") or ""
    email = caller_email_from_claims(claims)
    return {
        "email": email,
        "username": username,
        "groups": groups,
        "is_admin": "Admin" in groups,
    }


def _get_user_allowed_config_versions(caller_email):
    """The caller's `allowedConfigVersions`, or None for an unrestricted caller.

    Thin wrapper over the shared fail-closed lookup so every consumer of this
    rule resolves it identically. Raises `ScopeLookupError` when the scope cannot
    be evaluated; the caller must deny rather than proceed unrestricted.
    """
    return resolve_allowed_config_versions(
        caller_email,
        users_table_name=os.environ.get("USERS_TABLE_NAME", ""),
        dynamodb=_dynamodb,
        cache=_user_scope_cache,
        cache_ttl=_USER_SCOPE_CACHE_TTL,
    )


def _caller_scope_or_deny(caller):
    """The caller's config-version scope for one request, or a denial.

    Admins are unrestricted and never looked up. For everyone else this fails
    CLOSED: a scope that cannot be *evaluated* (no UsersTable wired, no email
    claim on the verified identity, a failed DynamoDB query) is not a caller
    without restrictions (AUTH.T07).
    """
    if caller["is_admin"]:
        return None
    try:
        return _get_user_allowed_config_versions(caller["email"])
    except ScopeLookupError as e:
        logger.error(
            "Denying reprocessDocument: config-version scope could not be "
            "resolved: %s",
            e,
        )
        raise PermissionError(
            "Unauthorized: your configuration scope could not be verified"
        ) from e


def _document_config_version(object_key):
    """The Configuration Profile a document was last processed under, or None.

    ``None`` means "cannot be established" — no tracking row, a row carrying no
    ``ConfigVersion``, or a read that failed. The caller treats all three the same
    way, because a document with no profile name cannot be proven to be in a
    scoped caller's scope.
    """
    try:
        document = document_service.get_document(object_key)
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Could not read the tracking row for a reprocess target, so its "
            "configuration profile cannot be established: %s",
            e,
        )
        return None
    return getattr(document, "config_version", None) if document else None


def _enforce_document_scope(allowed_versions, object_keys):
    """Refuse a scoped caller any document outside their scope.

    Applies to the profile each document was **last processed under**, so the
    check does not depend on the caller supplying a ``version`` argument. Runs
    before any document is queued, so a batch is refused whole rather than
    part-processed.

    Returns the profile each document currently carries, so the caller can pin the
    reprocess to it — see ``_version_for_document``.
    """
    current: dict = {}
    if not allowed_versions:
        return current
    for object_key in object_keys:
        current_version = _document_config_version(object_key)
        if not scope_allows(allowed_versions, current_version):
            logger.warning(
                "Rejecting reprocessDocument: a requested document was processed "
                "under config version %r, and the caller is scoped to %s",
                current_version,
                sorted(allowed_versions),
            )
            raise PermissionError(
                "Access denied: one or more of the requested documents is "
                "outside your allowed configuration scope"
            )
        current[object_key] = current_version
    return current


def _version_for_document(requested_version, object_key, current_versions):
    """The profile to reprocess one document under.

    An explicit ``version`` argument wins — it has already been scope-checked. When
    none is given, a **scoped** caller's reprocess is pinned to the profile the
    document already carries, which `_enforce_document_scope` has just verified is in
    their scope.

    That pin is the forward half of the same control. Left unpinned, the document
    reaches `queue_processor` with no `config_version`, which resolves the
    **globally active** profile — a value nothing scope-checks, and one that may sit
    outside the caller's scope. Reprocessing would then move the caller's own
    document *out* of their scope, and stamp the tracking row accordingly.

    Unscoped callers and Admins get an empty ``current_versions`` and so keep the
    previous behaviour exactly: no pin, and the active profile is used.
    """
    if requested_version:
        return requested_version
    return current_versions.get(object_key) or None

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

        # RBAC: an Author whose `allowedConfigVersions` restricts them to a subset
        # of profiles must not reprocess outside it, in either direction. Both
        # checks fail closed; an unset scope is unrestricted, which is the opt-in
        # default for single-user and pre-RBAC deployments. See the note at the top
        # of this module for why the document's own profile is checked and not only
        # the argument.
        allowed_versions = _caller_scope_or_deny(caller)

        # (a) the profile the documents would be re-run UNDER, when one is named.
        if version and not scope_allows(allowed_versions, version):
            logger.warning(
                "Rejecting reprocessDocument: caller is scoped to %s but "
                "requested version=%r",
                sorted(allowed_versions),
                version,
            )
            # Raised so the dispatcher answers 403 rather than a 200 with a body.
            raise PermissionError(
                f"Access denied: version '{version}' is not in your allowed scope"
            )

        # (b) the profile each document was last processed under. Independent of
        #     (a): omitting `version` must not stand the check down.
        current_versions = _enforce_document_scope(allowed_versions, object_keys)

        logger.info(
            f"Reprocessing {len(object_keys)} documents"
            + (f" with version: {version}" if version else "")
        )

        # Process each document
        success_count = 0
        for object_key in object_keys:
            try:
                reprocess_document(
                    object_key,
                    _version_for_document(version, object_key, current_versions),
                    revision,
                )
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
