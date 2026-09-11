# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#780 item 2: the section result carries a per-class prompt-cache summary and
the text Processing Report prints it."""

import pytest

from idp_common.extraction.service import ExtractionService

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


def test_section_metadata_gets_the_summary_before_metering_is_merged():
    md: dict = {}
    _svc()._record_prompt_cache_metadata(
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
