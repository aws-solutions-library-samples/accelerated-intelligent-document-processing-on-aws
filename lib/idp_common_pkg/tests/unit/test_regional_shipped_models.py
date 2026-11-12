# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every shipped model a local path translates is one the translation knows.

:func:`idp_common.regional_models.regional_shipped_model` translates a model read
from ``config/system_defaults`` for a path that never runs the deploy-time swap:
the SDK and CLI's local discovery. It returns an id outside
:data:`~idp_common.regional_models.FALLBACK_MODEL_IDS` unchanged, and the swap-parity
test in ``src/lambda/update_configuration/test_eu_region.py`` checks only that set,
so a shipped model changed to one outside it would be sent as a ``us.`` profile from
an EU stack with every gate green. This reads the YAML those paths read, and pins
where the translation is called so a new caller has to name what it reads here.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from idp_common import regional_models
from idp_common.config.merge_utils import load_system_defaults

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[4]

#: The sections of ``base-discovery.yaml`` whose ``model_id`` the SDK's local
#: discovery sends: schema discovery with and without ground truth, and section
#: detection.
LOCAL_DISCOVERY_SECTIONS = ("with_ground_truth", "without_ground_truth", "auto_split")

#: Every module that calls ``regional_shipped_model``, and how many times.
CALLERS = {"lib/idp_sdk/idp_sdk/operations/discovery.py": 2}


@pytest.mark.parametrize("section", LOCAL_DISCOVERY_SECTIONS)
def test_each_shipped_local_discovery_model_is_translated(section):
    model_id = load_system_defaults("pattern-2")["discovery"][section]["model_id"]
    assert model_id in regional_models.FALLBACK_MODEL_IDS, (section, model_id)
    assert regional_models.regional_shipped_model(model_id, "eu-west-1").startswith(
        "eu."
    ), model_id


def _calls(source: str) -> int:
    return sum(
        isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None))
        == "regional_shipped_model"
        for node in ast.walk(ast.parse(source))
    )


def test_the_translation_is_called_only_where_this_test_reads_the_defaults():
    tracked = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    found = {}
    for path in tracked:
        if "/tests/" in path or path.rsplit("/", 1)[-1].startswith("test_"):
            continue
        source = (_REPO / path).read_text(encoding="utf-8")
        if "regional_shipped_model" in source and _calls(source):
            found[path] = _calls(source)
    assert found == CALLERS


def test_the_call_count_reads_both_spellings():
    """Without this the pin above could count nothing and still match."""
    assert _calls("regional_shipped_model(m, r)") == 1
    assert _calls("regional_models.regional_shipped_model(m, r)") == 1
    assert _calls("regional_default(m, r)") == 0
