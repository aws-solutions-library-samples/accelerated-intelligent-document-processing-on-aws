# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Simple + integrated confidence is downgraded to a separate pass on list-bearing
classes.

Simple + ``integrated`` (1S-TopK) puts values and per-cell confidence for the whole
section into one response, and on a class with a list field the model stops
emitting rows rather than erroring. Measured at the shipped default extraction
model (config-guidance §2.1): 1–10 of 100 rows on 4/4 repeats, 5–10 of 400, an
800-row list ABSENT — all reporting COMPLETED with scalar accuracy 1.000. Mean
recall 0.294. Advanced + integrated is unaffected (sharding keeps calls small).

The downgrade is a per-section RUNTIME decision, not a config rejection: a stored
config that validated yesterday must still load today.
"""

from __future__ import annotations

import pytest

from idp_common.config.models import IDPConfig
from idp_common.extraction.prompt_assembly import select_extraction_task_prompt
from idp_common.extraction.service import ExtractionService

LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "AccountNumber": {"type": "string"},
        "Transactions": {"type": "array", "items": {"type": "object"}},
    },
}
SCALAR_SCHEMA = {
    "type": "object",
    "properties": {"AccountNumber": {"type": "string"}, "Total": {"type": "number"}},
}


def _svc(*, mode: str, confidence: str, schema: dict) -> ExtractionService:
    cfg = IDPConfig(
        **{
            "extraction": {
                "mode": mode,
                "agentic": {"enabled": mode == "advanced"},
                "confidence": {"mode": confidence},
                "task_prompt": "PLAIN {DOCUMENT_TEXT}",
                "task_prompt_extraction_with_confidence_topk": "TOPK {DOCUMENT_TEXT}",
                "task_prompt_extraction_with_confidence": "TOOL {DOCUMENT_TEXT}",
            }
        }
    )
    svc = ExtractionService(config=cfg)
    svc._reset_context()
    svc._class_schema = schema
    svc._class_label = "BankStatement"
    return svc


@pytest.mark.unit
def test_simple_integrated_on_a_list_class_is_downgraded():
    svc = _svc(mode="simple", confidence="integrated", schema=LIST_SCHEMA)
    reason = svc._simple_integrated_list_downgrade()
    assert reason and "Transactions" in reason and "0.294" in reason
    # No inline confidence -> the standalone Assessment step will run.
    assert svc._integrated_assessment_enabled() is False
    # And the extraction prompt is the plain one, not 1S-TopK.
    assert (
        select_extraction_task_prompt(
            svc.config.extraction,
            integrated_ok=svc._simple_integrated_list_downgrade() is None,
        )
        == "PLAIN {DOCUMENT_TEXT}"
    )


@pytest.mark.unit
def test_simple_integrated_on_a_scalar_only_class_is_unchanged():
    """The single-inference saving is kept where it is safe."""
    svc = _svc(mode="simple", confidence="integrated", schema=SCALAR_SCHEMA)
    assert svc._simple_integrated_list_downgrade() is None
    assert svc._integrated_assessment_enabled() is True
    assert (
        select_extraction_task_prompt(svc.config.extraction, integrated_ok=True)
        == "TOPK {DOCUMENT_TEXT}"
    )


@pytest.mark.unit
def test_advanced_integrated_on_a_list_class_is_unchanged():
    """Sharding keeps each agentic call small; benchmarked recall 1.000."""
    svc = _svc(mode="advanced", confidence="integrated", schema=LIST_SCHEMA)
    assert svc._simple_integrated_list_downgrade() is None
    assert svc._integrated_assessment_enabled() is True
    assert (
        select_extraction_task_prompt(svc.config.extraction, integrated_ok=True)
        == "TOOL {DOCUMENT_TEXT}"
    )


@pytest.mark.unit
def test_separate_and_off_are_never_touched():
    for confidence in ("separate", "off"):
        svc = _svc(mode="simple", confidence=confidence, schema=LIST_SCHEMA)
        assert svc._simple_integrated_list_downgrade() is None
        assert svc._integrated_assessment_enabled() is False


@pytest.mark.unit
def test_multi_instance_wrapper_counts_as_list_bearing():
    """``instances`` is a top-level array and truncates like any other list."""
    wrapper = {
        "type": "object",
        "properties": {
            "instances": {"type": "array", "minItems": 1, "items": {"type": "object"}}
        },
        "required": ["instances"],
    }
    svc = _svc(mode="simple", confidence="integrated", schema=wrapper)
    assert svc._simple_integrated_list_downgrade() is not None


@pytest.mark.unit
def test_the_decision_is_per_section_and_resets():
    """A scalar-only section processed after a list section must not inherit the
    downgrade, and vice versa."""
    svc = _svc(mode="simple", confidence="integrated", schema=LIST_SCHEMA)
    assert svc._simple_integrated_list_downgrade() is not None
    svc._reset_context()
    svc._class_schema = SCALAR_SCHEMA
    assert svc._simple_integrated_list_downgrade() is None
    assert svc._integrated_assessment_enabled() is True


@pytest.mark.unit
def test_downgrade_emits_a_processing_issue_and_metadata():
    svc = _svc(mode="simple", confidence="integrated", schema=LIST_SCHEMA)
    svc._simple_integrated_list_downgrade()
    metadata: dict = {}
    issues = svc._build_extraction_issues(
        extracted_fields={"AccountNumber": "1", "Transactions": [{"a": 1}]},
        metadata=metadata,
        section_id="s1",
    )
    codes = [i.code for i in issues]
    assert "confidence_integrated_downgraded" in codes
    issue = next(i for i in issues if i.code == "confidence_integrated_downgraded")
    assert issue.severity == "info"  # a routing decision, not a shortfall
    assert issue.section_id == "s1"
    assert issue.details["effective_confidence_mode"] == "separate"
    assert issue.details["list_fields"] == ["Transactions"]
    assert metadata["confidence_mode_effective"] == "separate"
    # A populated list must NOT also trip the empty-list issue.
    assert "extraction_incomplete" not in codes


@pytest.mark.unit
def test_no_issue_when_nothing_was_downgraded():
    svc = _svc(mode="simple", confidence="integrated", schema=SCALAR_SCHEMA)
    svc._simple_integrated_list_downgrade()
    issues = svc._build_extraction_issues(
        extracted_fields={"AccountNumber": "1", "Total": 2.0},
        metadata={},
        section_id="s1",
    )
    assert all(i.code != "confidence_integrated_downgraded" for i in issues)


@pytest.mark.unit
def test_a_per_class_task_prompt_override_opts_the_class_out():
    """The downgrade works by swapping the prompt. When the user controls the prompt
    it must not half-apply (gate False but the TopK prompt sent would leave raw
    {G1,P1} candidate objects in the result), so the class stays integrated."""
    schema = dict(
        LIST_SCHEMA, **{"x-aws-idp-extraction-task-prompt": "MY TOPK {DOCUMENT_TEXT}"}
    )
    svc = _svc(mode="simple", confidence="integrated", schema=schema)
    assert svc._simple_integrated_list_downgrade() is None
    assert svc._integrated_assessment_enabled() is True


@pytest.mark.unit
def test_the_content_builder_sends_the_plain_prompt_for_a_downgraded_class():
    """Pins the call site that matters (the simple path's prompt selection), which
    the direct prompt_assembly test could not: drop `integrated_ok=` there and the
    1S-TopK prompt goes out while the split stays off."""
    from unittest.mock import patch

    from idp_common.extraction import prompt_assembly

    svc = _svc(mode="simple", confidence="integrated", schema=LIST_SCHEMA)
    svc._document_text = "doc text"
    seen = {}
    real = prompt_assembly.select_extraction_task_prompt

    def spy(cfg, **kw):
        seen.update(kw)
        return real(cfg, **kw)

    with patch.object(prompt_assembly, "select_extraction_task_prompt", spy):
        try:
            content, _system = svc._build_extraction_content(
                document=None, page_images=[]
            )  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - the selection happens before any use of document
            content = None
    assert seen.get("integrated_ok") is False
    if content is not None:
        assert any("PLAIN" in str(block) for block in content)
        assert not any("TOPK" in str(block) for block in content)


@pytest.mark.unit
def test_processing_flow_reports_the_effective_mode():
    svc = _svc(mode="simple", confidence="integrated", schema=LIST_SCHEMA)
    svc._simple_integrated_list_downgrade()
    flow = svc._build_processing_flow(
        metadata={}, extraction_method="simple", tool_used=False
    )
    conf = next(s for s in flow["stages"] if s["key"] == "confidence")
    assert "separate pass" in conf["detail"] and "downgraded" in conf["detail"]
    assert "inline" not in conf["detail"]


@pytest.mark.unit
def test_config_validation_warns_about_the_affected_classes_without_failing():
    from idp_common.config.merge_utils import validate_config

    config = {
        "extraction": {
            "mode": "simple",
            "agentic": {"enabled": False},
            "confidence": {"mode": "integrated"},
        },
        "classes": [
            {
                "$id": "BankStatement",
                "type": "object",
                "properties": LIST_SCHEMA["properties"],
            },
            {
                "$id": "IdCard",
                "type": "object",
                "properties": SCALAR_SCHEMA["properties"],
            },
            {
                "$id": "Custom",
                "type": "object",
                "properties": LIST_SCHEMA["properties"],
                "x-aws-idp-extraction-task-prompt": "x",
            },
        ],
    }
    result = validate_config(config)
    hits = [w for w in result.get("warnings", []) if "declare list fields" in w]
    assert len(hits) == 1 and "BankStatement" in hits[0]
    assert "IdCard" not in hits[0] and "Custom" not in hits[0]
    assert result["valid"] is True or not any(
        "integrated" in e for e in result.get("errors", [])
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
