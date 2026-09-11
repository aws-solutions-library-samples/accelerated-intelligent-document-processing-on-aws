# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#780: config validation warns, per class, when a Simple-mode prompt prefix is
under the extraction model's minimum cacheable prefix — naming both numbers."""

import pytest

from idp_common.config.merge_utils import _validate_prompt_cache_prefix

pytestmark = pytest.mark.unit

THIN = {"$id": "Thin", "type": "object", "properties": {"A": {"type": "string"}}}
# ~900 tokens of prose: over the 512 Opus 5 minimum, under the 1,024 Sonnet one.
MID = {
    "$id": "Mid",
    "type": "object",
    "properties": {
        f"Field{i}": {"type": "string", "description": "A medium description " * 4}
        for i in range(24)
    },
}
FAT = {
    "$id": "Fat",
    "type": "object",
    "properties": {
        # ~6,000 tokens of prose: clearly above even the 4,096 Haiku minimum.
        f"Field{i}": {"type": "string", "description": "A long description " * 20}
        for i in range(60)
    },
}


def _merged(model, classes, *, mode="simple", prompt_cache="auto", marker=True):
    task = "Extract {ATTRIBUTE_NAMES_AND_DESCRIPTIONS} for {DOCUMENT_CLASS}. "
    task += "<<CACHEPOINT>> " if marker else ""
    task += "{DOCUMENT_TEXT}"
    return {
        "extraction": {
            "mode": mode,
            "agentic": {"enabled": mode == "advanced"},
            "model": model,
            "prompt_cache": prompt_cache,
            "system_prompt": "You extract fields.",
            "task_prompt": task,
        },
        "classes": classes,
    }


def _warnings(cfg):
    result = {"warnings": [], "errors": []}
    _validate_prompt_cache_prefix(cfg, result)
    return result["warnings"]


def test_thin_class_on_sonnet_is_named_with_both_numbers():
    w = _warnings(_merged("us.anthropic.claude-sonnet-5", [THIN, FAT]))
    assert len(w) == 1
    assert "Thin (~" in w[0] and "Fat" not in w[0]
    assert "1024 tokens" in w[0] and "us.anthropic.claude-sonnet-5" in w[0]
    assert "prompt_cache: off" in w[0]


def test_the_same_class_caches_on_opus_5_but_not_on_sonnet_or_haiku():
    """Newer is not safer: a ~900-token class clears Opus 5's 512 minimum, misses
    Sonnet's 1,024, and misses Haiku 4.5's 4,096 along with the thin class."""
    assert _warnings(_merged("us.anthropic.claude-opus-5", [MID, FAT])) == []
    w = _warnings(_merged("us.anthropic.claude-sonnet-5", [MID, FAT]))
    assert len(w) == 1 and "1 class(es)" in w[0] and "Mid (~" in w[0]
    w = _warnings(
        _merged("us.anthropic.claude-haiku-4-5-20251001-v1:0", [THIN, MID, FAT])
    )
    assert len(w) == 1 and "2 class(es)" in w[0] and "4096 tokens" in w[0]
    assert "Thin (~" in w[0] and "Mid (~" in w[0] and "Fat" not in w[0]


def test_no_warning_when_caching_is_off_or_there_is_no_marker_or_the_path_is_advanced():
    assert (
        _warnings(_merged("us.anthropic.claude-sonnet-5", [THIN], prompt_cache="off"))
        == []
    )
    assert (
        _warnings(_merged("us.anthropic.claude-sonnet-5", [THIN], marker=False)) == []
    )
    assert (
        _warnings(_merged("us.anthropic.claude-sonnet-5", [THIN], mode="advanced"))
        == []
    )


def test_no_warning_for_models_without_a_published_minimum():
    assert _warnings(_merged("us.amazon.nova-pro-v1:0", [THIN])) == []
    assert _warnings(_merged("LambdaHook", [THIN])) == []


def test_borderline_class_is_reported_as_may_not_cache_not_never():
    # ~1,040 tokens of prompt: over 1,024 but inside the estimate's 10% band.
    near = {
        "$id": "Near",
        "type": "object",
        "properties": {"F": {"type": "string", "description": "x" * 4000}},
    }
    w = _warnings(_merged("us.anthropic.claude-sonnet-5", [near]))
    assert len(w) == 1 and "MAY not cache" in w[0] and "within 10%" in w[0]
    assert "Near (~" in w[0]


def test_yaml_boolean_false_means_off():
    cfg = _merged("us.anthropic.claude-sonnet-5", [THIN])
    cfg["extraction"]["prompt_cache"] = False  # what `prompt_cache: off` parses to
    assert _warnings(cfg) == []


def test_integrated_confidence_estimates_the_topk_prompt_that_is_actually_sent():
    """Under Simple + integrated confidence the service sends the 1S-TopK prompt,
    not task_prompt. A long TopK prompt lifts the thin class over the minimum."""
    cfg = _merged("us.anthropic.claude-sonnet-5", [THIN])
    cfg["extraction"]["confidence"] = {"mode": "integrated", "enabled": True}
    cfg["extraction"]["task_prompt_extraction_with_confidence_topk"] = (
        "Rank guesses. " * 400
        + "{ATTRIBUTE_NAMES_AND_DESCRIPTIONS} <<CACHEPOINT>> {DOCUMENT_TEXT}"
    )
    assert _warnings(cfg) == []
    cfg["extraction"]["confidence"] = {"mode": "separate", "enabled": True}
    assert len(_warnings(cfg)) == 1  # back on the short task_prompt


def test_a_per_class_prompt_override_is_what_gets_estimated():
    long_override = (
        "Own prompt. " * 500
        + "{ATTRIBUTE_NAMES_AND_DESCRIPTIONS} <<CACHEPOINT>> {DOCUMENT_TEXT}"
    )
    cls = dict(THIN, **{"x-aws-idp-extraction-task-prompt": long_override})
    assert _warnings(_merged("us.anthropic.claude-sonnet-5", [cls])) == []
    no_marker = dict(
        THIN, **{"x-aws-idp-extraction-task-prompt": "plain {DOCUMENT_TEXT}"}
    )
    assert _warnings(_merged("us.anthropic.claude-sonnet-5", [no_marker])) == []
