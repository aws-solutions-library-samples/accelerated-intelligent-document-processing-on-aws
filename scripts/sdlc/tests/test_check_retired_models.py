# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The retired-model completeness check behaves offline.

``check_retired_models.py`` is the opt-in, network-requiring ratchet: it asks
Bedrock whether any model this repository OFFERS has been retired, which is the one
thing the offline gates cannot know. These tests cover the parts that do not need
network — id normalisation, template discovery, and above all that it **exits 0
when it cannot answer**, since that property is what makes it safe to run anywhere.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts/sdlc/check_retired_models.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("check_retired_models", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_retired_models"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_base_strips_region_prefix_and_tier_suffix(mod):
    """A tier or context suffix is not a different model, and GetFoundationModel
    rejects it outright — so leaving it on turns every `:flex` / `:priority` /
    `:1m` id into unresolvable noise that buries the one line that matters."""
    assert mod._base("us.amazon.nova-premier-v1:0") == "amazon.nova-premier-v1:0"
    assert mod._base("amazon.nova-2-lite-v1:0:flex") == "amazon.nova-2-lite-v1:0"
    assert mod._base("global.amazon.nova-2-lite-v1:0:priority") == (
        "amazon.nova-2-lite-v1:0"
    )
    assert mod._base("us.anthropic.claude-sonnet-4-6:1m") == "anthropic.claude-sonnet-4-6"
    # `v1:0` is part of a real id and must survive.
    assert mod._base("eu.anthropic.claude-sonnet-4-5-20250929-v1:0") == (
        "anthropic.claude-sonnet-4-5-20250929-v1:0"
    )


@pytest.mark.unit
def test_offered_ids_are_discovered_from_tracked_templates(mod):
    """Not vacuous: if discovery collapses, the check silently examines nothing and
    reports success."""
    offered = mod._offered_model_ids()
    assert len(offered) >= 80, f"only {len(offered)} offered ids discovered"
    assert "us.amazon.nova-pro-v1:0" in offered
    # A bare GovCloud id and a parameter Default must both be reachable.
    assert "amazon.nova-pro-v1:0" in offered


@pytest.mark.unit
def test_discovery_reads_only_tracked_files(mod):
    for path in mod._tracked_templates():
        rel = path.relative_to(REPO_ROOT)
        assert ".aws-sam" not in str(rel), rel
        assert (
            subprocess.run(
                ["git", "ls-files", "--error-unmatch", str(rel)],
                cwd=REPO_ROOT,
                capture_output=True,
            ).returncode
            == 0
        ), f"{rel} is not tracked by git"


@pytest.mark.unit
def test_registry_is_the_shared_one(mod):
    """It must read the same registry the offline gates and validate_config read,
    or it would report a model as newly retired that is already handled."""
    registry = mod._registry()
    assert registry, "the shared retired-model registry is empty"
    assert "us.amazon.nova-premier-v1:0" in registry
    for facts in registry.values():
        assert "eol" in facts and "verify" in facts


@pytest.mark.unit
def test_skip_exits_zero_by_default(mod, capsys):
    """The property that makes this safe to run anywhere.

    No credentials, no network or no `bedrock:GetFoundationModel` must all produce
    an explanation and a clean exit. A check that needs the network and exits
    non-zero without it would red-line every offline branch for a condition nobody
    can fix there — which is why this is opt-in and in neither CI.
    """
    args = argparse.Namespace(json=False, fail_on_skip=False)
    assert mod._skip("no AWS credentials available", args) == 0
    assert "Skipped" in capsys.readouterr().out


@pytest.mark.unit
def test_skip_exits_nonzero_with_fail_on_skip(mod, capsys):
    """…and a caller that wants a definite answer can demand one."""
    args = argparse.Namespace(json=False, fail_on_skip=True)
    assert mod._skip("no network", args) == 1
    capsys.readouterr()


@pytest.mark.unit
def test_end_of_life_marker_is_not_a_bare_resource_not_found(mod):
    """`Model not found` means "not offered in this region", which is a DIFFERENT
    fact from retirement — Claude 3.5 Sonnet 20241022 answers exactly that in
    eu-west-1 while answering end-of-life in us-east-1. Reporting the two alike
    would manufacture retirements."""
    assert mod.EOL_MARKER == "reached the end of its life"
    assert "Model not found" not in mod.EOL_MARKER


@pytest.mark.unit
def test_this_check_is_not_wired_into_either_ci():
    """Deliberately opt-in. It needs network and credentials, so making it blocking
    would break CI for a reason unrelated to the change under test."""
    for config in (".gitlab-ci.yml", ".github/workflows/code-checks.yml"):
        path = REPO_ROOT / config
        if path.is_file():
            assert "check-retired-models" not in path.read_text(encoding="utf-8"), (
                f"{config} runs check-retired-models; it requires AWS credentials "
                "and network, so it must stay opt-in"
            )
