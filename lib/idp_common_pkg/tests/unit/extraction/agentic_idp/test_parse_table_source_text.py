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
