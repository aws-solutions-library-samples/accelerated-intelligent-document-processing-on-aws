# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#775: the shard budget subtracts the MEASURED per-request prompt overhead.

Before, ``compute_sizing_plan`` budgeted shards against OCR page text only and the
prompt (system prompt, rendered schema, few-shot text, tool schema) was silently
absorbed by the blanket ``context_buffer`` — so a prompt-heavy configuration was
over-budgeted, and removing ~1,100 tokens of duplicated schema prose (#710) could
never change a shard count. Now the overhead is estimated from what the service
would actually send and comes off the budget; ``context_buffer`` is a safety margin
on top of it.
"""

from __future__ import annotations

import pytest

from idp_common.bedrock.sizing import compute_sizing_plan
from idp_common.extraction.service import ExtractionService
from idp_common.extraction.sharding import estimate_tokens, plan_shards

pytestmark = pytest.mark.unit

MODEL = "us.anthropic.claude-sonnet-5"


def _schema(n_fields: int, desc_words: int) -> dict:
    return {
        "$id": "Statement",
        "type": "object",
        "properties": {
            f"Field{i}": {"type": "string", "description": "word " * desc_words}
            for i in range(n_fields)
        },
    }


def _service(
    schema: dict, *, mode: str = "simple", restate: bool = True, forced: bool = False
):
    """A service on a config MERGED with the system defaults, as at runtime — the
    default system/task prompts are most of the overhead being measured."""
    from idp_common.config.merge_utils import merge_config_with_defaults

    cfg = merge_config_with_defaults(
        {
            "extraction": {
                "mode": mode,
                "model": MODEL,
                "agentic": {
                    "enabled": mode == "advanced",
                    "restate_schema_in_system_prompt": restate,
                },
                "forced_tool": {"enabled": forced},
            },
            "classes": [schema],
        },
        validate=False,
    )
    return ExtractionService(region="us-west-2", config=cfg)


def _init_context(svc: ExtractionService, schema: dict) -> None:
    """The state _initialize_extraction_context leaves behind for a section."""
    svc._class_schema = schema
    svc._class_label = "Statement"
    svc._attribute_descriptions = svc._format_schema_for_prompt(schema)


# ------------------------------------------------------------- sizing planner


def test_prompt_overhead_comes_straight_off_the_shard_budget():
    base = compute_sizing_plan(model_id=MODEL)
    heavy = compute_sizing_plan(model_id=MODEL, prompt_overhead_tokens=12_000)
    assert heavy.shard_token_budget == base.shard_token_budget - 12_000
    assert heavy.prompt_overhead_tokens == 12_000
    assert base.prompt_overhead_tokens == 0
    assert heavy.to_dict()["prompt_overhead_tokens"] == 12_000


def test_prompt_overhead_cannot_push_the_budget_below_the_floor():
    plan = compute_sizing_plan(model_id=MODEL, prompt_overhead_tokens=10_000_000)
    assert plan.shard_token_budget == 2_000  # _MIN_SHARD_TOKEN_BUDGET
    assert (
        compute_sizing_plan(
            model_id=MODEL, prompt_overhead_tokens=-5
        ).prompt_overhead_tokens
        == 0
    )


def test_an_explicit_shard_budget_override_still_wins():
    plan = compute_sizing_plan(
        model_id=MODEL,
        prompt_overhead_tokens=12_000,
        shard_token_budget_override=40_000,
    )
    assert plan.shard_token_budget == 40_000
    assert plan.prompt_overhead_tokens == 12_000  # still reported


# ------------------------------------------------------------ service estimate


def test_no_overhead_before_the_prompt_context_exists():
    svc = _service(_schema(10, 20))
    assert svc._prompt_overhead_tokens() == 0
    assert svc._get_sizing_plan().prompt_overhead_tokens == 0


def test_overhead_is_measured_once_the_context_exists_and_the_plan_is_recomputed():
    schema = _schema(10, 20)
    svc = _service(schema)
    early = svc._get_sizing_plan()  # memoized without overhead
    _init_context(svc, schema)
    overhead = svc._prompt_overhead_tokens()
    assert overhead > estimate_tokens(
        svc._attribute_descriptions
    )  # schema + prompt text
    later = svc._get_sizing_plan()
    assert later is not early
    assert later.prompt_overhead_tokens == overhead
    assert later.shard_token_budget == early.shard_token_budget - overhead
    assert svc._shard_token_budget() == later.shard_token_budget


def test_a_larger_class_schema_shrinks_the_shard_budget():
    small, big = _service(_schema(5, 5)), _service(_schema(60, 40))
    _init_context(small, _schema(5, 5))
    _init_context(big, _schema(60, 40))
    assert big._prompt_overhead_tokens() > small._prompt_overhead_tokens() + 2_000
    assert big._shard_token_budget() < small._shard_token_budget() - 2_000


def test_the_forced_tool_schema_counts_on_the_simple_path():
    schema = _schema(30, 20)
    plain, forced = _service(schema), _service(schema, forced=True)
    _init_context(plain, schema)
    _init_context(forced, schema)
    extra = forced._prompt_overhead_tokens() - plain._prompt_overhead_tokens()
    assert extra == estimate_tokens(plain._attribute_descriptions)


def test_the_agentic_restatement_is_the_knob_that_pays_for_itself():
    """#710's restate_schema_in_system_prompt knob: turning it off now really
    frees shard budget (one schema copy), which is the mechanism #710 assumed and
    the code did not implement."""
    schema = _schema(30, 20)
    on, off = (
        _service(schema, mode="advanced", restate=True),
        _service(schema, mode="advanced", restate=False),
    )
    _init_context(on, schema)
    _init_context(off, schema)
    saved = on._prompt_overhead_tokens() - off._prompt_overhead_tokens()
    assert saved == estimate_tokens(on._attribute_descriptions)
    assert off._shard_token_budget() == on._shard_token_budget() + saved


def test_a_class_prompt_override_is_what_gets_measured():
    schema = _schema(10, 10)
    schema["x-aws-idp-extraction-task-prompt"] = "x " * 20_000 + "{DOCUMENT_TEXT}"
    svc = _service(schema)
    _init_context(svc, schema)
    assert svc._prompt_overhead_tokens() > 9_000


# --------------------------------------------------- the shard-count mechanism


def test_a_document_just_over_a_boundary_changes_shard_count_with_the_overhead():
    """The test #710's A/B could never pass: a document whose pages fit the
    text-only budget in N shards needs N+1 once the prompt overhead is charged,
    and drops back to N when the overhead is reduced (e.g. restatement off)."""
    base = compute_sizing_plan(model_id=MODEL, default_max_pages_per_shard=0)
    heavy = compute_sizing_plan(
        model_id=MODEL, default_max_pages_per_shard=0, prompt_overhead_tokens=6_000
    )
    # Six pages of just under a third of the text-only budget each: three per
    # shard fit at the text-only budget (2 shards); once 6,000 tokens of prompt
    # are charged only two fit (3 shards).
    page_tokens = base.shard_token_budget // 3 - 10
    page = "w " * (page_tokens * 4 // 2)  # "w " is 2 chars; chars/4 estimator
    assert estimate_tokens(page) <= page_tokens
    assert 2 * page_tokens <= heavy.shard_token_budget < 3 * page_tokens
    pages = [page] * 6
    n_base = len(
        plan_shards(
            pages, token_budget=base.shard_token_budget, max_pages_per_shard=None
        )
    )
    n_heavy = len(
        plan_shards(
            pages, token_budget=heavy.shard_token_budget, max_pages_per_shard=None
        )
    )
    assert n_base == 2
    assert n_heavy == 3
