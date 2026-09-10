# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Structural assertions for #787: the extraction and assessment tasks retry
TRANSIENT failures — including the ones a handler surfaces as ``TransientError`` —
and never blanket-retry every function error.

Step Functions matches ``Retry.ErrorEquals`` against the Lambda-reported
``errorType`` (the Python exception class name). ``TransientError`` is the one name
``idp_common.utils.transient_errors`` re-raises transient causes under; listing it
here is what makes the handler-side classification effective. ``States.TaskFailed``
and ``States.ALL`` in a Retry would retry deterministic failures eight times for a
document that can never succeed, which is the behaviour this change removes from
ShardExtractionStep and deliberately does not add to the others.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ASL_PATH = Path(__file__).resolve().parents[1] / "statemachine" / "workflow.asl.json"
_UNQUOTED_PLACEHOLDER_RE = re.compile(r":\s*\$\{[^}]+\}")

# Every task whose Lambda handler re-raises TransientError: the in-process
# extraction task, the assessment task, and all THREE shard-runtime tasks (plan,
# shard, merge share one handler, so all three must list the name).
TASKS = (
    "ExtractionStep",
    "AssessmentStep",
    "ExtractionPlanStep",
    "ShardExtractionStep",
    "ExtractionMergeStep",
)


def _find_state(states: dict, name: str) -> dict:
    for key, st in states.items():
        if key == name:
            return st
        for branch in st.get("Branches", []):
            found = _find_state(branch["States"], name)
            if found:
                return found
        for sub in ("ItemProcessor", "Iterator"):
            if sub in st:
                found = _find_state(st[sub]["States"], name)
                if found:
                    return found
    return {}


@pytest.fixture(scope="module")
def states() -> dict:
    raw = ASL_PATH.read_text(encoding="utf-8")
    return json.loads(_UNQUOTED_PLACEHOLDER_RE.sub(": 1", raw))["States"]


@pytest.mark.parametrize("task", TASKS)
def test_transient_error_is_retried(states, task):
    st = _find_state(states, task)
    assert st, f"{task} not found"
    names = {n for r in st["Retry"] for n in r["ErrorEquals"]}
    assert "TransientError" in names, (
        f"{task} must retry TransientError — the name "
        "idp_common.utils.transient_errors re-raises transient causes under"
    )
    for n in ("ThrottlingException", "ServiceUnavailableException"):
        assert n in names, f"{task} lost {n}"


@pytest.mark.parametrize("task", TASKS)
def test_no_blanket_retry_of_every_function_error(states, task):
    st = _find_state(states, task)
    names = {n for r in st["Retry"] for n in r["ErrorEquals"]}
    assert "States.TaskFailed" not in names and "States.ALL" not in names, (
        f"{task} would retry deterministic failures (bad schema, unparseable "
        "document) eight times for a document that can never succeed"
    )


def test_the_handlers_actually_raise_the_listed_name():
    """The ASL name is only useful if the three handlers re-raise under it."""
    src_dir = ASL_PATH.parents[1] / "src"
    for (
        rel
    ) in (  # each wraps its WHOLE handler, so loads before the main call count too
        "extraction_function/index.py",
        "extraction_function/sfn_runtime_handler.py",
        "assessment_function/index.py",
    ):
        text = (src_dir / rel).read_text(encoding="utf-8")
        assert "transient_errors" in text and (
            "raise_if_transient" in text or "TransientError(" in text
        ), f"{rel} does not surface transient failures as TransientError"
