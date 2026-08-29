# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Simple-mode coercion + full-schema validation (WS-09).

Simple extraction previously did a raw ``json.loads`` and passed whatever came
back downstream, so a wrong type or a non-ISO date reached DynamoDB
unchallenged. Agentic extraction has had full-schema validation and escalation
for some time; this brings the same guarantee to the path most deployments run.

The load-bearing property is **cost**: validation is on by default, so the
default ``fail_action: warn`` must add NO inference. Only the explicit
``escalate`` opt-in is allowed to spend money.
"""

from __future__ import annotations

from unittest.mock import patch

from idp_common.config.models import IDPConfig
from idp_common.extraction.service import ExtractionService, SectionInfo

SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": {"type": "string"},
        "amount": {"type": "number"},
        "due_date": {"type": "string", "format": "date"},
    },
    "required": ["invoice_number", "amount", "due_date"],
}


def _svc(**validation) -> ExtractionService:
    cfg = IDPConfig(
        **{
            "extraction": {
                "agentic": {"enabled": False},
                "validation": validation or {},
            }
        }
    )
    svc = ExtractionService(config=cfg)
    svc._class_schema = SCHEMA
    svc._class_label = "invoice"
    svc._pending_extraction_model = "us.anthropic.claude-sonnet-5"
    return svc


def _section() -> SectionInfo:
    return SectionInfo(
        class_label="invoice",
        sorted_page_ids=["1"],
        page_indices=[0],
        output_bucket="b",
        output_key="k",
        output_uri="s3://b/k",
        start_page=1,
        end_page=1,
    )


def _validate(svc, fields, **over):
    kwargs = dict(
        extracted_fields=fields,
        content=[{"text": "doc"}],
        system_prompt="sys",
        model_id="us.anthropic.claude-sonnet-5",
        metering={},
        section_info=_section(),
        parsing_succeeded=True,
    )
    kwargs.update(over)
    return svc._validate_simple_result(**kwargs)


# --------------------------------------------------------------------------
# Coercion
# --------------------------------------------------------------------------


def test_coercion_fixes_currency_and_dates_for_free():
    svc = _svc()
    fields, meta = svc._coerce_simple_result(
        {
            "invoice_number": "INV-1",
            "amount": "$1,234.00",
            "due_date": "03/15/2024",
        }
    )
    assert fields["amount"] == 1234.0
    assert fields["due_date"] == "2024-03-15"
    assert meta is not None, "coercions must be recorded, never silent"


def test_coercion_is_a_noop_when_values_are_already_correct():
    svc = _svc()
    clean = {"invoice_number": "INV-1", "amount": 10.0, "due_date": "2024-03-15"}
    fields, meta = svc._coerce_simple_result(clean)
    assert fields == clean
    assert meta is None, "a clean result must not produce audit noise"


def test_coercion_never_fails_extraction():
    """A broken repair must be strictly better than no repair."""
    svc = _svc()
    with patch(
        "idp_common.extraction.coercion.coerce_extraction",
        side_effect=RuntimeError("boom"),
    ):
        fields, meta = svc._coerce_simple_result({"amount": "x"})
    assert fields == {"amount": "x"}
    assert meta is None


# --------------------------------------------------------------------------
# Validation and fail_action — the cost contract
# --------------------------------------------------------------------------


def test_disabled_is_a_complete_noop():
    svc = _svc(enabled=False)
    fields, meta, ok = _validate(svc, {"invoice_number": "INV-1"})
    assert meta is None
    assert ok is True


def test_valid_result_records_a_clean_report():
    svc = _svc(enabled=True)
    fields, meta, ok = _validate(
        svc,
        {"invoice_number": "INV-1", "amount": 10.0, "due_date": "2024-03-15"},
    )
    assert meta is not None
    assert meta["valid"] is True
    assert meta["escalated"] is False
    assert meta["mode"] == "simple"
    assert ok is True


def test_warn_costs_no_inference():
    """The default must be free — validation is on by default because of this."""
    svc = _svc(enabled=True, fail_action="warn")
    with patch("idp_common.bedrock.invoke_model") as spy:
        fields, meta, ok = _validate(
            svc, {"invoice_number": "INV-1", "amount": 10.0, "due_date": "not-a-date"}
        )
        spy.assert_not_called()
    assert meta["valid"] is False
    assert meta["escalated"] is False
    assert meta["initial_failed_fields"] == ["due_date"]
    # warn never fails the section — the partial data is still useful.
    assert ok is True


def test_reject_marks_the_section_failed_without_inference():
    svc = _svc(enabled=True, fail_action="reject")
    with patch("idp_common.bedrock.invoke_model") as spy:
        fields, meta, ok = _validate(
            svc, {"invoice_number": "INV-1", "amount": 10.0, "due_date": "nope"}
        )
        spy.assert_not_called()
    assert ok is False
    assert meta["valid"] is False


def test_already_failed_parse_is_not_validated():
    svc = _svc(enabled=True)
    fields, meta, ok = _validate(
        svc, {"raw_output": "garbage"}, parsing_succeeded=False
    )
    assert meta is None
    assert ok is False


# --------------------------------------------------------------------------
# Escalation — the only path allowed to spend money
# --------------------------------------------------------------------------


def _escalation_response(payload: str):
    return {
        "response": {
            "output": {"message": {"content": [{"text": payload}]}},
            "stopReason": "end_turn",
        },
        "metering": {"esc/bedrock/model": {"inputTokens": 10, "outputTokens": 5}},
    }


def test_escalate_reextracts_only_the_failing_field_and_resolves():
    svc = _svc(enabled=True, fail_action="escalate", escalation_model="us.big-model")
    metering: dict = {}
    with patch(
        "idp_common.bedrock.invoke_model",
        return_value=_escalation_response('{"due_date": "2024-03-15"}'),
    ) as spy:
        fields, meta, ok = _validate(
            svc,
            {"invoice_number": "INV-1", "amount": 10.0, "due_date": "March 15th"},
            metering=metering,
        )
        spy.assert_called_once()
        assert spy.call_args.kwargs["model_id"] == "us.big-model"

    assert fields["due_date"] == "2024-03-15"
    # Fields that already validated are untouched.
    assert fields["invoice_number"] == "INV-1"
    assert fields["amount"] == 10.0
    assert meta["escalated"] is True
    assert meta["resolved_by_escalation"] is True
    assert meta["escalation_fields"] == ["due_date"]
    assert metering, "escalation cost must be metered"


def test_escalation_cannot_overwrite_fields_that_already_validated():
    """An over-eager escalation response must not clobber good data."""
    svc = _svc(enabled=True, fail_action="escalate", escalation_model="us.big-model")
    with patch(
        "idp_common.bedrock.invoke_model",
        return_value=_escalation_response(
            '{"due_date": "2024-03-15", "invoice_number": "WRONG", "amount": 999}'
        ),
    ):
        fields, meta, ok = _validate(
            svc,
            {"invoice_number": "INV-1", "amount": 10.0, "due_date": "March 15th"},
            metering={},
        )
    assert fields["invoice_number"] == "INV-1"
    assert fields["amount"] == 10.0
    assert fields["due_date"] == "2024-03-15"


def test_escalation_failure_keeps_the_original_extraction():
    svc = _svc(enabled=True, fail_action="escalate", escalation_model="us.big-model")
    original = {"invoice_number": "INV-1", "amount": 10.0, "due_date": "March 15th"}
    with patch(
        "idp_common.bedrock.invoke_model", side_effect=RuntimeError("throttled")
    ):
        fields, meta, ok = _validate(svc, dict(original), metering={})
    assert fields == original
    assert meta["escalated"] is True
    assert meta["resolved_by_escalation"] is False
    # A failed escalation must not fail the section under 'escalate'.
    assert ok is True


def test_escalation_ignores_an_unusable_response():
    svc = _svc(enabled=True, fail_action="escalate", escalation_model="us.big-model")
    original = {"invoice_number": "INV-1", "amount": 10.0, "due_date": "March 15th"}
    with patch(
        "idp_common.bedrock.invoke_model",
        return_value=_escalation_response("not json at all"),
    ):
        fields, meta, ok = _validate(svc, dict(original), metering={})
    assert fields == original


def test_escalation_coerces_its_own_output_before_revalidating():
    """The stronger model is not exempt from deterministic repair."""
    svc = _svc(enabled=True, fail_action="escalate", escalation_model="us.big-model")
    with patch(
        "idp_common.bedrock.invoke_model",
        return_value=_escalation_response('{"due_date": "03/15/2024"}'),
    ):
        fields, meta, ok = _validate(
            svc,
            {"invoice_number": "INV-1", "amount": 10.0, "due_date": "March 15th"},
            metering={},
        )
    assert fields["due_date"] == "2024-03-15"
    assert meta["resolved_by_escalation"] is True
