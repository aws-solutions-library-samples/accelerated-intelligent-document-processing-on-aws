# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#780: `extraction.prompt_cache: off` sends no cache points on either path."""

import pytest

from idp_common.extraction.service import ExtractionService

pytestmark = pytest.mark.unit

SCHEMA = {
    "$id": "Invoice",
    "type": "object",
    "properties": {"Total": {"type": "string"}},
}


def _svc(prompt_cache):
    return ExtractionService(
        region="us-west-2",
        config={
            "extraction": {
                "model": "us.anthropic.claude-sonnet-5",
                "prompt_cache": prompt_cache,
            },
            "classes": [SCHEMA],
        },
    )


def _texts(content):
    return " ".join(c.get("text", "") for c in content)


def test_default_keeps_the_marker_for_the_client_to_turn_into_a_cachepoint():
    content = _svc("auto")._build_prompt_content("static <<CACHEPOINT>> dynamic")
    assert "<<CACHEPOINT>>" in _texts(content)


def test_off_strips_every_marker_before_content_is_built():
    content = _svc("off")._build_prompt_content("a <<CACHEPOINT>> b <<CACHEPOINT>> c")
    assert "<<CACHEPOINT>>" not in _texts(content)
    assert "a" in _texts(content) and "c" in _texts(content)


def test_config_model_rejects_unknown_values():
    with pytest.raises(Exception):
        _svc("sometimes")


def test_agentic_prompt_content_has_no_cachepoint_when_off():
    agentic = pytest.importorskip("idp_common.extraction.agentic_idp")
    blocks_on = agentic._prepare_prompt_content(
        prompt="hello",
        page_images=None,
        existing_data=None,
        model_id="us.anthropic.claude-sonnet-5",
    )
    blocks_off = agentic._prepare_prompt_content(
        prompt="hello",
        page_images=None,
        existing_data=None,
        model_id="us.anthropic.claude-sonnet-5",
        prompt_cache="off",
    )
    assert any("cachePoint" in b for b in blocks_on)
    assert not any("cachePoint" in b for b in blocks_off)


def test_agentic_model_config_has_no_cache_flags_when_off():
    agentic = pytest.importorskip("idp_common.extraction.agentic_idp")
    kw = dict(
        model_id="us.anthropic.claude-sonnet-5",
        max_tokens=None,
        max_retries=1,
        connect_timeout=1.0,
        read_timeout=1.0,
    )
    on = agentic._build_model_config(**kw)
    off = agentic._build_model_config(**kw, prompt_cache="off")
    assert on.get("cache_prompt") == "default"
    assert "cache_prompt" not in off and "cache_tools" not in off


def test_the_documented_yaml_value_works():
    """PyYAML reads a bare `off` as boolean False (and `on` as True); the documented
    snippet must validate and mean off. Quoted and boolean spellings too."""
    import yaml

    from idp_common.config.models import ExtractionConfig

    doc_snippet = yaml.safe_load(
        "extraction:\n  prompt_cache: off     # auto (default) | off\n"
    )
    assert doc_snippet["extraction"]["prompt_cache"] is False  # the trap
    assert ExtractionConfig(**doc_snippet["extraction"]).prompt_cache == "off"
    assert ExtractionConfig(prompt_cache="off").prompt_cache == "off"
    assert ExtractionConfig(prompt_cache=False).prompt_cache == "off"
    assert ExtractionConfig(prompt_cache="false").prompt_cache == "off"
    assert ExtractionConfig(prompt_cache=True).prompt_cache == "auto"
    assert ExtractionConfig(prompt_cache="auto").prompt_cache == "auto"
    assert ExtractionConfig().prompt_cache == "auto"


def test_config_validate_accepts_the_documented_yaml_snippet():
    import yaml

    from idp_common.config.merge_utils import validate_config

    cfg = yaml.safe_load(
        "extraction:\n  prompt_cache: off\nclasses:\n  - $id: A\n    type: object\n    properties: {X: {type: string}}\n"
    )
    result = validate_config(cfg)
    assert result["valid"], result["errors"]
    assert not any("caching" in w.lower() for w in result["warnings"])
