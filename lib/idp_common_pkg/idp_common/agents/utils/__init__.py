# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Shared utilities for the agent system.

Holds conversation-management and memory-persistence helpers used by
conversational agents built through ``idp_common.agents.factory``.

This module deliberately re-exports nothing. Both submodules import ``strands``
at module scope, and ``agent_factory`` imports them lazily inside
``create_conversational_agent`` so that the dependency is only paid for on the
chat path. Eager re-exports here would undo that. Import the submodules
directly, as their own docstrings show:

    from idp_common.agents.utils.conversation_manager import (
        DropAndSlideConversationManager,
    )
    from idp_common.agents.utils.memory_provider import DynamoDBMemoryHookProvider

The file itself is not optional: ``lib/idp_common_pkg/pyproject.toml`` discovers
packages with setuptools ``packages.find``, which skips any directory without an
``__init__.py``. Without it this subpackage was omitted from the built wheel and
the lazy imports above raised ``ModuleNotFoundError`` on any non-editable
install. See ``scripts/tests/test_package_discovery.py``, which fails if a
directory under ``idp_common/`` holding ``.py`` files is ever again left
undiscoverable.
"""
