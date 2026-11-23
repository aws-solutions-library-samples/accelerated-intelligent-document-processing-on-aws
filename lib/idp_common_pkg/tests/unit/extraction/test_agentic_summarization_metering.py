# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Conversation-summarization calls are metered like every other model call (#1952).

Strands' ``SummarizingConversationManager`` summarizes a conversation that has
overflowed the context window by calling the agent's model directly. The agent loop
is what records a model call's usage in ``agent.event_loop_metrics``, so the
summary's tokens were billed and reported nowhere: not on the success path, which
reads the ``AgentResult``'s metrics, and not on the failure path, which reads the
agent's (#1923).

Two levels, both on the real Strands classes:

- The manager on its own: a real ``Agent`` around a ``strands.models.Model`` stub,
  and ``reduce_context`` called as the agent loop calls it.
- The whole agentic extraction: ``structured_output_async`` with the real
  ``BedrockModel``, only ``converse_stream`` of the boto3 client replaced, scripted
  so the agent overflows its context once and Strands summarizes for real.

The stream chunks are Converse stream events, the shape ``BedrockModel`` passes
through unchanged and ``strands.event_loop.streaming.process_stream`` reads
(``metadata.usage`` carrying ``inputTokens``/``outputTokens``/``totalTokens`` and the
cache fields). The summary request is recognised by its system prompt, which is
Strands' own ``DEFAULT_SUMMARIZATION_PROMPT``, so the stub answers it correctly in
whatever position Strands makes it.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import boto3
import botocore.exceptions
import pytest
from pydantic import BaseModel

for _name in [
    k for k in list(sys.modules) if k == "strands" or k.startswith("strands.")
]:
    if isinstance(sys.modules[_name], MagicMock):
        del sys.modules[_name]

_agentic = pytest.importorskip(
    "idp_common.extraction.agentic_idp",
    reason="strands-agents not installed (a stub does not count)",
)

from strands import Agent  # noqa: E402
from strands.agent.conversation_manager import (  # noqa: E402
    SummarizingConversationManager,
)
from strands.agent.conversation_manager.compression.context_compression import (  # noqa: E402
    DEFAULT_SUMMARIZATION_PROMPT,
)
from strands.models import Model  # noqa: E402

from idp_common.agents.utils import conversation_manager as cm  # noqa: E402
from idp_common.bedrock.model_utils import metering_model_id  # noqa: E402
from idp_common.config.models import IDPConfig  # noqa: E402
from idp_common.utils import bedrock_utils  # noqa: E402
from idp_common.utils.metering import failure_metering  # noqa: E402

pytestmark = pytest.mark.unit

_MODEL = "us.anthropic.claude-sonnet-4-6"
_CONTEXT = "Extraction"
_KEY = f"{_CONTEXT}/bedrock/{metering_model_id(_MODEL)}"

#: Usage of one ordinary agent turn.
_TURN_USAGE = {
    "inputTokens": 1000,
    "outputTokens": 50,
    "totalTokens": 1050,
    "cacheReadInputTokens": 300,
    "cacheWriteInputTokens": 20,
}
#: Usage of the summarization call: different in every unit from a turn, so a
#: total that includes it cannot be mistaken for one more ordinary turn.
_SUMMARY_USAGE = {
    "inputTokens": 7001,
    "outputTokens": 403,
    "totalTokens": 7404,
    "cacheReadInputTokens": 11,
    "cacheWriteInputTokens": 3,
}


def _text_chunks(text: str, usage: dict[str, int]) -> list[dict[str, Any]]:
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": dict(usage), "metrics": {"latencyMs": 1}}},
    ]


def _plus(*usages: dict[str, int]) -> dict[str, int]:
    total: dict[str, int] = {}
    for usage in usages:
        for unit, value in usage.items():
            total[unit] = total.get(unit, 0) + value
    return total


# ------------------------------------------------- the manager, on a real Agent


class _SummaryModel(Model):
    """A ``strands.models.Model`` that answers every call with one summary."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def update_config(self, **model_config: Any) -> None:
        return None

    def get_config(self) -> Any:
        return {"model_id": "stub"}

    def structured_output(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append({"messages": messages, "system_prompt": system_prompt})
        for chunk in _text_chunks("The summary.", _SUMMARY_USAGE):
            yield chunk


def _conversation(turns: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for n in range(turns):
        messages.append({"role": "user", "content": [{"text": f"question {n}"}]})
        messages.append({"role": "assistant", "content": [{"text": f"answer {n}"}]})
    return messages


def _agent_with(manager: Any, model: _SummaryModel) -> Agent:
    agent = Agent(model=model, conversation_manager=manager, callback_handler=None)
    agent.messages = _conversation(6)
    return agent


def test_strands_own_manager_does_not_meter_its_summary():
    """The defect, measured on the pinned library rather than taken from its docs.

    If a Strands release starts recording the summary itself, this goes red, and
    the subclass would then count every summary twice.
    """
    model = _SummaryModel()
    agent = _agent_with(
        SummarizingConversationManager(summary_ratio=0.5, preserve_recent_messages=2),
        model,
    )
    agent.event_loop_metrics.reset_usage_metrics()
    agent.conversation_manager.reduce_context(agent, e=RuntimeError("overflow"))

    assert len(model.calls) == 1
    assert model.calls[0]["system_prompt"] == DEFAULT_SUMMARIZATION_PROMPT
    assert agent.event_loop_metrics.accumulated_usage["inputTokens"] == 0
    assert agent.event_loop_metrics.accumulated_usage["outputTokens"] == 0


def test_the_metered_manager_adds_the_summary_usage_to_the_agent():
    model = _SummaryModel()
    agent = _agent_with(
        cm.MeteredSummarizingConversationManager(
            summary_ratio=0.5, preserve_recent_messages=2
        ),
        model,
    )
    agent.event_loop_metrics.reset_usage_metrics()
    before = len(agent.messages)
    agent.conversation_manager.reduce_context(agent, e=RuntimeError("overflow"))

    # Strands' summarization ran unchanged: one call, its prompt, a shorter history
    # that now starts with the summary.
    assert len(model.calls) == 1
    assert model.calls[0]["system_prompt"] == DEFAULT_SUMMARIZATION_PROMPT
    assert len(agent.messages) < before
    assert agent.messages[0]["content"][0].get("text") == "The summary."
    # Every unit of the summary's usage reached the agent's metrics, in the running
    # total and in the current invocation's record.
    metrics = agent.event_loop_metrics
    assert dict(metrics.accumulated_usage) == _SUMMARY_USAGE
    assert dict(metrics.agent_invocations[-1].usage) == _SUMMARY_USAGE


def test_a_summary_outside_an_invocation_still_reaches_the_total():
    """``update_usage`` needs an invocation record; the total must not depend on it."""
    model = _SummaryModel()
    agent = _agent_with(
        cm.MeteredSummarizingConversationManager(
            summary_ratio=0.5, preserve_recent_messages=2
        ),
        model,
    )
    assert agent.event_loop_metrics.agent_invocations == []
    agent.conversation_manager.reduce_context(agent)
    assert len(model.calls) == 1
    assert dict(agent.event_loop_metrics.accumulated_usage) == _SUMMARY_USAGE


def test_two_summaries_add_up():
    model = _SummaryModel()
    agent = _agent_with(
        cm.MeteredSummarizingConversationManager(
            summary_ratio=0.5, preserve_recent_messages=2
        ),
        model,
    )
    agent.event_loop_metrics.reset_usage_metrics()
    agent.conversation_manager.reduce_context(agent, e=RuntimeError("overflow"))
    agent.messages.extend(_conversation(4))
    agent.conversation_manager.reduce_context(agent, e=RuntimeError("overflow"))
    assert len(model.calls) == 2
    assert dict(agent.event_loop_metrics.accumulated_usage) == _plus(
        _SUMMARY_USAGE, _SUMMARY_USAGE
    )


def test_a_metering_failure_does_not_break_the_summary(caplog):
    """The summary is what keeps the run alive; losing its metering must not end it."""
    model = _SummaryModel()
    agent = _agent_with(
        cm.MeteredSummarizingConversationManager(
            summary_ratio=0.5, preserve_recent_messages=2
        ),
        model,
    )
    agent.event_loop_metrics = None  # pyright: ignore[reportAttributeAccessIssue]
    with caplog.at_level("WARNING"):
        agent.conversation_manager.reduce_context(agent, e=RuntimeError("overflow"))
    assert agent.messages[0]["content"][0].get("text") == "The summary."
    assert "Could not record conversation-summarization token usage" in caplog.text


def test_the_summarizer_sees_the_real_agent_apart_from_its_model():
    """Only ``model`` is replaced; anything else a Strands release reads is real."""
    agent = _agent_with(
        cm.MeteredSummarizingConversationManager(preserve_recent_messages=2),
        _SummaryModel(),
    )
    wrapped = cm._UsageReportingModel(agent.model, lambda _u: None)
    view = cm._SummaryAgentView(agent, wrapped)
    assert view.model is wrapped
    assert view.messages is agent.messages
    assert view.event_loop_metrics is agent.event_loop_metrics
    assert wrapped.get_config() == agent.model.get_config()


def test_a_summarization_agent_is_refused():
    """A dedicated agent would record the summary on its own metrics."""
    helper = Agent(model=_SummaryModel(), callback_handler=None)
    with pytest.raises(ValueError, match="summarization_agent"):
        cm.MeteredSummarizingConversationManager(summarization_agent=helper)


def test_a_summarization_agent_set_later_is_refused_too():
    """Strands reads the attribute at summary time, so assignment is a route in."""
    helper = Agent(model=_SummaryModel(), callback_handler=None)
    manager = cm.MeteredSummarizingConversationManager()
    with pytest.raises(ValueError, match="summarization_agent"):
        manager.summarization_agent = helper  # pyright: ignore[reportAttributeAccessIssue]
    assert manager.summarization_agent is None


def test_outside_an_invocation_the_summary_adds_to_an_existing_total(caplog):
    """The fallback adds token counts and skips Bedrock's ``cacheDetails`` list."""

    class _DetailedModel(_SummaryModel):
        async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
            self.calls.append({"messages": messages, "system_prompt": system_prompt})
            for chunk in _text_chunks("The summary.", _SUMMARY_USAGE):
                if "metadata" in chunk:
                    # First, so a list that broke the loop would lose every count.
                    usage = chunk["metadata"]["usage"]
                    chunk["metadata"]["usage"] = {
                        "cacheDetails": [{"ttl": "5m", "inputTokens": 11}],
                        **usage,
                    }
                yield chunk

    model = _DetailedModel()
    agent = _agent_with(
        cm.MeteredSummarizingConversationManager(
            summary_ratio=0.5, preserve_recent_messages=2
        ),
        model,
    )
    cast(dict, agent.event_loop_metrics.accumulated_usage).update(_TURN_USAGE)
    with caplog.at_level("WARNING"):
        agent.conversation_manager.reduce_context(agent)
    assert "Could not record" not in caplog.text
    assert len(model.calls) == 1
    assert dict(agent.event_loop_metrics.accumulated_usage) == _plus(
        _TURN_USAGE, _SUMMARY_USAGE
    )


# ------------------------------------------ the whole agentic extraction, end to end


class _Invoice(BaseModel):
    invoice_number: str


def _tool_call(payload: dict[str, Any], n: int) -> list[dict[str, Any]]:
    return [
        {"messageStart": {"role": "assistant"}},
        {
            "contentBlockStart": {
                "start": {"toolUse": {"toolUseId": f"t{n}", "name": "extraction_tool"}}
            }
        },
        {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(payload)}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": dict(_TURN_USAGE), "metrics": {"latencyMs": 1}}},
    ]


def _overflow() -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "Input is too long for requested model.",
            }
        },
        "ConverseStream",
    )


def _throttle() -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Too many requests"}},
        "ConverseStream",
    )


INVALID = "invalid"
VALID = "valid"
TEXT = "text"
OVERFLOW = "overflow"
THROTTLE = "throttle"


class _Script:
    """Agent turns answer from ``steps`` then ``tail``; a summary request answers
    with ``_SUMMARY_USAGE`` and is counted separately."""

    def __init__(self, steps: list[str], tail: str | None = None) -> None:
        self.steps = list(steps)
        self.tail = tail
        self.turns_answered = 0
        self.summaries = 0

    def __call__(self, **request: Any) -> dict[str, Any]:
        system = request.get("system") or []
        if any(block.get("text") == DEFAULT_SUMMARIZATION_PROMPT for block in system):
            self.summaries += 1
            return {"stream": _text_chunks("Summary of the work.", _SUMMARY_USAGE)}
        if self.steps:
            step = self.steps.pop(0)
        elif self.tail is not None:
            step = self.tail
        else:
            raise AssertionError("the agent made a model call the script did not plan")
        if step == OVERFLOW:
            raise _overflow()
        if step == THROTTLE:
            raise _throttle()
        self.turns_answered += 1
        if step == VALID:
            return {
                "stream": _tool_call(
                    {"extraction": {"invoice_number": "INV-1"}}, self.turns_answered
                )
            }
        if step == INVALID:
            return {"stream": _tool_call({"extraction": {}}, self.turns_answered)}
        if step == TEXT:
            return {"stream": _text_chunks("Done.", _TURN_USAGE)}
        raise AssertionError(f"unknown step {step!r}")


async def _instant_sleep(_seconds: float, *args: Any, **kwargs: Any) -> None:
    return None


def _run(script: _Script):
    real_client = boto3.session.Session.client

    def _client(self, *args: Any, **kwargs: Any):
        client = real_client(self, *args, **kwargs)
        client.converse_stream = script
        return client

    mp = pytest.MonkeyPatch()
    mp.setenv("AWS_DEFAULT_REGION", "us-west-2")
    mp.setenv("AWS_ACCESS_KEY_ID", "AKIA" + "IOSFODNN7EXAMPLE")
    mp.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMIK7MDENG" + "bPxRfiCYEXAMPLEKEY")
    mp.delenv("AWS_SESSION_TOKEN", raising=False)
    mp.delenv("AWS_PROFILE", raising=False)
    mp.setattr(boto3.session.Session, "client", _client)
    mp.setattr(bedrock_utils.asyncio, "sleep", _instant_sleep)
    # Small enough that three tool turns are summarizable; the model-derived
    # default keeps 10+ recent messages for this model, which a short stub
    # conversation never exceeds.
    mp.setattr(_agentic, "_summarization_params", lambda *_a, **_k: (2, 0.9))
    try:
        return asyncio.run(
            _agentic.structured_output_async(
                model_id=_MODEL,
                data_format=_Invoice,
                prompt="Extract the invoice number.",
                config=IDPConfig(),
                context=_CONTEXT,
                strands_throttle_retries=False,
            )
        )
    finally:
        mp.undo()


def _turns(n: int) -> dict[str, int]:
    return _plus(*([_TURN_USAGE] * n))


def test_a_successful_run_is_metered_for_its_summary():
    script = _Script([INVALID, INVALID, INVALID, OVERFLOW, VALID, TEXT])
    result, response = _run(script)

    assert result == _Invoice(invoice_number="INV-1")
    # Strands really summarized, once, and every planned turn was consumed.
    assert script.summaries == 1
    assert script.turns_answered == 5
    assert script.steps == []
    metering = {
        k: dict(v) for k, v in response["metering"].items() if not k.startswith("_")
    }
    # One key: the summary was made by the extraction model, so it is billed under
    # the extraction model's key rather than as a separate line.
    assert metering == {_KEY: _plus(_turns(5), _SUMMARY_USAGE)}


def test_a_failed_run_is_metered_for_its_summary():
    """The failure path reads the agent's metrics (#1923); the summary is in them."""
    script = _Script([INVALID, INVALID, INVALID, OVERFLOW, INVALID], THROTTLE)
    with pytest.raises(Exception) as info:
        _run(script)

    assert script.summaries == 1
    assert script.turns_answered == 4
    assert failure_metering(info.value) == {_KEY: _plus(_turns(4), _SUMMARY_USAGE)}


# ------------------------------------------------ no unmetered summarizer is built


_REPO = Path(__file__).resolve().parents[5]
_OWNER = "lib/idp_common_pkg/idp_common/agents/utils/conversation_manager.py"


#: Every word that names a way into Strands' unmetered summarization: the manager
#: (class and module), the helper it calls, the method this package overrides, the
#: ``Agent(context_manager=...)`` facade and the package behind it, and the
#: attribute that selects Strands' unmetered summarization-agent path.
_SUMMARIZER_WORDS = frozenset(
    {
        "SummarizingConversationManager",
        "summarizing_conversation_manager",
        "generate_summary",
        "context_compression",
        "_generate_summary_with_model",
        "context_manager",
        "_context_manager",
        "summarization_agent",
    }
)


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text))


def _unmetered_summarizer_uses(source: str) -> list[int]:
    """Lines that reach Strands' summarization by any name the source spells.

    The rule is about the words, not about call shapes: a hit is any identifier,
    attribute, keyword argument, parameter, definition, import (module path or
    name) or string or bytes constant that contains one of ``_SUMMARIZER_WORDS``. So a dict of
    ``Agent`` keyword arguments, ``getattr``, ``importlib``, ``__dict__`` lookups,
    an import of the helper and an override of the metered method are all read,
    because each has to write one of those words somewhere. Docstrings are not
    read. What it cannot read is a name assembled at run time from pieces, or
    arguments loaded from data.
    """
    tree = ast.parse(source)
    # A bare string statement (a docstring) is prose; it cannot be passed to
    # anything, so it is the one string constant not read.
    prose = {
        id(stmt.value)
        for stmt in ast.walk(tree)
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
    }
    lines: set[int] = set()
    for node in ast.walk(tree):
        if id(node) in prose:
            continue
        words: set[str] = set()
        if isinstance(node, ast.Name):
            words = {node.id}
        elif isinstance(node, ast.Attribute):
            words = {node.attr}
        elif isinstance(node, ast.keyword) and node.arg:
            words = {node.arg}
        elif isinstance(node, ast.arg):
            words = {node.arg}
        elif isinstance(node, ast.ExceptHandler) and node.name:
            words = {node.name}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            words = {node.name}
        elif isinstance(node, ast.ImportFrom):
            words = _words(node.module or "")
            for alias in node.names:
                words |= {alias.name, alias.asname or ""}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                words |= _words(alias.name) | {alias.asname or ""}
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            words = _words(node.value)
        elif isinstance(node, ast.Constant) and isinstance(node.value, bytes):
            words = _words(node.value.decode("latin-1"))
        if words & _SUMMARIZER_WORDS:
            lines.add(getattr(node, "lineno", 0))
    return sorted(lines)


def _production_python() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [
        p
        for p in out
        if "/tests/" not in f"/{p}"
        and not Path(p).name.startswith("test_")
        and Path(p).name != "conftest.py"
    ]


def test_the_rule_reads_every_route_it_names():
    """Each spelling is caught on its own, so a green scan is not a blind one."""
    samples = [
        "from strands.agent.conversation_manager import SummarizingConversationManager",
        "from x import SummarizingConversationManager as S",
        "import strands\nstrands.agent.conversation_manager."
        "SummarizingConversationManager()",
        "from strands import Agent\nAgent(context_manager='auto')",
        "Agent(**{'context_manager': 'auto'})",
        "kw = {}\nkw['context_manager'] = 'agentic'\nAgent(**kw)",
        "dict(context_manager='auto')",
        "getattr(cm, 'SummarizingConversationManager')",
        "import importlib\nimportlib.import_module("
        "'strands.agent.conversation_manager.summarizing_conversation_manager')",
        "cm.__dict__['SummarizingConversationManager']",
        "from strands.agent.conversation_manager.compression.context_compression "
        "import generate_summary",
        "from strands.agent.conversation_manager.compression import generate_summary",
        "from strands._context_manager.modes import agentic",
        "import strands._context_manager.modes.agentic as m",
        "x = 1\n'''SummarizingConversationManager''' + ''\n",
        "def build(model, context_manager='auto'):\n"
        "    return Agent(model=model, **locals())",
        "f = lambda context_manager='auto': Agent(**locals())",
        "Agent(**{b'context_manager'.decode(): 'auto'})",
        "class X(MeteredSummarizingConversationManager):\n"
        "    summarization_agent = helper",
        "class M(Metered):\n    def _generate_summary_with_model(self, m, a):\n"
        "        return None",
    ]
    for sample in samples:
        assert _unmetered_summarizer_uses(sample), sample
    assert not _unmetered_summarizer_uses(
        '"""Uses SummarizingConversationManager and context_manager in prose."""\n'
    )
    assert not _unmetered_summarizer_uses(
        "from idp_common.agents.utils.conversation_manager import "
        "MeteredSummarizingConversationManager\n"
        "MeteredSummarizingConversationManager(summary_ratio=0.5)"
    )


def test_no_production_module_builds_an_unmetered_summarizer():
    files = _production_python()
    # The scan reaches the call site this issue was about.
    assert "lib/idp_common_pkg/idp_common/extraction/agentic_idp.py" in files
    assert _OWNER in files
    found = {}
    for path in files:
        if path == _OWNER:
            continue
        try:
            source = (_REPO / path).read_text(encoding="utf-8")
        except FileNotFoundError:  # deleted in the working tree, not yet in git
            continue
        uses = _unmetered_summarizer_uses(source)
        if uses:
            found[path] = uses
    assert found == {}, (
        "Build conversation summarization with "
        "idp_common.agents.utils.conversation_manager."
        "MeteredSummarizingConversationManager: Strands' own manager summarizes "
        f"with a model call no agent metric records (#1952). Found: {found}"
    )
