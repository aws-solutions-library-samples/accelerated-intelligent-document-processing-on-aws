# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3

from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.prefix_mappings import (
    SOURCE_ACTIVE_PROFILE,
    SOURCE_DOCUMENT_PIN,
    SOURCE_EXPLICIT_REQUEST,
    SOURCE_PREFIX_MAPPING,
    PrefixMappingStore,
    resolve_config_assignment,
)
from idp_common.config_scope import (
    ScopeLookupError,
    caller_email_from_claims,
    caller_sub_from_claims,
    resolve_allowed_config_versions,
    scope_allows,
)
from idp_common.docs_service import create_document_service
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


def _prefix_mappings():
    """The deployment's config prefix mappings, or ``[]`` if unavailable.

    Fails open, matching ``queue_sender``: reprocessing a document under the profile
    it already has, or under the active one, is a far better outcome than refusing
    the reprocess because a *routing* table could not be read.
    """
    table_name = os.environ.get("CONFIGURATION_TABLE_NAME")
    if not table_name:
        # Same signal as a read failure, for the same reason: the two branches
        # produce the identical outcome (no mapping applies) and only one of them
        # was visible. Lower stakes than the upload path, since a reprocess resolves
        # no presigned POST, but an unexplained "mappings stopped applying on
        # re-runs" is the kind of report that costs a day.
        logger.error(
            "CONFIGURATION_TABLE_NAME is not set, so no prefix mapping can be "
            "applied on reprocess"
        )
        _emit("PrefixMappingLookupFailed")
        return []
    try:
        return PrefixMappingStore(_dynamodb.Table(table_name)).list()
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Could not read the configuration prefix mappings (%s); reprocessing "
            "without them",
            e,
        )
        _emit("PrefixMappingLookupFailed")
        return []


def _emit(metric_name, dimensions=None):
    """Fire-and-forget telemetry, into the ROOT stack's namespace.

    The reprocess path sends straight to the document queue, so ``queue_sender``
    never runs for it and the prefix-mapping alarms would otherwise cover one entry
    point of two — the same reason ``StaleOutputPurgeFailed`` has two emitters.

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
# 29-second ceiling -- the same reason the seams below are memoized -- and three
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


def _version_for_document(
    requested_version,
    requested_revision,
    object_key,
    current_versions,
    allowed_versions=None,
    mappings=None,
    manager=None,
    seam_cache=None,
):
    """The configuration to reprocess one document under, and where it came from.

    Returns ``(version, revision, source, mapping_prefix)``.

    The order is a precedence chain and each step is there for a different reason:

    1. **An explicit ``version`` argument is adjudicated by the mapping, not
       exempt from it.** It is a deliberate choice by an authenticated caller and it
       has already been scope-checked -- but so is a profile named in upload
       metadata, and a mapping's conflict mode is precisely the rule for what
       happens when the two disagree. Returning the request unconditionally made
       ``mapping`` precedence mean "the mapping wins, except on reprocess" and made
       ``reject`` mean "refuse the upload, then accept the same conflict on a
       re-run" -- so an operator could defeat an administrator's routing policy by
       uploading, letting it be refused or overridden, and reprocessing with the
       profile they wanted. The request therefore goes through
       ``resolve_config_assignment`` as the metadata profile, which applies the same
       three modes the upload path applies, including refusing the reprocess
       outright under ``reject``.
    2. **A scoped caller's reprocess stays pinned to the document's own profile.**
       ``_enforce_document_scope`` has just verified that profile is in their scope.
       ⚠️ This step must come *before* the prefix mapping, and the reason is easy to
       get backwards: the pin exists for exactly the case "no explicit profile was
       requested", which is also the case a mapping would like to fill. Consulting
       the mapping first would let a reprocess move a scoped caller's own document
       out of their scope — reintroducing the bug this module's header documents,
       through a feature that looks unrelated to it.
    3. **Then a prefix mapping**, so that moving a mapping and reprocessing is how an
       operator re-runs a backlog under a new profile. Only reachable for an
       unscoped caller or an Admin, and the resolved profile is scope-checked anyway
       so the rule holds even if (2) ever stops producing a pin.
    4. **Otherwise unpinned**, and ``queue_processor`` resolves the active profile —
       previous behaviour, unchanged.

    ⚠️ **The revision travels with the profile it belongs to.** Revision numbers are
    per profile, so a caller's ``revision`` argument means nothing against a profile
    the caller did not name. Passing one through alongside a mapping-supplied profile
    would stamp, say, r5 of profile A onto profile B — the hazard
    ``resolve_config_assignment`` guards against internally, reintroduced at the call
    site. So a mapping supplies both or neither, and ``revision`` is honoured only
    where ``version`` came from the caller or from the document's own pin.
    """
    if requested_version:
        # No mapping set, or no mapping governs this key: the request stands, which
        # is the previous behaviour and the overwhelmingly common case.
        if not mappings:
            return requested_version, requested_revision, SOURCE_EXPLICIT_REQUEST, None

        seam_cache = {} if seam_cache is None else seam_cache
        decided = resolve_config_assignment(
            object_key,
            # The caller's request takes the metadata slot, because it is the same
            # kind of claim -- "process this under X" from whoever initiated it --
            # and `metadataPrecedence` is the administrator's rule for adjudicating
            # it against the prefix.
            metadata_profile=requested_version,
            metadata_revision=requested_revision,
            mappings=mappings,
            active_profile=_memo_active(manager, seam_cache),
            published_revision=_memo_published(manager, seam_cache),
            profile_exists=_memo_exists(manager, seam_cache),
            allowed_profiles=list(allowed_versions) if allowed_versions else None,
        )
        if decided.rejected:
            _emit("PrefixMappingRejected")
            logger.warning("Refusing to reprocess %s: %s", object_key, decided.reason)
            # Reworded rather than passed through. The resolver's own reason is
            # written for the ingest path and says the object "carries conflicting
            # upload METADATA" -- true there, and wrong here, where the conflicting
            # profile came from this request rather than from the object. An
            # operator reading it would go looking at the object's metadata for a
            # value that is not on it.
            #
            # The PREFIX, not the profile: a rejected assignment deliberately
            # carries no profile, and the prefix is both available and the
            # actionable half -- it is what the admin edits to change the rule.
            raise ValueError(
                f"Refused: {object_key!r} is governed by the configuration prefix "
                f"mapping for {decided.mapping_prefix!r}, which is set to refuse a "
                f"conflicting profile rather than choose between them. Reprocess "
                f"without naming a profile to use the mapping's own, or change that "
                f"mapping's conflict mode."
            )
        if decided.scope_denied:
            # Reachable now that the request is adjudicated: a mapping may resolve a
            # profile outside the caller's scope even though their own request was
            # in scope. Refusing is the same answer the upload path gives.
            logger.warning(
                "Refusing to reprocess %s: the resolved Configuration Profile is "
                "outside the caller's configuration scope",
                object_key,
            )
            raise PermissionError(
                "Unauthorized: that document's destination is governed by a "
                "Configuration Profile outside your allowed configuration scope."
            )
        # ⚠️ Emission conditions are `queue_sender`'s, verbatim, and must stay
        # that way. Both emitters feed one dashboard expression
        # (`SEARCH('{<ns>,Prefix} ...')`), so a metric that means "matched the key"
        # at ingest and "won the contest" here makes a prefix under `metadata`
        # precedence show ingest hits and no reprocess hits -- which reads as "that
        # prefix is not used on the reprocess path" rather than as two definitions.
        # The CHANGELOG states the ingest meaning as the definition.
        #
        # `Applied` therefore fires whenever a mapping MATCHED, whoever won, and
        # `Conflict` only on an actual disagreement -- an explicit request naming
        # the profile the mapping assigns is agreement, not a conflict, which is
        # the whole point of the resolver's agreement rule and would otherwise make
        # this metric fire on the harmless case.
        if decided.unresolvable:
            # Same marker string the other two emitters log, because
            # PrefixMappingUnresolvableAlarm sends an operator looking for it and
            # this path would otherwise be a hole in the alarm's claim.
            logger.warning(
                "Stale prefix mapping for %s: %s", object_key, decided.reason
            )
            _emit("PrefixMappingUnresolvable")
        if decided.mapping_prefix:
            _emit("PrefixMappingApplied", {"Prefix": decided.mapping_prefix})
        if decided.conflict:
            _emit("PrefixMappingConflict", {"Winner": decided.source})
            logger.warning(
                "Configuration conflict for %s: %s", object_key, decided.reason
            )

        if decided.mapping_prefix and decided.source == SOURCE_PREFIX_MAPPING:
            # The mapping won, so it supplies profile AND revision -- revision
            # numbers are per profile, so the caller's cannot travel.
            logger.info("Reprocess of %s: %s", object_key, decided.reason)
            return (
                decided.profile,
                decided.revision,
                SOURCE_PREFIX_MAPPING,
                decided.mapping_prefix,
            )
        return (
            requested_version,
            requested_revision,
            SOURCE_EXPLICIT_REQUEST,
            decided.mapping_prefix,
        )

    pinned = current_versions.get(object_key) or None
    if pinned:
        # The caller named no profile, so a `revision` argument is theirs to apply to
        # the document's existing one -- the pair the UI's reprocess dialog sends.
        return pinned, requested_revision, SOURCE_DOCUMENT_PIN, None

    if not mappings:
        # `active-profile`, not an empty source. The profile is left None so the
        # queue processor resolves whatever is active when the document is picked
        # up, but the PROVENANCE is known now and is the whole point of the field:
        # a blank ConfigSource is indistinguishable from a document written before
        # the field existed, so "why did this reprocess under lending r7?" stops
        # being answerable from the tracking row for exactly the ordinary case.
        return None, requested_revision, SOURCE_ACTIVE_PROFILE, None

    seam_cache = {} if seam_cache is None else seam_cache
    assignment = resolve_config_assignment(
        object_key,
        mappings=mappings,
        active_profile=_memo_active(manager, seam_cache),
        published_revision=_memo_published(manager, seam_cache),
        profile_exists=_memo_exists(manager, seam_cache),
        # Scope is evaluated on the RESOLVED profile. For an unscoped caller
        # `allowed_versions` is empty, which `scope_allows` reads as unrestricted.
        allowed_profiles=list(allowed_versions) if allowed_versions else None,
    )
    if assignment.unresolvable:
        # Logged as well as emitted, with the same marker queue_sender uses. The
        # alarm description sends an operator looking for this string, and the
        # metric has two emitters -- an alarm fired by a reprocess with no log
        # line on this path sends them to the wrong Lambda.
        logger.warning("Stale prefix mapping for %s: %s", object_key, assignment.reason)
        _emit("PrefixMappingUnresolvable")
    if assignment.scope_denied or not assignment.mapping_prefix:
        if assignment.scope_denied:
            logger.warning(
                "Not applying a prefix mapping to %s on reprocess: the mapped "
                "profile is outside the caller's configuration scope",
                object_key,
            )
        # Also `active-profile`: no mapping applied, so the document reprocesses
        # the way it would have with none configured. The scope denial is recorded
        # in the log above rather than in the source, deliberately -- the source
        # travels onto the document and is read back by anyone who can see it, so
        # a value meaning "a mapping was refused here" would tell a scoped user
        # that a mapping governs a prefix they cannot see, which is the disclosure
        # `resolve_config_assignment` strips for the same reason.
        return None, requested_revision, SOURCE_ACTIVE_PROFILE, None

    logger.info("Reprocess of %s: %s", object_key, assignment.reason)
    _emit("PrefixMappingApplied", {"Prefix": assignment.mapping_prefix})
    # Both, or neither: the mapping's revision, never the caller's.
    return (
        assignment.profile,
        assignment.revision,
        assignment.source,
        assignment.mapping_prefix,
    )


def _profile_head_exists(manager, profile):
    """Whether a profile head item is there, without reading its body."""
    item = manager.table.get_item(
        Key={"Configuration": f"Config#{profile}"},
        ProjectionExpression="Configuration",
    ).get("Item")
    return bool(item)


# The three resolver seams, memoized for one request. A batch reprocess of N
# documents sharing one prefix would otherwise spend up to 3N DynamoDB reads whose
# answers are identical, inside API Gateway's 29s integration ceiling -- which is
# the shape that turns a constant factor into a timeout on a large batch. Same
# treatment upload_resolver gives the same three seams.


def _memo_active(manager, cache):
    if manager is None:
        return None

    def active():
        if "active" not in cache:
            cache["active"] = manager.resolve_active_version()
        return cache["active"]

    return active


def _memo_published(manager, cache):
    if manager is None:
        return None

    def published(profile):
        key = ("published", profile)
        if key not in cache:
            cache[key] = manager.resolve_published_revision(profile)
        return cache[key]

    return published


def _memo_exists(manager, cache):
    if manager is None:
        return None

    def exists(profile):
        key = ("exists", profile)
        if key not in cache:
            cache[key] = _profile_head_exists(manager, profile)
        return cache[key]

    return exists


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

        # Read the mapping set and build the manager ONCE, outside the loop. A batch
        # reprocess is bounded by API Gateway's 29s integration ceiling, and this
        # path previously spent no reads here at all.
        mappings = _prefix_mappings()
        manager = _configuration_manager()
        seam_cache: dict = {}

        # Resolve EVERY document's configuration before queueing any of it.
        #
        # ⚠️ The loop below swallows per-document failures and returns success for
        # the request either way, which is right for a transient SQS error -- one
        # document missing from a batch of fifty should not fail the other
        # forty-nine. It is wrong for a REFUSAL: a `reject` mapping that was
        # logged, skipped and reported as success would mean an operator believes
        # the re-run happened, which is the same outcome as the mapping not
        # existing. Resolution is therefore a pre-pass that raises, and it raises
        # for the batch rather than per document -- matching `_enforce_document_scope`
        # above, which refuses a batch whole rather than part-processing it, for the
        # same reason: a partly-applied routing policy is harder to reason about
        # than a refused request.
        resolutions = []
        for object_key in object_keys:
            resolutions.append(
                (
                    object_key,
                    _version_for_document(
                        version,
                        revision,
                        object_key,
                        current_versions,
                        allowed_versions,
                        mappings=mappings,
                        manager=manager,
                        seam_cache=seam_cache,
                    ),
                )
            )

        # Process each document
        success_count = 0
        for object_key, (
            resolved_version,
            resolved_revision,
            config_source,
            mapping_prefix,
        ) in resolutions:
            try:
                reprocess_document(
                    object_key,
                    resolved_version,
                    resolved_revision,
                    config_source=config_source,
                    config_mapping_prefix=mapping_prefix,
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


#: Statuses whose documents must never be re-admitted to the pipeline.
#:
#: The malware-scanning ingest gate (see ``src/lambda/queue_sender``) refuses a
#: document *before* any workflow starts, so the file is still sitting in the
#: input bucket. Reprocess does not go through that gate — it builds a fresh
#: QUEUED document and puts it straight on the queue — so without this check an
#: Admin clicking Reprocess on a quarantined document would push known malware
#: into OCR, page rendering and Bedrock, which is the one thing the gate exists to
#: prevent.
#:
#: Stated as the statuses that are REFUSED rather than the ones that are allowed,
#: unlike ``process_changes_resolver._EDITABLE_STATUSES``, and the difference is
#: deliberate: reprocess is legitimate from *most* statuses (that is what makes it
#: useful on a FAILED or half-processed document), so an allow-list here would
#: have to enumerate nearly the whole enum and would refuse every pipeline stage
#: added later. The hazard being closed is a small, named set. A newly added
#: refusal-worthy status is covered by
#: ``test_every_malware_status_is_refused``, which derives the set from
#: ``Status`` rather than restating it.
_REPROCESS_REFUSED_STATUSES = frozenset(
    {
        Status.QUARANTINED.value,
        Status.MALWARE_SCAN_FAILED.value,
    }
)


def _refuse_if_refused_by_the_malware_gate(object_key):
    """Raise if this document was refused at the malware-scanning ingest gate.

    Fails **closed** on a read error, matching the "both directions fail closed"
    posture this file's own header states for its scope checks. The reasoning is
    narrower than for the other best-effort reads here: those protect against
    stale state after the pipeline has already seen a document, whereas this one
    is the ONLY backstop for a known-infected or unscannable file. Reprocess does
    not go back through the ingest gate — it builds a fresh QUEUED document and
    hands it to SQS, so a DynamoDB blip that admits a possibly-quarantined file
    cannot be recovered by "the gate will see it again". A retry is cheap; a
    reprocessed infected document is not.
    """
    try:
        document = document_service.get_document(object_key)
    except Exception as e:  # noqa: BLE001
        logger.error(
            f"Could not read {object_key}'s status before reprocessing ({e}); "
            f"refusing until the status can be read"
        )
        raise ValueError(
            f"Could not verify {object_key}'s status before reprocessing. "
            f"This is temporary — retry the request. ({e})"
        ) from e
    if document is None:
        return
    status = getattr(document.status, "value", document.status)
    if status in _REPROCESS_REFUSED_STATUSES:
        raise ValueError(
            f"Document {object_key} was refused at the malware-scanning gate "
            f"(status {status}) and cannot be reprocessed. Delete it, or — if it "
            f"is a legitimate document GuardDuty could not read, such as a "
            f"password-protected PDF — re-upload an unprotected copy so it is "
            f"scanned again."
        )


def reprocess_document(
    object_key,
    version=None,
    revision=None,
    config_source=None,
    config_mapping_prefix=None,
):
    """
    Reprocess a document by creating a fresh Document object and queueing it.
    This exactly mirrors the queue_sender pattern for consistency and avoids
    S3 copy operations that can trigger duplicate events for large files.

    Args:
        object_key: S3 object key of the document to reprocess
        version: Optional Configuration Profile to use for reprocessing
        revision: Optional revision of that profile. Omit to reprocess under the
            profile's current configuration.
        config_source: Where ``version`` came from — see
            ``_version_for_document``. Recorded on the document so a reprocess
            that picked up a prefix mapping is distinguishable from one the
            caller pinned, which is otherwise invisible after the fact.
        config_mapping_prefix: The mapping prefix that decided it, when one did.
    """
    logger.info(
        f"Reprocessing document: {object_key}"
        + (f" with version: {version}" if version else "")
        + (f" r{revision}" if revision is not None else "")
    )

    _refuse_if_refused_by_the_malware_gate(object_key)

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
        config_source=config_source,
        config_mapping_prefix=config_mapping_prefix,
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
