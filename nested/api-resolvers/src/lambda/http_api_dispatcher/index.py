# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
HTTP API dispatcher — single entry point for the API Gateway HTTP API that
replaces AppSync for UI<->backend queries and mutations.

The HTTP API exposes one route, ``POST /op/{field}``, backed by a Cognito JWT
authorizer. This Lambda:

1. Normalizes the HTTP API (payload v2.0) event into the AppSync resolver event
   shape via :mod:`idp_common.api_adapter` (restoring ``cognito:groups`` to a
   list — see that module for why this matters).
2. Enforces a **default-deny** group check (:mod:`authz`) before dispatching: the
   caller's groups (from the verified JWT claim) must satisfy the field's entry in
   the bundled required-groups manifest, and a field with NO entry is denied. This
   restores at the API layer what the AppSync schema's ``cognito_groups``
   directives used to do; the per-resolver checks remain in place beneath it.
3. Routes the field to its handler:
   - **Lambda-backed fields**: synchronously invokes the existing resolver
     Lambda (the same function AppSync invokes) with the AppSync-shaped event,
     so those resolvers need NO changes.
   - **DynamoDB-direct fields** (discovery jobs, agent jobs) that AppSync
     handled with VTL: served locally by :mod:`ddb_direct` (no Lambda hop).
4. Wraps the result into an HTTP API proxy response, mapping errors to status
   codes with the GraphQL-style ``{"errors": [...]}`` body the UI parses.

Field -> resolver function ARN mapping is provided via the ``FIELD_FUNCTION_MAP``
environment variable (JSON: ``{"fieldName": "FUNCTION_ARN", ...}``) populated by
CloudFormation. Fields absent from the map are handled by ``ddb_direct``; a field
that is neither authorized (step 2) nor routable is rejected.
"""

import json
import logging
import os
from typing import Any, Dict

import authz
import boto3
import ddb_direct
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError
from validation import validate_arguments

from idp_common.api_adapter import (
    ResourceAccessDenied,
    ResourceNotFound,
    _http_response,
    normalize_event,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ---------------------------------------------------------------------------
# Which control refused — the `authzControl` field on every 403 body
# ---------------------------------------------------------------------------
# A 403 with `errorType: "Unauthorized"` is the ONLY authorization-refusal shape
# this API has, and it is produced by controls that answer opposite questions:
#
#   * the field-level group floor in authz.py — "this caller's GROUPS do not admit
#     this operation";
#   * a per-object control inside a resolver — "this caller's groups were fine and
#     the OBJECT they named was refused" (a bucket outside the deployment's
#     allow-list, a chat session they do not own, a document selection that is
#     unfinished or out of their configuration scope).
#
# Nothing in the response distinguished them. The live RBAC matrix
# (`scripts/test_api_rbac.py`) asserts the first and cannot avoid provoking the
# second — it drives every operation with deliberately bogus arguments — so eight
# cells reported a per-object control working correctly as a group-authorization
# failure, and a reader had to go to CloudWatch to tell the two apart. Issue #1308.
#
# `errorType` stays `"Unauthorized"` and the status stays 403 for every value
# below: the UI keys on those and nothing about the client contract changes. This
# field is ADDITIVE, and a consumer that does not read it sees exactly what it saw
# before.
#
# The vocabulary is closed, and the names are the stable part — a consumer matches
# on these strings. `scripts/test_api_rbac.py` derives its own table from this
# dict, so the two cannot drift.
AUTHZ_CONTROL_FIELD = "authzControl"
#: authz.enforce refused: undeclared field, IAM_ONLY operation, or group mismatch.
#: All three are decisions about the FIELD and the CALLER'S GROUPS.
AUTHZ_CONTROL_GROUP_FLOOR = "api-group-floor"
#: A resolver refused the OBJECT the caller named, having admitted the caller.
#: Declared by the resolver, by raising an exception named `ResourceAccessDenied`
#: (recognised by class name across the invoke boundary, like `ResourceNotFound`).
AUTHZ_CONTROL_RESOURCE = "resource"
#: A resolver refused and did NOT say which control did it. Deliberately distinct
#: from `resource`: an unlabelled refusal gets the STRICT reading downstream, so
#: forgetting to declare a per-object control costs a visible failure rather than a
#: silent pass. Defaulting the other way would let a genuine group-check regression
#: inside a resolver read as a known coverage gap.
AUTHZ_CONTROL_RESOLVER_UNSPECIFIED = "resolver"
#: normalize_event refused an invocation that asserted its own `identity`. Neither
#: a group decision nor an object one — reachable only by a principal holding
#: lambda:InvokeFunction on this function directly.
AUTHZ_CONTROL_UNVERIFIED_IDENTITY = "unverified-identity"

AUTHZ_CONTROLS = (
    AUTHZ_CONTROL_GROUP_FLOOR,
    AUTHZ_CONTROL_RESOURCE,
    AUTHZ_CONTROL_RESOLVER_UNSPECIFIED,
    AUTHZ_CONTROL_UNVERIFIED_IDENTITY,
)


def _denial_response(message: str, control: str) -> Dict[str, Any]:
    """The one place a 403 body is built, so every denial carries a control label.

    One function rather than four literals: a new refusal path that forgets the
    label is the failure mode this exists to prevent, and the harness reads an
    absent label as "this deployment cannot tell me", which silently restores the
    behaviour #1308 is about.

    An unrecognised value is **logged and downgraded, never raised.** Every call
    site passes one of the module constants, so reaching this branch is a
    programming error — but this function runs inside an ``except`` handler, so an
    exception from here escapes the handler entirely and API Gateway answers a
    bodiless 502. A refusal that becomes a 502 is worse than one carrying the wrong
    label: it stops being a denial the UI can render, and it moves a deliberate
    policy decision into the 5xx rate that server faults are alarmed on. Denying
    with the strict label keeps the refusal a refusal, and the ERROR line is how the
    mistake gets found.
    """
    if control not in AUTHZ_CONTROLS:
        logger.error(
            "Unknown authz control %r; expected one of %s. Denying with %r, which "
            "every consumer reads strictly. This is a programming error in this "
            "module, not an authorization decision about the caller.",
            control,
            AUTHZ_CONTROLS,
            AUTHZ_CONTROL_RESOLVER_UNSPECIFIED,
        )
        control = AUTHZ_CONTROL_RESOLVER_UNSPECIFIED
    return _http_response(
        403,
        {
            "errors": [
                {
                    "message": message,
                    "errorType": "Unauthorized",
                    AUTHZ_CONTROL_FIELD: control,
                }
            ]
        },
    )


# REST API Gateway abandons an integration after 29s (account default) and
# answers the browser `504` with no body, so anything slower than this is
# unreportable by construction: the UI shows `Request failed (504)` and neither
# the dispatcher nor the resolver logs say which call was slow. That is how a
# hung S3 data-plane call in the test-set resolver read as "Test Studio is
# broken" with nothing to go on.
#
# Bounding the invoke under the gateway budget lets US lose the race instead: the
# resolver read times out first, this Lambda returns a labelled 504 naming the
# field, and the UI has something to show.
#
# 20s, not 26s. API Gateway's 29s clock starts when it dispatches the
# integration, which is BEFORE this function's init — and init here is not free
# (the idp_common base layer, plus an SSM get_parameter for FIELD_FUNCTION_MAP at
# module import). On a cold start, init + the invoke bound + building the response
# all have to fit in 29s, so a 26s bound leaves ~3s for all of it and the labelled
# 504 loses the race in exactly the situation it exists for. 20s leaves real
# headroom on the one clock we do not control.
_RESOLVER_READ_TIMEOUT_SECONDS = 20

# Reaching the Lambda control plane is fast or not happening; a connect timeout
# here means a networking fault (or a missing VPC endpoint), not a busy resolver.
_RESOLVER_CONNECT_TIMEOUT_SECONDS = 3

# total_max_attempts=1 means ONE attempt. This must not be written as
# `max_attempts: 1` — botocore reads max_attempts as a RETRY count and normalizes
# it to `total_max_attempts: 2`, i.e. the opposite of what is wanted here. (The
# S3 clients in test_set_resolver/upload_resolver use `max_attempts` deliberately
# and do want 2 attempts; this client does not.)
#
# One attempt is required, not merely preferred. botocore classifies
# ReadTimeoutError as transient (it subclasses HTTPClientError), so a second
# attempt would (a) start after the first 20s window plus jitter and be killed by
# the 29s function Timeout before the ReadTimeoutError handler below could return
# the labelled 504, and (b) run a second concurrent copy of the resolver — a
# duplicate mutation for the long-running fields (copyToBaseline, startTestRun,
# syncBdaIdp, deleteTests, addDocumentsToTestSet, …) whose own Timeout exceeds
# this bound. The retries that DO matter — invoke-level failures that never ran
# the resolver, costing milliseconds rather than a full attempt — are reissued
# explicitly in _invoke_resolver below, so turning botocore's retries off does not
# make a healthy deployment more fragile.
_lambda = boto3.client(
    "lambda",
    config=BotoConfig(
        connect_timeout=_RESOLVER_CONNECT_TIMEOUT_SECONDS,
        read_timeout=_RESOLVER_READ_TIMEOUT_SECONDS,
        retries={"mode": "standard", "total_max_attempts": 1},
    ),
)
_ssm = boto3.client("ssm")

# Invoke failures worth one immediate retry: the request never reached the
# resolver (or the Lambda service could not answer), so reissuing is both safe
# and cheap — none of these consume a read-timeout window. Deliberately NOT
# retried: ReadTimeoutError (the resolver IS running; a retry would double-run a
# mutation and cannot fit the budget anyway) and every deterministic 4xx
# (AccessDenied, ResourceNotFound — a retry only delays the same error).
_RETRYABLE_INVOKE_ERROR_CODES = frozenset(
    {
        # Concurrency limit hit before the resolver ran.
        "TooManyRequestsException",
        # Lambda service-side failure (the 5xx branch below also catches these
        # when the code is absent or unfamiliar).
        "ServiceException",
        # ENI/EC2 capacity for a VPC-attached resolver.
        "EC2ThrottledException",
        "ENILimitReachedException",
    }
)


# {fieldName: resolverFunctionArn} — fields routed to existing resolver Lambdas.
# The map can hold ~60 full ARNs (>4KB), exceeding the Lambda env-var limit, so
# it is stored in an SSM parameter (FIELD_FUNCTION_MAP_PARAM) and loaded once at
# cold start. A direct FIELD_FUNCTION_MAP env var is still honored as a fallback
# (e.g. for tests).
def _load_field_function_map() -> Dict[str, str]:
    inline = os.environ.get("FIELD_FUNCTION_MAP")
    if inline:
        return json.loads(inline)
    param = os.environ.get("FIELD_FUNCTION_MAP_PARAM")
    if param:
        try:
            resp = _ssm.get_parameter(Name=param)
            return json.loads(resp["Parameter"]["Value"])
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to load FIELD_FUNCTION_MAP from SSM %s: %s", param, e)
    return {}


FIELD_FUNCTION_MAP: Dict[str, str] = _load_field_function_map()

# Field aliases: fields served by the SAME resolver Lambda as another field.
# Kept out of FIELD_FUNCTION_MAP (the SSM parameter that carries it): one map
# entry per unique resolver Lambda, not per GraphQL field. Advanced-tier SSM
# value limit is 8,192 bytes; the resolved worst case is checked by
# scripts/tests/test_http_api_field_map_size.py. Route additional fields of an
# existing Lambda through FIELD_ALIASES. Each alias's resolver
# branches on the GraphQL `fieldName`, which the normalized event carries
# regardless of which field name routed to it.
FIELD_ALIASES: Dict[str, str] = {
    "getFilePresignedUrl": "getFileContents",
    # Both presign fields alias the same resolver, which dispatches on fieldName.
    # An alias rather than a FIELD_FUNCTION_MAP entry, which would grow the SSM
    # parameter towards its 8 KB Advanced-tier limit; measure the headroom with
    # scripts/tests/test_http_api_field_map_size.py.
    "getFilePresignedUrls": "getFileContents",
    "listSampleDocuments": "uploadDocument",
    "uploadSampleDocument": "uploadDocument",
    # addDocumentsToTestSet (TestSetResolverFunction)
    "addDocumentsToTestSetByKey": "addDocumentsToTestSet",
    "addDocumentsToTestSetFromUpload": "addDocumentsToTestSet",
    "addTestSet": "addDocumentsToTestSet",
    "addTestSetFromUpload": "addDocumentsToTestSet",
    "clearDraftLabels": "addDocumentsToTestSet",
    "createEmptyTestSet": "addDocumentsToTestSet",
    "deleteTestSets": "addDocumentsToTestSet",
    "estimateReviewEffort": "addDocumentsToTestSet",
    "getAnnotationQueue": "addDocumentsToTestSet",
    "assignTestSetDocuments": "addDocumentsToTestSet",
    "unassignTestSetDocuments": "addDocumentsToTestSet",
    "listMyTestSetTasks": "addDocumentsToTestSet",
    "listTestSetQueues": "addDocumentsToTestSet",
    "saveTestSetDocumentSection": "addDocumentsToTestSet",
    "saveTestSetDocumentSectionDraft": "addDocumentsToTestSet",
    "skipTestSetDocument": "addDocumentsToTestSet",
    "claimTestSetDocument": "addDocumentsToTestSet",
    "updateTestSetQueue": "addDocumentsToTestSet",
    # Single hop to a real SSM map entry (aliases do not chain). Advanced-tier
    # SSM value limit is 8,192 bytes; the resolved worst case is checked by
    # scripts/tests/test_http_api_field_map_size.py.
    "openTestSetAnnotationDraft": "addDocumentsToTestSet",
    "generateDraftLabels": "addDocumentsToTestSet",
    "getDraftLabelJob": "addDocumentsToTestSet",
    "updateDocumentSections": "processChanges",
    "updateTestSetDocumentSections": "addDocumentsToTestSet",
    "getTestSetDocuments": "addDocumentsToTestSet",
    "getTestSets": "addDocumentsToTestSet",
    "getTestSetVersions": "addDocumentsToTestSet",
    "listBucketFiles": "addDocumentsToTestSet",
    "publishTestSetVersion": "addDocumentsToTestSet",
    "reextractTestSetDocument": "addDocumentsToTestSet",
    "resetTestSetLabels": "addDocumentsToTestSet",
    "removeDocumentsFromTestSet": "addDocumentsToTestSet",
    "updateTestSet": "addDocumentsToTestSet",
    "validateTestFileName": "addDocumentsToTestSet",
    # compareDocumentVersions (DocumentVersionsResolverFunction)
    "deleteDocumentVersion": "compareDocumentVersions",
    "getDocumentVersion": "compareDocumentVersions",
    "listDocumentVersions": "compareDocumentVersions",
    # deleteConfigVersion (ConfigurationResolverFunction)
    "deleteConfigProfileRevision": "deleteConfigVersion",
    "getConfigProfileRevision": "deleteConfigVersion",
    "labelConfigProfileRevision": "deleteConfigVersion",
    "listConfigProfileRevisions": "deleteConfigVersion",
    "restoreConfigProfileRevision": "deleteConfigVersion",
    # Config prefix mappings (S3 prefix -> Configuration Profile), served by the
    # same configuration resolver.
    "listConfigPrefixMappings": "deleteConfigVersion",
    "putConfigPrefixMapping": "deleteConfigVersion",
    "deleteConfigPrefixMapping": "deleteConfigVersion",
    "resolveConfigPrefixMapping": "deleteConfigVersion",
    "getConfigVersion": "deleteConfigVersion",
    "getConfigVersions": "deleteConfigVersion",
    "getConfigurationLibraryFile": "deleteConfigVersion",
    "getModelConfigLimits": "deleteConfigVersion",
    "getPricing": "deleteConfigVersion",
    "listConfigurationLibrary": "deleteConfigVersion",
    "restoreDefaultModelConfigLimits": "deleteConfigVersion",
    "restoreDefaultPricing": "deleteConfigVersion",
    "setActiveVersion": "deleteConfigVersion",
    "updateConfiguration": "deleteConfigVersion",
    "updateModelConfigLimits": "deleteConfigVersion",
    "updatePricing": "deleteConfigVersion",
    "generateRuleJson": "deleteConfigVersion",
    # compareTestRuns (TestResultsResolverFunction)
    "getTestRun": "compareTestRuns",
    "getTestRunStatus": "compareTestRuns",
    "getTestRuns": "compareTestRuns",
    # getDocumentCount (ListDocumentsGSIResolverFunction)
    "listDocuments": "getDocumentCount",
    # getCircuitBreakerStatus (CircuitBreakerResolverFunctionArn)
    "pauseCircuitBreaker": "getCircuitBreakerStatus",
    "probeCircuitBreaker": "getCircuitBreakerStatus",
    "resumeCircuitBreaker": "getCircuitBreakerStatus",
    # autoDetectSections (DiscoveryUploadResolverFunction)
    "startMultiDocDiscovery": "autoDetectSections",
    "uploadDiscoveryDocument": "autoDetectSections",
    "uploadMultiDocDiscoveryZip": "autoDetectSections",
    # claimReview (CompleteSectionReviewFunctionArn)
    "completeSectionReview": "claimReview",
    "releaseReview": "claimReview",
    "skipAllSectionsReview": "claimReview",
    "saveSectionReviewEdits": "claimReview",
    # createUser (UserManagementFunctionArn)
    "updateUser": "createUser",
    "deleteUser": "createUser",
    "listUsers": "createUser",
    "getMyProfile": "createUser",
    # createFinetuningJob (FinetuningJobsResolverFunctionArn)
    "deleteFinetuningJob": "createFinetuningJob",
    "getFinetuningJob": "createFinetuningJob",
    "listFinetuningJobs": "createFinetuningJob",
    # sendTestRunToReview (TestRunnerFunction)
    "sendTestRunToReview": "startTestRun",
    # listInboxMessages (NotificationsResolverFunction)
    "getInboxUnreadCount": "listInboxMessages",
    "getInboxPreferences": "listInboxMessages",
    "markInboxMessages": "listInboxMessages",
    "markAllInboxMessagesRead": "listInboxMessages",
    "deleteInboxMessages": "listInboxMessages",
    "clearReadInboxMessages": "listInboxMessages",
    "updateInboxPreferences": "listInboxMessages",
    # getMyReviewQueues (ReviewQueuesResolverFunction)
    "listReviewQueueItems": "getMyReviewQueues",
    "listMyReviewWork": "getMyReviewQueues",
    "getReviewItem": "getMyReviewQueues",
    "claimReviewItem": "getMyReviewQueues",
    "claimNextReviewItem": "getMyReviewQueues",
    "renewReviewLease": "getMyReviewQueues",
    "releaseReviewItem": "getMyReviewQueues",
    "saveReviewDraft": "getMyReviewQueues",
    "completeReviewItem": "getMyReviewQueues",
    "rejectReviewItem": "getMyReviewQueues",
    "removeReviewItem": "getMyReviewQueues",
    "getReviewQueuesOverview": "getMyReviewQueues",
    "forceReleaseReviewItem": "getMyReviewQueues",
    "reassignReviewItem": "getMyReviewQueues",
    "takeOverReviewItem": "getMyReviewQueues",
    "moveReviewItem": "getMyReviewQueues",
    "listReviewQueues": "getMyReviewQueues",
    "listAnnotators": "getMyReviewQueues",
    "createReviewQueue": "getMyReviewQueues",
    "updateReviewQueue": "getMyReviewQueues",
    "deleteReviewQueue": "getMyReviewQueues",
}


class ResolverTimeout(Exception):
    """A resolver did not answer inside the API Gateway integration budget.

    Distinct from a generic failure so the handler can return 504 with the field
    name rather than letting API Gateway emit a bodiless one.
    """


def _is_retryable_invoke_error(error: ClientError) -> bool:
    """True for invoke failures that did not reach (or run) the resolver."""
    code = error.response.get("Error", {}).get("Code", "")
    if code in _RETRYABLE_INVOKE_ERROR_CODES:
        return True
    # Any 5xx from the Lambda service, including codes not named above.
    status = (error.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return isinstance(status, int) and status >= 500


def _invoke_resolver(function_arn: str, appsync_event: Dict[str, Any]) -> Any:
    """Synchronously invoke a resolver Lambda with an AppSync-shaped event."""
    payload_bytes = json.dumps(appsync_event).encode("utf-8")

    # Two attempts at most, and only for errors that never ran the resolver.
    for attempt in (1, 2):
        try:
            resp = _lambda.invoke(
                FunctionName=function_arn,
                InvocationType="RequestResponse",
                Payload=payload_bytes,
            )
            break
        except ReadTimeoutError as e:
            # The resolver is still running; we simply cannot wait for it and
            # stay inside the gateway budget. Name the function so the log points
            # at the right CloudWatch group instead of leaving a bare 504.
            logger.error(
                "Resolver %s did not respond within %ss (API Gateway aborts the "
                "integration at 29s); returning 504",
                function_arn,
                _RESOLVER_READ_TIMEOUT_SECONDS,
            )
            raise ResolverTimeout(
                f"resolver did not respond within {_RESOLVER_READ_TIMEOUT_SECONDS}s"
            ) from e
        except ConnectTimeoutError as e:
            # We never reached the Lambda service, so the resolver did NOT run.
            # This is still a gateway-timeout condition rather than an internal
            # error, so report 504 — with a message that does not blame the
            # resolver, which never started — instead of letting it fall through
            # to the generic 500 handler.
            #
            # Deliberately NOT retried, even though reissuing would be safe here:
            # a connect timeout to the Lambda control plane means a networking
            # fault (missing VPC endpoint, security-group ingress), which is
            # persistent, so a second attempt almost never succeeds and would add
            # its 3s to a budget the read timeout below already spends 20s of.
            logger.error(
                "Could not connect to the Lambda service to invoke %s within %ss; "
                "returning 504",
                function_arn,
                _RESOLVER_CONNECT_TIMEOUT_SECONDS,
            )
            raise ResolverTimeout(
                "could not reach the Lambda service to start the resolver"
            ) from e
        except ClientError as e:
            if attempt == 2 or not _is_retryable_invoke_error(e):
                raise
            logger.warning(
                "Invoke of %s failed with a transient error (%s); retrying once",
                function_arn,
                e.response.get("Error", {}).get("Code", "unknown"),
            )

    payload = resp["Payload"].read()
    data = json.loads(payload) if payload else None

    # A handled Lambda error surfaces as FunctionError. Re-raise as the closest
    # Python exception TYPE so the handler maps it to the right HTTP status —
    # crucially, a resolver's RBAC denial (PermissionError) must become a 403
    # with errorType "Unauthorized" (which the UI keys on), NOT an opaque 500.
    # The invoke response carries the original exception class name in
    # data["errorType"] (e.g. "PermissionError") and the message in
    # data["errorMessage"].
    if resp.get("FunctionError"):
        msg = "resolver error"
        err_type = ""
        if isinstance(data, dict):
            msg = data.get("errorMessage", msg)
            err_type = data.get("errorType", "") or ""
        # A PER-OBJECT refusal -> 403, labelled `resource`. Checked BEFORE the
        # general authorization branch below, because this class's messages keep the
        # "Unauthorized" prefix (deliberately, so the mapping survives a resolver
        # deployed ahead of this dispatcher) and that prefix would otherwise match
        # the general branch first and lose the attribution. Matched by class NAME:
        # the exception object does not
        # cross the invoke boundary, only data["errorType"], so a resolver with no
        # idp_common dependency can declare its own class of this name. See
        # idp_common.api_adapter.ResourceAccessDenied.
        if err_type == "ResourceAccessDenied":
            raise ResourceAccessDenied(msg)
        # Authorization denials -> 403. Match by exception class name or a
        # conventional "Unauthorized:"/"Forbidden" message prefix. Reaching here
        # means the resolver did not say which control refused, which the handler
        # labels `resolver` rather than guessing.
        if err_type in ("PermissionError", "AuthorizationError") or msg.startswith(
            ("Unauthorized", "Forbidden")
        ):
            raise PermissionError(msg)
        # A named resource that does not exist -> 404. Matched by class NAME: the
        # exception object does not cross the invoke boundary, only
        # data["errorType"], so a resolver with no idp_common dependency can
        # declare its own class of this name. See
        # idp_common.api_adapter.ResourceNotFound for why its errorType is not
        # "NotFound" (that one means "operation not routable here").
        if err_type == "ResourceNotFound":
            raise ResourceNotFound(msg)
        # Client input errors -> 400.
        if err_type in ("ValueError", "KeyError"):
            raise ValueError(msg)
        raise RuntimeError(msg)
    return data


def handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    # No CORS-preflight branch here on purpose. The API is an
    # AWS::ApiGateway::RestApi, so OPTIONS /op/{field} is answered by the
    # HttpApiOptionsMethod MOCK integration in nested/api-resolvers/template.yaml
    # and never reaches this function; only the POST method has a Lambda proxy
    # integration. The branch this replaces read requestContext.http.method,
    # which is a payload-format-2.0 key that a REST API never sends anyway (1.0
    # uses requestContext.httpMethod), so it could not have matched even if
    # OPTIONS were routed here.
    # normalize_event is called OUTSIDE the try below (the field name it yields is
    # what every log line and error body names), so its one authorization failure
    # has to be mapped here: it raises CallerIdentityRefused — a PermissionError —
    # for an invocation that asserts its own `identity` rather than presenting one
    # the transport verified. Reachable only by a principal holding
    # lambda:InvokeFunction on this function directly, since API Gateway builds the
    # event itself and a request body cannot add top-level keys to it. Without this
    # branch the refusal would leave the function on an unhandled exception, which
    # API Gateway reports as a bodiless 502.
    try:
        appsync_event = normalize_event(event)
    except PermissionError as e:
        logger.warning("Rejecting invocation with an unverified identity: %s", e)
        return _denial_response(str(e), AUTHZ_CONTROL_UNVERIFIED_IDENTITY)
    field = appsync_event.get("info", {}).get("fieldName", "")

    if not field:
        return _http_response(
            400,
            {
                "errors": [
                    {"message": "missing operation field", "errorType": "BadRequest"}
                ]
            },
        )

    try:
        # Default-deny group authorization (restores what the AppSync schema's
        # cognito_groups directives did at the API layer). Runs BEFORE argument
        # validation and before routing: an unauthorized caller learns nothing
        # about the operation's argument shape, and a field with no manifest
        # entry is denied rather than dispatched. Raises PermissionError -> 403
        # via the existing handler below. The per-resolver checks are unchanged
        # and still run — this is the floor, not a replacement for them, since
        # only the resolver can enforce per-object scope. See authz.py.
        authz.enforce(field, appsync_event)

        # Central schema-shape validation (restores what AppSync did for free).
        # Validate under the ORIGINAL field name — aliases (getFilePresignedUrl,
        # etc.) resolve to a target only for ROUTING; their own name is what the
        # UI sends and, when present in schema.graphql, what we validate against.
        # Fields not in the spec (unknown / non-schema) are a no-op here and get
        # rejected/served downstream. Raises ValueError → 400/BadRequest below.
        validate_arguments(field, appsync_event.get("arguments") or {})

        # A mapped-but-empty ARN means the resolver is feature-flagged off (its
        # Lambda is conditional and absent), e.g. the circuit-breaker fields when
        # CircuitBreakerEnabled=false. Treat empty as unroutable so it falls
        # through to ddb_direct (which serves a graceful disabled response for
        # getCircuitBreakerStatus) rather than invoking an empty FunctionName.
        #
        # The 404 below is now reached only by an operation that IS declared (it
        # passed authz above) yet is not routable in this deployment — i.e. a
        # feature-flagged-off resolver. An undeclared field never gets this far:
        # authz denies it with 403, which is also why the 404 cannot be used to
        # probe which operations a deployment has.
        mapped_arn = FIELD_FUNCTION_MAP.get(FIELD_ALIASES.get(field, field))
        if mapped_arn:
            result = _invoke_resolver(mapped_arn, appsync_event)
        elif ddb_direct.handles(field):
            result = ddb_direct.dispatch(field, appsync_event)
        else:
            return _http_response(
                404,
                {
                    "errors": [
                        {
                            "message": f"unknown operation: {field}",
                            "errorType": "NotFound",
                        }
                    ]
                },
            )
    except authz.GroupFloorDenied as e:
        # The FIELD-level group floor refused: the caller's groups do not admit this
        # operation (or it is undeclared, or IAM-only). Attributed from the
        # exception CLASS, which only authz.enforce raises, rather than from the
        # message — resolver group refusals and resolver per-object refusals are
        # both worded "Unauthorized: ...", so prose cannot carry this.
        logger.warning("Authorization denied for %s: %s", field, e)
        return _denial_response(str(e), AUTHZ_CONTROL_GROUP_FLOOR)
    except ResourceAccessDenied as e:
        # A resolver admitted the caller's groups and refused the OBJECT they
        # named. Same status and errorType as every other denial; the label is the
        # only difference, and it is what lets a consumer tell "correctly refused
        # for their group" from "group was fine, the resource was refused".
        logger.warning("Resource access denied for %s: %s", field, e)
        return _denial_response(str(e), AUTHZ_CONTROL_RESOURCE)
    except PermissionError as e:
        # A refusal this layer cannot attribute: a resolver's own group check, or a
        # per-object control that has not been declared with ResourceAccessDenied.
        # Labelled as unattributed rather than guessed at, and read downstream as
        # the strict verdict.
        logger.warning("Authorization denied for %s: %s", field, e)
        return _denial_response(str(e), AUTHZ_CONTROL_RESOLVER_UNSPECIFIED)
    except ResolverTimeout as e:
        # 504 with a body. API Gateway's own 29s timeout produces a 504 with an
        # empty body, which is what made this failure mode undiagnosable from
        # the browser; include the field so the UI can say what timed out.
        logger.error("Timeout for %s: %s", field, e)
        return _http_response(
            504,
            {
                "errors": [
                    {
                        "message": f"operation '{field}' timed out: {e}",
                        "errorType": "Timeout",
                    }
                ]
            },
        )
    except ResourceNotFound as e:
        # 404 with errorType "ResourceNotFound" — NOT the "NotFound" the
        # unroutable-operation branch above returns. The two 404s mean different
        # things ("this object does not exist" vs "this deployment does not have
        # this operation") and the live RBAC harness keys its feature-disabled
        # probe on the difference.
        #
        # Logged at warning, not error: this is an ordinary outcome. Without this
        # arm a resolver that refused because the named object does not exist
        # answered 500, which counted against the API's 5xx signals. Not *every*
        # deliberate not-found did: the unroutable-operation branch above already
        # answered 404, and the IAM-gated direct path in `api_adapter.py`
        # propagates the exception rather than mapping it to a status at all.
        logger.warning("Not found for %s: %s", field, e)
        return _http_response(
            404,
            {"errors": [{"message": str(e), "errorType": "ResourceNotFound"}]},
        )
    except (ValueError, KeyError) as e:
        logger.warning("Bad request for %s: %s", field, e)
        return _http_response(
            400, {"errors": [{"message": str(e), "errorType": "BadRequest"}]}
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Dispatch error for %s: %s", field, e, exc_info=True)
        return _http_response(
            500, {"errors": [{"message": str(e), "errorType": "InternalError"}]}
        )

    return _http_response(200, result)
