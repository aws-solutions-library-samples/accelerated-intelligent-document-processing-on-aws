# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#895: a model that cannot emit a valid tool-use sequence fails once, with advice.

Bedrock reports "Model produced invalid sequence as part of ToolUse" as
``modelStreamErrorException`` — a code that IS transient in general, because
``ConverseStream`` really does break mid-stream for transport reasons. A Nova Lite
benchmark grid logged 247 of them in three hours: every one was surfaced as
``TransientError`` and retried by ``workflow.asl.json`` for every shard of every
document, so a model that simply cannot run the Advanced (agentic) path looked like
a flaky stack and left documents in the shard map for 45+ minutes.

``idp_common.utils.transient_errors`` (tested in
``tests/unit/utils/test_transient_errors.py``) stops the retries. This module covers
the other half: the extraction path translates the bare stream error into a message
that names the model, says the fault is a capability limit rather than a transient
one, and suggests a model that does work.

Placement note: the strands-dependent tests under ``tests/unit/extraction/agentic_idp/``
are skipped by that directory's ``conftest.py`` whenever ``CI`` is set, so a test
there would never run in CI. This file lives one level up, where collection is not
gated, and follows ``test_schema_restatement.py`` in restoring the real ``strands``
package before importing (a MagicMock stub does not count as installed).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import botocore.exceptions
import pytest
from pydantic import BaseModel

from idp_common.utils.transient_errors import is_transient_error, raise_if_transient

for _name in [
    k for k in list(sys.modules) if k == "strands" or k.startswith("strands.")
]:
    if isinstance(sys.modules[_name], MagicMock):
        del sys.modules[_name]

_agentic = pytest.importorskip(
    "idp_common.extraction.agentic_idp",
    reason="strands-agents not installed (a stub does not count)",
)

_TOOL_USE_MESSAGE = (
    "Model produced invalid sequence as part of ToolUse. Please refer to the "
    "model tool use troubleshooting guide."
)
_NOVA_LITE = "us.amazon.nova-lite-v1:0"


def _stream_error():
    """The ``EventStreamError`` from the #895 log streams, verbatim in shape."""
    return botocore.exceptions.EventStreamError(
        {"Error": {"Code": "modelStreamErrorException", "Message": _TOOL_USE_MESSAGE}},
        "ConverseStream",
    )


class _Tiny(BaseModel):
    invoice_number: str


class TestTheMessage:
    """What the user reads in the Step Functions cause and the document's error."""

    def test_it_names_the_model_that_failed(self):
        msg = _agentic._explain_invalid_tool_use_sequence(_stream_error(), _NOVA_LITE)
        assert _NOVA_LITE in msg

    def test_it_says_the_fault_is_a_capability_limit_not_a_transient_one(self):
        """Without this the fast failure reads as a broken stack, and the operator
        retries the whole batch instead of changing the model."""
        msg = _agentic._explain_invalid_tool_use_sequence(_stream_error(), _NOVA_LITE)
        assert "capability limitation" in msg
        assert "NOT a transient fault" in msg

    def test_it_names_the_outcome_and_the_bedrock_code(self):
        """So the message can be matched to the raw log line it replaces."""
        msg = _agentic._explain_invalid_tool_use_sequence(_stream_error(), _NOVA_LITE)
        assert "invalid tool-use sequence" in msg
        assert "modelStreamErrorException" in msg

    def test_it_suggests_what_to_change(self):
        msg = _agentic._explain_invalid_tool_use_sequence(_stream_error(), _NOVA_LITE)
        assert "extraction.model" in msg
        # The simple path needs no tool use at all, so it is the other way out.
        assert "extraction.mode to simple" in msg
        for model in _agentic._AGENTIC_CAPABLE_EXAMPLE_MODELS:
            assert model in msg

    def test_it_keeps_the_underlying_error(self):
        msg = _agentic._explain_invalid_tool_use_sequence(_stream_error(), _NOVA_LITE)
        assert _TOOL_USE_MESSAGE in msg

    def test_an_unknown_model_id_does_not_produce_a_none(self):
        """``model_id`` is threaded down from ``structured_output_async``; a caller
        that does not pass it must still get a readable message."""
        msg = _agentic._explain_invalid_tool_use_sequence(_stream_error(), None)
        assert "None" not in msg
        assert "the configured extraction model" in msg


class TestTheSuggestedModelsAreReal:
    """The remedy is only useful if the model ids in it exist. These are taken from
    ``docs/extraction-and-confidence.md`` and must resolve in ``pricing.yaml``."""

    def test_every_suggested_model_is_priced_in_the_config_library(self):
        pricing = (
            Path(__file__).resolve().parents[5] / "config_library" / "pricing.yaml"
        )
        if not pricing.exists():  # installed-package test run, no repo alongside
            pytest.skip("config_library/pricing.yaml not present in this layout")
        text = pricing.read_text(encoding="utf-8")
        for model in _agentic._AGENTIC_CAPABLE_EXAMPLE_MODELS:
            assert f"bedrock/{model}\n" in text, f"{model} is not in pricing.yaml"


class TestTheTranslation:
    """``_invoke_agent_for_extraction`` is where the raw stream error is caught."""

    def _run(self, exc, model_id=_NOVA_LITE):
        async def _boom(**_kwargs):
            raise exc

        async def _go():
            return await _agentic._invoke_agent_for_extraction(
                agent=MagicMock(),
                prompt_content=[],
                data_format=_Tiny,
                model_id=model_id,
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_agentic, "invoke_agent_with_retry", _boom)
            return asyncio.run(_go())

    def test_the_stream_error_becomes_the_named_deterministic_exception(self):
        original = _stream_error()
        with pytest.raises(_agentic.ModelInvalidToolUseSequence) as info:
            self._run(original)
        assert info.value.__cause__ is original
        assert _NOVA_LITE in str(info.value)
        # The NAME is what a reader (and any future Retry list) sees; it must not be
        # the one name the state machine retries.
        assert type(info.value).__name__ == "ModelInvalidToolUseSequence"

    def test_it_is_raised_on_the_first_attempt_not_after_the_extraction_retries(self):
        """``max_extraction_retries`` exists for a model that answers but answers
        badly. This model cannot answer at all, so the loop must not spin."""
        calls = {"n": 0}
        original = _stream_error()

        async def _boom(**_kwargs):
            calls["n"] += 1
            raise original

        async def _go():
            return await _agentic._invoke_agent_for_extraction(
                agent=MagicMock(),
                prompt_content=[],
                data_format=_Tiny,
                max_extraction_retries=3,
                model_id=_NOVA_LITE,
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_agentic, "invoke_agent_with_retry", _boom)
            with pytest.raises(_agentic.ModelInvalidToolUseSequence):
                asyncio.run(_go())
        assert calls["n"] == 1

    def test_the_translated_failure_is_still_not_transient(self):
        """Belt and braces: the shard handler classifies whatever reaches it, and
        the ToolUse text is in the chain either way."""
        with pytest.raises(_agentic.ModelInvalidToolUseSequence) as info:
            self._run(_stream_error())
        assert is_transient_error(info.value) is False
        raise_if_transient(info.value, where="shard runtime")  # must not raise

    def test_an_unrelated_failure_is_re_raised_untouched(self):
        """The translation is narrow: nothing else changes shape."""
        boom = RuntimeError("agent loop failed for an unrelated reason")
        with pytest.raises(RuntimeError) as info:
            self._run(boom)
        assert info.value is boom

    def test_a_genuine_mid_stream_break_is_re_raised_untouched(self):
        """A ``modelStreamErrorException`` WITHOUT the ToolUse text is a transport
        fault; it must keep its own type so the retry machinery still sees it."""
        transport = botocore.exceptions.EventStreamError(
            {
                "Error": {
                    "Code": "modelStreamErrorException",
                    "Message": "The connection to the model was closed unexpectedly",
                }
            },
            "ConverseStream",
        )
        with pytest.raises(botocore.exceptions.EventStreamError) as info:
            self._run(transport)
        assert info.value is transport
        assert is_transient_error(info.value) is True
