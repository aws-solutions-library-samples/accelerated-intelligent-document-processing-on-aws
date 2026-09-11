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
