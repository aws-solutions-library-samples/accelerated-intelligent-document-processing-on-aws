# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The system prompt's cache point survives the move off ``cache_prompt``.

strands 1.48 deprecates the ``BedrockModel`` ``cache_prompt`` option and emits a
``UserWarning`` from ``format_request`` whenever it is set. Its documented
replacement is a ``cachePoint`` block in the system content, so agentic
extraction now passes the system prompt as ``[text, cachePoint]`` instead.

These tests drive the REAL ``structured_output_async`` with the REAL strands
``Agent`` and ``BedrockModel``, and capture the request ``format_request`` builds
on its way to ``converse_stream``. The seam is ``format_request`` itself, wrapped
rather than replaced, so what is asserted is the request strands would send.
"""

from __future__ import annotations

import asyncio
import warnings
from typing import Any

import pytest

pytest.importorskip("strands", reason="agentic extras not installed")

from pydantic import BaseModel  # noqa: E402

from idp_common.extraction import agentic_idp  # noqa: E402

CLAUDE_MODEL = "us.anthropic.claude-sonnet-4-6"
CACHE_POINT = {"cachePoint": {"type": "default"}}


class _Stop(Exception):
    """Raised once the request is captured, so no network call is attempted."""


class _Doc(BaseModel):
    status: str | None = None


def _requests_for(model_id: str, prompt_cache: str = "auto"):
    """Run one extraction and return (captured requests, warnings raised)."""
    from strands.models.bedrock import BedrockModel

    from idp_common.config.models import IDPConfig

    config = IDPConfig(extraction={"prompt_cache": prompt_cache})
    captured: list[dict[str, Any]] = []
    real_format_request = BedrockModel.format_request

    def _capture(self, *args, **kwargs):
        request = real_format_request(self, *args, **kwargs)
        captured.append(request)
        raise _Stop()

    with (
        pytest.MonkeyPatch.context() as patched,
        warnings.catch_warnings(record=True) as caught,
    ):
        warnings.simplefilter("always")
        patched.setenv("AWS_DEFAULT_REGION", "us-west-2")
        patched.setattr(BedrockModel, "format_request", _capture)
        with pytest.raises(Exception) as raised:
            asyncio.run(
                agentic_idp.structured_output_async(
                    model_id=model_id,
                    data_format=_Doc,
                    prompt="extract this",
                    config=config,
                    max_retries=1,
                )
            )
    assert captured, (
        "format_request was never reached, so nothing below is about the request; "
        f"the run stopped with {raised.value!r}"
    )
    return captured, list(caught)


@pytest.mark.agentic
def test_the_system_prompt_ends_in_a_cache_point_for_a_caching_model():
    requests, _ = _requests_for(CLAUDE_MODEL)
    system = requests[0]["system"]
    assert system[-1] == CACHE_POINT, (
        f"the system content no longer ends in a cache point: {system[-1]!r}"
    )
    assert [b for b in system if "cachePoint" in b] == [CACHE_POINT], (
        "exactly one system cache point is expected, as cache_prompt produced"
    )
    assert "text" in system[0] and system[0]["text"].strip()


@pytest.mark.agentic
def test_the_request_still_carries_all_three_cache_points():
    """System, toolConfig and the trailing prompt block: the count the
    four-block Bedrock limit in ``test_retry_does_not_duplicate_prompt`` is
    pinned against."""
    requests, _ = _requests_for(CLAUDE_MODEL)
    request = requests[0]
    tools = request["toolConfig"]["tools"]
    message_points = [
        block
        for message in request["messages"]
        for block in message["content"]
        if "cachePoint" in block
    ]
    assert sum("cachePoint" in b for b in request["system"]) == 1
    assert sum("cachePoint" in t for t in tools) == 1
    assert len(message_points) == 1


@pytest.mark.agentic
def test_no_cache_prompt_deprecation_warning_is_raised():
    _, caught = _requests_for(CLAUDE_MODEL)
    deprecated = [w for w in caught if "cache_prompt is deprecated" in str(w.message)]
    assert not deprecated, f"strands still warns: {deprecated[0].message}"


@pytest.mark.agentic
def test_prompt_cache_off_sends_no_system_cache_point():
    requests, _ = _requests_for(CLAUDE_MODEL, prompt_cache="off")
    assert not any("cachePoint" in b for b in requests[0]["system"])


@pytest.mark.agentic
def test_a_1m_model_id_still_gets_the_system_cache_point():
    """``_build_model_config`` strips ``:1m`` before deciding on caching; the
    system cache point is decided on the same stripped id."""
    requests, _ = _requests_for("us.anthropic.claude-opus-4-7:1m")
    assert requests[0]["system"][-1] == CACHE_POINT


@pytest.mark.agentic
@pytest.mark.parametrize(
    "model_id",
    sorted(agentic_idp.CACHEPOINT_SUPPORTED_MODELS),
)
def test_the_helper_agrees_with_the_model_capability_table(model_id):
    content = agentic_idp._build_system_prompt_content("sys", model_id)
    assert content == [{"text": "sys"}, CACHE_POINT]


@pytest.mark.agentic
def test_the_helper_returns_the_plain_prompt_for_a_model_not_sent_cache_points():
    unsupported = "us.meta.llama3-3-70b-instruct-v1:0"
    assert not agentic_idp.supports_prompt_caching(unsupported)
    assert agentic_idp._build_system_prompt_content("sys", unsupported) == "sys"


@pytest.mark.agentic
def test_the_capability_table_is_not_empty():
    """An empty table would collect the parametrised test above as a skip."""
    assert agentic_idp.CACHEPOINT_SUPPORTED_MODELS


@pytest.mark.agentic
def test_the_decision_is_made_on_the_id_without_the_1m_suffix(monkeypatch):
    """Today every ``:1m`` id is listed beside its base id, so no real model can
    tell the stripped lookup from the raw one; a table listing only the base id
    can. ``_build_model_config`` decides on the stripped id, and the system
    cache point must agree with it."""
    monkeypatch.setattr(
        agentic_idp, "CACHEPOINT_SUPPORTED_MODELS", ["us.anthropic.claude-opus-4-7"]
    )
    content = agentic_idp._build_system_prompt_content(
        "sys", "us.anthropic.claude-opus-4-7:1m"
    )
    assert content == [{"text": "sys"}, CACHE_POINT]
