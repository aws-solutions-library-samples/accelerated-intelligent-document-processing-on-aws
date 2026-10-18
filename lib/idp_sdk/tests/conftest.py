# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Pytest configuration and shared fixtures.
"""

import os
import sys
from pathlib import Path

import pytest

from idp_sdk import IDPClient

# Make the in-repo idp_common importable so the DocumentState-superset test can
# compare against the runtime's real Status enum. Without this it silently
# skips via importorskip — and a skipped test protects nothing, which is how
# four runtime statuses came to be missing from DocumentState.
_IDP_COMMON = Path(__file__).resolve().parents[2] / "idp_common_pkg"
if _IDP_COMMON.is_dir() and str(_IDP_COMMON) not in sys.path:
    sys.path.append(str(_IDP_COMMON))


# --- No outbound AWS from this suite -----------------------------------------
# This package drives deployed stacks — it starts and stops executions, reads and
# writes the tracking table, uploads and deletes documents — so a test that builds a
# real client by accident does not fail. It succeeds against whichever account is
# reachable, which on a developer machine is a live deployment. The placeholder
# credentials `tests/unit/conftest.py` installs are not that control: the request is
# still sent, and the operation does not have to succeed to be a problem.
#
# This package ships its own `pytest.ini`, so it computes its own rootdir and no
# conftest further up reaches it. It measured zero escaping requests in #1398, and
# the control is here so that stays a property of the suite rather than of the code
# it happens to exercise today.
#
# One definition, walked up to rather than copied: scripts/tests/no_outbound_aws.py.
def _no_outbound_aws_fixture():
    import sys as _sys
    from pathlib import Path as _Path

    for ancestor in _Path(__file__).resolve().parents:
        helper = ancestor / "scripts" / "tests"
        if not (helper / "no_outbound_aws.py").is_file():
            continue
        _sys.path.insert(0, str(helper))
        try:
            from no_outbound_aws import fixture

            return fixture()
        finally:
            _sys.path.pop(0)
    raise RuntimeError(
        "scripts/tests/no_outbound_aws.py not found above this file; it carries the "
        "control that stops this suite transacting with a real AWS account (#1398)"
    )


no_outbound_aws = _no_outbound_aws_fixture()


# --- No build output left in the checkout ------------------------------------
# This package's tests drive real build and packaging steps, and those steps
# resolve BOTH their inputs and their output directory against the process cwd. A
# test that chdirs to the repository root to satisfy the inputs therefore writes
# the output into the working tree.
#
# The cost is not tidiness. `make srt-scan` discovers `.aws-sam` directories by
# globbing the project root (scripts/srt/run.py) and drops into a degraded mode
# when it finds one — phantom LOCAL-ONLY findings, and a documented risk of
# crashing checkov on the largest templates. So a test that leaves one makes the
# security gate depend on whether the test suite ran first; it stops being the
# scan CI runs. That shipped once (#1491).
#
# The control is here, session-scoped, for the same reason `no_outbound_aws` is:
# so it stays a property of the SUITE rather than of the one test that happened to
# leak. An assertion inside a single test's own fixture can only ever observe its
# own call, so the next test to chdir to the repo root and run a writing step
# would leak exactly as #1491 did with that assertion green.
#
# ⚠️ The semantics are "unchanged by this suite", never "absent". A genuine
# `python3 publish.py …` writes these very paths in the checkout it runs from, so
# demanding absence would fail on any tree that has published and blame the test
# run for a file it did not create.
_CHECKOUT_BUILD_PATHS = (".aws-sam",)


def _build_output_state():
    """``(exists, st_mtime_ns)`` per watched repo-root build path."""
    root = Path(__file__).resolve().parents[3]
    state = {}
    for rel in _CHECKOUT_BUILD_PATHS:
        p = root / rel
        state[rel] = (p.exists(), p.stat().st_mtime_ns if p.exists() else None)
    return root, state


@pytest.fixture(scope="session", autouse=True)
def no_build_output_in_checkout():
    """Fail the session if it changed a gitignored build path in the checkout."""
    root, before = _build_output_state()
    yield
    _, after = _build_output_state()
    changed = [rel for rel in _CHECKOUT_BUILD_PATHS if after[rel] != before[rel]]
    assert not changed, (
        f"this test session changed {', '.join(repr(c) for c in changed)} under "
        f"{root} (before={before}, after={after}). A build or packaging step ran "
        f"with the cwd set to the repository root instead of a staging directory. "
        f"`make srt-scan` degrades when an .aws-sam directory is present, so this "
        f"makes the security gate depend on whether the test suite ran first — see "
        f"issue #1491 and the chdir section in .claude/skills/testing-qa.md. "
        f"`make srt-clean` removes it."
    )


@pytest.fixture(scope="session")
def stack_name():
    """Get stack name from environment."""
    return os.environ.get("IDP_STACK_NAME", "idp-stack-01")


@pytest.fixture(scope="session")
def region():
    """Get AWS region from environment."""
    return os.environ.get("AWS_REGION", "us-east-1")


@pytest.fixture
def client(stack_name, region):
    """Create IDP client instance."""
    return IDPClient(stack_name=stack_name, region=region)


@pytest.fixture
def client_no_stack():
    """Create IDP client without stack (for config/manifest operations)."""
    return IDPClient()


def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "unit: Unit tests (fast, mocked, no AWS required)"
    )
    config.addinivalue_line(
        "markers",
        "integration: Integration tests (slow, real AWS, requires credentials)",
    )
    config.addinivalue_line("markers", "stack: Stack operation tests")
    config.addinivalue_line("markers", "batch: Batch operation tests")
    config.addinivalue_line("markers", "document: Document operation tests")
    config.addinivalue_line("markers", "config: Config operation tests")
