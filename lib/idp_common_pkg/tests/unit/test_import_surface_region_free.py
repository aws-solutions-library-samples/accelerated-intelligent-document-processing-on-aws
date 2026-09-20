# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Importing config, bedrock, or the shard primitives must not need an AWS region.

``idp_common.utils.settings_helper`` builds an SSM client at **module scope**
(``ssm_client = boto3.client("ssm")``), so importing anything from
``idp_common.utils`` needs a resolvable AWS region before any handler code runs.
That is survivable where it happens today — a Lambda always has a region — but it
is transitive, and that is what makes it a trap: one new module-scope import
anywhere on a chain propagates the requirement to everything that imports it.

Both directions of that trap have been sprung in this area, which is why the
property is asserted rather than assumed:

* Importing the shard time budget from ``idp_common.utils`` inside
  ``bedrock/client.py`` made ``import idp_common.config`` require a region, because
  the chain is ``config/__init__`` -> ``configuration_manager`` -> ``merge_utils``
  -> ``idp_common.bedrock`` -> ``bedrock.client``. Nothing in ``idp_common_pkg``'s
  own suite noticed, because its ``conftest`` sets ``AWS_REGION`` at module scope;
  it surfaced in a *different* package's CI step as 27 ``NoRegionError`` collection
  errors, a long way from the cause.
* The same import in ``extraction/runtime.py`` — where it cannot be deferred,
  because it supplies a default argument — broke that module's documented promise
  to be import-light at module top (no strands, no PIL, no boto3). Both are why the
  constants live in ``idp_common/timeout_budget.py``, a leaf module that imports
  nothing.

Each check runs in a **subprocess** with the region scrubbed from the environment
AND from the AWS config file, because the in-process environment cannot be un-set
once ``conftest`` has set it, and a developer machine usually has a region in
``~/.aws/config`` which would mask the failure.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[2]  # lib/idp_common_pkg

#: Packages that lean Lambda handlers and the SDK import at cold start, and which
#: must therefore be importable before anything has resolved a region. Both hold on
#: ``develop``; the first is what the shard-budget change briefly broke.
REGION_FREE_IMPORTS = ("idp_common.config", "idp_common.bedrock")

#: Modules whose OWN top-level imports must stay region-free even though their
#: package ``__init__`` is not. ``idp_common.extraction.__init__`` imports
#: ``ExtractionService``, which pulls in ``idp_common.utils``, so
#: ``import idp_common.extraction.runtime`` needs a region for a reason that predates
#: the budget work and is not this module's business. What IS checkable — and what
#: the module documents about itself — is that executing ``runtime.py`` alone does
#: not. It is loaded by path, which bypasses the package ``__init__``.
REGION_FREE_MODULE_FILES = ("idp_common/extraction/runtime.py",)

#: The known landmine, asserted so it stays a known one: ``idp_common.utils`` builds
#: an SSM client as it imports, and that is why the entries above must not import it
#: at module scope.
REGION_REQUIRING_IMPORT = "idp_common.utils"

_LOAD_BY_PATH = (
    "import importlib.util, sys;"
    "spec = importlib.util.spec_from_file_location('probe', sys.argv[1]);"
    "mod = importlib.util.module_from_spec(spec);"
    "spec.loader.exec_module(mod)"
)


def _run_region_free(code: str, *args: str) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
            "AWS_PROFILE",
            "AWS_CONFIG_FILE",
            "AWS_SHARED_CREDENTIALS_FILE",
        }
    }
    # A developer machine usually has a region in ~/.aws/config, which would mask
    # the failure; CI has neither the env var nor the file.
    env["AWS_CONFIG_FILE"] = os.devnull
    env["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


_ADVICE = (
    "Something on its import chain now builds a boto3 client at module scope, or "
    "imports a module that does — `idp_common.utils` is the usual one, via "
    "`settings_helper`. Move that import to its point of use, or take the value from "
    "`idp_common.timeout_budget`, which imports nothing. Left in place it breaks "
    "every importer in any environment without a region, and the failure surfaces "
    "far from the cause."
)


@pytest.mark.unit
@pytest.mark.parametrize("module", REGION_FREE_IMPORTS)
def test_importing_it_does_not_need_a_region(module: str):
    result = _run_region_free(f"import {module}")
    assert result.returncode == 0, (
        f"`import {module}` fails with no AWS region configured:\n"
        f"{result.stderr.strip()[-2000:]}\n\n{_ADVICE}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", REGION_FREE_MODULE_FILES)
def test_the_module_itself_does_not_need_a_region(rel_path: str):
    path = PKG_ROOT / rel_path
    assert path.is_file(), f"{rel_path} has moved; update this list"
    result = _run_region_free(_LOAD_BY_PATH, str(path))
    assert result.returncode == 0, (
        f"executing {rel_path} on its own fails with no AWS region configured:\n"
        f"{result.stderr.strip()[-2000:]}\n\n{_ADVICE}"
    )


@pytest.mark.unit
def test_the_known_region_requiring_import_is_still_the_only_one():
    """Pins the landmine, so the checks above keep their meaning.

    If ``idp_common.utils`` ever becomes region-free this fails — at which point the
    reasoning above is obsolete and the constraint can be relaxed rather than
    carried forever as an unexplained rule.
    """
    result = _run_region_free(f"import {REGION_REQUIRING_IMPORT}")
    assert result.returncode != 0 and "NoRegionError" in result.stderr, (
        f"`import {REGION_REQUIRING_IMPORT}` no longer requires a region. That is an "
        "improvement: settings_helper presumably stopped building its SSM client at "
        "module scope. Update this module's docstring and delete this test rather "
        "than leaving a stale explanation in place."
    )
