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
def test_downgrade_is_recorded_in_metadata_and_NOT_as_a_processing_issue():
    """The document-level HasProcessingIssues flag and the list-view badge are
    severity-blind, so even an `info` issue would badge every document of a
    Simple + integrated deployment for a routing decision. Metadata + flow only."""
    svc = _svc(mode="simple", confidence="integrated", schema=LIST_SCHEMA)
    svc._simple_integrated_list_downgrade()
    metadata: dict = {}
    issues = svc._build_extraction_issues(
        extracted_fields={"AccountNumber": "1", "Transactions": [{"a": 1}]},
        metadata=metadata,
        section_id="s1",
    )
    codes = [i.code for i in issues]
    assert "confidence_integrated_downgraded" not in codes
    assert not any("downgrad" in (i.code or "") for i in issues)
    assert metadata["confidence_mode_effective"] == "separate"
    assert "Transactions" in metadata["confidence_mode_downgraded_reason"]
    # A populated list must NOT trip the empty-list issue either.
    assert "extraction_incomplete" not in codes


@pytest.mark.unit
def test_no_metadata_when_nothing_was_downgraded():
    svc = _svc(mode="simple", confidence="integrated", schema=SCALAR_SCHEMA)
    svc._simple_integrated_list_downgrade()
    metadata: dict = {}
    svc._build_extraction_issues(
        extracted_fields={"AccountNumber": "1", "Total": 2.0},
        metadata=metadata,
        section_id="s1",
    )
    assert "confidence_mode_effective" not in metadata


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
    # Short enough not to distort the flow graph; the long reason lives in
    # metadata.confidence_mode_downgraded_reason. `info` matches the decision.
    assert len(conf["detail"]) < 70
    assert conf["status"] == "info"


@pytest.mark.unit
def test_processing_flow_status_is_ok_when_not_downgraded():
    svc = _svc(mode="simple", confidence="integrated", schema=SCALAR_SCHEMA)
    svc._simple_integrated_list_downgrade()
    flow = svc._build_processing_flow(
        metadata={}, extraction_method="simple", tool_used=False
    )
    conf = next(s for s in flow["stages"] if s["key"] == "confidence")
    assert conf["status"] == "ok" and "integrated" in conf["detail"]


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


@pytest.mark.unit
def test_the_allow_flag_keeps_a_list_class_on_integrated():
    """`x-aws-idp-allow-integrated-lists: true` is the explicit opt-in."""
    schema = dict(LIST_SCHEMA, **{"x-aws-idp-allow-integrated-lists": True})
    svc = _svc(mode="simple", confidence="integrated", schema=schema)
    assert svc._simple_integrated_list_downgrade() is None
    assert svc._integrated_assessment_enabled() is True
    # Falsey spellings do not opt in.
    for v in (False, None, 0, ""):
        svc = _svc(
            mode="simple",
            confidence="integrated",
            schema=dict(LIST_SCHEMA, **{"x-aws-idp-allow-integrated-lists": v}),
        )
        assert svc._simple_integrated_list_downgrade() is not None


@pytest.mark.unit
def test_the_allow_flag_survives_the_multi_instance_wrapper():
    """The service reads the flag off `_class_schema`, which for a multi-instance
    class is the `instances[]` wrapper — wrap_class_schema must keep it there."""
    from idp_common.schema.multi_instance import wrap_class_schema

    stored = {
        "$id": "Receipt",
        "type": "object",
        "properties": {"Total": {"type": "number"}},
        "x-aws-idp-multi-instance": True,
        "x-aws-idp-allow-integrated-lists": True,
    }
    wrapped = wrap_class_schema(stored)
    assert "instances" in wrapped["properties"]
    assert wrapped["x-aws-idp-allow-integrated-lists"] is True
    svc = _svc(mode="simple", confidence="integrated", schema=wrapped)
    assert svc._simple_integrated_list_downgrade() is None
    # ...and without the flag the wrapper IS downgraded (instances is a list).
    wrapped_plain = wrap_class_schema(
        {k: v for k, v in stored.items() if "allow" not in k}
    )
    svc = _svc(mode="simple", confidence="integrated", schema=wrapped_plain)
    assert svc._simple_integrated_list_downgrade() is not None


@pytest.mark.unit
def test_the_allow_flag_is_rollback_safe_in_the_config_model():
    """Classes are free-form dicts in IDPConfig (also at v0.6.7), so a stored
    config carrying the new key loads on the previous release unchanged."""
    cfg = IDPConfig(
        **{
            "classes": [
                dict(
                    LIST_SCHEMA,
                    **{"$id": "S", "x-aws-idp-allow-integrated-lists": True},
                )
            ]
        }
    )
    assert cfg.classes[0]["x-aws-idp-allow-integrated-lists"] is True


def _validate(extraction: dict, classes: list) -> dict:
    from idp_common.config.merge_utils import validate_config

    return validate_config({"extraction": extraction, "classes": classes})


def _hits(result: dict) -> list:
    return [w for w in result.get("warnings", []) if "declare list fields" in w]


_LIST_CLASS = {
    "$id": "BankStatement",
    "type": "object",
    "properties": LIST_SCHEMA["properties"],
}


@pytest.mark.unit
def test_config_check_treats_mode_as_authoritative_over_agentic_enabled():
    """The UI writes only `mode`; reconcile_mode_and_agentic derives
    agentic.enabled from it. The check must agree with the runtime, which reads
    the reconciled model."""
    integrated = {"mode": "integrated"}
    # Advanced + integrated: no downgrade at runtime -> no warning.
    assert (
        _hits(_validate({"mode": "advanced", "confidence": integrated}, [_LIST_CLASS]))
        == []
    )
    assert (
        _hits(
            _validate(
                {
                    "mode": "advanced",
                    "agentic": {"enabled": False},
                    "confidence": integrated,
                },
                [_LIST_CLASS],
            )
        )
        == []
    )
    # Simple with a STALE agentic.enabled: the runtime downgrades -> warn.
    assert (
        len(
            _hits(
                _validate(
                    {
                        "mode": "simple",
                        "agentic": {"enabled": True},
                        "confidence": integrated,
                    },
                    [_LIST_CLASS],
                )
            )
        )
        == 1
    )
    # Legacy config without `mode`: fall back to agentic.enabled.
    assert (
        _hits(
            _validate(
                {"agentic": {"enabled": True}, "confidence": integrated}, [_LIST_CLASS]
            )
        )
        == []
    )
    assert (
        len(
            _hits(
                _validate(
                    {"agentic": {"enabled": False}, "confidence": integrated},
                    [_LIST_CLASS],
                )
            )
        )
        == 1
    )


@pytest.mark.unit
def test_config_check_is_silent_when_confidence_is_disabled_or_opted_in():
    off = {"mode": "integrated", "enabled": False}  # reconciles to mode off
    assert _hits(_validate({"mode": "simple", "confidence": off}, [_LIST_CLASS])) == []
    opted = dict(_LIST_CLASS, **{"x-aws-idp-allow-integrated-lists": True})
    assert (
        _hits(
            _validate({"mode": "simple", "confidence": {"mode": "integrated"}}, [opted])
        )
        == []
    )


@pytest.mark.unit
def test_config_check_sees_a_legacy_attributes_list_class():
    """merge_config_with_defaults does not migrate legacy classes but the runtime
    does, so the downgrade fires for them; the check must say so too."""
    legacy = {
        "name": "Statement",
        "attributes": [
            {"name": "AccountNumber", "attributeType": "simple"},
            {"name": "Transactions", "attributeType": "list", "listItemTemplate": {}},
        ],
    }
    hits = _hits(
        _validate({"mode": "simple", "confidence": {"mode": "integrated"}}, [legacy])
    )
    assert len(hits) == 1 and "Statement" in hits[0]
    scalar_legacy = {
        "name": "Card",
        "attributes": [{"name": "Id", "attributeType": "simple"}],
    }
    assert (
        _hits(
            _validate(
                {"mode": "simple", "confidence": {"mode": "integrated"}},
                [scalar_legacy],
            )
        )
        == []
    )


@pytest.mark.unit
def test_agentic_call_site_does_not_consult_the_downgrade():
    """The agentic branch cannot be downgraded (agentic.enabled short-circuits the
    memo), so it must not pass `integrated_ok=` — the one remaining caller is the
    simple content builder."""
    import inspect

    from idp_common.extraction import service as svc_mod

    src = inspect.getsource(svc_mod)
    assert (
        src.count("integrated_ok=self._simple_integrated_list_downgrade() is None") == 1
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
