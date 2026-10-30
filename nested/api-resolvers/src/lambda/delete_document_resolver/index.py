# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Delete document resolver Lambda function.

This function handles document deletion via GraphQL API, using the shared
delete_documents implementation from idp_common for consistent behavior
across CLI, SDK, and Web UI.
"""

import json
import logging
import os
import re
from typing import List

import boto3
from idp_common.api_adapter import ReportedServerError
from idp_common.delete_documents import delete_single_document
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

# Review queues: a deleted document's review items are purged with it. None when
# HITL_TABLE_NAME is unset; the store builds no client until it is used.
_REVIEW_STORE = None
if os.environ.get("HITL_TABLE_NAME"):
    from idp_common.review_queues.store import create_review_store

    _REVIEW_STORE = create_review_store()


#: Any ARN, in any partition. Matched to the first whitespace, quote or comma, which is
#: how an ARN is delimited inside an AWS error sentence. The partition segment is a
#: pattern rather than the literal ``aws``, because GovCloud and China spell it
#: ``aws-us-gov`` and ``aws-cn`` and this repository supports both.
_ARN = re.compile(r"arn:[a-z0-9-]*:[^\s\"',;)]*")

#: A standalone 12-digit run, which is what an AWS account id looks like.
#:
#: The ARN pattern above covers every message shape actually observed on this path —
#: an authorization denial names the principal and the resource as ARNs — but "the
#: shapes I could name" is a weaker claim than "no account id", and the two are one
#: differently-worded service error apart. The lookarounds keep it from matching part
#: of a longer number.
_BARE_ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")


def _redact_arns(text: str) -> str:
    """Strip ARNs out of a message that is about to be shown in a browser.

    The reason a failed delete now reports its cause is that "it failed" is not
    actionable — but an AWS authorization denial names the **execution role's ARN and
    the resource ARN**, both of which embed this deployment's account id:

        User: arn:...:assumed-role/<stack>-DeleteDocumentResolverFunctionRole-.../...
        is not authorized to perform: dynamodb:Query on resource: arn:...:table/...

    What a reader needs out of that is the error code and the action — "not authorized
    to perform: dynamodb:Query" — and those survive redaction. The account id does not
    need to reach a browser to make the message useful, and this repository treats a
    committed account id as a gate failure (``scripts/check_account_ids.py``), so
    putting one in a UI banner would be inconsistent with that posture even though the
    operation is Admin/Author-gated.

    Redaction rather than truncation, because truncating at the operation name would
    also discard details that are the whole answer and carry no identifiers — the
    rejected-expression fault ends with "Primary key attribute: SK", which is what
    tells a maintainer *which* attribute was refused. The unredacted text stays in the
    resolver's log, which is where an operator with access to the account should read
    it.

    A bare 12-digit run is redacted as well, for the message that names an account
    without wrapping it in an ARN. ⚠️ **That can also mask a 12-digit run inside the
    error detail** — the purge failure quotes a sample of refused object keys, and a
    key carrying a twelve-digit invoice or batch number would come back partly masked.
    The document being reported is named *outside* this function, in the
    ``"{object_key}: "`` prefix the caller builds, so identifying which document failed
    is unaffected; what is lost is a duplicate of that key inside the detail.
    """
    return _BARE_ACCOUNT_ID.sub("<account>", _ARN.sub("arn:<redacted>", text))


def _caller_in_groups(event, allowed):
    """Defense-in-depth RBAC check against the caller's Cognito groups.

    The schema restricts this field via @aws_cognito_user_pools(cognito_groups),
    but we also enforce the group server-side so the operation is never reachable
    by an unauthorized caller even if the schema directive is missing or
    misconfigured (e.g. the prior @aws_auth directive, which AppSync silently
    ignores on a multi-auth API).
    """
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return bool(set(allowed).intersection(groups))


def handler(event, context):
    """Handle document deletion requests from AppSync."""
    logger.info(f"Delete document resolver invoked with event: {json.dumps(sanitize_event_for_logging(event))}")

    try:
        # Defense-in-depth: deleteDocument is an Admin+Author operation.
        if not _caller_in_groups(event, ("Admin", "Author")):
            raise PermissionError(
                "Unauthorized: deleteDocument requires Admin or Author group"
            )

        object_keys: List[str] = event["arguments"]["objectKeys"]

        # Validate input
        if not object_keys or not isinstance(object_keys, list):
            raise ValueError("objectKeys must be a non-empty list")

        tracking_table = dynamodb.Table(os.environ["TRACKING_TABLE_NAME"])
        input_bucket = os.environ["INPUT_BUCKET"]
        output_bucket = os.environ["OUTPUT_BUCKET"]

        logger.info(f"Preparing to delete {len(object_keys)} documents: {object_keys}")

        deleted_count = 0
        failures: List[str] = []

        # Delete each document using the shared idp_common implementation
        for object_key in object_keys:
            logger.info(f"Processing deletion for document: {object_key}")

            result = delete_single_document(
                object_key=object_key,
                tracking_table=tracking_table,
                s3_client=s3,
                input_bucket=input_bucket,
                output_bucket=output_bucket,
                review_store=_REVIEW_STORE,
            )

            if result["success"]:
                deleted_count += 1
                logger.info(f"Successfully deleted document: {object_key}")
            else:
                errors = result.get("errors", [])
                # What the cleanup DID remove, so the message cannot imply that nothing
                # happened to a document most of whose cleanup succeeded. An S3 purge
                # failure in particular does not hold back the tracking record, so the
                # document is gone from the list even though this call failed — saying
                # only "0 deleted" would send the reader looking for a row that is not
                # there.
                #
                # ⚠️ **Values, not a list of names.** Reporting the truthy keys as
                # "completed" was wrong in both directions, because two of the five are
                # counts rather than flags: a purge that deleted 9 of 10 versions listed
                # `output_files` as completed in the same sentence that said a version
                # was refused, and a document with no output objects omitted it although
                # that step succeeded with nothing to do. A count has no true/false
                # answer, so the honest form is to show it.
                removed = ", ".join(
                    f"{name}={value}"
                    for name, value in sorted((result.get("deleted") or {}).items())
                )
                failures.append(
                    f"{object_key}: "
                    + _redact_arns("; ".join(str(e) for e in errors))
                    + (f" [removed: {removed}]" if removed else "")
                )
                logger.error(f"Failed to delete document {object_key}: {errors}")

        logger.info(
            f"Completed deletion: {deleted_count} successful, {len(failures)} failed"
        )

        # ⚠️ **A failed delete RAISES; it does not return a falsy value.** Returning
        # `failed_count == 0` was an HTTP 200 whose body was `false`, and nothing
        # between here and the user looked at it: the dispatcher returns a 200 for any
        # value a resolver yields, and the UI only logged the result at debug level. So
        # a delete that cleared a document's version rows, could not clear its list row
        # and kept the document record answered "OK" to the browser, with the two
        # underlying faults reaching only the resolver's own log (#1366). A raise is the
        # only outcome the transport distinguishes — the dispatcher maps it to a non-200
        # carrying this message, which the UI surfaces.
        #
        # The loop above still attempts every document before this point, so one
        # document's failure does not abandon the rest of the selection, and the message
        # names each one that failed and why rather than a bare count: the cause is
        # usually a deployment-level fault (a missing permission, a rejected
        # expression) that is identical for every document and actionable only if it is
        # said out loud. The operation is Admin/Author-gated.
        # "could not be completely deleted" rather than "deleted N of M": a document
        # whose output purge was refused has already had its list row, run records and
        # tracking record removed, because an S3 failure deliberately does not hold the
        # record back. Reporting that as "deleted 0 of 1" is true only of *complete*
        # deletes and reads to a user as "nothing happened" — after which the document
        # is gone from the list anyway. The per-document detail above says which steps
        # did complete.
        # `ReportedServerError`, so the dispatcher passes this message through: it
        # is written for the caller, with ARNs redacted, and an unexpected
        # exception's text is replaced by a generic message.
        if failures:
            raise ReportedServerError(
                f"{len(failures)} of {len(object_keys)} document(s) could not be "
                f"completely deleted ({deleted_count} completed) — "
                + " | ".join(failures)
            )
        return True

    except Exception as e:
        logger.error(f"Error in delete_document resolver: {str(e)}", exc_info=True)
        raise e