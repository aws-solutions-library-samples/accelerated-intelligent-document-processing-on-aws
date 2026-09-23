# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Test configuration and fixtures for idp_cli tests
"""

import os
import sys
from pathlib import Path

import pytest

# Set a region and dummy credentials BEFORE anything imports boto3, for the same
# reason idp_common's conftest does: this package is a deployment CLI and it builds
# boto3 clients inside command bodies, so a client can be constructed during
# collection as well as during a test. CI has neither a region nor credentials, a
# developer machine has both, and `botocore` raises NoRegionError only in the
# former — which is how a suite passes locally and fails in CI. `setdefault` so a
# deliberately-exported region still wins for anyone debugging.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
# No IMDS lookup: without this, a missing credential turns into a multi-second
# connect attempt to 169.254.169.254 rather than an immediate failure.
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

# Add idp_common_pkg to Python path for testing
# This mirrors the production code's approach of dynamically adding the path
# Since idp_cli_pkg is in lib/, we go up to lib/ level and find idp_common_pkg there
project_root = Path(__file__).parent.parent.parent  # lib/
idp_common_path = project_root / "idp_common_pkg"

if idp_common_path.exists():
    sys.path.insert(0, str(idp_common_path))
else:
    raise RuntimeError(
        f"idp_common_pkg not found at expected location: {idp_common_path}"
    )


@pytest.fixture(autouse=True)
def unstyled_cli_console():
    """
    Render CLI output unstyled at a fixed width, so assertions are about CONTENT.

    Two environment-dependent renderings used to decide whether the suite passed
    (issues 714 and 905):

    - **Colour.** `CliRunner` captures into a non-tty stream, so Rich normally
      disables styling by itself and the suite passes. Set `FORCE_COLOR=1` — or
      run under a pty, as some CI runners do — and Rich highlights, so
      `assert "--config-revision 7" in output` fails on the `\\x1b[1;36m` sitting
      between the flag and its value.
    - **Width.** With no tty Rich falls back to 80 columns and ellipsizes the
      wider table columns, so a test passed standalone and failed in the suite.

    `Console(force_terminal=False)` settles the first: an explicit `False` beats
    `FORCE_COLOR` in the environment, and a non-terminal console emits no ANSI at
    all. Note `no_color=True` is NOT enough — it drops colour but keeps `bold`,
    so escapes still land mid-assertion. `width=200` settles the second; assigning
    the width overrides Rich's detection outright, which is why `COLUMNS` has no
    effect here.

    Autouse and package-wide on purpose. As a per-test fixture this was easy to
    omit from a new test, which then carried the same sensitivity back in.

    `err_console` — the stderr console the payload-bearing commands send their
    progress lines to — is pinned the same way. `CliRunner.result.output` is the
    two streams combined, so an unpinned stderr console would put escapes into the
    text most of this suite asserts on, in exactly the way this fixture exists to
    prevent.
    """
    from rich.console import Console

    from idp_cli import cli as cli_module

    original = cli_module.console
    original_err = cli_module.err_console
    cli_module.console = Console(width=200, force_terminal=False)
    cli_module.err_console = Console(stderr=True, width=200, force_terminal=False)
    try:
        yield cli_module.console
    finally:
        cli_module.console = original
        cli_module.err_console = original_err


@pytest.fixture(autouse=True)
def hermetic_aws_environment(monkeypatch):
    """
    Make every test see the same AWS environment: a region, dummy credentials, and
    no profile or config file from the machine it runs on.

    The module-level `setdefault` block above covers import time. This covers run
    time, and it removes things rather than adding them: `AWS_PROFILE` and a real
    `~/.aws/config` are present on a developer machine and absent in CI, and a
    command that resolves a profile behaves differently in the two places. Pointing
    both file variables at `os.devnull` is what makes a developer run reproduce a CI
    run, which is the whole point.

    `AWS_PROFILE` is deleted rather than set, because this suite has tests that
    assert what `--profile` does to the session boto3 builds; leaving an ambient one
    in place would let such a test pass for the wrong reason.
    """
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture(autouse=True)
def no_outbound_http(monkeypatch):
    """
    Fail loudly if a test reaches the network, naming the URL it tried to reach.

    This package deploys CloudFormation stacks, empties S3 buckets and stops Step
    Functions executions. A test that builds a real client by accident does not fail
    — it succeeds against whatever account the ambient credentials point at, which on
    a developer machine is a live deployment. Dummy credentials are not enough on
    their own: the request is still sent, and `DeleteStack` does not need to succeed
    to be a problem.

    The seam is `botocore.httpsession.URLLib3Session.send`, the single point every
    botocore request passes through on its way out. `moto` short-circuits earlier, on
    botocore's `before-send` event, so a `mock_aws` test never reaches this and needs
    no exemption; a `MagicMock` client never reaches it either. What does reach it is
    exactly the mistake worth catching.
    """
    from botocore.httpsession import URLLib3Session

    def _refuse(self, request):
        raise RuntimeError(
            "This test attempted a real network request to "
            f"{getattr(request, 'url', '<unknown>')}. Use moto (mock_aws) or patch "
            "the client; see the no_outbound_http fixture in tests/conftest.py."
        )

    monkeypatch.setattr(URLLib3Session, "send", _refuse)


FIRST_PARTY_UNDER_TEST = (
    "idp_cli",
    "idp_common",
    "idp_sdk",
)


# Fail fast if a first-party package resolves outside this checkout. These tests import
# the library as an external dependency, so nothing puts its source on `sys.path` and the
# editable-install pointer alone decides which revision runs. On a machine sharing one
# interpreter between checkouts that pointer is rewritten by whoever last ran
# `make test-cicd`, and the wrong tree produces a green run describing other code.
#
# The imports sit inside the function deliberately: this block is appended after a
# module's own imports, and module-level imports there are an E402 lint failure.
#
# Rationale, and the identity-vs-ancestry distinction:
# scripts/tests/first_party_provenance.py
def _assert_first_party_provenance(packages):
    import pathlib as _pathlib
    import sys as _sys

    for ancestor in _pathlib.Path(__file__).resolve().parents:
        gate = ancestor / "scripts" / "tests" / "first_party_provenance.py"
        if not gate.is_file():
            continue
        _sys.path.insert(0, str(gate.parent))
        try:
            from first_party_provenance import assert_resolves_in

            for package in packages:
                assert_resolves_in(package, __file__)
        finally:
            _sys.path.pop(0)
        return


_assert_first_party_provenance(FIRST_PARTY_UNDER_TEST)
