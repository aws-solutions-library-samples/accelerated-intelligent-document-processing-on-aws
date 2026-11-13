# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Multi-document discovery's code-level model defaults, per Region family.

The Region translation and its rows live in :mod:`idp_common.regional_models`,
shared with the other code-level fallbacks; this module names discovery's two
defaults and keeps the names their callers import.
"""

from __future__ import annotations

from typing import Optional

from idp_common.regional_models import (
    EMBEDDING_MODEL_ID as DEFAULT_EMBEDDING_MODEL_ID,
)
from idp_common.regional_models import (
    SONNET_4_6_MODEL_ID as DEFAULT_ANALYSIS_MODEL_ID,
)
from idp_common.regional_models import regional_default

__all__ = [
    "DEFAULT_ANALYSIS_MODEL_ID",
    "DEFAULT_EMBEDDING_MODEL_ID",
    "default_analysis_model_id",
    "default_embedding_model_id",
]


def default_embedding_model_id(region: Optional[str] = None) -> str:
    """The default multimodal embedding model callable from ``region``."""
    return regional_default(DEFAULT_EMBEDDING_MODEL_ID, region)


def default_analysis_model_id(region: Optional[str] = None) -> str:
    """The default cluster-analysis model callable from ``region``."""
    return regional_default(DEFAULT_ANALYSIS_MODEL_ID, region)
