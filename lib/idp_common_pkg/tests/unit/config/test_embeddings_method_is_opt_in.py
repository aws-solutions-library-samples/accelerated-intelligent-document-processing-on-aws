# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Section classification by embeddings (#1657) changes nothing it is not asked to.

Every shipped preset, merged with the system defaults through the real merge, still
selects the LLM labeler, carries no ``embeddings`` block in its merged dictionary
(the shipped defaults document the keys in comments only, so the merged dictionary
is what it was), and validates with the code defaults. The typed model's dump of
the ``embeddings`` block does gain the new keys, at defaults that are read only
under ``method: embeddings``.
"""

from pathlib import Path

import pytest
import yaml

from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.config.models import EmbeddingsConfig, IDPConfig

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[5]
_PRESETS = sorted(
    p
    for p in (_REPO / "config_library" / "unified").glob("*/*.yaml")
    if p.name != "README.yaml"
)


def test_presets_are_found():
    assert len([p for p in _PRESETS if p.name == "config.yaml"]) > 1


@pytest.mark.parametrize("path", _PRESETS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_a_preset_does_not_select_or_carry_embeddings(path):
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or "classes" not in raw:
        pytest.skip("not a configuration file")
    merged = merge_config_with_defaults(raw)
    section = (
        merged.get("classification", {})
        .get("split_then_classify", {})
        .get("section_classification", {})
    )
    assert section.get("method", "llm") == "llm"
    assert "embeddings" not in section
    model = IDPConfig(**merged)
    typed = model.classification.split_then_classify.section_classification
    assert typed.method == "llm"
    assert typed.embeddings == EmbeddingsConfig()
