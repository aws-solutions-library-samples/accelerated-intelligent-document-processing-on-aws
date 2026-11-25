# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os

import boto3
from pydantic import ValidationError

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config_scope import (
    ScopeLookupError,
    caller_email_from_claims,
    caller_sub_from_claims,
    resolve_allowed_config_versions,
    scope_allows,
)
from idp_common.docs_service import create_document_service
from idp_common.mcp_access import Caller
from idp_common.run_plan.reprocess import QUEUED, ReprocessOrigin, ReprocessService
from idp_common.run_plan.steps import BEGINNING
from idp_common.testset_scope import is_direct_invoke
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
sqs_client = boto3.client("sqs")
s3_client = boto3.client("s3")
cloudwatch = boto3.client("cloudwatch")
# Namespace for the metrics a reprocess publishes: ``StaleOutputPurgeFailed`` on
# a partial-purge failure (parity with queue_sender's #719 metric) and the
# prefix-mapping counts. Set to ``AWS::StackName`` by the template — falls back
# to ``IDP`` for local test contexts where the env var isn't wired.
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "IDP")
_dynamodb = boto3.resource("dynamodb")

# Review queues: reprocessing withdraws the document's review (claim released,
# status object deleted), which ``ReprocessService`` does through this service.
# Built on first use; None when no table is configured.
_REVIEW_SERVICE_CACHE: dict = {}


def _review_service():
    if "svc" not in _REVIEW_SERVICE_CACHE:
        if os.environ.get("HITL_TABLE_NAME"):
            from idp_common.review_queues.service import create_review_queue_service

            _REVIEW_SERVICE_CACHE["svc"] = create_review_queue_service()
        else:
            _REVIEW_SERVICE_CACHE["svc"] = None
    return _REVIEW_SERVICE_CACHE["svc"]


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
#     row accordingly. See `ReprocessService._version_for_document`.
#
# Both directions fail closed. A document whose `ConfigVersion` cannot be read — no
# tracking row, an unstamped row, or a failed GetItem — is refused, because a document
# with no profile name cannot be proven in scope. That is the same rule the list
# resolvers apply, so a scoped caller cannot see such a document to reprocess it.
# Admins and unscoped callers are unaffected in either direction.
#
# This resolver authorises the caller and hands the scope it resolved to
# `idp_common.run_plan.reprocess.ReprocessService`, which does the reprocess and
# applies the same two refusals to what it reads itself.
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
    # The immutable Cognito sub, the config-version scope lookup's PREFERRED key.
    # It is not a fallback for the email: the two go to disjoint key spaces on the
    # UsersTable. See caller_sub_from_claims.
    caller_sub = caller_sub_from_claims(claims)
    return {
        "email": email,
        "sub": caller_sub,
        "username": username,
        "groups": groups,
        "is_admin": "Admin" in groups,
    }


def _get_user_allowed_config_versions(caller_email, caller_sub=""):
    """The caller's `allowedConfigVersions`, or None for an unrestricted caller.

    Thin wrapper over the shared fail-closed lookup so every consumer of this
    rule resolves it identically. Raises `ScopeLookupError` when the scope cannot
    be evaluated; the caller must deny rather than proceed unrestricted.
    """
    return resolve_allowed_config_versions(
        caller_email,
        caller_sub=caller_sub,
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
        return _get_user_allowed_config_versions(caller["email"], caller.get("sub", ""))
    except ScopeLookupError as e:
        logger.error(
            "Denying reprocessDocument: config-version scope could not be resolved: %s",
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
    """
    if not allowed_versions:
        return
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


def _emit(metric_name, dimensions=None):
    """Fire-and-forget telemetry, into the ROOT stack's namespace.

    ``ReprocessService`` publishes its counts through this function. The reprocess
    path sends straight to the document queue, so ``queue_sender`` never runs for
    it and the prefix-mapping alarms would otherwise cover one entry point of two
    — the same reason ``StaleOutputPurgeFailed`` has two emitters.

    ⚠️ **The dimension schema has to match ``queue_sender``'s, not merely the
    namespace.** A dimensionless datum is a *different metric* from one carrying
    ``Prefix``, so the dashboard's ``SEARCH('{<ns>,Prefix} …')`` filter does not
    match it and a reprocess that picked up a mapping would never appear on the
    graph — covering one entry point of two, which is the asymmetry this function
    exists to prevent. The alarms are unaffected either way (none is on
    ``PrefixMappingApplied``), so the graph is the thing to get right here.
    Truncated to CloudWatch's 255-character dimension-value limit, as S3 keys
    exceed it and ``put_metric_data`` would raise.
    """
    try:
        datum = {"MetricName": metric_name, "Value": 1, "Unit": "Count"}
        if dimensions:
            datum["Dimensions"] = [
                {"Name": name, "Value": str(value)[:255]}
                for name, value in dimensions.items()
            ]
        cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=[datum])
    except Exception:
        pass  # telemetry must not affect a reprocess


# Per-container cache, keyed on the table name so a changed environment is not
# served a stale client. Matches `queue_sender`'s `_CONFIG_CLIENTS` and the upload
# resolver's caches, which is the point: this resolver sits behind API Gateway's
# 29-second ceiling -- the same reason the service memoizes its reads -- and three
# paths answering one question should not differ in whether they rebuild the
# client. Called once per request rather than per document, so this is latency
# across requests rather than per-document cost.
_CONFIG_MANAGERS: dict = {}


def _configuration_manager():
    """A ConfigurationManager over this deployment's table, or ``None``."""
    table_name = os.environ.get("CONFIGURATION_TABLE_NAME")
    if not table_name:
        return None
    if table_name not in _CONFIG_MANAGERS:
        _CONFIG_MANAGERS[table_name] = ConfigurationManager(table_name=table_name)
    return _CONFIG_MANAGERS[table_name]


# Initialize document service (same as queue_sender - defaults to AppSync)
document_service = create_document_service()

# Environment variables
queue_url = os.environ.get("QUEUE_URL")
input_bucket = os.environ.get("INPUT_BUCKET")
output_bucket = os.environ.get("OUTPUT_BUCKET")
retentionDays = int(os.environ.get("DATA_RETENTION_IN_DAYS", "365"))


def _service():
    """The reprocess service over this deployment's resources.

    Built per request from the module's clients and settings; building it reads
    nothing.
    """
    return ReprocessService(
        document_service=document_service,
        sqs_client=sqs_client,
        queue_url=queue_url,
        s3=s3_client,
        input_bucket=input_bucket,
        output_bucket=output_bucket,
        working_bucket=os.environ.get("WORKING_BUCKET") or None,
        config_manager_factory=_configuration_manager,
        review_service_factory=_review_service,
        metrics=_emit,
        retention_days=retentionDays,
    )


#: The groups that may reprocess a document.
_REPROCESS_GROUPS = frozenset({"Admin", "Author"})

_REPROCESS = "reprocessDocument"
_OPTIONS = "getReprocessOptions"


def _allowed_versions(event, field):
    """Who is asking, and the configuration scope the request must stay inside.

    Returns the scope: ``None`` for an unrestricted caller. Three kinds of caller
    reach this function:

    * **The REST route.** The dispatcher sends the identity it verified. The
      caller must be in the Admin or Author group, and a scoped Author's
      ``allowedConfigVersions`` is the scope. Fails closed when the scope cannot
      be read.
    * **A direct invoke.** No identity at all: the SDK, the CLI, and the review
      queues resolver sending a document back. It is admitted by IAM on this
      function's ARN, so it is trusted and unscoped, as the abort workflow
      resolver treats a direct invoke.
    * **A direct invoke for someone.** The MCP handler adds ``actingCaller``, the
      principal it resolved. That can only narrow what a direct invoke may do:
      the principal must hold the Admin or Author role, and its scope applies.

    ``actingCaller`` is read only when there is no identity. On the REST route a
    caller could put anything in the request, and it must not replace the
    identity the dispatcher verified.
    """
    if not is_direct_invoke(event):
        caller = _get_caller_info(event)
        if not _REPROCESS_GROUPS.intersection(caller["groups"]):
            logger.warning(
                "Forbidden: caller %s (groups=%s) attempted %s",
                caller["email"],
                caller["groups"],
                field,
            )
            raise PermissionError(
                f"Unauthorized: {field} requires Admin or Author group"
            )
        # An Author whose `allowedConfigVersions` restricts them to a subset of
        # profiles must not reprocess outside it, in either direction. An unset
        # scope is unrestricted, which is the opt-in default for single-user and
        # pre-RBAC deployments. See the note at the top of this module.
        return _caller_scope_or_deny(caller)

    acting = event.get("actingCaller")
    if acting is None:
        return None
    if not isinstance(acting, dict):
        logger.warning("Denied %s: the acting caller is not readable", field)
        raise PermissionError(f"Unauthorized: {field} requires Admin or Author role")
    caller = Caller.from_payload(acting)
    if not _REPROCESS_GROUPS.intersection(caller.roles):
        logger.warning(
            "Denied %s: the acting caller (roles=%s) is not an Admin or Author",
            field,
            caller.roles,
        )
        raise PermissionError(f"Unauthorized: {field} requires Admin or Author role")
    return caller.scope


def _origin(event, args):
    """Why the run is asked for, when a trusted caller says so.

    Only a direct invoke may name an origin: the review queues resolver does,
    for a send-back it has authorised. On the REST route the argument is
    ignored, so a caller cannot record a reprocess as someone's send-back.
    """
    raw = args.get("origin")
    if raw is None or not is_direct_invoke(event):
        return None
    if not isinstance(raw, dict):
        raise ValueError("BAD_ARGUMENT: origin must be an object")
    try:
        return ReprocessOrigin(
            kind=raw.get("kind"),
            actor=raw.get("actor") or "",
            cycle_id=raw.get("cycleId") or "",
            note=raw.get("note") or "",
        )
    except ValidationError:
        raise ValueError("BAD_ARGUMENT: origin is not a known kind of run") from None


def _result(outcome):
    """One document's outcome, in the API's shape (``ReprocessDocumentResult``)."""
    return {
        "objectKey": outcome.object_key,
        "status": outcome.status,
        "startStep": outcome.start_step or None,
        "reason": outcome.reason or None,
        "message": outcome.message or None,
    }


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

        field = (event.get("info") or {}).get("fieldName") or _REPROCESS
        if field not in (_REPROCESS, _OPTIONS):
            raise ValueError(f"BAD_ARGUMENT: unsupported field '{field}'")

        # Defense-in-depth RBAC: both operations are Admin+Author. The schema
        # enforces this via @aws_cognito_user_pools(cognito_groups), but it is
        # also gated here so a Viewer can never reach it even if the schema
        # directive is missing or misconfigured. Both checks fail closed.
        allowed_versions = _allowed_versions(event, field)

        args = event.get("arguments") or {}
        object_keys = args.get("objectKeys") or []
        version = args.get("version")  # Optional version parameter
        revision = args.get("revision")  # Optional revision of that version

        # (a) the profile the documents would be re-run UNDER, when one is named.
        if version and not scope_allows(allowed_versions, version):
            logger.warning(
                "Rejecting %s: caller is scoped to %s but requested version=%r",
                field,
                sorted(allowed_versions),
                version,
            )
            # Raised so the dispatcher answers 403 rather than a 200 with a body.
            raise PermissionError(
                f"Access denied: version '{version}' is not in your allowed scope"
            )

        # (b) the profile each document was last processed under. Independent of
        #     (a): omitting `version` must not stand the check down.
        _enforce_document_scope(allowed_versions, object_keys)

        if field == _OPTIONS:
            if not object_keys:
                return []
            return _service().options(
                object_keys,
                version=version,
                revision=revision,
                allowed_versions=allowed_versions,
            )

        if not object_keys:
            logger.error("objectKeys is required but not provided")
            return {"queued": 0, "results": []}

        logger.info(
            f"Reprocessing {len(object_keys)} documents"
            + (f" with version: {version}" if version else "")
        )

        # The service checks every document and decides every configuration before
        # it queues any. A refusal that is about the REQUEST (a prefix mapping set
        # to refuse a conflicting profile, a profile outside the caller's scope)
        # raises, so the batch is refused whole, as `_enforce_document_scope` does
        # above. A document that cannot run again, or whose send fails, comes back
        # as its own result and does not stop the others.
        outcomes = _service().reprocess(
            object_keys,
            start=args.get("startStep") or BEGINNING,
            skip=args.get("skipSteps") or (),
            version=version,
            revision=revision,
            allowed_versions=allowed_versions,
            origin=_origin(event, args),
        )

        queued = sum(1 for outcome in outcomes if outcome.status == QUEUED)
        logger.info(
            f"Successfully queued {queued}/{len(object_keys)} documents for reprocessing"
        )
        return {"queued": queued, "results": [_result(o) for o in outcomes]}

    except Exception as e:
        logger.error(f"Error in reprocess handler: {str(e)}", exc_info=True)
        raise e
