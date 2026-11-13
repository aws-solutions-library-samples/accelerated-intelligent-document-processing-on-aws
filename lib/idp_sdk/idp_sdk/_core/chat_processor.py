# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Chat Processor Module

Sets up environment and runs the Agent Companion Chat orchestrator locally.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from typing import TYPE_CHECKING, Optional

import boto3

from idp_sdk._core.stack_info import StackInfo

if TYPE_CHECKING:
    from idp_sdk.models.chat import ChatResponse

logger = logging.getLogger(__name__)

#: Owner of the conversation memory the SDK's local chat writes and reads.
SDK_MEMORY_OWNER = "sdk-local"


class ChatProcessor:
    """Runs the agent orchestrator locally for chat interactions."""

    def __init__(self, stack_name: str, region: Optional[str] = None):
        self.stack_name = stack_name
        self.region = region
        self._orchestrator = None
        self._session_id = None
        self._env_ready = False

    def _setup_env(self):
        """Discover stack resources and set env vars for idp_common agents."""
        if self._env_ready:
            return

        info = StackInfo(self.stack_name, self.region)
        resources = info.get_resources()
        outputs = info._get_stack_outputs()

        # Discover DynamoDB tables by logical ID
        table_map = {}
        paginator = info.cfn.get_paginator("list_stack_resources")
        for page in paginator.paginate(StackName=self.stack_name):
            for r in page.get("StackResourceSummaries", []):
                lid = r.get("LogicalResourceId", "")
                pid = r.get("PhysicalResourceId", "")
                if lid in (
                    "ConfigurationTable",
                    "TrackingTable",
                    "IdHelperChatMemoryTable",
                ):
                    table_map[lid] = pid

        reporting_bucket = outputs.get("S3ReportingBucketName", "")
        reporting_db = outputs.get("ReportingDatabase", "")
        resolved_region = self.region or boto3.Session().region_name or "us-east-1"

        env = {
            "AWS_STACK_NAME": self.stack_name,
            "AWS_REGION": resolved_region,
            "BEDROCK_REGION": resolved_region,
            "CONFIGURATION_TABLE_NAME": table_map.get("ConfigurationTable", ""),
            "TRACKING_TABLE_NAME": table_map.get("TrackingTable", ""),
            "LOOKUP_FUNCTION_NAME": resources.get("LookupFunctionName", ""),
            "SETTINGS_PARAMETER_NAME": resources.get("SettingsParameter", ""),
            "ATHENA_DATABASE": reporting_db,
            "ATHENA_OUTPUT_LOCATION": (
                f"s3://{reporting_bucket}/athena-results/" if reporting_bucket else ""
            ),
            "ID_HELPER_CHAT_MEMORY_TABLE": table_map.get("IdHelperChatMemoryTable", ""),
            "CLOUDWATCH_LOG_GROUP_PREFIX": f"/aws/lambda/{self.stack_name}",
            "ENABLE_AGENT_MONITORING": "false",
            "STRANDS_LOG_LEVEL": "CRITICAL",
        }

        for k, v in env.items():
            if v:
                os.environ[k] = v

        self._env_ready = True

    def _ensure_orchestrator(
        self,
        session_id: Optional[str] = None,
        enable_code_intelligence: bool = False,
    ):
        """Create orchestrator if not already created."""
        if self._orchestrator:
            return

        self._setup_env()

        from idp_common.agents.analytics.config import get_analytics_config
        from idp_common.agents.factory import agent_factory

        self._session_id = session_id or f"sdk-{uuid.uuid4().hex[:12]}"
        config = get_analytics_config()
        session = boto3.Session(region_name=self.region)

        all_agents = agent_factory.list_available_agents()
        agent_ids = [a["agent_id"] for a in all_agents]
        if not enable_code_intelligence:
            agent_ids = [aid for aid in agent_ids if aid != "Code-Intelligence-Agent"]

        self._orchestrator = agent_factory.create_conversational_orchestrator(
            agent_ids=agent_ids,
            session_id=self._session_id,
            config=config,
            session=session,
            # Memory is stored per user and session. This runs with the
            # operator's own AWS credentials rather than as a signed-in web user,
            # so its conversations get an owner of their own, which no Cognito
            # sub (a UUID) can equal.
            user_id=SDK_MEMORY_OWNER,
        )

    def send_message(
        self, prompt: str, session_id: Optional[str] = None
    ) -> "ChatResponse":  # noqa: F821
        """
        Send a message and get the full response.

        Args:
            prompt: The user's message
            session_id: Optional session ID for conversation continuity

        Returns:
            ChatResponse with response text and session_id
        """
        from idp_sdk._core.async_utils import run_async
        from idp_sdk.models.chat import ChatResponse

        if session_id and session_id != self._session_id:
            self._orchestrator = None
            self._session_id = None

        self._ensure_orchestrator(session_id)

        text = run_async(self._collect_response(prompt))

        return ChatResponse(
            response=text,
            session_id=self._session_id,
        )

    async def _collect_response(self, prompt: str) -> str:
        """Stream and collect the full response text."""
        full_text = ""
        async for event in self._orchestrator.stream_async(prompt):
            if "data" in event:
                full_text += event["data"]
            elif "result" in event:
                break

        return re.sub(
            r"<thinking>.*?</thinking>", "", full_text, flags=re.DOTALL
        ).strip()

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id
