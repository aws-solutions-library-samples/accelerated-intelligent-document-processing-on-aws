# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bulk review resolver: bulk Skip All Reviews, its jobs, and Send to review.

A thin dispatcher over ``idp_common.review_queues`` (``bulk_jobs.BulkSkipService``
and ``send_to_review.SendToReviewService``). The caller comes only from verified
identity claims (``_caller_context``). Errors leave as exactly ``PermissionError``
(403), ``ValueError`` (400) or ``ResourceNotFound`` (404), because the dispatcher
maps them by class name. Per-document refusals (out of scope, unknown key, held by
another reviewer, ...) are outcomes in the result, never errors.
"""

import json
import logging
import os
import threading
from enum import Enum
from typing import Any, Callable, Optional

import boto3
from botocore.config import Config
from pydantic import BaseModel, ValidationError
from pydantic.alias_generators import to_camel

from idp_common.api_adapter import ResourceNotFound
from idp_common.config_scope import (
    caller_email_from_claims,
    caller_sub_from_claims,
    resolve_allowed_config_versions,
)
from idp_common.document_scope import assert_profile_in_scope
from idp_common.notifications import recipient_key_from_claims
from idp_common.review_queues import ReviewAccessDenied, ReviewValidationError
from idp_common.review_queues.api_models import summarize_validation_error
from idp_common.review_queues.bulk_models import (
    BulkMode,
    BulkSkipRequest,
    ReviewCaller,
    SendToReviewRequest,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_DYNAMODB = boto3.resource("dynamodb")
_SCOPE_CACHE: dict = {}
#: The inline path runs inside the dispatcher's 20 s budget: short timeouts, one retry.
_FAST = Config(connect_timeout=1, read_timeout=3, retries={"total_max_attempts": 2})

_REVIEWERS = frozenset({"Admin", "Reviewer"})

#: Mirrors scripts/api_rbac_expectations.yaml.
_FIELD_GROUPS: dict[str, frozenset] = {
    "bulkSkipAllReviews": _REVIEWERS,
    "getBulkReviewJob": _REVIEWERS,
    "listBulkReviewJobs": _REVIEWERS,
    "sendToReview": _REVIEWERS,
}


# ----------------------------------------------------------- wiring


class _Services:
    """Built on first use (module scope, so warm invocations reuse them)."""

    def __init__(self) -> None:
        from idp_common.dynamodb.client import DynamoDBClient
        from idp_common.review_queues.bulk_jobs import BulkJobStore, BulkSkipService
        from idp_common.review_queues.bulk_selection import PendingReviewQuery
        from idp_common.review_queues.store import ReviewStore

        hitl_table = os.environ["HITL_TABLE_NAME"]
        tracking_table = os.environ["TRACKING_TABLE"]
        self._local = threading.local()
        self.store = ReviewStore(hitl_table)
        self.tracking_client = DynamoDBClient(table_name=tracking_table)
        self.sqs = boto3.client("sqs", config=_FAST)
        self.job_queue_url = os.environ.get("JOB_QUEUE_URL", "")
        self.bulk = BulkSkipService(
            self._deps,
            BulkJobStore(DynamoDBClient(table_name=hitl_table)),
            PendingReviewQuery(self.tracking_client),
            tracking_client=self.tracking_client,
            job_sender=self.send_job if self.job_queue_url else None,
            retention_days=int(os.environ.get("DATA_RETENTION_IN_DAYS") or 365),
        )
        self._send: Any = None

    def _deps(self):
        """One set of clients per thread (boto3 resources aren't thread-safe)."""
        from idp_common.docs_service import create_document_service
        from idp_common.review_queues.review_skip import SkipDeps
        from idp_common.review_queues.service import (
            create_resubmitter,
            create_review_queue_service,
        )

        documents = create_document_service(mode="dynamodb")
        service = create_review_queue_service()
        table = boto3.resource("dynamodb", config=_FAST).Table(
            os.environ["TRACKING_TABLE"]
        )
        return SkipDeps(
            tracking_table=table,
            get_document=documents.get_document,
            review_service=service,
            store=service.store if service is not None else self.store,
            resubmitter=create_resubmitter(documents),
        )

    def send_job(self, job_id: str, wake_token: str, delay_seconds: int) -> None:
        self.sqs.send_message(
            QueueUrl=self.job_queue_url,
            MessageBody=json.dumps({"jobId": job_id, "wakeToken": wake_token}),
            DelaySeconds=max(0, min(900, int(delay_seconds))),
            MessageAttributes={
                "EventType": {"DataType": "String", "StringValue": "BulkReviewJob"}
            },
        )

    @property
    def send(self):
        if self._send is None:
            from idp_common.docs_service import create_document_service
            from idp_common.review_queues.send_to_review import (
                create_send_to_review_service,
            )

            self._send = create_send_to_review_service(
                self.store,
                _DYNAMODB.Table(os.environ["TRACKING_TABLE"]),
                create_document_service(mode="dynamodb"),
            )
        return self._send


_SERVICES: Optional[_Services] = None


def _services() -> _Services:
    global _SERVICES
    if _SERVICES is None:
        _SERVICES = _Services()
    return _SERVICES


# ----------------------------------------------------------- caller and scope


def _groups(event: dict) -> list:
    claims = (event.get("identity") or {}).get("claims") or {}
    groups = claims.get("cognito:groups") or []
    return [groups] if isinstance(groups, str) else list(groups)


def _caller_context(event: dict) -> ReviewCaller:
    """The caller, from verified claims only, with their configuration scope.

    The scope (``allowed_config_versions``) fails closed: a lookup that can't be
    evaluated refuses the request. Per-document scope (``scope_allows``) is applied
    in ``review_skip.classify``; a by-profile request is checked in
    :func:`_enforce_scope`.
    """
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = frozenset(_groups(event))
    try:
        reviewer_key = recipient_key_from_claims(claims)
    except Exception:
        logger.warning("Rejecting bulk review request code=ROLE: no caller identity")
        raise PermissionError("ROLE: no caller identity") from None
    email = caller_email_from_claims(claims).lower()
    is_admin = "Admin" in groups
    allowed = None
    if not is_admin:
        try:
            allowed = resolve_allowed_config_versions(
                email,
                users_table_name=os.environ.get("USERS_TABLE_NAME", ""),
                dynamodb=_DYNAMODB,
                caller_sub=caller_sub_from_claims(claims),
                cache=_SCOPE_CACHE,
            )
        except Exception:
            logger.warning(
                "Rejecting bulk review request code=OUT_OF_SCOPE: scope lookup failed"
            )
            raise PermissionError("OUT_OF_SCOPE: scope lookup failed") from None
    return ReviewCaller(
        reviewer_key=reviewer_key,
        username=str(identity.get("username") or ""),
        email=email,
        groups=groups,
        is_admin=is_admin,
        allowed_versions=allowed,
    )


def _enforce_scope(caller: ReviewCaller, request: BulkSkipRequest) -> None:
    """A by-profile request is refused unless the profile is in the caller's scope,
    before any document is read. A selected-documents request is scoped per document
    (``scope_allows`` in ``review_skip.classify``): an out-of-scope document is the
    in-band outcome ``NOT_PERMITTED``, the same as an unknown key."""
    if request.mode is BulkMode.PROFILE and not caller.is_admin:
        assert_profile_in_scope(
            caller.allowed_versions, request.profile, what="profile"
        )


# ----------------------------------------------------------- shapes


def _camel(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _camel(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {to_camel(k): _camel(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_camel(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _job_shape(view: Any) -> dict:
    shape = _camel(view)
    shape.pop("requesterKey", None)
    return shape


_INPUT_FIELDS = {
    "mode": "mode",
    "objectKeys": "object_keys",
    "profile": "profile",
    "submittedFrom": "submitted_from",
    "submittedTo": "submitted_to",
    "reason": "reason",
    "note": "note",
    "resubmit": "resubmit",
    "dryRun": "dry_run",
    "clientRequestId": "client_request_id",
}


def _bulk_request(args: dict) -> BulkSkipRequest:
    raw = args.get("input")
    if not isinstance(raw, dict):
        raise ValueError("BAD_ARGUMENT: input is required")
    unknown = set(raw) - set(_INPUT_FIELDS)
    if unknown:
        raise ValueError("BAD_ARGUMENT: unknown input field " + sorted(unknown)[0])
    data = {_INPUT_FIELDS[k]: v for k, v in raw.items() if v is not None}
    return BulkSkipRequest.model_validate(data)


def _limit(args: dict, default: int, maximum: int) -> int:
    value = args.get("limit")
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("BAD_ARGUMENT: limit must be a positive integer")
    return min(value, maximum)


# ----------------------------------------------------------- operations


def _bulk_skip(caller: ReviewCaller, args: dict) -> dict:
    request = _bulk_request(args)
    _enforce_scope(caller, request)
    return _camel(_services().bulk.submit(caller, request))


def _get_job(caller: ReviewCaller, args: dict) -> dict:
    job_id = args.get("jobId")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("BAD_ARGUMENT: jobId is required")
    outcome = args.get("outcome")
    try:
        page = _services().bulk.get_job(
            caller,
            job_id,
            outcome=outcome,
            limit=_limit(args, 100, 500),
            next_token=args.get("nextToken"),
        )
    except ValueError:
        raise ValueError("BAD_ARGUMENT: unknown outcome") from None
    if page is None:
        raise ResourceNotFound("NOT_FOUND: no such job")
    return {
        "job": _job_shape(page.job),
        "results": _camel(page.results),
        "nextToken": page.next_token,
    }


def _list_jobs(caller: ReviewCaller, args: dict) -> dict:
    page = _services().bulk.list_jobs(
        caller, limit=_limit(args, 25, 100), next_token=args.get("nextToken")
    )
    return {"jobs": [_job_shape(j) for j in page.jobs], "nextToken": page.next_token}


def _send_to_review(caller: ReviewCaller, args: dict) -> dict:
    request = SendToReviewRequest.model_validate(
        {
            "object_keys": args.get("objectKeys") or [],
            "reason": args.get("reason"),
            "note": args.get("note"),
        }
    )
    return _camel(_services().send.send(caller, request))


_FIELDS: dict[str, Callable[[ReviewCaller, dict], Any]] = {
    "bulkSkipAllReviews": _bulk_skip,
    "getBulkReviewJob": _get_job,
    "listBulkReviewJobs": _list_jobs,
    "sendToReview": _send_to_review,
}


def handler(event, context):
    field = (event.get("info") or {}).get("fieldName", "")
    args = event.get("arguments") or {}
    # Field and argument keys only: never identity, claims, notes or keys.
    logger.info("Bulk review resolver field=%s argKeys=%s", field, sorted(args))
    fn = _FIELDS.get(field)
    if fn is None:
        raise ValueError("BAD_ARGUMENT: unsupported field")
    if not _FIELD_GROUPS[field].intersection(_groups(event)):
        logger.warning("Rejecting bulk review request field=%s code=ROLE", field)
        raise PermissionError(
            "ROLE: this operation requires one of "
            + ", ".join(sorted(_FIELD_GROUPS[field]))
        )
    caller = _caller_context(event)
    try:
        return fn(caller, args)
    except ValidationError as e:
        raise ValueError("BAD_ARGUMENT: " + summarize_validation_error(e)) from None
    except ReviewAccessDenied as e:
        logger.warning("Rejecting bulk review request field=%s code=%s", field, e.code)
        raise PermissionError(str(e)) from None
    except ReviewValidationError as e:
        logger.info("Refused bulk review request field=%s code=%s", field, e.code)
        raise ValueError(str(e)) from None
