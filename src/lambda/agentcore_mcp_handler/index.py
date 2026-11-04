# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda function for AgentCore Gateway MCP tools.
Provides IDP operations through AgentCore Gateway's built-in MCP server.
"""

import json
import logging
import os
from typing import TYPE_CHECKING, Any, Dict

from tools import get_tool

# Configure logging. Importing idp_common.agents.common.config transitively
# imports strands (via the agents.common package __init__), so a missing/broken
# agents layer would fail THIS module's import and take down every tool with
# Runtime.ImportModuleError. Fall back to stdlib logging so the handler always
# loads; the failure then surfaces per-tool with a diagnosable message.
try:
    from idp_common.agents.common.config import configure_logging

    configure_logging()
except ImportError as _logging_import_error:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    logging.getLogger(__name__).warning(
        f"idp_common agents logging unavailable, using stdlib logging: "
        f"{_logging_import_error}"
    )

# The log redactor lives in idp_common.utils, which does not pull in strands, but
# it is guarded for the same reason as the logging import above: this module must
# load even when the agents layer is missing. The fallback is fail-closed — with
# no redactor available the handler logs a placeholder instead of the event, since
# the event is the tool's argument payload as the caller supplied it.
# Guarded at run time but NOT for the type checker: under TYPE_CHECKING the real
# signature is always the declared one. Declaring the name from the fallback instead
# makes `make typecheck`'s verdict depend on whether idp_common happens to resolve on
# the path — clean in CI, which does not put it there, and a reportAssignmentType
# error on a machine that does, over a parameter-name and **kwargs mismatch no
# runtime caller can observe.
if TYPE_CHECKING:
    from idp_common.utils.log_sanitizer import sanitize_event_for_logging
else:
    try:
        from idp_common.utils.log_sanitizer import sanitize_event_for_logging
    except ImportError:  # pragma: no cover - only reachable with no agents layer

        def sanitize_event_for_logging(event, **_kwargs):  # type: ignore[misc]
            return "<redactor unavailable: idp_common not importable>"


# Whether the stack runs with TenantIsolationMode=Strict. The shared helper is the
# one definition of that answer; the fallback exists only for the no-layer case
# above and fails CLOSED, because refusing every tool is the safe reading of a mode
# this function cannot evaluate.
_MODE_UNKNOWN = False
try:
    from idp_common.config_scope import unscoped_is_denied
except ImportError:  # pragma: no cover - only reachable with no agents layer
    _MODE_UNKNOWN = True

    def unscoped_is_denied(environ=None) -> bool:  # type: ignore[misc]
        return True


# Get logger for this module
logger = logging.getLogger(__name__)

# Version marker for deployment verification
CODE_VERSION = "2024-01-SDK-INTEGRATION-v2"
CODE_UPDATED = "2025-01-09T19:30:00Z"


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Route MCP tool invocations to appropriate handlers"""
    logger.info(f"=== Lambda Handler Started ===")
    logger.info(f"Code Version: {CODE_VERSION}")
    logger.info(f"Code Updated: {CODE_UPDATED}")
    logger.info(f"Received event: {json.dumps(sanitize_event_for_logging(event))}")

    # Log context details for debugging
    logger.info(f"Context type: {type(context)}")
    if hasattr(context, 'client_context'):
        logger.info(f"client_context: {context.client_context}")
        if context.client_context:
            custom = getattr(context.client_context, 'custom', {})
            logger.info(f"client_context.custom: {custom}")
    
    # Extract tool name from context (AgentCore Gateway pattern)
    # Format: ${target_name}__${tool_name} or _${tool_name}
    tool_name_full = None
    if hasattr(context, 'client_context') and context.client_context:
        custom = getattr(context.client_context, 'custom', {})
        tool_name_full = custom.get('bedrockAgentCoreToolName')
    
    if not tool_name_full:
        logger.error("No bedrockAgentCoreToolName in context")
        return {
            'statusCode': 400,
            'body': json.dumps({'error': 'No tool name in context'})
        }
    
    # Strip target name prefix (handle ___ delimiter)
    if '___' in tool_name_full:
        tool_name = tool_name_full.split('___', 1)[1]
    else:
        tool_name = tool_name_full
    
    # Strip leading underscore if present
    if tool_name.startswith('_'):
        tool_name = tool_name[1:]
    
    logger.info(f"Tool name from context: {tool_name_full} -> {tool_name}")

    # Tenant isolation, from Strict or an external grant source: every tool here
    # (search, batch status, results, reprocess) acts across the whole deployment,
    # and the gateway hands this function no user identity -- only a machine
    # client's -- so there is no tenant to scope a call to and no way to tell an
    # Admin from anyone else.
    if unscoped_is_denied():
        if _MODE_UNKNOWN:
            # Kept distinguishable from a configured refusal: the mode or the grant
            # source could not be read at all, so the refusal is the fail-closed
            # default.
            logger.error(
                f"Rejecting MCP tool {tool_name!r}: the tenant isolation mode cannot "
                f"be determined (idp_common is not importable), so every tool is "
                f"refused"
            )
            message = (
                "The tenant isolation mode of this deployment could not be "
                "determined, so MCP tools are refused. Check that the function's "
                "idp_common layer is attached."
            )
        else:
            logger.warning(
                f"Rejecting MCP tool {tool_name!r}: tenant isolation refuses "
                f"MCP calls, which carry no user identity to scope"
            )
            message = (
                "This deployment runs with tenant isolation. MCP tools act "
                "across every tenant's documents and the gateway supplies no "
                "user identity to scope them, so they are disabled."
            )
        return {
            'statusCode': 403,
            'body': json.dumps({'error': message, 'tool_name': tool_name})
        }

    try:
        logger.info(f"Looking up tool: {tool_name}")
        tool = get_tool(tool_name)
        logger.info(f"Found tool class: {tool.__class__.__name__}")
        logger.info(f"Tool module: {tool.__class__.__module__}")
        
        # Parameters are sent directly as top-level event fields
        # The whole event is the tool's parameter payload (see `tool.execute(**event)`
        # below), so it is caller-supplied data and is redacted before logging.
        logger.info(
            f"Executing tool: {tool_name} with event: "
            f"{json.dumps(sanitize_event_for_logging(event))}"
        )
        logger.info(f"Tool execution starting...")
        result = tool.execute(**event)
        logger.info(f"Tool execution completed successfully")
        logger.info(f"Result: {json.dumps(result)[:500]}...")  # Log first 500 chars
        
        return {
            'statusCode': 200,
            'body': json.dumps(result)
        }
    
    except Exception as e:
        logger.error(f"Tool execution failed: {e}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': str(e),
                'tool_name': tool_name
            })
        }