# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""parse_table parses the section's own OCR text, not a copy the model retypes.

The extraction agent used to be told to call ``parse_table`` with "the full document
text", so the tool's input was model output: a reproduction of every page, costing as
many output tokens as the section holds and not guaranteed to match the OCR. The
parser's blank-header recovery decides by comparing the label rows that open each
page's fragment, so a copy whose repeated label rows drift turns the recovery off and
the table's header rows come back as data. Measured on the nightly's
``samples/Nuveen.pdf``: one ``parse_table`` call returned 534 rows for a 532-row
table, while the same run's OCR, parsed directly, returns 532.

So the tool is built with the section text (``create_parse_table_tool(source_text=)``)
and ignores ``table_text`` when it has it. The fixture below has the artifact's exact
shape -- a blank header row on every page, the label row repeated on every page, the
wrapped "Fund Name" label on page 1 only -- and a ``_DRIFTED`` copy in which the later
pages' label rows were tidied away, which parses to the +2 the nightly saw.

The wiring tests drive each real caller to the point Strands builds the agent (the
``built_agents`` harness of ``test_throttle_retry_ownership``, reused here as ``recorded_agents``), take the
``parse_table`` tool off that agent, and call it with the drifted copy. What is
asserted is what the agent's own tool does, on every path that builds one.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from idp_common.config.models import IDPConfig
from idp_common.extraction import agentic_idp
from idp_common.extraction.service import SectionInfo
from idp_common.extraction.tools.table_parser import (
    create_parse_table_tool,
    parse_markdown_tables,
)

from . import test_throttle_retry_ownership as _harness
from .test_throttle_retry_ownership import (
    _Built,
    _Full,
    _Section,
    _service,
    _StatusOnly,
)

# The harness's agent-recording fixture, reused rather than copied. pytest registers
# it under the attribute name that holds it here.
recorded_agents = _harness.built_agents

pytestmark = pytest.mark.agentic

FUNDS_PER_PAGE = 6
PAGES = (1, 2, 3)
REAL_ROWS = FUNDS_PER_PAGE * len(PAGES)

_LABELS = (
    "|  | Ticker | Record Date | Ex-Dividend Date | Payment Date "
    "| Short Term | Long Term |\n"
)
_SEPARATOR = "|---|---|---|---|---|---|---|\n"


def _page(n: int) -> str:
    rows = "\n".join(
        f"| Fund {n}-{i} | TK{n}{i:02d}X | 12/11/2024 | 12/11/2024 | 12/12/2024 "
        f"| 0.{i:02d} | 1.{i:02d} |"
        for i in range(FUNDS_PER_PAGE)
    )
    return (
        f"--- PAGE {n} ---\nNuveen capital gains estimates.\n\n"
        "|  |  |  |  |  |  |  |\n"
        + _SEPARATOR
        + _LABELS
        + ("| Fund Name |  |  |  |  |  |  |\n" if n == 1 else "")
        + rows
        + "\n\nConsult a tax advisor.\n"
    )


_DOCUMENT = "\n".join(_page(n) for n in PAGES)
# What a model's copy can look like: page 1 intact, the label row on later pages
# dropped. Nothing else differs, and it is enough to switch the recovery off.
_DRIFTED = _DOCUMENT.replace(_LABELS, "").replace(_SEPARATOR, _SEPARATOR + _LABELS, 1)


def _rows(result: dict[str, Any]) -> int:
    return sum(t["row_count"] for t in result.get("tables") or [result])


# -- the fixture really has the failure's shape ---------------------------------------


def test_the_document_parses_to_its_real_rows():
    assert _rows(parse_markdown_tables(_DOCUMENT)) == REAL_ROWS


def test_the_drifted_copy_parses_two_header_rows_as_data():
    """Non-vacuity: without this the tests below could pass on any text at all."""
    assert _rows(parse_markdown_tables(_DRIFTED)) == REAL_ROWS + 2


# -- the tool -----------------------------------------------------------------------


def test_with_the_section_text_a_drifted_copy_is_not_parsed():
    result = create_parse_table_tool(source_text=_DOCUMENT)(table_text=_DRIFTED)
    assert _rows(result) == REAL_ROWS
    assert result["text_source"] == "document_text"
    assert result["ignored_table_text"] is True


def test_with_the_section_text_no_argument_is_needed():
    result = create_parse_table_tool(source_text=_DOCUMENT)()
    assert _rows(result) == REAL_ROWS
    assert result["ignored_table_text"] is False


def test_without_the_section_text_the_agents_text_is_parsed():
    """Direct callers that build the tool without a section keep the old contract."""
    result = create_parse_table_tool()(table_text=_DOCUMENT)
    assert _rows(result) == REAL_ROWS
    assert result["text_source"] == "table_text"


def test_with_neither_the_call_is_a_bedrock_valid_error():
    result = create_parse_table_tool()()
    assert result["status"] == "error"
    assert result["parser_status"] == "error"
    assert result["content"] and "text" in result["content"][0]


# -- every path that builds an agent hands the tool the section text ------------------


def _parse_table_of(agent) -> Any:
    tool = agent.tool_registry.registry.get("parse_table")
    assert tool is not None, "table parsing is on, so the agent must carry parse_table"
    return tool


def _assert_parses_the_section(built_agents_list) -> None:
    assert len(built_agents_list) == 1
    result = _parse_table_of(built_agents_list[0])(table_text=_DRIFTED)
    assert result["text_source"] == "document_text"
    assert _rows(result) == REAL_ROWS


class _Doc(BaseModel):
    name: str | None = None


def _table_config() -> IDPConfig:
    config = IDPConfig()
    config.extraction.agentic.table_parsing.enabled = True
    return config


def test_the_single_pass_agent_parses_the_section(recorded_agents):
    svc = _service(table_parsing={"enabled": True})
    svc._page_texts = [_page(n) for n in PAGES]
    svc._document_text = _DOCUMENT
    svc._page_images = []
    section = SectionInfo(
        class_label="Invoice",
        sorted_page_ids=["1", "2", "3"],
        page_indices=[0, 1, 2],
        output_bucket="b",
        output_key="k",
        output_uri="s3://b/k",
        start_page=1,
        end_page=3,
    )
    content = svc._build_prompt_content("Extract:\n{DOCUMENT_TEXT}\nEnd.", [])
    with pytest.raises(_Built):
        svc._invoke_extraction_model(content, "system", section)
    _assert_parses_the_section(recorded_agents)


def test_a_shard_agent_parses_its_own_shard_text(recorded_agents):
    payload = {
        "content": [{"text": "pages"}],
        "page_start": 0,
        "page_end": 3,
        "total_pages": 3,
        "assess_document_text": _DOCUMENT,
    }
    with pytest.raises(_Built):
        asyncio.run(
            agentic_idp.default_shard_runner(
                shard_index=0,
                total_shards=2,
                payload=payload,
                model_id="us.amazon.nova-pro-v1:0",
                data_format=_Doc,
                config=_table_config(),
                context="Extraction",
                max_retries=7,
                connect_timeout=10.0,
                read_timeout=60.0,
                max_tokens=None,
                checkpoint_callback=None,
                custom_instruction=None,
            )
        )
    _assert_parses_the_section(recorded_agents)


@pytest.mark.parametrize("subset", [True, False], ids=["field-subset", "whole"])
def test_an_escalation_agent_parses_the_section(recorded_agents, subset):
    svc = _service(
        table_parsing={"enabled": True},
        validation={
            "enabled": True,
            "fail_action": "escalate",
            "escalation_model": "us.amazon.nova-pro-v1:0",
        },
    )
    svc._document_text = _DOCUMENT
    bad = {"invoice_id": "INV-9", "status": "pending"}
    whole = patch(
        "idp_common.extraction.service.build_subset_schema",
        side_effect=lambda schema, _fields: schema,
    )
    with (
        patch(
            "idp_common.extraction.service.create_pydantic_model_from_json_schema",
            return_value=_StatusOnly,
        ),
        whole if not subset else nullcontext(),
    ):
        with pytest.raises(_Built):
            svc._validate_and_maybe_escalate(
                extracted_fields=dict(bad),
                structured_data=_Full(**bad),
                data_model=_Full,
                model_id="us.amazon.nova-pro-v1:0",
                message_prompt="(p)",
                agentic_images=[],
                custom_instruction=None,
                section_info=_Section(),
                parsing_succeeded=True,
            )
    _assert_parses_the_section(recorded_agents)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_the_sync_wrapper_forwards_it_across_its_thread_hop(recorded_agents):
    """Inside a running loop, structured_output runs the agent on a fresh thread,
    which starts with an empty context -- why the text is passed as an argument
    rather than through a ContextVar the way confidence data is.

    The harness stops the call by raising a BaseException once the agent exists;
    on that worker thread it is not handed back to the caller, which then fails
    its own ``result is not None`` check. The agent was recorded either way, and
    the agent is what is asserted on."""

    async def inside_a_loop():
        agentic_idp.structured_output(
            model_id="us.amazon.nova-pro-v1:0",
            data_format=_Doc,
            prompt="pages",
            config=_table_config(),
            table_source_text=_DOCUMENT,
        )

    with pytest.raises((_Built, AssertionError)):
        asyncio.run(inside_a_loop())
    _assert_parses_the_section(recorded_agents)


# -- the instruction no longer asks for a copy ---------------------------------------


def test_the_system_prompt_tells_the_agent_not_to_pass_the_text():
    addendum = agentic_idp.TABLE_PARSING_PROMPT_ADDENDUM
    assert "Call parse_table with NO table_text" in addendum
    assert "Call parse_table with the full document text" not in addendum


# -- the instruction matches what the tool can do -----------------------------------


def test_the_tool_description_tells_a_tool_holding_the_text_to_be_called_empty():
    spec = create_parse_table_tool(source_text=_DOCUMENT).tool_spec
    assert "Call this with NO table_text" in spec["description"]
    assert (
        "Leave empty"
        in spec["inputSchema"]["json"]["properties"]["table_text"]["description"]
    )


def test_the_tool_description_asks_for_markdown_when_the_tool_holds_none():
    """Agentic rules discovery builds the tool with no section text; told "pass
    nothing", its agent would spend a turn on the error."""
    spec = create_parse_table_tool().tool_spec
    assert "Pass the table's Markdown as table_text" in spec["description"]
    assert "Call this with NO table_text" not in spec["description"]


@pytest.mark.parametrize(
    "source,step",
    [
        (_DOCUMENT, agentic_idp.PARSE_TABLE_STEP_HOLDS_TEXT),
        (None, agentic_idp.PARSE_TABLE_STEP_NEEDS_TEXT),
    ],
    ids=["holds-text", "no-text"],
)
def test_the_system_prompt_step_follows_the_tool(recorded_agents, source, step):
    other = (
        agentic_idp.PARSE_TABLE_STEP_NEEDS_TEXT
        if step is agentic_idp.PARSE_TABLE_STEP_HOLDS_TEXT
        else agentic_idp.PARSE_TABLE_STEP_HOLDS_TEXT
    )
    with pytest.raises(_Built):
        asyncio.run(
            agentic_idp.structured_output_async(
                model_id="us.amazon.nova-pro-v1:0",
                data_format=_Doc,
                prompt="pages",
                config=_table_config(),
                table_source_text=source,
            )
        )
    prompt = recorded_agents[0].system_prompt
    assert step in prompt
    assert other not in prompt


def test_the_addendum_carries_the_line_that_is_swapped():
    """The swap is a string replace, so the line must really be in the template."""
    assert (
        agentic_idp.PARSE_TABLE_STEP_HOLDS_TEXT
        in agentic_idp.TABLE_PARSING_PROMPT_ADDENDUM
    )


@pytest.mark.parametrize(
    "strength,names_the_convention",
    [("MANDATORY", True), ("STRONGLY_RECOMMENDED", True), ("RECOMMENDED", False)],
)
def test_the_large_table_guidance_does_not_ask_for_a_copy(
    strength, names_the_convention
):
    """This block precedes the pre-parsed table guidance in the same instruction, on
    exactly the large tables this change is about. RECOMMENDED says only "call
    parse_table", which never asked for a copy; OPTIONAL emits nothing."""
    svc = _service(table_parsing={"enabled": True})
    text = svc._build_table_parsing_guidance(
        {"recommendation_strength": strength},
        {"recommendation_strength": strength, "estimated_row_count": 532},
    )
    assert "parse_table" in text
    assert ("parse_table with NO table_text" in text) is names_the_convention
    assert "full document text" not in text
    assert "document text containing the table" not in text


def test_the_map_tool_description_agrees_on_the_calling_convention():
    """The model reads both tool descriptions in the same agent."""
    from idp_common.extraction.tools.table_parser import (
        create_map_table_to_schema_tool,
    )

    description = create_map_table_to_schema_tool().tool_spec["description"]
    assert "parse_table on the document text" not in description


# -- table_indices scopes what map_table_to_schema maps -----------------------------


class _State:
    def __init__(self):
        self._data: dict[str, Any] = {}

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value):
        self._data[key] = value


class _Agent:
    def __init__(self):
        self.state = _State()


# Two tables that share column names: the funds this document reports on, and a list
# of funds closed during the year. map_table_to_schema already drops rows whose
# mapped columns are all empty, so only an unrelated table sharing column names can
# leak rows into the array -- the case table_indices exists for.
_TWO_TABLES = (
    "--- PAGE 1 ---\n"
    "| Fund | Ticker |\n|---|---|\n| A | AAAAX |\n| B | BBBBX |\n\n"
    "Funds closed during the year.\n\n"
    "| Fund | Ticker | Closed |\n|---|---|---|\n| C | CCCCX | 06/30/2024 |\n"
)


def test_without_a_table_index_every_table_is_mapped():
    """Non-vacuity for the table_indices tests below: with no selection, the
    fixture's two tables both map, so a narrowed result there is the selection's
    doing."""
    from idp_common.extraction.tools.table_parser import (
        create_map_table_to_schema_tool,
    )

    agent = _Agent()
    create_parse_table_tool(source_text=_TWO_TABLES)(agent=agent)
    mapped = create_map_table_to_schema_tool()(
        column_mapping={"Fund": "fund", "Ticker": "ticker"}, agent=agent
    )
    assert mapped["row_count"] == 3


def test_the_error_result_has_the_documented_shape():
    result = create_parse_table_tool()()
    for key in ("columns", "rows", "row_count", "table_count", "quality", "warnings"):
        assert key in result, key
    assert result["ignored_table_text"] is False


# Two fragments of one table that the merge did not join (merging switched off
# here; in the wild, fragments too far apart), so parse_table reports two tables
# with identical columns -- the case its fragmentation warning is about.
_FRAGMENTS = (
    "--- PAGE 1 ---\n| Fund | Ticker |\n|---|---|\n| A | AAAAX |\n| B | BBBBX |\n\n"
    "--- PAGE 2 ---\n| Fund | Ticker |\n|---|---|\n| C | CCCCX |\n"
)


def test_the_agent_is_told_in_conversation_that_its_copy_was_not_used():
    """Without this, an agent that sends the text anyway gets no signal and can keep
    paying output tokens to retype the section on later turns."""
    result = create_parse_table_tool(source_text=_DOCUMENT)(table_text=_DRIFTED)
    summary = result["content"][0]["json"]
    assert any("table_text was not used" in w for w in summary["warnings"])


# -- observability --------------------------------------------------------------------


def test_the_invocation_log_carries_the_warnings_this_call_added(caplog):
    with caplog.at_level("INFO", logger="idp_common.extraction.tools.table_parser"):
        create_parse_table_tool(source_text=_DOCUMENT)(table_text=_DRIFTED)
    record = next(
        r for r in caplog.records if r.getMessage() == "parse_table tool invoked"
    )
    assert record.ignored_table_text is True
    assert any("table_text was not used" in w for w in record.warnings)


def test_a_call_with_nothing_to_parse_is_logged(caplog):
    with caplog.at_level("WARNING", logger="idp_common.extraction.tools.table_parser"):
        create_parse_table_tool()()
    assert any("no text to parse" in r.getMessage() for r in caplog.records)


def _map(agent):
    from idp_common.extraction.tools.table_parser import (
        create_map_table_to_schema_tool,
    )

    return create_map_table_to_schema_tool()(
        column_mapping={"Fund": "fund", "Ticker": "ticker"}, agent=agent
    )


def _tickers(mapped) -> list[str]:
    return [r["ticker"] for r in mapped.get("mapped_rows", [])]


def test_table_indices_maps_only_the_tables_named():
    agent = _Agent()
    create_parse_table_tool(source_text=_TWO_TABLES)(table_indices=[1], agent=agent)
    assert _tickers(_map(agent)) == ["CCCCX"]


def test_table_indices_tells_same_column_tables_apart():
    """With two same-column tables -- unrelated ones ("Transactions" and "Pending
    transactions") or fragments the merge did not join; this fixture is the
    latter -- the agent names the one it wants and is warned about the other."""
    agent = _Agent()
    result = create_parse_table_tool(
        source_text=_FRAGMENTS, auto_merge_adjacent_tables=False
    )(table_indices=[1], agent=agent)
    assert _tickers(_map(agent)) == ["CCCCX"]
    # ...and is told the other may be a fragment of it.
    assert any("[0] have the same columns" in w for w in result["warnings"])


def test_a_repeated_index_maps_that_table_once():
    """A model emitting [1, 0, 0] must not map table 0's rows twice: each
    selected table is mapped once."""
    agent = _Agent()
    create_parse_table_tool(source_text=_FRAGMENTS, auto_merge_adjacent_tables=False)(
        table_indices=[1, 0, 0], agent=agent
    )
    assert _tickers(_map(agent)) == ["AAAAX", "BBBBX", "CCCCX"]


def test_an_index_that_selects_nothing_says_so():
    """Every other way the agent's call can miss leaves a warning; so does this."""
    result = create_parse_table_tool(source_text=_TWO_TABLES)(table_index=7)
    assert any("selects nothing" in w for w in result["warnings"])


@pytest.mark.parametrize("indices", [[], [8, 9]], ids=["empty", "all-miss"])
def test_a_selection_of_nothing_maps_nothing(indices):
    """Asserted through map_table_to_schema, which falls back to the top-level rows
    when there are no tables: the top level must be empty too."""
    agent = _Agent()
    result = create_parse_table_tool(source_text=_TWO_TABLES)(
        table_indices=indices, agent=agent
    )
    assert _tickers(_map(agent)) == []
    assert (result["rows"], result["columns"], result["row_count"]) == ([], [], 0)
    # ...and says so, the empty list included: every other way to miss warns.
    assert any("selected no table" in w for w in result["warnings"])


def test_the_result_the_agent_reads_describes_the_selection():
    """It builds its column mapping and checks its row count against the top level,
    so the top level must be the tables that will be mapped, not table 0."""
    result = create_parse_table_tool(source_text=_TWO_TABLES)(table_indices=[1])
    assert result["columns"] == ["Fund", "Ticker", "Closed"]
    assert result["row_count"] == 1
    assert [r["Ticker"] for r in result["rows"]] == ["CCCCX"]
    assert result["mapped_table_indices"] == [1]
    assert result["table_count"] == 1 and result["tables_found"] == 2
    summary = result["content"][0]["json"]
    assert summary["mapped_table_indices"] == [1]
    assert (summary["table_count"], summary["tables_found"]) == (1, 2)


def test_table_indices_take_precedence_over_table_index():
    agent = _Agent()
    result = create_parse_table_tool(source_text=_TWO_TABLES)(
        table_index=0, table_indices=[1], agent=agent
    )
    assert any("table_index was ignored" in w for w in result["warnings"])
    assert result["columns"] == ["Fund", "Ticker", "Closed"]
    assert _tickers(_map(agent)) == ["CCCCX"]


def test_table_index_alone_does_not_change_what_is_mapped():
    """Its meaning before this change: it chooses the table shown at the top level."""
    agent = _Agent()
    result = create_parse_table_tool(source_text=_TWO_TABLES)(
        table_index=1, agent=agent
    )
    assert result["columns"] == ["Fund", "Ticker", "Closed"]
    assert _tickers(_map(agent)) == ["AAAAX", "BBBBX", "CCCCX"]


def test_a_narrowed_selection_keeps_the_status_contract_in_state():
    """State's parser_status/status/content match the returned result, as they do
    when nothing was narrowed (test_tool_writeback pins that path)."""
    agent = _Agent()
    returned = create_parse_table_tool(source_text=_TWO_TABLES)(
        table_indices=[0], agent=agent
    )
    stored = agent.state.get("last_parse_table_result")
    assert stored is not returned  # the narrowed copy, not the result itself
    assert len(stored["tables"]) == 1
    for key in ("parser_status", "status", "content"):
        assert stored[key] == returned[key], key


def test_a_multi_table_selection_reports_the_quality_of_what_it_selected():
    """The agent compares parse_success_rate with min_parse_success_rate to decide
    whether to fall back to row-by-row extraction, so the top level must not carry
    an unselected table's quality."""
    from idp_common.extraction.tools.table_parser import _combined_quality

    clean = {
        "row_count": 3,
        "quality": {
            "parse_success_rate": 1.0,
            "total_cells": 6,
            "empty_cell_count": 0,
            "consistent_column_count": True,
        },
    }
    rough = {
        "row_count": 1,
        "quality": {
            "parse_success_rate": 0.0,
            "total_cells": 2,
            "empty_cell_count": 2,
            "consistent_column_count": False,
        },
    }
    combined = _combined_quality([clean, rough])
    assert combined["parse_success_rate"] == pytest.approx(0.75)  # row-weighted
    assert combined["total_cells"] == 8 and combined["empty_cell_count"] == 2
    assert combined["empty_cell_ratio"] == pytest.approx(0.25)
    assert combined["consistent_column_count"] is False


def test_the_top_level_quality_is_the_selections_not_table_0s():
    """Wired through the tool: selecting tables 0 and 1 of the fragments fixture
    reports a quality computed over both rather than table 0's alone."""
    result = create_parse_table_tool(
        source_text=_FRAGMENTS, auto_merge_adjacent_tables=False
    )(table_indices=[0, 1])
    t0, t1 = result["tables"][0]["quality"], result["tables"][1]["quality"]
    assert result["quality"]["total_cells"] == t0["total_cells"] + t1["total_cells"]


def test_a_call_with_nothing_to_parse_clears_an_earlier_parse_from_state():
    """Otherwise map_table_to_schema maps the previous call's rows after this one."""
    agent = _Agent()
    tool_fn = create_parse_table_tool()
    tool_fn(table_text=_TWO_TABLES, agent=agent)
    tool_fn(agent=agent)
    assert _tickers(_map(agent)) == []


def test_combined_quality_sums_counts_and_reindexes_cells():
    from idp_common.extraction.tools.table_parser import _combined_quality

    a = {
        "row_count": 10,
        "quality": {
            "cells_matched": 30,
            "low_confidence_cells": [{"row": 2, "column": "Ticker"}],
        },
    }
    b = {
        "row_count": 10,
        "quality": {
            "cells_matched": 30,
            "low_confidence_cells": [{"row": 4, "column": "Ticker"}],
        },
    }
    combined = _combined_quality([a, b])
    assert combined["cells_matched"] == 60
    # Table b's row 4 is row 14 of the selection's concatenated rows.
    assert [c["row"] for c in combined["low_confidence_cells"]] == [2, 14]


def test_the_summary_lists_every_table_so_the_agent_can_choose():
    """table_indices is the only way to scope the mapping, so the result the model
    reads must say what each index refers to."""
    result = create_parse_table_tool(source_text=_TWO_TABLES)()
    listed = result["content"][0]["json"]["tables"]
    assert listed == [
        {"index": 0, "columns": ["Fund", "Ticker"], "row_count": 2},
        {"index": 1, "columns": ["Fund", "Ticker", "Closed"], "row_count": 1},
    ]


def test_one_table_is_not_listed():
    single = "| Fund | Ticker |\n|---|---|\n| A | AAAAX |\n"
    result = create_parse_table_tool(source_text=single)()
    assert "tables" not in result["content"][0]["json"]


def test_a_confidence_one_table_lacks_does_not_hide_the_others():
    from idp_common.extraction.tools.table_parser import _combined_quality

    combined = _combined_quality(
        [
            {"row_count": 2, "quality": {"avg_confidence": None}},
            {"row_count": 2, "quality": {"avg_confidence": 97.0}},
        ]
    )
    assert combined["avg_confidence"] == pytest.approx(97.0)


def test_the_system_prompt_names_table_indices():
    assert "table_indices" in agentic_idp.PARSE_TABLE_STEP_HOLDS_TEXT
    assert "table_indices" in agentic_idp.PARSE_TABLE_STEP_NEEDS_TEXT


def test_the_preflight_block_names_table_indices_only_for_several_tables():
    """It is the instruction that drives the table workflow; one table needs no
    choice, so its wording (pinned in test_shard_payloads) stays as it was."""
    from idp_common.extraction.service import ExtractionService

    def block(count) -> str:
        out = ExtractionService._append_preflight_table_guidance(
            None,
            {
                "status": "success",
                "table_count": count,
                "tables": [{"row_count": 5}] * count,
                "columns": ["Fund"],
            },
        )
        assert out is not None
        return out

    assert "table_indices" in block(2)
    assert "table_indices" not in block(1)


@pytest.mark.parametrize("strength", ["MANDATORY", "STRONGLY_RECOMMENDED"])
def test_the_large_table_guidance_names_table_indices(strength):
    svc = _service(table_parsing={"enabled": True})
    text = svc._build_table_parsing_guidance(
        {"recommendation_strength": strength},
        {"recommendation_strength": strength, "estimated_row_count": 532},
    )
    assert "table_indices" in text


def test_selecting_tables_with_different_columns_warns():
    result = create_parse_table_tool(source_text=_TWO_TABLES)(table_indices=[0, 1])
    assert any("different columns" in w for w in result["warnings"])


def test_an_empty_selection_keeps_the_parsers_quality():
    unselected = create_parse_table_tool(source_text=_TWO_TABLES)()["quality"]
    result = create_parse_table_tool(source_text=_TWO_TABLES)(table_indices=[])
    assert result["quality"] == unselected


def test_table_index_on_several_tables_says_every_table_will_be_mapped():
    """The older argument does not scope the mapping; an agent that uses it on a
    multi-table section is told so, and how to scope it."""
    result = create_parse_table_tool(source_text=_TWO_TABLES)(table_index=0)
    warning = next(w for w in result["warnings"] if "only chooses" in w)
    assert "table_indices=[0]" in warning
