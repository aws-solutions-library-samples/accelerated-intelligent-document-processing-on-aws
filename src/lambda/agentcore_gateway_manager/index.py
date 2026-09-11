# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import boto3
import cfnresponse
import time
import logging
import os
from bedrock_agentcore_starter_toolkit.operations.gateway.client import GatewayClient

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Tool schema for the IDPTools Lambda target. Single source of truth:
# used when the gateway target is CREATED and re-applied when the target
# is UPDATED (see update_gateway_target_if_needed) so schema changes in
# this file reach existing deployments on a stack update.
IDP_TOOLS_SCHEMA = [
    {
        "name": "search",
        "description": "Search and query processed documents using natural language. Returns analytics, metrics, and document information from the IDP system.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language query about processed documents, metrics, or system status"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "process",
        "description": "Process documents through the IDP pipeline. Accepts S3 locations or base64-encoded content. Intelligently handles missing information by requesting specific details.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "S3 URI for batch processing (e.g., 's3://bucket/documents/'). Optional if content is provided."
                },
                "content": {
                    "type": "string",
                    "description": "Base64-encoded document content for single document processing. Optional if location is provided."
                },
                "name": {
                    "type": "string",
                    "description": "Document filename with extension (e.g., 'invoice.pdf', 'contract.docx'). Required if content is provided; optional for S3 locations."
                },
                "prefix": {
                    "type": "string",
                    "description": "Optional batch ID prefix (default: 'mcp-batch')"
                }
            },
            "required": []
        }
    },
    {
        "name": "reprocess",
        "description": "Reprocess documents from a specific pipeline step. Supports classification or extraction reprocessing. Returns batch ID for status tracking.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "step": {
                    "type": "string",
                    "description": "Pipeline step to reprocess from (classification or extraction)"
                },
                "document_ids": {
                    "type": "string",
                    "description": "Comma-separated list of document IDs to reprocess (alternative to batch_id)"
                },
                "batch_id": {
                    "type": "string",
                    "description": "Batch ID to get document IDs from (alternative to document_ids)"
                },
                "region": {
                    "type": "string",
                    "description": "AWS region (optional)"
                }
            },
            "required": ["step"]
        }
    },
    {
        "name": "status",
        "description": "Get processing status for a batch of documents. Returns progress, timing, and error information.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "batch_id": {
                    "type": "string",
                    "description": "Batch identifier (e.g., 'mcp-batch-20250124-143000')"
                },
                "options": {
                    "type": "object",
                    "description": "Optional status parameters",
                    "properties": {
                        "detailed": {
                            "type": "boolean",
                            "description": "Include per-document details (default: false)"
                        },
                        "include_errors": {
                            "type": "boolean",
                            "description": "Include error details (default: true)"
                        }
                    }
                },
                "region": {
                    "type": "string",
                    "description": "AWS region (optional)"
                }
            },
            "required": ["batch_id"]
        }
    },
    {
        "name": "get_results",
        "description": "Retrieve processing results and extracted metadata for documents. Use this tool when users ask for results, metadata, extracted fields, or processing outcomes. Provide either batch_id (all documents in a batch, paginated) or document_id (a single document — no batch id needed). Returns document classification, extracted fields with values, field-level confidence scores, page counts, and processing status for each document. Batch mode includes a batch-level summary with average confidence and document class distribution, and supports pagination for large batches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "batch_id": {
                    "type": "string",
                    "description": "Batch identifier (e.g., 'mcp-batch-20250124-143022') to retrieve results for every document in the batch. Provide either batch_id or document_id."
                },
                "document_id": {
                    "type": "string",
                    "description": "Single document identifier — the document's S3 object key (e.g., 'invoice.pdf') or its s3:// output-prefix URI (e.g., 's3://output-bucket/invoice.pdf/'). Use when no batch id is known, e.g. when following up on a single processed document. Provide either batch_id or document_id."
                },
                "section_id": {
                    "type": "integer",
                    "description": "Section number within documents (default: 1). Use for multi-section documents like healthcare packages. Section 1 contains primary extraction, sections 2+ contain additional document types within the same file."
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum documents to return per page (default: 10, max: 100). Batch mode only. Use lower values for faster responses, higher values to retrieve more documents in one call."
                },
                "next_token": {
                    "type": "string",
                    "description": "Pagination token from previous request for retrieving next page of results. Batch mode only. Omit for first page."
                }
            },
            "required": []
        }
    }
]


def handler(event, context):
    """CloudFormation custom resource handler for AgentCore Gateway"""
    logger.info(f"Received event: {json.dumps(event)}")

    props = event.get('ResourceProperties', {})
    gateway_name = f"{props.get('StackName', 'UNKNOWN')}-analytics-gateway"

    try:
        request_type = event['RequestType']

        if request_type == 'Delete':
            try:
                delete_gateway(props, gateway_name)
                cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, physicalResourceId=gateway_name)
            except Exception as e:
                logger.error(f"Delete failed: {e}", exc_info=True)
                cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, physicalResourceId=gateway_name, reason=str(e))
            return

        # Create or Update
        gateway_config = create_or_update_gateway(props, gateway_name)

        cfnresponse.send(event, context, cfnresponse.SUCCESS, {
            'GatewayUrl': gateway_config.get('gateway_url'),
            'GatewayId': gateway_config.get('gateway_id'),
            'GatewayArn': gateway_config.get('gateway_arn')
        }, physicalResourceId=gateway_name)

    except Exception as e:
        logger.error(f"Error: {str(e)}", exc_info=True)
        # Check if this is a bedrock-agentcore access issue
        if 'bedrock-agentcore' in str(e).lower() and ('access' in str(e).lower() or 'unauthorized' in str(e).lower()):
            logger.warning("bedrock-agentcore service appears unavailable - continuing without MCP gateway")
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {
                'GatewayUrl': 'N/A - Service not available',
                'GatewayId': 'N/A',
                'GatewayArn': 'N/A'
            }, physicalResourceId=gateway_name)
        else:
            cfnresponse.send(event, context, cfnresponse.FAILED, {},
                             physicalResourceId=gateway_name,
                             reason=str(e))


def create_or_update_gateway(props, gateway_name):
    """Create or update AgentCore Gateway using existing Cognito resources"""
    region = props['Region']
    lambda_arn = props['LambdaArn']  # The expected (new) Lambda ARN from CloudFormation

    # Check if gateway already exists — paginate through ALL gateways to avoid
    # a ConflictException when the account has more than 10 gateways and the
    # target gateway is not in the first page of list_gateways results.
    try:
        control_client = boto3.client("bedrock-agentcore-control", region_name=region)
        all_gateways = []
        paginator = control_client.get_paginator("list_gateways")
        for page in paginator.paginate():
            all_gateways.extend(page.get("items", []))
        existing_gateways = [g for g in all_gateways if g.get("name") == gateway_name]

        if existing_gateways:
            existing_gateway = existing_gateways[0]
            gateway_id = existing_gateway.get('gatewayId')

            if gateway_id:
                # Gateway is confirmed to exist — never fall through to create from here.
                # If get_gateway fails transiently, return basic info from list_gateways
                # rather than attempting a CreateGateway that would conflict.
                try:
                    gateway_details = control_client.get_gateway(gatewayIdentifier=gateway_id)
                    if gateway_details and gateway_details.get('gatewayUrl'):
                        # Check if the Lambda target ARN needs updating (e.g. after a stack update
                        # that replaced the Lambda function due to a logical resource ID rename)
                        update_result = update_gateway_target_if_needed(
                            control_client, gateway_id, lambda_arn
                        )
                        if update_result:
                            logger.info(f"Updated gateway target to new Lambda ARN: {lambda_arn}")

                        return {
                            'gateway_url': gateway_details.get('gatewayUrl'),
                            'gateway_id': gateway_details.get('gatewayId'),
                            'gateway_arn': gateway_details.get('gatewayArn')
                        }
                    else:
                        # Gateway exists but gatewayUrl not available yet (e.g. still initializing).
                        # Return what we have from list_gateways — do NOT attempt to re-create.
                        logger.warning(
                            f"Gateway {gateway_id} exists but gatewayUrl is not yet available. "
                            f"Returning partial details."
                        )
                        return {
                            'gateway_url': 'N/A - Gateway initializing',
                            'gateway_id': gateway_id,
                            'gateway_arn': existing_gateway.get('gatewayArn', 'N/A')
                        }
                except Exception as e:
                    # get_gateway failed transiently. The gateway is known to exist so we must
                    # NOT fall through to create. Return what list_gateways already gave us.
                    logger.warning(
                        f"Error getting gateway details for {gateway_id}: {e}. "
                        f"Returning partial details from list."
                    )
                    return {
                        'gateway_url': 'N/A - Error retrieving details',
                        'gateway_id': gateway_id,
                        'gateway_arn': existing_gateway.get('gatewayArn', 'N/A')
                    }

    except Exception as e:
        logger.warning(f"Error checking for existing gateway: {e}")

    # Gateway does not exist — create it now.
    logger.info(f"Gateway {gateway_name} does not exist, creating new one")
    # GatewayClient is only initialized when we actually need to create a new gateway.
    client = GatewayClient(region_name=region)
    return create_gateway(props, gateway_name, client)


def update_gateway_target_if_needed(control_client, gateway_id, expected_lambda_arn):
    """
    Check if any gateway target's Lambda ARN or tool schema differs from what
    this code expects. If so, update it. Returns True if an update was performed.

    Two staleness cases:
    - The Lambda function was replaced during a stack update (e.g. due to a
      CloudFormation logical resource ID rename), so the target points at the
      old deleted Lambda's ARN.
    - The IDPTools tool schema changed in this file (new tools or new/changed
      parameters). The schema is only sent at target CREATE time, so without
      this re-sync, existing deployments keep enforcing the old schema forever.
    """
    try:
        response = control_client.list_gateway_targets(gatewayIdentifier=gateway_id)
        targets = response.get("items", [])

        for target in targets:
            target_id = target.get("targetId")
            target_name = target.get("name", "")

            # Get full target details to check Lambda ARN
            try:
                target_details = control_client.get_gateway_target(
                    gatewayIdentifier=gateway_id,
                    targetId=target_id
                )
                # Navigate response structure to find the Lambda ARN.
                # Confirmed API response path: targetConfiguration.mcp.lambda.lambdaArn
                target_config = target_details.get("targetConfiguration", {})
                mcp_lambda = target_config.get("mcp", {}).get("lambda", {})
                current_lambda_arn = mcp_lambda.get("lambdaArn", "")
                existing_tool_schema = mcp_lambda.get("toolSchema", {})

                arn_stale = bool(
                    current_lambda_arn and current_lambda_arn != expected_lambda_arn
                )
                # Only the IDPTools target is owned by this code; leave any
                # other target's schema alone.
                desired_tool_schema = existing_tool_schema
                schema_stale = False
                if target_name == "IDPTools":
                    desired_tool_schema = {"inlinePayload": IDP_TOOLS_SCHEMA}
                    schema_stale = existing_tool_schema != desired_tool_schema

                if arn_stale or schema_stale:
                    logger.info(
                        f"Target {target_name} ({target_id}) is stale "
                        f"(arn_stale={arn_stale}, schema_stale={schema_stale}). "
                        f"Updating to Lambda ARN {expected_lambda_arn}..."
                    )
                    # Preserve credentialProviderConfigurations — required field
                    # for update_gateway_target.
                    existing_credential_configs = target_details.get(
                        "credentialProviderConfigurations", []
                    )
                    update_kwargs = dict(
                        gatewayIdentifier=gateway_id,
                        targetId=target_id,
                        name=target_name,
                        targetConfiguration={
                            "mcp": {
                                "lambda": {
                                    "lambdaArn": expected_lambda_arn,
                                    "toolSchema": desired_tool_schema,
                                }
                            }
                        },
                    )
                    if existing_credential_configs:
                        update_kwargs["credentialProviderConfigurations"] = existing_credential_configs
                    control_client.update_gateway_target(**update_kwargs)
                    logger.info(f"Successfully updated target {target_id}")
                    return True
                else:
                    logger.info(f"Target {target_name} is current, no update needed")

            except Exception as e:
                logger.warning(f"Could not get/update target {target_id}: {e}")

    except Exception as e:
        logger.warning(f"Could not list gateway targets for update check: {e}")

    return False


def create_gateway(props, gateway_name, client: GatewayClient):
    """Create new AgentCore Gateway"""
    region = props['Region']
    lambda_arn = props['LambdaArn']
    user_pool_id = props['UserPoolId']
    client_id = props['ClientId']
    execution_role_arn = props['ExecutionRoleArn']
    connector_client_id = props.get('ConnectorClientId')

    # Build allowed clients list — include MCP connector client if provided
    allowed_clients = [client_id]
    if connector_client_id:
        allowed_clients.append(connector_client_id)

    # Create JWT authorizer config using existing Cognito resources
    authorizer_config = {
        "customJWTAuthorizer": {
            "discoveryUrl": f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/.well-known/openid-configuration",
            "allowedClients": allowed_clients
        }
    }

    # Create gateway. Semantic search (natural-language tool discovery) is not
    # available for AgentCore Gateway in GovCloud regions — CreateGateway there
    # raises ValidationException: "SEMANTIC search type is not supported". Fall
    # back to a gateway without semantic search rather than failing the whole
    # stack; the gateway and its Lambda target still work, just without the
    # x_amz_bedrock_agentcore_search tool.
    try:
        gateway = client.create_mcp_gateway(
            name=gateway_name,
            role_arn=execution_role_arn,
            authorizer_config=authorizer_config,
            enable_semantic_search=True,
        )
    except Exception as e:
        if "SEMANTIC search type is not supported" in str(e):
            logger.warning(
                "Semantic search unsupported in this region — creating gateway "
                "without it: %s",
                e,
            )
            gateway = client.create_mcp_gateway(
                name=gateway_name,
                role_arn=execution_role_arn,
                authorizer_config=authorizer_config,
                enable_semantic_search=False,
            )
        else:
            raise

    logger.info(f"Gateway created: {gateway.get('gatewayUrl')}")

    # Wait for the execution role to propagate before attaching the target.
    #
    # We deliberately do NOT call client.fix_iam_permissions(gateway) here. That
    # helper rewrites the execution role's trust policy and puts an inline
    # LambdaInvokePolicy for the toolkit's own sample function
    # (AgentCoreLambdaTestFunction), neither of which this stack needs:
    # AgentCoreGatewayExecutionRole already declares the identical trust policy
    # and an InvokeLambdaPolicy for AgentCoreMCPHandlerFunction. Calling it would
    # require iam:UpdateAssumeRolePolicy / iam:PutRolePolicy on this function's
    # own role, which is the privilege-escalation path flagged by AppSec finding
    # b2b397a9, so those permissions are intentionally absent from the policy in
    # template.yaml.
    logger.info("Waiting for IAM propagation...")
    time.sleep(30)

    # Add IDP tools Lambda target with all tools
    logger.info("Adding IDP tools Lambda target...")
    client.create_mcp_gateway_target(
        gateway=gateway,
        name="IDPTools",
        target_type="lambda",
        target_payload={
            "lambdaArn": lambda_arn,
            "toolSchema": {"inlinePayload": IDP_TOOLS_SCHEMA},
        },
    )

    logger.info("Gateway setup complete")

    return {
        'gateway_url': gateway.get('gatewayUrl'),
        'gateway_id': gateway.get('gatewayId'),
        'gateway_arn': gateway.get('gatewayArn')
    }


def delete_gateway(props, gateway_name):
    """Delete AgentCore Gateway using toolkit"""
    region = props['Region']
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    
    # Paginate through all gateways
    all_gateways = []
    paginator = client.get_paginator("list_gateways")
    for page in paginator.paginate():
        all_gateways.extend(page.get("items", []))
    
    items = [g for g in all_gateways if g.get("name") == gateway_name]
    
    if not items:
        logger.info(f"Gateway {gateway_name} not found")
        return
    
    gateway_id = items[0].get("gatewayId")
    logger.info(f"Deleting gateway: {gateway_id}")
    
    # Delete all targets first (typically only one target per gateway)
    response = client.list_gateway_targets(gatewayIdentifier=gateway_id)
    targets = response.get("items", [])
    logger.info(f"Found {len(targets)} targets to delete")
    
    deletion_errors = []
    for target in targets:
        target_id = target["targetId"]
        logger.info(f"Deleting target: {target_id}")
        try:
            client.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
            time.sleep(2)
        except Exception as e:
            error_msg = f"target {target_id}: {str(e)}"
            logger.warning(f"Failed to delete {error_msg}")
            deletion_errors.append(error_msg)
    
    # Wait for targets to be fully deleted
    time.sleep(10)
    
    # Delete gateway
    try:
        client.delete_gateway(gatewayIdentifier=gateway_id)
        logger.info(f"Gateway deleted: {gateway_id}")
    except Exception as e:
        error_msg = f"gateway: {str(e)}"
        logger.warning(f"Failed to delete {error_msg}")
        deletion_errors.append(error_msg)
    
    # Wait for gateway deletion to complete
    time.sleep(5)
    
    if deletion_errors:
        raise Exception(f"Partial deletion errors: {'; '.join(deletion_errors)}")
