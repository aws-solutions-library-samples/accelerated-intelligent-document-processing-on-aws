# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Multi-document discovery's code-level model defaults, per Region family.

A ``us.`` inference profile is rejected as an invalid model identifier from an EU
Region, and some are from Canada. The deploy-time swap in
``src/lambda/update_configuration`` rewrites stored configuration, but a code-level
fallback is reached without it: by the SDK and CLI's local discovery pipeline,
which never runs the swap, and by the deployed handlers when their configuration
cannot be read. These fallbacks therefore follow the Region.

The rows must be what the swap would produce for the same id.
``src/lambda/update_configuration/test_eu_region.py`` asserts that against the
swap tables, so a model moved in one place and not the other fails there.
"""

from __future__ import annotations

import os
from typing import Optional

DEFAULT_EMBEDDING_MODEL_ID = "us.cohere.embed-v4:0"
DEFAULT_ANALYSIS_MODEL_ID = "us.anthropic.claude-sonnet-4-6"

#: Region-name prefix -> {US default: the default callable from that Region}. An id
#: absent from a family's row is callable there as it is.
REGIONAL_DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "eu-": {
        DEFAULT_EMBEDDING_MODEL_ID: "eu.cohere.embed-v4:0",
        DEFAULT_ANALYSIS_MODEL_ID: "eu.anthropic.claude-sonnet-4-6",
    },
    # Cohere Embed v4 has no ca. profile; the US Sonnet 4.6 profile is callable
    # from ca-central-1, so it stays.
    "ca-": {
        DEFAULT_EMBEDDING_MODEL_ID: "global.cohere.embed-v4:0",
    },
}


def regional_default(model_id: str, region: Optional[str] = None) -> str:
    """``model_id``, or its equivalent callable from ``region``.

    ``region`` defaults to ``AWS_REGION``.
    """
    region = region or os.environ.get("AWS_REGION", "")
    for prefix, row in REGIONAL_DEFAULT_MODELS.items():
        if region.startswith(prefix):
            return row.get(model_id, model_id)
    return model_id


def default_embedding_model_id(region: Optional[str] = None) -> str:
    """The default multimodal embedding model callable from ``region``."""
    return regional_default(DEFAULT_EMBEDDING_MODEL_ID, region)


def default_analysis_model_id(region: Optional[str] = None) -> str:
    """The default cluster-analysis model callable from ``region``."""
    return regional_default(DEFAULT_ANALYSIS_MODEL_ID, region)
