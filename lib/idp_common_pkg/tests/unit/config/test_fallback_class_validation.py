# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``validate_config`` warns when the classification fallback names no class.

``classification.fallback_class`` (``invalidClassFallback`` before 0.9) is the
class a page is given when its class cannot be decided. Any name other than the
built-in ``unclassified`` must be a configured class for that page to have an
extraction schema; nothing checked that, so a fallback such as ``other`` that no
class declares was accepted in silence and the pages it landed on yielded no data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from idp_common.config.merge_utils import validate_config

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[5]


def _config(fallback: Optional[str], *names: str, flat: bool = False) -> Dict:
    config: Dict[str, Any] = {
        "classes": [
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": name,
                "x-aws-idp-document-type": name,
                "type": "object",
                "description": f"A {name}",
                "properties": {"total": {"type": "string"}},
            }
            for name in names
        ]
    }
    if fallback is not None:
        key = "invalidClassFallback" if flat else "fallback_class"
        config["classification"] = {key: fallback}
    return config


def _fallback_findings(result: Dict[str, Any]) -> List[str]:
    return [m for m in result["warnings"] + result["errors"] if "fallback_class" in m]


@pytest.mark.parametrize("fallback", [None, "unclassified", "Invoice", "Receipt"])
def test_a_configured_or_built_in_fallback_is_not_reported(fallback):
    result = validate_config(_config(fallback, "Invoice", "Receipt"))
    assert result["valid"] is True
    assert _fallback_findings(result) == []


def test_an_unconfigured_fallback_is_a_warning_naming_it():
    result = validate_config(_config("other", "Invoice", "Receipt"))
    # A warning, not an error: the configuration still runs and still uploads.
    assert result["valid"] is True
    assert result["errors"] == []
    [finding] = _fallback_findings(result)
    assert "'other'" in finding
    assert "nothing is extracted" in finding
    assert "Did you mean" not in finding


@pytest.mark.parametrize("written", ["invoice", "Invoce"])
def test_a_near_miss_suggests_the_configured_class(written):
    [finding] = _fallback_findings(
        validate_config(_config(written, "Invoice", "Receipt"))
    )
    assert f"'{written}'" in finding
    assert finding.endswith("Did you mean 'Invoice'?")


def test_a_pre_0_9_flat_fallback_is_judged_after_migration():
    [finding] = _fallback_findings(
        validate_config(_config("other", "Invoice", flat=True))
    )
    assert "'other'" in finding


def test_the_match_reads_the_document_type_not_the_schema_id():
    # The classification service names a class by x-aws-idp-document-type.
    config = _config("Bill", "Invoice")
    config["classes"][0]["$id"] = "Bill"
    assert len(_fallback_findings(validate_config(config))) == 1


def test_the_system_defaults_alone_are_not_reported():
    assert _fallback_findings(validate_config({})) == []


_PRESETS = sorted((_REPO / "config_library" / "unified").glob("*/config.yaml"))


def test_presets_were_found():
    assert _PRESETS, "no config_library/unified presets found"


@pytest.mark.parametrize("preset", _PRESETS, ids=lambda p: p.parent.name)
def test_no_shipped_preset_is_reported(preset):
    config = yaml.safe_load(preset.read_text())
    assert _fallback_findings(validate_config(config)) == []


def test_a_short_name_differing_only_in_case_still_gets_a_suggestion():
    [finding] = _fallback_findings(validate_config(_config("w2", "W2", "Paystub")))
    assert finding.endswith("Did you mean 'W2'?")


def test_the_built_in_fallback_is_matched_exactly():
    [finding] = _fallback_findings(validate_config(_config("Unclassified", "Invoice")))
    assert "'Unclassified'" in finding


def test_a_legacy_class_list_is_read_after_conversion():
    legacy = {
        "classes": [
            {
                "name": "Invoice",
                "description": "An invoice",
                "attributes": [{"name": "total", "description": "Total"}],
            }
        ],
        "classification": {"fallback_class": "Invoice"},
    }
    assert _fallback_findings(validate_config(legacy)) == []
    legacy["classification"]["fallback_class"] = "other"
    assert len(_fallback_findings(validate_config(legacy))) == 1
