# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Dropping the prose schema as the extraction SERVICE actually runs it (#710).

``test_prose_schema.py`` covers the renderers and the claim that the tool schema
makes the prose redundant. None of that touches
``ExtractionService.process_document_section``, which is where this feature is
either safe or a data-loss bug, because the decision is an ORDERING problem:

The prompt is built in ``_initialize_extraction_context``; whether the forced
toolSpec goes on the wire used to be decided later, in ``_invoke_extraction_model``.
Forcing is skipped automatically for routes that cannot carry a ``toolConfig`` (a
custom Lambda hook, GPT-5.x). If the prose is dropped on such a request, the model
receives **no schema at all** and the section degrades silently — schema-valid-looking
output, no error, no warning. The two decisions must therefore come from ONE boolean
computed before the prompt exists, which is what these tests pin:

* :class:`TestTheProseIsDroppedOnlyWhenTheToolIsSent` — the correctness case, in all
  three of its shapes (forcing on, forcing off, route that cannot force).
* :class:`TestWhatReachesTheModel` — that the prompt really did change, asserted on
  the content passed to ``bedrock.invoke_model`` rather than on a helper's return.
* :class:`TestTheMultiInstanceProbeSurvives` — #753's probe is injected into the wire
  schema and therefore into the prose today. A drop is only safe because it also
  rides the toolSpec; a probe that reached NEITHER copy would make the count
  structurally impossible while everything still reported success.
* :class:`TestMetadata` — an A/B in which "no effect" and "never applied" look
  identical is unreadable, which is the same reason ``skip_reason`` is recorded.
* :class:`TestAdvancedPath` — the agentic knob is a different knob on a different
  path, and must not be reachable from the simple one.
* :class:`TestOffByDefault` — the shipped default, byte for byte.
"""

from __future__ import annotations

import json
from textwrap import dedent
from unittest.mock import patch

import pytest

from idp_common.extraction.forced_tool import EXTRACTION_TOOL_NAME
from idp_common.extraction.service import ExtractionService
from idp_common.models import Document, Page, Section, Status

pytestmark = pytest.mark.unit

# A Converse-shaped model that CAN carry a toolConfig.
_MODEL = "us.anthropic.claude-sonnet-4-6"

_PROPS = {
    "invoice_number": {"type": "string", "description": "THE-INVOICE-NUMBER-DESC"},
    "supplier": {
        "type": "object",
        "description": "THE-SUPPLIER-GROUP-DESC",
        "properties": {"name": {"type": "string", "description": "THE-NAME-DESC"}},
    },
}
_CLASS_DESC = "THE-CLASS-DESC"


def _config(
    *,
    forcing=True,
    drop=False,
    model=_MODEL,
    agentic=False,
    prose_schema=None,
    properties=None,
):
    extraction = {
        "model": model,
        "temperature": 0.0,
        "top_k": 5,
        "system_prompt": "You are a document extraction assistant.",
        "task_prompt": dedent("""
            Extract from this {DOCUMENT_CLASS} document:
            <attributes>
            {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
            </attributes>
            {DOCUMENT_TEXT}
        """),
        "forced_tool": {
            "enabled": forcing,
            "fallback_to_prompt": True,
            "drop_prose_schema": drop,
        },
        # Keep the assertions about the prompt, not about downstream repair.
        "coercion": {"enabled": False},
        "validation": {"enabled": False},
    }
    if agentic or prose_schema is not None:
        extraction["agentic"] = {"enabled": agentic}
        if prose_schema is not None:
            extraction["agentic"]["prose_schema"] = prose_schema
    return {
        "classes": [
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "invoice",
                "x-aws-idp-document-type": "invoice",
                "type": "object",
                "description": _CLASS_DESC,
                "properties": properties if properties is not None else _PROPS,
            }
        ],
        "extraction": extraction,
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


def _tool_use_response(tool_input):
    return {
        "response": {
            "output": {
                "message": {
                    "content": [
                        {"toolUse": {"name": EXTRACTION_TOOL_NAME, "input": tool_input}}
                    ]
                }
            },
            "stopReason": "tool_use",
        },
        "metering": {"tokens": 100},
    }


def _text_response(payload):
    return {
        "response": {
            "output": {"message": {"content": [{"text": json.dumps(payload)}]}},
            "stopReason": "end_turn",
        },
        "metering": {"tokens": 100},
    }


_OK = {"invoice_number": "INV-1", "supplier": {"name": "Acme"}}


def _run(config, response):
    """Drive the real section path; return (written_result, invoke_kwargs)."""
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
        svc.process_document_section(_document(), "1")
    written = write.call_args[0][0] if write.call_args else None
    return written, inv.call_args.kwargs


def _prompt_text(kwargs):
    return "\n".join(
        part.get("text", "") for part in kwargs["content"] if isinstance(part, dict)
    )


class TestTheProseIsDroppedOnlyWhenTheToolIsSent:
    """The correctness case. Each of these three would be a silent data-loss bug if
    the prose decision were made independently of the forcing decision."""

    def test_dropped_when_forcing_is_on(self):
        _, kwargs = _run(_config(drop=True), _tool_use_response(_OK))
        prompt = _prompt_text(kwargs)
        assert kwargs["tool_config"] is not None
        assert "THE-INVOICE-NUMBER-DESC" not in prompt
        assert "invoice_number" in prompt  # the names survive

    def test_kept_when_forcing_is_off(self):
        """`drop_prose_schema: true` with `enabled: false` must be inert. Otherwise
        one careless config removes the model's only description of the fields."""
        _, kwargs = _run(_config(forcing=False, drop=True), _text_response(_OK))
        assert kwargs["tool_config"] is None
        assert "THE-INVOICE-NUMBER-DESC" in _prompt_text(kwargs)

    def test_kept_when_the_route_cannot_carry_a_toolconfig(self):
        """A custom Lambda hook is not the Converse API. Forcing is skipped for it
        automatically — so the prose must be kept, or every hook-based deployment
        that set this flag starts extracting from a schema-less prompt."""
        written, kwargs = _run(
            _config(model="LambdaHook", drop=True), _text_response(_OK)
        )
        assert kwargs["tool_config"] is None
        assert "THE-INVOICE-NUMBER-DESC" in _prompt_text(kwargs)
        assert written["metadata"]["prose_schema"]["mode"] == "full"
        assert written["metadata"]["prose_schema"]["requested"] == "names"
        assert "Lambda" in written["metadata"]["prose_schema"]["kept_reason"]

    def test_kept_for_a_gpt5_responses_route(self):
        """The other route that cannot carry a toolConfig, for the same reason."""
        _, kwargs = _run(
            _config(model="openai.gpt-5-2025-08-07-v1:0", drop=True),
            _text_response(_OK),
        )
        assert kwargs["tool_config"] is None
        assert "THE-INVOICE-NUMBER-DESC" in _prompt_text(kwargs)


class TestWhatReachesTheModel:
    def test_the_tool_schema_still_carries_every_description(self):
        """The prose is only safe to drop because the toolSpec is lossless on this
        path. Asserted on the same request that dropped it, so the two cannot drift."""
        _, kwargs = _run(_config(drop=True), _tool_use_response(_OK))
        rendered = json.dumps(kwargs["tool_config"])
        for text in ("THE-CLASS-DESC", "THE-INVOICE-NUMBER-DESC", "THE-NAME-DESC"):
            assert text in rendered

    def test_the_attributes_element_is_not_left_empty(self):
        """An empty substitution would leave `<attributes></attributes>` and make the
        surrounding prompt sentence false, so `names` renders a real list."""
        _, kwargs = _run(_config(drop=True), _tool_use_response(_OK))
        prompt = _prompt_text(kwargs)
        body = prompt.split("<attributes>")[1].split("</attributes>")[0]
        assert body.strip()
        assert "invoice_number" in body

    def test_extraction_still_succeeds(self):
        written, _ = _run(_config(drop=True), _tool_use_response(_OK))
        assert written["inference_result"] == _OK
        assert written["metadata"]["parsing_succeeded"] is True

    def test_a_prose_answer_still_falls_back(self):
        """Dropping the prose must not disturb the fallback path: a model can accept
        a toolConfig and answer in text anyway."""
        written, _ = _run(_config(drop=True), _text_response(_OK))
        assert written["inference_result"]["invoice_number"] == "INV-1"
        assert written["metadata"]["forced_tool"]["honored"] is False


class TestTheMultiInstanceProbeSurvives:
    """#753's probe is injected into the WIRE schema, so it is in the prose today.

    Dropping the prose is only safe because the probe rides the toolSpec too. If it
    reached neither copy the count would be structurally impossible to return, and
    nothing would report that: the section would complete, `instance_count` would be
    1, and the silent-loss detection #753 exists for would be quietly disabled.
    """

    def _cfg(self):
        cfg = _config(drop=True)
        cfg["extraction"]["multi_instance_detection"] = {"enabled": True}
        return cfg

    def test_the_probe_is_in_the_tool_schema(self):
        from idp_common.extraction.instance_probe import INSTANCE_PROBE_FIELD

        _, kwargs = _run(self._cfg(), _tool_use_response(_OK))
        schema = kwargs["tool_config"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
        assert INSTANCE_PROBE_FIELD in schema["properties"]

    def test_the_probe_question_is_in_the_tool_schema(self):
        """The names-only prose carries the probe's NAME but not its question, and
        two clauses of that question are load-bearing (see base-extraction.yaml). It
        has to arrive somewhere."""
        from idp_common.extraction.instance_probe import INSTANCE_PROBE_FIELD

        _, kwargs = _run(self._cfg(), _tool_use_response(_OK))
        schema = kwargs["tool_config"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
        question = schema["properties"][INSTANCE_PROBE_FIELD].get("description", "")
        assert "do not count pages" in question.lower()

    def test_the_count_still_comes_back_and_is_reported(self):
        from idp_common.extraction.instance_probe import INSTANCE_PROBE_FIELD

        written, _ = _run(
            self._cfg(), _tool_use_response({**_OK, INSTANCE_PROBE_FIELD: 3})
        )
        assert written["metadata"]["instance_probe"] == 3
        assert INSTANCE_PROBE_FIELD not in written["inference_result"]


class TestMetadata:
    def test_the_applied_mode_is_recorded_on_the_forced_tool_block(self):
        """`forced_tool` is where a forcing A/B looks, so "tool only" and
        "tool + prose" must be distinguishable there without a second lookup."""
        written, _ = _run(_config(drop=True), _tool_use_response(_OK))
        assert written["metadata"]["forced_tool"]["prose_schema"] == "names"
        assert written["metadata"]["prose_schema"]["mode"] == "names"

    def test_the_default_records_nothing(self):
        """Absent means "the shipped rendering was used". Emitting a block for every
        section would change every existing deployment's stored metadata."""
        written, _ = _run(_config(), _tool_use_response(_OK))
        assert "prose_schema" not in written["metadata"]
        assert written["metadata"]["forced_tool"]["prose_schema"] == "full"

    def test_a_kept_prose_records_why(self):
        """Without this, an A/B arm that never applied reads exactly like one that
        applied and changed nothing."""
        written, _ = _run(_config(forcing=False, drop=True), _text_response(_OK))
        block = written["metadata"]["prose_schema"]
        assert block["requested"] == "names"
        assert block["mode"] == "full"
        assert "forced_tool.enabled is off" in block["kept_reason"]


class TestAdvancedPath:
    def test_the_agentic_knob_does_not_affect_the_simple_path(self):
        """`agentic.prose_schema` is an advanced-mode knob. If it leaked into simple
        mode it would drop the prose with no toolSpec necessarily present."""
        cfg = _config(forcing=False, prose_schema="names")
        _, kwargs = _run(cfg, _text_response(_OK))
        assert "THE-INVOICE-NUMBER-DESC" in _prompt_text(kwargs)

    def test_the_simple_knob_does_not_affect_the_advanced_path(self):
        """Advanced extraction ignores `forced_tool` entirely, so its prose must be
        governed by `agentic.prose_schema` alone."""
        svc = ExtractionService(
            region="us-west-2", config=_config(agentic=True, drop=True, forcing=True)
        )
        svc._initialize_extraction_context("invoice", "text", [], ["1"], _document())
        assert "THE-INVOICE-NUMBER-DESC" in svc._attribute_descriptions
        assert svc._prose_schema_mode == "full"

    @pytest.mark.parametrize(
        "mode,present,absent",
        [
            ("full", ["THE-INVOICE-NUMBER-DESC", "THE-CLASS-DESC"], []),
            (
                "minimal",
                ["THE-CLASS-DESC", "THE-SUPPLIER-GROUP-DESC"],
                ["THE-INVOICE-NUMBER-DESC"],
            ),
            ("names", ["invoice_number"], ["THE-CLASS-DESC", "THE-NAME-DESC"]),
        ],
    )
    def test_each_agentic_mode_renders_what_it_promises(self, mode, present, absent):
        svc = ExtractionService(
            region="us-west-2", config=_config(agentic=True, prose_schema=mode)
        )
        svc._initialize_extraction_context("invoice", "text", [], ["1"], _document())
        rendered = svc._attribute_descriptions
        for text in present:
            assert text in rendered, f"{mode}: missing {text}"
        for text in absent:
            assert text not in rendered, f"{mode}: unexpectedly kept {text}"

    def test_the_advanced_path_never_reports_a_forced_tool_decision(self):
        """`forced_tool` is simple-mode only. A True here would send a toolConfig on
        a path that already sends one through Strands."""
        svc = ExtractionService(
            region="us-west-2", config=_config(agentic=True, forcing=True)
        )
        svc._initialize_extraction_context("invoice", "text", [], ["1"], _document())
        assert svc._forced_tool_decision == (False, None)


class TestEscalationKeepsTheProse:
    def test_the_subset_schema_is_spelled_out_for_the_escalation_agent(self):
        """Escalation reuses the ORIGINAL prompt with a SUBSET data model, so with
        the prose dropped the escalating agent would see only field names. It is the
        recovery path for an already-failed result — the wrong place to economize."""
        from idp_common.extraction.validation import ValidationReport

        svc = ExtractionService(
            region="us-west-2",
            config=_config(agentic=True, prose_schema="names"),
        )
        svc._initialize_extraction_context("invoice", "text", [], ["1"], _document())
        report = ValidationReport(
            valid=False,
            errors=[{"path": "invoice_number", "message": "wrong type"}],
        )
        captured = {}

        def fake_structured_output(**kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop here; only the instruction matters")

        with patch(
            "idp_common.extraction.service.structured_output", fake_structured_output
        ):
            svc._escalate_failing_fields(
                extracted_fields={"invoice_number": 1},
                structured_data=None,
                data_model=None,
                full_report=report,
                escalation_model=_MODEL,
                message_prompt={"role": "user", "content": [{"text": "x"}]},
                agentic_images=[],
                custom_instruction=None,
                section_info=type("S", (), {"class_label": "invoice"})(),
            )
        assert "THE-INVOICE-NUMBER-DESC" in captured["custom_instruction"]


class TestOffByDefault:
    def test_the_shipped_default_prompt_is_unchanged(self):
        """The whole schema, exactly as every release before #710 sent it. A failure
        here means an upgrade silently rewrote every deployment's prompt — and
        invalidated its prompt-cache prefix."""
        _, kwargs = _run(_config(forcing=False), _text_response(_OK))
        prompt = _prompt_text(kwargs)
        for text in ("THE-CLASS-DESC", "THE-INVOICE-NUMBER-DESC", "THE-NAME-DESC"):
            assert text in prompt
        assert '"properties"' in prompt  # still the raw JSON Schema

    def test_forcing_on_alone_does_not_change_the_prompt(self):
        """The WS-05 A/B's premise: forcing measured on an UNCHANGED prompt. If
        enabling forcing started dropping the prose, that measurement would no
        longer describe the shipped behaviour."""
        _, with_force = _run(_config(forcing=True), _tool_use_response(_OK))
        _, without = _run(_config(forcing=False), _text_response(_OK))
        assert _prompt_text(with_force) == _prompt_text(without)
