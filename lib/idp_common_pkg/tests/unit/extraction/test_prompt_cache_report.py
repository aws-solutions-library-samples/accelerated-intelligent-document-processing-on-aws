# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#780 item 2: the section result carries a per-class prompt-cache summary and
the text Processing Report prints it."""

import time
from unittest.mock import patch

import pytest

from idp_common.extraction.service import (
    ExtractionResult,
    ExtractionService,
    SectionInfo,
)
from idp_common.models import Document, Section, Status

pytestmark = pytest.mark.unit

SCHEMA = {
    "$id": "Invoice",
    "type": "object",
    "properties": {"Total": {"type": "string"}},
}


def _svc(prompt_cache="auto"):
    return ExtractionService(
        region="us-west-2",
        config={
            "extraction": {
                "model": "us.anthropic.claude-sonnet-4-6",
                "prompt_cache": prompt_cache,
            },
            "classes": [SCHEMA],
        },
    )


MODEL = "us.anthropic.claude-sonnet-4-6"


def _marker_seen(svc):
    # a Simple-mode prompt with a marker was built for this section
    svc._build_prompt_content("static <<CACHEPOINT>> dynamic")
    return svc


def test_section_metadata_gets_the_summary_before_metering_is_merged():
    md: dict = {}
    _marker_seen(_svc())._record_prompt_cache_metadata(
        md,
        {
            "Extraction/bedrock/us.anthropic.claude-sonnet-4-6": {
                "inputTokens": 30,
                "cacheReadInputTokens": 970,
                "cacheWriteInputTokens": 0,
                "requests": 1,
            }
        },
    )
    assert md["prompt_cache"]["state"] == "caching"
    assert md["prompt_cache"]["read_share"] == 0.97


def test_off_configuration_is_recorded_as_disabled():
    md: dict = {}
    _svc("off")._record_prompt_cache_metadata(
        md,
        {
            "Extraction/bedrock/us.anthropic.claude-sonnet-4-6": {
                "inputTokens": 1000,
                "cacheReadInputTokens": 0,
                "cacheWriteInputTokens": 0,
            }
        },
    )
    assert md["prompt_cache"]["state"] == "disabled"


def _zero_zero(model=MODEL):
    return {
        f"Extraction/bedrock/{model}": {
            "inputTokens": 949,
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 0,
        }
    }


def test_marker_less_simple_prompt_is_no_cache_point_not_inert():
    svc = _svc()
    svc._build_prompt_content("no marker here")
    md: dict = {}
    svc._record_prompt_cache_metadata(md, _zero_zero())
    assert md["prompt_cache"]["state"] == "no-cache-point"
    assert md["prompt_cache"]["cache_point_sent"] is False


def test_marker_present_but_model_unsupported_is_no_cache_point():
    svc = _marker_seen(_svc())
    svc._pending_extraction_model = "xai.grok-4"
    md: dict = {}
    svc._record_prompt_cache_metadata(md, _zero_zero("xai.grok-4"))
    assert md["prompt_cache"]["state"] == "no-cache-point"


def test_marker_present_and_model_supported_zero_zero_is_inert():
    svc = _marker_seen(_svc())
    md: dict = {}
    svc._record_prompt_cache_metadata(md, _zero_zero())
    assert md["prompt_cache"]["state"] == "never-cached"
    assert md["prompt_cache"]["cache_point_sent"] is True


def test_reset_context_clears_the_marker_flag_for_the_next_section():
    svc = _marker_seen(_svc())
    assert svc._pending_cache_marker_seen is True
    svc._reset_context()
    assert svc._pending_cache_marker_seen is False


def test_save_results_records_the_section_summary_before_merging_into_the_document():
    """End to end through _save_results: the written result.json carries the
    section's own verdict, and the document total is merged afterwards."""
    svc = _marker_seen(_svc())
    svc._pending_extraction_model = MODEL
    doc = Document(
        id="d",
        input_key="d.pdf",
        input_bucket="in",
        output_bucket="out",
        status=Status.EXTRACTING,
    )
    doc.metering = {
        f"Extraction/bedrock/{MODEL}": {
            "inputTokens": 5,
            "cacheReadInputTokens": 5,
            "cacheWriteInputTokens": 0,
        }
    }
    section = Section(section_id="1", classification="Invoice", page_ids=["1"])
    doc.sections = [section]
    result = ExtractionResult(
        extracted_fields={"Total": "12.50"},
        metering={
            f"Extraction/bedrock/{MODEL}": {
                "inputTokens": 30,
                "cacheReadInputTokens": 0,
                "cacheWriteInputTokens": 970,
                "requests": 1,
            }
        },
        parsing_succeeded=True,
        total_duration=1.5,
    )
    info = SectionInfo(
        class_label="Invoice",
        sorted_page_ids=["1"],
        page_indices=[0],
        output_bucket="out",
        output_key="d.pdf/sections/1/result.json",
        output_uri="s3://out/d.pdf/sections/1/result.json",
        start_page=1,
        end_page=1,
    )
    with patch("idp_common.extraction.service.s3.write_content") as write:
        svc._save_results(doc, section, result, info, "1", time.time())

    written = write.call_args.args[0]
    summary = written["metadata"]["prompt_cache"]
    assert summary["state"] == "write-only", "the SECTION's metering, not the total"
    assert summary["cache_write_input_tokens"] == 970
    assert "Prompt cache (this section):" in written["processing_report"]
    merged = doc.metering[f"Extraction/bedrock/{MODEL}"]
    assert (
        merged["cacheReadInputTokens"] == 5 and merged["cacheWriteInputTokens"] == 970
    )


def test_no_bedrock_call_adds_nothing():
    md: dict = {}
    _svc()._record_prompt_cache_metadata(md, {})
    assert "prompt_cache" not in md


def test_text_report_prints_the_verdict():
    report = _svc()._generate_processing_report(
        {
            "extraction_method": "standard",
            "prompt_cache": {
                "state": "never-cached",
                "input_tokens": 949,
                "cache_read_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "requests": 1,
                "read_share": 0.0,
                "model_ids": ["us.anthropic.claude-sonnet-4-6"],
                "min_cacheable_prefix_tokens": 1024,
            },
        }
    )
    assert "Prompt cache (this section):" in report
    assert "never cached" in report and "1,024 tokens" in report


def test_text_report_is_silent_without_a_summary():
    assert "Prompt cache" not in _svc()._generate_processing_report(
        {"extraction_method": "standard"}
    )
