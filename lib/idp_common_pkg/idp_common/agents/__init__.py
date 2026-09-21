# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Agents module for IDP Common package.

This module provides agent-based functionality using the Strands framework.
All agents are built on top of Strands agents and tools.
"""

__version__ = "0.1.0"

# Cache for lazy-loaded submodules
_submodules = {}


def __getattr__(name):
    """Lazy load submodules only when accessed"""
    if name in [
        "analytics",
        "common",
        "factory",
        "orchestrator",
        "external_mcp",
    ]:
        if name not in _submodules:
            _submodules[name] = __import__(f"idp_common.agents.{name}", fromlist=["*"])
        return _submodules[name]

    raise AttributeError(f"module 'idp_common.agents' has no attribute '{name}'")


__all__ = [
    "analytics",
    "common",
    "factory",
    "orchestrator",
    "external_mcp",
]
