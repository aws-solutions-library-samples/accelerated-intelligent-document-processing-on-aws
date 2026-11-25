# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from idp_common.api_adapter import ResourceAccessDenied, ResourceNotFound
from idp_common.config.feature_flags import step_enabled
from idp_common.docs_service import create_document_service

# Import IDP Common modules
from idp_common.models import Section, Status
from idp_common.review_queues.regrouping import discard_drafts, regroup_refusal
from idp_common.review_queues.statuses import LegacyHitlStatus, normalize_hitl_status
from idp_common.run_plan.clearing import (
    ROW_POLICY_RULE_VALIDATION,
    clear_for_start,
    clear_policy_rule_validation,
)
from idp_common.run_plan.eligibility import (
    EligibilityContext,
    ReprocessRequest,
    check,
    reviewed_for,
)
from idp_common.run_plan.mode import PIPELINE, ProcessingMode, resolve_processing_mode
from idp_common.run_plan.models import RunKind, RunPlan
from idp_common.run_plan.planned import planned_run
from idp_common.run_plan.restart import decide_restart
from idp_common.run_plan.sender import EVENT_DOCUMENT_RUN_REQUESTED, send_run
from idp_common.run_plan.steps import BEGINNING, POLICY_RULE_VALIDATION
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
s3_client = boto3.client("s3")
sqs_client = boto3.client("sqs")

# Review queues: a document in review is regrouped only by its item's holder. None
# when HITL_TABLE_NAME is unset; the store builds no client until it is used.
_REVIEW_STORE = None
if os.environ.get("HITL_TABLE_NAME"):
    from idp_common.review_queues.store import create_review_store

    _REVIEW_STORE = create_review_store()

# Environment variables
QUEUE_URL = os.environ.get("QUEUE_URL")

_QUEUE_MANAGED_MESSAGE = (
    "QUEUE_MANAGED: this document is in a review queue; work it from Review Queues"
)

# Groups permitted per operation in this resolver. updateDocumentSections matches
# completeSectionReview (Admin, Reviewer, Annotator) — the sibling that edits a
# section's CONTENT — rather than processChanges' narrower pair, because correcting a
# packet split is the same kind of annotation work and, unlike processChanges, it does
# not requeue the document through the pipeline. getProcessChangesPlan says what
# processChanges would run, so it admits the callers processChanges admits.
_OPERATION_GROUPS = {
    "processChanges": {"Admin", "Reviewer"},
    "getProcessChangesPlan": {"Admin", "Reviewer"},
    "updateDocumentSections": {"Admin", "Reviewer", "Annotator"},
}

# A document being processed must not be written underneath the pipeline. This is the
# hazard processChanges avoids by not persisting at all (it hands the document to SQS
# and lets the run save it); a direct write has to check for itself.
#
# Stated as the statuses that ARE safe, not the ones that are not. The first version of
# this was a denylist of {QUEUED, RUNNING}, which let through every intermediate status
# the pipeline actually spends its time in — OCR, CLASSIFYING, EXTRACTING, ASSESSING,
# SUMMARIZING, RULE_VALIDATION* and the rest — so the guard missed 14 of the 16
# non-terminal states it existed to catch. A denylist over an enum somebody else extends
# is the wrong shape: adding a pipeline stage to Status silently widened the hole. An
# allow-list fails closed instead, and a new status is refused until it is considered.
#
# REDACTED_SUPERSEDED is terminal too: a preprocessing hook produced a redacted copy
# that processes separately, and this original is deliberately left alone.
_EDITABLE_STATUSES = {
    Status.COMPLETED.value,
    Status.FAILED.value,
    Status.ABORTED.value,
    Status.REDACTED_SUPERSEDED.value,
}

# Roles `_OPERATION_GROUPS` admits that are NOT additionally confined per object, and
# the role that is. Named rather than inlined so the two halves of the Annotator rule
# read as one decision: the group list above says which callers reach the operation,
# `_assert_annotator_scope` says which objects an Annotator reaches.
#
# Reviewer is deliberately absent from the unscoped set even though the group list
# admits it. Admin and Author are the pair `testset_scope` itself treats as unscoped,
# and exempting Reviewer as well would turn object scope OFF for anyone holding both
# Reviewer and Annotator instead of intersecting the two — the same correction
# `complete_section_review` carries, for the same reason: annotators are assigned by
# hand in Cognito, so holding both is an easy mistake to make.
_SCOPE_EXEMPT_GROUPS = {"Admin", "Author"}
_SCOPED_GROUP = "Annotator"

# A Reviewer works production review only: test-set documents are worked by
# Annotators. A caller holding Reviewer and none of these is refused a document that
# carries a `TestSetId` (`_refuse_reviewer_on_test_set`).
_REVIEWER_GROUP = "Reviewer"
_TEST_SET_WORK_GROUPS = {"Admin", "Author", "Annotator"}
_REVIEWER_TEST_SET_REFUSAL = "Unauthorized: test-set documents are worked by Annotators"


def _is_reviewer_only(groups):
    """Reviewer, and none of Admin, Author or Annotator."""
    return _REVIEWER_GROUP in groups and not _TEST_SET_WORK_GROUPS.intersection(groups)


def _refuse_reviewer_on_test_set(operation, groups, test_set_id):
    """Refuse a Reviewer-only caller a document that belongs to a test set.

    A decision about the OBJECT, so it raises `ResourceAccessDenied` (a
    `PermissionError`, 403, ``authzControl: "resource"``), as this resolver's other
    per-object refusals do.
    """
    if test_set_id and _is_reviewer_only(groups):
        logger.warning(
            "Forbidden: reviewer may not %s a test-set document; test-set documents "
            "are worked by Annotators",
            operation,
        )
        raise ResourceAccessDenied(_REVIEWER_TEST_SET_REFUSAL)


# Built on first use, not at import. The scope check is the only thing in this module
# that reads DynamoDB directly, and a resource built at import time binds before a
# test's mock (or moto) is in place.
_tracking_table = None


def _tracking_table_resource():
    """The tracking table, for the scope check's `TestSetId` read."""
    global _tracking_table
    if _tracking_table is None:
        _tracking_table = boto3.resource("dynamodb").Table(
            os.environ.get("TRACKING_TABLE", "")
        )
    return _tracking_table


def _assert_annotator_scope(event, object_key):
    """Verify a scoped Annotator may re-group this document.

    The per-object half of this operation's authorization. `_OPERATION_GROUPS` admits
    Annotator, which is a decision about the caller's GROUP; an Annotator is
    additionally confined to the test sets named in their `allowedTestSets`, which is
    a decision about the OBJECT they named. The three operations this operation's
    group list was copied from — `completeSectionReview`, `claimReview`,
    `releaseReview` — each apply this before touching the document. The group list was
    copied and the per-object check was not, so the operation admitted the role
    without confining it.

    The rule itself is `testset_scope.assert_can_access_test_set`, called unchanged:
    a scope matcher that differs between call sites admits somewhere what it denies
    elsewhere. What is local here is the derivation of the scope subject — a review
    document carries its originating `TestSetId` on its tracking-table record — and
    the refusal class.

    Shape follows `src/lambda/complete_section_review/index.py`'s helper of the same
    name, including that a document carrying no `TestSetId` is production work and is
    not read as unscoped: an Annotator may re-group it only while holding its
    review-queue item (`_assert_holds_review_item`), and is refused otherwise. Two things differ, both because this
    resolver's handler is shaped differently from that one's:

    * **The refusal class.** This resolver's own group floor raises `PermissionError`,
      which the dispatcher labels `authzControl: "resolver"`. Refusing the object with
      the same class would make the two refusals indistinguishable in the response,
      which is the attribution defect #1308 is about — and here specifically it would
      read as "the group floor stopped admitting Annotator". `ResourceAccessDenied`
      carries `authzControl: "resource"`, so the refusal is attributed to the object.
      It subclasses `PermissionError`, so `handler`'s existing `except
      PermissionError: raise` arm — which sits BEFORE the catch-all — keeps it out of
      the 200-with-`success: false` body the catch-all produces.

    * **A failed lookup is turned into a refusal here** rather than left to
      propagate. In the sibling an uncaught read error reaches the Lambda runtime; in
      this handler the catch-all would convert it into a 200 saying the re-grouping
      did not happen, which answers a question about availability to a question about
      authorization. An authorization question that cannot be answered must resolve to
      refused — not to allowed, and not to a fault.

    ⚠️ Resolving an Annotator's `allowedTestSets` reads the users table, which
    `testset_scope` locates through `USERS_TABLE_NAME`. Where that is unset the lookup
    yields no scope and every Annotator is refused — fail-closed, and the direction to
    fail in, but it also makes the role's legitimate use unavailable. See the note on
    this function in the operation's entry in `scripts/api_rbac_expectations.yaml`.
    """
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    if _SCOPE_EXEMPT_GROUPS.intersection(groups):
        return
    if _SCOPED_GROUP not in groups and not _is_reviewer_only(groups):
        return

    from idp_common.testset_scope import (
        TestSetAccessDenied,
        assert_can_access_test_set,
    )

    try:
        item = _tracking_table_resource().get_item(
            Key={"PK": f"doc#{object_key}", "SK": "none"}
        )
    except Exception as e:  # noqa: BLE001 — fail closed, see the docstring
        logger.error(
            "Refusing updateDocumentSections: the document's test set could not be "
            "read, so the caller's scope could not be evaluated: %s",
            e,
        )
        raise ResourceAccessDenied(
            "Unauthorized: your test-set assignment could not be verified"
        ) from e

    test_set_id = (item.get("Item") or {}).get("TestSetId")
    if _is_reviewer_only(groups):
        # A Reviewer re-groups production documents only; same read, no second one.
        _refuse_reviewer_on_test_set("re-group", groups, test_set_id)
        return
    if not test_set_id:
        # A production document: an Annotator may re-group it only while holding its
        # review-queue item, within their profile scope (`_assert_holds_review_item`).
        # Whether the document is in review, and the holder check itself, are decided
        # again below by `regroup_refusal`, as for every caller.
        _assert_holds_review_item(event, object_key)
        return

    try:
        assert_can_access_test_set(event, test_set_id)
    except TestSetAccessDenied as e:
        raise ResourceAccessDenied(str(e)) from e


def _assert_holds_review_item(event, object_key):
    """Verify an Annotator holds this production document's review-queue item.

    Regrouping is part of review, so the Annotator a Production item is assigned to
    may correct the document's split while working it, as an Admin holding it may.
    Admitted only when all of these hold: the stack has review queues, the document
    has a review item, the item is CLAIMED by the caller (the owner key a claim
    records, from verified claims), and the item's own profile is within the caller's
    ``allowedConfigVersions`` (``scope_allows``, which admits no item without a
    profile to a scoped caller). Every other case is refused, and so is a lookup that
    cannot be answered: as in `_assert_annotator_scope`, an authorization question
    that cannot be answered resolves to refused.
    """
    from idp_common.config_scope import ScopeLookupError, scope_allows
    from idp_common.review_queues.statuses import QueueItemState

    refused = ResourceAccessDenied(
        "Unauthorized: annotators may re-group only test-set documents and review "
        "items assigned to them"
    )
    if _REVIEW_STORE is None:
        logger.warning(
            "Forbidden: annotator re-group of a document with no review queues"
        )
        raise refused
    try:
        review_item = _REVIEW_STORE.get_item(object_key)
    except Exception as e:  # noqa: BLE001 — fail closed, see the docstring
        logger.error(
            "Refusing updateDocumentSections: the document's review item could not be "
            "read, so the caller's assignment could not be evaluated: %s",
            e,
        )
        raise ResourceAccessDenied(
            "Unauthorized: your review assignment could not be verified"
        ) from e
    caller_key = _caller_key(event)
    if (
        review_item is None
        or review_item.state != QueueItemState.CLAIMED.value
        or not caller_key
        or review_item.owner_key != caller_key
    ):
        logger.warning(
            "Forbidden: annotator attempted to re-group a document whose review item "
            "they do not hold"
        )
        raise refused
    claims = (event.get("identity") or {}).get("claims") or {}
    try:
        scope = _caller_config_scope(claims)
    except ScopeLookupError as e:
        logger.error(
            "Refusing updateDocumentSections: config-version scope unresolved: %s", e
        )
        raise ResourceAccessDenied(
            "Unauthorized: your configuration scope could not be verified"
        ) from e
    if not scope_allows(scope, review_item.profile):
        logger.warning(
            "Forbidden: annotator re-group of a review item outside their configuration scope"
        )
        raise ResourceAccessDenied(
            "Access denied: this document is outside your allowed configuration scope"
        )


def _assert_production_document_in_profile_scope(event, object_key):
    """Refuse a non-Admin caller a production document outside their profile scope.

    The test-set rules above decide who may re-group a test-set document. A
    production document (no ``TestSetId``) is governed by ``allowedConfigVersions``,
    as every other single-document operation is: the document's profile, read from
    its tracking row, must be one the caller's scope admits. This applies to every
    non-Admin role the operation admits — a Reviewer, and an Author or Annotator who
    also holds Reviewer — so it is checked here, once, rather than on each role's
    branch. A missing row is refused for a scoped caller; a lookup that cannot be
    answered is refused for every non-Admin caller.
    """
    from idp_common import document_scope

    claims = document_scope.caller_claims(event)
    if document_scope.is_unscoped_caller(claims):
        return
    try:
        scope = document_scope.resolve_caller_scope(
            claims,
            users_table_name=os.environ.get("USERS_TABLE_NAME", ""),
            dynamodb=lambda: boto3.resource("dynamodb"),
            cache=_user_scope_cache,
        )
    except PermissionError as e:
        raise ResourceAccessDenied(str(e)) from e
    if scope is None:
        return
    try:
        row = (
            _tracking_table_resource()
            .get_item(Key={"PK": f"doc#{object_key}", "SK": "none"})
            .get("Item")
        )
    except Exception as e:  # noqa: BLE001 — fail closed
        logger.error(
            "Refusing updateDocumentSections: the document's profile could not be "
            "read, so the caller's scope could not be evaluated: %s",
            e,
        )
        raise ResourceAccessDenied(document_scope.REFUSAL) from e
    if row and row.get("TestSetId"):
        return
    version = document_scope.config_version_of_item(row)
    try:
        if document_scope.is_absent(version):
            raise PermissionError(document_scope.REFUSAL)
        document_scope.assert_profile_in_scope(scope, version)
    except PermissionError as e:
        raise ResourceAccessDenied(str(e)) from e


def _refusal(message):
    """A processChanges refusal: a 400 through the dispatcher, never a 200 body.

    Deliberately a plain ``ValueError``: the dispatcher maps a resolver error to 400
    by its class NAME (``errorType == "ValueError"``), so a subclass would surface as
    a 500. The marker is what lets ``handler``'s two ``except ValueError`` arms tell
    a refusal apart from a processing error, which they turn into a 200 with
    ``success: false`` instead.
    """
    error = ValueError(message)
    error.process_changes_refusal = True  # type: ignore[attr-defined]
    return error


def _is_refusal(error):
    return (
        getattr(error, "process_changes_refusal", False)
        or str(error) == _QUEUE_MANAGED_MESSAGE
    )


# Built on first use, for the same reason as `_tracking_table`.
_users_dynamodb = None
_user_scope_cache: dict = {}


def _caller_config_scope(claims):
    """The caller's ``allowedConfigVersions``, or None when unrestricted.

    The shared fail-closed lookup, called unchanged, so this resolver resolves the
    scope exactly as the document-list and reprocess resolvers do. Raises
    ``ScopeLookupError`` when the scope cannot be evaluated.
    """
    from idp_common.config_scope import (
        caller_email_from_claims,
        caller_sub_from_claims,
        resolve_allowed_config_versions,
    )

    global _users_dynamodb
    if _users_dynamodb is None:
        _users_dynamodb = boto3.resource("dynamodb")
    return resolve_allowed_config_versions(
        caller_email_from_claims(claims),
        caller_sub=caller_sub_from_claims(claims),
        users_table_name=os.environ.get("USERS_TABLE_NAME", ""),
        dynamodb=_users_dynamodb,
        cache=_user_scope_cache,
    )


def _assert_caller_may_process(event, object_key, document):
    """The checks saving review edits applies, for a caller who is not an Admin.

    ``saveSectionReviewEdits`` (``src/lambda/complete_section_review``) lets a
    non-Admin write a section only while they own the document's open review, and
    refuses a finished review. processChanges writes more than that save does — it
    clears extraction results, completes the review and requeues the document — so
    it applies the same two checks, plus the two this resolver's own
    ``updateDocumentSections`` applies to a direct write: the document must not be
    in flight, and it must sit inside the caller's configuration scope.

    Order: scope first, because nothing about a document may be acted on for a
    caller it is out of scope for; then the review state; then the document status.
    Admin is unaffected, as it is on every sibling.

    Refusal classes follow the dispatcher's mapping. Scope and ownership are
    decisions about the OBJECT the caller named, so they raise
    ``ResourceAccessDenied`` (403, ``authzControl: "resource"``) rather than this
    resolver's group-floor ``PermissionError``. A finished review and a document in
    flight are state, so they raise a ``ValueError`` refusal (400), as the sibling
    does for a finished review.
    """
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    if "Admin" in groups:
        return

    from idp_common.config_scope import ScopeLookupError, scope_allows

    try:
        allowed = _caller_config_scope(claims)
    except ScopeLookupError as e:
        logger.error("Denying processChanges: config-version scope unresolved: %s", e)
        raise ResourceAccessDenied(
            "Unauthorized: your configuration scope could not be verified"
        ) from e
    if allowed is not None and not scope_allows(allowed, document.config_version):
        logger.warning(
            "Denied processChanges: the document was processed under config "
            "version %r, outside the caller's scope",
            document.config_version,
        )
        raise ResourceAccessDenied(
            "Access denied: this document is outside your allowed configuration scope"
        )

    # The review state is read from the tracking row, as the sibling reads it: the
    # owner is not on the Document model.
    try:
        row = (
            _tracking_table_resource()
            .get_item(
                Key={"PK": f"doc#{object_key}", "SK": "none"},
                ProjectionExpression="HITLStatus, HITLReviewOwner, TestSetId",
            )
            .get("Item")
            or {}
        )
    except Exception as e:  # noqa: BLE001 — an unanswerable owner check refuses
        logger.error(
            "Denying processChanges: the review owner could not be read: %s", e
        )
        raise ResourceAccessDenied(
            "Unauthorized: your ownership of this review could not be verified"
        ) from e

    # A Reviewer processes production documents only, read off the same row.
    _refuse_reviewer_on_test_set("process changes on", groups, row.get("TestSetId"))

    hitl_status = row.get("HITLStatus")
    view = normalize_hitl_status(hitl_status)
    # "COMPLETED" is the finished spelling the earlier A2I flow stored, which the
    # exact-match normaliser does not know; the sibling makes the same exception.
    if view.kind in ("complete", "rejected", "skipped") or hitl_status == "COMPLETED":
        logger.warning("Rejecting processChanges on a finished review")
        raise _refusal("This review is already finished; its edits are closed")

    username = identity.get("username") or ""
    owner = row.get("HITLReviewOwner") or ""
    if view.kind not in ("pending", "in_progress") or not username or owner != username:
        logger.warning("Denied processChanges by a caller who does not own the review")
        raise ResourceAccessDenied(
            "Only the review owner or an admin can process changes; start the "
            "review first"
        )

    status = str(getattr(document.status, "value", document.status) or "").upper()
    if status not in _EDITABLE_STATUSES:
        logger.warning("Rejecting processChanges on a document that is in flight")
        raise _refusal(
            f"{object_key} is currently {status.lower() or 'in an unknown state'}. "
            "Wait for processing to finish before processing changes."
        )


_BDA_MODE_MESSAGE = (
    "BDA_MODE: this document is processed with Bedrock Data Automation, which keeps "
    "a document's existing sections when it is reprocessed, so section, class, page "
    "and text changes are not available for it. Edit field values instead."
)


def _assert_not_a_bda_structural_edit(document, modified_sections, modified_pages):
    """Refuse a section or page change to a document that would reprocess in BDA mode.

    A BDA run keeps the sections a document already has (the workflow skips Bedrock
    Data Automation when any section exists), and this handler clears each changed
    section's extraction result before requeueing. Nothing in the BDA branch
    regenerates it, so the change would lose those values. Applies to every caller,
    Admin included: it is about what the run can do, not who asked.

    Stated as a capability rather than a list of edit kinds: in BDA mode this
    operation changes no section and no page. Any entry in either list is refused,
    and an empty request — field values already saved, reprocessed for evaluation
    and summarization — is unaffected.

    The mode is the one the requeued run will take: the routing rule the queue
    processor applies (``idp_common.config.configuration_manager.bda_routing``),
    including that an unpinned document is processed under the ACTIVE profile and
    that ``use_bda`` is read from the configuration revision the document is pinned
    to (the requeued document carries that pin to the queue processor, which routes
    by it), or from the profile head when it has none. The revision body lives in
    the Configuration bucket (``CONFIGURATION_BUCKET``). Every read is made strict
    (``raise_errors=True``), so a configuration that cannot be read is refused
    rather than read as pipeline mode: a wrong
    "pipeline" answer loses data and a wrong refusal costs a retry. That includes
    a pinned revision that cannot be read: the queue processor would route that
    run from the profile head, but the head is not the configuration the document
    is pinned to, so it is not used to allow the change here.
    """
    if not modified_sections and not modified_pages:
        return

    from idp_common.config import ConfigurationManager
    from idp_common.config.configuration_manager import bda_routing

    try:
        routing = bda_routing(
            ConfigurationManager(),
            document.config_version,
            raise_errors=True,
            config_revision=document.config_revision,
        )
    except Exception as e:  # noqa: BLE001 — an unknown mode refuses, see docstring
        logger.error(
            "Rejecting processChanges: the processing mode of profile %r "
            "(revision %r) could not be read: %s",
            document.config_version,
            document.config_revision,
            e,
        )
        raise _refusal(
            "The document's processing mode could not be determined, so section, "
            "page and text changes cannot be made now. Field values can still be "
            "edited."
        ) from e
    if routing.bda:
        logger.warning(
            "Rejecting processChanges: a section or page change to a BDA-mode document"
        )
        raise _refusal(_BDA_MODE_MESSAGE)


#: Stands in for the processing mode where it does not matter: a run from the
#: beginning exists in both modes and needs no earlier output.
_ANY_MODE = ProcessingMode(PIPELINE, None, None, False)


def _assert_no_other_run(object_key, document):
    """Refuse saved edits while a run is working on the document.

    Process Changes queues a run. A second run beside one that is in progress
    would have both write the same document, so the request is refused with the
    eligibility rule every run a person starts goes through
    (``idp_common.run_plan.eligibility``). The same rule refuses a document the
    malware gate holds or refused, which must never reach the pipeline.

    Applies to every caller, Admin included: it is about the document's state,
    not who asked. Runs before anything is written.
    """
    answer = check(
        ReprocessRequest(object_key=object_key, kind=RunKind.SAVED_EDITS),
        EligibilityContext(
            load_document=lambda _key: document,
            mode_for=lambda _profile, _revision: _ANY_MODE,
        ),
    )
    if not answer.ok:
        logger.warning("Rejecting processChanges: %s", answer.code)
        raise _refusal(f"{answer.code}: {answer.message}")


def _processing_mode(document):
    """The mode the requeued run takes, as the queue processor resolves it.

    Never raises: a configuration that cannot be read gives Pipeline mode, in
    which a step that is not ready is found not ready, so the run starts at the
    beginning.
    """
    try:
        from idp_common.config import ConfigurationManager

        manager = ConfigurationManager()
        version = document.config_version or manager.resolve_active_version()
        return resolve_processing_mode(manager, version, document.config_revision)
    except Exception as e:  # noqa: BLE001 — see the docstring
        logger.warning("Could not resolve the processing mode: %s", e)
        return _ANY_MODE


def _run_plan_for(object_key, document, modified_pages, mode=None):
    """The plan of the run that follows saved edits, from the edited document.

    Where it starts is decided by ``idp_common.run_plan.restart.decide_restart``:

    | The edits | Start |
    |---|---|
    | A page's class was reset | ``classification`` |
    | A section has no extraction result (changed, added, or its page text edited) | ``extraction``, for those sections |
    | Otherwise | ``policy_rule_validation`` |

    The start is checked against the document as edited. When the document is
    not ready for it (a page with no class that was not reset, a document that
    never finished), the run starts at the beginning, where each step's own check
    skips what exists.

    ``mode`` is the processing mode the run takes (a ``ProcessingMode``); it is
    resolved here when the caller has not resolved it.
    """
    mode = mode or _processing_mode(document)
    decision = decide_restart(
        document,
        kind=RunKind.SAVED_EDITS,
        mode=mode.mode,
        class_reset=any(page.get("classReset") for page in modified_pages or []),
    )
    answer = check(
        ReprocessRequest(
            object_key=object_key,
            start=decision.start,
            kind=RunKind.SAVED_EDITS,
            sections=decision.sections,
        ),
        EligibilityContext(
            load_document=lambda _key: document,
            mode_for=lambda _profile, _revision: mode,
        ),
    )
    if not answer.ok:
        logger.info(
            "The document is not ready to start at %s (%s); starting at the beginning",
            decision.start,
            answer.code,
        )
        return RunPlan(kind=RunKind.SAVED_EDITS, start=BEGINNING)
    try:
        return RunPlan(
            kind=RunKind.SAVED_EDITS,
            start=answer.start,
            sections=list(answer.sections),
            reviewed=reviewed_for(document, answer.start),
        )
    except ValueError as e:
        # More sections than a plan can name: extract every section, and each
        # one that has its result is skipped by the step's own check.
        logger.info("Not naming the sections in the run plan: %s", e)
        return RunPlan(kind=RunKind.SAVED_EDITS, start=answer.start)


def _planned_run_for(run_plan, mode):
    """The steps of the run ``run_plan`` starts, in the processing mode it takes.

    One derivation for the plan read and for the answer of ``processChanges``, so
    the steps a caller is shown before confirming are the steps the call reports.
    A step the document's configuration has switched off is listed as not enabled;
    with no configuration read, every step is listed as enabled.
    """
    return planned_run(
        run_plan.start,
        mode.mode,
        kind=RunKind.SAVED_EDITS,
        skip=run_plan.skip,
        enabled=step_enabled(mode.config) if mode.config else {},
        sections=run_plan.sections,
    )


#: What a saved regroup tells the holder of a review item, after the section count.
_REGROUPED_IN_REVIEW_MESSAGE = (
    "Sections that are new or whose pages changed are extracted again when the "
    "review is completed."
)


def _caller_key(event):
    """The caller's owner key from verified claims, as a review claim records it."""
    from idp_common.notifications.keys import recipient_key_from_claims

    claims = (event.get("identity") or {}).get("claims") or {}
    try:
        return recipient_key_from_claims(claims)
    except Exception:  # noqa: BLE001 — no identity: matches no holder
        logger.warning("Rejecting regroup: no caller identity")
        return ""


def update_document_sections(event):
    """Re-group a processed document's pages, keeping its extracted values.

    The document-side counterpart to updateTestSetDocumentSections, and deliberately
    NOT processChanges. processChanges clears each changed section's extraction data and
    requeues the document, because it delegates persistence to the pipeline — it does not
    write the document back at all, so the SQS run is the only thing that saves the
    change. Reprocessing is its persistence mechanism, not a consequence of re-grouping.

    A grouping-only edit can persist itself, exactly as complete_section_review does for
    field edits: `document_service.update_document(document)` writes Sections with their
    PageIds and Class. So the extracted values, and any HITL corrections made to them,
    survive. They may no longer match their pages afterwards, which the UI says while
    offering processChanges as the opt-in reprocess.

    Refuses while the document is in flight. That is the one hazard a direct write
    introduces: processChanges is safe to call mid-run because it never writes, whereas
    this would race the pipeline's own save. A document waiting in a review queue is
    not in flight, and is regrouped only by the holder of its review item.

    In a review, the run that follows its completion extracts again the sections the
    regroup added or whose pages it changed. A BDA run cannot, so a document that is
    processed in BDA mode is not regrouped in a review. The drafts of the sections a
    regroup removes are deleted once the grouping is saved, so a section that is later
    given one of their ids starts with no corrections.
    """
    args = event.get("arguments", {})
    object_key = args.get("objectKey")
    incoming = args.get("sections") or []

    if not object_key:
        return {
            "success": False,
            "message": "objectKey is required",
            "processingJobId": None,
        }

    # Before every other check on the arguments, and before the document is read for
    # any purpose other than deciding this. Two reasons it is first: nothing below may
    # run for a caller the object is out of scope for, and each guard below answers a
    # 200 saying the request was malformed — placed after any of them, the
    # authorization check would simply not be reached for the arguments that trip them
    # (`sections: []` is one, and is what the live RBAC matrix drives this operation
    # with).
    _assert_annotator_scope(event, object_key)
    _assert_production_document_in_profile_scope(event, object_key)

    if not incoming:
        return {
            "success": False,
            "message": "A document must have at least one section",
            "processingJobId": None,
        }

    dynamodb_service = create_document_service(mode="dynamodb")
    document = dynamodb_service.get_document(object_key)
    if not document:
        return {
            "success": False,
            "message": f"Document {object_key} not found",
            "processingJobId": None,
        }

    status = str(getattr(document.status, "value", document.status) or "").upper()
    # Regrouping is part of review: a document waiting in a review queue may be
    # regrouped by the holder of its item. Nothing is running on it (phase 1 has ended
    # and the finalize run hasn't started), and the finalize run reprocesses the
    # tracked document, so the new grouping is the one it finishes with.
    in_review = (
        status == Status.HITL_IN_PROGRESS.value
        and normalize_hitl_status(document.hitl_status).in_review
    )
    if in_review:
        refusal = regroup_refusal(_REVIEW_STORE, object_key, _caller_key(event))
        if refusal:
            logger.warning("Rejecting updateDocumentSections on a document in review")
            return {"success": False, "message": refusal, "processingJobId": None}
        # The run after the review extracts a regrouped section again. A BDA run
        # keeps the sections a document has, so the regroup is refused (a 400).
        _assert_not_a_bda_structural_edit(document, incoming, [])
    elif status not in _EDITABLE_STATUSES:
        return {
            "success": False,
            "message": (
                f"{object_key} is currently {status.lower() or 'in an unknown state'}. "
                "Wait for processing to finish before changing its page grouping, or "
                "the change would be overwritten by the run."
            ),
            "processingJobId": None,
        }

    # A partition of the pages we are given: no page in two sections, none empty. The
    # client additionally requires every page of the document to be assigned, which it
    # can check because it has the rendered page list.
    seen = {}
    section_ids = set()
    for section in incoming:
        section_id = str(section.get("sectionId") or "")
        page_ids = section.get("pageIds") or []
        # A repeated id corrupts silently rather than failing. The rebuild below looks
        # each id up in existing_by_id, so two entries sharing one id resolve to the
        # *same* Section object: it is appended twice and the second assignment
        # overwrites page_ids, so the document ends up holding two references to one
        # section and the first group's pages belong to none. The page-level checks
        # cannot catch it — both groups' pages are accounted for in `seen`.
        if section_id in section_ids:
            return {
                "success": False,
                "message": f"Section '{section_id}' appears more than once",
                "processingJobId": None,
            }
        section_ids.add(section_id)
        if not page_ids:
            return {
                "success": False,
                "message": f"Section '{section_id}' has no pages",
                "processingJobId": None,
            }
        for raw in page_ids:
            page_id = str(raw)
            # Refused by name rather than left to fail later. A non-numeric id reaches
            # int() in the sort below and surfaces as a generic caught error, and an id
            # for a page the document does not have was simply accepted into a section
            # and then skipped by the classification loop — a grouping claiming a page
            # that does not exist, saved without complaint.
            if not page_id.isdigit():
                return {
                    "success": False,
                    "message": (
                        f"Section '{section_id}' has an invalid page id {raw!r}; "
                        "expected a page number"
                    ),
                    "processingJobId": None,
                }
            if page_id not in document.pages:
                return {
                    "success": False,
                    "message": (
                        f"Page {page_id} is not in {object_key}, so section "
                        f"'{section_id}' cannot claim it"
                    ),
                    "processingJobId": None,
                }
            if page_id in seen:
                return {
                    "success": False,
                    "message": (
                        f"Page {page_id} is in both section '{seen[page_id]}' and "
                        f"section '{section_id}'"
                    ),
                    "processingJobId": None,
                }
            seen[page_id] = section_id

    previously_grouped = {pid for sec in document.sections for pid in sec.page_ids}
    lost = sorted(previously_grouped - set(seen))
    if lost:
        return {
            "success": False,
            "message": (
                f"Page{'s' if len(lost) > 1 else ''} {', '.join(lost)} would no longer "
                "belong to any section"
            ),
            "processingJobId": None,
        }

    existing_by_id = {section.section_id: section for section in document.sections}
    rebuilt = []
    for section in incoming:
        source_id = str(section.get("sectionId") or "")
        page_ids = [str(pid) for pid in section.get("pageIds") or []]
        classification = section.get("classification")
        carried = existing_by_id.get(source_id)
        if carried is not None:
            # Content follows its pages: the extraction result, attributes and any
            # confidence alerts stay attached to the section that keeps them.
            carried.page_ids = page_ids
            if classification:
                carried.classification = classification
            rebuilt.append(carried)
        else:
            # A section the reviewer added. No extraction result, which is the truth —
            # nothing has run over these pages as a group yet.
            rebuilt.append(
                Section(
                    section_id=source_id,
                    classification=classification or "",
                    page_ids=page_ids,
                )
            )

    # By first page, as processChanges does (:240), so a section's position matches its
    # place in the document — consumers take a group index from list position.
    rebuilt.sort(
        key=lambda sec: min([int(pid) for pid in sec.page_ids] + [float("inf")])
    )
    document.sections = rebuilt

    # Page-level classification follows the section that now owns the page, or a page
    # would keep a class its section no longer has.
    for section in document.sections:
        for page_id in section.page_ids:
            if page_id in document.pages and section.classification:
                document.pages[page_id].classification = section.classification

    # Persisted here, unlike processChanges: nothing else is going to save it, and
    # nothing is running that we could race.
    dynamodb_service.update_document(document)

    saved = f"Page grouping saved as {len(document.sections)} section(s)."
    if in_review:
        # After the row is written, so a regroup that fails discards nothing. The
        # drafts of the sections that are gone go with them: a later regroup may
        # give one of their ids to a new section.
        discard_drafts(
            _REVIEW_STORE,
            object_key,
            set(existing_by_id) - {section.section_id for section in rebuilt},
        )
        message = f"{saved} {_REGROUPED_IN_REVIEW_MESSAGE}"
    else:
        message = (
            f"{saved} Extracted values were kept and may no longer match their pages."
        )

    logger.info(
        f"Re-grouped {object_key} into {len(document.sections)} section(s) without "
        "reprocessing; extracted values preserved"
    )
    return {"success": True, "message": message, "processingJobId": None}


def process_changes_plan(event):
    """Where Process Changes would start for the document as saved, and its steps.

    What ``processChanges`` would queue when it is called with no section or page
    change: the step the run starts at, every step of the run with its state, and
    the sections it extracts again. The start is decided by ``_run_plan_for``, as
    ``processChanges`` decides it.

    A read: it writes no tracking row, deletes no object and queues no message.

    The caller is checked as for ``processChanges``, and before the document is
    read, as for a re-group. A document that does not exist is a
    ``ResourceNotFound`` (404). A document in a review queue is refused (400): it
    is worked from Review Queues, and what finishing its review runs is
    ``getReviewCompletionPlan``'s to say. A document a run is working on, or one
    the malware gate holds, is refused (400) as ``processChanges`` refuses it,
    for every caller: the confirmation shows no plan for a run that cannot be
    queued.
    """
    object_key = (event.get("arguments") or {}).get("objectKey")
    if not object_key:
        raise _refusal("objectKey is required")

    # Before the document is read: nothing about a document is said to a caller
    # it is out of scope for, including whether it exists.
    _assert_annotator_scope(event, object_key)
    _assert_production_document_in_profile_scope(event, object_key)

    document = create_document_service(mode="dynamodb").get_document(object_key)
    if not document:
        raise ResourceNotFound(f"Document {object_key} not found")
    if normalize_hitl_status(document.hitl_status).in_review:
        logger.warning(
            "Rejecting getProcessChangesPlan on a document in a review queue"
        )
        raise _refusal(_QUEUE_MANAGED_MESSAGE)
    _assert_caller_may_process(event, object_key, document)
    _assert_no_other_run(object_key, document)

    mode = _processing_mode(document)
    run_plan = _run_plan_for(object_key, document, [], mode=mode)
    return _planned_run_for(run_plan, mode).model_dump(by_alias=True)


def _assert_operation_group(event, field_name):
    """Refuse a caller who is in none of the groups the operation admits (403).

    Defense-in-depth RBAC. The schema enforces this via
    @aws_cognito_user_pools(cognito_groups), but we also gate it server-side so a
    caller can never reach an operation even if the schema directive is
    missing/misconfigured.
    """
    caller_groups = (
        event.get("identity", {}).get("claims", {}).get("cognito:groups") or []
    )
    if isinstance(caller_groups, str):
        caller_groups = [caller_groups]
    required = _OPERATION_GROUPS.get(field_name, {"Admin", "Reviewer"})
    if not (required.intersection(caller_groups)):
        logger.warning(
            f"Forbidden: caller (groups={caller_groups}) attempted {field_name}"
        )
        # Raise (not return a 200 dict) so the dispatcher maps to 403.
        raise PermissionError(
            f"Unauthorized: {field_name} requires one of "
            f"{' or '.join(sorted(required))} group"
        )


def handler(event, context):
    logger.info(
        f"ProcessChanges resolver invoked with event: {json.dumps(sanitize_event_for_logging(event))}"
    )

    if (event.get("info") or {}).get("fieldName") == "getProcessChangesPlan":
        # Served outside the error handling below, which answers a write that
        # failed with a 200 `success: false` body. This read answers a planned
        # run or an error: a refusal (403 or 400), a missing document (404) or
        # a fault.
        _assert_operation_group(event, "getProcessChangesPlan")
        return process_changes_plan(event)

    # Add comprehensive error handling
    try:
        field_name = (event.get("info") or {}).get("fieldName") or "processChanges"
        _assert_operation_group(event, field_name)

        if field_name == "updateDocumentSections":
            return update_document_sections(event)

        # Extract arguments from the GraphQL event
        args = event.get("arguments", {})
        logger.info(f"Arguments received: {json.dumps(args)}")

        object_key = args.get("objectKey")
        modified_sections = args.get("modifiedSections", [])
        modified_pages = args.get("modifiedPages", [])

        if not object_key:
            logger.error("objectKey is required but not provided")
            return {
                "success": False,
                "message": "objectKey is required",
                "processingJobId": None,
            }

        # Allow empty arrays for evaluation-only reprocessing
        # When both are empty, the document will be resubmitted and the existing
        # Lambda skip logic will bypass OCR/Classification/Extraction/Assessment
        # and proceed directly to Summarization and Evaluation steps
        if not modified_sections and not modified_pages:
            logger.info(
                "No section or page modifications - reprocessing for evaluation/summarization only"
            )

        logger.info(f"Processing changes for document: {object_key}")
        logger.info(f"Modified sections: {json.dumps(modified_sections)}")
        logger.info(f"Modified pages: {json.dumps(modified_pages)}")

        # Use DynamoDB service to get the document (only service that supports get_document)
        try:
            dynamodb_service = create_document_service(mode="dynamodb")
            document = dynamodb_service.get_document(
                object_key
            )  # Returns Document object directly

            if not document:
                raise ValueError(f"Document {object_key} not found")

            # A document waiting in a review queue is worked through the review
            # operations; editing it here would bypass the reviewer's lease (D30).
            # A finished queue document can be edited (a fresh, rerouted run).
            if normalize_hitl_status(document.hitl_status).in_review:
                logger.warning(
                    "Rejecting processChanges on a document in a review queue"
                )
                raise ValueError(_QUEUE_MANAGED_MESSAGE)

            # Before anything below writes: completing the review, clearing
            # extraction results and requeueing are all acted on the caller's
            # behalf.
            _assert_caller_may_process(event, object_key, document)
            _assert_not_a_bda_structural_edit(
                document, modified_sections, modified_pages
            )
            _assert_no_other_run(object_key, document)

            # Set bucket names from environment variables (fix for null bucket issue)
            input_bucket = os.environ.get("INPUT_BUCKET")
            output_bucket = os.environ.get("OUTPUT_BUCKET")
            document.input_bucket = input_bucket
            document.output_bucket = output_bucket
            logger.info(
                f"Set document buckets - input_bucket: {input_bucket}, output_bucket: {output_bucket}"
            )

            logger.info(f"Found document: {document.id}")

            # Mark HITL review as completed when processing changes
            # This handles the case where user edits data and clicks "Process Changes"
            # on a review that is still owed. A finished review — legacy completed or
            # skipped, or a queue review that ended complete or rejected — is not
            # finished again: its outcome and reviewer stay as recorded (#1568).
            # "COMPLETED" is the one finished spelling a real writer stored that the
            # exact-match normaliser does not know: the earlier A2I flow wrote it,
            # and retained rows may still carry it.
            review_finished = (
                normalize_hitl_status(document.hitl_status).kind
                in ("complete", "rejected", "skipped")
                or document.hitl_status == "COMPLETED"
            )
            if document.hitl_status and not review_finished:
                identity = event.get("identity", {})
                username = identity.get("username", "system")
                user_email = identity.get("claims", {}).get("email", "")

                # Mark all pending sections as completed
                pending_sections = document.hitl_sections_pending or []
                completed_sections = list(
                    dict.fromkeys(
                        list(document.hitl_sections_completed or [])
                        + list(pending_sections)
                    )
                )

                document.hitl_status = LegacyHitlStatus.REVIEW_COMPLETED.value
                document.hitl_sections_pending = []
                document.hitl_sections_completed = completed_sections

                # Update review fields in DynamoDB (not in document model)
                # HITLReviewedBy tracks who completed the review via Process Changes.
                # The two section lists are written in this same statement. The
                # status write at the end of the handler also carries them, but it
                # is best-effort and swallows its own failure, and the pipeline
                # cannot carry an empty pending list (`to_dict` omits it), so without
                # them here a failed status write left a completed review still
                # listing its sections as pending.
                tracking_table = os.environ.get("TRACKING_TABLE")
                if tracking_table:
                    dynamodb_resource = boto3.resource("dynamodb")
                    table = dynamodb_resource.Table(tracking_table)
                    table.update_item(
                        Key={"PK": f"doc#{object_key}", "SK": "none"},
                        UpdateExpression=(
                            "SET HITLStatus = :status, HITLReviewedBy = :reviewedBy, "
                            "HITLReviewedByEmail = :reviewedByEmail, "
                            "HITLCompleted = :completed, "
                            "HITLSectionsPending = :pendingSections, "
                            "HITLSectionsCompleted = :completedSections "
                            "REMOVE HITLPendingReview"
                        ),
                        ExpressionAttributeValues={
                            ":status": LegacyHitlStatus.REVIEW_COMPLETED.value,
                            ":reviewedBy": username,
                            ":reviewedByEmail": user_email,
                            ":completed": True,
                            ":pendingSections": [],
                            ":completedSections": completed_sections,
                        },
                    )

                logger.info(
                    f"Marked HITL review as completed by {username} via Process Changes"
                )
            elif document.hitl_status:
                # Already finished. A review completed section by section used to
                # finish without recording who did it; fill that in from this caller,
                # never overwriting a recorded reviewer. The name and email are one
                # record, written together or not at all: a review queue records its
                # reviewer in HITLReviewedBy and writes no HITLReviewedByEmail, and
                # filling only the email would pair that reviewer with this caller's
                # address. Either half recorded means a reviewer is recorded.
                identity = event.get("identity", {})
                tracking_table = os.environ.get("TRACKING_TABLE")
                if tracking_table:
                    table = boto3.resource("dynamodb").Table(tracking_table)
                    try:
                        table.update_item(
                            Key={"PK": f"doc#{object_key}", "SK": "none"},
                            UpdateExpression=(
                                "SET HITLReviewedBy = :reviewedBy, "
                                "HITLReviewedByEmail = :reviewedByEmail"
                            ),
                            ConditionExpression=(
                                "attribute_not_exists(HITLReviewedBy) AND "
                                "attribute_not_exists(HITLReviewedByEmail)"
                            ),
                            ExpressionAttributeValues={
                                ":reviewedBy": identity.get("username", "system"),
                                ":reviewedByEmail": identity.get("claims", {}).get(
                                    "email", ""
                                ),
                            },
                        )
                    except table.meta.client.exceptions.ConditionalCheckFailedException:
                        pass  # a reviewer is already recorded

        except PermissionError:
            raise
        except ValueError as e:
            if _is_refusal(e):
                raise
            logger.error(f"Error retrieving document {object_key}: {str(e)}")
            raise ValueError(
                f"Document {object_key} not found or error retrieving: {str(e)}"
            )
        except Exception as e:
            logger.error(f"Error retrieving document {object_key}: {str(e)}")
            raise ValueError(
                f"Document {object_key} not found or error retrieving: {str(e)}"
            )

        # Track modified section IDs for selective processing
        modified_section_ids = []

        # Process page-level modifications first (if any)
        if modified_pages:
            process_page_changes(document, modified_pages, modified_section_ids)

        # Process each section modification
        for modified_section in modified_sections:
            section_id = modified_section["sectionId"]
            classification = modified_section["classification"]
            page_ids = [
                int(pid) for pid in modified_section["pageIds"]
            ]  # Ensure integer page IDs
            is_new = modified_section.get("isNew", False)
            is_deleted = modified_section.get("isDeleted", False)

            if is_deleted:
                # Find section to delete BEFORE removing it
                section_to_delete = None
                for s in document.sections:
                    if s.section_id == section_id:
                        section_to_delete = s
                        break

                if section_to_delete:
                    # Clear S3 extraction data before removing section
                    if section_to_delete.extraction_result_uri:
                        clear_extraction_data(section_to_delete.extraction_result_uri)
                        logger.info(
                            f"Cleared extraction data for deleted section: {section_id}"
                        )

                    # Remove section from document
                    document.sections = [
                        s for s in document.sections if s.section_id != section_id
                    ]
                    logger.info(f"Deleted section: {section_id}")
                else:
                    logger.warning(
                        f"Section {section_id} marked for deletion but not found"
                    )

                continue

            elif is_new:
                # Create new section (don't search for existing)
                logger.info(f"Creating new section: {section_id}")
                new_section = Section(
                    section_id=section_id,
                    classification=classification,
                    confidence=1.0,
                    page_ids=[str(pid) for pid in page_ids],
                    extraction_result_uri=None,
                    attributes=None,
                    confidence_threshold_alerts=[],
                )
                document.sections.append(new_section)

            else:
                # Update existing section
                existing_section = None
                for section in document.sections:
                    if section.section_id == section_id:
                        existing_section = section
                        break

                if existing_section:
                    logger.info(f"Updating existing section: {section_id}")
                    existing_section.classification = classification
                    existing_section.page_ids = [str(pid) for pid in page_ids]
                    # The class is now the operator's assertion, not the model's
                    # prediction, so the model's score for the class it replaced
                    # no longer describes this section. 1.0 follows the same
                    # convention as the other deterministic assertions (a
                    # document-name regex match, a single-class configuration).
                    existing_section.confidence = 1.0

                    # Clear extraction data for reprocessing
                    if existing_section.extraction_result_uri:
                        clear_extraction_data(existing_section.extraction_result_uri)
                        existing_section.extraction_result_uri = None
                        existing_section.attributes = None

                    # Clear confidence threshold alerts for modified sections
                    existing_section.confidence_threshold_alerts = []
                    logger.info(
                        f"Cleared confidence alerts for modified section: {section_id}"
                    )
                else:
                    logger.warning(
                        f"Section {section_id} marked as update but not found - treating as new"
                    )
                    # Treat as new section if not found
                    new_section = Section(
                        section_id=section_id,
                        classification=classification,
                        confidence=1.0,
                        page_ids=[str(pid) for pid in page_ids],
                        extraction_result_uri=None,
                        attributes=None,
                        confidence_threshold_alerts=[],
                    )
                    document.sections.append(new_section)

            # Only add to modified list if not deleted
            modified_section_ids.append(section_id)

            # Update page classifications to match section classification (only if not deleted)
            for page_id in page_ids:
                page_id_str = str(page_id)
                if page_id_str in document.pages:
                    document.pages[page_id_str].classification = classification
                    # Operator-asserted class: score it 1.0 like the section
                    # above, and DROP the model's reason — it argues for the
                    # class that was just replaced, so keeping it would show a
                    # justification for "invoice" on a page relabelled "receipt".
                    document.pages[page_id_str].confidence = 1.0
                    document.pages[page_id_str].classification_reason = None
                    logger.info(
                        f"Updated page {page_id} classification to {classification}"
                    )

        # Sort sections by starting page ID
        document.sections.sort(
            key=lambda s: min([int(pid) for pid in s.page_ids] + [float("inf")])
        )

        # Where the run starts, decided on the document as edited and before its
        # status is reset. The steps of that run are returned with the answer.
        mode = _processing_mode(document)
        run_plan = _run_plan_for(object_key, document, modified_pages, mode=mode)
        planned = _planned_run_for(run_plan, mode)
        logger.info(
            f"Run plan: start={run_plan.start} sections={run_plan.sections} "
            f"reviewed={run_plan.reviewed}"
        )

        # Update document status and timing - reset for reprocessing
        current_time = datetime.now(timezone.utc).isoformat()
        document.status = Status.QUEUED
        document.initial_event_time = document.queued_time or current_time
        document.start_time = None
        document.completion_time = None
        document.workflow_execution_arn = None

        # What the run produces again is cleared before it is queued. The tracking
        # row attributes that held it are removed with the status write below.
        cleared_row_attributes = ()
        if run_plan.start == POLICY_RULE_VALIDATION:
            # The run starts here, so its result, the summary and the evaluation
            # are all produced again.
            cleared_row_attributes = clear_for_start(
                document,
                POLICY_RULE_VALIDATION,
                s3=s3_client,
                output_bucket=document.output_bucket,
            )
            logger.info("Cleared the results the run produces again")
        elif modified_sections or modified_pages:
            # Resets the result on the document and deletes its files in S3.
            clear_policy_rule_validation(
                document, s3=s3_client, output_bucket=document.output_bucket
            )
            cleared_row_attributes = ROW_POLICY_RULE_VALIDATION
            logger.info("Cleared rule validation data for reprocessing")

        logger.info(
            f"Document updated with {len(document.sections)} sections and {len(document.pages)} pages"
        )

        # Log uncompressed document for troubleshooting
        uncompressed_document_json = json.dumps(document.to_dict(), default=str)
        logger.info(
            f"Uncompressed document (size: {len(uncompressed_document_json)} chars): {uncompressed_document_json}"
        )

        # NOTE: We intentionally do NOT write the document back to the database here.
        # The processing pipeline will handle document updates via AppSync as it processes.
        # This avoids race conditions and ensures consistent state management.

        # The document is stored in the working bucket, with a reference in the
        # message, for large document optimization.
        working_bucket = os.environ.get("WORKING_BUCKET") or None
        if not working_bucket:
            logger.warning(
                "No WORKING_BUCKET configured, sending uncompressed document"
            )
        logger.info(f"Modified sections will be reprocessed: {modified_section_ids}")

        if QUEUE_URL:
            processing_job_id = send_run(
                sqs_client,
                QUEUE_URL,
                document=document,
                plan=run_plan,
                event_type=EVENT_DOCUMENT_RUN_REQUESTED,
                working_bucket=working_bucket,
                prefix="process_changes",
            )
            logger.info(f"Sent document to SQS queue. MessageId: {processing_job_id}")
        else:
            logger.warning("QUEUE_URL not configured, skipping SQS message")
            processing_job_id = None

        # Update document status for immediate UI feedback
        try:
            status_service = create_document_service(mode="dynamodb")
            document.status = Status.QUEUED  # Ensure status is QUEUED for UI
            status_service.update_document(document)
            if cleared_row_attributes:
                # update_document sets attributes and removes none.
                status_service.remove_attributes(object_key, cleared_row_attributes)
            logger.info("Updated document status to QUEUED for immediate UI feedback")
        except Exception as e:
            logger.warning(f"Failed to update document status: {str(e)}")
            # Don't fail the entire operation if the status update fails

        # Log successful completion
        logger.info(
            f"Successfully processed changes for {len(modified_sections)} sections"
        )

        response = {
            "success": True,
            "message": f"Successfully processed changes for {len(modified_sections)} sections",
            "processingJobId": processing_job_id,
            # The run that was queued, as getProcessChangesPlan lists it.
            "startStep": planned.start_step,
            "steps": [step.model_dump(by_alias=True) for step in planned.steps],
        }

        logger.info(f"Returning response: {json.dumps(response)}")
        return response

    except PermissionError:
        # RBAC denial must reach the dispatcher as 403; do not swallow into 200.
        raise
    except ValueError as e:
        if _is_refusal(e):
            # A refusal, not a processing error: 400 through the dispatcher.
            raise
        logger.error(f"Error processing changes: {str(e)}", exc_info=True)
        return {
            "success": False,
            "message": f"Error processing changes: {str(e)}",
            "processingJobId": None,
        }
    except Exception as e:
        logger.error(f"Error processing changes: {str(e)}", exc_info=True)
        # Returned as a literal so the failed shape is visible where it is returned:
        # a scope lookup that cannot be answered is turned into a refusal before it
        # reaches here (`_assert_caller_may_process`), and this arm must not read as
        # a success-shaped answer either way.
        return {
            "success": False,
            "message": f"Error processing changes: {str(e)}",
            "processingJobId": None,
        }


def process_page_changes(document, modified_pages, modified_section_ids):
    """
    Process page-level modifications

    Args:
        document: Document object to modify
        modified_pages: List of modified page dictionaries
        modified_section_ids: List to track which sections need reprocessing
    """
    for modified_page in modified_pages:
        page_id = modified_page["pageId"]
        page_id_str = str(page_id)
        text_modified = modified_page.get("textModified", False)
        class_reset = modified_page.get("classReset", False)
        new_text_uri = modified_page.get("newTextUri")
        new_confidence_uri = modified_page.get("newConfidenceUri")

        logger.info(
            f"Processing page {page_id}: textModified={text_modified}, classReset={class_reset}"
        )

        # Check if page exists in document
        if page_id_str not in document.pages:
            logger.warning(f"Page {page_id} not found in document, skipping")
            continue

        page = document.pages[page_id_str]

        # Handle class reset - removes sections containing this page
        if class_reset:
            logger.info(f"Resetting classification for page {page_id}")
            page.classification = None  # Reset to unclassified
            # There is no class left, so its confidence and the model's reasoning
            # for it must go too — otherwise an unclassified page keeps a score
            # and an argument for a class it no longer has.
            page.confidence = None
            page.classification_reason = None

            # Find and remove all sections containing this page
            sections_to_remove = []
            for section in document.sections:
                if page_id_str in section.page_ids:
                    sections_to_remove.append(section)
                    logger.info(
                        f"Marking section {section.section_id} for removal (contains page {page_id})"
                    )

            # Clear extraction data and remove sections
            for section in sections_to_remove:
                if section.extraction_result_uri:
                    clear_extraction_data(section.extraction_result_uri)
                document.sections = [
                    s for s in document.sections if s.section_id != section.section_id
                ]
                logger.info(
                    f"Removed section {section.section_id} due to page {page_id} class reset"
                )

        # Handle text modification - clears extraction results but keeps sections
        elif text_modified:
            logger.info(f"Text modified for page {page_id}")

            # Update page URIs if provided
            if new_text_uri:
                page.text_uri = new_text_uri
                logger.info(f"Updated text URI for page {page_id}: {new_text_uri}")

            if new_confidence_uri:
                page.text_confidence_uri = new_confidence_uri
                logger.info(
                    f"Updated confidence URI for page {page_id}: {new_confidence_uri}"
                )

            # Find sections containing this page and clear their extraction results
            for section in document.sections:
                if page_id_str in section.page_ids:
                    if section.extraction_result_uri:
                        clear_extraction_data(section.extraction_result_uri)
                        section.extraction_result_uri = None
                        section.attributes = None
                        logger.info(
                            f"Cleared extraction results for section {section.section_id} (page {page_id} text modified)"
                        )

                    # Track section for reprocessing
                    if section.section_id not in modified_section_ids:
                        modified_section_ids.append(section.section_id)


def clear_extraction_data(s3_uri):
    """Clear extraction data from S3"""
    try:
        if not s3_uri or not s3_uri.startswith("s3://"):
            return

        # Parse S3 URI
        parts = s3_uri.replace("s3://", "").split("/", 1)
        if len(parts) != 2:
            return

        bucket, key = parts

        # Delete the object
        s3_client.delete_object(Bucket=bucket, Key=key)
        logger.info(f"Cleared extraction data: {s3_uri}")

    except Exception as e:
        logger.warning(f"Failed to clear extraction data {s3_uri}: {str(e)}")
