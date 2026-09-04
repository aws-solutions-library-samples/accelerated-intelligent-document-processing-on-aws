# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
IDP MCP Connector

A lightweight local MCP server that bridges IDE tools (Cline, Kiro) to the
IDP Accelerator's remote MCP Server on Amazon Bedrock AgentCore Gateway.
"""

__version__ = "0.6.7"
__all__ = ["CognitoAuth", "IDPMCPConnector", "run_connector"]

from .auth import CognitoAuth
from .connector import IDPMCPConnector, run_connector
