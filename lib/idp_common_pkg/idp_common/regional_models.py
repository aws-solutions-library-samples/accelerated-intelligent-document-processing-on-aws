# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Code-level model fallbacks, translated to the Region family that calls them.

A ``us.`` inference profile is rejected as an invalid model identifier from an EU
Region, and some are from Canada. The deploy-time swap in
``src/lambda/update_configuration`` rewrites stored configuration, but a fallback
written in code is reached without it: by the SDK and CLI's local pipelines, which
never run the swap, and by deployed handlers when their configuration is empty or
cannot be read. A fallback names its US model and passes it through
:func:`regional_default`, which returns what the swap would have produced.

The rows must be what the swap produces for the same id, and every id a caller
passes must be in :data:`FALLBACK_MODEL_IDS`:
``src/lambda/update_configuration/test_eu_region.py`` asserts the first against
the swap tables for every id in that set, and
``lib/idp_common_pkg/tests/unit/test_geographic_fallbacks.py`` asserts the second
by reading every call in the tree.

Standard library only, so a Lambda carrying just the base layer can import it.
"""

from __future__ import annotations

import os
from typing import Optional

#: Multi-document discovery's embedding model.
EMBEDDING_MODEL_ID = "us.cohere.embed-v4:0"
#: Multi-document discovery's cluster analysis, synthesis schema authoring and
#: catalog matching.
SONNET_4_6_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
#: The SDK's local discovery pipeline, when the shipped defaults name no model.
NOVA_PRO_MODEL_ID = "us.amazon.nova-pro-v1:0"
#: Chat with Document, when neither the chat nor the summarization section names
#: a model.
CHAT_MODEL_ID = "us.anthropic.claude-opus-4-8:1m"

#: Every US id a code fallback passes to :func:`regional_default`.
FALLBACK_MODEL_IDS = frozenset(
    {EMBEDDING_MODEL_ID, SONNET_4_6_MODEL_ID, NOVA_PRO_MODEL_ID, CHAT_MODEL_ID}
)

#: Region-name prefix -> {US id: the id callable from that Region}. An id absent
#: from a family's row is callable there as it is, which is the swap's own claim:
#: an id absent from ``CA_UNAVAILABLE_MODELS`` is offered in a Canada stack.
REGIONAL_DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "eu-": {
        EMBEDDING_MODEL_ID: "eu.cohere.embed-v4:0",
        SONNET_4_6_MODEL_ID: "eu.anthropic.claude-sonnet-4-6",
        NOVA_PRO_MODEL_ID: "eu.amazon.nova-pro-v1:0",
        CHAT_MODEL_ID: "eu.anthropic.claude-opus-4-8:1m",
    },
    # Cohere Embed v4 is reachable from Canada only through its global. profile.
    # Nova Pro has no profile there, global. included, so the swap moves it to
    # Nova 2 Lite. The US Sonnet 4.6 and Opus 4.8 profiles are callable from
    # ca-central-1, so they stay.
    "ca-": {
        EMBEDDING_MODEL_ID: "global.cohere.embed-v4:0",
        NOVA_PRO_MODEL_ID: "us.amazon.nova-2-lite-v1:0",
    },
}


def regional_default(model_id: str, region: Optional[str] = None) -> str:
    """``model_id``, or its equivalent callable from ``region``.

    ``region`` defaults to ``AWS_REGION``. A Region outside the families above
    gets ``model_id`` unchanged, as the deploy-time swap leaves it.
    """
    region = region or os.environ.get("AWS_REGION", "")
    for prefix, row in REGIONAL_DEFAULT_MODELS.items():
        if region.startswith(prefix):
            return row.get(model_id, model_id)
    return model_id
