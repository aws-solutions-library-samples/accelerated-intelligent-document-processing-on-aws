# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Pytest configuration file for the IDP Common package tests.
"""

import importlib
import os
import sys
from unittest.mock import MagicMock

import pytest

# Set up AWS credentials and region BEFORE any imports that might use boto3
# This must be done at module load time, not in a fixture, because fixtures
# run after module imports and some code may initialize boto3 clients at import time
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Mock external dependencies that may not be available in test environments
# These mocks need to be set up before any imports that might use these packages


def _stub_if_absent(*module_names: str) -> None:
    """Install a MagicMock for each module ONLY when it is genuinely absent.

    This used to stub unconditionally, and for ``strands`` that was actively
    harmful: ``strands-agents`` is a real dependency of the ``[all]`` extra, which
    both ``make dev`` and CI install, so the stub replaced a package that WAS
    importable. Every agentic test guards itself with
    ``pytest.importorskip``-style detection (``from strands.types.agent import
    AgentInput``), that import saw a MagicMock without the attribute, and 42 tests
    reported "strands-agents package not installed" and skipped — permanently, in
    every environment, including CI.

    Skipped tests read as green. So the agentic extraction path had 42 tests that
    could never fail, which is the same failure mode as having no tests at all
    except that it looks covered. Stubbing only what is missing keeps the suite
    runnable in a bare environment while letting a properly installed one actually
    exercise the code.
    """
    for name in module_names:
        if name in sys.modules:
            continue
        try:
            importlib.import_module(name)
        except ImportError:
            sys.modules[name] = MagicMock()


_stub_if_absent(
    "strands",
    "strands.models",
    "strands.hooks",
    "strands.hooks.events",
)

# Agent submodules some test modules used to stub for themselves; centralized
# here so no single module can replace a real installed package for every module
# imported after it.
_stub_if_absent(
    "strands.agent",
    "strands.agent.conversation_manager",
    "strands.tools",
    "strands.tools.mcp",
)

# bedrock_agentcore (secure code execution) is not a test dependency, so in
# practice these are always stubbed — routed through the same helper so the rule
# is uniform rather than a second, differently-behaved mechanism.
_stub_if_absent(
    "bedrock_agentcore",
    "bedrock_agentcore.tools",
    "bedrock_agentcore.tools.code_interpreter_client",
)

# PIL module is now used directly for document conversion functionality
# No mocking needed as PIL is a required dependency for the OCR module


@pytest.fixture(scope="session", autouse=True)
def aws_credentials():
    """Set up AWS credentials and region for testing."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"  # nosec B105 - dummy moto credential  # pragma: allowlist secret
    os.environ["AWS_SECURITY_TOKEN"] = "testing"  # nosec B105 - dummy moto credential
    os.environ["AWS_SESSION_TOKEN"] = "testing"  # nosec B105 - dummy moto credential
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["AWS_REGION"] = (
        "us-east-1"  # Also set AWS_REGION for code that checks this variable
    )
