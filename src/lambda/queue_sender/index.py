# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Document ingest: the one place a document is admitted to the pipeline.

This function handles **two** EventBridge event types, and which one arrives
decides what it does:

* ``Object Created`` on the input bucket — an upload. With malware scanning
  disabled (the default) this is the whole story: record the document and send it
  to SQS, exactly as it always has.
* ``GuardDuty Malware Protection Object Scan Result`` — a scan verdict. Only
  delivered when the ``MalwareScanning`` stack parameter is not ``DISABLED``, and
  this is what admits or refuses the document.

With scanning enabled the upload path stops short of SQS and parks the document
in ``MALWARE_SCANNING``; the verdict path moves it to ``QUEUED``, ``QUARANTINED``
or ``MALWARE_SCAN_FAILED``. **Gating here rather than inside the Step Functions
workflow is the point**: a refused document never starts an execution, so its
bytes never reach OCR, page rendering or Bedrock, and it never occupies a
concurrency slot while an asynchronous scan runs.

Both paths share one enqueue implementation on purpose. It carries the #719
stale-output purge, the S3-metadata read that resolves
``config-version``/``submission-source``/``test-set-id``/forced class, the
active-profile fallback, the X-Ray annotation and the TTL calculation — none of
which should exist twice.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from aws_xray_sdk.core import patch_all, xray_recorder

from idp_common.docs_service import create_document_service
from idp_common.document_versions import delete_current_output_objects
from idp_common.job_completion import finalize_job_file, parse_job_document_id
from idp_common.job_service import create_job_service
from idp_common.models import Document, Status
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

# Patch AWS SDK calls for X-Ray tracing
patch_all()

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger("idp_common.bedrock.client").setLevel(
    os.environ.get("BEDROCK_LOG_LEVEL", "INFO")
)
# Get LOG_LEVEL from environment variable with INFO as default

# Initialize clients
sqs = boto3.client("sqs")
s3 = boto3.client("s3")
sns = boto3.client("sns")
cloudwatch = boto3.client("cloudwatch")
# Used only to hand a completed /Jobs batch to `job_tracker` for zipping — see
# `_delegate_job_results`. Created at import like the rest so the client is
# reused across invocations rather than built on a rare path.
lambda_client = boto3.client("lambda")
document_service = create_document_service()
job_service = create_job_service()
queue_url = os.environ["QUEUE_URL"]
retentionDays = int(os.environ["DATA_RETENTION_IN_DAYS"])
# Matches queue_processor's namespace so both concurrency and ingest
# telemetry share the same operator-facing surface.
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "IDP")

# --- Malware scanning (GuardDuty Malware Protection for S3) ------------------

#: ``DISABLED`` | ``GATE_ONLY`` | ``CREATE_PLAN``. Only the distinction between
#: DISABLED and the rest matters here: who *created* the GuardDuty plan changes
#: nothing about reading its verdicts.
MALWARE_SCANNING_MODE = os.environ.get("MALWARE_SCANNING", "DISABLED")
MALWARE_SCANNING_ENABLED = MALWARE_SCANNING_MODE.upper() != "DISABLED"

#: Stack-level ceiling on the config policy. A deployer who has not opted in here
#: cannot have unscannable documents admitted by a config edit alone — the config
#: knob is Admin-editable in the web UI, this is not.
ALLOW_UNSCANNABLE_CEILING = os.environ.get(
    "ALLOW_UNSCANNABLE", "false"
).strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
    "on",
)

ALERTS_TOPIC_ARN = os.environ.get("ALERTS_TOPIC_ARN", "")
INPUT_BUCKET = os.environ.get("INPUT_BUCKET", "")
CONFIG_TABLE = os.environ.get("CONFIG_TABLE", "")

S3_OBJECT_CREATED_DETAIL_TYPE = "Object Created"
GUARDDUTY_SCAN_RESULT_DETAIL_TYPE = "GuardDuty Malware Protection Object Scan Result"

#: GuardDuty PUTs this object into a protected bucket to prove it can write, and
#: it lands as an ordinary ``Object Created`` event. Without this skip it becomes
#: a phantom document in the UI — one per stack, and one more every time the plan
#: revalidates. Skipped unconditionally rather than only when scanning is enabled,
#: because in ``GATE_ONLY`` the plan is managed outside this stack and may be
#: attached (and so write its probe) at any time.
MALWARE_VALIDATION_OBJECT_KEY = "malware-protection-resource-validation-object"

#: GuardDuty's two decisive verdicts. Everything else — ``UNSUPPORTED``,
#: ``ACCESS_DENIED``, ``FAILED``, and any value added to the API later — means
#: "no verdict", and is handled by the fail-closed branch in
#: :func:`decide_admission`.
SCAN_STATUS_COMPLETED = "COMPLETED"
SCAN_RESULT_CLEAN = "NO_THREATS_FOUND"
SCAN_RESULT_THREATS = "THREATS_FOUND"

METRIC_SCAN_CLEAN = "MalwareScanClean"
METRIC_SCAN_THREATS_FOUND = "MalwareScanThreatsFound"
METRIC_SCAN_UNSCANNABLE_BLOCKED = "MalwareScanUnscannableBlocked"
METRIC_SCAN_UNSCANNABLE_ALLOWED = "MalwareScanUnscannableAllowed"
METRIC_SCAN_VERDICT_BEFORE_INGEST = "MalwareScanVerdictBeforeIngest"
METRIC_SCAN_VERDICT_DUPLICATE = "MalwareScanVerdictDuplicate"
METRIC_SCAN_VERDICT_SUPERSEDED = "MalwareScanVerdictSuperseded"
METRIC_SCAN_JOB_FINALIZE_FAILED = "MalwareScanJobFinalizeFailed"

#: The #719 stale-output purge failure. Not a malware metric, but it is emitted
#: through the same helper, so it gets a constant for the same reason the others
#: do: an inline literal is invisible to the gate that checks metrics are
#: dashboarded.
METRIC_STALE_OUTPUT_PURGE_FAILED = "StaleOutputPurgeFailed"

#: Published when a document is HELD at the gate (`MalwareScanHeld`), and when
#: any verdict is applied (`MalwareScanVerdictApplied`).
#:
#: These two exist for the one stall nothing else can see. Every other metric
#: here is published when something is *decided*, so the failure where GuardDuty
#: never answers at all — a plan deleted, a GATE_ONLY stack pointed at a plan
#: that does not cover this bucket, GuardDuty disabled in the Region, the plan's
#: role broken — halts ingest completely while every one of them reads zero. On
#: a gate that fails closed that is the loudest possible failure and the
#: quietest signal: uploads arriving with nothing being decided is only visible
#: as the *absence* of the others, and you cannot alarm on an absence without a
#: series to compare it against.
METRIC_SCAN_HELD = "MalwareScanHeld"
METRIC_SCAN_VERDICT_APPLIED = "MalwareScanVerdictApplied"


def object_version_of(event_object: dict) -> str:
    """The discriminator identifying WHICH upload a verdict belongs to.

    S3's ``version-id`` when the event carries one, else the ETag. Derived the
    same way from the ``Object Created`` event (``detail.object``) and from the
    GuardDuty verdict (``detail.s3ObjectDetails``), which is what makes the two
    comparable — the two services spell the same two fields differently
    (``version-id``/``etag`` against ``versionId``/``eTag``), so both spellings
    are accepted on each side rather than assumed per caller.

    The ETag fallback matters because it is the only field guaranteed present,
    and ``"null"`` is specifically excluded from the version-id side on purpose:
    a bucket with versioning *suspended* reports ``version-id="null"`` for every
    object, which would make every upload look like the same version, and
    ``"null"`` is truthy so a plain ``or`` chain would short-circuit on it. The
    ETag is specific to the object's bytes, so falling through to it when the
    version id is ``"null"`` restores discrimination between distinct files.
    This repository's input bucket has versioning Enabled, so the fallback
    should never engage in practice — but a document that cannot be
    discriminated at all would be admitted by any verdict, so there is no safe
    "unknown" value.
    """

    def _real(value):
        value = str(value or "").strip()
        return "" if value in ("", "null") else value

    return (
        _real(event_object.get("version-id"))
        or _real(event_object.get("versionId"))
        or _real(event_object.get("etag"))
        or _real(event_object.get("eTag"))
        or ""
    )


class ScanVerdictBeforeIngestError(Exception):
    """The verdict arrived before the upload event that creates the document row.

    Raised so the EventBridge rule's ``RetryPolicy`` re-delivers. GuardDuty's scan
    is *triggered by* the same ``Object Created`` event this function consumes, so
    the verdict normally cannot precede it — but EventBridge offers no ordering or
    latency guarantee, and GuardDuty's own ``s3Throttled`` flag exists because
    delays happen. Creating the row here instead would race the upload path into
    overwriting a terminal status with ``MALWARE_SCANNING``, which hangs the
    document for good.
    """


def _put_metric(name: str, value: float = 1.0) -> None:
    """Emit one count. Fire-and-forget: telemetry must never fail an ingest."""
    try:
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[{"MetricName": name, "Value": value, "Unit": "Count"}],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not emit metric {name}: {e}")


def _is_validation_object(object_key: str) -> bool:
    """True for GuardDuty's permission-probe object.

    Matches the bare key and any prefixed form. The key is documented at the
    bucket root, which is where it lands for a whole-bucket plan, but a plan
    scoped to object prefixes writes it under each prefix — so matching only the
    root form would reintroduce the phantom document for anyone using
    ``ObjectPrefixes`` on a ``GATE_ONLY`` plan.
    """
    return object_key == MALWARE_VALIDATION_OBJECT_KEY or object_key.endswith(
        "/" + MALWARE_VALIDATION_OBJECT_KEY
    )


def decide_admission(
    scan_status: str, result_status: str, allow_unscannable: bool
) -> "tuple[Status, str]":
    """Map a GuardDuty verdict to the status to store, and a reason code.

    Pure, so the policy can be tested without DynamoDB, SQS or an event.

    **Default-deny on anything unrecognised.** A document is admitted only on the
    exact pair ``scanStatus=COMPLETED`` and ``scanResultStatus=NO_THREATS_FOUND``.
    An empty result, a value GuardDuty adds in future, and a malformed event all
    fall through to the unscannable branch. The alternative — treating "not
    THREATS_FOUND" as clean — turns every future API addition and every parse slip
    into an open gate. Requiring ``COMPLETED`` as well as the clean result is
    belt-and-braces: the two fields are documented to agree, so a payload where
    they do not is one this code does not understand.
    """
    if scan_status == SCAN_STATUS_COMPLETED and result_status == SCAN_RESULT_CLEAN:
        return Status.QUEUED, "clean"
    if result_status == SCAN_RESULT_THREATS:
        return Status.QUARANTINED, "threats_found"
    if allow_unscannable:
        return Status.QUEUED, "unscannable_allowed"
    return Status.MALWARE_SCAN_FAILED, "unscannable_blocked"


#: The policy when the configuration cannot be read, or names none. Fails closed
#: on the admission question: an unreadable policy is not a permissive one.
#: Alerting defaults ON for the same reason — the safe failure for a notification
#: is an extra message, not a missing one.
_DEFAULT_MALWARE_POLICY = {"allow_unscannable": False, "alert_on_block": True}


def malware_policy_for(config_version=None, config_revision=None) -> "dict":
    """Read the ``malware_scanning`` policy for one document's configuration.

    Read from the document's own Configuration Profile, so the policy applied to
    a document is the one in the configuration version pinned at upload rather
    than whatever is active when the verdict happens to land — which may be
    minutes later and, for a batch, a different version for every file.

    ``allow_unscannable`` needs TWO yeses: the config value and the
    ``AllowUnscannableDocuments`` stack parameter. The config is editable by any
    Admin from the web UI, and a posture that one UI edit can lower is not much
    of a posture — so the stack parameter is a ceiling, not a default.
    """
    policy = dict(_DEFAULT_MALWARE_POLICY)
    try:
        from idp_common.config import get_config

        config = get_config(
            table_name=CONFIG_TABLE or None,
            version=config_version,
            revision=config_revision,
        )
        section = (config or {}).get("malware_scanning") or {}
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"Could not read the malware_scanning policy ({e}); falling back to "
            f"{policy} — unscannable documents stay blocked"
        )
        return policy

    configured_allow = (
        str(section.get("on_unscannable", "block")).strip().lower() == "allow"
    )
    policy["allow_unscannable"] = configured_allow and ALLOW_UNSCANNABLE_CEILING
    if configured_allow and not ALLOW_UNSCANNABLE_CEILING:
        logger.info(
            "Configuration asks to allow unscannable documents, but the "
            "AllowUnscannableDocuments stack parameter is false; blocking"
        )
    # Explicit `is not False` rather than truthiness: the schema default is true,
    # so only a deliberate `false` turns alerting off, and an absent key must not.
    policy["alert_on_block"] = section.get("alert_on_block", True) is not False
    return policy


def _publish_alert(subject: str, message: str) -> None:
    """Publish an operator alert, if a topic is configured.

    Best-effort by design: the document's status is already persisted before this
    runs, so a failure here loses a notification, not a decision. Raising would
    re-deliver the verdict, and the re-delivery takes the duplicate path, which
    would not re-publish anyway.
    """
    if not ALERTS_TOPIC_ARN:
        logger.warning("No ALERTS_TOPIC_ARN configured; not publishing: %s", subject)
        return
    try:
        sns.publish(TopicArn=ALERTS_TOPIC_ARN, Subject=subject[:100], Message=message)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to publish malware alert: {e}")


def resolve_active_config_version(config_table):
    """Return the name of the active Configuration Profile, or None.

    Reads the active-profile pointer item first — one get_item, on a path that
    runs for EVERY document queued. The scan below remains as the fallback for a
    stack that has not activated a profile since the pointer was introduced.

    The fallback paginates. DynamoDB applies the 1MB page size to the items
    EXAMINED, not the items matching FilterExpression, so a single scan call
    finds the active row only when it falls within the first page.
    ProjectionExpression keeps that page as wide as possible: without it the
    scan reads whole config bodies (tens to hundreds of KB each), so only a few
    profiles fit per page and the active one is easily missed. Missing it here
    silently processes the document under the DEFAULT config rather than the
    active one — the same filtered-scan defect as issue #599.
    """
    try:
        pointer = config_table.get_item(
            Key={"Configuration": "Config#__active"},
            ProjectionExpression="ActiveVersion",
        ).get("Item")
        if pointer and pointer.get("ActiveVersion"):
            return str(pointer["ActiveVersion"])
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"Could not read the active-profile pointer ({e}); scanning instead"
        )

    scan_kwargs = {
        "FilterExpression": (
            "begins_with(Configuration, :config_prefix) AND IsActive = :active"
        ),
        "ExpressionAttributeValues": {
            ":config_prefix": "Config#",
            ":active": True,
        },
        "ProjectionExpression": "Configuration",
    }
    while True:
        response = config_table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            # Extract version from Config#v1 format
            config_key = item["Configuration"]
            if "#" in config_key:
                return config_key.split("#", 1)[1]
            return None
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return None
        scan_kwargs["ExclusiveStartKey"] = last_key


def _resolve_config_version(document, object_key):
    """Fill in the active Configuration Profile when the document names none."""
    if document.config_version:
        return
    try:
        config_table = boto3.resource("dynamodb").Table(os.environ["CONFIG_TABLE"])
        document.config_version = resolve_active_config_version(config_table)
        if document.config_version:
            logger.info(
                f"Using active config version {document.config_version} "
                f"for {object_key}"
            )
        else:
            logger.warning(
                f"No active config version found for {object_key} after a "
                "full scan; it will be processed under the default config"
            )
    except Exception as e:
        logger.warning(f"Could not retrieve active config version: {e}")
        document.config_version = None


def _send_to_queue(document, object_key, event_type="DocumentQueued"):
    """Hand a document to the processing queue."""
    doc_json = document.to_json()
    message = {
        "QueueUrl": queue_url,
        "MessageBody": doc_json,
        "MessageAttributes": {
            "EventType": {"StringValue": event_type, "DataType": "String"},
            "ObjectKey": {"StringValue": object_key, "DataType": "String"},
        },
    }
    logger.info(f"Sending document to SQS queue: {object_key}")
    response = sqs.send_message(**message)
    logger.info(f"SQS response: {response}")
    return response


def _purge_stale_outputs(output_bucket, object_key):
    """Remove output left behind by a previous upload of this same key.

    Without this, a re-upload that reuses an existing filename (e.g. replacing
    a W2 with a W3 under `test.pdf`) would let the OCR function's retry-safe
    recovery mechanism (`discover_existing_ocr_pages`) resurrect the previous
    document's OCR results, so classification/extraction would consume text
    from the OLD document and the UI would show stale extraction. Issue #719.

    Scoped to `<key>/pages/` — that's the only subprefix
    ``discover_existing_ocr_pages`` reads, so purging just pages/ fully
    closes #719 while making it impossible for an upload of ``foo`` to
    destroy a nested document at ``foo/bar.pdf/*`` (the reprocess resolver
    keeps the broad ``<key>/*`` purge because its "start over" intent is
    deliberate and the caller is an authenticated admin action).

    No-op on a fresh key. Preserves `<key>/runs/` (version-history manifests).

    Known trade-off (concurrent re-uploads mid-flight): if a prior
    workflow for the same key is still writing to ``pages/*`` when a
    NEW upload arrives, this purge deletes pages the running OCR is
    actively writing. The prior workflow keeps writing whatever pages
    come after the purge, so the new workflow's OCR discovery sees a
    partial subset of stale pages and resurrects them — the resulting
    extraction is a mix of old and new document text. This is a
    regression only in the concurrent-race case (previously the new
    workflow saw ALL of the old document's pages and was uniformly
    wrong; now it can be mixed). A full fix needs content-etag-keyed
    OCR recovery (out of scope for #719). For the non-concurrent
    case — which is what #719 actually covers — the purge is correct.

    Runs on the upload event, not on the malware verdict: it is about the
    previous occupant of the key, which the new upload has already replaced.
    """
    try:
        deleted = delete_current_output_objects(
            s3, output_bucket, object_key, subprefixes=("pages/",)
        )
        if deleted:
            logger.info(
                f"Purged {deleted} stale output objects for re-uploaded key "
                f"s3://{output_bucket}/{object_key}/pages/"
            )
    except Exception as e:
        # Non-fatal: OCR will still run but may recover stale partial data,
        # silently reproducing #719. Logged at ERROR AND emitted as a
        # CloudWatch metric so an operator can alarm on it without
        # depending on log-scraping (matches the round-16 pattern in
        # queue_processor._put_drift_sample). The alternative (raise →
        # EventBridge async retries exhaust → QueueSenderDLQ) is worse:
        # customers would rather see a possibly-stale extraction than
        # have the ingest silently disappear. queue_sender is
        # ``Type: CloudWatchEvent`` (EventBridge Object-Created rule),
        # so retries/DLQ behavior is EventBridge's async-invoke semantics,
        # not an SQS DLQ on the source.
        logger.error(f"Failed to purge previous output data for {object_key}: {e}")
        _put_metric(METRIC_STALE_OUTPUT_PURGE_FAILED)


def _expiry_timestamp():
    return int((datetime.now(timezone.utc) + timedelta(days=retentionDays)).timestamp())


@xray_recorder.capture("queue_sender")  # pyright: ignore[reportCallIssue] - aws-xray-sdk types capture() as the wrapped function, not the decorator factory
def handler(event, context):
    logger.info(f"Processing event: {json.dumps(sanitize_event_for_logging(event))}")

    detail_type = event.get("detail-type") or ""
    if detail_type == GUARDDUTY_SCAN_RESULT_DETAIL_TYPE:
        return handle_scan_result(event)
    if detail_type and detail_type != S3_OBJECT_CREATED_DETAIL_TYPE:
        # Nothing targets this function with anything else, so reaching here means
        # a rule was pointed at it by mistake. Reported and ignored rather than
        # raised: raising would retry forever against an event we will never
        # understand.
        logger.warning(f"Ignoring unexpected detail-type: {detail_type}")
        return {"statusCode": 200, "skipped": "unexpected_detail_type"}
    # No detail-type is treated as an upload: that is the only event this
    # function had for its whole life, and some callers construct the event by
    # hand.
    return handle_object_created(event)


def handle_object_created(event):
    """An upload landed. Record it, and enqueue it unless a scan must clear it."""
    detail = event["detail"]
    object_key = detail["object"]["key"]
    logger.info(f"Processing file: {object_key}")

    # Ignore S3 "folder" pseudo-objects. Creating a folder in the S3 console
    # writes a zero-byte object whose key ends with '/'. These are not real
    # documents, so skip them instead of starting a spurious workflow.
    if object_key.endswith("/"):
        logger.info(f"Skipping folder pseudo-object (key ends with '/'): {object_key}")
        return {"statusCode": 200, "detail": detail, "skipped": "folder_pseudo_object"}

    if _is_validation_object(object_key):
        logger.info(
            f"Skipping GuardDuty malware-protection validation object: {object_key}"
        )
        return {
            "statusCode": 200,
            "detail": detail,
            "skipped": "malware_validation_object",
        }

    # Get output bucket from environment for the document
    output_bucket = os.environ.get("OUTPUT_BUCKET", "")
    if output_bucket == "":
        raise Exception("OUTPUT_BUCKET environment variable not set")

    _purge_stale_outputs(output_bucket, object_key)

    # Create document object - config version will be read from S3 metadata automatically
    current_time = datetime.now(timezone.utc).isoformat()
    document = Document.from_s3_event(event, output_bucket)
    # Held at the gate, or queued immediately when scanning is off. The two
    # branches differ only here and in whether SQS is called below, which is what
    # keeps a DISABLED deployment on exactly the path it was on before.
    document.status = (
        Status.MALWARE_SCANNING if MALWARE_SCANNING_ENABLED else Status.QUEUED
    )
    document.queued_time = current_time
    if MALWARE_SCANNING_ENABLED:
        # Record WHICH upload the gate is now waiting on. A re-upload of the same
        # key overwrites this, so the earlier upload's verdict no longer satisfies
        # the gate's compare-and-set and cannot admit the newer bytes.
        document.malware_scan_version_id = object_version_of(detail.get("object") or {})

    # If no config version found in metadata or filename, get active config version
    _resolve_config_version(document, object_key)

    # Capture X-Ray trace ID for error analysis
    current_segment = xray_recorder.current_segment()
    if current_segment:
        document.trace_id = current_segment.trace_id
        xray_recorder.put_annotation("document_id", document.id)
        logger.info(f"X-Ray trace ID captured: {document.trace_id}")

    # Create document in DynamoDB via document service
    logger.info(f"Creating document via document service: {document.input_key}")

    # Create document in document service with TTL
    created_key = document_service.create_document(
        document, expires_after=_expiry_timestamp()
    )
    logger.info(f"Document created with key: {created_key}")

    if MALWARE_SCANNING_ENABLED:
        # Deliberately NOT sent to SQS. The GuardDuty verdict event is what
        # admits this document; until it arrives the document is visible in the
        # UI as MALWARE_SCANNING and no workflow exists for it.
        logger.info(
            f"Holding {object_key} at the malware gate (mode={MALWARE_SCANNING_MODE})"
        )
        _put_metric(METRIC_SCAN_HELD)
        return {
            "statusCode": 200,
            "detail": detail,
            "document_id": document.id,
            "held_for_malware_scan": True,
        }

    _send_to_queue(document, object_key)
    return {"statusCode": 200, "detail": detail, "document_id": document.id}


def handle_scan_result(event):
    """A GuardDuty verdict arrived. Admit the document, or refuse it."""
    detail = event.get("detail") or {}
    object_details = detail.get("s3ObjectDetails") or {}
    bucket = object_details.get("bucketName") or ""
    object_key = object_details.get("objectKey") or ""

    if not bucket or not object_key:
        logger.error("Scan result carries no s3ObjectDetails bucket/key; ignoring")
        return {"statusCode": 200, "skipped": "malformed_scan_result"}

    if INPUT_BUCKET and bucket != INPUT_BUCKET:
        # The rule filters on the input bucket, so this is defence in depth
        # against a hand-made event or a second plan in the same account.
        logger.info(f"Scan result for another bucket ({bucket}); ignoring")
        return {"statusCode": 200, "skipped": "other_bucket"}

    if _is_validation_object(object_key):
        logger.info("Scan result for the GuardDuty validation object; ignoring")
        return {"statusCode": 200, "skipped": "malware_validation_object"}

    if not MALWARE_SCANNING_ENABLED:
        # Reachable if a stack was switched to DISABLED while a verdict was in
        # flight, or if an externally managed plan still points here. Admitting
        # on the strength of it would be acting on a gate this stack no longer
        # claims to run.
        logger.warning(
            "Received a malware verdict while MalwareScanning is DISABLED; ignoring"
        )
        return {"statusCode": 200, "skipped": "scanning_disabled"}

    scan_status = detail.get("scanStatus") or ""
    result_details = detail.get("scanResultDetails") or {}
    result_status = result_details.get("scanResultStatus") or ""
    threats = result_details.get("threats") or []
    # GuardDuty reports the FIRST detected threat only and then marks the scan
    # COMPLETED, so there is never more than one to record.
    threat_name = None
    if threats and isinstance(threats[0], dict):
        threat_name = threats[0].get("name")
    reasons = [str(r) for r in (result_details.get("statusReasons") or [])]
    verdict_time = event.get("time") or datetime.now(timezone.utc).isoformat()
    verdict_version = object_version_of(object_details)

    output_bucket = os.environ.get("OUTPUT_BUCKET", "")
    if output_bucket == "":
        raise Exception("OUTPUT_BUCKET environment variable not set")

    # The document handed to the pipeline must be the one the un-gated path would
    # have handed it. Neither source alone can produce that, and the invariant is
    # worth stating because the two slips are not obvious:
    #
    #   * The TRACKING ROW does not persist the buckets, the config version or
    #     the forced class. `_document_to_create_item` writes the key, the
    #     status, the timestamps, the submission source and the test-set id, and
    #     nothing else. A row-loaded document therefore carries
    #     `input_bucket=None`, so OCR reads `s3://None/<key>` and fails the
    #     document — and `config_version=None`, so a document uploaded with a
    #     pinned configuration silently processes under the active one instead.
    #   * The VERDICT EVENT has no upload timestamp. Rebuilding from it alone
    #     sets `initial_event_time` to the verdict time, so the document list's
    #     "Submitted" column and every duration from it read minutes late, and
    #     the X-Ray trace id the upload captured is lost.
    #
    # So: build the document exactly as the upload path does — same loader, same
    # S3 metadata read, same active-profile fallback — and then overlay the few
    # things only the row knows.
    document = Document.from_s3_event(
        {
            "detail": {"bucket": {"name": bucket}, "object": {"key": object_key}},
            "time": verdict_time,
        },
        output_bucket,
    )
    _resolve_config_version(document, object_key)

    existing = document_service.get_document(object_key)
    if existing is None:
        # The verdict beat the upload event. Handled the same way as a lost
        # compare-and-set below, and for the same reason: creating the row here
        # would race the upload path into overwriting a terminal status with
        # MALWARE_SCANNING, which hangs the document for good.
        _put_metric(METRIC_SCAN_VERDICT_BEFORE_INGEST)
        raise ScanVerdictBeforeIngestError(
            f"Malware verdict for {object_key} arrived before its document row "
            f"existed; relying on the EventBridge rule's RetryPolicy to re-deliver"
        )
    # `or` rather than unconditional assignment: the row is authoritative for
    # these, but a row written by an older release may not carry them, and a
    # None would be worse than the verdict-derived value.
    document.initial_event_time = (
        existing.initial_event_time or document.initial_event_time
    )
    document.queued_time = existing.queued_time or document.queued_time
    document.trace_id = existing.trace_id or document.trace_id
    document.submission_source = (
        existing.submission_source or document.submission_source
    )
    document.test_set_id = existing.test_set_id or document.test_set_id

    document.malware_scan_status = scan_status
    document.malware_scan_result = result_status
    document.malware_scan_threat = threat_name
    document.malware_scan_reasons = reasons
    document.malware_scan_time = verdict_time

    policy = malware_policy_for(
        config_version=document.config_version,
        config_revision=document.config_revision,
    )
    new_status, reason = decide_admission(
        scan_status, result_status, policy["allow_unscannable"]
    )
    document.status = new_status
    logger.info(
        "Malware verdict for %s: scanStatus=%s result=%s threat=%s reasons=%s "
        "-> %s (%s)",
        object_key,
        scan_status,
        result_status,
        threat_name,
        reasons,
        new_status.value,
        reason,
    )

    # One conditional write lands the verdict and the new status together.
    # Conditional because GuardDuty delivers scan results AT LEAST ONCE: an
    # unconditional write would let a duplicate verdict enqueue the same document
    # a second time. Atomic because two writes would leave a window in which the
    # UI shows QUARANTINED with no threat name to display.
    extra_attributes = {
        "MalwareScanStatus": scan_status,
        "MalwareScanResult": result_status,
        "MalwareScanTime": verdict_time,
        ":scanning": Status.MALWARE_SCANNING.value,
    }
    if threat_name:
        extra_attributes["MalwareScanThreat"] = threat_name
    if reasons:
        extra_attributes["MalwareScanReasons"] = reasons

    # The version clause is what stops a clean verdict for one upload admitting a
    # different one: re-uploading the same key while a scan is in flight gives
    # both uploads one document row, so on the status clause alone v1's clean
    # verdict would admit an unscanned v2 — and v2's own verdict would then be
    # discarded as a duplicate. A superseded verdict now fails the condition and
    # the document keeps waiting for the verdict that belongs to the bytes
    # actually in the bucket.
    #
    # ⚠️ Only added when BOTH sides have a discriminator, which is the usual
    # case here (the input bucket has versioning Enabled, and the ETag fallback
    # covers an object a plan reports without a version id). If either is empty,
    # pinning the condition on empty-equals-empty would be unsatisfiable — the
    # service only writes `MalwareScanVersionId` under a truthiness guard, so
    # the stored value is absent, the compare-and-set never holds, the handler
    # takes the "stored equals verdict" duplicate path and silently drops the
    # verdict. The document would then sit at MALWARE_SCANNING until the
    # no-verdicts alarm fired an hour later. Reaching this needs a
    # versioning-disabled bucket AND a missing ETag, which should not happen,
    # but a silent hour-long stall reported as a 200 is the wrong shape of
    # failure to leave on the floor.
    condition = "attribute_exists(PK) AND #ObjectStatus = :scanning"
    stored_version = (existing.malware_scan_version_id or "") if existing else ""
    if verdict_version and stored_version:
        condition += " AND #MalwareScanVersionId = :verdict_version"
        extra_attributes[":verdict_version"] = verdict_version
        # SET as well as compared: an expression attribute NAME has to be
        # registered by something, and the SET clause is what registers it. The
        # write is a no-op by construction when the condition holds.
        extra_attributes["MalwareScanVersionId"] = verdict_version
    elif verdict_version or stored_version:
        # One side has a discriminator and the other does not: GuardDuty omitted
        # the version from the event, or the bucket is unversioned and the ETag
        # was absent too. Report it so the mismatch is attributable, and fall
        # back to the status clause alone — that preserves the at-least-once
        # guard the condition exists for, and the "stored equals verdict" compare
        # in `_handle_verdict_condition_failure` still routes a true duplicate to
        # the duplicate path.
        logger.warning(
            "Verdict version mismatch for %s: event=%r stored=%r — falling back "
            "to the status clause alone. If this is sustained, something upstream "
            "is not supplying the object version.",
            object_key,
            verdict_version,
            stored_version,
        )

    try:
        document_service.update_document_status(
            document.input_key,
            new_status,
            condition_expression=condition,
            extra_attributes=extra_attributes,
        )
    except Exception as e:  # noqa: BLE001
        if getattr(e, "error_code", "") != "ConditionalCheckFailedException":
            raise
        return _handle_verdict_condition_failure(
            document, object_key, new_status, reason, verdict_version
        )

    if new_status == Status.QUEUED:
        try:
            _send_to_queue(document, object_key)
        except Exception:
            # Put the document back at the gate before re-raising, so the
            # retried delivery finds the condition true and can enqueue it.
            # Without this the status is already QUEUED, the retry takes the
            # duplicate path, and the document sits in QUEUED with no workflow
            # and no SQS message — the silent-stall shape this whole function
            # exists to avoid.
            #
            # ⚠️ The revert is CONDITIONAL on the row still being the admission
            # this invocation wrote (status QUEUED, scan time this verdict's).
            # If the send actually reached SQS and only the response was lost,
            # a retry re-admits and sends a second message; an unconditional
            # revert would then re-open the gate with a message already in
            # flight, so the admission would be delivered twice with the row
            # back in a pre-admission state. Letting the condition fail is the
            # safer answer: nobody else moved the row, so the retry's own
            # compare-and-set will find it where it was left and proceed.
            logger.error(
                "SQS send failed after admitting %s; reverting to MALWARE_SCANNING",
                object_key,
            )
            try:
                document_service.update_document_status(
                    document.input_key,
                    Status.MALWARE_SCANNING,
                    condition_expression=(
                        "#ObjectStatus = :queued AND #MalwareScanTime = :verdict_time"
                    ),
                    extra_attributes={
                        ":queued": Status.QUEUED.value,
                        ":verdict_time": verdict_time,
                        # SET as well as compared, for the same reason as
                        # MalwareScanVersionId on the admission above: an
                        # expression attribute NAME is only registered by a SET
                        # clause, and `update_document_status` registers
                        # `#<Name>` solely for extra_attributes keys that do not
                        # begin with ':'. Without this line `#MalwareScanTime`
                        # reaches DynamoDB undefined and the call raises
                        # ValidationException on EVERY execution — which the
                        # except below logs rather than raises, so the row would
                        # stay QUEUED with nothing in the queue and no way back:
                        # the retried verdict's own condition wants
                        # ObjectStatus=MALWARE_SCANNING and would read the row as
                        # a duplicate. The write is a no-op by construction,
                        # since the condition only holds when the stored value
                        # already equals this one.
                        "MalwareScanTime": verdict_time,
                    },
                )
            except Exception as revert_error:  # noqa: BLE001
                # A condition failure here is the "actually succeeded" case:
                # another delivery (or the SQS message's own workflow) already
                # moved the row. Logged but not re-raised, because we are about
                # to re-raise the original send error for EventBridge to retry.
                code = getattr(revert_error, "error_code", "")
                if code == "ConditionalCheckFailedException":
                    logger.warning(
                        "Not reverting %s: the row is no longer the admission "
                        "this invocation wrote (the SQS send may have reached "
                        "the service despite the error).",
                        object_key,
                    )
                else:
                    logger.error(
                        "Could not revert %s to MALWARE_SCANNING: %s",
                        object_key,
                        revert_error,
                    )
            raise
        _put_metric(METRIC_SCAN_VERDICT_APPLIED)
        _put_metric(
            METRIC_SCAN_UNSCANNABLE_ALLOWED
            if reason == "unscannable_allowed"
            else METRIC_SCAN_CLEAN
        )
        if reason == "unscannable_allowed" and policy["alert_on_block"]:
            # Admitted under an explicit policy, and still worth saying out loud:
            # an unscanned document went into the pipeline.
            _publish_alert(
                f"IDP: unscannable document admitted ({object_key})",
                _refusal_message(document, bucket, object_key, admitted=True),
            )
        return {
            "statusCode": 200,
            "document_id": document.id,
            "malware_verdict": reason,
            "queued": True,
        }

    _handle_refusal(
        document, bucket, object_key, reason, alert=policy["alert_on_block"]
    )
    return {
        "statusCode": 200,
        "document_id": document.id,
        "malware_verdict": reason,
        "queued": False,
    }


def _handle_verdict_condition_failure(
    document, object_key, new_status, reason, verdict_version
):
    """The compare-and-set lost. Work out which of the three reasons it was.

    The condition has two clauses, so a failure has three distinct causes and they
    call for three different responses — collapsing them would either drop a
    verdict that should be retried or re-apply one that should be ignored.
    """
    existing = None
    try:
        existing = document_service.get_document(document.input_key)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not re-read {object_key} after a failed condition: {e}")

    if existing is None:
        _put_metric(METRIC_SCAN_VERDICT_BEFORE_INGEST)
        raise ScanVerdictBeforeIngestError(
            f"Malware verdict for {object_key} arrived before its document row "
            f"existed; relying on the EventBridge rule's RetryPolicy to re-deliver"
        )

    current = getattr(existing.status, "value", existing.status)
    stored_version = existing.malware_scan_version_id or ""

    # SUPERSEDED: the row is still waiting, but for a different upload. Dropped
    # deliberately — this verdict describes bytes that are no longer in the
    # bucket, and applying it would admit or refuse the wrong ones. The document
    # keeps waiting for the verdict belonging to the version actually stored, and
    # the unapplied-verdict alarm covers the case where that never arrives.
    if current == Status.MALWARE_SCANNING.value and stored_version != verdict_version:
        logger.warning(
            "Superseded malware verdict for %s: it describes version %r but the "
            "document is now waiting on version %r (the key was re-uploaded). "
            "Discarding it; the newer upload's own verdict decides.",
            object_key,
            verdict_version,
            stored_version,
        )
        _put_metric(METRIC_SCAN_VERDICT_SUPERSEDED)
        return {
            "statusCode": 200,
            "document_id": document.id,
            "malware_verdict": reason,
            "superseded": True,
        }

    # DUPLICATE: the row has already left MALWARE_SCANNING, so this verdict was
    # applied before (delivery is at-least-once) or a human intervened.
    logger.info(
        "Duplicate malware verdict for %s: row is already %s, this verdict would "
        "have set %s",
        object_key,
        current,
        new_status.value,
    )
    _put_metric(METRIC_SCAN_VERDICT_DUPLICATE)

    # The job bookkeeping is re-attempted even on the duplicate path, and this is
    # the one recovery route it has: it is idempotent (the same file status, the
    # same all-terminal check, the same zip), and if the first delivery failed
    # partway through then without this the /Jobs batch stays IN_PROGRESS forever.
    if new_status in (Status.QUARANTINED, Status.MALWARE_SCAN_FAILED):
        _finalize_job_file_if_job_document(object_key, new_status)

    return {
        "statusCode": 200,
        "document_id": document.id,
        "malware_verdict": reason,
        "duplicate": True,
    }


def _refusal_message(document, bucket, object_key, admitted=False):
    lines = [
        (
            "An unscannable document was ADMITTED to the pipeline."
            if admitted
            else "A document was refused at the malware-scanning ingest gate."
        ),
        "",
        f"Document:        s3://{bucket}/{object_key}",
        f"Status:          {getattr(document.status, 'value', document.status)}",
        f"Scan status:     {document.malware_scan_status or 'unknown'}",
        f"Scan result:     {document.malware_scan_result or 'unknown'}",
    ]
    if document.malware_scan_threat:
        lines.append(f"Threat:          {document.malware_scan_threat}")
    if document.malware_scan_reasons:
        lines.append(f"Reasons:         {', '.join(document.malware_scan_reasons)}")
    lines.append(f"Verdict time:    {document.malware_scan_time or 'unknown'}")
    lines.append("")
    if admitted:
        lines.append(
            "GuardDuty could not scan this file and the configured policy admits "
            "unscannable documents (malware_scanning.on_unscannable=allow with the "
            "AllowUnscannableDocuments stack parameter set to true)."
        )
    else:
        lines.append(
            "The file was NOT processed. It remains in the input bucket, tagged "
            "with its GuardDuty scan result. Review it, then either delete it or "
            "— if it is a legitimate document GuardDuty could not read, such as a "
            "password-protected PDF — re-upload an unprotected copy."
        )
    return "\n".join(lines)


def _handle_refusal(document, bucket, object_key, reason, alert=True):
    """Record and announce a refusal, and stop any /Jobs batch from hanging."""
    _put_metric(METRIC_SCAN_VERDICT_APPLIED)
    _put_metric(
        METRIC_SCAN_THREATS_FOUND
        if reason == "threats_found"
        else METRIC_SCAN_UNSCANNABLE_BLOCKED
    )
    if alert:
        subject = (
            f"IDP: malware detected, document quarantined ({object_key})"
            if reason == "threats_found"
            else f"IDP: document could not be malware-scanned ({object_key})"
        )
        _publish_alert(subject, _refusal_message(document, bucket, object_key))
    _finalize_job_file_if_job_document(object_key, document.status)


def _finalize_job_file_if_job_document(object_key, status):
    """Mark a refused /Jobs API file terminal so its batch can finish.

    ``job_tracker`` is driven by Step Functions execution events, and a refused
    document never starts an execution — so without this the job's ``Files`` map
    keeps the file non-terminal, no ``results.zip`` is written, and
    ``GET /jobs/<id>`` answers ``IN_PROGRESS`` indefinitely.

    ``Status.FAILED`` is what goes into the ``Files`` map rather than the
    document's own ``QUARANTINED``/``MALWARE_SCAN_FAILED``: the terminal-status
    set that reads that map is spelled out twice — ``TERMINAL_FILE_STATUSES`` in
    ``idp_common.job_completion`` and the ``terminal`` literal in
    ``api_handler.compute_job_status`` — so a value neither knows would hang the
    job in a new way. The precise reason stays on the document row.
    """
    parsed = parse_job_document_id(object_key)
    if parsed is None:
        return
    if job_service is None:
        logger.error(
            "Cannot finalize job file for %s: no job service (TRACKING_TABLE unset)",
            object_key,
        )
        return
    job_id, filename = parsed
    output_bucket = os.environ.get("OUTPUT_BUCKET", "")
    try:
        # build_results=False: this function is the ingest gate — 128 MB, a 30 s
        # timeout, and no get/put on the output bucket, because it is invoked
        # once per upload and has never needed them. Zipping a job's outputs is
        # what `job_tracker` carries 3008 MB, 900 s and 5 GB of ephemeral storage
        # for. Granting those here would size the hot ingest path for a job it
        # almost never has to finish.
        result = finalize_job_file(
            job_id=job_id,
            filename=filename,
            status=Status.FAILED,
            job_service=job_service,
            s3_client=s3,
            output_bucket=output_bucket,
            build_results=False,
        )
        logger.info(
            "Finalized job file %s/%s after %s: %s",
            job_id,
            filename,
            getattr(status, "value", status),
            result,
        )
        if result.get("job_complete"):
            # This refusal was the last file to go terminal, so nothing else is
            # coming: `job_tracker` fires on Step Functions state changes and a
            # refused document starts no execution. Without this the job sits at
            # IN_PROGRESS for ever — the exact hang the gate's job handling
            # exists to prevent, just moved one step along.
            _delegate_job_results(job_id)
    except Exception as e:  # noqa: BLE001
        # Not raised: the document's own status is already persisted, and a retry
        # would take the duplicate path. Logged at ERROR and metered so a hung
        # batch is attributable instead of mysterious.
        logger.error(f"Failed to finalize job file {job_id}/{filename}: {e}")
        _put_metric(METRIC_SCAN_JOB_FINALIZE_FAILED)


def _delegate_job_results(job_id):
    """Ask ``job_tracker`` to build the results zip for a job this gate completed.

    An asynchronous (``Event``) invoke of the one function in the stack that is
    already resourced and granted for the job: 3008 MB, a 900 s timeout, 5 GB of
    ephemeral storage and ``S3CrudPolicy`` on the output bucket. The gate has
    none of those and should not acquire them for a path it rarely takes.

    ⚠️ **Failures here are logged and metered, never raised.** Raising would
    re-run the verdict handler, which would take the duplicate path and not
    retry this, so the retry would cost an invocation and change nothing. The
    cost of a failure is bounded: the job's file statuses are already correct, so
    the worst case is the IN_PROGRESS stall that existed before this path, now
    attributable to one named metric instead of being silent. ``job_tracker``
    also still finishes the job by its normal route whenever any *other* file in
    the batch is the last to go terminal, which is the common ordering.
    """
    function_name = os.environ.get("JOB_TRACKER_FUNCTION_NAME", "")
    if not function_name:
        # Expected on a deployment without the Jobs API: `JobTracker` is
        # conditional on DeployApiGateway, and so is this variable. A `jobs/`
        # document cannot exist there, so reaching this is a real
        # misconfiguration and is logged as one.
        logger.error(
            "Cannot delegate results for job %s: JOB_TRACKER_FUNCTION_NAME is "
            "unset, so the job will stay IN_PROGRESS",
            job_id,
        )
        _put_metric(METRIC_SCAN_JOB_FINALIZE_FAILED)
        return
    try:
        lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps({"idpJobCompletion": {"jobId": job_id}}).encode("utf-8"),
        )
        logger.info(
            "Delegated results-zip creation for job %s to %s", job_id, function_name
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Could not delegate results for job %s: %s", job_id, e)
        _put_metric(METRIC_SCAN_JOB_FINALIZE_FAILED)
