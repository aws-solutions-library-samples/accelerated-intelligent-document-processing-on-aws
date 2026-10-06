# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Pytest configuration file for the IDP Common package tests.
"""

import importlib
import os
import sys
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import MagicMock

import pytest

#: The AWS credential variables this file forces to sentinels for the unit suite.
#: Named once because three things below have to agree about the set: the
#: snapshot, the sentinel assignment, and the integration reconciliation.
_AWS_CREDENTIAL_VARS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SECURITY_TOKEN",
    "AWS_SESSION_TOKEN",
)

#: Whatever real credentials the invoking environment exported, captured BEFORE
#: the sentinels below overwrite them. The `aws_credentials` fixture assigns
#: unconditionally (not `setdefault`), so by the time any test runs, exported
#: real credentials are gone — and an integration test cannot get them back from
#: the environment. Recovering them from the boto3 profile/role chain only works
#: for a machine that HAS a profile or an instance role, which is why
#: `AWS_PROFILE=...` worked and the exported `AWS_ACCESS_KEY_ID` form documented
#: alongside it did not.
_REAL_AWS_ENV = {var: os.environ.get(var) for var in _AWS_CREDENTIAL_VARS}

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


# Fail fast when the idp_common being tested is not the one in this checkout. The
# rationale, and why this is an error rather than a warning, is in the shared helper.
#
# This suite usually shadows the installed package by accident rather than by design:
# tests/ and tests/unit/ carry __init__.py, so pytest's prepend import mode inserts the
# PACKAGE ROOT on sys.path and the local idp_common wins. That is luck, not a control --
# it disappears if those files go away or import mode changes -- so the check is stated
# explicitly here instead of being left to infer.
# The guard is skipped only when the helper is genuinely ABSENT -- an export with no
# scripts/ tree. A blanket `except ImportError` around the import and the call was
# broader than that claim: it also swallowed a renamed symbol, or any ImportError raised
# from inside the helper, either of which turned the guard into a no-op in this suite
# with nothing printed and every test still green.
_GATE_DIR = Path(__file__).resolve().parents[3] / "scripts" / "tests"
if (_GATE_DIR / "first_party_provenance.py").is_file():
    sys.path.insert(0, str(_GATE_DIR))
    try:
        from first_party_provenance import assert_resolves_in

        assert_resolves_in("idp_common", __file__)
    finally:
        sys.path.pop(0)


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


# ---------------------------------------------------------------------------
# Integration-tier credential reconciliation
# ---------------------------------------------------------------------------
# This reconciliation is keyed on the ``integration`` MARKER, not on the
# directory, and that is the whole point of it living here. It used to be an
# autouse session fixture in ``tests/integration/conftest.py``, so it reached
# integration tests by virtue of where their file sat. Two integration-marked
# tests live under ``tests/unit/`` (the live-Bedrock agentic extraction pair,
# which belong beside the agentic conftest that guards on a real ``strands``),
# and `make test-integration` runs ``pytest -m "integration"`` over the whole
# tree — so those two were handed the sentinel ``testing`` key, signed real
# Bedrock calls with it and failed with ``InvalidClientTokenId``. The marker is
# what says "this test calls AWS"; the directory only says where someone filed
# it. Keyed on the marker, a future misplaced file is covered on arrival.
# Pinned by test_integration_marked_tests_get_real_credentials in
# tests/unit/test_suite_hygiene.py. See #1307.

_SENTINEL = "testing"

#: Memo for the resolution below, which otherwise re-walks the boto3 credential
#: chain once per integration test. ``None`` is a real answer here (no
#: credentials available), so the "not yet computed" state needs its own flag.
_RESOLVED_REAL_CREDENTIALS: Optional[Dict[str, str]] = None
_CREDENTIAL_RESOLUTION_DONE = False


def _resolve_real_credentials() -> Optional[Dict[str, str]]:
    """Return real AWS credentials as an env mapping, or None if only sentinels.

    Two sources, in order: the values the invoking environment exported before
    this module replaced them (``_REAL_AWS_ENV``), then the boto3 chain with the
    sentinels hidden, which covers ``AWS_PROFILE`` and instance roles.
    """
    global _RESOLVED_REAL_CREDENTIALS, _CREDENTIAL_RESOLUTION_DONE
    if _CREDENTIAL_RESOLUTION_DONE:
        return _RESOLVED_REAL_CREDENTIALS

    resolved: Optional[Dict[str, str]] = None
    snapshot_key = _REAL_AWS_ENV.get("AWS_ACCESS_KEY_ID")
    if snapshot_key not in (None, _SENTINEL):
        # The environment really did carry credentials; hand back exactly what it
        # had, including the absence of a session token for a long-lived key.
        resolved = {
            k: v for k, v in _REAL_AWS_ENV.items() if v is not None and v != _SENTINEL
        }
    else:
        # Hide the sentinels so boto3 falls through to the profile/role chain,
        # then put the environment back exactly as it was. Leaving them popped is
        # what made the previous session-scoped version unsafe to generalise: a
        # later moto test in the same session would then sign with whatever the
        # chain resolved. (Popping at module scope is worse still — #988.)
        saved = {var: os.environ.pop(var, None) for var in _AWS_CREDENTIAL_VARS}
        # Imported here rather than at module scope: this file runs before every
        # test module, and the sentinel assignment above has to land before
        # anything builds a boto3 client.
        import boto3

        try:
            creds = boto3.Session().get_credentials()
            frozen = creds.get_frozen_credentials() if creds else None
        except Exception:
            frozen = None
        finally:
            for var, val in saved.items():
                if val is not None:
                    os.environ[var] = val

        if frozen is not None and frozen.access_key not in (None, _SENTINEL):
            resolved = {
                "AWS_ACCESS_KEY_ID": frozen.access_key,
                "AWS_SECRET_ACCESS_KEY": frozen.secret_key,
                "AWS_SESSION_TOKEN": frozen.token,
                "AWS_SECURITY_TOKEN": frozen.token,
            }
            resolved = {k: v for k, v in resolved.items() if v is not None}

    _RESOLVED_REAL_CREDENTIALS = resolved
    _CREDENTIAL_RESOLUTION_DONE = True
    return resolved


@pytest.fixture(autouse=True)
def real_aws_credentials_for_integration_tests(request):
    """Give every ``integration``-marked test real credentials, and only those.

    Function-scoped and restoring on teardown, so a run that spans both tiers —
    a bare ``pytest`` with no ``-m`` — leaves the sentinels in force for the
    moto tests either side of an integration test.
    """
    if request.node.get_closest_marker("integration") is None:
        yield
        return

    real = _resolve_real_credentials()
    if real is None:
        pytest.skip(
            "Integration tests require real AWS credentials. Configure a profile "
            "or export AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY (and "
            "AWS_SESSION_TOKEN if using temporary creds)."
        )

    saved = {var: os.environ.get(var) for var in _AWS_CREDENTIAL_VARS}
    for var in _AWS_CREDENTIAL_VARS:
        os.environ.pop(var, None)
    os.environ.update(real)
    try:
        yield
    finally:
        for var in _AWS_CREDENTIAL_VARS:
            os.environ.pop(var, None)
        os.environ.update({k: v for k, v in saved.items() if v is not None})
