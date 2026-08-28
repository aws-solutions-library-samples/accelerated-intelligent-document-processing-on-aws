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
2. Routes the field to its handler:
   - **Lambda-backed fields**: synchronously invokes the existing resolver
     Lambda (the same function AppSync invokes) with the AppSync-shaped event,
     so those resolvers need NO changes.
   - **DynamoDB-direct fields** (discovery jobs, agent jobs) that AppSync
     handled with VTL: served locally by :mod:`ddb_direct` (no Lambda hop).
3. Wraps the result into an HTTP API proxy response, mapping errors to status
   codes with the GraphQL-style ``{"errors": [...]}`` body the UI parses.

Field -> resolver function ARN mapping is provided via the ``FIELD_FUNCTION_MAP``
environment variable (JSON: ``{"fieldName": "FUNCTION_ARN", ...}``) populated by
CloudFormation. Fields absent from the map are handled by ``ddb_direct`` or
rejected as unknown.
"""

import json
import logging
import os
from typing import Any, Dict

import boto3
import ddb_direct
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, ReadTimeoutError
from validation import validate_arguments

from idp_common.api_adapter import _http_response, normalize_event

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# REST API Gateway abandons an integration after 29s (account default) and
# answers the browser `504` with no body, so anything slower than this is
# unreportable by construction: the UI shows `Request failed (504)` and neither
# the dispatcher nor the resolver logs say which call was slow. That is how a
# hung S3 data-plane call in the test-set resolver read as "Test Studio is
# broken" with nothing to go on.
#
# Bounding the invoke just under the gateway budget lets US lose the race
# instead: the resolver read times out at 26s, this Lambda returns a labelled
# 504 naming the field, and the UI has something to show. The function's own
# Timeout is 29s (see the nested template) so a slower path cannot outrun it
# either way.
_RESOLVER_READ_TIMEOUT_SECONDS = 26

# max_attempts=1 (no automatic retry) is deliberate. botocore retries a read
# timeout, and a second 26s attempt cannot fit inside the 29s budget — it would
# just replace the labelled 504 with a dead Lambda, and for a mutation it would
# risk running the operation twice. Invoke-level throttling, the one retry that
# was worth keeping, is retried explicitly in _invoke_resolver below.
_lambda = boto3.client(
    "lambda",
    config=BotoConfig(
        connect_timeout=3,
        read_timeout=_RESOLVER_READ_TIMEOUT_SECONDS,
        retries={"mode": "standard", "max_attempts": 1},
    ),
)
_ssm = boto3.client("ssm")


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
# Kept out of FIELD_FUNCTION_MAP (the SSM parameter that carries it) because
# that parameter is at the 8 KB Advanced-tier ceiling — one map entry per
# GraphQL field (rather than per unique resolver Lambda) would duplicate the
# same ARN/name dozens of times and overflow it (worse in GovCloud, where
# arn:aws-us-gov:... is longer than arn:aws:...). Each alias's resolver
# branches on the GraphQL `fieldName`, which the normalized event carries
# regardless of which field name routed to it.
FIELD_ALIASES: Dict[str, str] = {
    "getFilePresignedUrl": "getFileContents",
    "listSampleDocuments": "uploadDocument",
    "uploadSampleDocument": "uploadDocument",
    # addDocumentsToTestSet (TestSetResolverFunction)
    "addDocumentsToTestSetFromUpload": "addDocumentsToTestSet",
    "addTestSet": "addDocumentsToTestSet",
    "addTestSetFromUpload": "addDocumentsToTestSet",
    "clearDraftLabels": "addDocumentsToTestSet",
    "deleteTestSets": "addDocumentsToTestSet",
    "estimateReviewEffort": "addDocumentsToTestSet",
    "getAnnotationQueue": "addDocumentsToTestSet",
    "generateDraftLabels": "addDocumentsToTestSet",
    "getDraftLabelJob": "addDocumentsToTestSet",
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
}


class ResolverTimeout(Exception):
    """A resolver did not answer inside the API Gateway integration budget.

    Distinct from a generic failure so the handler can return 504 with the field
    name rather than letting API Gateway emit a bodiless one.
    """


def _invoke_resolver(function_arn: str, appsync_event: Dict[str, Any]) -> Any:
    """Synchronously invoke a resolver Lambda with an AppSync-shaped event."""
    payload_bytes = json.dumps(appsync_event).encode("utf-8")
    try:
        resp = _lambda.invoke(
            FunctionName=function_arn,
            InvocationType="RequestResponse",
            Payload=payload_bytes,
        )
    except ReadTimeoutError as e:
        # The resolver is still running; we simply cannot wait for it and stay
        # inside the gateway budget. Name the function so the log points at the
        # right CloudWatch group instead of leaving a bare 504.
        logger.error(
            "Resolver %s did not respond within %ss (API Gateway aborts the "
            "integration at 29s); returning 504",
            function_arn,
            _RESOLVER_READ_TIMEOUT_SECONDS,
        )
        raise ResolverTimeout(
            f"resolver did not respond within {_RESOLVER_READ_TIMEOUT_SECONDS}s"
        ) from e
    except ClientError as e:
        # Retries are off (see the client config), so absorb the one class of
        # transient error that was worth retrying: invoke-level throttling.
        # A single immediate retry still fits the budget.
        if e.response.get("Error", {}).get("Code") != "TooManyRequestsException":
            raise
        logger.warning("Invoke of %s throttled; retrying once", function_arn)
        try:
            resp = _lambda.invoke(
                FunctionName=function_arn,
                InvocationType="RequestResponse",
                Payload=payload_bytes,
            )
        except ReadTimeoutError as timeout_error:
            raise ResolverTimeout(
                f"resolver did not respond within {_RESOLVER_READ_TIMEOUT_SECONDS}s"
            ) from timeout_error

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
        # Authorization denials -> 403. Match by exception class name or a
        # conventional "Unauthorized:"/"Forbidden" message prefix.
        if err_type in ("PermissionError", "AuthorizationError") or msg.startswith(
            ("Unauthorized", "Forbidden")
        ):
            raise PermissionError(msg)
        # Client input errors -> 400.
        if err_type in ("ValueError", "KeyError"):
            raise ValueError(msg)
        raise RuntimeError(msg)
    return data


def handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    # CORS preflight (HTTP API can be configured to route OPTIONS here).
    http = (event.get("requestContext") or {}).get("http") or {}
    if http.get("method") == "OPTIONS":
        return _http_response(200, {})

    appsync_event = normalize_event(event)
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
    except PermissionError as e:
        logger.warning("Authorization denied for %s: %s", field, e)
        return _http_response(
            403, {"errors": [{"message": str(e), "errorType": "Unauthorized"}]}
        )
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
