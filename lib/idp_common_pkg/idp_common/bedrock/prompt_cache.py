# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Prompt-cache facts the product needs to reason about, in one place (#780).

A ``<<CACHEPOINT>>`` only creates a cache entry if the prefix before it clears the
model's **minimum cacheable prefix**. Below it Bedrock returns ``cacheWrite = 0`` and
``cacheRead = 0``, raises nothing, and bills the prefix at full input price on every
request. The minimum is model-dependent and NOT monotonic across generations, so
"newer is safer" is false; measured in ``docs/benchmarking/prompt-caching.md``.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Optional

CACHEPOINT_MARKER = "<<CACHEPOINT>>"

# Published per-model minimum cacheable prefix, in tokens. Order matters: the first
# matching pattern wins, so the specific families precede the broad ones. Amazon
# Nova is deliberately absent: its minimum was measured at <=355 tokens, below any
# shipped class, so it never needs a warning.
_MIN_PREFIX_TIERS = (
    (re.compile(r"claude-(opus-5|fable-5)"), 512),
    (re.compile(r"claude-opus-4-7"), 2048),
    (re.compile(r"claude-(opus-4-6|opus-4-5|haiku-4-5)"), 4096),
    # Sonnet 5, Sonnet 4.6, Opus 4.8, Sonnet 4.5, Sonnet 4, Opus 4.1, Opus 4, 3.7 Sonnet
    (
        re.compile(
            r"claude-(sonnet-5|sonnet-4|opus-4-8|opus-4-1|opus-4-2|opus-4|3-7-sonnet)"
        ),
        1024,
    ),
)

# chars/4 was within ~2% of Bedrock's own count on real prompt text
# (benchmarks/harness/cache_prefix_survey.py); rounded up so a borderline class is
# reported as close rather than as safe.
_CHARS_PER_TOKEN = 4.0


def min_cacheable_prefix_tokens(model_id: Optional[str]) -> Optional[int]:
    """The model's minimum cacheable prefix, or None when unknown / not applicable."""
    if not model_id:
        return None
    for pattern, minimum in _MIN_PREFIX_TIERS:
        if pattern.search(model_id):
            return minimum
    return None


def schema_prose(class_schema: Dict[str, Any]) -> str:
    """The class schema as the Simple-mode prompt renders it: cleaned of
    ``x-aws-idp-*`` keys and pretty-printed (mirrors
    ``ExtractionService._format_schema_for_prompt``)."""

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: clean(v)
                for k, v in node.items()
                if not str(k).startswith("x-aws-idp-")
            }
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node

    return json.dumps(clean(class_schema), indent=2)


def estimate_tokens(text: str) -> int:
    return int(math.ceil(len(text) / _CHARS_PER_TOKEN))


def estimate_prefix_tokens(
    system_prompt: str,
    task_prompt: str,
    class_schema: Dict[str, Any],
    class_id: str,
    few_shot_text: str = "",
) -> Optional[int]:
    """Estimated tokens Bedrock counts before the FIRST cache point of a Simple-mode
    extraction request for ``class_schema``: the system prompt plus the task prompt
    up to the marker with the class placeholders substituted. None when the task
    prompt has no marker (nothing would cache either way)."""
    if CACHEPOINT_MARKER not in (task_prompt or ""):
        return None
    head = task_prompt.split(CACHEPOINT_MARKER)[0]
    head = (
        head.replace("{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}", schema_prose(class_schema))
        .replace("{DOCUMENT_CLASS}", class_id)
        .replace("{FEW_SHOT_EXAMPLES}", few_shot_text or "")
    )
    return estimate_tokens((system_prompt or "") + head)
