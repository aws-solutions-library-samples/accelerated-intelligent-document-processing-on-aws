# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Forced tool use as the extraction SERVICE actually runs it (WS-05).

``test_forced_tool.py`` covers the pure helpers — build a toolConfig, restore
renamed properties, decide whether to force. None of it touches
``ExtractionService.process_document_section``, which is where the feature either
works or doesn't: the toolConfig has to reach ``bedrock.invoke_model``, the tool
input has to become ``inference_result``, an unhonored force has to fall back (or
not, per config), a skipped route has to be recorded, and the whole thing must be
inert when the flag is off.

Every test here drives the real section path with Bedrock mocked, and each one
asserts something that a green helper-level suite would not have caught:

* :class:`TestTheToolReachesBedrock` — the wiring itself. A feature that builds a
  perfect toolConfig and forgets to pass it is indistinguishable from one that
  works, in every helper test.
* :class:`TestHonoredAndUnhonored` — the three response outcomes, including the
  one that decides whether an operator loses data (fallback disabled).
* :class:`TestRenamedPropertiesRoundTrip` — the #709 sanitize/restore pair on the
  live path. If restore is skipped, extraction silently returns fields under
  wire-safe names nobody configured, and downstream scoring sees empty fields.
* :class:`TestOffByDefault` — the regression guard for the shipped default.
"""

from __future__ import annotations

import json
from textwrap import dedent
from unittest.mock import patch

import pytest

from idp_common.extraction.forced_tool import EXTRACTION_TOOL_NAME
from idp_common.extraction.service import ExtractionService
from idp_common.models import Document, Page, Section, Status

# A Converse-shaped model that CAN carry a toolConfig.
_MODEL = "us.anthropic.claude-sonnet-4-6"


def _config(properties, *, enabled=True, fallback=True, model=_MODEL):
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
            "forced_tool": {"enabled": enabled, "fallback_to_prompt": fallback},
            # Keep the assertions about forcing, not about downstream repair.
            "coercion": {"enabled": False},
            "validation": {"enabled": False},
        },
    }


_SIMPLE_PROPS = {
    "invoice_number": {"type": "string", "description": "The invoice number"},
    "total_amount": {"type": "number", "description": "The total amount"},
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


def _tool_use_response(tool_input, *, text=None):
    """A Converse response in which the model CALLED the tool."""
    content = [{"toolUse": {"name": EXTRACTION_TOOL_NAME, "input": tool_input}}]
    if text is not None:
        content.insert(0, {"text": text})
    return {
        "response": {
            "output": {"message": {"content": content}},
            "stopReason": "tool_use",
        },
        "metering": {"tokens": 100},
    }


def _text_response(payload):
    """A Converse response in which the model answered in PROSE instead."""
    return {
        "response": {
            "output": {"message": {"content": [{"text": json.dumps(payload)}]}},
            "stopReason": "end_turn",
        },
        "metering": {"tokens": 100},
    }


def _run(config, response):
    """Drive the real section path; return (written_result, invoke_kwargs, document)."""
    svc = ExtractionService(region="us-west-2", config=config)
    with (
        patch("idp_common.s3.get_text_content", return_value="Page 1 text"),
        patch("idp_common.image.prepare_image", return_value=b"img"),
        patch(
            "idp_common.image.prepare_bedrock_image_attachment",
            return_value={"image": "b64"},
        ),
        patch("idp_common.bedrock.invoke_model", return_value=response) as inv,
        patch("idp_common.s3.write_content") as write,
        patch("idp_common.utils.merge_metering_data", return_value={"tokens": 100}),
        patch("idp_common.metrics.put_metric"),
    ):
        doc = svc.process_document_section(_document(), "1")
    written = write.call_args[0][0] if write.call_args else None
    return written, inv.call_args.kwargs, doc


@pytest.mark.unit
class TestTheToolReachesBedrock:
    def test_the_toolconfig_and_choice_are_passed(self):
        _, kwargs, _ = _run(
            _config(_SIMPLE_PROPS),
            _tool_use_response({"invoice_number": "INV-1", "total_amount": 100.0}),
        )
        tc = kwargs["tool_config"]
        assert [t["toolSpec"]["name"] for t in tc["tools"]] == [EXTRACTION_TOOL_NAME]
        # toolChoice must NAME the tool. `{"any": {}}` would let the model pick a
        # different tool and `{"auto": {}}` would let it skip tools entirely —
        # both would make the arm measure something other than forcing.
        assert kwargs["tool_choice"] == {"tool": {"name": EXTRACTION_TOOL_NAME}}

    def test_the_tool_schema_carries_the_class_fields(self):
        from idp_common.extraction.instance_probe import INSTANCE_PROBE_FIELD

        config = _config(_SIMPLE_PROPS)
        config["extraction"]["multi_instance_detection"] = {"enabled": True}
        _, kwargs, _ = _run(
            config,
            _tool_use_response({"invoice_number": "INV-1", "total_amount": 100.0}),
        )
        schema = kwargs["tool_config"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
        # The class's own fields, plus the multi-instance detection probe (#753).
        # A forced tool whose input schema OMITS the probe would make the count
        # structurally impossible to return — which is precisely the regression
        # #753 warns about for forced tool use, so the probe must ride the
        # toolSpec, not just the prompt.
        assert set(schema["properties"]) == {
            "invoice_number",
            "total_amount",
            INSTANCE_PROBE_FIELD,
        }

    def test_the_probe_is_absent_from_the_tool_schema_by_default(self):
        """Detection is OFF by default (gated on the benchmark A/B), so the shipped
        toolSpec is byte-identical to earlier releases."""
        from idp_common.extraction.instance_probe import INSTANCE_PROBE_FIELD

        config = _config(_SIMPLE_PROPS)
        _, kwargs, _ = _run(
            config,
            _tool_use_response({"invoice_number": "INV-1", "total_amount": 100.0}),
        )
        schema = kwargs["tool_config"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
        assert set(schema["properties"]) == {"invoice_number", "total_amount"}
        assert INSTANCE_PROBE_FIELD not in schema["properties"]

    def test_a_probe_returned_by_the_tool_never_reaches_the_inference_result(self):
        """Off-schema for the CLASS, so it must be stripped before the
        off-schema filter, assessment, reporting and evaluation ever see it."""
        from idp_common.extraction.instance_probe import INSTANCE_PROBE_FIELD

        config = _config(_SIMPLE_PROPS)
        config["extraction"]["multi_instance_detection"] = {"enabled": True}
        written, _, _ = _run(
            config,
            _tool_use_response(
                {
                    "invoice_number": "INV-1",
                    "total_amount": 100.0,
                    INSTANCE_PROBE_FIELD: 3,
                }
            ),
        )
        assert INSTANCE_PROBE_FIELD not in written["inference_result"]
        assert written["metadata"]["instance_probe"] == 3
        assert written["metadata"]["instance_source"] == "suspected"
        assert written["metadata"]["instance_count"] == 3
        assert written["metadata"]["instance_extracted_count"] == 1


@pytest.mark.unit
class TestHonoredAndUnhonored:
    def test_tool_input_becomes_the_inference_result(self):
        """The values come from the tool input, NOT from parsed text. The text
        block here is deliberately unparseable, so a result that still contains
        the right fields can only have come from the tool."""
        written, _, doc = _run(
            _config(_SIMPLE_PROPS),
            _tool_use_response(
                {"invoice_number": "INV-1", "total_amount": 100.0},
                text="Sure! Here is the data you asked for:",
            ),
        )
        assert written["inference_result"] == {
            "invoice_number": "INV-1",
            "total_amount": 100.0,
        }
        assert written["metadata"]["parsing_succeeded"] is True
        assert written["metadata"]["forced_tool"]["honored"] is True
        assert not doc.errors

    def test_a_prose_answer_falls_back_and_records_that_it_did(self):
        """Forcing is a request, not a guarantee — a model can accept a toolConfig
        and answer in prose anyway. With fallback on, that must still succeed, and
        `honored: False` is what tells an A/B the arm didn't actually engage."""
        written, _, doc = _run(
            _config(_SIMPLE_PROPS),
            _text_response({"invoice_number": "INV-2", "total_amount": 5.0}),
        )
        assert written["inference_result"]["invoice_number"] == "INV-2"
        assert written["metadata"]["parsing_succeeded"] is True
        assert written["metadata"]["forced_tool"]["honored"] is False
        assert written["metadata"]["forced_tool"]["requested"] is True
        assert not doc.errors

    def test_fallback_disabled_turns_a_prose_answer_into_a_parse_failure(self):
        """The measurement-only mode. Data loss is the POINT here (it is how the
        honored rate is measured without fallback masking it), so the failure has
        to be explicit and attributable rather than an empty result."""
        written, _, _ = _run(
            _config(_SIMPLE_PROPS, fallback=False),
            _text_response({"invoice_number": "INV-3", "total_amount": 5.0}),
        )
        assert written["metadata"]["parsing_succeeded"] is False
        assert "forced tool use" in written["inference_result"]["error"].lower()
        assert written["metadata"]["forced_tool"]["honored"] is False

    def test_fallback_disabled_does_not_penalise_an_honored_call(self):
        """Guard-the-guard: turning fallback off must not break the happy path."""
        written, _, _ = _run(
            _config(_SIMPLE_PROPS, fallback=False),
            _tool_use_response({"invoice_number": "INV-4", "total_amount": 1.0}),
        )
        assert written["metadata"]["parsing_succeeded"] is True
        assert written["inference_result"]["invoice_number"] == "INV-4"


@pytest.mark.unit
class TestRenamedPropertiesRoundTrip:
    """A class whose field name Bedrock's toolSpec pattern rejects.

    ``^[a-zA-Z0-9_.-]{1,64}$`` is enforced on top-level property names, and four
    shipped presets have field names with spaces. The tool schema must be
    sanitized on the way out and the response restored on the way back — if
    either half is missing on the live path, extraction "succeeds" while
    returning fields under names nobody configured.
    """

    PROPS = {
        "Invoice Number": {"type": "string", "description": "with a space"},
        "Total (USD)": {"type": "number", "description": "with parens"},
    }

    def test_the_wire_schema_is_sanitized(self):
        _, kwargs, _ = _run(
            _config(self.PROPS),
            _tool_use_response({"Invoice_Number": "INV-9", "Total__USD_": 12.0}),
        )
        schema = kwargs["tool_config"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
        assert "Invoice Number" not in schema["properties"]
        import re

        for name in schema["properties"]:
            assert re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", name), name

    def test_the_result_uses_the_authored_names(self):
        sanitized = None
        # Ask the builder what it renamed to, so the fake response speaks the
        # wire names the model would actually have been given.
        from idp_common.extraction.forced_tool import build_extraction_tool_config

        tc, name_map = build_extraction_tool_config(
            {"type": "object", "properties": self.PROPS}
        )
        sanitized = list(
            tc["tools"][0]["toolSpec"]["inputSchema"]["json"]["properties"]
        )
        assert name_map.renamed, "nothing was renamed; this test would be vacuous"
        written, _, _ = _run(
            _config(self.PROPS),
            _tool_use_response({sanitized[0]: "INV-9", sanitized[1]: 12.0}),
        )
        assert written["inference_result"]["Invoice Number"] == "INV-9"
        assert written["inference_result"]["Total (USD)"] == 12.0
        assert written["metadata"]["forced_tool"]["renamed_properties"] == 2


@pytest.mark.unit
class TestSkippedRoutes:
    def test_a_lambda_hook_route_is_skipped_and_recorded(self):
        """A custom Lambda hook is not the Converse API and cannot carry a
        toolConfig. Sending one anyway would break extraction for every operator
        using a hook, so the route must be skipped — and RECORDED, or an A/B on a
        hook-based stack silently reports "forcing had no effect"."""
        written, kwargs, doc = _run(
            _config(_SIMPLE_PROPS, model="LambdaHook"),
            _text_response({"invoice_number": "INV-5", "total_amount": 2.0}),
        )
        assert kwargs["tool_config"] is None
        assert kwargs["tool_choice"] is None
        assert written["metadata"]["forced_tool"]["skipped"]
        assert written["inference_result"]["invoice_number"] == "INV-5"
        assert not doc.errors

    def test_a_class_with_no_properties_makes_no_request_at_all(self):
        """``should_force_tool`` refuses an empty schema, but the service never
        gets that far: a class with no attributes skips LLM extraction entirely.

        Asserted here so the division of labour is recorded — the helper's
        empty-schema branch is defense-in-depth for direct callers, not the live
        path — and so that a future change which starts extracting empty classes
        cannot quietly begin sending a forced tool with no properties in it.
        """
        svc = ExtractionService(region="us-west-2", config=_config({}))
        with (
            patch("idp_common.s3.get_text_content", return_value="Page 1 text"),
            patch("idp_common.image.prepare_image", return_value=b"img"),
            patch(
                "idp_common.image.prepare_bedrock_image_attachment",
                return_value={"image": "b64"},
            ),
            patch("idp_common.bedrock.invoke_model") as inv,
            patch("idp_common.s3.write_content"),
            patch("idp_common.utils.merge_metering_data", return_value={}),
            patch("idp_common.metrics.put_metric"),
        ):
            svc.process_document_section(_document(), "1")
        inv.assert_not_called()


@pytest.mark.unit
class TestOffByDefault:
    def test_the_flag_off_sends_no_toolconfig_and_records_nothing(self):
        """The shipped default. If this ever fails, forcing has become on-by-default
        for every existing deployment without a release note."""
        written, kwargs, _ = _run(
            _config(_SIMPLE_PROPS, enabled=False),
            _text_response({"invoice_number": "INV-6", "total_amount": 3.0}),
        )
        assert kwargs["tool_config"] is None
        assert kwargs["tool_choice"] is None
        # No `forced_tool` block at all: absent means "the feature was off", which
        # is different from "requested and skipped".
        assert "forced_tool" not in written["metadata"]
        assert written["inference_result"]["invoice_number"] == "INV-6"

    def test_the_system_default_is_off(self):
        """Read from the shipped system defaults, not from a test fixture."""
        from idp_common.config.models import IDPConfig

        cfg = IDPConfig()
        assert cfg.extraction.forced_tool.enabled is False
        assert cfg.extraction.forced_tool.fallback_to_prompt is True


@pytest.mark.unit
class TestTheWrapperKeyRegression:
    """The live failure: a 100-row list silently reduced to nothing.

    On a benchmark run against a real stack, Sonnet 5 answered a forced tool call
    with the ENTIRE extraction nested under one invented key::

        {"fields": {"Account_Number": ..., "Transactions": [ ...100 rows... ]}}

    ``fields`` is not a property the class declares, so the off-schema-key handling
    dropped it — and with it every extracted value. The section stored
    ``inference_result: {}``, recorded ``parsing_succeeded: true`` and
    ``forced_tool.honored: true``, raised no error, and completed. The benchmark
    scored it COMPLETED with completeness recall **0.0**, three runs out of three.

    The tool is named ``emit_extracted_fields`` and asked for "the fields", which
    plausibly cued the nesting. The description now forbids it and
    ``unwrap_tool_payload`` recovers it anyway, because a prompt instruction is a
    request rather than a guarantee.

    Ruled out on the way here, each by measurement rather than reasoning: the
    sanitize/restore round trip (100 rows survive it), ``$ref``/``$defs`` in the
    tool schema (Bedrock returned 100 rows for both the ref'd and dereferenced
    form, identical output tokens), and the live task/system prompt (100 rows with
    and without it). Only the wrapper reproduces the loss.
    """

    PROPS = {
        "Account Number": {"type": "string", "description": "acct"},
        "Transactions": {
            "type": "array",
            "description": "rows",
            "items": {
                "type": "object",
                "properties": {"Amount": {"type": "number"}},
            },
        },
    }

    def _payload(self, n=100):
        # Keyed by the SANITIZED names, which is what the model is given.
        return {
            "Account_Number": "000123456789",
            "Transactions": [{"Amount": float(i)} for i in range(n)],
        }

    def test_a_nested_payload_is_recovered_in_full(self):
        written, _, doc = _run(
            _config(self.PROPS),
            _tool_use_response({"fields": self._payload()}),
        )
        assert written["inference_result"]["Account Number"] == "000123456789"
        assert len(written["inference_result"]["Transactions"]) == 100
        assert not doc.errors

    @pytest.mark.parametrize("wrapper", ["fields", "data", "result", "output"])
    def test_the_other_plausible_wrappers_too(self, wrapper):
        written, _, _ = _run(
            _config(self.PROPS), _tool_use_response({wrapper: self._payload(5)})
        )
        assert len(written["inference_result"]["Transactions"]) == 5

    def test_an_unknown_wrapper_is_recovered_when_the_payload_matches_the_schema(self):
        """A key we did not anticipate still gets unwrapped, but only because the
        payload inside is unmistakably the extraction."""
        written, _, _ = _run(
            _config(self.PROPS),
            _tool_use_response({"zzz_unexpected": self._payload(3)}),
        )
        assert len(written["inference_result"]["Transactions"]) == 3

    def test_a_flat_response_is_untouched(self):
        """Guard-the-guard: unwrapping must not disturb a well-behaved reply."""
        written, _, _ = _run(_config(self.PROPS), _tool_use_response(self._payload(7)))
        assert len(written["inference_result"]["Transactions"]) == 7
        assert written["inference_result"]["Account Number"] == "000123456789"

    def test_a_single_field_class_is_not_unwrapped(self):
        """A class with exactly ONE declared property produces a single-key tool
        input legitimately. Unwrapping that would destroy the extraction — the
        precise mirror of the bug being fixed."""
        written, _, _ = _run(
            _config({"Account Number": {"type": "string"}}),
            _tool_use_response({"Account_Number": "12345"}),
        )
        assert written["inference_result"]["Account Number"] == "12345"

    def test_a_nested_object_field_is_not_mistaken_for_a_wrapper(self):
        """A single-key reply whose key IS declared and whose value is an object
        must survive — it is a group field, not a wrapper."""
        props = {
            "Address": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
            }
        }
        written, _, _ = _run(
            _config(props), _tool_use_response({"Address": {"city": "Anytown"}})
        )
        assert written["inference_result"]["Address"] == {"city": "Anytown"}

    def test_the_description_tells_the_model_not_to_nest(self):
        """Fix the cue, not just the symptom."""
        from idp_common.extraction.forced_tool import build_extraction_tool_config

        tc, _ = build_extraction_tool_config(
            {"type": "object", "properties": self.PROPS}
        )
        desc = tc["tools"][0]["toolSpec"]["description"].lower()
        assert "top-level" in desc
        assert "wrapper" in desc and "fields" in desc
