"""The output budget a confidence call requests (``bedrock.sizing.confidence_output_budget``)
and its wiring into ``AssessmentService.assess_results``.

Why: Nova Lite at temperature 0 looped the same row object 189 times on a 25-row batch
until it hit its 10,000-token cap (60 s, 10,000 output tokens, every time). The cap was
the only thing stopping it, so the batcher paid the full cap before recovering. Asking
for what a correct answer needs bounds that loss; the truncation recovery is unchanged.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from idp_common.bedrock.sizing import (
    _CONFIDENCE_OUTPUT_OVERHEAD_TOKENS,
    _MIN_CONFIDENCE_OUTPUT_BUDGET,
    confidence_output_budget,
    confidence_per_row_tokens,
)

CELL = confidence_per_row_tokens(1, "ocr_only")  # 40


def _rows(n: int, cols: int = 3) -> list[dict]:
    return [{f"c{c}": "v" for c in range(cols)} for _ in range(n)]


@pytest.mark.unit
def test_budget_is_leaves_times_per_cell_plus_overhead():
    er = {"Account": "1", "Period": "2", "Transactions": _rows(25)}
    expect = int((2 + 25 * 3) * CELL) + _CONFIDENCE_OUTPUT_OVERHEAD_TOKENS  # 4,580
    assert confidence_output_budget(er, "ocr_only", 10_000) == expect
    assert expect < 10_000  # a 25-row Nova Lite loop now costs 4.6k tokens, not 10k


@pytest.mark.unit
def test_budget_tracks_the_batch_size():
    small = confidence_output_budget({"Transactions": _rows(8)}, "ocr_only", 10_000)
    mid = confidence_output_budget({"Transactions": _rows(12)}, "ocr_only", 10_000)
    assert _MIN_CONFIDENCE_OUTPUT_BUDGET <= small < mid


@pytest.mark.unit
def test_budget_is_floored_and_capped():
    assert (
        confidence_output_budget({"a": "x"}, "ocr_only", 10_000)
        == _MIN_CONFIDENCE_OUTPUT_BUDGET
    )
    assert (
        confidence_output_budget({"Transactions": _rows(500)}, "ocr_only", 10_000)
        == 10_000
    )
    # unknown cap: floor and arithmetic only
    assert (
        confidence_output_budget({"Transactions": _rows(500)}, "ocr_only", None)
        > 10_000
    )


@pytest.mark.unit
def test_bbox_geometry_triples_the_per_cell_allowance():
    er = {"Transactions": _rows(12)}
    assert confidence_output_budget(
        er, "llm_grounded", 10_000
    ) > confidence_output_budget(er, "ocr_only", 10_000)


@pytest.mark.unit
def test_nested_rows_and_scalar_lists_are_counted():
    nested = {
        "instances": [{"Payee": "p", "Legs": _rows(5, 2)}] * 4
    }  # 4 + 4*5*2 = 44 leaves
    flat = {"tags": ["a", "b", "c"]}  # 3 leaves
    assert (
        confidence_output_budget(nested, "ocr_only", 100_000)
        == int(44 * CELL) + _CONFIDENCE_OUTPUT_OVERHEAD_TOKENS
    )
    assert (
        confidence_output_budget(flat, "ocr_only", 100_000)
        == _MIN_CONFIDENCE_OUTPUT_BUDGET
    )


@pytest.mark.unit
def test_assess_results_requests_the_budget_not_the_full_cap():
    """The wiring: ``bedrock.invoke_model`` receives ``max_tokens`` = the budget for
    THIS call's rows, and an unknown model falls back to None (the client then asks
    for the model default)."""
    from idp_common.assessment.service import AssessmentService
    from idp_common.config.merge_utils import merge_config_with_defaults
    from idp_common.config.models import IDPConfig

    cfg = IDPConfig(
        **merge_config_with_defaults(
            {
                "classes": [
                    {
                        "$id": "Stmt",
                        "x-aws-idp-document-type": "Stmt",
                        "type": "object",
                        "properties": {
                            "Account": {"type": "string"},
                            "Transactions": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "c0": {"type": "string"},
                                        "c1": {"type": "string"},
                                        "c2": {"type": "string"},
                                    },
                                },
                            },
                        },
                    }
                ]
            }
        )
    )
    svc = AssessmentService(region="us-west-2", config=cfg)
    er = {"Account": "1", "Transactions": _rows(12)}
    fake = {
        "response": {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"text": "{}"}]}},
        },
        "metering": {},
    }
    with (
        patch(
            "idp_common.assessment.service.bedrock.invoke_model", return_value=fake
        ) as inv,
        patch(
            "idp_common.assessment.service.bedrock.extract_text_from_response",
            return_value="{}",
        ),
    ):
        svc.assess_results(
            class_label="Stmt", extraction_results=er, document_text="t", page_images=[]
        )
    sent = inv.call_args.kwargs["max_tokens"]
    assert sent == confidence_output_budget(er, cfg.extraction.geometry.mode, 10_000)
    assert _MIN_CONFIDENCE_OUTPUT_BUDGET <= sent < 10_000

    with (
        patch(
            "idp_common.assessment.service.bedrock.invoke_model", return_value=fake
        ) as inv,
        patch(
            "idp_common.assessment.service.bedrock.extract_text_from_response",
            return_value="{}",
        ),
    ):
        svc.assess_results(
            class_label="Stmt",
            extraction_results=er,
            document_text="t",
            page_images=[],
            model_id_override="vendor.unknown-model-v9",
        )
    assert inv.call_args.kwargs["max_tokens"] is None
