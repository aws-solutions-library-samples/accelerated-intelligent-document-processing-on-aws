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
# The definition is NOT here. It lives in `scripts/tests/no_build_output.py` and is
# walked up to, which is the load-bearing half of the convention `no_outbound_aws`
# follows above: a definition reachable from any pytest root in the tree, so the next
# suite to need it writes two lines rather than a copy. A control that only this
# rootdir can reach is the pre-#1398 state that module's docstring describes.
#
# What it watches, why it is a glob rather than a list, and why the semantics are
# "unchanged by this session" rather than "absent" are all documented there.
_CHECKOUT_ROOT = Path(__file__).resolve().parents[3]


def _no_build_output_fixtures():
    import sys as _sys
    from pathlib import Path as _Path

    for ancestor in _Path(__file__).resolve().parents:
        helper = ancestor / "scripts" / "tests"
        if not (helper / "no_build_output.py").is_file():
            continue
        _sys.path.insert(0, str(helper))
        try:
            import no_build_output

            return no_build_output
        finally:
            _sys.path.pop(0)
    raise RuntimeError(
        "scripts/tests/no_build_output.py not found above this file; it carries the "
        "control that stops this suite leaving SAM build output in the checkout and "
        "silently degrading `make srt-scan` (#1491)"
    )


_no_build_output = _no_build_output_fixtures()

no_build_output_in_checkout = _no_build_output.fixture(_CHECKOUT_ROOT)


@pytest.fixture(scope="session")
def checkout_build_output():
    """The shared watch, for a test asserting the narrower claim about its own call.

    Hands back the same snapshot, diff and message the session control uses, so a
    per-test assertion is made against one definition rather than a second copy.
    """
    root = _CHECKOUT_ROOT
    return (
        lambda: _no_build_output.snapshot(root),
        _no_build_output.changed,
        lambda moved, _before, _after, what: _no_build_output.message(
            moved, root, what
        ),
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
