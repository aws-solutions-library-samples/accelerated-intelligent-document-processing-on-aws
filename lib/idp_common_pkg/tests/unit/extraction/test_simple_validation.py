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


# --------------------------------------------------------------------------
# Coercion must be disableable
#
# Coercion REWRITES extracted document values. On by default because the
# alternative is a wrongly-typed value reaching storage — but a feature that
# rewrites data and cannot be turned off is not an acceptable default.
# --------------------------------------------------------------------------


def _svc_coercion(**coercion) -> ExtractionService:
    cfg = IDPConfig(
        **{"extraction": {"agentic": {"enabled": False}, "coercion": coercion}}
    )
    svc = ExtractionService(config=cfg)
    svc._class_schema = SCHEMA
    svc._class_label = "invoice"
    return svc


def test_coercion_on_by_default():
    svc = _svc_coercion()
    assert svc.config.extraction.coercion.enabled is True
    assert svc.config.extraction.coercion.date_order == "auto"
    fields, meta = svc._coerce_simple_result({"amount": "$1,234.00"})
    assert fields["amount"] == 1234.0


def test_coercion_can_be_turned_off_entirely():
    svc = _svc_coercion(enabled=False)
    original = {"amount": "$1,234.00", "due_date": "03/15/2024"}
    fields, meta = svc._coerce_simple_result(dict(original))
    assert fields == original, "disabled coercion must not rewrite anything"
    assert meta is None


def test_disabled_coercion_still_lets_validation_report_the_mismatch():
    """Turning off the repair must not also turn off the reporting."""
    svc = _svc_coercion(enabled=False)
    svc._pending_extraction_model = "us.anthropic.claude-sonnet-5"
    fields, _ = svc._coerce_simple_result(
        {"invoice_number": "INV-1", "amount": "$1,234.00", "due_date": "2024-03-15"}
    )
    cfg = IDPConfig(
        **{
            "extraction": {
                "agentic": {"enabled": False},
                "coercion": {"enabled": False},
                "validation": {"enabled": True, "fail_action": "warn"},
            }
        }
    )
    svc2 = ExtractionService(config=cfg)
    svc2._class_schema = SCHEMA
    svc2._class_label = "invoice"
    _f, meta, _ok = svc2._validate_simple_result(
        extracted_fields=fields,
        content=[{"text": "doc"}],
        system_prompt="sys",
        model_id="us.anthropic.claude-sonnet-5",
        metering={},
        section_info=_section(),
        parsing_succeeded=True,
    )
    assert meta["valid"] is False
    assert "amount" in meta["initial_failed_fields"]


def test_date_order_is_honoured_and_validated():
    # 'auto' refuses an ambiguous date...
    auto = _svc_coercion()
    fields, _ = auto._coerce_simple_result({"due_date": "01/02/2024"})
    assert fields["due_date"] == "01/02/2024"

    # ...and an explicit convention resolves it.
    dmy = _svc_coercion(date_order="DMY")
    fields, _ = dmy._coerce_simple_result({"due_date": "01/02/2024"})
    assert fields["due_date"] == "2024-02-01"

    mdy = _svc_coercion(date_order="MDY")
    fields, _ = mdy._coerce_simple_result({"due_date": "01/02/2024"})
    assert fields["due_date"] == "2024-01-02"


def test_bad_date_order_is_rejected_at_config_time():
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError, match="date_order"):
        IDPConfig(**{"extraction": {"coercion": {"date_order": "YMD-ish"}}})
