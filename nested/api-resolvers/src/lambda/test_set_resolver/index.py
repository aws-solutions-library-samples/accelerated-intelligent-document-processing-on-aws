import concurrent.futures
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from idp_common.dynamodb import DynamoDBClient  # type: ignore
from idp_common.evaluation.confidence_curve import (  # type: ignore
    DEFAULT_FIELDS_PER_DOC,
    DEFAULT_PAGES_PER_DOC,
    estimate_for_target,
)
from idp_common.evaluation.curve_store import CurveStore  # type: ignore
from idp_common.models import Status  # type: ignore
from idp_common.s3 import find_matching_files  # type: ignore
from idp_common.testset_scope import (  # type: ignore
    assert_can_access_test_set,
    caller_email,
)

# Constants
MAX_ZIP_SIZE_BYTES = 1073741824  # 1 GB

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def validate_test_set_name(name):
    """Validate test set name: alphanumeric, spaces, hyphens, underscores only, max 50 chars"""
    if not name or not isinstance(name, str):
        return False
    return re.match(r"^[a-zA-Z0-9\s_-]+$", name) and len(name) <= 50


# Document class names come from a config's `classes`. All 120 shipped across
# config_library use letters, digits, spaces, hyphens and underscores and nothing
# else ("Bank Statement", "Bank-Statement", "PA-Claims-Evidence", "BANK_CHECK"),
# so the character set is exactly that — no dot and no slash, which a first draft
# allowed and which let "../../etc/passwd" through.
#
# A literal space rather than `\s`, which admits newlines and tabs. And matched with
# `fullmatch`, not `match`: `$` matches BEFORE a trailing newline, so
# "Bank Statement\n" passes `^...$` even with the space literal. A test caught that
# after this comment had already claimed otherwise.
#
# ASCII-only matters concretely: the value is carried as S3 object *user metadata*,
# where a non-ASCII class name surfaced as a botocore failure and a 500 rather
# than a clean rejection.
_DOCUMENT_CLASS_RE = re.compile(r"[a-zA-Z0-9 _-]+")
_DOCUMENT_CLASS_MAX_LEN = 100


def validate_document_class(document_class):
    """Validate a caller-supplied document class before it reaches S3 metadata.

    ``reextractTestSetDocument`` is reachable by the **Annotator** group, the
    lowest-privilege role, and its ``documentClass`` flows reviewer → resolver →
    ``generate_draft_labels`` → SQS → S3 user metadata → ``Document.from_s3_event``
    → a forced class and a forced section. Nothing along that path constrained it,
    and the dispatcher deliberately does not deep-validate nested input fields.

    This bounds the shape only. It deliberately does NOT check membership of the
    deployment's configured classes: this Lambda has no CONFIGURATION_TABLE grant,
    so that check needs an env var and a read permission it does not currently
    have. A well-formed but unconfigured class therefore still yields a section
    with no schema and so no extracted fields — visibly wrong in the UI rather
    than silently wrong. Worth closing separately.
    """
    if document_class is None or document_class == "":
        return True  # Optional: absence means "leave the class alone".
    if not isinstance(document_class, str):
        return False
    if len(document_class) > _DOCUMENT_CLASS_MAX_LEN:
        return False
    return bool(_DOCUMENT_CLASS_RE.fullmatch(document_class))


def validate_description(description):
    """Validate description: max 500 chars only"""
    if description is None or description == "":
        return True  # Optional field
    if not isinstance(description, str):
        return False
    return len(description) <= 500


# Two S3 clients, deliberately. They differ only in endpoint, and conflating
# them is what made Test Studio return an opaque 504 in private deployments.
#
# S3_ENDPOINT_URL (set only when S3PresignedUrlViaVpcEndpoint=true or a BYO
# endpoint override is configured) is the *S3 interface VPC endpoint* hostname.
# Its purpose is the URL string handed to the BROWSER: presigning is an offline
# signing operation, so a Lambda can mint a VPCE-hosted URL from anywhere.
#
# Data-plane calls are the opposite. This function is NOT VPC-attached (see the
# cfn_nag/checkov notes on TestSetResolverFunction in the nested template), and
# a VPCE hostname resolves to the endpoint's PRIVATE addresses. Sending LIST /
# GET / PUT there from outside the VPC does not fail — it hangs until the
# connect timeout, and since REST API Gateway abandons an integration at 29s the
# browser only ever sees `504`, with nothing in the resolver log to explain it.
# getTestSets is where this bites first: it lists the whole test-set bucket plus
# ~4 more calls per prefix on every poll.
#
# So: presign with the endpoint, talk to S3 without it. The regular client also
# matches what the rest of the codebase already does from these Lambdas
# (idp_common.s3 builds a plain client), which is why only the calls made
# through this module were affected.
_s3_endpoint_url = os.environ.get("S3_ENDPOINT_URL") or None
_s3_addressing = "virtual" if _s3_endpoint_url else "path"

# Browser-facing presigned POST/GET URLs only.
s3_presign_config = Config(
    signature_version="s3v4",
    s3={"addressing_style": _s3_addressing},
)
s3_presign_client = boto3.client(
    "s3", endpoint_url=_s3_endpoint_url, config=s3_presign_config
)

# Bounds for the data-plane client, applied ONLY where there is private-network
# S3 configuration to get wrong. That is the whole failure this guards: an
# endpoint or route that cannot be reached, which stalls instead of erroring and
# gets turned into a bodiless 504 by the 29s API Gateway ceiling. A public
# deployment has no endpoint and no route to misconfigure, so it keeps botocore's
# stock timeouts and this change is a no-op there rather than a new way to fail.
#
# botocore reads max_attempts as a RETRY count, so this is 2 total attempts:
# worst case 2 x (3s connect + 8s read) = 22s for a SINGLE call, plus standard-mode
# backoff sleeps (rand(0,1) x min(2^i, 20)s), which are not in that 22s. This does
# NOT bound the operation: getTestSets lists the bucket and makes ~4 more calls per
# prefix, so a whole-operation bound would have to be a multiple of this. It is a
# per-call guard against an unreachable endpoint stalling indefinitely, not a proof
# the operation fits the 29s budget — that is the dispatcher's read bound
# (_RESOLVER_READ_TIMEOUT_SECONDS), which returns a labelled 504 regardless of how
# many calls this resolver makes. One retry is kept for genuinely transient S3
# errors (503 SlowDown). read_timeout is per socket read rather than per operation,
# so 8s of silence mid-transfer is far outside normal for the small LIST/GET
# responses this resolver makes — S3 answers these in tens of milliseconds.
_S3_DATAPLANE_BOUNDS = (
    {
        "connect_timeout": 3,
        "read_timeout": 8,
        "retries": {"mode": "standard", "max_attempts": 1},
    }
    if _s3_endpoint_url
    else {}
)

# Every actual S3 API call this resolver makes.
# Fan-out for the baseline snapshot, and the ceiling it can reach inside one request.
_SNAPSHOT_CONCURRENCY = 16
# ~16 concurrent server-side copies fit roughly this many objects inside the
# dispatcher's 29-second budget with margin; see _snapshot_baselines.
_SNAPSHOT_MAX_OBJECTS = 6000

s3_config = Config(
    signature_version="s3v4",
    s3={"addressing_style": "path"},
    # Two 16-worker pools share this client; the default pool of 10 would log
    # "Connection pool is full" and serialize the rest.
    max_pool_connections=_SNAPSHOT_CONCURRENCY,
    **_S3_DATAPLANE_BOUNDS,
)
s3_client = boto3.client("s3", config=s3_config)
db_client = DynamoDBClient(table_name=os.environ["TRACKING_TABLE"])


def _caller_in_groups(event, allowed):
    """Defense-in-depth RBAC check against the caller's Cognito groups.

    The schema restricts these fields via @aws_cognito_user_pools(cognito_groups),
    but we also enforce the group server-side so the operation is never reachable
    by an unauthorized caller even if the schema directive is missing or
    misconfigured (e.g. the prior @aws_auth directive, which AppSync silently
    ignores on a multi-auth API).
    """
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return bool(set(allowed).intersection(groups))


# Operations a scoped Annotator may reach; every other field here is test-set
# management and stays Admin/Author. Each of these must also enforce per-set scope
# via assert_can_access_test_set — reaching the operation is not the same as being
# allowed to see a given set.
ANNOTATOR_ALLOWED_FIELDS = (
    "getAnnotationQueue",
    "getTestSetDocuments",
    "reextractTestSetDocument",
    # The workspace shows annotators what their review is buying (accuracy now vs
    # after, evidence count, quality tier). Without this the call 403s on every
    # annotator page load and the panel is silently absent for the role it exists
    # for. Per-set scope is still asserted in the handler below.
    "estimateReviewEffort",
    # Starting a re-extract without being able to watch it is not a capability. The
    # editor kicks off reextractTestSetDocument (allowed above) and then polls this
    # for the outcome, so leaving it off the list meant an annotator's class
    # correction ran to completion server-side while the UI reported
    # "Could not re-extract this document" — a failure message over a job that
    # worked. Per-set scope is asserted in the handler below, as for the others.
    "getDraftLabelJob",
    # Correcting a wrong packet split is annotation work, exactly as correcting a wrong
    # class is — and it is the correction most often needed in practice. Per-set scope
    # asserted in the dispatch below.
    "updateTestSetDocumentSections",
    # Opening the version transition annotating commits to. An annotator is the role that
    # begins an annotation session, so refusing this would make the queue unusable for
    # them — and it is the operation that preserves the labels being edited away from.
    # Per-set scope asserted in the dispatch below.
    "openTestSetAnnotationDraft",
)

# Fields narrower than the Admin/Author default. Resetting discards every label in
# a set including human-reviewed ones, so an Author who can otherwise manage test
# sets cannot destroy the team's annotation work.
ADMIN_ONLY_FIELDS = ("resetTestSetLabels",)


def handler(event, context):
    field_name = event["info"]["fieldName"]
    logger.info(f"Test set resolver invoked with field_name: {field_name}")

    # Defense-in-depth: test-set operations are Admin+Author, plus the
    # ANNOTATOR_ALLOWED_FIELDS allowlist.
    # Allow direct Lambda invocations (no 'identity' field or identity=None) for CI/automation.
    # AppSync invocations always have 'identity' with non-None value, so RBAC is still enforced for UI users.
    # Security: Direct invocation path is gated by IAM (lambda:InvokeFunction permission on this ARN),
    # not Cognito groups. CI/automation uses IAM credentials; UI users go through AppSync + Cognito.
    is_appsync_invoke = event.get("identity") is not None
    if is_appsync_invoke:
        if field_name in ADMIN_ONLY_FIELDS:
            allowed_groups = ["Admin"]
        else:
            allowed_groups = ["Admin", "Author"]
            if field_name in ANNOTATOR_ALLOWED_FIELDS:
                allowed_groups.append("Annotator")
        if not _caller_in_groups(event, tuple(allowed_groups)):
            logger.warning(
                f"Forbidden: caller attempted '{field_name}' without "
                f"{'/'.join(allowed_groups)} group"
            )
            raise Exception(
                f"Unauthorized: '{field_name}' requires "
                f"{' or '.join(allowed_groups)} group"
            )

    if field_name == "addTestSet":
        return add_test_set(event["arguments"])
    elif field_name == "addTestSetFromUpload":
        return add_test_set_from_upload(event["arguments"])
    elif field_name == "addDocumentsToTestSet":
        return add_documents_to_test_set(event["arguments"])
    elif field_name == "addDocumentsToTestSetFromUpload":
        return add_documents_to_test_set_from_upload(event["arguments"])
    elif field_name == "updateTestSet":
        return update_test_set(event["arguments"])
    elif field_name == "removeDocumentsFromTestSet":
        return remove_documents_from_test_set(event["arguments"])
    elif field_name == "clearDraftLabels":
        return clear_draft_labels(event["arguments"])
    elif field_name == "resetTestSetLabels":
        return reset_test_set_labels(event["arguments"])
    elif field_name == "deleteTestSets":
        return delete_test_sets(event["arguments"])
    elif field_name == "getTestSets":
        return get_test_sets()
    elif field_name == "getTestSetDocuments":
        # Annotator-reachable: group membership alone would expose other sets.
        assert_can_access_test_set(event, event["arguments"].get("testSetId") or "")
        return get_test_set_documents(event["arguments"])
    elif field_name == "publishTestSetVersion":
        return publish_test_set_version(event["arguments"], event)
    elif field_name == "getTestSetVersions":
        return get_test_set_versions(event["arguments"])
    elif field_name == "generateDraftLabels":
        return generate_draft_labels(event["arguments"], event)
    elif field_name == "openTestSetAnnotationDraft":
        # Annotator-reachable: group membership alone would let one annotator open a
        # version transition on a set they were never assigned.
        input_data = event["arguments"].get("input", event["arguments"])
        assert_can_access_test_set(event, input_data.get("testSetId") or "")
        return open_test_set_annotation_draft(event["arguments"], event)
    elif field_name == "updateTestSetDocumentSections":
        # Annotator-reachable: group membership alone would expose other sets.
        input_data = event["arguments"].get("input", event["arguments"])
        assert_can_access_test_set(event, input_data.get("testSetId") or "")
        return update_test_set_document_sections(event["arguments"], event)
    elif field_name == "reextractTestSetDocument":
        # Annotator-reachable: group membership alone would expose other sets.
        input_data = event["arguments"].get("input", event["arguments"])
        assert_can_access_test_set(event, input_data.get("testSetId") or "")
        return reextract_test_set_document(event["arguments"], event)
    elif field_name == "getDraftLabelJob":
        # Annotator-reachable: group membership alone would expose other sets' jobs.
        assert_can_access_test_set(event, event["arguments"].get("testSetId") or "")
        return get_draft_label_job(event["arguments"])
    elif field_name == "estimateReviewEffort":
        # Annotator-reachable: group membership alone would expose other sets.
        assert_can_access_test_set(event, event["arguments"].get("testSetId") or "")
        return estimate_review_effort(event["arguments"])
    elif field_name == "getAnnotationQueue":
        return get_annotation_queue(event["arguments"], event)
    elif field_name == "listBucketFiles":
        return list_bucket_files(event["arguments"])
    elif field_name == "validateTestFileName":
        return validate_test_file_name(event["arguments"])
    else:
        raise Exception(f"Unknown field: {field_name}")


def add_test_set_from_upload(args):
    logger.info(f"Adding test set from zip upload: {args}")

    input_data = args["input"]
    zip_filename = input_data["fileName"]
    description = input_data.get("description", "")  # Optional field
    document_class_type = input_data.get("documentClassType")  # Optional field
    requested_name = (input_data.get("name") or "").strip()

    # Validate zip file extension
    if not zip_filename.lower().endswith(".zip"):
        raise Exception("File must be a zip file")

    # The caller's name wins. This used to be derived from the filename
    # unconditionally, so a user who typed "my-test-set" in the wizard and uploaded
    # Archive.zip got a set called "Archive" — and the wizard's own success toast
    # reported the name it had never sent. Only the suffix is stripped: `.replace`
    # removed *every* occurrence, so "my.zip.backup.zip" lost both.
    derived_name = zip_filename[: -len(".zip")]
    test_set_name = requested_name or derived_name
    if not requested_name:
        # Logged rather than silent: a caller reaching this has no name of its own, and
        # this is the path that produced the wrong name for two releases.
        logger.info(
            f"No name supplied for zip upload '{zip_filename}'; "
            f"falling back to the filename '{derived_name}'"
        )

    # Validate test set name
    if not validate_test_set_name(test_set_name):
        raise Exception(
            "Test set name can only contain letters, numbers, spaces, hyphens, and underscores (max 50 characters)"
        )

    # Validate description
    if description and not validate_description(description):
        raise Exception("Description cannot exceed 500 characters")

    test_set_id = f"{test_set_name.replace(' ', '-').lower()}"

    test_set_bucket = os.environ["TEST_SET_BUCKET"]

    # Upload with .zip extension in the test set folder
    key = f"{test_set_id}/{zip_filename}"

    # Generate presigned URL for zip file. Presign client: the URL goes to the
    # browser, so it must carry the VPC-endpoint host in private deployments.
    presigned_post = s3_presign_client.generate_presigned_post(
        Bucket=test_set_bucket,
        Key=key,
        Fields={"Content-Type": "application/zip"},
        Conditions=[
            ["content-length-range", 1, MAX_ZIP_SIZE_BYTES],
            {"Content-Type": "application/zip"},
        ],
        ExpiresIn=900,  # 15 minutes
    )

    logger.info(f"Generated presigned POST for zip file {key}")

    # Add test set entry to tracking table
    now = datetime.utcnow().isoformat() + "Z"

    item = {
        "PK": f"testset#{test_set_id}",
        "SK": "metadata",
        "ItemType": "testset",
        "InitialEventTime": now,
        "id": test_set_id,
        "name": test_set_name,
        "description": description,
        "filePattern": "",  # Empty for uploaded test sets
        "source": "uploaded",
        "status": "QUEUED",
        "createdAt": now,
    }

    # Add documentClassType if provided
    if document_class_type:
        item["documentClassType"] = document_class_type

    # Don't set fileCount for uploads - will be added after zip processing

    db_client.put_item(item)
    logger.info(f"Created test set {test_set_id} in tracking table with QUEUED status")

    logger.info(
        f"Test set {test_set_id} ready for zip upload - will be processed automatically on upload"
    )

    return {
        "testSetId": test_set_id,
        "presignedUrl": json.dumps(presigned_post),
        "objectKey": key,
    }


def add_test_set(args):
    logger.info(f"Adding test set: {args}")

    test_set_name = args["name"]
    description = args.get("description", "")  # Optional field
    file_count = args["fileCount"]
    document_class_type = args.get("documentClassType")  # Optional field

    # Validate test set name
    if not validate_test_set_name(test_set_name):
        raise Exception(
            "Test set name can only contain letters, numbers, spaces, hyphens, and underscores (max 50 characters)"
        )

    # Validate description
    if description and not validate_description(description):
        raise Exception("Description cannot exceed 500 characters")

    # Generate test set ID with name format, replace spaces with dashes
    test_set_id = f"{test_set_name.replace(' ', '-').lower()}"

    # Create initial test set record
    now = datetime.utcnow().isoformat() + "Z"

    item = {
        "PK": f"testset#{test_set_id}",
        "SK": "metadata",
        "ItemType": "testset",
        "InitialEventTime": now,
        "id": test_set_id,
        "name": test_set_name,
        "description": description,
        "filePattern": args["filePattern"],
        "fileCount": file_count,
        "source": "uploaded",
        "status": "QUEUED",
        "createdAt": now,
    }

    # Add documentClassType if provided
    if document_class_type:
        item["documentClassType"] = document_class_type

    db_client.put_item(item)
    logger.info(f"Created test set {test_set_id} in tracking table")

    # Send file copying job to SQS queue
    import boto3

    sqs = boto3.client("sqs")
    queue_url = os.environ["TEST_SET_COPY_QUEUE_URL"]

    message_body = {
        "testSetId": test_set_id,
        "filePattern": args["filePattern"],
        "bucketType": args["bucketType"],
        "trackingTable": os.environ["TRACKING_TABLE"],
    }
    if args.get("modifiedAfter"):
        message_body["modifiedAfter"] = args["modifiedAfter"]

    sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(message_body))

    logger.info(
        f"Queued test set creation job for {test_set_id} with pattern '{args['filePattern']}'"
    )

    result = {
        "id": test_set_id,
        "name": test_set_name,
        "description": description,
        "filePattern": args["filePattern"],
        "fileCount": file_count,
        "source": "uploaded",
        "status": "QUEUED",
        "createdAt": now,
    }

    # Add documentClassType to response if provided
    if document_class_type:
        result["documentClassType"] = document_class_type

    return result


def add_documents_to_test_set(args):
    logger.info(f"Adding documents to existing test set: {args}")

    test_set_id = args["testSetId"]
    file_pattern = args["filePattern"]
    bucket_type = args["bucketType"]
    file_count = args["fileCount"]

    # Look up existing test set
    item = db_client.get_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})

    if not item:
        raise Exception(f"Test set '{test_set_id}' not found")

    if item.get("status") != "COMPLETED":
        raise Exception(
            f"Test set '{test_set_id}' is not in COMPLETED status (current: {item.get('status')})"
        )

    # Update status to UPDATING. statusUpdatedAt is what makes this reapable: without
    # it a set whose copier dies sits in UPDATING forever, because
    # _reap_abandoned_test_sets has no way to tell a slow copy from an abandoned one.
    tracking_table = os.environ["TRACKING_TABLE"]
    table = boto3.resource("dynamodb").Table(tracking_table)
    table.update_item(
        Key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
        UpdateExpression=(
            "SET #status = :status, statusUpdatedAt = :now REMOVE lastAddResult"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": "UPDATING",
            ":now": datetime.utcnow().isoformat() + "Z",
        },
    )

    # Send file copying job to SQS queue
    sqs = boto3.client("sqs")
    queue_url = os.environ["TEST_SET_COPY_QUEUE_URL"]

    message_body = {
        "testSetId": test_set_id,
        "filePattern": file_pattern,
        "bucketType": bucket_type,
        "trackingTable": tracking_table,
        "mode": "append",
    }
    if args.get("modifiedAfter"):
        message_body["modifiedAfter"] = args["modifiedAfter"]

    sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(message_body))

    logger.info(
        f"Queued append job for test set {test_set_id} with pattern '{file_pattern}'"
    )

    return {
        "id": test_set_id,
        "name": item["name"],
        "description": item.get("description", ""),
        "filePattern": item.get("filePattern", ""),
        "fileCount": item.get("fileCount"),
        "status": "UPDATING",
        "createdAt": item["createdAt"],
    }


def add_documents_to_test_set_from_upload(args):
    logger.info(f"Adding documents to test set from zip upload: {args}")

    input_data = args["input"]
    test_set_id = input_data["testSetId"]
    zip_filename = input_data["fileName"]

    # Validate zip file extension
    if not zip_filename.lower().endswith(".zip"):
        raise Exception("File must be a zip file")

    # Look up existing test set
    item = db_client.get_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})

    if not item:
        raise Exception(f"Test set '{test_set_id}' not found")

    if item.get("status") != "COMPLETED":
        raise Exception(
            f"Test set '{test_set_id}' is not in COMPLETED status (current: {item.get('status')})"
        )

    # Update status to UPDATING. statusUpdatedAt is what makes this reapable: without
    # it a set whose copier dies sits in UPDATING forever, because
    # _reap_abandoned_test_sets has no way to tell a slow copy from an abandoned one.
    tracking_table = os.environ["TRACKING_TABLE"]
    table = boto3.resource("dynamodb").Table(tracking_table)
    table.update_item(
        Key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
        UpdateExpression=(
            "SET #status = :status, statusUpdatedAt = :now REMOVE lastAddResult"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": "UPDATING",
            ":now": datetime.utcnow().isoformat() + "Z",
        },
    )

    test_set_bucket = os.environ["TEST_SET_BUCKET"]

    # Upload with .zip extension in the test set folder
    key = f"{test_set_id}/{zip_filename}"

    # Generate presigned URL for zip file. Presign client: the URL goes to the
    # browser, so it must carry the VPC-endpoint host in private deployments.
    presigned_post = s3_presign_client.generate_presigned_post(
        Bucket=test_set_bucket,
        Key=key,
        Fields={"Content-Type": "application/zip"},
        Conditions=[
            ["content-length-range", 1, MAX_ZIP_SIZE_BYTES],
            {"Content-Type": "application/zip"},
        ],
        ExpiresIn=900,  # 15 minutes
    )

    logger.info(f"Generated presigned POST for append zip file {key}")

    return {
        "testSetId": test_set_id,
        "presignedUrl": json.dumps(presigned_post),
        "objectKey": key,
    }


# ---------------------------------------------------------------------------
# Versioning: a test set has a mutable working draft (the SK='metadata' item)
# plus zero or more immutable published versions (SK='version#<n>'). Publishing
# freezes the current document + label state into a numbered version and, by
# default, marks it the "active reference" that scoring runs compare against.
#
# Test sets with no version items read as latestVersion=0 / activeReference=None,
# so no backfill is required.
# ---------------------------------------------------------------------------


def _version_sk(n):
    return f"version#{int(n):06d}"


def _list_version_items(test_set_id):
    """Return all version items for a test set, ascending by version number."""
    from boto3.dynamodb.conditions import Key as DDBKey

    tracking_table = boto3.resource("dynamodb").Table(os.environ["TRACKING_TABLE"])
    items = []
    query_kwargs = {
        "KeyConditionExpression": (
            DDBKey("PK").eq(f"testset#{test_set_id}")
            & DDBKey("SK").begins_with("version#")
        ),
    }
    while True:
        resp = tracking_table.query(**query_kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    items.sort(key=lambda it: it.get("versionNumber", 0))
    return items


def _version_to_result(item):
    return {
        "testSetId": item.get("testSetId"),
        "version": item.get("versionNumber"),
        "label": item.get("label"),
        "notes": item.get("notes"),
        "fileCount": item.get("fileCount"),
        "createdAt": item.get("createdAt"),
        "createdBy": item.get("createdBy"),
    }


def get_test_set_versions(args):
    """List the immutable published versions of a test set (ascending)."""
    test_set_id = args["testSetId"]
    return [_version_to_result(it) for it in _list_version_items(test_set_id)]


def publish_test_set_version(args, event=None):
    """Freeze the current test-set state into a new immutable version.

    Optionally (default true) set the new version as the active reference. The
    metadata pointer tracks latestVersion / publishedVersion / activeReference.
    """
    input_data = args.get("input", args)
    test_set_id = input_data["testSetId"]
    label = input_data.get("label")
    notes = input_data.get("notes")
    set_active = input_data.get("setAsActiveReference", True)

    meta = db_client.get_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})
    if not meta:
        raise Exception(f"Test set '{test_set_id}' not found")

    # Reserve the version number with an atomic ADD before writing the version
    # item. Deriving it from the read above would be a read-modify-write race in
    # which two concurrent publishes both write version N+1, the second
    # overwriting the first's "immutable" version. attribute_exists(PK) stops
    # update_item upserting metadata for a set deleted since the read.
    tracking_table = boto3.resource("dynamodb").Table(os.environ["TRACKING_TABLE"])
    try:
        reserve = tracking_table.update_item(
            Key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
            UpdateExpression="ADD latestVersion :one",
            ExpressionAttributeValues={":one": 1},
            ConditionExpression="attribute_exists(PK)",
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Exception(f"Test set '{test_set_id}' not found")
        raise
    next_version = int(reserve["Attributes"]["latestVersion"])

    now = datetime.utcnow().isoformat() + "Z"
    created_by = None
    if event:
        try:
            created_by = event.get("identity", {}).get("claims", {}).get("email")
        except Exception:
            created_by = None

    version_item = {
        "PK": f"testset#{test_set_id}",
        "SK": _version_sk(next_version),
        "ItemType": "testset_version",
        "testSetId": test_set_id,
        "versionNumber": next_version,
        "label": label or f"v{next_version}",
        "notes": notes or "",
        "source": meta.get("source"),
        "fileCount": meta.get("fileCount"),
        "configVersion": meta.get("boundConfigVersion"),
        "createdAt": now,
        "createdBy": created_by,
    }
    # Versions are immutable, even if the counter were rewound by hand.
    db_client.put_item(version_item, condition_expression="attribute_not_exists(SK)")

    # Pointers are advanced only after the version item exists, so a failed version
    # write leaves a numbering gap rather than a pointer to a missing version, and
    # only ever forwards, since concurrent publishes can arrive out of order. A
    # failed condition means a newer publish won and is not an error here.
    pointer_expr = "SET publishedVersion = :v"
    pointer_values = {":v": next_version}
    if set_active:
        pointer_expr += ", activeReference = :v"
    # And the open transition has landed, so it stops being open. Leaving draftVersion
    # set would have the annotate view reporting a transition toward a version that
    # already exists, and would stop the next annotation session snapshotting.
    pointer_expr += " REMOVE draftVersion"
    try:
        tracking_table.update_item(
            Key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
            UpdateExpression=pointer_expr,
            ExpressionAttributeValues=pointer_values,
            ConditionExpression=(
                "attribute_not_exists(publishedVersion) OR publishedVersion < :v"
            ),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        logger.info(
            f"Test set '{test_set_id}' pointers already at a version newer than "
            f"{next_version}; leaving them unchanged"
        )
        # The pointers stay, but the transition this publish closes must still
        # close: the REMOVE rode on the conditional update and was lost with it,
        # leaving the annotate view reporting an open draft toward a version that
        # now exists.
        tracking_table.update_item(
            Key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
            UpdateExpression="REMOVE draftVersion",
        )

    logger.info(
        f"Published test set '{test_set_id}' version {next_version} "
        f"(active={set_active})"
    )
    result = _version_to_result(version_item)
    result["activeReference"] = (
        next_version if set_active else meta.get("activeReference")
    )
    return result


def _snapshot_baselines(test_set_bucket, test_set_id, version):
    """Copy the live baselines to ``{id}/versions/{version}/baseline/``.

    Server-side copies, paginated: a 2000-document set has thousands of baseline
    objects, which is exactly why this runs once when a draft opens rather than on every
    save. Returns the number of objects copied.

    Idempotent by overwrite: re-copying the same keys is harmless, so a retry after a
    partial failure converges instead of needing cleanup.
    """
    source_prefix = f"{test_set_id}/baseline/"
    dest_prefix = f"{test_set_id}/versions/{int(version)}/baseline/"

    keys = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=test_set_bucket, Prefix=source_prefix):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            if not key.endswith("/"):
                keys.append(key)

    if len(keys) > _SNAPSHOT_MAX_OBJECTS:
        # Refused with a reason rather than left to time out. The pointer is written
        # after the copy, so an oversize set would otherwise fail with a generic
        # error and fail identically on every retry — permanently unable to start
        # annotating, with nothing saying why.
        raise Exception(
            f"Test set '{test_set_id}' has {len(keys)} baseline objects, more than "
            f"the {_SNAPSHOT_MAX_OBJECTS} that can be snapshotted within one request. "
            "Publishing a version of a set this large needs an asynchronous snapshot, "
            "which is not available yet."
        )

    def _copy(key):
        s3_client.copy_object(
            Bucket=test_set_bucket,
            CopySource={"Bucket": test_set_bucket, "Key": key},
            Key=f"{dest_prefix}{key[len(source_prefix) :]}",
        )

    # Bounded fan-out. Each copy is server-side, so the cost is a round trip, and
    # this runs inside a synchronous request the dispatcher abandons after 29s: a
    # sequential pass over a few thousand objects did not fit, and left the draft
    # unrecorded while the resolver kept copying to its own timeout. Sixteen at a
    # time fits the set sizes seen so far; beyond that this belongs in an
    # asynchronous job the UI polls. `list()` re-raises the first failure, so a
    # partial snapshot is reported as an error rather than as a version.
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=_SNAPSHOT_CONCURRENCY
    ) as pool:
        list(pool.map(_copy, keys))
    copied = len(keys)

    logger.info(
        f"Snapshotted {copied} baseline object(s) for test set '{test_set_id}' "
        f"as version {version}"
    )
    return copied


def open_test_set_annotation_draft(args, event=None):
    """Open the version transition that annotating a test set commits you to.

    Starting annotation on a set, even one that already has ground truth, commits to a
    new version of it, and the queue link should name that transition.

    The problem underneath is worse than the queue link. A version was
    a **DynamoDB row only** — ``publish_test_set_version`` records a number, a label and
    a file count, and copies nothing. Annotation writes straight to ``{id}/baseline/``.
    So a run stamped ``TestSetVersion = 3`` could not be reproduced against the labels it
    actually scored: the number was immutable, its content was not.

    This makes the transition explicit and preserves what it moves away from:

      * ``baseVersion`` is the state being left. A set that has never been published gets
        its arriving labels published as a version first — the "even if we still have
        ground truth" case, and the common one for an uploaded set.
      * that state is snapshotted to ``{id}/versions/{baseVersion}/baseline/``, so the
        number now refers to bytes.
      * ``draftVersion`` is ``baseVersion + 1``, recorded on the metadata row. The queue
        link carries it, so a link says which transition it belongs to.

    Idempotent: opening a draft that is already open returns it and copies nothing, which
    matters because the annotate view calls this on entry.

    Why an explicit call rather than copy-on-write: the editor saves a baseline through a
    **presigned POST straight from the browser**, so no Lambda observes the write. There
    is no server-side moment to hang a lazy snapshot on — and making the commitment
    visible is what was being asked for anyway.
    """
    input_data = args.get("input", args)
    test_set_id = input_data["testSetId"]

    meta = db_client.get_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})
    if not meta:
        raise Exception(f"Test set '{test_set_id}' not found")

    existing_draft = _as_int(meta.get("draftVersion"))
    if existing_draft:
        # Already open. Returning it unchanged keeps the annotate view's on-entry call
        # cheap and stops a second reviewer opening a second transition.
        base = existing_draft - 1
        logger.info(
            f"Test set '{test_set_id}' already has draft version {existing_draft}; "
            "returning it"
        )
        return {
            "testSetId": test_set_id,
            "baseVersion": base,
            "draftVersion": existing_draft,
            "snapshotObjectCount": 0,
            "alreadyOpen": True,
        }

    base_version = _as_int(meta.get("publishedVersion"))
    if not base_version:
        # Never published. Publishing the arriving state first is what stops the labels
        # a set was uploaded with from being the thing that gets overwritten with no
        # record of what they were.
        published = publish_test_set_version(
            {
                "input": {
                    "testSetId": test_set_id,
                    "label": "As uploaded",
                    "notes": "Captured automatically before annotation began.",
                    # Not the active reference: that would let an annotator's Start
                    # annotating decide which version every future run of the set
                    # scores against, a choice publishTestSetVersion reserves for
                    # Admin/Author. Runs default to the set's current labels and pin
                    # a version only when asked (see test_runner), so nothing needs
                    # this pointer moved here.
                    "setAsActiveReference": False,
                }
            },
            event,
        )
        base_version = int(published["version"])
        logger.info(
            f"Test set '{test_set_id}' had no published version; captured its current "
            f"labels as version {base_version} before opening a draft"
        )

    test_set_bucket = os.environ["TEST_SET_BUCKET"]
    copied = _snapshot_baselines(test_set_bucket, test_set_id, base_version)

    # publish_test_set_version reserves ``latestVersion + 1``, and a failed version
    # write leaves a gap by design, so ``publishedVersion + 1`` can name a number the
    # next publish will never create. Follow the same counter it does.
    draft_version = max(_as_int(meta.get("latestVersion")) or 0, base_version) + 1
    # Written after the snapshot: a failure part-way leaves no draft pointer, so the
    # next call retries the copy rather than annotating against a version whose content
    # was never captured.
    db_client.update_item(
        key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
        update_expression="SET draftVersion = :d",
        expression_attribute_values={":d": draft_version},
    )

    logger.info(
        f"Opened annotation draft {draft_version} for test set '{test_set_id}' "
        f"(base {base_version}, {copied} baseline object(s) snapshotted)"
    )
    return {
        "testSetId": test_set_id,
        "baseVersion": base_version,
        "draftVersion": draft_version,
        "snapshotObjectCount": copied,
        "alreadyOpen": False,
    }


# ---------------------------------------------------------------------------
# Draft labeling: run the active config over a test set's documents to produce
# machine-generated ground-truth candidates ("draft labels") with per-field
# confidence, which a human then reviews and confirms.
#
# A labeling job is an ordinary test run whose results are harvested back into the
# test set's baseline/ prefix. Reusing the scoring pipeline rather than a second
# extraction path keeps confidence semantics identical to the ones scoring runs and
# the estimator rely on.
#
# The job item is SK='labeljob#<testRunId>' under the test set's PK, so jobs are
# listable per set and expire with it.
# ---------------------------------------------------------------------------

LABEL_SOURCE_DRAFT = "draft-machine"
LABEL_SOURCE_HUMAN = "reviewed-human"
# Ground truth supplied with the test set. Authoritative — draft labeling will not
# overwrite it — but not review work, so it never counts toward annotation progress.
LABEL_SOURCE_UPLOADED = "uploaded"


def _label_job_sk(test_run_id):
    return f"labeljob#{test_run_id}"


def _as_int(value):
    """Coerce a DynamoDB number (Decimal) to int; None stays None."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _label_jobs(test_set_id):
    """Every labeling job recorded for a set, oldest first.

    Job items are per-run (SK ``labeljob#{runId}``), so they accumulate rather than
    overwrite — which is what makes per-document run resolution possible without
    storing a new map.
    """
    jobs = []
    start_key = None
    for _ in range(20):
        page = db_client.query(
            key_condition_expression="PK = :pk AND begins_with(SK, :sk)",
            expression_attribute_values={
                ":pk": f"testset#{test_set_id}",
                ":sk": "labeljob#",
            },
            exclusive_start_key=start_key,
        )
        jobs.extend(page.get("Items") or [])
        next_key = page.get("LastEvaluatedKey")
        if not next_key or next_key == start_key:
            break
        start_key = next_key
    return sorted(jobs, key=lambda j: str(j.get("createdAt") or ""))


def _run_id_by_object_key(test_set_id):
    """Which labeling run produced each document's pipeline copy.

    Review keys are ``{runId}/{filename}``, so this cannot be a single per-set
    pointer: re-extracting one document creates a one-document run, and resolving
    the whole set through that run's id makes every *other* document look
    unprocessed — no review key, no claim state, "not ready to annotate" for work
    that is in fact ready.

    Later jobs win, so a re-extracted document points at its newest run while its
    neighbours keep theirs. A job with no explicit ``objectKeys`` covered the whole
    set, and is applied as a default for documents no later job names.
    """
    per_doc = {}
    default_run = None
    for job in _label_jobs(test_set_id):
        run_id = job.get("jobId")
        if not run_id:
            continue
        object_keys = job.get("objectKeys") or []
        if object_keys:
            for key in object_keys:
                per_doc[key] = run_id
        else:
            default_run = run_id
    return per_doc, default_run


def _harvest_active_label_job(test_set_id, meta):
    """Advance the set's most recent labeling job and return its current state.

    Labels are harvested on read, so every caller that displays a job must drive
    the harvest or the job never progresses. Best-effort: a harvest failure must
    not fail the queue, which still works for already-labeled documents.

    All in-flight jobs share one time budget, since the caller is a single request
    and copying results is S3-bound.
    """
    # Every non-terminal job, not just the one the set's pointer names: a
    # one-document re-extract repoints it, and harvesting only the pointer would
    # orphan a full run still in flight — its remaining documents would never
    # reach the baseline.
    jobs = [
        j
        for j in _label_jobs(test_set_id)
        if j.get("status") not in ("COMPLETED", "FAILED")
    ]
    if not jobs:
        pointer = meta.get("labelJobId")
        if not pointer:
            return None
        return db_client.get_item(
            {"PK": f"testset#{test_set_id}", "SK": _label_job_sk(pointer)}
        )

    deadline = time.monotonic() + HARVEST_TIME_BUDGET_SECONDS
    harvested = []
    for job in jobs:
        try:
            harvested.append(_harvest_label_job(job, deadline=deadline))
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"Queue harvest failed for labeling job {job.get('jobId')}: {e}"
            )
            harvested.append(job)

    # Report the job the UI is tracking when it is one of these, else the newest,
    # so a progress banner follows the run the user started.
    pointer = meta.get("labelJobId")
    for job in harvested:
        if job.get("jobId") == pointer:
            return job
    return harvested[-1]


def _documents_needing_labels(test_set_id):
    """Object keys with no ground truth of their own.

    A document already carrying ground truth (uploaded, generated or reviewed) is
    excluded, because the harvester declines to overwrite anything but a machine
    draft — labeling it would pay for inference that gets discarded.

    Returns ``(needs_labels, already_labeled_count)``.
    """
    needs = []
    already = 0
    next_token = None

    while True:
        page = get_test_set_documents(
            {"testSetId": test_set_id, "limit": 200, "nextToken": next_token}
        )
        for doc in page.get("documents") or []:
            # A draft is replaceable, so a drafted document is still a candidate.
            if doc.get("labelSource") in (None, LABEL_SOURCE_DRAFT):
                needs.append(doc["objectKey"])
            else:
                already += 1
        next_token = page.get("nextToken")
        if not next_token:
            break

    return needs, already


def generate_draft_labels(args, event=None):
    """Start a labeling job: run the active config over a test set's documents.

    Returns immediately with a jobId (the underlying test run id); the caller
    polls getDraftLabelJob, which harvests results as documents finish. Existing
    labels are only replaced when they are themselves machine drafts, so
    re-running never clobbers reviewed or hand-uploaded ground truth.

    Without an explicit ``objectKeys``, only documents that need labels are
    processed; labeling the rest would spend inference on results the harvester
    then refuses to write.
    """
    input_data = args.get("input", args)
    test_set_id = input_data["testSetId"]
    config_version = input_data.get("configVersion")
    config_revision = input_data.get("configRevision")
    object_keys = input_data.get("objectKeys") or []
    # Forces the class for this run's documents instead of classifying them. Only
    # ever set by a single-document re-extract after a class correction.
    document_class = input_data.get("documentClass")

    if not validate_test_set_name(test_set_id):
        raise Exception("Invalid test set id")
    if not validate_document_class(document_class):
        raise Exception(
            f"Invalid document class: expected up to {_DOCUMENT_CLASS_MAX_LEN} "
            "characters of letters, digits, spaces, hyphens or underscores"
        )

    meta = db_client.get_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})
    if not meta:
        raise Exception(f"Test set '{test_set_id}' not found")

    file_count = int(meta.get("fileCount", 0) or 0)
    if file_count <= 0:
        raise Exception(f"Test set '{test_set_id}' has no documents to label")

    already_labeled = 0
    if not object_keys:
        object_keys, already_labeled = _documents_needing_labels(test_set_id)
        if not object_keys and already_labeled:
            raise Exception(
                f"Every document in '{test_set_id}' already has ground truth "
                f"({already_labeled} document(s)) — there is nothing to draft-label. "
                "Run a test to score the pipeline against it instead."
            )
        if already_labeled:
            logger.info(
                f"Draft labeling {len(object_keys)} document(s) in {test_set_id}; "
                f"skipping {already_labeled} that already have ground truth"
            )

    # The test runner is the single owner of run creation, config capture and
    # version pinning, so the run is delegated to it rather than built here.
    runner_arn = os.environ["TEST_RUNNER_FUNCTION_ARN"]
    run_input = {
        "testSetId": test_set_id,
        "context": "Draft labeling run",
        # Suppresses baseline staging (and therefore evaluation) for this run.
        "draftLabeling": True,
    }
    if config_version:
        run_input["configVersion"] = config_version
        # Drafting under a pinned revision matters for the same reason scoring
        # does: a later save must not change what the labels were drafted with.
        if config_revision is not None:
            run_input["configRevision"] = config_revision
    if object_keys:
        run_input["objectKeys"] = object_keys
    if document_class:
        run_input["documentClass"] = document_class

    lambda_client = boto3.client("lambda")
    response = lambda_client.invoke(
        FunctionName=runner_arn,
        InvocationType="RequestResponse",
        # No 'identity' key: a trusted service-to-service invoke, already
        # authorized for generateDraftLabels above.
        Payload=json.dumps(
            {"info": {"fieldName": "startTestRun"}, "arguments": {"input": run_input}}
        ).encode("utf-8"),
    )
    payload = json.loads(response["Payload"].read() or b"{}")
    if response.get("FunctionError"):
        raise Exception(f"Failed to start labeling run: {payload}")

    test_run_id = payload["testRunId"]
    now = datetime.utcnow().isoformat() + "Z"
    started_by = None
    if event:
        started_by = (event.get("identity") or {}).get("claims", {}).get("email")

    job_item = {
        "PK": f"testset#{test_set_id}",
        "SK": _label_job_sk(test_run_id),
        "ItemType": "testset_label_job",
        "testSetId": test_set_id,
        "jobId": test_run_id,
        "status": "RUNNING",
        "configVersion": config_version,
        "configRevision": config_revision,
        "total": len(object_keys) or file_count,
        "labeled": 0,
        "objectKeys": object_keys,
        "skippedAlreadyLabeled": already_labeled,
        "createdAt": now,
        "startedBy": started_by,
    }
    db_client.put_item(job_item)

    # Recorded on the set so progress survives the user navigating away. The
    # drafting run's resolved config version is not stored here: labelJobId IS a
    # test run id, so testrun#{labelJobId}.ConfigVersion already carries it,
    # resolved by the runner even when the caller passed none.
    db_client.update_item(
        key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
        update_expression="SET labelJobId = :j, labelJobStatus = :s",
        expression_attribute_values={":j": test_run_id, ":s": "RUNNING"},
    )

    logger.info(
        f"Started draft-labeling job {test_run_id} for test set {test_set_id} "
        f"({job_item['total']} document(s), configVersion={config_version})"
    )
    return _label_job_to_result(job_item)


def reextract_test_set_document(args, event=None):
    """Re-run extraction for one document after its class was corrected.

    Correcting the class leaves the fields beneath it extracted against the wrong
    schema, so the document is re-run and the ordinary harvest replaces its draft
    labels.

    Runs as a one-document labeling job rather than via ``processChanges``: labels
    are harvested from the job named by the set's ``labelJobId``, so a document
    reprocessed outside a job would never reach the baseline. Going through a job
    also keeps the harvest's overwrite safety, pruning and curve bookkeeping.

    The corrected class is applied in **two** places, and both are load-bearing:

    1. Sent into the run, which stamps it as S3 metadata on the copied document so
       the classification step uses it instead of classifying. This is what makes
       extraction run against the class the reviewer chose.
    2. Written onto the existing baseline up front, so a run that never completes
       still records the correction, and so a reviewed label is demoted (the
       harvest refuses to overwrite ``reviewed-human``).

    Step 2 alone used to be the whole implementation, and it silently did not
    work: the run classifies from the input document and never sees the test set's
    baseline, so the pipeline re-derived the original class and the harvest wrote
    it back over the pin. The observable result was the demotion sticking while
    the correction vanished, leaving fields extracted under the old schema beside
    a "Re-extracted as X" success message.
    """
    input_data = args.get("input", args)
    test_set_id = input_data["testSetId"]
    object_key = input_data["objectKey"]
    document_class = input_data.get("documentClass")

    if not validate_test_set_name(test_set_id):
        raise Exception("Invalid test set id")
    if not validate_document_class(document_class):
        raise Exception(
            f"Invalid document class: expected up to {_DOCUMENT_CLASS_MAX_LEN} "
            "characters of letters, digits, spaces, hyphens or underscores"
        )

    meta = db_client.get_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})
    if not meta:
        raise Exception(f"Test set '{test_set_id}' not found")

    test_set_bucket = os.environ["TEST_SET_BUCKET"]
    if document_class:
        _set_baseline_document_class(
            test_set_bucket, test_set_id, object_key, document_class
        )

    # Reuse the config that drafted the set's labels, so one document is not
    # re-extracted under a different config than its neighbours. Read from the
    # drafting run rather than the set: labelJobId is a test run id, and the runner
    # resolved the version there even when the original call passed none. (This
    # previously read a labelJobConfigVersion attribute that was never written, so
    # the fallback silently did nothing.)
    config_version = input_data.get("configVersion")
    if not config_version and meta.get("labelJobId"):
        tracking = boto3.resource("dynamodb").Table(os.environ["TRACKING_TABLE"])
        drafting_run = (
            tracking.get_item(
                Key={"PK": f"testrun#{meta['labelJobId']}", "SK": "metadata"}
            ).get("Item")
            or {}
        )
        config_version = drafting_run.get("ConfigVersion")
    result = generate_draft_labels(
        {
            "input": {
                "testSetId": test_set_id,
                "objectKeys": [object_key],
                "configVersion": config_version,
                # The correction has to reach the PIPELINE, not just the baseline.
                # Stamping it on the baseline alone does not work: the run
                # classifies from the input document, which never sees the test
                # set's baseline, and the harvest then writes the pipeline's own
                # class back over the pin. The visible result was the demotion
                # sticking while the corrected class silently disappeared, and
                # fields still extracted under the old schema.
                "documentClass": document_class,
            }
        },
        event,
    )
    logger.info(
        f"Re-extracting {object_key} in {test_set_id} as "
        f"'{document_class or 'its existing class'}' via job {result['jobId']}"
    )
    return result


# A packet with more sections than this is far more likely to be a client bug than a
# real document, and each section is a separate S3 write.
MAX_SECTIONS_PER_DOCUMENT = 200


def _read_baseline_sections(test_set_bucket, test_set_id, object_key):
    """Every section of a document's baseline, as ``{section_id: (key, parsed)}``.

    An unreadable section is fatal here, unlike in ``_set_baseline_document_class``
    where one bad file must not block a class correction. Re-grouping rewrites the
    whole set of sections, so proceeding without knowing what one of them contained
    would discard its field values silently.
    """
    prefix = f"{test_set_id}/baseline/{object_key}/sections/"
    paginator = s3_client.get_paginator("list_objects_v2")
    out = {}
    for page in paginator.paginate(Bucket=test_set_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("/result.json"):
                continue
            section_id = key[len(prefix) :].split("/")[0]
            body = s3_client.get_object(Bucket=test_set_bucket, Key=key)["Body"].read()
            out[section_id] = (key, json.loads(body))
    return out


def _validate_regrouping(sections, previously_labelled_pages):
    """Reject a re-grouping that would corrupt or lose ground truth.

    The client validates more than this — it also requires every page of the rendered
    PDF to be assigned, which only it can check, because the server would have to
    parse the document to learn its page count. What the server *can* enforce is that
    nothing already labelled disappears, and that the result is a partition of what it
    is given:

    * no page in two sections, which would make the grouping meaningless;
    * no empty section, which would carry field values belonging to no pages;
    * no page that was previously labelled going missing, which would silently drop
      the ground truth for that page.

    A page the baseline never mentioned *is* allowed in: a split that dropped a page
    entirely is exactly the defect a reviewer is here to fix.
    """
    if not sections:
        raise Exception("A document must have at least one section")
    if len(sections) > MAX_SECTIONS_PER_DOCUMENT:
        raise Exception(
            f"Too many sections ({len(sections)}); the maximum is "
            f"{MAX_SECTIONS_PER_DOCUMENT}"
        )

    seen = {}
    section_ids = set()
    for section in sections:
        section_id = str(section.get("sectionId") or "")
        # Sections are keyed by id when the existing baselines are looked up, so a
        # repeated id would write the same baseline content into two section files and
        # renumber around a group that no longer exists. The page-level checks below
        # cannot catch it: both groups' pages are legitimately accounted for.
        if section_id in section_ids:
            raise Exception(f"Section '{section_id}' appears more than once")
        section_ids.add(section_id)
        indices = section.get("pageIndices")
        if not isinstance(indices, list) or not indices:
            raise Exception(f"Section '{section_id}' has no pages")
        for raw in indices:
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                raise Exception(
                    f"Section '{section_id}' has an invalid page index {raw!r}; "
                    "expected a non-negative integer"
                )
            if raw in seen:
                raise Exception(
                    f"Page index {raw} is in both section '{seen[raw]}' and "
                    f"section '{section_id}'"
                )
            seen[raw] = section_id

    lost = sorted(previously_labelled_pages - set(seen))
    if lost:
        raise Exception(
            f"Page index{'es' if len(lost) > 1 else ''} {', '.join(map(str, lost))} "
            "would no longer belong to any section, which would discard the ground "
            "truth for those pages"
        )


def update_test_set_document_sections(args, event=None):
    """Re-group a document's pages into sections, keeping every field value.

    The reason this exists: a packet split that grouped pages wrongly makes the
    classification ground truth wrong, and until now there was no way to correct it —
    the editor showed ``page_indices`` read-only. Fixing it by re-running extraction
    would regenerate the field values, which is precisely the annotation loss the
    reviewer is trying to avoid.

    So this writes the grouping and the class and **nothing else**. Each surviving
    section keeps its ``inference_result``, its ``labelSource`` and its
    ``_editHistory`` — a reviewer's corrections are not touched. The fields may no
    longer match their pages afterwards, which is true and is why the UI says so and
    offers an opt-in re-extract rather than doing it here.

    ## Sections are renumbered to agree with page order

    Consumers derive a section's group index from its *position in a list*
    (``compute_graded_packet_metrics`` enumerates ``sections_gt``), and nothing
    guarantees that list is in page order. Writing sections as ``1..N`` in first-page
    order makes id order, lexical key order and page order all the same thing, so no
    consumer can disagree about which group a page is in.

    That means rewriting every section file rather than renaming a few. Writes happen
    before deletes, so an interrupted call leaves an extra stale section — visible,
    and recoverable by re-saving — rather than a missing one.
    """
    input_data = args.get("input", args)
    test_set_id = input_data["testSetId"]
    object_key = input_data["objectKey"]
    incoming = input_data.get("sections") or []

    if not validate_test_set_name(test_set_id):
        raise Exception("Invalid test set id")
    for section in incoming:
        if not validate_document_class(section.get("documentClass")):
            raise Exception(
                f"Invalid document class: expected up to {_DOCUMENT_CLASS_MAX_LEN} "
                "characters of letters, digits, spaces, hyphens or underscores"
            )

    meta = db_client.get_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})
    if not meta:
        raise Exception(f"Test set '{test_set_id}' not found")

    test_set_bucket = os.environ["TEST_SET_BUCKET"]
    existing = _read_baseline_sections(test_set_bucket, test_set_id, object_key)
    if not existing:
        raise Exception(
            f"'{object_key}' has no baseline sections to re-group. Generate draft "
            "labels for this test set first."
        )

    previously_labelled = {
        int(index)
        for _key, parsed in existing.values()
        for index in (parsed.get("split_document") or {}).get("page_indices") or []
    }
    _validate_regrouping(incoming, previously_labelled)

    # First-page order, so the ids we assign below agree with page order. Keyed on
    # ``min`` rather than ``pageIndices[0]``: now that a section can carry a manual page
    # order, those differ, and a section's place in the document is where it *starts*, not
    # which of its pages happens to be read first.
    ordered = sorted(incoming, key=lambda sec: min(sec["pageIndices"]))

    prefix = f"{test_set_id}/baseline/{object_key}/sections/"
    written_keys = set()
    for position, section in enumerate(ordered, start=1):
        source_id = str(section.get("sectionId") or "")
        _old_key, content = existing.get(source_id, (None, None))
        if content is None:
            # A section the reviewer added. It has no field values yet, and saying so
            # in the data beats writing a plausible-looking empty result.
            content = {"inference_result": {}, "labelSource": LABEL_SOURCE_DRAFT}
            logger.info(
                f"Section '{source_id}' of {object_key} is new; writing it with no "
                "field values"
            )
        content = dict(content)

        split = dict(content.get("split_document") or {})
        # Order-preserving, deliberately. ``page_indices`` records the section's reading
        # order as well as its membership: ``split_accuracy_with_order`` compares the two
        # lists with ``==`` and the graded packet score is half Kendall's Tau over each
        # page's position (see ``stickler_backend/doc_split.py:111``). Sorting here — as
        # this once did — silently discarded authored order on every save, including on a
        # save that only meant to correct a section's class.
        split["page_indices"] = [int(i) for i in section["pageIndices"]]
        content["split_document"] = split

        document_class = section.get("documentClass")
        if document_class:
            doc_class = dict(content.get("document_class") or {})
            doc_class["type"] = document_class
            content["document_class"] = doc_class

        key = f"{prefix}{position}/result.json"
        s3_client.put_object(
            Bucket=test_set_bucket,
            Key=key,
            Body=json.dumps(content, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        written_keys.add(key)

    # After the writes, never before: an interruption should leave a stale extra
    # section rather than a hole.
    for old_key, _content in existing.values():
        if old_key not in written_keys:
            s3_client.delete_object(Bucket=test_set_bucket, Key=old_key)

    logger.info(
        f"Re-grouped {object_key} in {test_set_id} into {len(ordered)} section(s); "
        f"removed {len(existing) - len(written_keys) if len(existing) > len(written_keys) else 0} "
        "stale section file(s)"
    )
    return {
        "testSetId": test_set_id,
        "objectKey": object_key,
        "sections": [
            {
                "sectionId": str(position),
                "documentClass": section.get("documentClass"),
                # As written, so the client's board reflects what is now on disk.
                "pageIndices": [int(i) for i in section["pageIndices"]],
            }
            for position, section in enumerate(ordered, start=1)
        ],
    }


def _set_baseline_document_class(test_set_bucket, test_set_id, object_key, class_type):
    """Stamp a corrected class onto every section of a document's baseline.

    Written before the run so the extraction uses it, and written even for sections
    the run will overwrite, so a failed run still records the correction.

    A ``reviewed-human`` section is demoted to ``draft-machine`` — the only place a
    reviewed label is downgraded. The harvest refuses to overwrite reviewed labels,
    so otherwise a re-extract would report success and leave the wrong-class fields
    in place; requesting it after a class correction asserts the labels are wrong.

    A baseline with no ``labelSource`` is authored ground truth and is never
    demoted: nothing predicted it, so there is nothing to correct and overwriting it
    would replace authoritative data with a machine guess. The class correction
    still lands, but the label keeps its provenance and the harvest skips it.
    """
    prefix = f"{test_set_id}/baseline/{object_key}/sections/"
    paginator = s3_client.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=test_set_bucket, Prefix=prefix):
        keys.extend(
            obj["Key"]
            for obj in page.get("Contents", [])
            if obj["Key"].endswith("/result.json")
        )

    demoted = 0
    for key in keys:
        try:
            body = s3_client.get_object(Bucket=test_set_bucket, Key=key)["Body"].read()
            result = json.loads(body)
        except Exception as e:  # noqa: BLE001 — one unreadable section must not block
            logger.warning(f"Could not read baseline section {key} to set class: {e}")
            continue
        doc_class = result.get("document_class")
        if not isinstance(doc_class, dict):
            doc_class = {}
        doc_class["type"] = class_type
        result["document_class"] = doc_class
        if result.get("labelSource") == LABEL_SOURCE_HUMAN:
            result["labelSource"] = LABEL_SOURCE_DRAFT
            demoted += 1
        s3_client.put_object(
            Bucket=test_set_bucket,
            Key=key,
            Body=json.dumps(result, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    logger.info(
        f"Set class '{class_type}' on {len(keys)} baseline section(s) of {prefix}; "
        f"demoted {demoted} reviewed label(s) so the harvest can replace them"
    )


def _label_job_to_result(item):
    return {
        "jobId": item.get("jobId"),
        "testSetId": item.get("testSetId"),
        "status": item.get("status"),
        "total": int(item.get("total", 0) or 0),
        "labeled": int(item.get("labeled", 0) or 0),
        "configVersion": item.get("configVersion"),
        "error": item.get("error"),
        "createdAt": item.get("createdAt"),
        "completedAt": item.get("completedAt"),
        "skippedAlreadyLabeled": int(item.get("skippedAlreadyLabeled", 0) or 0),
        "failedDocuments": len(item.get("failedFiles") or []),
    }


def get_draft_label_job(args):
    """Poll a labeling job, harvesting any newly-finished documents.

    Test-run completion is poll-based (the results resolver recounts doc items on
    read), so there is no completion event to hook; progress is computed by
    harvesting on read, which is idempotent.
    """
    test_set_id = args["testSetId"]
    job_id = args["jobId"]

    job = db_client.get_item(
        {"PK": f"testset#{test_set_id}", "SK": _label_job_sk(job_id)}
    )
    if not job:
        raise Exception(f"Labeling job '{job_id}' not found")

    if job.get("status") in ("COMPLETED", "FAILED"):
        return _label_job_to_result(job)

    return _label_job_to_result(
        _harvest_label_job(job, deadline=time.monotonic() + HARVEST_TIME_BUDGET_SECONDS)
    )


def _walk_confidence(explainability_info):
    """Collect (confidence, confidence_threshold) pairs from explainability_info.

    The shape is nested and irregular — ``{field: {"confidence": 0.9}}`` for
    scalars, nested dicts for compound fields (``PayPeriod.StartDate``), lists of
    such dicts for tables — so walk it and collect every ``confidence`` leaf
    rather than assuming a depth.
    """
    found = []

    def walk(node):
        if isinstance(node, dict):
            value = node.get("confidence")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                threshold = node.get("confidence_threshold")
                if not isinstance(threshold, (int, float)) or isinstance(
                    threshold, bool
                ):
                    threshold = None
                found.append((float(value), threshold))
            for key, child in node.items():
                if key != "confidence":
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(explainability_info)
    return found


def _field_path(prefix, key):
    """Join a field path segment, matching ``curve_store``'s path convention."""
    return f"{prefix}.{key}" if prefix else key


def _list_item_path(prefix, node, index):
    """Path for one member of a list.

    A single-element list adds no level: ``explainability_info`` arrives wrapped in
    one, and adding a level there would misalign it from ``inference_result``.
    """
    return prefix if len(node) == 1 else f"{prefix}[{index}]"


def _absent_field_paths(inference_result):
    """Field *paths* whose extracted value is absent (null / "" / empty container).

    A field the document does not contain is assessed at confidence 0.0, which is a
    correct reading of a blank box but indistinguishable from real uncertainty once
    reduced to a number. Callers exclude these from headline scores.

    Paths, not bare leaf names: one empty ``Description`` cell would otherwise
    exclude *every* Description score in a 200-row transaction table, understating
    review need on exactly the table-heavy documents this feature targets. The path
    shape matches :func:`_walk_confidence_named` and ``curve_store._flatten_values``.
    """
    absent = set()

    def walk(node, prefix=""):
        if isinstance(node, dict):
            for key, child in node.items():
                walk(child, _field_path(prefix, key))
        elif isinstance(node, list):
            if not node and prefix:
                absent.add(prefix)
            for index, child in enumerate(node):
                walk(child, _list_item_path(prefix, node, index))
        elif prefix and (node is None or node == ""):
            absent.add(prefix)

    walk(inference_result)
    return absent


def _min_confidence(explainability_info, inference_result=None):
    """Lowest confidence among fields the document actually has a value for.

    Returns None when the payload carries no confidence at all (e.g. assessment
    disabled), so callers can distinguish "no confidence data" from "confidence 0".

    Absent fields are excluded: their 0.0 describes the emptiest box on the form,
    not the quality of the extraction, so including it would report 0.0% for a
    document whose populated fields all score above 0.99. The per-field 0.0 stays in
    explainability_info; it just does not define the headline score.
    """
    found = _walk_confidence(explainability_info)
    if not found:
        return None
    if inference_result is not None:
        absent = _absent_field_paths(inference_result)
        populated = [
            (c, t)
            for c, t, name in _walk_confidence_named(explainability_info)
            if name not in absent
        ]
        # An entirely absent extraction falls back to the unfiltered minimum, so it
        # reports 0 rather than "no data".
        if populated:
            return min(c for c, _ in populated)
    return min(c for c, _ in found)


def _walk_confidence_named(explainability_info):
    """As :func:`_walk_confidence`, plus the field *path* each score belongs to.

    Paths are built the same way as :func:`_absent_field_paths`, so the two line up
    per occurrence rather than per field name — see that function for why the
    distinction matters on tables.
    """
    found = []

    def walk(node, prefix=""):
        if isinstance(node, dict):
            value = node.get("confidence")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                threshold = node.get("confidence_threshold")
                if not isinstance(threshold, (int, float)) or isinstance(
                    threshold, bool
                ):
                    threshold = None
                found.append((float(value), threshold, prefix or None))
            for key, child in node.items():
                if key != "confidence":
                    walk(child, _field_path(prefix, key))
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, _list_item_path(prefix, node, index))

    walk(explainability_info)
    return found


def _confidence_threshold(explainability_info, inference_result=None):
    """The configured alert threshold for the weakest field, if it carries one.

    Reported alongside minConfidence so the UI colors against the config's threshold
    rather than hardcoded bands: 0.85 fails under a 0.9 threshold and passes under
    0.8.
    """
    found = _walk_confidence_named(explainability_info)
    if not found:
        return None
    if inference_result is not None:
        absent = _absent_field_paths(inference_result)
        populated = [f for f in found if f[2] not in absent]
        if populated:
            found = populated
    # Tie to the same field minConfidence reports.
    return min(found, key=lambda triple: triple[0])[1]


# The bar a field is measured against when its own assessment carries no threshold.
# Must match the UI's fallback, or alert counts differ across the API.
DEFAULT_ALERT_THRESHOLD = 0.8


def _alert_counts(explainability_info, inference_result=None):
    """(alerts, fields): how many fields fall below their configured threshold.

    The count, not the single lowest score, is what decides a document needs a
    human, and it ranks review work the way an annotator experiences it: eight weak
    fields is more work than one weak field at a lower score.

    Absent fields are excluded, as in :func:`_min_confidence` — a blank box is a
    correct reading rather than an alert.
    """
    found = _walk_confidence_named(explainability_info)
    if not found:
        return None, None
    if inference_result is not None:
        absent = _absent_field_paths(inference_result)
        populated = [f for f in found if f[2] not in absent]
        if populated:
            found = populated
    alerts = sum(
        1
        for confidence, threshold, _ in found
        if confidence
        < (threshold if threshold is not None else DEFAULT_ALERT_THRESHOLD)
    )
    return alerts, len(found)


# ---------------------------------------------------------------------------
# Review-effort estimator: "how many documents must a human review to reach a
# target accuracy?" Reads the measured confidence→accuracy curve for this test set
# (see idp_common.evaluation.confidence_curve) and the set's per-document
# confidences, returning review depth, implied cutoff, effort, audit sample and how
# far the numbers can be trusted.
# ---------------------------------------------------------------------------


def get_annotation_queue(args, event=None):
    """The worst-first annotation queue for one test set.

    Documents are ordered worst-first, so each review removes the most expected
    error, and carry claim state so several annotators can work one set in parallel.
    Documents claimed by someone else or already reviewed drop out of everyone
    else's "next in queue"; exclusivity itself is enforced by ``claimReview``, which
    this query only reflects.

    Access is scoped server-side to the caller's ``allowedTestSets``, so a shared
    queue link is a navigation aid and not a credential.
    """
    test_set_id = args["testSetId"]
    limit = max(1, min(int(args.get("limit") or 50), 200))
    include_completed = bool(args.get("includeCompleted"))

    if not validate_test_set_name(test_set_id):
        raise Exception("Invalid test set id")

    # Scope check precedes any read, so an unauthorized caller cannot even learn
    # whether the set exists.
    assert_can_access_test_set(event, test_set_id)

    meta = db_client.get_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})
    if not meta:
        raise Exception(f"Test set '{test_set_id}' not found")

    label_job = _harvest_active_label_job(test_set_id, meta)

    documents = _collect_queue_documents(test_set_id)
    claims = _claim_state_for_documents(test_set_id, meta, documents)

    caller = caller_email(event)
    # Review operations (claimReview / completeSectionReview) key on the pipeline
    # copy of a document, "{runId}/{filename}", not the test-set key. It is returned
    # so no client has to reconstruct that backend key layout.
    # Per document, not one pointer for the set: see _run_id_by_object_key.
    per_doc_run, default_run = _run_id_by_object_key(test_set_id)
    if not per_doc_run and not default_run:
        default_run = meta.get("labelJobId")
    entries = []
    for doc in documents:
        state = claims.get(doc["objectKey"], {})
        owner = state.get("owner") or ""
        status = state.get("status") or ""
        reviewed = doc.get("labelSource") == LABEL_SOURCE_HUMAN or status in (
            "Review Completed",
            "Completed",
            "Review Skipped",
            "Skipped",
        )
        claimed_by_other = bool(owner) and owner != caller
        # A pipeline copy exists only for documents the labeling run processed, and
        # draft labeling skips documents that already have ground truth. Offering a
        # review key without one yields "Document <runId>/<file> not found".
        doc_run = per_doc_run.get(doc["objectKey"]) or default_run
        has_pipeline_copy = bool(doc_run) and state.get("exists", False)
        entries.append(
            {
                "objectKey": doc["objectKey"],
                "inputKey": doc["inputKey"],
                "reviewObjectKey": (
                    f"{doc_run}/{doc['objectKey']}" if has_pipeline_copy else None
                ),
                "minConfidence": doc.get("minConfidence"),
                "confidenceThreshold": doc.get("confidenceThreshold"),
                "alertCount": doc.get("alertCount"),
                "fieldCount": doc.get("fieldCount"),
                "labelSource": doc.get("labelSource"),
                # So a reviewer can see what each document was classified as
                # without opening it. A wrong class is not visible any other way
                # from the queue: extraction under the wrong schema can be
                # confidently wrong, so the row's confidence and alert count look
                # entirely normal.
                "documentClasses": doc.get("documentClasses") or [],
                "sectionCount": len(doc.get("sections") or []),
                "sections": doc.get("sections") or [],
                "claimedBy": owner or None,
                "claimedByMe": bool(owner) and owner == caller,
                "reviewStatus": status or None,
                "reviewed": reviewed,
                "available": not reviewed and not claimed_by_other,
            }
        )

    def sort_key(entry):
        """Most-alerts-first. Unlabeled sorts first, authored ground truth last.

        Ranked by alert count rather than the single lowest score, since eight weak
        fields is more review work than one weak field at a lower confidence;
        minConfidence breaks ties.

        A missing confidence means two different things, so the two must not share a
        sentinel: an unlabeled document has had nothing attempted and needs the most
        attention, while authored ground truth was never predicted and needs the
        least.
        """
        confidence = entry["minConfidence"]
        if confidence is not None:
            # Negated so more alerts sort earlier while confidence still sorts
            # ascending within a tie.
            return (0, -(entry["alertCount"] or 0), confidence, entry["objectKey"])
        if entry["labelSource"] in (None, LABEL_SOURCE_DRAFT):
            return (-1, 0, 0.0, entry["objectKey"])
        return (1, 0, 0.0, entry["objectKey"])

    entries.sort(key=sort_key)

    reviewed_count = sum(1 for e in entries if e["reviewed"])
    inspected = len(entries)
    # The set's real size, not the inspected page: the page is capped, so counting it
    # would report a 2000-document set as the cap and claim "0 remaining" while most
    # of the set was untouched.
    total = int(meta.get("fileCount", 0) or 0) or inspected
    # Authored ground truth counts toward totalDocs but has no pipeline copy to
    # claim, so a mixed set cannot reach 100% here. Excluding it instead would report
    # an uploaded set as fully annotated, which
    # test_uploaded_ground_truth_is_not_counted_as_review_work forbids.
    queue = [e for e in entries if include_completed or not e["reviewed"]]

    # The open transition, so the workspace does not have to call the mutation to discover
    # one — that call snapshots, and probing with it would open a transition just by
    # visiting the page. Bound once: `_as_int` is Optional, so calling it twice leaves the
    # type checker unable to see that the subtraction is guarded.
    draft_version = _as_int(meta.get("draftVersion")) or None

    result = {
        "testSetId": test_set_id,
        "totalDocs": total,
        "inspectedDocs": inspected,
        "reviewedDocs": reviewed_count,
        "draftVersion": draft_version,
        "baseVersion": draft_version - 1 if draft_version else None,
        # Documents beyond the inspected page are unreviewed, so they count here.
        "remainingDocs": max(0, total - reviewed_count),
        "claimedByOthers": sum(
            1
            for e in entries
            if not e["reviewed"] and e["claimedBy"] and not e["claimedByMe"]
        ),
        "documents": queue[:limit],
        # The next document this caller can open; None when their queue is drained.
        "nextObjectKey": next((e["objectKey"] for e in queue if e["available"]), None),
        "labelJobStatus": (label_job or {}).get("status"),
        "labelJobLabeled": _as_int((label_job or {}).get("labeled")),
        "labelJobTotal": _as_int((label_job or {}).get("total")),
    }
    logger.info(
        f"getAnnotationQueue({test_set_id}) for {caller or 'service'}: "
        f"{result['remainingDocs']}/{total} remaining "
        f"({inspected} inspected), "
        f"{result['claimedByOthers']} claimed by others"
    )
    return result


def _collect_queue_documents(test_set_id):
    """Documents in the set with their label metadata, up to the queue cap.

    Capped and time-bounded because each page reads every section on it from S3
    (~24s per 200 documents), which times the Lambda out on a large set. A short
    queue is workable, since annotators take documents from the front; a timeout
    means the workspace cannot open. inspectedDocs reports how much was ranked.
    """
    documents = []
    next_token = None
    started = time.monotonic()
    while len(documents) < MAX_DOCS_FOR_ESTIMATE:
        page = get_test_set_documents(
            {
                "testSetId": test_set_id,
                "limit": min(200, MAX_DOCS_FOR_ESTIMATE - len(documents)),
                "nextToken": next_token,
            }
        )
        documents.extend(page.get("documents") or [])
        next_token = page.get("nextToken")
        if not next_token:
            break
        if time.monotonic() - started > SAMPLING_TIME_BUDGET_SECONDS:
            logger.warning(
                f"getAnnotationQueue({test_set_id}): stopped collecting at "
                f"{len(documents)} document(s) after "
                f"{SAMPLING_TIME_BUDGET_SECONDS}s"
            )
            break
    return documents


def _claim_state_for_documents(test_set_id, meta, documents):
    """Map objectKey → claim/review state from the HITL document records.

    Review happens against the pipeline copy of a document
    (``doc#{testRunId}/{filename}``), not the test-set copy, so the run prefix comes
    from the set's most recent labeling run. Without one, nothing is claimed and
    every document reads as available.
    """
    per_doc_run, default_run = _run_id_by_object_key(test_set_id)
    if not per_doc_run and not default_run:
        # Fall back to the set pointer for sets labeled before per-run resolution.
        default_run = meta.get("labelJobId")
    if not per_doc_run and not default_run:
        return {}

    table_name = os.environ["TRACKING_TABLE"]
    resource = boto3.resource("dynamodb")
    state = {}

    # BatchGetItem rather than a GetItem per document; batches cap at 100 keys.
    # Each document is keyed by the run that produced ITS copy, so a re-extracted
    # document and its neighbours resolve independently.
    keys_by_pk = {}
    for doc in documents:
        run = per_doc_run.get(doc["objectKey"]) or default_run
        if run:
            keys_by_pk[f"doc#{run}/{doc['objectKey']}"] = doc["objectKey"]
    if not keys_by_pk:
        return {}
    all_keys = list(keys_by_pk)
    for start in range(0, len(all_keys), 100):
        batch = [{"PK": pk, "SK": "none"} for pk in all_keys[start : start + 100]]
        try:
            pending = {table_name: {"Keys": batch}}
            # UnprocessedKeys is normal under throttling; unretried keys would
            # silently report their documents as unclaimed.
            for _ in range(4):
                response = resource.batch_get_item(RequestItems=pending)
                for item in response.get("Responses", {}).get(table_name, []):
                    object_key = keys_by_pk.get(item.get("PK", ""))
                    if not object_key:
                        continue
                    state[object_key] = {
                        "owner": item.get("HITLReviewOwner") or "",
                        "status": item.get("HITLStatus") or "",
                        # The item's presence proves the run processed this
                        # document, which is a precondition for claiming it.
                        "exists": True,
                    }
                pending = response.get("UnprocessedKeys") or {}
                if not pending:
                    break
        except Exception as e:  # noqa: BLE001 — a missing claim is not an error
            logger.warning(
                f"Could not read claim state for {len(batch)} document(s): {e}"
            )
    return state


def estimate_review_effort(args):
    """Server-side estimate for the "set up team annotation" flow.

    The estimate always reports its own trustworthiness (``estimateConfidence`` plus
    the calibration block): on a cold or miscalibrated set a bare docs-to-review
    number looks measured when it is a prior, and understates effort when confidence
    hides errors in the auto-accepted zone.
    """
    test_set_id = args["testSetId"]
    target_accuracy = float(args.get("targetAccuracy") or 99.0)
    config_version = args.get("configVersion")

    if not validate_test_set_name(test_set_id):
        raise Exception("Invalid test set id")
    if not (0.0 < target_accuracy <= 100.0):
        raise Exception("targetAccuracy must be between 0 and 100")

    meta = db_client.get_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})
    if not meta:
        raise Exception(f"Test set '{test_set_id}' not found")

    # Default to the config the set is bound to, so the curve matches the confidence
    # semantics that produced these labels.
    if not config_version:
        config_version = meta.get("boundConfigVersion")

    tracking_table = boto3.resource("dynamodb").Table(os.environ["TRACKING_TABLE"])
    store = CurveStore(tracking_table)
    curve = store.get_curve(test_set_id, config_version)
    prior = store.get_global_prior()

    (
        doc_confidences,
        fields_per_doc,
        pages_per_doc,
        ground_truth_docs,
        alerts_per_doc,
    ) = _collect_doc_confidences(test_set_id)

    # The set's real size, which may exceed the sample. Using the sample size as the
    # total understates the work, the effort and the audit pool by however far the
    # set exceeds MAX_DOCS_FOR_ESTIMATE.
    total_docs = int(meta.get("fileCount", 0) or 0) or len(doc_confidences)
    # Ground truth is not reviewable work, so it comes off the total.
    total_docs = max(0, total_docs - ground_truth_docs)
    sampled_docs = len(doc_confidences)

    # A sample is treated as representative and repeated across the whole set, so the
    # ordering the estimator walks has one entry per real document. Repetition
    # preserves the distribution's shape, which is what the estimate depends on.
    if doc_confidences and sampled_docs < total_docs:
        repeats = -(-total_docs // sampled_docs)  # ceil
        doc_confidences = (doc_confidences * repeats)[:total_docs]
        logger.info(
            f"estimateReviewEffort({test_set_id}): extrapolating {sampled_docs} "
            f"sampled confidences across {total_docs} documents"
        )

    estimate = estimate_for_target(
        curve,
        target_accuracy,
        total_docs,
        doc_confidences=doc_confidences or None,
        prior=prior,
        fields_per_doc=fields_per_doc,
        pages_per_doc=pages_per_doc,
        alerts_per_doc=alerts_per_doc,
    )

    result = estimate.to_dict()
    result["testSetId"] = test_set_id
    result["targetAccuracy"] = target_accuracy
    result["configVersion"] = config_version
    result["reliabilityTable"] = curve.reliability_table(prior)
    # Surfaced so a caller can say how much of the set was actually inspected.
    result["sampledDocs"] = sampled_docs
    logger.info(
        f"estimateReviewEffort({test_set_id}, target={target_accuracy}): "
        f"{estimate.docs_to_review}/{total_docs} docs, "
        f"confidence={estimate.estimate_confidence.value}"
    )
    return result


# Cap on how many documents the estimator reads confidences for. Each document's
# sections are read from S3, costing ~24s per 200-document page, so a larger cap
# needs several pages and exceeds the resolver's 60s timeout. 200 keeps the call
# inside one page and already pins the curve (10 reliability bins; document shape is
# measured from at most 20 sections). The cost is a wider docs-to-review range on
# very large sets, which sampledDocs and estimateConfidence report.
MAX_DOCS_FOR_ESTIMATE = 200

# Hard stop for any operation that pages the document list, independent of the
# document cap: a set whose pages are unusually slow must yield a narrower sample
# rather than a timeout. Shared by the estimator and the annotation queue.
SAMPLING_TIME_BUDGET_SECONDS = 25

# Ceiling on baseline sections read to characterise document shape for the effort
# model. Each read is a separate S3 GET, and sets vary widely in sections per
# document, so the bound has to be on reads rather than on usable results.
MAX_SECTIONS_FOR_FIELD_SAMPLE = 40

# Shared by every labeling job a single request harvests. Copying results is
# several S3 calls per section, so a large set cannot finish in one pass; stopping
# short leaves the job RUNNING and the next poll continues.
HARVEST_TIME_BUDGET_SECONDS = 20

# Document states the pipeline never leaves. A labeling job waiting on one of
# these would never complete, and every caller that shows a job re-harvests on a
# timer, so the wait is not free. Derived from the enum rather than spelled out, so
# a new terminal state cannot silently start hanging jobs.
TERMINAL_DOCUMENT_STATUSES = frozenset(
    s.value for s in (Status.FAILED, Status.ABORTED, Status.REDACTED_SUPERSEDED)
)

# How long a job waits for a document that has no tracking record at all before
# giving up on it. Absence cannot be distinguished from "not started yet", so this
# is deliberately far beyond any plausible processing time.
STALE_LABEL_JOB_HOURS = 6


def _collect_doc_confidences(test_set_id):
    """Per-document minimum confidence, plus observed doc shape for the effort model.

    Reuses the same baseline read as the document list, so the estimator orders
    documents exactly as the reviewer will see them; an estimate over a different
    ordering would not describe the work the user is about to do.

    Returns ``(confidences, fields_per_doc, pages_per_doc, ground_truth_docs,
    alerts_per_doc)``. The
    two shape figures fall back to the module defaults when the baseline carries
    nothing to measure. ``ground_truth_docs`` counts inspected documents that already
    have ground truth and are therefore absent from ``confidences``.
    """
    test_set_bucket = os.environ["TEST_SET_BUCKET"]
    documents = []
    next_token = None
    started = time.monotonic()

    while len(documents) < MAX_DOCS_FOR_ESTIMATE:
        page = get_test_set_documents(
            {
                "testSetId": test_set_id,
                "limit": min(200, MAX_DOCS_FOR_ESTIMATE - len(documents)),
                "nextToken": next_token,
            }
        )
        documents.extend(page.get("documents") or [])
        next_token = page.get("nextToken")
        if not next_token:
            break
        # A narrower sample still produces a usable estimate, and says so via
        # sampledDocs; a timeout produces nothing.
        if time.monotonic() - started > SAMPLING_TIME_BUDGET_SECONDS:
            logger.warning(
                f"estimateReviewEffort({test_set_id}): stopped sampling at "
                f"{len(documents)} document(s) after "
                f"{SAMPLING_TIME_BUDGET_SECONDS}s; the estimate is based on that "
                "sample"
            )
            break

    confidences = []
    alert_counts = []
    ground_truth_docs = 0
    for doc in documents:
        if doc.get("minConfidence") is not None or doc.get("labelSource") in (
            None,
            LABEL_SOURCE_DRAFT,
        ):
            confidences.append(doc.get("minConfidence"))
            if doc.get("alertCount") is not None:
                alert_counts.append(int(doc["alertCount"]))
        else:
            ground_truth_docs += 1

    # Mean alerts per reviewable document — what the effort model charges for,
    # rather than every field in the document. None when no document carries
    # assessment data, so the model falls back to its assumed rate.
    alerts_per_doc = sum(alert_counts) / len(alert_counts) if alert_counts else None

    # Effort model input: the number of fields a reviewer has to check drives review
    # time far more than a global average does, so measure it where possible.
    # Bounded by sections READ, not by counts collected: a set whose baselines carry
    # no fields yet (an unlabeled set, or split-only ground truth with an empty
    # inference_result) yields no counts at all, so a success-based cap never trips
    # and this reads every section in the set one at a time.
    field_counts = []
    sections_read = 0
    for doc in documents:
        if sections_read >= MAX_SECTIONS_FOR_FIELD_SAMPLE or len(field_counts) >= 20:
            break
        for section in doc.get("sections") or []:
            if (
                sections_read >= MAX_SECTIONS_FOR_FIELD_SAMPLE
                or len(field_counts) >= 20
            ):
                break
            key = section.get("baselineKey")
            if not key:
                continue
            sections_read += 1
            count = _count_baseline_fields(test_set_bucket, key)
            if count:
                field_counts.append(count)

    fields_per_doc = (
        sum(field_counts) / len(field_counts)
        if field_counts
        else DEFAULT_FIELDS_PER_DOC
    )
    # Sections stand in for pages: the baseline records sections, not page counts,
    # and a section is the unit a reviewer opens.
    section_counts = [len(doc.get("sections") or []) for doc in documents]
    pages_per_doc = (
        sum(section_counts) / len(section_counts)
        if section_counts
        else DEFAULT_PAGES_PER_DOC
    )
    return (
        confidences,
        fields_per_doc,
        max(1.0, pages_per_doc),
        ground_truth_docs,
        alerts_per_doc,
    )


def _count_baseline_fields(bucket, key):
    """Number of leaf fields in a baseline section result, or None if unreadable."""
    try:
        body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
        result = json.loads(body)
    except Exception as e:  # noqa: BLE001 — effort model input is best-effort
        logger.warning(f"Could not read baseline for field count {key}: {e}")
        return None

    def count(node):
        if isinstance(node, dict):
            return sum(count(v) for v in node.values()) or len(node)
        if isinstance(node, list):
            return sum(count(v) for v in node)
        return 1

    inference = result.get("inference_result")
    return count(inference) if inference else None


def _label_job_is_stale(job):
    """True when a job is old enough that a missing document will never arrive."""
    created = str(job.get("createdAt") or "")
    if not created:
        return False
    try:
        started = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return False
    age_hours = (datetime.now(started.tzinfo) - started).total_seconds() / 3600.0
    return age_hours >= STALE_LABEL_JOB_HOURS


def _harvest_label_job(job, deadline=None):
    """Copy finished pipeline results into the test set's baseline as drafts.

    For each of the run's documents whose processing has completed, read the
    section extraction results the pipeline wrote and store them at
    ``{test_set_id}/baseline/<doc>/sections/<n>/result.json`` — the same layout
    the ground-truth editor and scoring already read — tagged
    ``labelSource=draft-machine`` with the per-field confidence preserved.

    Idempotent and non-destructive: a document already carrying a human-reviewed
    label is skipped, so re-harvesting (or re-running a job) never overwrites
    confirmed ground truth.

    Documents already copied are recorded on the job and not re-read. Without
    that, every poll re-did the whole set — several S3 reads and writes per
    section — so a large set grew slower to harvest the closer it got to done,
    and eventually could not finish inside the resolver timeout. ``deadline``
    (a :func:`time.monotonic` value) stops the pass early; the unfinished
    documents stay pending, so the job remains RUNNING and the next poll resumes
    where this one stopped.
    """
    test_set_id = job["testSetId"]
    job_id = job["jobId"]
    test_set_bucket = os.environ["TEST_SET_BUCKET"]
    tracking_table = boto3.resource("dynamodb").Table(os.environ["TRACKING_TABLE"])

    run = tracking_table.get_item(
        Key={"PK": f"testrun#{job_id}", "SK": "metadata"}
    ).get("Item")
    if not run:
        return _fail_label_job(job, f"Labeling run '{job_id}' not found")
    if run.get("Status") == "FAILED":
        return _fail_label_job(job, run.get("Error") or "Labeling run failed")

    wanted = set(job.get("objectKeys") or [])
    files = [f for f in (run.get("Files") or []) if not wanted or f in wanted]

    done = set(job.get("harvestedFiles") or [])
    failed = list(job.get("failedFiles") or [])
    resolved = done | set(failed)
    pending = 0
    out_of_time = False
    for file_name in files:
        if file_name in resolved:
            continue
        if deadline is not None and time.monotonic() > deadline:
            # Remaining documents are pending, not lost: the job stays RUNNING and
            # the next poll picks them up.
            out_of_time = True
            pending += 1
            continue

        doc = tracking_table.get_item(
            Key={"PK": f"doc#{job_id}/{file_name}", "SK": "none"}
        ).get("Item")
        status = (doc or {}).get("ObjectStatus")
        if status != "COMPLETED":
            if doc is None and _label_job_is_stale(job):
                # No tracking item at all: either the copy never landed or the
                # document was removed. Indistinguishable from "not started yet" by
                # status, so it is only given up on once the job is far past any
                # plausible processing time.
                logger.warning(
                    f"Draft labeling job {job_id}: no tracking record for "
                    f"'{file_name}' after {STALE_LABEL_JOB_HOURS}h; recording it "
                    "as failed"
                )
                failed.append(file_name)
            elif status in TERMINAL_DOCUMENT_STATUSES:
                # Resolved with an error, not pending. Counting a document that has
                # already failed as pending left the job RUNNING forever — and every
                # caller that displays a job drives the harvest on a timer, so the
                # banner never cleared and each tick re-read the whole set.
                logger.warning(
                    f"Draft labeling job {job_id}: '{file_name}' ended {status}; "
                    "recording it as failed rather than waiting for it"
                )
                failed.append(file_name)
            else:
                pending += 1
            continue

        try:
            _write_draft_labels_for_doc(
                test_set_bucket,
                test_set_id,
                file_name,
                doc.get("Sections") or [],
                config_version=job.get("configVersion") or doc.get("ConfigVersion"),
            )
            # TestSetId on the pipeline document is how completeSectionReview knows
            # to write back to the baseline, tag the label reviewed-human and record
            # the curve observation; without it the save reports success and does
            # none of that.
            if not doc.get("TestSetId"):
                tracking_table.update_item(
                    Key={"PK": f"doc#{job_id}/{file_name}", "SK": "none"},
                    UpdateExpression="SET TestSetId = :tsid",
                    ExpressionAttributeValues={":tsid": test_set_id},
                )
            # Recorded only once both steps land, so a partial failure is retried.
            # Writing no sections still counts: that means every section was
            # already human-owned, which needs no draft.
            done.add(file_name)
        except Exception as e:  # noqa: BLE001 — one bad doc must not fail the job
            # Deliberately not counted pending: a document that fails every time
            # would hold the job RUNNING forever. It is left out of the harvested
            # set, so it retries while the job is in flight and then stops.
            logger.error(
                f"Draft labeling: failed to harvest '{file_name}' for job {job_id}: {e}"
            )

    labeled = len(done)
    if pending:
        status = "RUNNING"
    elif failed and not labeled:
        # Nothing to harvest and nothing left to wait for.
        status = "FAILED"
    else:
        status = "COMPLETED"
    now = datetime.utcnow().isoformat() + "Z"
    update_expr = "SET #st = :s, labeled = :n, harvestedFiles = :h, failedFiles = :f"
    expr_values = {
        ":s": status,
        ":n": labeled,
        ":h": sorted(done),
        ":f": sorted(set(failed)),
    }
    if status == "FAILED":
        update_expr += ", #er = :e"
        expr_values[":e"] = (
            f"All {len(set(failed))} document(s) failed processing; no labels were "
            "produced"
        )
    if status in ("COMPLETED", "FAILED"):
        update_expr += ", completedAt = :c"
        expr_values[":c"] = now

    expr_names = {"#st": "status"}
    if status == "FAILED":
        expr_names["#er"] = "error"
    db_client.update_item(
        key={"PK": f"testset#{test_set_id}", "SK": _label_job_sk(job_id)},
        update_expression=update_expr,
        expression_attribute_names=expr_names,
        expression_attribute_values=expr_values,
    )

    meta_expr = "SET labelJobStatus = :s"
    meta_values = {":s": status}
    if status == "COMPLETED":
        # The set now carries machine labels; publishing freezes them as unreviewed.
        meta_expr += ", labelState = :ls"
        meta_values[":ls"] = "draft"
    db_client.update_item(
        key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
        update_expression=meta_expr,
        expression_attribute_values=meta_values,
    )

    logger.info(
        f"Draft labeling job {job_id}: labeled={labeled} pending={pending} "
        f"failed={len(set(failed))} status={status}"
        + (
            f" (stopped after {HARVEST_TIME_BUDGET_SECONDS}s; resuming on the "
            "next poll)"
            if out_of_time
            else ""
        )
    )
    updated = dict(job)
    updated.update(
        {
            "status": status,
            "labeled": labeled,
            "harvestedFiles": sorted(done),
            "failedFiles": sorted(set(failed)),
        }
    )
    if status == "FAILED":
        updated["error"] = expr_values[":e"]
    if status in ("COMPLETED", "FAILED"):
        updated["completedAt"] = now
    return updated


def _write_draft_labels_for_doc(
    test_set_bucket, test_set_id, file_name, sections, config_version=None
):
    """Write one document's sections into the test-set baseline as draft labels.

    Returns True if anything was written. Sections already reviewed by a human
    are left untouched.

    Sections a previous run wrote that this one no longer produces are pruned —
    see :func:`_prune_superseded_draft_sections`.
    """
    wrote = False
    written_section_ids = set()
    for section in sections:
        section_id = str(section.get("Id") or section.get("SectionId") or "")
        output_uri = section.get("OutputJSONUri") or ""
        if not section_id or not output_uri.startswith("s3://"):
            continue

        baseline_key = (
            f"{test_set_id}/baseline/{file_name}/sections/{section_id}/result.json"
        )
        if _existing_label_is_human(test_set_bucket, baseline_key):
            logger.info(f"Draft labeling: keeping reviewed label at {baseline_key}")
            continue

        src_bucket, src_key = output_uri[len("s3://") :].split("/", 1)
        body = s3_client.get_object(Bucket=src_bucket, Key=src_key)["Body"].read()
        result = json.loads(body)

        explainability = result.get("explainability_info")
        inference = result.get("inference_result")
        min_conf = _min_confidence(explainability, inference)
        result["labelSource"] = LABEL_SOURCE_DRAFT
        # completeSectionReview keys the confidence curve on
        # metadata.config_version, so without it review observations land in the
        # version-agnostic curve while scoring observations go to the per-version
        # one, and the two halves of the calibration signal never combine.
        if config_version:
            metadata = result.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            metadata.setdefault("config_version", config_version)
            result["metadata"] = metadata
        if min_conf is not None:
            result["minConfidence"] = min_conf
            threshold = _confidence_threshold(explainability, inference)
            if threshold is not None:
                result["confidenceThreshold"] = threshold

        s3_client.put_object(
            Bucket=test_set_bucket,
            Key=baseline_key,
            Body=json.dumps(result, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        written_section_ids.add(section_id)
        wrote = True

    if wrote:
        _prune_superseded_draft_sections(
            test_set_bucket, test_set_id, file_name, written_section_ids
        )

    return wrote


def _prune_superseded_draft_sections(
    test_set_bucket, test_set_id, file_name, written_section_ids
):
    """Delete draft sections a previous run wrote that this one did not produce.

    Draft labeling writes one object per section, so a re-run producing fewer
    sections leaves orphans behind. They are not inert: a document's confidence is
    the minimum across its sections, so a stale low-confidence orphan holds the
    document's score down after a corrected run, and it still appears in the
    annotation queue and in scoring.

    Only the document's own ``draft-machine`` sections are eligible; ground truth is
    never touched. Runs only when this run wrote at least one section, so a partial
    failure cannot empty an existing baseline.
    """
    prefix = f"{test_set_id}/baseline/{file_name}/sections/"
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        existing = []
        for page in paginator.paginate(Bucket=test_set_bucket, Prefix=prefix):
            existing.extend(obj["Key"] for obj in page.get("Contents", []))
    except Exception as e:  # noqa: BLE001 — pruning must not fail the harvest
        logger.warning(f"Could not list baseline sections under {prefix}: {e}")
        return

    removed = 0
    for key in existing:
        if not key.endswith("/result.json"):
            continue
        section_id = key[len(prefix) :].split("/")[0]
        if section_id in written_section_ids:
            continue
        # Only a prior machine draft is disposable.
        if _existing_label_is_human(test_set_bucket, key):
            continue
        try:
            s3_client.delete_object(Bucket=test_set_bucket, Key=key)
            removed += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not prune superseded draft section {key}: {e}")

    if removed:
        logger.info(
            f"Pruned {removed} superseded draft section(s) for {file_name}: this "
            f"run produced sections {sorted(written_section_ids)}"
        )


def _existing_label_is_human(bucket, key):
    """True if an existing baseline label must not be overwritten by a draft.

    Anything present counts as human-owned unless explicitly tagged a machine draft.
    An uploaded baseline carries no labelSource at all, so absence of the tag is
    protective, not permissive: only a prior draft-machine label is replaceable.
    """
    try:
        body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:
        return False  # No existing label (or unreadable) — safe to write.
    try:
        return json.loads(body).get("labelSource") != LABEL_SOURCE_DRAFT
    except Exception:
        # Present but unparseable: not safe to clobber.
        return True


def _fail_label_job(job, message):
    db_client.update_item(
        key={
            "PK": f"testset#{job['testSetId']}",
            "SK": _label_job_sk(job["jobId"]),
        },
        update_expression="SET #st = :s, #er = :e",
        expression_attribute_names={"#st": "status", "#er": "error"},
        expression_attribute_values={":s": "FAILED", ":e": message},
    )
    db_client.update_item(
        key={"PK": f"testset#{job['testSetId']}", "SK": "metadata"},
        update_expression="SET labelJobStatus = :s",
        expression_attribute_values={":s": "FAILED"},
    )
    logger.error(f"Draft labeling job {job['jobId']} failed: {message}")
    updated = dict(job)
    updated.update({"status": "FAILED", "error": message})
    return updated


def remove_documents_from_test_set(args):
    """Remove named documents from a test set (delete input + baseline objects).

    Deletes, for each requested file name, the ``{id}/input/<file>`` object and the
    whole ``{id}/baseline/<file>/`` folder, then recounts fileCount. Membership edits
    target the mutable working draft; a later publish cuts the next version.
    """
    test_set_id = args["testSetId"]
    if not validate_test_set_name(test_set_id):
        raise Exception("Invalid test set id")
    file_names = args["fileNames"]
    logger.info(f"Removing {len(file_names)} document(s) from test set {test_set_id}")

    meta = db_client.get_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})
    if not meta:
        raise Exception(f"Test set '{test_set_id}' not found")

    test_set_bucket = os.environ["TEST_SET_BUCKET"]

    removed = 0
    for file_name in file_names:
        keys_to_delete = []
        keys_to_delete.append(f"{test_set_id}/input/{file_name}")
        # The baseline folder may contain nested section results.
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=test_set_bucket, Prefix=f"{test_set_id}/baseline/{file_name}/"
        ):
            for obj in page.get("Contents", []):
                keys_to_delete.append(obj["Key"])

        if keys_to_delete:
            # delete_objects caps at 1000 keys per request.
            for i in range(0, len(keys_to_delete), 1000):
                s3_client.delete_objects(
                    Bucket=test_set_bucket,
                    Delete={
                        "Objects": [{"Key": k} for k in keys_to_delete[i : i + 1000]]
                    },
                )
            removed += 1

    # Only the count is used, so an unlabeled set must still recount.
    validation = _validate_test_set_files(
        s3_client, test_set_bucket, test_set_id, allow_unlabeled=True
    )
    if validation.get("validation_failed"):
        # Transient S3 error inside the validator — its `input_count=0`
        # placeholder is NOT a real observation. Writing it to fileCount
        # would overwrite the correct count with 0. Bail on the counter
        # update and let the reconcile fix the row on the next
        # getTestSets when S3 recovers. fileCount stays at
        # (pre-delete-count - len(deletions)) via S3 side effect on the
        # next successful validation.
        logger.warning(
            f"Skipping fileCount update for {test_set_id}: validator failed "
            f"transiently ({validation.get('error')}). Reconcile will pick "
            "up the correct count on the next getTestSets."
        )
        return {
            "id": test_set_id,
            "name": meta.get("name"),
            "fileCount": _as_int(meta.get("fileCount")) or 0,
            "status": meta.get("status"),
            "createdAt": meta.get("createdAt"),
            "lastAddResult": f"Removed {removed} document(s)",
        }
    new_count = validation.get("input_count", 0)
    # Two REMOVEs in one UpdateItem:
    #  - lastAddResult is the ASYNCHRONOUS add flow's completion notice; this
    #    mutation is synchronous, so the caller sees the count in the response
    #    and persisting a second copy on the record produced an immortal alert
    #    that client-side dismissal could not remove.
    #  - contentSignature is dropped so the next getTestSets reconcile takes
    #    the full path once and rebuilds it — the file listing we just did
    #    doesn't include the '.source' bit the reconcile's signature also folds
    #    in, so we can't cheaply build a correct new signature here. Otherwise
    #    every remove would leave a stale signature that burns a wasted full
    #    validate + DDB write on the next poll.
    _get_tracking_table().update_item(
        Key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
        UpdateExpression="SET fileCount = :c REMOVE lastAddResult, contentSignature",
        ExpressionAttributeValues={":c": new_count},
    )
    # Drop the warm-container memo so this container's next getTestSets
    # doesn't short-circuit on the pre-remove signature. The signature-
    # divergence check in _within_reconcile_ttl would catch this anyway
    # (DDB row's contentSignature was just REMOVEd), but popping is cheap
    # and explicit.
    _RECONCILE_MEMO.pop(test_set_id, None)

    logger.info(
        f"Removed {removed} document(s) from test set {test_set_id}; "
        f"{new_count} remaining"
    )
    return {
        "id": test_set_id,
        "name": meta.get("name"),
        "fileCount": new_count,
        "status": meta.get("status"),
        "createdAt": meta.get("createdAt"),
        "lastAddResult": f"Removed {removed} document(s)",
    }


def clear_draft_labels(args):
    """Delete a test set's machine draft labels, keeping its documents.

    The harvest replaces a draft only when the new run produces a section for it, so
    a run that splits a document differently leaves orphans that drag the document's
    confidence down; this returns the set to a clean unlabeled state.

    Only ``draft-machine`` labels are removed. Reviewed, uploaded and generated
    ground truth survives, so a config retry cannot discard human work.

    Also resets the set's ``labelState`` — see the comment at the update below for
    why the reconciler cannot be left to do it.
    """
    test_set_id = args["testSetId"]
    if not validate_test_set_name(test_set_id):
        raise Exception("Invalid test set id")

    meta = db_client.get_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})
    if not meta:
        raise Exception(f"Test set '{test_set_id}' not found")

    test_set_bucket = os.environ["TEST_SET_BUCKET"]
    paginator = s3_client.get_paginator("list_objects_v2")
    candidates = []
    for page in paginator.paginate(
        Bucket=test_set_bucket, Prefix=f"{test_set_id}/baseline/"
    ):
        candidates.extend(
            obj["Key"]
            for obj in page.get("Contents", [])
            if obj["Key"].endswith("/result.json")
        )

    to_delete = [
        key
        for key in candidates
        # is_draft already implies not-human (it requires an explicit
        # draft-machine tag), so checking both doubled the S3 GETs per section
        # for no added safety.
        if _existing_label_is_draft(test_set_bucket, key)
    ]

    for i in range(0, len(to_delete), 1000):
        s3_client.delete_objects(
            Bucket=test_set_bucket,
            Delete={"Objects": [{"Key": k} for k in to_delete[i : i + 1000]]},
        )

    kept = len(candidates) - len(to_delete)
    # Without dropping the label-job pointer the set keeps reporting a job whose
    # output no longer exists.
    # Cleared, never written: a synchronous operation returns its count in the
    # response, so persisting a second copy only created an alert nothing could
    # retract. See clear_draft_labels for the full reasoning.
    #
    # Two independent skip-markers have to be invalidated here, because two different
    # reconcilers memoize against this row:
    #
    #  - labelProbedFileCount guards _reconcile_label_state, which is keyed on
    #    fileCount. Clearing drafts deletes baseline objects without changing
    #    membership, so the marker still matches and the set is skipped on every
    #    subsequent list. Observed on a dev stack: a set cleared for a retest kept
    #    reporting "Draft (machine)" and a 97.6% estimated accuracy while every one of
    #    its 100 documents read "Unlabeled", permanently.
    #  - contentSignature (plus the in-process memo) guards the file-listing
    #    reconcile. We just deleted baselines from S3, so the memoized signature no
    #    longer describes the bucket.
    #
    # Dropping the label probe marker is also what makes the ambiguous case correct.
    # With no drafts left the state is knowably "unlabeled"; with some non-draft labels
    # surviving it depends on whether coverage is still complete across documents,
    # which `kept` (a count of label objects) cannot answer. So invalidate the marker
    # and let the reconciler re-derive it through _validate_test_set_files — the same
    # helper registration uses, so the two cannot disagree about what "labeled" means.
    if kept == 0:
        db_client.update_item(
            key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
            update_expression=(
                "SET labelState = :u "
                "REMOVE lastAddResult, labelJobId, labelJobStatus, "
                "labelProbedFileCount, contentSignature"
            ),
            expression_attribute_values={":u": "unlabeled"},
        )
    else:
        db_client.update_item(
            key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
            update_expression=(
                "REMOVE lastAddResult, labelJobId, labelJobStatus, "
                "labelProbedFileCount, contentSignature"
            ),
        )
    _RECONCILE_MEMO.pop(test_set_id, None)

    logger.info(
        f"Cleared {len(to_delete)} draft label section(s) from {test_set_id}; "
        f"kept {kept} non-draft label(s)"
    )
    return {
        "id": test_set_id,
        "name": meta.get("name"),
        "fileCount": _as_int(meta.get("fileCount")) or 0,
        "status": meta.get("status"),
        "createdAt": meta.get("createdAt"),
        "lastAddResult": f"Cleared {len(to_delete)} draft label section(s)",
    }


def reset_test_set_labels(args):
    """Return a test set to unlabeled, discarding every label including reviewed ones.

    The destructive counterpart to :func:`clear_draft_labels`, which spares human
    work by design and therefore cannot return an annotated set to a clean state.
    Admin-only, and the UI requires the set id to be typed to confirm.

    Five kinds of state have to go together, or the set is left inconsistent:

    * every baseline section, including ``reviewed-human`` and uploaded ground truth
    * HITL attributes on the pipeline documents of the set's labeling runs —
      otherwise the queue still reports the set fully reviewed with no labels present
    * the labeling-job records
    * the confidence-curve observations, which would otherwise let the next run's
      estimate claim evidence from labels that no longer exist
    * ``labelState`` and the label-job pointers on the set's metadata

    Documents under ``{id}/input/`` are never touched: this resets labels, not
    membership.
    """
    test_set_id = args["testSetId"]
    if not validate_test_set_name(test_set_id):
        raise Exception("Invalid test set id")

    meta = db_client.get_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})
    if not meta:
        raise Exception(f"Test set '{test_set_id}' not found")

    test_set_bucket = os.environ["TEST_SET_BUCKET"]
    paginator = s3_client.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(
        Bucket=test_set_bucket, Prefix=f"{test_set_id}/baseline/"
    ):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))

    for i in range(0, len(keys), 1000):
        s3_client.delete_objects(
            Bucket=test_set_bucket,
            Delete={"Objects": [{"Key": k} for k in keys[i : i + 1000]]},
        )

    _clear_review_state_for_label_jobs(test_set_id)

    now = f"Reset {len(keys)} label object(s)"
    # Cleared, never written: a synchronous operation returns its count in the
    # response, so persisting a second copy only created an alert nothing could
    # retract. See clear_draft_labels for the full reasoning.
    # contentSignature is dropped so the next warm-container reconcile within
    # the TTL window doesn't skip on a stale signature — every baseline just
    # went away, so the memoized signature no longer describes S3.
    db_client.update_item(
        key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
        update_expression=(
            "SET labelState = :u "
            "REMOVE lastAddResult, labelJobId, labelJobStatus, contentSignature"
        ),
        expression_attribute_values={":u": "unlabeled"},
    )
    _RECONCILE_MEMO.pop(test_set_id, None)

    logger.info(f"Reset {test_set_id}: deleted {len(keys)} baseline object(s)")
    return {
        "id": test_set_id,
        "name": meta.get("name"),
        "fileCount": _as_int(meta.get("fileCount")) or 0,
        "status": meta.get("status"),
        "createdAt": meta.get("createdAt"),
        "lastAddResult": now,
    }


# HITL attributes written by the review API. Cleared wholesale on reset, since a
# document whose labels are gone must not still read as reviewed.
_HITL_ATTRS = (
    "HITLStatus",
    "HITLCompleted",
    "HITLReviewHistory",
    "HITLSectionsCompleted",
    "HITLSectionsPending",
    "HITLSectionsSkipped",
    "HITLReviewOwner",
    "HITLReviewOwnerEmail",
    "HITLReviewedBy",
    "HITLReviewedByEmail",
    "HITLPendingReview",
)


def _clear_review_state_for_label_jobs(test_set_id):
    """Drop review state, labeling jobs and curve observations for a test set.

    Best-effort per item: a document that cannot be updated must not abort the
    reset and leave the set half-cleared.
    """
    tracking_table = boto3.resource("dynamodb").Table(os.environ["TRACKING_TABLE"])
    items = []
    start_key = None
    # Bounded rather than `while True`: a query that keeps returning the same
    # continuation token would otherwise spin until the Lambda timeout. A test set
    # carries one metadata item plus a handful of job and curve records, so this
    # ceiling is far above any real set.
    for _ in range(100):
        page = db_client.query(
            key_condition_expression="PK = :pk",
            expression_attribute_values={":pk": f"testset#{test_set_id}"},
            exclusive_start_key=start_key,
        )
        items.extend(page.get("Items") or [])
        next_key = page.get("LastEvaluatedKey")
        if not next_key or next_key == start_key:
            break
        start_key = next_key

    remove_expr = "REMOVE " + ", ".join(_HITL_ATTRS)
    for item in items:
        sk = item.get("SK", "")
        if sk.startswith("labeljob#"):
            job_id = item.get("jobId")
            for file_name in item.get("objectKeys") or []:
                try:
                    tracking_table.update_item(
                        Key={"PK": f"doc#{job_id}/{file_name}", "SK": "none"},
                        UpdateExpression=remove_expr,
                    )
                except Exception as e:  # noqa: BLE001 — one doc must not stop the reset
                    logger.warning(f"Could not clear review state for {file_name}: {e}")
            db_client.delete_item({"PK": f"testset#{test_set_id}", "SK": sk})
        elif sk.startswith("curve#"):
            db_client.delete_item({"PK": f"testset#{test_set_id}", "SK": sk})


def _existing_label_is_draft(test_set_bucket, key):
    """True when this label is explicitly a machine draft.

    Not the inverse of "human": a baseline with no labelSource is ground truth
    supplied when the set was created, and deleting it would destroy user data.
    """
    try:
        body = s3_client.get_object(Bucket=test_set_bucket, Key=key)["Body"].read()
        return json.loads(body).get("labelSource") == LABEL_SOURCE_DRAFT
    except Exception as e:  # noqa: BLE001 — an unreadable label is not deletable
        logger.warning(f"Could not read {key} to check label source: {e}")
        return False


def update_test_set(args):
    logger.info(f"Updating test set: {args}")

    input_data = args["input"]
    test_set_id = input_data["id"]
    description = input_data.get("description")
    document_class_type = input_data.get("documentClassType")

    # Validate description if provided
    if description is not None and not validate_description(description):
        raise Exception("Description cannot exceed 500 characters")

    # Look up existing test set
    item = db_client.get_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})

    if not item:
        raise Exception(f"Test set '{test_set_id}' not found")

    # Build update expression dynamically
    update_parts = []
    expression_values = {}
    expression_names = {}

    if description is not None:
        update_parts.append("#desc = :desc")
        expression_values[":desc"] = description
        expression_names["#desc"] = "description"

    # Check if we need to remove documentClassType
    remove_expression = False
    if "documentClassType" in input_data and document_class_type is None:
        # Explicitly remove documentClassType if set to None
        remove_expression = True
    elif document_class_type is not None:
        # Set documentClassType to the new value
        update_parts.append("documentClassType = :docType")
        expression_values[":docType"] = document_class_type

    if not update_parts and not remove_expression:
        # No updates requested, just return current item
        return {
            "id": item["id"],
            "name": item["name"],
            "description": item.get("description", ""),
            "filePattern": item.get("filePattern", ""),
            "fileCount": item.get("fileCount"),
            "status": item.get("status"),
            "createdAt": item["createdAt"],
            "documentClassType": item.get("documentClassType"),
        }

    # Perform the update
    tracking_table = os.environ["TRACKING_TABLE"]
    table = boto3.resource("dynamodb").Table(tracking_table)

    # Build update expression
    update_expression = ""
    if update_parts:
        update_expression = f"SET {', '.join(update_parts)}"
    if remove_expression:
        if update_expression:
            update_expression += " REMOVE documentClassType"
        else:
            update_expression = "REMOVE documentClassType"

    update_kwargs = {
        "Key": {"PK": f"testset#{test_set_id}", "SK": "metadata"},
        "UpdateExpression": update_expression,
        "ReturnValues": "ALL_NEW",
    }

    if expression_names:
        update_kwargs["ExpressionAttributeNames"] = expression_names
    if expression_values:
        update_kwargs["ExpressionAttributeValues"] = expression_values

    response = table.update_item(**update_kwargs)
    updated_item = response["Attributes"]

    logger.info(f"Updated test set {test_set_id}")

    return {
        "id": updated_item["id"],
        "name": updated_item["name"],
        "description": updated_item.get("description", ""),
        "filePattern": updated_item.get("filePattern", ""),
        "fileCount": updated_item.get("fileCount"),
        "status": updated_item.get("status"),
        "createdAt": updated_item["createdAt"],
        "documentClassType": updated_item.get("documentClassType"),
    }


def delete_test_sets(args):
    logger.info(f"Deleting test sets: {args['testSetIds']}")

    test_set_ids = args["testSetIds"]
    test_set_bucket = os.environ["TEST_SET_BUCKET"]

    for test_set_id in test_set_ids:
        # Delete files from test set bucket. Both APIs cap at 1000 keys per call
        # (list_objects_v2 and delete_objects), so both must be looped: an
        # unpaginated pass orphans every object past the first 1000, leaving files
        # in the bucket after the DynamoDB record is gone. Test sets of a few
        # thousand objects are routine.
        try:
            deleted_count = 0
            continuation_token = None
            while True:
                list_kwargs = {
                    "Bucket": test_set_bucket,
                    "Prefix": f"{test_set_id}/",
                }
                if continuation_token:
                    list_kwargs["ContinuationToken"] = continuation_token
                response = s3_client.list_objects_v2(**list_kwargs)

                objects_to_delete = [
                    {"Key": key}
                    for key in (obj.get("Key") for obj in response.get("Contents", []))
                    if key
                ]
                for i in range(0, len(objects_to_delete), 1000):
                    batch = objects_to_delete[i : i + 1000]
                    s3_client.delete_objects(
                        Bucket=test_set_bucket, Delete={"Objects": batch}
                    )
                    deleted_count += len(batch)

                if not response.get("IsTruncated"):
                    break
                continuation_token = response.get("NextContinuationToken")
                if not continuation_token:
                    # A truncated response without a token would loop forever.
                    logger.warning(
                        f"Truncated listing without a continuation token for "
                        f"test set {test_set_id}; stopping after {deleted_count} objects"
                    )
                    break

            if deleted_count:
                logger.info(f"Deleted {deleted_count} files for test set {test_set_id}")

        except Exception as e:
            logger.error(f"Failed to delete files for test set {test_set_id}: {str(e)}")

        # Delete tracking table record + drop the warm-container memo entry.
        # An id could later be reused (test-set names are user-chosen), and a
        # stale memo entry from a deleted namesake would occupy memory and
        # produce spurious signature comparisons on the new set (the DDB
        # signature-divergence check catches it, but popping is cleaner).
        db_client.delete_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})
        _RECONCILE_MEMO.pop(test_set_id, None)

    logger.info(f"Deleted {len(test_set_ids)} test sets")
    return True


def get_test_sets():
    logger.info("Retrieving all test sets and scanning for direct uploads")

    # Use GSI to find testset PK/SK keys efficiently, then BatchGetItem for full records.
    # This avoids scanning the entire TrackingTable (which includes all documents).
    tracking_table = boto3.resource("dynamodb").Table(os.environ["TRACKING_TABLE"])
    items = []
    try:
        from boto3.dynamodb.conditions import Key as DDBKey

        # Step 1: GSI query to get testset keys (lightweight - only projected attrs)
        gsi_items = []
        query_kwargs = {
            "IndexName": "TypeDateIndex",
            "KeyConditionExpression": DDBKey("ItemType").eq("testset"),
            "ProjectionExpression": "PK, SK",
        }
        while True:
            response = tracking_table.query(**query_kwargs)
            gsi_items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        logger.info(f"GSI query found {len(gsi_items)} testset keys")

        if gsi_items:
            # Step 2: BatchGetItem to fetch full records from base table
            keys = [{"PK": item["PK"], "SK": item["SK"]} for item in gsi_items]
            # DynamoDB BatchGetItem supports max 100 keys per call
            for i in range(0, len(keys), 100):
                batch_keys = keys[i : i + 100]
                batch_response = boto3.resource("dynamodb").batch_get_item(
                    RequestItems={os.environ["TRACKING_TABLE"]: {"Keys": batch_keys}}
                )
                items.extend(
                    batch_response.get("Responses", {}).get(
                        os.environ["TRACKING_TABLE"], []
                    )
                )
            logger.info(f"BatchGetItem returned {len(items)} full testset records")
    except Exception as e:
        logger.warning(f"GSI+BatchGet failed, falling back to scan: {e}")
        items = []

    # Fallback to scan only if GSI approach failed
    if not items:
        items = db_client.scan_all(
            filter_expression="begins_with(PK, :pk) AND SK = :sk",
            expression_attribute_values={":pk": "testset#", ":sk": "metadata"},
        )

    existing_test_sets = {}
    result = []

    # Sets that gained ground truth after registration still say "unlabeled"; see
    # _reconcile_label_state. Done before building the response so the repair is
    # visible on the same call that discovers it.
    _reconcile_label_state(items)
    # A non-terminal status whose owning job has disappeared would otherwise show as a
    # permanent spinner; see _reap_abandoned_test_sets.
    _reap_abandoned_test_sets(items)

    for item in items:
        # GSI projection may not include 'id' - derive from PK if needed
        test_set_id = item.get("id") or item.get("PK", "").replace("testset#", "")
        existing_test_sets[test_set_id] = item
        result.append(
            {
                "id": test_set_id,
                "name": item["name"],
                "description": item.get("description", ""),
                "filePattern": item.get("filePattern", ""),
                "fileCount": item.get(
                    "fileCount"
                ),  # Returns None if attribute doesn't exist
                "source": item.get(
                    "source"
                ),  # 'uploaded' | 'synthetic'; None for pre-existing records
                "latestVersion": item.get(
                    "latestVersion"
                ),  # highest published version (None if never published)
                "activeReference": item.get(
                    "activeReference"
                ),  # version scoring runs compare against
                "labelState": item.get(
                    "labelState"
                ),  # 'unlabeled' | 'draft' | 'labeled'; None for pre-existing records
                "labelJobId": item.get("labelJobId"),
                "labelJobStatus": item.get("labelJobStatus"),
                "status": item.get("status"),
                "createdAt": item["createdAt"],
                # Set by _reconcile_test_set_tracking_entry when direct-S3
                # edits are detected; absent on rows that predate reconcile.
                "updatedAt": item.get("updatedAt"),
                "error": item.get(
                    "error"
                ),  # Include error message for failed test sets
                "documentClassType": item.get("documentClassType"),
                # Optional preselected config version. Absent for the stack-managed
                # benchmark sets, which rely on the id==version-name convention.
                "configVersion": item.get("configVersion"),
            }
        )
    # Scan TestSetBucket for direct uploads
    try:
        test_set_bucket = os.environ["TEST_SET_BUCKET"]

        # Track which test sets still exist in S3
        s3_test_sets = set()

        # List all top-level prefixes (potential test sets)
        paginator = s3_client.get_paginator("list_objects_v2")
        page_iterator = paginator.paginate(Bucket=test_set_bucket, Delimiter="/")

        # Per-request reconcile budget. Cold-container invocations pay 2
        # paginated list_objects_v2 + 1 HeadObject per registered prefix
        # before the warm-container memo kicks in, and this resolver runs
        # under a 60 s timeout with 256 MB. Cap the work here the same way
        # ``_reconcile_label_state`` caps its own probes (25 / 5 s) — the
        # remainder is picked up by the next poll, which is what the memo
        # is designed for. The listing paths do NOT skip on this budget;
        # only the reconcile-vs-S3 probes are bounded.
        reconcile_probes = 0
        reconcile_deadline = time.monotonic() + RECONCILE_PROBE_BUDGET_SECONDS

        for page in page_iterator:
            # Check common prefixes (folders)
            for prefix_info in page.get("CommonPrefixes", []):
                prefix = prefix_info["Prefix"].rstrip("/")
                s3_test_sets.add(prefix)

                # Already-registered folders: reconcile in place. Skipping here
                # (as the original code did) leaves fileCount and status stale
                # forever when a user drops more files into `<prefix>/input/`
                # directly in S3 — no S3 notification fires for that, so this
                # lazy scan is the only chance to refresh the DDB row.
                if prefix in existing_test_sets:
                    existing_row = existing_test_sets[prefix]
                    # Memo check BEFORE the budget gate so a warm container
                    # serving > MAX_RECONCILE_PROBES sets doesn't lock out the
                    # alphabetically-later ones. Memo hits are O(1) dict
                    # lookups with no S3 traffic — the budget exists to cap
                    # S3 work, not cheap in-process checks. Without this, on
                    # a stack of 30 sets the first 25 would eat the budget on
                    # every warm poll (all memo hits, no work) and sets 26-30
                    # would never be visited by reconcile until cold start.
                    if _within_reconcile_ttl(
                        prefix, existing_row.get("contentSignature")
                    ):
                        continue
                    # Budget exhausted? Skip the reconcile — the row keeps its
                    # last-known values on this response, and the next poll's
                    # warm-container memo takes the fast path for anything we
                    # already visited this pass.
                    if (
                        reconcile_probes >= MAX_RECONCILE_PROBES
                        or time.monotonic() > reconcile_deadline
                    ):
                        continue
                    reconcile_probes += 1
                    reconciled = _reconcile_test_set_tracking_entry(
                        s3_client,
                        test_set_bucket,
                        prefix,
                        existing_row,
                    )
                    if reconciled is not None:
                        # Refresh the in-memory row + the result entry we
                        # already appended for this test set.
                        existing_test_sets[prefix] = reconciled
                        for entry in result:
                            if entry["id"] == prefix:
                                entry["fileCount"] = reconciled.get("fileCount")
                                entry["status"] = reconciled.get("status")
                                entry["labelState"] = reconciled.get("labelState")
                                entry["source"] = reconciled.get("source")
                                entry["error"] = reconciled.get("error")
                                entry["updatedAt"] = reconciled.get("updatedAt")
                                break
                    continue

                # Check if this looks like a test set (has an input/ folder)
                if _is_valid_test_set_structure(s3_client, test_set_bucket, prefix):
                    logger.info(f"Found direct upload test set: {prefix}")

                    # Get creation timestamp from first file in the test set
                    created_at = _get_test_set_creation_time(
                        s3_client, test_set_bucket, prefix
                    )

                    # A set with no baseline is valid-but-unlabeled, so it registers
                    # and can be draft-labeled rather than being marked FAILED.
                    validation_result = _validate_test_set_files(
                        s3_client, test_set_bucket, prefix, allow_unlabeled=True
                    )

                    # A transient S3 failure in the validator returns a sentinel
                    # (input_count=0, valid=False, labeled=False) that would look
                    # exactly like a broken empty folder. Registering the row
                    # with that would write a FAILED entry with poisoned state
                    # and leave the UI showing a fake failure. Skip this
                    # prefix on this pass; the next getTestSets will retry
                    # once S3 recovers.
                    if validation_result.get("validation_failed"):
                        logger.warning(
                            f"Skipping first-discovery registration for {prefix}: "
                            f"validator failed transiently "
                            f"({validation_result.get('error')})."
                        )
                        continue

                    # Source: synthetic generator drops a '.source' marker; otherwise a user upload
                    source = _get_test_set_source(s3_client, test_set_bucket, prefix)

                    # Create tracking entry
                    status = "COMPLETED" if validation_result["valid"] else "FAILED"
                    error_message = validation_result.get("error")
                    label_state = (
                        "labeled" if validation_result.get("labeled") else "unlabeled"
                    )

                    # Store the full contentSignature (base + src marker) in
                    # the exact same format reconcile compares against, so a
                    # newly-discovered folder does not force one wasted full
                    # reconcile on the very next poll to normalise it.
                    # If the marker check failed transiently (source is None),
                    # skip the signature — reconcile will fill it in later,
                    # rather than writing a poisoned literal ``|src=None``.
                    content_signature = (
                        _compute_content_signature(
                            validation_result.get("signature", ""), source
                        )
                        if source is not None
                        else None
                    )

                    _create_test_set_tracking_entry(
                        prefix,
                        prefix,  # Use prefix as name
                        validation_result["input_count"],
                        status,
                        error_message,
                        created_at,
                        source,
                        label_state,
                        content_signature,
                    )

                    # Add to results
                    result.append(
                        {
                            "id": prefix,
                            "name": prefix,
                            "description": "",  # Direct uploads don't have descriptions
                            "filePattern": "",
                            "fileCount": validation_result["input_count"],
                            "source": source,
                            "labelState": label_state,
                            "status": status,
                            "createdAt": created_at,
                            "documentClassType": None,
                        }
                    )

                    logger.info(
                        f"Registered direct upload test set {prefix} with status {status}"
                    )

        # Check for deleted test sets (exist in DynamoDB but not in S3)
        # Only delete old FAILED test sets or any COMPLETED test sets
        from datetime import datetime, timedelta

        deleted_test_sets = []
        cutoff_time = datetime.utcnow() - timedelta(
            hours=1
        )  # Only delete FAILED if older than 1 hour

        for test_set_id in existing_test_sets:
            test_set_item = existing_test_sets[test_set_id]
            test_set_status = test_set_item.get("status")
            created_at_str = test_set_item.get("createdAt", "")

            # Parse creation time
            try:
                created_at = datetime.fromisoformat(
                    created_at_str.replace("Z", "+00:00")
                )
            except:
                continue  # Skip if can't parse date

            # Only delete if S3 folder missing AND:
            # - Status is COMPLETED (any time), OR
            # - Status is FAILED and older than cutoff time
            if test_set_id not in s3_test_sets and (
                test_set_status == "COMPLETED"
                or (test_set_status == "FAILED" and created_at < cutoff_time)
            ):
                deleted_test_sets.append(test_set_id)

        # Delete orphaned test sets from DynamoDB
        for test_set_id in deleted_test_sets:
            try:
                db_client.delete_item(
                    {"PK": f"testset#{test_set_id}", "SK": "metadata"}
                )
                logger.info(f"Deleted orphaned test set from DynamoDB: {test_set_id}")

                # Drop the warm-container memo entry — an id could be reused
                # later (test-set names are user-chosen), and a stale entry
                # from a deleted namesake would produce spurious signature
                # matches on the new set.
                _RECONCILE_MEMO.pop(test_set_id, None)

                # Remove from result list
                result = [item for item in result if item["id"] != test_set_id]

            except Exception as e:
                logger.error(
                    f"Failed to delete orphaned test set {test_set_id}: {str(e)}"
                )

    except Exception as e:
        logger.error(f"Error scanning for direct uploads: {str(e)}")

    logger.info(f"Returning {len(result)} test sets")
    return result


def get_test_set_documents(args):
    """List the documents in a test set with their baseline (ground truth) sections.

    Paginated over the set's `input/` prefix; the S3 continuation token is
    passed through opaquely as `nextToken`. For each page of input files, the
    whole `baseline/` prefix is listed once (bulk) and section result.json
    keys are matched to their document in memory — one extra LIST per page
    regardless of page size, and it handles nested input file names.
    """
    test_set_id = args["testSetId"]
    limit = args.get("limit") or 100
    next_token = args.get("nextToken")
    # Optional exact-match filter, so a deep-linked document detail page does not
    # page through the whole set.
    object_key = args.get("objectKey")

    # The id is derived from a validated name, so it must satisfy the same charset.
    # This also rejects '/' and '..', which could traverse outside the S3 prefix.
    if not validate_test_set_name(test_set_id):
        raise Exception("Invalid test set id")
    if object_key and ".." in object_key:
        raise Exception("Invalid object key")
    limit = max(1, min(int(limit), 1000))

    item = db_client.get_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})
    if not item:
        raise Exception(f"Test set '{test_set_id}' not found")

    test_set_bucket = os.environ["TEST_SET_BUCKET"]
    input_prefix = f"{test_set_id}/input/"

    list_kwargs = {
        "Bucket": test_set_bucket,
        # The objectKey equality check below drops same-prefix siblings.
        "Prefix": f"{input_prefix}{object_key}" if object_key else input_prefix,
        "MaxKeys": limit,
    }
    if next_token:
        list_kwargs["ContinuationToken"] = next_token
    response = s3_client.list_objects_v2(**list_kwargs)

    documents = []
    for obj in response.get("Contents", []):
        key = obj["Key"]
        if key.endswith("/"):
            continue  # skip folder placeholder objects
        relative_name = key[len(input_prefix) :]
        if object_key and relative_name != object_key:
            continue
        documents.append(
            {
                "objectKey": relative_name,
                "inputKey": key,
                "size": obj.get("Size"),
                "lastModified": obj["LastModified"].isoformat(),
                "sections": [],
            }
        )

    if documents:
        # Bulk-list baseline section files once and match to this page's docs.
        # Baseline layout: <id>/baseline/<relative_name>/sections/<n>/result.json
        baseline_prefix = f"{test_set_id}/baseline/"
        sections_by_doc = {d["objectKey"]: d["sections"] for d in documents}
        section_re = re.compile(r"^(.+)/sections/([^/]+)/result\.json$")
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=test_set_bucket, Prefix=baseline_prefix):
            for obj in page.get("Contents", []):
                rel = obj["Key"][len(baseline_prefix) :]
                match = section_re.match(rel)
                if not match:
                    continue
                doc_name, section_id = match.groups()
                sections = sections_by_doc.get(doc_name)
                if sections is not None:
                    sections.append(
                        {
                            "sectionId": section_id,
                            "baselineKey": obj["Key"],
                        }
                    )

        # Sort sections numerically where possible so "10" doesn't precede "2"
        for doc in documents:
            doc["sections"].sort(
                key=lambda s: (
                    (0, int(s["sectionId"]))
                    if s["sectionId"].isdigit()
                    else (1, s["sectionId"])
                )
            )

        _attach_label_metadata(test_set_bucket, documents)

    result = {
        "documents": documents,
        "nextToken": response.get("NextContinuationToken"),
        # The set's whole size, not this page's. A paginated response that reports
        # only what it returned leaves the caller counting the page and calling it
        # the total: the UI showed "Documents (50)" and offered to "Label 50
        # document(s)" for a 100-document set, then labeled all 100 — because
        # select-all sends no objectKeys and the server walks the set itself.
        # Read from the stored counter already fetched above, so this is O(1) and
        # stays O(1) as sets grow.
        "totalCount": _as_int(item.get("fileCount")) or 0,
    }

    # Surfaced so a page load resumes polling an in-flight job. Labels are harvested
    # on read, so a job that nothing polls stays RUNNING forever.
    meta = db_client.get_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})
    if meta and meta.get("labelJobStatus") == "RUNNING" and meta.get("labelJobId"):
        result["activeLabelJobId"] = meta["labelJobId"]

    logger.info(
        f"getTestSetDocuments({test_set_id}): {len(documents)} documents"
        f"{' (more available)' if result['nextToken'] else ''}"
    )
    return result


def _attach_label_metadata(test_set_bucket, documents):
    """Add labelSource, alert counts and minConfidence to each document on a page.

    Label state lives inside each section's baseline result.json, so those are read
    here — bounded to one page of documents and fetched concurrently, since the calls
    are pure I/O.

    Alert counts sum across a document's sections, since weak fields in two sections
    is more review work than in one; minConfidence takes the minimum, because it
    names the single weakest field.

    Best-effort per section: an unreadable result.json is skipped rather than failing
    the listing.
    """
    tasks = []
    for doc in documents:
        for section in doc["sections"]:
            tasks.append((doc, section))
    if not tasks:
        for doc in documents:
            doc["labelSource"] = None
            doc["minConfidence"] = None
            doc["confidenceThreshold"] = None
            doc["alertCount"] = None
            doc["fieldCount"] = None
        return

    def read(key):
        try:
            body = s3_client.get_object(Bucket=test_set_bucket, Key=key)["Body"].read()
            return json.loads(body)
        except Exception as e:  # noqa: BLE001 — best-effort enrichment
            logger.warning(f"Could not read baseline label {key}: {e}")
            return None

    per_doc = {
        id(doc): {
            "sources": [],
            "confidences": [],
            "alerts": [],
            "fields": [],
            # The class each section was given. Collected here because these
            # baselines are already being read for label state, so it costs
            # nothing extra — and a reviewer working the queue could not
            # previously see what class a document had been assigned without
            # opening it, which is the whole difficulty with a misclassified
            # document: extraction against the wrong schema can be confidently
            # wrong, so nothing else about the row looks unusual.
            "classes": [],
        }
        for doc, _ in tasks
    }
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(tasks), 16)
    ) as executor:
        results = executor.map(lambda t: (t[0], t[1], read(t[1]["baselineKey"])), tasks)
        for doc, section, result in results:
            if not result:
                continue
            # The section's OWN class, distinct from the document-level
            # `documentClasses` badge list: the regrouping board shows a class per
            # section, and the editor only ever loads the section being viewed.
            section_class = (result.get("document_class") or {}).get("type")
            if section_class:
                section["documentClass"] = str(section_class)
            # The section's page grouping, so the page-regrouping editor can show every
            # section at once instead of fetching each result.json again. Free: this
            # file is already open for label state and class, same as `classes` below.
            # Left absent rather than empty when unreadable — an empty grouping would
            # read as "this section has no pages", which is a different claim.
            indices = (result.get("split_document") or {}).get("page_indices")
            if isinstance(indices, list):
                section["pageIndices"] = [
                    int(i)
                    for i in indices
                    if isinstance(i, int) and not isinstance(i, bool)
                ]
            bucket_for_doc = per_doc[id(doc)]
            # A baseline with no labelSource is ground truth supplied when the set was
            # created: authoritative, but not reviewed here. Reporting it as
            # LABEL_SOURCE_UPLOADED rather than reviewed-human keeps overwrite safety
            # (via _existing_label_is_human) without letting the annotation queue
            # count it as completed review work.
            bucket_for_doc["sources"].append(
                result.get("labelSource") or LABEL_SOURCE_UPLOADED
            )
            inference = result.get("inference_result")
            # Recomputed rather than read back, because a stored minConfidence may
            # predate the exclusion of absent fields and carry a 0.0 from a blank box.
            # The stored value is only a fallback when there is no
            # explainability_info to recompute from.
            confidence = _min_confidence(result.get("explainability_info"), inference)
            if confidence is None:
                confidence = result.get("minConfidence")
            if confidence is not None:
                threshold = _confidence_threshold(
                    result.get("explainability_info"), inference
                )
                if threshold is None:
                    threshold = result.get("confidenceThreshold")
                bucket_for_doc["confidences"].append((float(confidence), threshold))
            alerts, fields = _alert_counts(result.get("explainability_info"), inference)
            if alerts is not None:
                bucket_for_doc["alerts"].append(alerts)
                bucket_for_doc["fields"].append(fields)
            # Reusing section_class from above — same `result`, same iteration.
            if section_class:
                bucket_for_doc["classes"].append(str(section_class))

    for doc in documents:
        collected = per_doc.get(
            id(doc),
            {
                "sources": [],
                "confidences": [],
                "alerts": [],
                "fields": [],
                "classes": [],
            },
        )
        # Distinct classes, order preserved. A single-section document yields one;
        # a packet yields the classes its sections were split into. Reported as a
        # list rather than joined, so the UI decides how to render more than one
        # rather than parsing a string back apart.
        seen_classes = []
        for cls in collected["classes"]:
            if cls not in seen_classes:
                seen_classes.append(cls)
        doc["documentClasses"] = seen_classes
        sources = collected["sources"]
        confidences = collected["confidences"]
        # A document counts as reviewed only when every section is.
        if not sources:
            doc["labelSource"] = None
        elif all(s == LABEL_SOURCE_HUMAN for s in sources):
            doc["labelSource"] = LABEL_SOURCE_HUMAN
        elif any(s == LABEL_SOURCE_DRAFT for s in sources):
            doc["labelSource"] = LABEL_SOURCE_DRAFT
        else:
            doc["labelSource"] = sources[0]
        if confidences:
            worst, threshold = min(confidences, key=lambda pair: pair[0])
            doc["minConfidence"] = worst
            doc["confidenceThreshold"] = threshold
        else:
            doc["minConfidence"] = None
            doc["confidenceThreshold"] = None
        if collected["alerts"]:
            doc["alertCount"] = sum(collected["alerts"])
            doc["fieldCount"] = sum(collected["fields"])
        else:
            doc["alertCount"] = None
            doc["fieldCount"] = None


def _is_valid_test_set_structure(s3_client, bucket, prefix):
    """Check if prefix contains an input/ folder (baseline/ optional).

    Also checks for a .uploading marker file which indicates the CLI is still
    uploading files. This prevents a race condition where the resolver auto-detects
    and validates a test set before all files (especially baselines) are uploaded.
    See: https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/193

    baseline/ is not required: a documents-only set is a legitimate unlabeled set
    awaiting generateDraftLabels, and requiring it would hide such sets from
    discovery. The .uploading marker, not the presence of baselines, is what guards
    against reading a half-done upload. A partially-labeled set is still reported as
    FAILED by _validate_test_set_files.
    """
    try:
        # Check for upload-in-progress marker
        try:
            s3_client.head_object(Bucket=bucket, Key=f"{prefix}/.uploading")
            logger.info(
                f"Skipping {prefix} - upload in progress (.uploading marker found)"
            )
            return False
        except Exception:
            pass  # No marker = not uploading, proceed with validation

        # Check for input/ folder
        input_response = s3_client.list_objects_v2(
            Bucket=bucket, Prefix=f"{prefix}/input/", MaxKeys=1
        )

        return input_response.get("KeyCount", 0) > 0

    except Exception as e:
        logger.error(f"Error checking test set structure for {prefix}: {str(e)}")
        return False


# Bound on how many labelState probes one listTestSets call performs. The list path
# is otherwise pure DynamoDB and is the most frequently hit query in Test Studio.
# Probed sets record the result, so the sets skipped here are genuinely picked up by
# the next call rather than the cap re-rolling over an arbitrary subset.
MAX_LABEL_STATE_PROBES = 25

# And a wall-clock bound, because the count cap does not limit latency: each probe
# paginates a set's input/ and baseline/ prefixes, so 25 probes over 500-document sets is
# far more time than belongs on the query TestSets.tsx polls every 3 seconds. Same idiom
# as HARVEST_TIME_BUDGET_SECONDS. Sets not reached are picked up by the next call, which
# is true rather than aspirational because each probe records its result.
#
# Checked between probes, not inside one, so this bounds how many probes are *started*
# rather than capping total duration: a single enormous set can overrun it by however
# long its own pagination takes. The function timeout is the real backstop there. Worth
# knowing before treating 5 seconds as a latency guarantee.
LABEL_STATE_PROBE_BUDGET_SECONDS = 5

# Statuses in which a set's S3 contents are still being written, so coverage read from
# S3 is not evidence about the set's real label state. Used to skip reconciliation
# rather than act on a half-copied prefix.
IN_FLUX_TEST_SET_STATUSES = frozenset({"UPDATING", "COPYING", "GENERATING", "QUEUED"})


# How long a test set may sit in a non-terminal status before the host gives up on it.
# Per status, because the plausible durations differ by orders of magnitude: a synthetic
# generation legitimately runs for hours (its own runtime ceiling is ~8h), whereas a file
# copy does not. Generous on purpose — declaring a live run dead is worse than a spinner
# that lasts a little longer than it should.
STALE_STATUS_HOURS = {"GENERATING": 12, "UPDATING": 2, "QUEUED": 2}


def _reap_abandoned_test_sets(items):
    """Fail test sets whose non-terminal status has no owner left to clear it.

    GENERATING is written by the synthetic-generator extension and cleared by its
    runtime. If that runtime dies mid-run — or the extension is uninstalled — nothing
    remaining can clear it, and Test Studio renders the status as an in-progress
    spinner, so the set shows "Generating…" indefinitely, across reloads and redeploys,
    because the state is a database record rather than client state. Observed on a dev
    stack: a set stuck for over an hour, then permanently once the extension was
    removed. The same applies to UPDATING if the file copier dies.

    Deliberately lives in the host resolver rather than the extension: the whole point
    is to survive the extension being absent.

    A set with no ``statusUpdatedAt`` is left alone unless its status is QUEUED, where
    ``createdAt`` marks the same moment. Records written before that field existed
    cannot be aged, and presuming them dead would fail live work.
    """
    now = datetime.now(timezone.utc)
    for item in items:
        status = item.get("status")
        max_hours = STALE_STATUS_HOURS.get(status)
        if max_hours is None:
            continue
        stamp = item.get("statusUpdatedAt") or (
            item.get("createdAt") if status == "QUEUED" else None
        )
        if not stamp:
            continue
        try:
            since = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        idle_hours = (now - since).total_seconds() / 3600
        if idle_hours < max_hours:
            continue

        test_set_id = item.get("id") or item.get("PK", "").replace("testset#", "")
        error = (
            f"{status} for {int(idle_hours)}h with no progress; the job that owns this "
            "status is gone. The documents already written are unaffected."
        )
        try:
            # boto3 directly rather than db_client: this needs a ConditionExpression,
            # so that a job which reported in between the read and this write wins.
            boto3.resource("dynamodb").Table(os.environ["TRACKING_TABLE"]).update_item(
                Key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
                UpdateExpression="SET #st = :failed, #er = :e",
                ConditionExpression="#st = :seen",
                ExpressionAttributeNames={"#st": "status", "#er": "error"},
                ExpressionAttributeValues={
                    ":failed": "FAILED",
                    ":seen": status,
                    ":e": error,
                },
            )
            # Only after the write lands. Mutating first would report FAILED to the
            # caller in exactly the case the condition exists to catch — a job that
            # reported COMPLETED between the read and the write.
            item["status"] = "FAILED"
            item["error"] = error
            logger.warning(f"Reaped abandoned test set {test_set_id}: {error}")
        except Exception as e:  # noqa: BLE001 — reaping must not fail the list
            logger.info(f"Did not reap test set {test_set_id}: {e}")


def _reconcile_label_state(items):
    """Repair ``labelState`` on sets that gained ground truth after registration.

    labelState is derived once, when a set is first registered, and afterwards only
    moved by draft-label harvest and reset. Nothing re-derives it when *already
    labelled* documents are added to an existing set — which is exactly what the
    synthetic generator does, writing documents and their baselines straight to S3.
    A set can therefore hold 47 documents of ground truth and still report
    "Unlabeled" with no estimated accuracy, which understates it where the number
    matters most.

    It corrects in both directions. A set can also become *less* labelled than its
    record claims — add documents without baselines to a labelled set and "labeled" is
    simply wrong — and a state that only ever ratchets upward would keep asserting
    ground truth that is no longer complete. (A set promoted in error by an earlier,
    laxer version of this function self-heals for the same reason.)

    Overstating is worse than understating, so the decision is deliberately strict:

    * **Sets owned by a labeling job are never touched.** The harvest writes
      ``draft-machine`` labels under the same ``baseline/`` prefix and only sets
      labelState once it reaches COMPLETED, so a running — or failed — harvest is a
      set with baseline objects that are *not* ground truth. Flipping those to
      "labeled" would put a green badge on unreviewed machine output, start the
      effort estimator on it, and suppress the "Labeling failed" warning.
    * **Coverage must be complete.** Decided by ``_validate_test_set_files``, the
      same helper registration uses, so repair and registration cannot disagree
      about what "labeled" means. One labelled document out of 47 is not a labelled
      set.
    * **Drafts are recorded as drafts.** If the baselines are machine drafts the
      state becomes "draft", never "labeled".

    Converging, too: the fileCount a probe ran against is recorded, so a set is probed
    once per membership change rather than on every read — which is also what makes
    re-checking already-labelled sets affordable, since membership rarely changes.
    Known limitation: baselines added for documents that were *already* in the set (a
    hand-upload straight to S3, rather than a generation that adds documents) leave
    fileCount unchanged and are not re-probed until it changes.
    """
    # An optional repair must never be the reason a list fails. A deployment without
    # the bucket configured simply does not get reconciliation.
    bucket = os.environ.get("TEST_SET_BUCKET")
    if not bucket:
        return
    probes = 0
    deadline = time.monotonic() + LABEL_STATE_PROBE_BUDGET_SECONDS
    for item in items:
        if probes >= MAX_LABEL_STATE_PROBES or time.monotonic() > deadline:
            logger.info(
                "labelState reconciliation stopped after %d probe(s); the rest are "
                "picked up by the next list",
                probes,
            )
            break
        # A labeling job owns this set's labelState; see the docstring.
        if item.get("labelJobId") or item.get("labelJobStatus"):
            continue
        # A set whose contents are still being written is not evidence of anything. The
        # copier lands input/ keys before the matching baseline/ folders, so probing
        # mid-copy sees real-but-temporary incomplete coverage and demotes a labelled
        # set. It self-heals — fileCount changes at COMPLETED, which invalidates the
        # marker and forces a re-probe — but the user watching the list sees the badge
        # flip and flip back, and the probes spent to get there are wasted.
        if item.get("status") in IN_FLUX_TEST_SET_STATUSES:
            continue
        test_set_id = item.get("id") or item.get("PK", "").replace("testset#", "")
        if not test_set_id:
            continue
        # -1 stands for "no file count recorded", so a set without one still converges
        # instead of being re-probed on every list and consuming the budget forever. A
        # real count appearing later differs from -1, which re-probes as it should.
        file_count = _as_int(item.get("fileCount"))
        probe_key = file_count if file_count is not None else -1
        # Keyed on membership rather than on the current state, so the same probe
        # promotes an under-stated set and demotes an over-stated one. A set whose
        # membership has not changed since it was last validated costs nothing.
        if _as_int(item.get("labelProbedFileCount")) == probe_key:
            continue

        probes += 1
        try:
            validation = _validate_test_set_files(
                s3_client, bucket, test_set_id, allow_unlabeled=True
            )
        except Exception as e:  # noqa: BLE001 — probing must not fail the list call
            logger.warning(f"Could not probe labels for {test_set_id}: {e}")
            continue

        # A transient S3 failure inside the validator surfaces as a sentinel
        # dict (validation_failed=True) whose ``labeled=False`` value would
        # otherwise persistently demote a labelled row to 'unlabeled' AND
        # cache that demotion via `_remember_label_probe`, making it sticky
        # until the fileCount changes. Skip this row on this pass; the next
        # getTestSets after S3 recovers will re-probe.
        if validation.get("validation_failed"):
            logger.warning(
                f"Skipping labelState reconciliation for {test_set_id}: "
                f"validator failed transiently ({validation.get('error')})."
            )
            continue

        if validation.get("labeled"):
            state = (
                "draft" if _probed_labels_are_drafts(bucket, test_set_id) else "labeled"
            )
            reason = "every document carries a baseline"
        else:
            # Coverage is incomplete, so "labeled" is an overstatement whatever the
            # record says. Reached by adding documents without baselines to a labelled
            # set, and by a set promoted in error before this check was strict.
            state = "unlabeled"
            reason = "not every document carries a baseline"

        current = item.get("labelState")
        _remember_label_probe(test_set_id, probe_key)
        if current == state:
            continue

        item["labelState"] = state
        try:
            db_client.update_item(
                key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
                update_expression="SET labelState = :ls",
                expression_attribute_values={":ls": state},
            )
            logger.info(
                f"Corrected labelState for {test_set_id}: '{current}' -> '{state}' "
                f"({reason})"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not persist labelState for {test_set_id}: {e}")


def _probed_labels_are_drafts(bucket, test_set_id):
    """True when this set's baselines are machine drafts rather than ground truth.

    Reads one section result. The harvest writes drafts for a whole run at once, so a
    single key is representative; being wrong here can only mis-label a mixed set as
    "draft", which understates rather than overstates.
    """
    try:
        page = s3_client.list_objects_v2(
            Bucket=bucket, Prefix=f"{test_set_id}/baseline/", MaxKeys=10
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not list baselines for {test_set_id}: {e}")
        return False
    for obj in page.get("Contents") or []:
        if obj["Key"].endswith("/result.json"):
            return _existing_label_is_draft(bucket, obj["Key"])
    return False


def _remember_label_probe(test_set_id, file_count):
    """Record that a set was probed and had no complete ground truth.

    Without this the probe never converges: a genuinely unlabelled set writes no
    marker, so every listTestSets re-probes it forever. Callers pass -1 for a set with
    no recorded file count, so that case converges too.
    """
    if file_count is None:
        return
    try:
        db_client.update_item(
            key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
            update_expression="SET labelProbedFileCount = :n",
            expression_attribute_values={":n": file_count},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not record label probe for {test_set_id}: {e}")


def _validate_test_set_files(s3_client, bucket, prefix, allow_unlabeled=False):
    """Validate that input and baseline files match.

    Each input file must have a corresponding baseline folder with the exact same name
    (including extension). For example, input file 'doc.png' requires baseline folder 'doc.png/'.
    Any file extension is supported, and mixed extensions within a test set are allowed.

    ``allow_unlabeled=True`` permits a set with no baseline files at all, which is
    valid but unlabeled and awaits generateDraftLabels. A partially labeled set is an
    error either way, since it indicates a botched upload rather than a deliberate
    label-later flow.

    Also returns a ``signature`` string summarizing the current on-disk state
    (input/baseline counts + max LastModified). Callers use it as a cheap
    change-detector: identical signature => nothing to reconcile.
    """
    try:
        input_files = set()
        baseline_files = set()
        latest_modified = None

        def _observe(obj):
            # Track the max LastModified across every input+baseline object so a
            # single deleted file still moves the signature (count changes) and a
            # single overwritten baseline moves it too (LastModified changes).
            nonlocal latest_modified
            lm = obj.get("LastModified")
            if lm is not None and (latest_modified is None or lm > latest_modified):
                latest_modified = lm

        # Get input files
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/input/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith("/"):  # Skip directories
                    filename = key.split("/")[-1]
                    input_files.add(filename)
                    _observe(obj)

        # Get baseline folder names (first folder after /baseline/)
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/baseline/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith("/"):  # Skip directories
                    _observe(obj)
                    # Extract folder name after /baseline/
                    parts = key.split(f"{prefix}/baseline/", 1)
                    if len(parts) == 2 and "/" in parts[1]:
                        # First path component is the baseline folder name
                        folder_name = parts[1].split("/")[0]
                        if folder_name:
                            baseline_files.add(folder_name)

        signature = "{}:{}:{}".format(
            len(input_files),
            len(baseline_files),
            latest_modified.isoformat() if latest_modified is not None else "",
        )

        # Validate matching
        if len(input_files) == 0:
            return {
                "valid": False,
                "error": "No input files found",
                "input_count": 0,
                "signature": signature,
            }

        if len(baseline_files) == 0:
            if allow_unlabeled:
                return {
                    "valid": True,
                    "input_count": len(input_files),
                    "labeled": False,
                    "signature": signature,
                }
            return {
                "valid": False,
                "error": "No baseline files found",
                "input_count": len(input_files),
                "signature": signature,
            }

        missing_baselines = input_files - baseline_files
        if missing_baselines:
            return {
                "valid": False,
                "error": f"Missing baseline files for: {', '.join(list(missing_baselines)[:3])}{'...' if len(missing_baselines) > 3 else ''}",
                "input_count": len(input_files),
                "signature": signature,
            }

        extra_baselines = baseline_files - input_files
        if extra_baselines:
            return {
                "valid": False,
                "error": f"Extra baseline files: {', '.join(list(extra_baselines)[:3])}{'...' if len(extra_baselines) > 3 else ''}",
                "input_count": len(input_files),
                "signature": signature,
            }

        return {
            "valid": True,
            "input_count": len(input_files),
            "labeled": True,
            "signature": signature,
        }

    except Exception as e:
        logger.error(f"Error validating test set files for {prefix}: {str(e)}")
        # ``validation_failed=True`` is the sentinel that says "the return
        # values below are not observations of the folder — they're placeholders
        # from a transient failure." Reconcile MUST check this before writing:
        # otherwise a throttled S3 call would return ``input_count=0``,
        # ``valid=False`` and no ``labeled`` key, and reconcile would happily
        # write that over a valid COMPLETED/labeled/N-file row, destroying its
        # state until the next successful call.
        return {
            "valid": False,
            "validation_failed": True,
            "error": f"Validation error: {str(e)}",
            "input_count": 0,
            "labeled": False,
            "signature": "",
        }


def _get_test_set_creation_time(s3_client, bucket, prefix):
    """Get the earliest creation time from files in the test set"""
    earliest_time = None

    # Check input folder for earliest file
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=bucket, Prefix=f"{prefix}/input/", MaxKeys=10
    ):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith("/"):  # Skip directories
                if earliest_time is None or obj["LastModified"] < earliest_time:
                    earliest_time = obj["LastModified"]

    if earliest_time is None:
        raise Exception(f"No files found in {prefix}/input/ to determine creation time")

    return earliest_time.isoformat()


def _get_test_set_source(s3_client, bucket, prefix):
    """One HeadObject on ``<prefix>/.source`` producing the row's ``source`` value.

    Returns ``'synthetic'`` if the marker is present, ``'uploaded'`` if it is
    definitively absent (S3 404), or ``None`` if the answer is transiently
    unknown (throttling / 5xx / non-ClientError). Callers must treat ``None``
    as "don't overwrite the row's current source" — flipping synthetic → uploaded
    on a bad AWS day would misclassify silently.

    This is the *only* helper that reads the marker. Reconcile also uses its
    return value to build the ``contentSignature`` so a late marker write
    invalidates the fast path; feeding the signature from a second HeadObject
    would race this one and let ``signature=src=uploaded`` coexist with
    ``source='synthetic'`` in the same reconcile pass.
    """
    try:
        s3_client.head_object(Bucket=bucket, Key=f"{prefix}/.source")
        return "synthetic"
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        # S3 answers 404 as 404, but HeadObject with s3v4 sometimes surfaces
        # missing objects as NoSuchKey; treat both as definitively absent.
        if code in ("404", "NoSuchKey", "NotFound"):
            return "uploaded"
        if code in ("AccessDenied", "403", "Forbidden"):
            # Persistent IAM misconfiguration — not transient. Reconcile will
            # bail for this prefix (None return) until the IAM policy is
            # fixed, but log at ERROR so operators actually see the problem
            # instead of watching reconcile silently stop working. Returning
            # a fabricated 'uploaded' would silently rebrand every stack-
            # managed dataset on this stack; None is safer.
            logger.error(
                f"HeadObject for {prefix}/.source denied ({code}) — IAM policy "
                "on the resolver's role is missing s3:GetObject on the test-set "
                "bucket. Reconcile will bail for this prefix until fixed."
            )
            return None
        logger.warning(
            f"HeadObject for {prefix}/.source failed transiently ({code}); "
            "returning None (source unchanged)."
        )
        return None
    except Exception as e:
        # Anything that isn't a ClientError is definitely not "the object is
        # missing" — SDK/config bug, network unreachable, etc. Same treatment.
        logger.warning(
            f"Unexpected error checking {prefix}/.source: {e}; "
            "returning None (source unchanged)."
        )
        return None


def _compute_content_signature(base_signature, source):
    """Combine the validator's base signature with the source marker's state.

    Kept as one helper so the new-folder-discovery ``_create_test_set_tracking_entry``
    and the reconcile path write signatures in the *exact same format*.
    Divergence here forces every newly-discovered folder into one wasted full
    reconcile on the next poll to normalise the signature.

    ``source`` must be one of ``'synthetic' | 'uploaded'``. Callers must not
    pass ``None`` — they should bail before reaching this helper, because a
    signature containing the literal string ``|src=None`` would poison the
    stored signature and trigger a full-scan retry every subsequent poll.
    """
    return f"{base_signature}|src={source}"


def _create_test_set_tracking_entry(
    test_set_id,
    name,
    file_count,
    status,
    error=None,
    created_at=None,
    source=None,
    label_state=None,
    content_signature=None,
):
    """Create tracking table entry for direct upload test set"""
    try:
        now = datetime.utcnow().isoformat() + "Z"
        item = {
            "PK": f"testset#{test_set_id}",
            "SK": "metadata",
            "ItemType": "testset",
            "InitialEventTime": now,
            "id": test_set_id,
            "name": name,
            "description": "",  # Direct uploads don't have descriptions
            "filePattern": "",
            "fileCount": file_count,
            "status": status,
            "createdAt": now,
        }

        if source:
            item["source"] = source
        if label_state:
            item["labelState"] = label_state
        if error:
            item["error"] = error
        if content_signature:
            item["contentSignature"] = content_signature

        db_client.put_item(item)
        logger.info(f"Created tracking entry for direct upload test set {test_set_id}")

    except Exception as e:
        logger.error(f"Error creating tracking entry for {test_set_id}: {str(e)}")


# Rows in one of these non-terminal states are being actively mutated by a
# copier/extractor/generator — reconcile MUST NOT touch them. Row-level
# protection here; ``ConditionExpression`` on the UpdateItem closes the race
# window between the read and the write. Gate 1 uses ``IN_FLUX_TEST_SET_STATUSES``
# (defined above), keyed as an explicit skip-list rather than an allow-list so
# legacy rows without a persisted ``status`` field remain reconcilable rather
# than permanently invisible.

# Minimum wait between full reconcile passes for the same prefix. Without this,
# the UI's 3 s fast poll (armed whenever any row on the page is non-terminal,
# ``TestSets.tsx``) repeats two paginated ``list_objects_v2`` calls plus a
# HeadObject per registered test set on every tick — for a 2000+ doc set the
# ``baseline/`` prefix alone is three or more LIST pages.
_RECONCILE_TTL_SECONDS = 30

# Per-request cap on reconcile probes, mirroring the label-state probe
# budget (MAX_LABEL_STATE_PROBES / LABEL_STATE_PROBE_BUDGET_SECONDS above).
# Reconcile does the same 2 x list_objects_v2 + 1 x HeadObject per prefix
# on a cold container; the resolver's Timeout is 60 s and MemorySize 256 MB.
# Beyond this budget, the remainder of the prefixes are picked up by the
# next getTestSets call (which finds them in the warm-container memo when
# it comes back for them).
MAX_RECONCILE_PROBES = 25
RECONCILE_PROBE_BUDGET_SECONDS = 5

# Warm-container memo of the last time each prefix went through a full reconcile
# pass. Keyed by (region-agnostic) prefix → (last_signature, monotonic_time).
# Deliberately in-process rather than on the DDB row: keying on ``updatedAt``
# would require an unconditional DDB write to bump the timestamp even on the
# no-op path, defeating the whole point of the fast path. The memo resets on
# cold start — a fresh Lambda container simply pays for one full scan per set
# on its first invocation, which is bounded and fine.
_RECONCILE_MEMO: dict[str, tuple[str, float]] = {}


def _within_reconcile_ttl(prefix, existing_signature):
    """True when the warm-container memo says this prefix was scanned recently
    AND the DDB row's stored signature still matches what we cached — i.e.
    nobody else wrote to the row since our last pass, so we can skip the
    listing.
    """
    entry = _RECONCILE_MEMO.get(prefix)
    if entry is None:
        return False
    last_sig, last_ts = entry
    if (time.monotonic() - last_ts) >= _RECONCILE_TTL_SECONDS:
        return False
    # Another writer (copier/extractor/an out-of-process reconcile) may have
    # bumped the row; if the DDB signature drifted from what we cached, our
    # stability assumption no longer holds and we must re-check.
    return last_sig == existing_signature


# The row's ``source`` field carries dataset provenance that is not owned by
# the ``.source`` marker: HuggingFace-backed benchmark deployers (``fake-w2``,
# ``ocr-benchmark``, ``fcc``, ``docsplit``) and the ConfBench extension write
# strings like ``huggingface:amazon-agi/fake-w2``. Reconcile must only accept
# the marker's answer when the current value is one of these three
# "marker-owned" states — otherwise a first pass on a stack-managed benchmark
# would silently overwrite ``source`` with ``uploaded``, blank out the Source
# column in the UI and lose the provenance until the deployer re-runs on the
# next stack update.
_MARKER_OWNED_SOURCES = (None, "uploaded", "synthetic")


# Lazy singleton so we don't pay ``boto3.resource("dynamodb")`` construction
# cost per reconciled row inside a hot poll.
_tracking_table = None


def _get_tracking_table():
    global _tracking_table
    if _tracking_table is None:
        _tracking_table = boto3.resource("dynamodb").Table(os.environ["TRACKING_TABLE"])
    return _tracking_table


def _reconcile_test_set_tracking_entry(s3_client, bucket, prefix, existing_row):
    """Reconcile an already-registered test set against its current S3 contents.

    Direct S3 edits (files added or removed under ``<prefix>/input/`` or
    ``<prefix>/baseline/``) don't fire any event; without this helper the
    ``getTestSets`` short-circuit at ``if prefix in existing_test_sets: continue``
    leaves ``fileCount`` and ``status`` stale forever.

    **Not** literally the same treatment as new-folder discovery. New folders
    hard-fail on partial ``input`` ↔ ``baseline`` pairing because that
    indicates a broken upload. For a row that has already entered the
    labeling lifecycle, partial pairing is a legitimate state — the
    draft-labeling harvester writes baselines one document per poll (leaving
    a partial state mid-run), and ``clear_draft_labels`` deliberately keeps
    human-reviewed labels while removing machine drafts (leaving partial
    pairing as *steady* state). Applying the new-folder rule to those cases
    would flag every draft-labeling run as FAILED and demote the surviving
    reviewed labels to ``labelState=unlabeled``. Reconcile therefore
    refreshes ``fileCount`` and the signature on those rows but leaves
    ``status`` / ``labelState`` / ``error`` alone.

    Returns a dict of the reconciled fields merged with the existing row, so
    callers can propagate into the GraphQL response without a second DDB
    read. Returns None when the row is untouched.
    """
    try:
        # Gate 1 — row-status. Copiers/extractors write fileCount+status
        # eagerly under UPDATING/QUEUED; reconciling on top would race them.
        # Skip-list (not allow-list) so legacy rows without a ``status`` are
        # reconcilable rather than permanently invisible.
        if existing_row.get("status") in IN_FLUX_TEST_SET_STATUSES:
            return None

        # Gate 2 — labeling-job-status. The draft-labeling harvester leaves
        # the row's ``status`` at COMPLETED for the *entire* run while it
        # writes baselines document-by-document. Partial pairing is the
        # transient invariant of that flow — reconcile flagging it as FAILED
        # would break every draft-labeling job that reached the resolver
        # mid-run (i.e. all of them, since ``getTestSets`` polls every 3s).
        if existing_row.get("labelJobStatus") == "RUNNING":
            return None

        # Gate 3 — warm-container TTL. The signature match short-circuits the
        # DDB write but not the two paginated ``list_objects_v2`` calls that
        # compute the signature. Under the UI's 3 s fast poll on a stack with
        # N test sets that is 2N list_objects_v2 requests every 3 s per Lambda
        # container. The memo says "this prefix was fully scanned within the
        # last _RECONCILE_TTL_SECONDS and DDB still shows the signature we
        # cached" — cold starts pay the scan cost once, then coast.
        if _within_reconcile_ttl(prefix, existing_row.get("contentSignature")):
            return None

        validation = _validate_test_set_files(
            s3_client, bucket, prefix, allow_unlabeled=True
        )

        # A transient S3 failure in the validator returns a sentinel dict
        # (``validation_failed=True``) whose fields would otherwise look like
        # "the folder is now empty and broken" — writing that over a real row
        # destroys its state. Bail without a DDB write; the next successful
        # reconcile will handle it.
        if validation.get("validation_failed"):
            logger.warning(
                f"Skipping reconcile for {prefix}: validation failed transiently "
                f"({validation.get('error')})"
            )
            return None

        # One HeadObject on ``.source`` produces the row's source value AND
        # the signature's src component — using two separate HeadObjects that
        # could disagree on a marker landing between them would let
        # ``|src=uploaded`` coexist with ``source='synthetic'`` and force a
        # spurious full reconcile on the next poll.
        detected_source = _get_test_set_source(s3_client, bucket, prefix)

        # If the marker check failed transiently we cannot build a coherent
        # signature (writing ``|src=None`` would poison the stored value and
        # trigger a full-scan retry every subsequent poll until the next
        # write happens to succeed). Bail — the next reconcile after the TTL
        # expires will retry.
        if detected_source is None:
            logger.warning(
                f"Skipping reconcile for {prefix}: source marker check failed "
                "transiently — cannot build a coherent signature."
            )
            return None

        base_signature = validation.get("signature", "")
        new_signature = _compute_content_signature(base_signature, detected_source)
        old_signature = existing_row.get("contentSignature")

        # Fast path: signature unchanged AND we already have one on the row =>
        # no S3 delta since last reconcile. Rows written before this change lack
        # contentSignature entirely — reconcile them once so they get a
        # baseline. Also update the memo so subsequent polls within the TTL
        # window can skip the S3 listing entirely.
        if old_signature and old_signature == new_signature:
            _RECONCILE_MEMO[prefix] = (new_signature, time.monotonic())
            return None

        new_file_count = validation.get("input_count", 0)
        existing_label_state = existing_row.get("labelState")
        existing_source = existing_row.get("source")

        # Reconcile only ever sees rows that were previously registered, and
        # partial input/baseline pairing is NEVER the "botched upload" signal
        # on an already-registered row. That signal belongs to first-discovery
        # (_create_test_set_tracking_entry) — by the time we get here, the
        # folder has passed that check once. Partial pairing here is one of:
        #  - draft-labeling harvest mid-run (per-poll baselines)
        #  - clear_draft_labels leaving human labels + drop drafts (steady)
        #  - direct-S3 add of an input on a labelled set (before its baseline)
        #  - stack-managed dataset with per-doc processing failures
        # None of these are FAILED conditions.
        #
        # We deliberately do NOT key this decision on labelState, because
        # _reconcile_label_state (index.py:2595, ships from develop) runs
        # earlier in the same get_test_sets call and CAN demote labelState
        # to 'unlabeled' in-place on the item we receive. A softening keyed
        # on labelState=='labeled' would then be defeated by that demoter
        # — the exact interaction that produced the round-4 blocker.
        # Keying on the validator's error class survives the demoter
        # because it looks at S3, not at the row.
        error_message = validation.get("error") or ""
        partial_pairing = not validation["valid"] and (
            error_message.startswith("Missing baseline files for:")
            or error_message.startswith("Extra baseline files:")
        )
        # The one remaining hard-fail: every input file has been deleted from
        # S3 but the row survives. Truly broken; user should see it.
        no_inputs = not validation["valid"] and error_message == "No input files found"

        if partial_pairing:
            # Refresh fileCount and signature; leave status / labelState alone.
            # Recovery direction (FAILED → COMPLETED when the pairing is fully
            # restored) is handled by the valid-branch below.
            new_status = existing_row.get("status")
            new_label_state = existing_label_state
            # Error message must reflect the CURRENT partial-pairing state, not
            # the one recorded at first-discovery time. Example: a row that
            # started FAILED with "Missing baseline files for: a.pdf, b.pdf,
            # c.pdf", then the user added baselines for a.pdf and b.pdf. The
            # validator now reports "Missing baseline files for: c.pdf". If we
            # kept the old error, the UI would show all three names still.
            # Only meaningful when the row is FAILED; a COMPLETED row shouldn't
            # carry an error field.
            if new_status == "FAILED":
                new_error = validation.get("error")
            else:
                new_error = existing_row.get("error")
        elif no_inputs:
            new_status = "FAILED"
            new_error = error_message
            # Preserve the current labelState. Overwriting to 'unlabeled' would
            # silently destroy a 'draft' signal: a user who accidentally deletes
            # inputs and then restores them would see the recovery valid-branch
            # promote 'unlabeled' → 'labeled' on the next reconcile, blessing
            # unreviewed machine drafts as ground truth. The draft-preservation
            # guard in the valid-branch keys on existing_label_state — if that
            # was 'draft' before the input deletion, it must stay 'draft'
            # through the transient FAILED state so recovery preserves it.
            new_label_state = existing_label_state
        elif validation["valid"]:
            # Fully paired OR unlabeled-with-no-baselines (allow_unlabeled=True
            # path). Both are healthy states.
            new_status = "COMPLETED"
            new_error = None
            # labelState transitions: 'unlabeled', 'draft' (machine labels
            # awaiting review), 'labeled' (uploaded or reviewed). The
            # validator only sees folder shape; a fully-paired folder could
            # be either 'draft' or 'labeled'. Preserve 'draft' when it was
            # already there — writing 'labeled' would silently promote
            # unreviewed drafts to "verified" ground truth, which is exactly
            # what the draft-labeling workflow exists to prevent.
            if validation.get("labeled"):
                if existing_label_state == "draft":
                    new_label_state = "draft"
                else:
                    new_label_state = "labeled"
            else:
                new_label_state = "unlabeled"
        else:
            # Fallback: validator reported valid=False but the error class
            # was neither Missing/Extra baseline nor "No input files found".
            # Present-day validator only emits those three, so this branch is
            # unreachable today — it exists so a future validator error class
            # doesn't silently mark a broken set as COMPLETED (as the previous
            # unconditional else did). Hard-fail with the current error string
            # and preserve labelState so a subsequent classification recovery
            # keeps whatever lifecycle state the row was in.
            new_status = "FAILED"
            new_error = error_message or "Validation failed (unknown error class)"
            new_label_state = existing_label_state

        # Determine which fields actually changed so we don't issue a DDB write
        # (or a DDB stream event) for a no-op.
        changed = {}
        if existing_row.get("fileCount") != new_file_count:
            changed["fileCount"] = new_file_count
        if existing_row.get("status") != new_status:
            changed["status"] = new_status
        if existing_row.get("labelState") != new_label_state:
            changed["labelState"] = new_label_state
        # Only adopt the marker's answer for marker-owned sources. HuggingFace
        # or ConfBench provenance is authoritative — the ``.source`` marker's
        # absence is not evidence that a stack-managed dataset should be
        # rebranded 'uploaded'. The signature still folds in detected_source
        # unchanged; that converges after one write regardless.
        if (
            existing_source in _MARKER_OWNED_SOURCES
            and existing_source != detected_source
        ):
            changed["source"] = detected_source
        # Error handling is asymmetric: SET when we have one, REMOVE when we
        # don't. Use ``in`` rather than truthiness so an empty-string error
        # attribute would also trigger REMOVE.
        clear_error = "error" in existing_row and not new_error
        if new_error and existing_row.get("error") != new_error:
            changed["error"] = new_error
        if existing_row.get("contentSignature") != new_signature:
            changed["contentSignature"] = new_signature

        if not changed and not clear_error:
            # Nothing to persist, but we did do the full scan — memoize so the
            # next poll inside the TTL takes the fast path.
            _RECONCILE_MEMO[prefix] = (new_signature, time.monotonic())
            return None

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        changed["updatedAt"] = now

        # Build one UpdateItem with SET for changed fields and (optionally)
        # REMOVE for the error attribute when the set is clean again. The
        # ConditionExpression has two clauses:
        #  1. Status must still be in {COMPLETED, FAILED} — this closes the
        #     race with TestSetFileCopier / TestSetZipExtractor / the synthetic
        #     generator writing a non-terminal status in the window between our
        #     read and this write.
        #  2. contentSignature must still match what we read (or still be
        #     missing if it was missing) — this closes the race between two
        #     concurrent reconciles: A and B both read sig X, B computes a
        #     fresh sig Y and writes, A finishes its listing later with a
        #     stale view and would otherwise clobber Y with a stale sig. With
        #     the signature match, A's write is rejected and A returns None;
        #     A's memo divergence check will trip on the next poll and re-scan.
        expr_names = {"#st": "status", "#sig": "contentSignature"}
        old_sig = existing_row.get("contentSignature")
        expr_values = {
            ":completed": "COMPLETED",
            ":failed": "FAILED",
            # Empty string is a sentinel that no real signature can take
            # (the format is ``count:count:iso|src=...``, always non-empty).
            # When old_sig is missing the OR branch attribute_not_exists wins;
            # when old_sig has a value, that branch matches; when someone else
            # moved the row, both fail and the condition rejects.
            ":old_sig": old_sig if old_sig else "",
        }
        set_parts = []
        for i, (field, value) in enumerate(changed.items()):
            name_key = f"#f{i}"
            value_key = f":v{i}"
            expr_names[name_key] = field
            expr_values[value_key] = value
            set_parts.append(f"{name_key} = {value_key}")

        update_expression = "SET " + ", ".join(set_parts)
        if clear_error:
            # Only include #err in ExpressionAttributeNames when it appears in
            # the expression — DynamoDB errors on unused attribute names.
            expr_names["#err"] = "error"
            update_expression += " REMOVE #err"

        # Write is allowed when the row is not being actively mutated
        # (terminal status OR no status attribute at all — legacy rows) AND
        # the signature we read is still current (or missing).
        condition_expression = (
            "(attribute_not_exists(#st) OR #st IN (:completed, :failed)) "
            "AND (attribute_not_exists(#sig) OR #sig = :old_sig)"
        )

        # Use boto3 directly here (rather than db_client.update_item) because
        # DynamoDBClient.update_item does not expose ConditionExpression, and
        # the race guards are the whole reason for this write's condition.
        try:
            _get_tracking_table().update_item(
                Key={"PK": f"testset#{prefix}", "SK": "metadata"},
                UpdateExpression=update_expression,
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values,
                ConditionExpression=condition_expression,
            )
        except ClientError as e:
            if (
                e.response.get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"
            ):
                logger.info(
                    f"Skipping reconcile for {prefix}: row was moved by "
                    "another writer between our read and this write "
                    "(status changed to non-terminal, OR another reconcile "
                    "wrote a fresher contentSignature). Our memo-divergence "
                    "check will pick up the newer state on the next poll."
                )
                return None
            raise
        # Success — record what we just wrote so the memo can short-circuit
        # subsequent polls inside the TTL window.
        _RECONCILE_MEMO[prefix] = (new_signature, time.monotonic())
        logger.info(
            f"Reconciled test set {prefix}: {sorted(changed.keys())}"
            + (" (+cleared error)" if clear_error else "")
        )

        # Merge into the in-memory row so the caller can return fresh values
        # without a second DDB read.
        merged = dict(existing_row)
        merged.update(changed)
        if clear_error:
            merged.pop("error", None)
        return merged

    except Exception as e:
        # Reconcile is best-effort; a transient S3/DDB failure must not break
        # the whole getTestSets response. The next call will retry.
        # ``logger.exception`` captures the traceback so a programmer bug
        # (KeyError, AttributeError, TypeError) is visible in CloudWatch
        # instead of being indistinguishable from a fast-path skip.
        logger.exception(f"Error reconciling test set {prefix}: {str(e)}")
        return None


def list_bucket_files(args):
    logger.info(
        f"Listing files with pattern: {args['filePattern']} from bucket type: {args['bucketType']}"
    )

    file_pattern = args["filePattern"]
    bucket_type = args["bucketType"]
    modified_after = args.get("modifiedAfter")

    # Determine which bucket to use based on bucket type
    if bucket_type == "input":
        bucket = os.environ["INPUT_BUCKET"]
    elif bucket_type == "testset":
        bucket = os.environ["TEST_SET_BUCKET"]
    else:
        raise Exception(f"Invalid bucket type: {bucket_type}")

    files = find_matching_files(bucket, file_pattern, modified_after=modified_after)
    logger.info(f"Found {len(files)} matching files in {bucket_type} bucket")

    return files


def validate_test_file_name(args):
    logger.info(f"Validating test file name: {args['fileName']}")

    test_set_name = args["fileName"]
    test_set_id = f"{test_set_name.replace(' ', '-').lower()}"

    # Check if test set already exists in tracking table
    try:
        item = db_client.get_item({"PK": f"testset#{test_set_id}", "SK": "metadata"})

        if item:
            logger.info(f"Test set {test_set_id} already exists")
            return {"exists": True, "testSetId": test_set_id}
        else:
            logger.info(f"Test set {test_set_id} does not exist")
            return {"exists": False, "testSetId": None}
    except Exception as e:
        logger.error(f"Error checking test set existence: {str(e)}")
        return {"exists": False, "testSetId": None}
