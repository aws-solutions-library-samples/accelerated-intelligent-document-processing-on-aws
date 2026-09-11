# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bedrock integration module for IDP Common package."""

from .client import BedrockClient, default_client, invoke_model
from .openai_responses import is_openai_responses_model, stream_responses_api
from .session import get_bedrock_session

# Add version info
__version__ = "0.1.0"

# Export the public API
__all__ = [
    "BedrockClient",
    "invoke_model",
    "default_client",
    "get_bedrock_session",
    "is_openai_responses_model",
    "stream_responses_api",
]

# Re-export key functions from the default client for backward compatibility.
# Callers reach these as MODULE functions (`bedrock.extract_text_from_response(...)`),
# so a method that exists on BedrockClient but is missing from this list raises
# AttributeError at the call site — see test_module_level_api.py, added after
# `extract_tool_use_from_response` shipped absent from here and made forced tool
# use (WS-05) fail every section it was enabled for.
extract_text_from_response = default_client.extract_text_from_response
extract_tool_use_from_response = default_client.extract_tool_use_from_response
generate_embedding = default_client.generate_embedding
format_prompt = default_client.format_prompt
