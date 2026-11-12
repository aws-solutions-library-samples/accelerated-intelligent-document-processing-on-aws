# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Structured outputs as the extraction SERVICE runs it (#1785).

Every test drives the real ``ExtractionService.process_document_section`` with
Bedrock mocked, because that is where the feature either works or does not: the
output format has to reach ``invoke_model``, a normal reply has to bypass the
repair path, a truncated or refused one has to take it, a rejected schema has to
fall back instead of failing the document, and the whole thing has to be inert
when the setting is off.
"""

from __future__ import annotations

import json
from textwrap import dedent
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from idp_common.extraction import structured_output as so_mod
from idp_common.extraction.forced_tool import EXTRACTION_TOOL_NAME
from idp_common.extraction.service import ExtractionService
from idp_common.models import Document, Page, Section, Status

pytestmark = pytest.mark.unit

_CLAUDE = "us.anthropic.claude-sonnet-5-5"

_PROPS = {
    "invoice_number": {"type": "string", "description": "The invoice number"},
    "total_amount": {"type": "number", "description": "The total amount"},
    "Account Number": {"type": "string", "description": "A name Bedrock rejects"},
}


def _config(properties=_PROPS, *, enabled=True, model=_CLAUDE, forced=False):
    return {
        "classes": [
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "invoice",
                "x-aws-idp-document-type": "invoice",
                "type": "object",
                "description": "An invoice document",
                "properties": properties,
            }
        ],
        "extraction": {
            "model": model,
            "temperature": 0.0,
            "top_k": 5,
            "system_prompt": "You are a document extraction assistant.",
            "task_prompt": dedent("""
                Extract from this {DOCUMENT_CLASS} document:
                {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
                {DOCUMENT_TEXT}
            """),
            "structured_output": {"enabled": enabled},
            "forced_tool": {"enabled": forced},
            "coercion": {"enabled": False},
            "validation": {"enabled": False},
        },
    }


def _document():
    doc = Document(
        id="test-doc",
        input_key="test-document.pdf",
        input_bucket="input-bucket",
        output_bucket="output-bucket",
        status=Status.EXTRACTING,
    )
    doc.pages["1"] = Page(
        page_id="1",
        image_uri="s3://input-bucket/test-document.pdf/pages/1/image.jpg",
        parsed_text_uri="s3://input-bucket/test-document.pdf/pages/1/parsed.txt",
    )
    doc.sections.append(
        Section(section_id="1", classification="invoice", page_ids=["1"])
    )
    return doc


def _reply(text, stop="end_turn"):
    return {
        "response": {
            "output": {"message": {"content": [{"text": text}]}},
            "stopReason": stop,
        },
        "metering": {"tokens": 100},
    }


def _wire_name(kwargs, authored):
    """The sanitized name the output-format schema uses for ``authored``."""
    js = kwargs["output_config"]["textFormat"]["structure"]["jsonSchema"]
    props = json.loads(js["schema"])["properties"]
    if authored in props:
        return authored
    (name,) = [p for p in props if p not in _PROPS]
    return name


def _validation_error(message="Schema is too complex"):
    return ClientError(
        {"Error": {"Code": "ValidationException", "Message": message}}, "Converse"
    )


def _run(config, side_effect, *, repair=None):
    svc = ExtractionService(region="us-west-2", config=config)
    patches = [
        patch("idp_common.s3.get_text_content", return_value="Page 1 text"),
        patch("idp_common.image.prepare_image", return_value=b"img"),
        patch(
            "idp_common.image.prepare_bedrock_image_attachment",
            return_value={"image": "b64"},
        ),
        patch("idp_common.bedrock.invoke_model", side_effect=side_effect),
        patch("idp_common.s3.write_content"),
        patch("idp_common.utils.merge_metering_data", return_value={"tokens": 100}),
        patch("idp_common.metrics.put_metric"),
    ]
    if repair is not None:
        patches.append(
            patch(
                "idp_common.extraction.service.repair_truncated_json",
                side_effect=repair,
            )
        )
    started = [p.start() for p in patches]
    try:
        svc.process_document_section(_document(), "1")
    finally:
        for p in patches:
            p.stop()
    inv, write = started[3], started[4]
    written = write.call_args[0][0] if write.call_args else None
    return written, [c.kwargs for c in inv.call_args_list]


@pytest.fixture(autouse=True)
def _clear_rejections():
    so_mod._REJECTED_SCHEMAS.clear()
    yield
    so_mod._REJECTED_SCHEMAS.clear()


def _never_repair(*_a, **_k):
    raise AssertionError("the repair path must not run for a structured reply")


class TestTheOutputFormatReachesBedrock:
    def test_passed_with_a_sanitized_closed_schema(self):
        calls = []

        def answer(**kw):
            calls.append(kw)
            return _reply("{}")

        _run(_config(), answer)
        cfg = calls[0]["output_config"]
        js = cfg["textFormat"]["structure"]["jsonSchema"]
        schema = json.loads(js["schema"])
        assert cfg["textFormat"]["type"] == "json_schema"
        assert js["name"] == so_mod.OUTPUT_FORMAT_NAME
        assert schema["additionalProperties"] is False
        assert "Account Number" not in schema["properties"]
        assert len(schema["properties"]) == 3
        # No forced tool on top of it.
        assert calls[0]["tool_config"] is None

    def test_off_by_default_sends_nothing(self):
        cfg = _config()
        del cfg["extraction"]["structured_output"]
        _, calls = _run(cfg, lambda **kw: _reply('{"invoice_number": "X"}'))
        assert "output_config" not in calls[0]


class TestANormalReplyIsTheJson:
    def test_parsed_directly_and_names_restored(self):
        def answer(**kw):
            acct = _wire_name(kw, "Account Number")
            return _reply(
                json.dumps(
                    {"invoice_number": "INV-1", "total_amount": 12.5, acct: "77"}
                )
            )

        written, _ = _run(_config(), answer, repair=_never_repair)
        assert written["inference_result"] == {
            "invoice_number": "INV-1",
            "total_amount": 12.5,
            "Account Number": "77",
        }
        meta = written["metadata"]
        assert meta["parsing_succeeded"] is True
        assert "output_repaired" not in meta
        so = meta["structured_output"]
        assert so["applied"] is True and so["parsed"] is True
        assert so["stop_reason"] == "end_turn"
        # All three optional fields are sent required-and-nullable.
        assert so["schema"]["made_nullable"] == 3
        assert so["schema"]["optional_properties"] == 0

    def test_a_fenced_reply_is_not_trusted_as_structured(self):
        """Constrained decoding emits bare JSON. Anything else is not trusted as
        the structured answer, and goes down the ordinary text path."""
        written, _ = _run(
            _config(), lambda **kw: _reply('```json\n{"invoice_number": "A"}\n```')
        )
        so = written["metadata"]["structured_output"]
        assert so["applied"] is True and so["parsed"] is False
        assert written["inference_result"]["invoice_number"] == "A"


class TestStopReasonsThatAreNotAnAnswer:
    def test_max_tokens_takes_the_repair_path(self):
        seen = []

        def repair(text):
            seen.append(text)
            return {"invoice_number": "PART"}, {
                "was_truncated": True,
                "repair_method": "close",
            }

        written, _ = _run(
            _config(),
            lambda **kw: _reply('{"invoice_number": "PART', stop="max_tokens"),
            repair=repair,
        )
        assert seen, "a max_tokens stop must reach the repair path"
        so = written["metadata"]["structured_output"]
        assert so["parsed"] is False and so["stop_reason"] == "max_tokens"
        assert written["metadata"]["output_repaired"] is True

    @pytest.mark.parametrize("stop", ["max_tokens", "content_filtered"])
    def test_json_that_happens_to_parse_is_still_not_trusted(self, stop):
        """The stop reason decides, not whether the text parses: a reply cut off
        at a point where it is still valid JSON is incomplete all the same."""
        written, _ = _run(
            _config(), lambda **kw: _reply('{"invoice_number": "A"}', stop=stop)
        )
        so = written["metadata"]["structured_output"]
        assert so["parsed"] is False and so["stop_reason"] == stop

    @pytest.mark.parametrize("stop", ["content_filtered", "guardrail_intervened"])
    def test_a_refusal_is_not_parsed_as_structured(self, stop):
        written, _ = _run(_config(), lambda **kw: _reply("I can't help", stop=stop))
        so = written["metadata"]["structured_output"]
        assert so["parsed"] is False and so["stop_reason"] == stop


class TestFallbacksNeverFailTheDocument:
    def test_a_rejected_schema_retries_on_todays_path(self):
        calls = []

        def answer(**kw):
            calls.append(kw)
            if "output_config" in kw:
                raise _validation_error()
            return _reply('{"invoice_number": "INV-9"}')

        written, _ = _run(_config(), answer)
        assert len(calls) == 2
        assert "output_config" not in calls[1]
        assert written["inference_result"]["invoice_number"] == "INV-9"
        so = written["metadata"]["structured_output"]
        assert so["applied"] is False
        assert "too complex" in so["rejected"]

    def test_the_rejection_is_remembered_for_the_next_section(self):
        def answer(**kw):
            if "output_config" in kw:
                raise _validation_error()
            return _reply('{"invoice_number": "INV-9"}')

        _run(_config(), answer)
        second = []

        def again(**kw):
            second.append(kw)
            return _reply('{"invoice_number": "INV-9"}')

        written, _ = _run(_config(), again)
        assert len(second) == 1 and "output_config" not in second[0]
        assert (
            "rejected this schema earlier"
            in (written["metadata"]["structured_output"]["skipped"])
        )

    def test_a_rejection_falls_back_to_the_forced_tool_when_that_is_on(self):
        calls = []

        def answer(**kw):
            calls.append(kw)
            if "output_config" in kw:
                raise _validation_error()
            return _reply('{"invoice_number": "INV-3"}')

        _run(_config(model="us.anthropic.claude-sonnet-5", forced=True), answer)
        assert calls[0]["tool_config"] is None
        tools = calls[1]["tool_config"]["tools"]
        assert tools[0]["toolSpec"]["name"] == EXTRACTION_TOOL_NAME

    def test_a_non_validation_error_is_not_swallowed(self):
        def answer(**kw):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                "Converse",
            )

        with pytest.raises(ClientError):
            _run(_config(), answer)

    def test_an_unsupported_model_skips_and_records_why(self):
        written, calls = _run(
            _config(model="us.amazon.nova-pro-v1:0"),
            lambda **kw: _reply('{"invoice_number": "N"}'),
        )
        assert "output_config" not in calls[0]
        so = written["metadata"]["structured_output"]
        assert so["applied"] is False and "not listed" in so["skipped"]

    def test_a_schema_that_does_not_fit_skips_and_records_why(self):
        props = {f"f{i}": {"type": "string"} for i in range(40)}
        written, calls = _run(_config(props), lambda **kw: _reply('{"f0": "x"}'))
        assert "output_config" not in calls[0]
        so = written["metadata"]["structured_output"]
        assert so["applied"] is False and "does not fit" in so["skipped"]
        assert written["inference_result"]["f0"] == "x"


class TestPrecedenceOverForcedTool:
    def test_forced_tool_records_that_it_stood_down(self):
        written, calls = _run(
            _config(model="us.anthropic.claude-sonnet-5", forced=True),
            lambda **kw: _reply('{"invoice_number": "Z"}'),
        )
        assert calls[0]["tool_config"] is None
        assert "output_config" in calls[0]
        assert "structured outputs" in written["metadata"]["forced_tool"]["skipped"]
