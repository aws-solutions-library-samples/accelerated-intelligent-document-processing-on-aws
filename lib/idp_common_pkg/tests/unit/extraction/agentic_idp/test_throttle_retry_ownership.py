# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which callers keep Strands' in-call throttle retries (#1759).

``structured_output_async`` builds the Strands ``Agent`` for every caller, and
``strands_throttle_retries`` decides whether it keeps Strands' default
``ModelRetryStrategy``. Agentic extraction turns it off: its calls sit inside a
deadline-bounded ladder with a Step Functions retrier behind it, so a second retrier
inside the call only spends the invocation (``test_agentic_throttle_is_retried_once``).
Agentic rules discovery keeps it: that job runs from an SQS-triggered Lambda with no
published deadline and no task-level retrier.

Each test drives a real caller to the point the agent is built, records the agent
Strands actually constructed, and stops there. The strategy is read from the agent,
so what is asserted is what Strands would do, not what a keyword said.

Requires the real strands package, per the conftest in this directory.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, nullcontext
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel
from strands.event_loop.event_loop import MAX_ATTEMPTS

from idp_common.config.models import IDPConfig
from idp_common.discovery import rules_discovery as rules_discovery_module
from idp_common.discovery.rules_discovery import RulesDiscovery
from idp_common.extraction import agentic_idp
from idp_common.extraction import service as service_module
from idp_common.extraction.service import ExtractionService, SectionInfo

pytestmark = pytest.mark.agentic


class _Built(BaseException):
    """Raised once the agent exists. A BaseException, so no ``except Exception`` on
    the way out — the retry ladder, the callers' own handlers — swallows it."""


@pytest.fixture
def built_agents():
    """Every agent ``structured_output_async`` constructs, recorded as Strands built
    it; the call stops right after."""
    agents: list[Any] = []
    # Several test modules drop ``agentic_idp`` from ``sys.modules`` and re-import it,
    # so a caller can hold a function from an older copy of the module. Patch ``Agent``
    # where each caller's function actually looks it up, not only in the copy this
    # file imported — patching only that let the discovery test reach the network
    # when it ran after one of them.
    namespaces = {
        id(ns): ns
        for ns in (
            agentic_idp.__dict__,
            rules_discovery_module.structured_output.__globals__,
            service_module.structured_output.__globals__,
            service_module.concurrent_structured_output_async.__globals__,
        )
    }.values()

    with ExitStack() as stack:
        for namespace in namespaces:
            real_agent = namespace["Agent"]

            def record(*args, _real=real_agent, **kwargs):
                agent = _real(*args, **kwargs)
                agents.append(agent)
                raise _Built

            stack.enter_context(patch.dict(namespace, {"Agent": record}))
        yield agents


def _attempts(agent) -> int:
    return agent._retry_strategy._max_attempts


class _Doc(BaseModel):
    name: str | None = None


def test_strands_default_retries_more_than_once():
    """Non-vacuity: the default the discovery test asserts is a real retrier."""
    assert MAX_ATTEMPTS > 1


def test_agentic_rules_discovery_keeps_strands_retry_strategy(built_agents):
    config = IDPConfig()
    config.discovery.rules.agentic.enabled = True
    discovery = RulesDiscovery(
        input_bucket="b", input_prefix="policy.pdf", config=config
    )
    with pytest.raises(_Built):
        discovery._extract_rules_agentic(
            document_content=b"%PDF-1.4",
            file_extension="pdf",
            model_id="us.amazon.nova-pro-v1:0",
            system_prompt="s",
            user_prompt="u",
            max_tokens=1000,
        )
    assert [_attempts(a) for a in built_agents] == [MAX_ATTEMPTS]


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "$id": "Invoice",
    "x-aws-idp-document-type": "Invoice",
    "required": ["invoice_id"],
    "properties": {
        "invoice_id": {"type": "string", "pattern": "^INV-[0-9]+$"},
        "status": {"type": "string", "enum": ["paid", "due", "void"]},
    },
}


def _service(**agentic: Any) -> ExtractionService:
    config = {
        "extraction": {
            "model": "us.amazon.nova-pro-v1:0",
            "task_prompt": "Extract:\n{DOCUMENT_TEXT}\nEnd.",
            "agentic": {"enabled": True, "max_concurrent_batches": 1, **agentic},
        },
        "classes": [_SCHEMA],
    }
    svc = ExtractionService(region="us-west-2", config=config)
    svc._class_label = "Invoice"
    svc._class_schema = _SCHEMA
    svc._attribute_descriptions = "invoice_id: the invoice number"
    return svc


def test_single_pass_agentic_extraction_drops_it(built_agents):
    svc = _service()
    svc._page_texts = ["Invoice INV-1"]
    svc._document_text = "--- PAGE 1 ---\nInvoice INV-1"
    svc._page_images = []
    section = SectionInfo(
        class_label="Invoice",
        sorted_page_ids=["1"],
        page_indices=[0],
        output_bucket="b",
        output_key="k",
        output_uri="s3://b/k",
        start_page=1,
        end_page=1,
    )
    content = svc._build_prompt_content("Extract:\n{DOCUMENT_TEXT}\nEnd.", [])
    with pytest.raises(_Built):
        svc._invoke_extraction_model(content, "system", section)
    assert [_attempts(a) for a in built_agents] == [1]


def test_a_shard_agent_drops_it(built_agents):
    with pytest.raises(_Built):
        asyncio.run(
            agentic_idp._run_shard_agent(
                shard_index=0,
                total_shards=2,
                page_start=0,
                page_end=1,
                total_pages=2,
                model_id="us.amazon.nova-pro-v1:0",
                data_format=_Doc,
                shard_prompt="page one",
                config=IDPConfig(),
                context="Extraction",
                max_retries=7,
                connect_timeout=10.0,
                read_timeout=60.0,
                max_tokens=None,
                checkpoint_callback=None,
            )
        )
    assert [_attempts(a) for a in built_agents] == [1]


class _StatusOnly(BaseModel):
    status: str | None = None


class _Full(BaseModel):
    invoice_id: str
    status: str | None = None


class _Section:
    class_label = "Invoice"


@pytest.mark.parametrize("subset", [True, False], ids=["field-subset", "whole"])
def test_escalation_drops_it(built_agents, subset):
    svc = _service(
        validation={
            "enabled": True,
            "fail_action": "escalate",
            "escalation_model": "us.amazon.nova-pro-v1:0",
        }
    )
    bad = {"invoice_id": "INV-9", "status": "pending"}
    # The whole-section branch is taken when the subset schema IS the class schema.
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
    assert [_attempts(a) for a in built_agents] == [1]


async def _from_a_running_loop(**kwargs):
    # A running loop sends the sync wrapper down its thread branch.
    return agentic_idp.structured_output(**kwargs)


@pytest.mark.parametrize("in_a_loop", [False, True], ids=["asyncio.run", "thread"])
@pytest.mark.parametrize(
    ("flag", "attempts"), [(None, MAX_ATTEMPTS), (True, MAX_ATTEMPTS), (False, 1)]
)
def test_the_builder_defaults_to_strands_strategy_and_honours_the_flag(
    built_agents, in_a_loop, flag, attempts
):
    """Both entry points, both branches of the sync one: the default is Strands'
    strategy, so a caller that has no ladder-plus-retrier keeps it without asking."""
    kwargs: dict[str, Any] = {
        "model_id": "us.amazon.nova-pro-v1:0",
        "data_format": _Doc,
        "prompt": "p",
    }
    if flag is not None:
        kwargs["strands_throttle_retries"] = flag
    # On the thread branch the sentinel ends the worker thread, which the wrapper
    # reports as having produced no result; the agent was built either way.
    with pytest.raises((_Built, AssertionError)):
        if in_a_loop:
            asyncio.run(_from_a_running_loop(**kwargs))
        else:
            agentic_idp.structured_output(**kwargs)
    with pytest.raises(_Built):
        asyncio.run(agentic_idp.structured_output_async(**kwargs))
    assert [_attempts(a) for a in built_agents] == [attempts, attempts]
