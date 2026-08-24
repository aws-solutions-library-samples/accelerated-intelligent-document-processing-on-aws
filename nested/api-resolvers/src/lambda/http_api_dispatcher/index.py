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
from idp_common.api_adapter import _http_response, normalize_event
from validation import validate_arguments

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_lambda = boto3.client("lambda")
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


def _invoke_resolver(function_arn: str, appsync_event: Dict[str, Any]) -> Any:
    """Synchronously invoke a resolver Lambda with an AppSync-shaped event."""
    resp = _lambda.invoke(
        FunctionName=function_arn,
        InvocationType="RequestResponse",
        Payload=json.dumps(appsync_event).encode("utf-8"),
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
