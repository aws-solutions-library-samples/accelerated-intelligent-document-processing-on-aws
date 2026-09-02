# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every committed copy of `classification.task_prompt` carries the #653 rules.

`test_boundary_prompt_contract.py` (in `lib/idp_common_pkg`) pins the rules in the
**default** prompt. That is not enough on its own: a preset, sample config, or
notebook that pins its own `classification.task_prompt` *overrides* the default, so
a stale copy silently reverts the fix for anyone using that config — with no test
failure and no visible symptom, because row completeness stays 100% and the
document still reaches COMPLETED. Only the section count changes.

When #737 landed the fix in the default prompt, seven such copies were left behind
(five `ocr-benchmark` presets, `scripts/sdlc/config/nuveen.yaml`, and the notebooks
example config), plus one notebook that inlines the prompt as a Python literal.
This test is the guard that keeps a new one from being added.

It scans the whole checkout rather than a hand-maintained list, so a copy added in
a location nobody thought of is still caught.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = (
    REPO_ROOT
    / "lib/idp_common_pkg/idp_common/config/system_defaults/base-classification.yaml"
)

#: The question the fix removed. It asked about "the previous document", which a
#: per-page classification request does not contain.
STALE = "Decide if this page starts a new document"

#: Directories that are build output, dependencies, or scratch — never source.
PRUNE = (
    ".git",
    ".venv",
    "node_modules",
    ".aws-sam",
    "site-packages",
    ".pytest_cache",
    "scratch",
    ".claude",
    "build",
    "dist",
)

#: Files that are *supposed* to contain the pre-fix text.
EXEMPT = {
    # The deliberate pre-#653 control prompt: the benchmark's whole purpose is to
    # measure the fix against the prompt it replaced.
    "benchmarks/matrices/prompts/classification_task_prompt_pre653.txt",
    # Declares that control suite, so it names the control prompt's text.
    "benchmarks/tests/test_suite_declarations.py",
    # This guard, which has to spell out the string it forbids.
    "scripts/tests/test_classification_prompt_copies_in_sync.py",
}


def _canonical_rules() -> str:
    """The `<boundary-detection-rules>` block as the default prompt carries it.

    `index` would match step 4's *reference* to the tag ("by applying the
    <boundary-detection-rules> below"), so the opening tag is found by searching
    backwards from the closing one.
    """
    prompt = yaml.safe_load(DEFAULTS.read_text())["classification"]["task_prompt"]
    close = "</boundary-detection-rules>"
    end = prompt.index(close)
    start = prompt.rindex("<boundary-detection-rules>", 0, end)
    return prompt[start : end + len(close)]


def _source_files():
    for path in REPO_ROOT.rglob("*"):
        if path.suffix not in (".yaml", ".yml", ".json", ".ipynb", ".py", ".txt"):
            continue
        rel = path.relative_to(REPO_ROOT)
        if any(part in PRUNE for part in rel.parts):
            continue
        yield rel, path


def _text_excluding_notebook_outputs(path: Path) -> str:
    """Notebook `source` only.

    A saved cell **output** is the record of a past execution. Rewriting one to
    match today's prompt would falsify that record, so outputs are not checked —
    `ds11-passport-application/demo.ipynb` legitimately shows the old prompt in
    five of them.
    """
    if path.suffix != ".ipynb":
        return path.read_text(errors="ignore")
    try:
        nb = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    return "\n".join(
        "".join(cell.get("source", [])) for cell in nb.get("cells", [])
    )


def test_no_committed_copy_still_asks_the_unanswerable_question():
    stale = [
        str(rel)
        for rel, path in _source_files()
        if str(rel) not in EXEMPT and STALE in _text_excluding_notebook_outputs(path)
    ]
    assert not stale, (
        "these files pin a pre-#653 classification.task_prompt, which OVERRIDES the "
        "fixed default and silently re-introduces intermittent mis-splitting:\n  "
        + "\n  ".join(sorted(stale))
    )


def _configs_doing_page_level_boundary_detection():
    """Pinned prompts that ask the model to emit `document_boundary`.

    That field is what the rules govern, so it is the precise scope. Two kinds of
    pinned prompt are correctly *outside* it and must not be dragged in:

    - `textbasedHolisticClassification` presets, which segment the whole packet in
      a single call and therefore *do* see the neighbouring pages. The per-page
      rules address the opposite situation and do not apply.
    - Page-class-only prompts (`rvl-cdip-with-few-shot-examples`, the dual-engine
      rule-validation sample) that never ask for a boundary at all.
    """
    for rel, path in _source_files():
        if path.suffix not in (".yaml", ".yml") or str(rel) in EXEMPT:
            continue
        try:
            loaded = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue  # not our concern; other tests cover parse validity
        if not isinstance(loaded, dict):
            continue
        classification = loaded.get("classification")
        if not isinstance(classification, dict):
            continue
        prompt = classification.get("task_prompt")
        if isinstance(prompt, str) and "document_boundary" in prompt:
            yield pytest.param(prompt, id=str(rel))


@pytest.mark.parametrize(
    "task_prompt", list(_configs_doing_page_level_boundary_detection())
)
def test_every_boundary_prompt_carries_the_canonical_rules(task_prompt):
    """Byte-equality with the default's block, not just "has the tag".

    A paraphrase would drift: the two CRITICAL clauses are a matched pair pulling
    in opposite directions, and dropping either one alone re-introduces a failure
    mode. Requiring the exact text means a preset can never carry a half-fix.
    """
    assert _canonical_rules() in task_prompt


def test_the_guard_can_actually_see_the_configs_it_guards():
    """A path or prune-list mistake would make every test above vacuously pass."""
    found = {p.id for p in _configs_doing_page_level_boundary_detection()}
    for expected in (
        "lib/idp_common_pkg/idp_common/config/system_defaults/base-classification.yaml",
        "config_library/managed_config/ocr-benchmark/config.yaml",
        "config_library/unified/ocr-benchmark/config.yaml",
        "config_library/unified/ocr-benchmark/fine_tuned_config.yaml",
        "config_library/unified/ocr-benchmark/ocr_config.yaml",
        "config_library/unified/ocr-benchmark/ocr_fine_tuned_config.yaml",
        "scripts/sdlc/config/nuveen.yaml",
        "notebooks/examples/config/classification.yaml",
    ):
        assert expected in found, f"{expected} not scanned; found {sorted(found)}"
