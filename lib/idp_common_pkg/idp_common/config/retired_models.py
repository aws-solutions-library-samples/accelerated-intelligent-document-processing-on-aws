# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bedrock models that have reached end of life, in one place.

A model past its AWS end-of-life date is **completely inaccessible in every
region** — every call returns
``ResourceNotFoundException: This model version has reached the end of its life``.
That is different from ``LEGACY``, where existing users can still invoke the model
and it correctly stays selectable. Only the first class belongs here.

This lives in shipped code rather than in a test because three consumers need the
same answer and used to disagree:

* ``config.merge_utils.validate_config`` — so ``idp-cli config-validate`` and
  ``config-upload --validate`` reject a configuration that pins a dead model
  *before* a document fails two stages in.
* ``scripts/tests/test_model_surface_consistency.py`` — so no selectable surface,
  code default or shipped preset can offer one.
* ``scripts/sdlc/tests/test_retired_models_not_offered.py`` — the original gate
  from #708, which additionally covers the Converse cachePoint allowlist and the
  UI's SchemaInspector list.

Keeping two registries was the alternative and it had already produced a
contradiction. The #708 gate asserted a retired model must be **absent** from
``config_library/pricing.yaml``, because ``validate_config`` derived its set of
valid model ids from that file and the absence is what made validation fail. But a
pricing entry is read **retrospectively**: a cost report over documents processed
while the model was still selectable resolves its rate by model id, so deleting
the row silently re-prices historical runs at zero. Both goals are legitimate and
they cannot both be met by the presence or absence of a pricing row.

``is_retired`` resolves that: the pricing entry stays, and validation rejects the
model because it is *known to be retired* rather than because a lookup table
happens not to mention it. That also removes a fragile coupling — the old
behaviour depended on a side effect of an unrelated file.

Every entry records the date and the exact command that establishes it. Re-run the
command to re-verify; nothing here can be derived from this repository.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

#: Region / geo prefixes used by Bedrock cross-region inference profiles.
_REGION_PREFIX = re.compile(r"^(?:us|eu|apac|global|us-gov)\.")


#: Model id -> facts about its retirement.
#:
#: ``was_offered`` records whether THIS repository ever made the model
#: selectable. It decides one thing only: whether a ``pricing.yaml`` entry must be
#: retained. A model this solution never offered cannot appear in anyone's
#: historical cost report, so it needs no rate — and inventing one would breach
#: the "never invent model facts" rule in ``.claude/skills/add-model.md``.
RETIRED_MODELS: Dict[str, Dict[str, Any]] = {
    "us.amazon.nova-premier-v1:0": {
        "was_offered": True,
        "eol": "2026-09-14",
        "verify": (
            "aws bedrock get-foundation-model --region us-east-1 "
            "--model-identifier amazon.nova-premier-v1:0"
        ),
        "note": "date from the Bedrock model card for Nova Premier",
    },
    "us.anthropic.claude-3-5-sonnet-20240620-v1:0": {
        "was_offered": True,
        "eol": "2026-09-20",
        "verify": (
            "aws bedrock get-foundation-model --region us-east-1 "
            "--model-identifier anthropic.claude-3-5-sonnet-20240620-v1:0"
        ),
        "note": (
            "past EOL, so AWS no longer publishes a model card; the date recorded "
            "is when this was confirmed, not necessarily the EOL date itself"
        ),
    },
    # The four below were already selectable when this registry was created, and all
    # four were confirmed dead: GetFoundationModel answers with the end-of-life
    # message in us-east-1 and us-west-2, and none appears in
    # list-foundation-models. Three of them were also MODEL_MAPPINGS *targets*, so
    # an EU deployment was being rewritten onto a dead model.
    "us.anthropic.claude-3-5-sonnet-20241022-v2:0": {
        "was_offered": True,
        "eol": "2026-09-20",
        "verify": (
            "aws bedrock get-foundation-model --region us-east-1 "
            "--model-identifier anthropic.claude-3-5-sonnet-20241022-v2:0"
        ),
        "note": (
            "confirmation date, not the EOL date. NOTE eu-west-1 answers 'Model "
            "not found' rather than the end-of-life message — that region never "
            "offered it, which is a weaker signal; the us-east-1 and us-west-2 "
            "answers are the evidence"
        ),
    },
    "us.anthropic.claude-3-7-sonnet-20250219-v1:0": {
        "was_offered": True,
        "eol": "2026-09-20",
        "verify": (
            "aws bedrock get-foundation-model --region us-east-1 "
            "--model-identifier anthropic.claude-3-7-sonnet-20250219-v1:0"
        ),
        "note": "confirmation date, not the EOL date; EOL in all three regions checked",
    },
    "us.anthropic.claude-3-haiku-20240307-v1:0": {
        "was_offered": True,
        "eol": "2026-09-20",
        "verify": (
            "aws bedrock get-foundation-model --region us-east-1 "
            "--model-identifier anthropic.claude-3-haiku-20240307-v1:0"
        ),
        "note": (
            "confirmation date, not the EOL date. Was the default for LLM-based "
            "evaluation, so evaluation ran a dead model out of the box"
        ),
    },
    "us.anthropic.claude-opus-4-20250514-v1:0": {
        "was_offered": True,
        "eol": "2026-09-20",
        "verify": (
            "aws bedrock get-foundation-model --region us-east-1 "
            "--model-identifier anthropic.claude-opus-4-20250514-v1:0"
        ),
        "note": "confirmation date, not the EOL date; EOL in all three regions checked",
    },
    "us.anthropic.claude-3-5-haiku-20241022-v1:0": {
        "was_offered": False,
        "eol": "2026-08-29",
        "verify": (
            "aws bedrock get-foundation-model --region us-east-1 "
            "--model-identifier anthropic.claude-3-5-haiku-20241022-v1:0"
        ),
        "note": (
            "removed from every surface for #708; it had no pricing entry, so "
            "there is no rate to retain and none may be invented"
        ),
    },
}


def base_model_id(model_id: str) -> str:
    """Strip a region/geo prefix, leaving the foundation-model id.

    End of life is a property of the FOUNDATION MODEL. When
    ``amazon.nova-premier-v1:0`` was withdrawn, the ``us.``, ``eu.`` and
    ``global.`` inference profiles routing to it died with it, and the bare form is
    the one GovCloud uses. Comparing by exact string therefore recognises only
    whichever spelling happens to be listed above.
    """
    return _REGION_PREFIX.sub("", model_id or "")


#: base model id -> the listed id, so any regional variant resolves.
_RETIRED_BASES = {base_model_id(m): m for m in RETIRED_MODELS}


def retirement_of(model_id: str) -> Optional[Dict[str, Any]]:
    """The retirement facts for ``model_id``, or ``None`` if it is not retired.

    Matches any region/geo variant of a retired foundation model.
    """
    listed = _RETIRED_BASES.get(base_model_id(model_id))
    return RETIRED_MODELS[listed] if listed else None


def is_retired(model_id: str) -> bool:
    """True if ``model_id`` names a model that has reached end of life."""
    return retirement_of(model_id) is not None
