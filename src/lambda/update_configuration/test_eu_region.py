# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An EU deployment leaves no `us.` model id in the shipped defaults.

A `us.` inference profile is rejected as an invalid model identifier from an EU
Region, so a default the EU swap misses is a step that fails on every EU stack.
The multi-document discovery embedding default was one: it had no row in
``MODEL_MAPPINGS``. This walks every string leaf of the merged defaults, so the
next default added without an EU row fails here rather than in a deployed stack.
"""

from __future__ import annotations

import pytest
from test_canada_region import _string_leaves
from test_model_mappings import index

from idp_common.config.merge_utils import merge_config_with_defaults


@pytest.mark.unit
def test_the_eu_swap_leaves_no_us_profile_in_the_shipped_defaults():
    cfg = merge_config_with_defaults({}, pattern="pattern-2", validate=False)
    after = index.swap_model_ids(cfg, "eu")
    left = {
        path: value for path, value in _string_leaves(after) if value.startswith("us.")
    }
    assert left == {}


@pytest.mark.unit
def test_the_discovery_embedding_default_moves_to_its_eu_profile():
    cfg = merge_config_with_defaults({}, pattern="pattern-2", validate=False)
    before = dict(_string_leaves(cfg))
    assert (
        before["discovery.multi_document.embedding_model_id"] == "us.cohere.embed-v4:0"
    )
    after = dict(_string_leaves(index.swap_model_ids(cfg, "eu")))
    assert (
        after["discovery.multi_document.embedding_model_id"] == "eu.cohere.embed-v4:0"
    )


_REGION_TYPES = {"eu-": "eu", "ca-": "ca"}


@pytest.mark.unit
def test_the_discovery_fallbacks_agree_with_the_swap_tables():
    """A code fallback must be what the deploy-time swap makes of the same id.

    Otherwise a stack that read its configuration gets one model and a stack that
    fell back gets another, which is the divergence the fallbacks exist to remove.
    """
    from idp_common.discovery import regional_defaults

    assert set(regional_defaults.REGIONAL_DEFAULT_MODELS) == set(_REGION_TYPES)
    for us_id in (
        regional_defaults.DEFAULT_EMBEDDING_MODEL_ID,
        regional_defaults.DEFAULT_ANALYSIS_MODEL_ID,
    ):
        for prefix, region_type in _REGION_TYPES.items():
            region = f"{prefix}central-1"
            assert regional_defaults.regional_default(
                us_id, region
            ) == index.get_model_mapping(us_id, region_type), (us_id, region)
