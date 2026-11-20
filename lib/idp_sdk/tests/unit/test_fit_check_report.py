# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The fit check's detectors and report builder, from recorded results.

No network: every input is a run record (the shape ``collect.py`` writes), built
here or read from ``fixtures/fit_check/``. Each detector is shown firing on the
condition it reports and staying silent just short of it, because a detector that
fires on everything passes a test that only checks the positive case.
"""

import copy
import json
from pathlib import Path

import jsonschema
import pytest

from idp_sdk._core.fit_check import detectors, refs
from idp_sdk._core.fit_check.report import (
    CONFIDENCE_IS_NOT_ACCURACY,
    _percentile,
    build_fields,
    build_report,
    contract_version,
    render_markdown,
    validate_report,
)
from idp_sdk._core.fit_check.scripts import count_scripts, observed_non_latin

FIXTURES = Path(__file__).parent / "fixtures" / "fit_check"

SIMPLE = {
    "use_bda": False,
    "ocr_backend": "textract",
    "ocr_features": ["TABLES", "LAYOUT", "SIGNATURES"],
    "extraction_mode": "simple",
    "classification_granularity": "page",
    "confidence_threshold": None,
    "classes": {"Statement": ["account", "transactions"]},
}


def _table(rows: int) -> str:
    head = "| Date | Amount |\n|---|---|\n"
    return head + "".join(
        f"| 2026-01-{i % 28 + 1:02d} | {i}.00 |\n" for i in range(rows)
    )


def _doc(**overrides):
    doc = {
        "file": "statement.pdf",
        "format": "pdf",
        "pages": 2,
        "status": "COMPLETED",
        "error": None,
        "errors": [],
        "duration_seconds": 40.0,
        "metering": {},
        "issues": [],
        "notes": [],
        "text_layer_scripts": {},
        "page_text": {"1": "Account 123", "2": ""},
        "sections": [
            {
                "section_id": "1",
                "class": "Statement",
                "page_ids": ["1", "2"],
                "issues": [],
                "inference_result": {"account": "123", "transactions": []},
                "explainability_info": {},
            }
        ],
        "evaluation": None,
    }
    doc.update(overrides)
    return doc


def _refs(findings):
    return {f.ref for f in findings if f.ref}


# --------------------------------------------------------------- table rows


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rows", "fires"),
    [
        (detectors.SIMPLE_MODE_MEASURED_COMPLETE_ROWS, False),
        (detectors.SIMPLE_MODE_MEASURED_COMPLETE_ROWS + 1, True),
    ],
)
def test_the_row_limit_fires_only_above_the_measured_complete_size(rows, fires):
    doc = _doc(page_text={"1": _table(rows), "2": ""})
    findings = detectors.detect_table_rows(doc, SIMPLE)
    assert (refs.SIMPLE_ROW_LIMIT in _refs(findings)) is fires
    assert any(f"a table of {rows} row(s)" in f.warning for f in findings)


@pytest.mark.unit
def test_a_table_split_by_a_page_break_is_counted_once():
    text = _table(300)
    doc = _doc(page_text={"1": text, "2": text})  # each page repeats the header
    warnings = [f.warning for f in detectors.detect_table_rows(doc, SIMPLE)]
    assert any("a table of 600 row(s)" in w for w in warnings), warnings


@pytest.mark.unit
def test_the_row_limit_is_a_simple_mode_limit():
    doc = _doc(page_text={"1": _table(900), "2": ""})
    advanced = dict(SIMPLE, extraction_mode="advanced")
    assert refs.SIMPLE_ROW_LIMIT not in _refs(
        detectors.detect_table_rows(doc, advanced)
    )
    assert detectors.detect_table_rows(doc, dict(SIMPLE, use_bda=True)) == []


@pytest.mark.unit
def test_a_truncation_issue_fires_the_row_limit_without_a_large_table():
    doc = _doc()
    doc["sections"][0]["issues"] = [
        {
            "code": detectors.LIST_TRUNCATED_CODE,
            "severity": "warning",
            "stage": "extraction",
            "message": "short",
        }
    ]
    assert refs.SIMPLE_ROW_LIMIT in _refs(detectors.detect_table_rows(doc, SIMPLE))
    doc["sections"][0]["issues"][0]["code"] = "extraction_sparse"
    assert refs.SIMPLE_ROW_LIMIT not in _refs(detectors.detect_table_rows(doc, SIMPLE))


# ------------------------------------------------------------------ refusal


@pytest.mark.unit
def test_an_input_too_large_refusal_is_the_simple_page_limit():
    doc = _doc(
        status="FAILED",
        error="ExtractionInputTooLarge: Input is too long for requested model",
        pages=66,
    )
    findings = detectors.detect_refusal(doc, SIMPLE)
    assert _refs(findings) == {refs.SIMPLE_PAGE_LIMIT}
    assert "at 66 pages" in findings[0].warning
    # The same refusal in advanced mode is reported but is not that limitation.
    advanced = detectors.detect_refusal(doc, dict(SIMPLE, extraction_mode="advanced"))
    assert advanced and _refs(advanced) == set()


@pytest.mark.unit
def test_page_count_alone_triggers_nothing():
    assert detectors.detect_refusal(_doc(pages=500), SIMPLE) == []


@pytest.mark.unit
def test_a_long_packet_classification_refusal_is_its_limitation():
    doc = _doc(
        status="FAILED",
        error="Classification failed: Input is too long for requested model",
    )
    packet = dict(SIMPLE, classification_granularity="packet")
    assert _refs(detectors.detect_refusal(doc, packet)) == {refs.PACKET_LONG_CONTEXT}
    assert _refs(detectors.detect_refusal(doc, SIMPLE)) == set()


# ------------------------------------------------------------------- script

ARABIC = "تقرير الحساب المصرفي الشهري للعميل " * 3


@pytest.mark.unit
def test_scripts_are_counted_from_letters_only():
    counts = count_scripts("Invoice 12.50 € — " + ARABIC)
    assert counts["Latin"] == len("Invoice")
    assert observed_non_latin(counts) == {"Arabic": counts["Arabic"]}
    assert observed_non_latin(count_scripts("Ж" * 19)) == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("settings", "fires"),
    [
        (dict(SIMPLE, ocr_features=[]), True),
        (dict(SIMPLE, ocr_features=["FORMS"]), True),
        (dict(SIMPLE, use_bda=True), True),
        (SIMPLE, False),  # TABLES/LAYOUT/SIGNATURES cover the broadest set
        (dict(SIMPLE, ocr_features=[], ocr_backend="bedrock"), False),
    ],
)
def test_the_script_detector_needs_both_observation_and_narrow_ocr(settings, fires):
    doc = _doc(text_layer_scripts=count_scripts(ARABIC))
    findings = detectors.detect_script(doc, settings)
    assert (refs.LANGUAGE_SCRIPT in _refs(findings)) is fires
    for f in findings:
        assert "Arabic" in f.warning and "PDF text layer" in f.warning
        assert (
            "language" not in f.warning.split(";")[0].lower()
        )  # names a script, not a language


@pytest.mark.unit
def test_latin_text_never_fires_the_script_detector():
    doc = _doc(page_text={"1": "Kontoauszug für Herrn Müller " * 10})
    assert detectors.detect_script(doc, dict(SIMPLE, ocr_features=[])) == []


# ------------------------------------------------------------------- fields


@pytest.mark.unit
def test_a_list_field_reports_its_lowest_confidence():
    explain = {
        "account": {"confidence": 0.97, "confidence_threshold": 0.8},
        "transactions": [
            {"amount": {"confidence": 0.95, "confidence_threshold": 0.8}},
            {"amount": {"confidence": 0.62, "confidence_threshold": 0.8}},
        ],
    }
    fields = build_fields(
        ["account", "transactions", "due_date"],
        {"account": "123", "transactions": [{"amount": 1}], "due_date": None},
        explain,
        None,
    )
    by_name = {f["name"]: f for f in fields}
    assert by_name["account"] == {
        "name": "account",
        "present": True,
        "confidence": 0.97,
        "below_threshold": False,
    }
    assert by_name["transactions"]["confidence"] == 0.62
    assert by_name["transactions"]["below_threshold"] is True
    assert by_name["due_date"] == {
        "name": "due_date",
        "present": False,
        "confidence": None,
        "below_threshold": None,
    }


@pytest.mark.unit
def test_percentiles_interpolate_between_ranks():
    assert _percentile([10.0, 20.0], 0.5) == 15.0
    assert _percentile([1.0, 2.0, 3.0, 4.0, 100.0], 0.9) == pytest.approx(61.6)
    assert _percentile([], 0.5) is None


@pytest.mark.unit
def test_contract_version_matches_the_profile_form():
    assert contract_version("0.6.16.dev1") == "0.6.16-dev1"
    assert contract_version("0.6.16rc1") == "0.6.16-rc1"
    assert contract_version("0.6.16") == "0.6.16"


# ------------------------------------------------------------------- report


def _record():
    return json.loads(
        (FIXTURES / "synthetic-run-record.json").read_text(encoding="utf-8")
    )


@pytest.mark.unit
def test_the_report_from_a_recorded_run_meets_its_schema():
    report = build_report(_record())
    validate_report(report)
    s = report["summary"]
    assert s["documents"] == s["succeeded"] + s["failed"] == len(report["documents"])
    assert s["refused"] == sum(d["status"] == "refused" for d in report["documents"])
    assert s["refused"] >= 1 and s["failed"] > s["refused"]


@pytest.mark.unit
def test_refused_files_carry_their_ref_and_reason():
    report = build_report(_record())
    refused = {d["file"]: d for d in report["documents"] if d["status"] == "refused"}
    assert refused["slides.pptx"]["triggered_limitations"] == [refs.UNSUPPORTED_FORMAT]
    assert refused["old.doc"]["triggered_limitations"] == [refs.LEGACY_OFFICE]
    assert all(d["error"].startswith("Refused before upload") for d in refused.values())


@pytest.mark.unit
def test_costs_come_from_the_shared_pricing_code():
    from idp_common.config.models import IDPConfig
    from idp_common.reporting.pricing import build_pricing_map, estimate_metering_cost

    record = _record()
    pricing = build_pricing_map(
        IDPConfig.model_validate(
            {"pricing": record["configuration"]["pricing"]}
        ).pricing
    )
    report = build_report(record)
    for doc in record["documents"]:
        expected = estimate_metering_cost(doc["metering"], pricing).estimated_cost_usd
        got = next(d for d in report["documents"] if d["file"] == doc["file"])
        assert got["estimated_cost_usd"] == (
            round(expected, 6) if expected is not None else None
        )
    assert any(d["estimated_cost_usd"] for d in report["documents"])


@pytest.mark.unit
def test_without_ground_truth_evaluation_is_null_and_the_report_says_why():
    record = _record()
    report = build_report(record)
    assert report["evaluation"] is None
    assert CONFIDENCE_IS_NOT_ACCURACY in render_markdown(report, record)


@pytest.mark.unit
def test_with_ground_truth_evaluation_is_the_mean_of_per_document_metrics():
    record = _record()
    record["ground_truth"] = True
    done = [d for d in record["documents"] if d["status"] == "COMPLETED"]
    done[0]["evaluation"] = {
        "precision": 1.0,
        "recall": 0.5,
        "f1_score": 0.6667,
        "accuracy": 0.5,
    }
    done[1]["evaluation"] = {
        "precision": 0.5,
        "recall": 0.5,
        "f1_score": 0.5,
        "accuracy": 0.25,
    }
    report = build_report(record)
    validate_report(report)
    e = report["evaluation"]
    assert e["documents_evaluated"] == 2
    assert e["precision"] == 0.75 and e["accuracy"] == 0.375
    assert CONFIDENCE_IS_NOT_ACCURACY not in render_markdown(report, record)


@pytest.mark.unit
@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.setdefault("notes", "extra top-level field"),
        lambda r: r["documents"][0].update(status="ok"),
        lambda r: r["documents"][0]["triggered_limitations"].append(
            "limitation:Not_An_Id"
        ),
        lambda r: r["summary"].pop("failed"),
    ],
)
def test_the_schema_rejects_a_report_that_breaks_the_contract(mutate):
    report = copy.deepcopy(build_report(_record()))
    mutate(report)
    with pytest.raises(jsonschema.ValidationError):
        validate_report(report)


@pytest.mark.unit
def test_from_record_rebuilds_offline(tmp_path):
    from idp_sdk import IDPClient

    source = tmp_path / "fit-run-record.json"
    source.write_text(json.dumps(_record()), encoding="utf-8")
    result = IDPClient().fit_check.run(
        from_record=str(source), output_dir=str(tmp_path / "out")
    )
    assert result.record_path is None  # the record is the input, not rewritten
    written = json.loads(Path(result.report_path).read_text(encoding="utf-8"))
    assert written == build_report(_record())
