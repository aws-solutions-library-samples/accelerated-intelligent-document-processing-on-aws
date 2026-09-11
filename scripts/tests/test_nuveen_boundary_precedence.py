# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Step-8 config's boundary instruction, and the clause that honours it (#750).

`samples/Nuveen.pdf` is ONE 16-page fund table whose every page reprints the
title, the logo and the full column-header block, and paginates with a BARE page
number. That defeats the shipped `<boundary-detection-rules>`: their OPENING
HEADER BLOCK test is satisfied by the running header and is evaluated BEFORE
their CONTINUATION EVIDENCE test, while a lone "7" matches none of their
PAGINATION patterns. Measured on the config's own classifier
(`us.amazon.nova-2-lite-v1:0`, temperature 0), the document split into 2-5
sections in 10 of 10 runs.

The fix is a BOUNDARY sentence in the class description, which the rules' own
PRECEDENCE clause promotes over the generic rules: 1 section in 10 of 10 runs.
It is not defended by any other test — the prompt-copy guard checks
`classification.task_prompt`, and nothing checks a class *description*. Two
things have to hold together for it to work, so both are asserted here: the
sentence has to be present, and the prompt has to still say descriptions win.

Deliberately NOT a fix to the shared prompt. Every generic lever measured on the
existing boundary fixtures regressed one of the two directions #653 balances —
prompt wording cost the over-split case (`small_narrow`: 7/10 -> 4/10, 2/10 or
0/10 depending on the wording), and `contextPagesCount: 1` cost the over-merge
case (`twodocs_2x20`: 10/10 -> 5/10). See docs/classification.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
NUVEEN = REPO_ROOT / "scripts/sdlc/config/nuveen.yaml"
DEFAULTS = (
    REPO_ROOT
    / "lib/idp_common_pkg/idp_common/config/system_defaults/base-classification.yaml"
)


@pytest.fixture(scope="module")
def nuveen_class() -> dict:
    classes = yaml.safe_load(NUVEEN.read_text())["classes"]
    assert len(classes) == 1, "Step 8's config is single-class by design"
    return classes[0]


def test_the_class_carries_a_boundary_instruction(nuveen_class):
    """Without this, Step 8 fails 10/10 — and reports it as lost rows."""
    description = nuveen_class["description"]
    assert "BOUNDARY:" in description, (
        "scripts/sdlc/config/nuveen.yaml's class description lost its BOUNDARY "
        "instruction. The #653 boundary rules will read this document's repeated "
        "running header as an opening header block and split the 16-page table "
        "(#750). Restore it or Step 8 fails on every pipeline."
    )


def test_the_instruction_actually_answers_the_boundary_question(nuveen_class):
    """ "Multi-page" alone is not an instruction.

    The model has to be told what to *emit*, in the vocabulary the output format
    uses, or the sentence is a description of the document rather than a rule.
    """
    description = nuveen_class["description"]
    for token in ('"start"', '"continue"'):
        assert token in description, (
            f"the BOUNDARY instruction must name {token} explicitly — that is the "
            "value document_boundary takes, and a paraphrase leaves the model to "
            "guess the mapping"
        )


def test_the_prompt_still_lets_a_class_description_win(nuveen_class):
    """The other half of the fix.

    A class description only overrides the generic rules because the rules say
    so. Drop the PRECEDENCE clause and this config silently reverts to the
    behaviour that produced "expected 532 fund items, got 193", with the
    description still sitting there looking like it does something.
    """
    prompt = yaml.safe_load(DEFAULTS.read_text())["classification"]["task_prompt"]
    assert "PRECEDENCE:" in prompt
    assert "boundary or page-ordering instructions" in prompt, (
        "the PRECEDENCE clause no longer promotes a document type's own boundary "
        "instructions over the generic rules, which is the mechanism "
        "scripts/sdlc/config/nuveen.yaml relies on (#750)"
    )


def test_the_pinned_copy_agrees_with_the_default_on_precedence():
    """Step 8's config pins its own task_prompt, so it needs the clause too.

    `test_classification_prompt_copies_in_sync.py` enforces byte-equality of the
    whole rules block, which subsumes this — but that test would still pass if
    the clause were dropped from the default AND every copy together, and this
    config would then break with no failure pointing at it.
    """
    prompt = yaml.safe_load(NUVEEN.read_text())["classification"]["task_prompt"]
    assert "PRECEDENCE:" in prompt
