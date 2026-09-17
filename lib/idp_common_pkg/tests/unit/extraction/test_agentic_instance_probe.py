# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#772 (option 1): the multi-instance detection probe rides on the transport
model of the UNSHARDED agentic call and is popped before anything downstream
sees the fields."""

import pytest

from idp_common.config.models import IDPConfig
from idp_common.extraction.instance_probe import INSTANCE_PROBE_FIELD
from idp_common.extraction.service import ExtractionService

pytestmark = pytest.mark.unit

SCHEMA = {
    "$id": "Invoice",
    "type": "object",
    "properties": {
        "Total": {"type": "string", "description": "total"},
        "Vendor": {"type": "string", "description": "vendor"},
    },
    "required": ["Total"],
}


def _svc(detection=True, question=None):
    det = {"enabled": detection}
    if question:
        det["question"] = question
    cfg = IDPConfig(
        **{
            "extraction": {
                "agentic": {"enabled": True},
                "multi_instance_detection": det,
            },
            "classes": [SCHEMA],
        }
    )
    svc = ExtractionService(config=cfg)
    svc._reset_context()
    svc._class_schema = SCHEMA
    svc._class_label = "Invoice"
    return svc


def test_probe_model_carries_an_optional_integer_probe_when_enabled():
    svc = _svc()
    base = svc._transport_model(SCHEMA, "Invoice")
    model, added = svc._agentic_probe_model(base, "Invoice", resuming=False)
    assert added is True and model is not base
    assert svc._instance_probe_requested is True
    assert INSTANCE_PROBE_FIELD in model.model_fields
    # optional: a reply without the count still validates
    assert model(Total="1.00").model_dump(mode="json")[INSTANCE_PROBE_FIELD] is None
    # the declared fields are untouched
    assert set(base.model_fields) == set(model.model_fields) - {INSTANCE_PROBE_FIELD}


def test_probe_answer_is_popped_from_the_dumped_fields_and_read_as_the_count():
    svc = _svc()
    model, _ = svc._agentic_probe_model(
        svc._transport_model(SCHEMA, "Invoice"), "Invoice", resuming=False
    )
    fields = model(**{"Total": "1.00", INSTANCE_PROBE_FIELD: 3}).model_dump(mode="json")
    assert svc._read_instance_probe(fields) == 3
    assert INSTANCE_PROBE_FIELD not in fields
    assert fields == {"Total": "1.00", "Vendor": None}


def test_disabled_and_resume_keep_the_plain_model():
    svc = _svc(detection=False)
    base = svc._transport_model(SCHEMA, "Invoice")
    assert svc._agentic_probe_model(base, "Invoice", resuming=False) == (base, False)
    assert svc._instance_probe_requested is False
    svc = _svc(detection=True)
    assert svc._agentic_probe_model(base, "Invoice", resuming=True) == (base, False)


def test_the_configured_question_is_the_probe_description():
    svc = _svc(question="Count the {DOCUMENT_CLASS}s.")
    model, _ = svc._agentic_probe_model(
        svc._transport_model(SCHEMA, "Invoice"), "Invoice", resuming=False
    )
    assert model.model_fields[INSTANCE_PROBE_FIELD].description == "Count the Invoices."


def test_simple_path_wire_schema_helper_still_defers_to_the_agentic_helper():
    """_build_wire_schema is the Simple path's hook; under agentic it must keep
    returning the schema unchanged (the probe goes onto the model instead)."""
    svc = _svc()
    wire, added = svc._build_wire_schema(SCHEMA, "Invoice")
    assert added is False and wire is SCHEMA


STRICT_SCHEMA = {**SCHEMA, "additionalProperties": False}


def test_in_loop_validator_ignores_the_probe_even_on_a_strict_schema():
    """The agent's tool payload reaches the in-loop validator BEFORE the pop. On a
    class with additionalProperties: false the probe must not read as a violation,
    or the agent spends its retries removing it and detection is silently off."""
    cfg = IDPConfig(
        **{
            "extraction": {
                "agentic": {"enabled": True},
                "multi_instance_detection": {"enabled": True},
                "validation": {"enabled": True},
            },
            "classes": [STRICT_SCHEMA],
        }
    )
    svc = ExtractionService(config=cfg)
    svc._reset_context()
    svc._class_schema = STRICT_SCHEMA
    validate = svc._build_schema_validator()
    assert validate is not None
    ok, feedback = validate({"Total": "1.00", "Vendor": None, INSTANCE_PROBE_FIELD: 3})
    assert ok is True, feedback
    # a genuinely off-schema key is still caught
    ok, feedback = validate({"Total": "1.00", "Bogus": 1})
    assert ok is False and "Bogus" in feedback


def test_unsharded_agentic_call_carries_the_probe_and_pops_it_before_downstream(
    monkeypatch,
):
    """Wiring test through _invoke_extraction_model: the single-agent call is made
    with the probe model, the answer becomes result.instance_probe, and the key is
    gone from the extracted fields and from the inline field assessment."""
    from idp_common.extraction import service as service_mod
    from idp_common.extraction.service import SectionInfo
    from idp_common.models import Document, Status

    svc = _svc()
    svc._document = Document(
        id="d",
        input_key="d.pdf",
        input_bucket="in",
        output_bucket="out",
        status=Status.EXTRACTING,
    )
    svc._document_text = "Invoice total 1.00"
    svc._page_texts = ["Invoice total 1.00"]  # one page -> never sharded
    svc._page_images = []
    seen = {}

    def fake_structured_output(**kwargs):
        model = kwargs["data_format"]
        seen["fields"] = set(model.model_fields)
        seen["validator_ok"] = (
            kwargs["schema_validator"](
                {"Total": "1.00", "Vendor": None, INSTANCE_PROBE_FIELD: 3}
            )
            if kwargs.get("schema_validator")
            else None
        )
        return (
            model(**{"Total": "1.00", INSTANCE_PROBE_FIELD: 3}),
            {
                "metering": {
                    "Extraction/bedrock/m": {"inputTokens": 10, "outputTokens": 5},
                    "_integrated_field_assessment": {
                        "Total": {"confidence": 0.9},
                        INSTANCE_PROBE_FIELD: {"confidence": 0.4},
                    },
                }
            },
        )

    monkeypatch.setattr(service_mod, "structured_output", fake_structured_output)
    info = SectionInfo(
        class_label="Invoice",
        sorted_page_ids=["1"],
        page_indices=[0],
        output_bucket="out",
        output_key="k",
        output_uri="s3://out/k",
        start_page=1,
        end_page=1,
    )
    result = svc._invoke_extraction_model(
        content=[{"text": "extract"}], system_prompt="s", section_info=info
    )

    assert INSTANCE_PROBE_FIELD in seen["fields"], "the probe model was sent"
    # the in-loop validator saw the probe in the payload and did not object
    assert seen["validator_ok"] is None or seen["validator_ok"][0] is True, seen[
        "validator_ok"
    ]
    assert result.instance_probe == 3
    assert INSTANCE_PROBE_FIELD not in result.extracted_fields
    for key in ("_integrated_field_assessment", "_merged_assessment"):
        block = result.metering.get(key)
        if isinstance(block, dict):
            assert INSTANCE_PROBE_FIELD not in block
