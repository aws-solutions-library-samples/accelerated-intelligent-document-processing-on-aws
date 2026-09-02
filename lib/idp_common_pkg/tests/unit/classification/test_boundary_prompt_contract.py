# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The boundary-detection rules in the default classification prompt (#653).

Each page is classified in an **independent** LLM call that sees only that page
(plus any `contextPagesCount` neighbours) and is never told its position in the
packet. The prompt nonetheless used to ask "does this page continue the *previous*
document?" — a question the model has no way to answer from what it was given.
Failing runs said so in their own reasoning: *"Since no prior page 1 has been
established in this sequence, this is treated as the start of the document."*

Consequences measured live: one 3-page statement fragmented into 2-3 sections in
82% of runs (14/17) on Nova 2 Lite, and 6/24 correct on Sonnet 5 in #653's own
reproduction. Silent — row completeness stays 100% and the status stays COMPLETED.

These are **contract** tests on prompt text, which is unusual, and deliberate. The
fix is prose, so nothing else can catch its removal: a well-meaning prompt tidy-up
could drop a clause and every unit test would still pass while the pipeline
silently regressed to intermittent mis-splitting. The two CRITICAL clauses in
particular are a matched pair that pull in *opposite* directions, so removing
either one alone re-introduces a failure mode.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

DEFAULTS = (
    pathlib.Path(__file__).resolve().parents[3]
    / "idp_common"
    / "config"
    / "system_defaults"
    / "base-classification.yaml"
)


@pytest.fixture(scope="module")
def task_prompt() -> str:
    if not DEFAULTS.exists():  # pragma: no cover - packaged install
        pytest.skip(f"{DEFAULTS} not present")
    cfg = yaml.safe_load(DEFAULTS.read_text())
    return cfg["classification"]["task_prompt"]


def test_the_rules_block_is_present(task_prompt):
    assert "<boundary-detection-rules>" in task_prompt
    assert "</boundary-detection-rules>" in task_prompt


def test_the_under_specified_question_is_gone(task_prompt):
    """The original step 4 asked about "the previous document", which a
    single-page request does not contain."""
    assert "starts a new document" not in task_prompt
    assert "continues the previous document" not in task_prompt
    assert "apply" in task_prompt.lower()


def test_the_no_preceding_page_clause_is_present(task_prompt):
    """Stops OVER-SPLITTING: absence of page 1 in the input proves nothing."""
    assert "CRITICAL - no preceding page shown" in task_prompt
    assert "tells you nothing about the document" in task_prompt


def test_the_consecutive_same_type_clause_is_present(task_prompt):
    """Stops OVER-MERGING: the pair to the clause above.

    Without it, "prefer continue" biases the model into collapsing back-to-back
    copies of one form into a single section — #653 measured the unfixed prompt
    at 1/10 on that shape, and a naive fix makes it worse rather than better.
    """
    assert "CRITICAL - consecutive documents of the same type" in task_prompt
    assert "two separate documents" in task_prompt


def test_both_critical_clauses_survive_together(task_prompt):
    """They are a matched pair pulling in opposite directions.

    Keeping only the anti-over-split clause trades one failure mode for the
    other, so this asserts the pair rather than each in isolation.
    """
    n = task_prompt.count("CRITICAL - ")
    assert n >= 2, f"expected both CRITICAL boundary clauses, found {n}"


def test_pagination_is_the_first_signal(task_prompt):
    """`Page 2 of 2` is the one unambiguous, self-contained continuation signal,
    so it must be checked before the softer header-block heuristics."""
    rules = task_prompt[task_prompt.index("<boundary-detection-rules>") :]
    i_pag = rules.index("PAGINATION")
    i_hdr = rules.index("OPENING HEADER BLOCK")
    assert i_pag < i_hdr, "pagination must be evaluated before the header block"


def test_class_descriptions_take_precedence(task_prompt):
    """A class that documents its own page ordering must win over the generic
    rules — otherwise the rules would override a deliberate configuration."""
    assert "PRECEDENCE:" in task_prompt


def test_the_rules_are_inside_the_cacheable_prefix(task_prompt):
    """Cost: classification runs PER PAGE.

    Everything before `<<CACHEPOINT>>` is a cache read on the 2nd page onward, so
    placing ~650 tokens of rules after it would re-bill them for every page of
    every document.
    """
    assert task_prompt.index("<boundary-detection-rules>") < task_prompt.index(
        "<<CACHEPOINT>>"
    )


def test_the_rules_do_not_mention_a_specific_model(task_prompt):
    """#653 was reported as a Sonnet 5 defect and it is not one.

    The same over-splitting was measured on Nova 2 Lite, which — unlike Sonnet 5
    — is not in `_CLAUDE_4_7_BASE_NAMES` and so does receive `temperature: 0`.
    The cause is the prompt, so the fix must not be conditioned on a model.
    """
    rules = task_prompt[
        task_prompt.index("<boundary-detection-rules>") : task_prompt.index(
            "</boundary-detection-rules>"
        )
    ]
    for name in ("sonnet", "nova", "claude", "opus", "haiku"):
        assert name not in rules.lower(), (
            f"rules must be model-agnostic, found {name!r}"
        )


def test_the_output_format_still_asks_for_document_boundary(task_prompt):
    """The rules are useless if the model is not asked to emit the field."""
    assert '"document_boundary"' in task_prompt


def test_the_prompt_still_carries_its_required_placeholders(task_prompt):
    """A prompt edit must not drop the substitutions the service performs."""
    for ph in (
        "{CLASS_NAMES_AND_DESCRIPTIONS}",
        "{DOCUMENT_TEXT}",
        "{FEW_SHOT_EXAMPLES}",
    ):
        assert ph in task_prompt, ph
