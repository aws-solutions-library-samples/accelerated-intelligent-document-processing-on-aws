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

Why this file exists at all, stated accurately: it makes the package boundary
explicit. It does **not** fix a broken wheel. Before it was added this directory
was an implicit PEP 420 namespace package, and
``lib/idp_common_pkg/pyproject.toml``'s ``[tool.setuptools.packages.find]`` table
defaults to ``namespaces=True``, so setuptools discovered it regardless — a wheel
built from the unmarked tree was measured to contain both submodules and to
import cleanly from a non-editable install. No release shipped without them, and
no user saw a ``ModuleNotFoundError`` from this.

The marker is still worth having, because depending on the ``namespaces`` default
is fragile: the same tree behaves differently under an explicit ``packages`` list,
under ``setup.cfg``'s strict ``find``, and under any tool that walks the
directories itself. ``scripts/tests/test_package_discovery.py`` enforces that as a
repository policy — every package directory under a first-party distribution
declares itself — and is documented there as a policy gate rather than as a model
of setuptools behaviour.
"""
