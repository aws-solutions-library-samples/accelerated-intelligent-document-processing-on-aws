# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda function to list all available agents.
"""

import json
import logging
import os

from idp_common.agents.factory import agent_factory
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Agent Chat is available to Admin/Author/Viewer; Reviewer is excluded.
_AGENT_CHAT_GROUPS = ("Admin", "Author", "Viewer")
_QUICK_START_AGENT_ID = "Quick-Start-Agent"


def _caller_in_groups(event, allowed):
    """Defense-in-depth RBAC check against the caller's Cognito groups.

    The single REST route's Cognito authorizer only *authenticates* — it does
    not enforce the group. So we enforce it server-side here (matching the
    pattern in calculate_capacity_resolver) so a Reviewer calling
    /op/listAvailableAgents directly is rejected (closes GAP-03).

    A Cognito invocation always carries a non-None ``identity``; direct Lambda
    invocations (backend/automation) have no identity and are gated by IAM, so
    only real UI callers are group-checked.
    """
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return bool(set(allowed).intersection(groups))


def handler(event, context):
    """
    List all available agents from the factory.

    Args:
        event: The normalized resolver event
        context: The Lambda context

    Returns:
        List of available agents with metadata

    Quick Start's agent is left out: it runs only from its own panel, which offers
    it to Admins, and never as an Agent Chat or analytics agent.
    """
    logger.info(f"Received event: {json.dumps(sanitize_event_for_logging(event))}")

    # Defense-in-depth RBAC: Reviewer is excluded from Agent Chat. Raise so the
    # dispatcher maps it to 403/Unauthorized (not an opaque 500).
    if event.get("identity") is not None and not _caller_in_groups(
        event, _AGENT_CHAT_GROUPS
    ):
        raise PermissionError(
            "Unauthorized: Agent Chat requires Admin, Author or Viewer group"
        )

    try:
        # Get list of available agents from factory
        available_agents = [
            agent
            for agent in agent_factory.list_available_agents()
            if agent.get("agent_id") != _QUICK_START_AGENT_ID
        ]

        logger.info(f"Found {len(available_agents)} available agents")
        return available_agents

    except Exception as e:
        error_msg = f"Error listing available agents: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)
