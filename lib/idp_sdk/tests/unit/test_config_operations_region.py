# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every config operation hands its client's region to ConfigurationManager.

This is the SDK half of the ``idp-cli --region`` fix. ``ConfigurationManager``
now accepts a region (asserted in
``idp_common_pkg/tests/unit/config/test_config_client_region.py``), but that only
helps if the operations layer actually passes it — and the operations layer is
what every ``config-*`` CLI command and ``bootstrap`` go through.

The test is written against the *source* of ``operations/config.py`` rather than
by invoking each operation, because each one first makes a CloudFormation call to
resolve the table name and then talks to DynamoDB; mocking all of that for eight
commands would test the mocks. What matters is the invariant "no construction
site here is region-less", and that is exactly what a reader of the file has to
maintain when adding the ninth command. The construction sites are *discovered*
from the source, not listed here, so a new one is covered the moment it is added.
"""

from __future__ import annotations

import inspect
import re

import pytest

from idp_sdk.operations import config as config_ops

# `ConfigurationManager(` / `ConfigurationReader(` followed by anything up to the
# matching close paren on the same logical construction. Both are region-bearing
# entry points into idp_common's configuration layer.
_CTOR = re.compile(
    r"\b(ConfigurationManager|ConfigurationReader)\(([^()]*(?:\([^()]*\)[^()]*)*)\)",
    re.S,
)


def _construction_sites():
    src = inspect.getsource(config_ops)
    return [(m.group(1), m.group(2)) for m in _CTOR.finditer(src)]


@pytest.mark.unit
def test_construction_sites_are_discoverable():
    """If the regex stops matching, every assertion below passes vacuously."""
    sites = _construction_sites()
    # 8 ConfigurationManager (download/upload/list/revisions/activate/delete/
    # sync_bda, plus the revision branch of download) and 1 ConfigurationReader.
    assert len(sites) >= 9, f"expected >=9 construction sites, found {len(sites)}"


@pytest.mark.unit
def test_every_config_manager_construction_passes_a_region():
    offenders = [
        f"{name}({args.strip()})"
        for name, args in _construction_sites()
        if "region=" not in args
    ]
    assert not offenders, (
        "these construct idp_common's configuration layer without a region, so "
        "they resolve the table name in the requested region and then read or "
        "write it in whatever region the ambient credentials pick: "
        f"{offenders}"
    )


@pytest.mark.unit
def test_region_comes_from_the_client_not_a_literal():
    """A hardcoded region would satisfy the check above while still being wrong."""
    bad = [
        f"{name}({args.strip()})"
        for name, args in _construction_sites()
        if "region=" in args and "self._client._region" not in args
    ]
    assert not bad, f"region must come from self._client._region, got: {bad}"


@pytest.mark.unit
def test_the_module_under_test_is_the_one_in_this_checkout():
    """Guard against testing a different tree's code.

    `idp_sdk` and `idp_cli` are editable installs pointing at whichever checkout
    was pip-installed, so a test run from a git worktree can silently inspect the
    MAIN tree's module and report green for a fix that is not in it. Under pytest
    the rootdir insertion makes `idp_sdk` resolve to this checkout, but that is a
    property of how the suite is invoked, not a guarantee — so assert it, because
    every assertion in this file reads module source.

    Routed through `scripts/tests/first_party_provenance.py` rather than hand-rolled.
    The local version derived the root as `parents[4]` and asked whether the module sat
    UNDER it, which accepts a git worktree nested at `.claude/worktrees/` — the very
    "test run from a git worktree" this docstring names as the hazard.
    """
    import pathlib
    import sys

    gate_dir = pathlib.Path(__file__).resolve().parents[4] / "scripts" / "tests"
    if not (gate_dir / "first_party_provenance.py").is_file():
        pytest.skip("shared provenance helper is not present in this tree")
    sys.path.insert(0, str(gate_dir))
    try:
        from first_party_provenance import assert_resolves_in

        assert_resolves_in(config_ops.__name__, __file__)
    finally:
        sys.path.pop(0)


@pytest.mark.unit
def test_configure_config_env_bridges_the_region_for_deep_clients():
    """The backstop for clients built too deep to be handed a region explicitly.

    `idp_common.bedrock.model_utils._load_model_limits_from_dynamodb` builds a
    ConfigurationManager with no caller able to pass one, and it sits on
    `config-upload`'s own validation path. Its failure is swallowed by a total
    `except`, so an out-of-region read degrades silently to the on-disk default
    limits and can reject a configuration that is legitimately above a default cap.
    """
    src = inspect.getsource(config_ops.ConfigOperation._configure_config_env)
    assert 'os.environ["AWS_DEFAULT_REGION"] = region' in src, (
        "_configure_config_env no longer bridges the resolved region into the "
        "environment, so regionless clients deep in idp_common fall back to the "
        "ambient region"
    )
    assert "if region:" in src, (
        "the bridge must be conditional — writing AWS_DEFAULT_REGION "
        "unconditionally would pin the process to an empty region"
    )
