# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import os
from typing import Any, Dict, List

import boto3
from log_sanitizer import sanitize_event_for_logging


def _caller_in_groups(event, allowed):
    """Defense-in-depth RBAC check against the caller's Cognito groups.

    The schema restricts calculateCapacity via @aws_cognito_user_pools(
    cognito_groups: ["Admin","Author","Viewer"]), but the REST dispatcher's
    Cognito authorizer only authenticates — it does not enforce the group. So we
    also enforce the group server-side (matching the pattern in
    abort_test_runs / abort_workflow_resolver) so a Reviewer calling
    /op/calculateCapacity directly is rejected (closes GAP-05).
    """
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return bool(set(allowed).intersection(groups))


def sanitize_quota_requirements(quota_reqs: List[Dict]) -> List[Dict]:
    """
    Sanitize quota requirements to only include fields defined in GraphQL schema.
    Schema: service!, category!, currentQuota!, requiredQuota!, statusText!, modelId
    """
    if not quota_reqs:
        return []
    
    sanitized = []
    for req in quota_reqs:
        if not isinstance(req, dict):
            continue
        # Only include fields that match the GraphQL schema
        sanitized_req = {
            "service": str(req.get("service", "Unknown")),
            "category": str(req.get("category", "Unknown")),
            "currentQuota": str(req.get("currentQuota", "0")),
            "requiredQuota": str(req.get("requiredQuota", "0")),
            "statusText": str(req.get("statusText", "Unknown")),
        }
        # modelId is optional
        if req.get("modelId"):
            sanitized_req["modelId"] = str(req["modelId"])
        sanitized.append(sanitized_req)
    return sanitized


def sanitize_latency_distribution(latency: Dict) -> Dict:
    """
    Sanitize latency distribution to only include fields defined in GraphQL schema.
    Schema: p50, p75, p90, p95, p99, baseLatency, queueLatency, totalLatency, maxAllowed, exceedsLimit
    """
    if not latency or not isinstance(latency, dict):
        return {
            "p50": "0s", "p75": "0s", "p90": "0s", "p95": "0s", "p99": "0s",
            "baseLatency": "0s", "queueLatency": "0s", "totalLatency": "0s",
            "exceedsLimit": False, "maxAllowed": "0s",
        }
    
    return {
        "p50": str(latency.get("p50", "0s")),
        "p75": str(latency.get("p75", "0s")),
        "p90": str(latency.get("p90", "0s")),
        "p95": str(latency.get("p95", "0s")),
        "p99": str(latency.get("p99", "0s")),
        "baseLatency": str(latency.get("baseLatency", "0s")),
        "queueLatency": str(latency.get("queueLatency", "0s")),
        "totalLatency": str(latency.get("totalLatency", "0s")),
        "maxAllowed": str(latency.get("maxAllowed", "0s")),
        "exceedsLimit": bool(latency.get("exceedsLimit", False)),
    }


def sanitize_metrics(metrics: List[Dict]) -> List[Dict]:
    """
    Sanitize metrics to only include fields defined in GraphQL schema.
    Schema: label!, value!
    """
    if not metrics:
        return [{"label": "Status", "value": "No Data"}]
    
    sanitized = []
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        if metric.get("label") and metric.get("value"):
            sanitized.append({
                "label": str(metric["label"]),
                "value": str(metric["value"]),
            })
    return sanitized if sanitized else [{"label": "Status", "value": "No Data"}]


def sanitize_calculation_details(details: Dict) -> Dict:
    """Sanitize calculation details to match schema."""
    if not details or not isinstance(details, dict):
        return {"quotasUsed": {"bedrock_models": {}}}
    
    quotas_used = details.get("quotasUsed", {})
    if not isinstance(quotas_used, dict):
        quotas_used = {}
    
    bedrock_models = quotas_used.get("bedrock_models", {})
    if not isinstance(bedrock_models, dict):
        bedrock_models = {}
    
    return {"quotasUsed": {"bedrock_models": bedrock_models}}


def sanitize_response(response: Dict) -> Dict:
    """
    Sanitize the entire response to match GraphQL CapacityResult schema exactly.
    This prevents AppSync from returning null due to type mismatches.
    """
    if not response or not isinstance(response, dict):
        return build_error_response("Invalid response from capacity calculator")
    
    return {
        "success": bool(response.get("success", False)),
        "errorMessage": response.get("errorMessage"),
        "metrics": sanitize_metrics(response.get("metrics", [])),
        "quotaRequirements": sanitize_quota_requirements(response.get("quotaRequirements", [])),
        "latencyDistribution": sanitize_latency_distribution(response.get("latencyDistribution", {})),
        "calculationDetails": sanitize_calculation_details(response.get("calculationDetails", {})),
        "recommendations": [str(r) for r in response.get("recommendations", []) if r] or ["No recommendations"],
    }


def build_error_response(error_message: str) -> Dict[str, Any]:
    """
    Build a complete error response that matches the GraphQL CapacityResult schema exactly.
    This ensures AppSync doesn't return null due to missing fields.
    """
    return {
        "success": False,
        "errorMessage": str(error_message)[:500],  # Truncate long errors
        "metrics": [
            {"label": "Status", "value": "Error"},
            {"label": "Details", "value": str(error_message)[:100]},
        ],
        "quotaRequirements": [],
        "latencyDistribution": {
            "p50": "0s",
            "p75": "0s",
            "p90": "0s",
            "p95": "0s",
            "p99": "0s",
            "baseLatency": "0s",
            "queueLatency": "0s",
            "totalLatency": "0s",
            "exceedsLimit": False,
            "maxAllowed": "0s",
        },
        "calculationDetails": {
            "quotasUsed": {
                "bedrock_models": {}
            }
        },
        "recommendations": [f"❌ Error: {str(error_message)[:200]}"],
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    GraphQL resolver for calculateCapacity query.
    Invokes the calculate_capacity Lambda function and returns the result.
    """
    try:
        # Redacted before logging: an AppSync event carries `identity.claims`, so
        # the Cognito sub, email and group list of every caller would otherwise be
        # written to CloudWatch on every invocation.
        print(
            "[RESOLVER] Received event: "
            f"{json.dumps(sanitize_event_for_logging(event), default=str)[:1000]}"
        )

        # Defense-in-depth RBAC: calculateCapacity is Admin/Author/Viewer (schema).
        # A Cognito invocation always carries a non-None 'identity'; direct Lambda
        # invocations (CI/automation) have no identity and are gated by IAM
        # (lambda:InvokeFunction), so we only group-check real UI callers.
        # Raise (not a 200 error dict) so the dispatcher maps it to 403/Unauthorized.
        if event.get('identity') is not None and not _caller_in_groups(
            event, ("Admin", "Author", "Viewer")
        ):
            raise PermissionError(
                "Unauthorized: calculateCapacity requires Admin, Author or Viewer group"
            )

        # Get the input from GraphQL arguments
        input_data = event.get('arguments', {}).get('input', '{}')
        print(f"[RESOLVER] Input data type: {type(input_data).__name__}, length: {len(str(input_data))}")
        
        # Get the calculate_capacity function name from environment
        calculate_capacity_function = os.environ.get('CALCULATE_CAPACITY_FUNCTION_NAME')
        if not calculate_capacity_function:
            print("[RESOLVER] ERROR: Calculate capacity function not configured")
            return build_error_response('Calculate capacity function not configured')
        
        print(f"[RESOLVER] Invoking function: {calculate_capacity_function}")
        
        # Invoke the calculate_capacity Lambda function
        lambda_client = boto3.client('lambda')
        
        try:
            response = lambda_client.invoke(
                FunctionName=calculate_capacity_function,
                InvocationType='RequestResponse',
                Payload=json.dumps({
                    'body': input_data
                })
            )
        except Exception as invoke_error:
            print(f"[RESOLVER] Lambda invoke error: {invoke_error}")
            return build_error_response(f'Failed to invoke capacity calculator: {str(invoke_error)}')
        
        status_code = response.get('StatusCode')
        print(f"[RESOLVER] Lambda response status: {status_code}")
        
        # Check for Lambda function errors (e.g., timeout, out of memory)
        if 'FunctionError' in response:
            error_type = response.get('FunctionError', 'Unknown')
            print(f"[RESOLVER] FunctionError detected: {error_type}")
            
            try:
                payload_raw = response['Payload'].read()
                payload = json.loads(payload_raw) if payload_raw else {}
            except Exception:
                payload = {}
            
            error_msg = f'Lambda function error: {error_type}'
            if payload and isinstance(payload, dict):
                if 'errorMessage' in payload:
                    error_msg += f' - {payload["errorMessage"]}'
                elif 'errorType' in payload:
                    error_msg += f' ({payload["errorType"]})'
                if 'stackTrace' in payload:
                    print(f"[RESOLVER] Stack trace: {payload['stackTrace']}")
            
            print(f"[RESOLVER] ERROR: {error_msg}")
            return build_error_response(error_msg)
        
        # Parse the response payload
        try:
            payload_raw = response['Payload'].read()
            print(f"[RESOLVER] Raw payload length: {len(payload_raw)}")
            payload = json.loads(payload_raw) if payload_raw else None
        except json.JSONDecodeError as je:
            print(f"[RESOLVER] JSON decode error: {je}")
            return build_error_response(f'Invalid JSON from capacity calculator: {str(je)}')
        except Exception as pe:
            print(f"[RESOLVER] Payload read error: {pe}")
            return build_error_response(f'Failed to read response: {str(pe)}')
        
        if payload is None:
            print("[RESOLVER] Payload is None")
            return build_error_response('Empty response from capacity calculator')
        
        print(f"[RESOLVER] Payload type: {type(payload).__name__}")
        if isinstance(payload, dict):
            print(f"[RESOLVER] Payload keys: {list(payload.keys())}")
        
        if status_code == 200:
            # Parse body if present (HTTP API Gateway format)
            if 'body' in payload:
                try:
                    body_str = payload['body']
                    if isinstance(body_str, str):
                        body_data = json.loads(body_str)
                    else:
                        body_data = body_str
                    print(f"[RESOLVER] Parsed body, success={body_data.get('success')}")
                except (json.JSONDecodeError, TypeError) as e:
                    print(f"[RESOLVER] Error parsing body: {e}")
                    return build_error_response(f'Error parsing response body: {str(e)}')
            else:
                # Direct invocation format
                body_data = payload
                print(f"[RESOLVER] Using payload directly, success={body_data.get('success') if isinstance(body_data, dict) else 'N/A'}")
            
            # Validate and sanitize the response to match GraphQL schema exactly
            if not isinstance(body_data, dict):
                print(f"[RESOLVER] body_data is not a dict: {type(body_data)}")
                return build_error_response(f'Invalid response type: {type(body_data).__name__}')
            
            # Sanitize all fields to match GraphQL schema
            sanitized = sanitize_response(body_data)
            print(f"[RESOLVER] Returning sanitized response, success={sanitized.get('success')}")
            return sanitized
        else:
            error_msg = f'Capacity calculation service returned status {status_code}'
            if payload and isinstance(payload, dict) and 'errorMessage' in payload:
                error_msg += f' - {payload["errorMessage"]}'
            print(f"[RESOLVER] ERROR: {error_msg}")
            return build_error_response(error_msg)
            
    except PermissionError:
        # Authorization denial must propagate so the dispatcher returns
        # 403/Unauthorized — NOT be swallowed into a 200 build_error_response.
        raise
    except Exception as e:
        print(f"[RESOLVER] Unhandled exception: {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()
        return build_error_response(f'Internal error: {str(e)}')
