# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The GovCloud preset, merged with the system defaults, names only GovCloud models.

``config_library/unified/lending-package-sample-govcloud/config.yaml`` is a
partial configuration: every setting it leaves out is filled from
``idp_common/config/system_defaults/``, and those defaults name commercial
cross-region profiles (``us.*``, ``global.*``). The deploy-time swap in
``src/lambda/update_configuration/index.py`` resolves a ``us-gov-*`` region to the
``us`` region type and rewrites nothing for it, so whatever the merge produces is
what a GovCloud stack stores and calls. Reading the preset file alone therefore
answers the wrong question: the file pinned the models for classification and
extraction while the merged configuration still sent OCR (bedrock backend),
schema-validation escalation and both rule-validation stages to commercial ids
(#1529).

So the check runs over the **merged** configuration, and a value is treated as a
model id by three independent routes, any one of which suffices:

* its key ends in ``model`` or ``model_id`` (the suffix rule
  ``config_library/test_config_library.py`` uses, which reaches
  ``escalation_model`` and ``analysis_model_id``);
* it is a model id ``config_library/pricing.yaml`` prices, whatever its key;
* it starts with a cross-region prefix from
  ``idp_common.bedrock.model_utils.REGION_PREFIXES``.

Every such value must be in :data:`GOVCLOUD_VERIFIED_MODELS`, or be the one
residual :data:`GOVCLOUD_UNAVAILABLE_MODEL_EXEMPT` records.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from idp_common.bedrock.model_utils import REGION_PREFIXES
from idp_common.config.merge_utils import merge_config_with_defaults

_CONFIG_LIBRARY = Path(__file__).resolve().parents[5] / "config_library"
_PRESET_PATH = _CONFIG_LIBRARY / "unified/lending-package-sample-govcloud/config.yaml"
_PRICING_PATH = _CONFIG_LIBRARY / "pricing.yaml"

#: The models the preset's header records as confirmed by a real Converse call in
#: us-gov-west-1. Written out rather than derived, because it is a statement about
#: live Bedrock that nothing in this tree can compute; the header says not to add a
#: model without that confirmation, and adding one here is the reviewed act that
#: claims it. ``test_the_verified_list_is_exactly_what_the_preset_pins`` keeps this
#: set and the preset from drifting apart in either direction.
GOVCLOUD_VERIFIED_MODELS = frozenset(
    {
        "amazon.nova-pro-v1:0",
        "amazon.nova-lite-v1:0",
        "us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0",
    }
)

#: Merged settings that still name a model not available in GovCloud, keyed by
#: dotted path, with the exact value excused. One member, and its reason:
#:
#: ``discovery.multi_document.embedding_model_id`` — Multi-Doc Discovery needs a
#: multimodal embedding model and GovCloud offers none (the preset's comment on
#: ``discovery.multi_document`` records what was checked). The setting has no
#: enable flag to turn off, and pinning an id nobody has confirmed would trade a
#: clear failure for a guessed one, so the preset leaves it at the system default
#: and says so. Keyed on path AND value: a different default, or the same id
#: appearing anywhere else, is not covered.
GOVCLOUD_UNAVAILABLE_MODEL_EXEMPT = {
    "discovery.multi_document.embedding_model_id": "us.cohere.embed-v4:0",
}


def _raw_preset() -> dict[str, Any]:
    return yaml.safe_load(_PRESET_PATH.read_text(encoding="utf-8"))


def _merged_preset() -> dict[str, Any]:
    # pattern-2 is what update_configuration resolves for a Pipeline-mode preset
    # (its get_pattern fallback), and this preset sets use_bda: false.
    return merge_config_with_defaults(
        _raw_preset(), pattern="pattern-2", validate=False
    )


def _priced_model_ids() -> frozenset[str]:
    pricing = yaml.safe_load(_PRICING_PATH.read_text(encoding="utf-8"))["pricing"]
    return frozenset(
        entry["name"].removeprefix("bedrock/")
        for entry in pricing
        if entry["name"].startswith("bedrock/")
    )


def _string_leaves(node: Any, path: str = "") -> Iterator[tuple[str, str, str]]:
    """Yield ``(dotted path, key, value)`` for every string in the tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if isinstance(value, str):
                yield here, str(key), value
            else:
                yield from _string_leaves(value, here)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            here = f"{path}[{index}]"
            if isinstance(item, str):
                yield here, "", item
            else:
                yield from _string_leaves(item, here)


def _model_values(config: dict[str, Any]) -> dict[str, str]:
    """``{dotted path: value}`` for every value that is a model id by any route."""
    priced = _priced_model_ids()
    geo_prefixes = tuple(f"{prefix}." for prefix in REGION_PREFIXES)
    found = {}
    for path, key, value in _string_leaves(config):
        value = value.strip()
        if not value:
            continue  # an empty model setting means "fall back", not a model
        if (
            key.endswith("model")
            or key.endswith("model_id")
            or value in priced
            or value.startswith(geo_prefixes)
        ):
            found[path] = value
    return found


@pytest.mark.unit
def test_the_derivations_are_not_empty():
    """Each route has to reach something, or the main assertion holds vacuously."""
    priced = _priced_model_ids()
    assert any(model.startswith("us.") for model in priced), (
        f"{_PRICING_PATH} yielded no commercial us.* model ids; the pricing route "
        "is broken"
    )
    assert "us" in REGION_PREFIXES and "us-gov" in REGION_PREFIXES

    merged_models = _model_values(_merged_preset())
    raw_models = _model_values(_raw_preset())
    assert raw_models, f"no model values found in {_PRESET_PATH}"
    # The merge has to contribute model settings the preset does not name, or this
    # file is not testing the inheritance it exists for.
    inherited = set(merged_models) - set(raw_models)
    assert inherited, (
        "every model setting in the merged configuration is also in the raw preset, "
        "so the merge with system defaults is not being exercised"
    )


@pytest.mark.unit
def test_every_merged_model_value_is_govcloud_verified():
    offenders = {
        path: value
        for path, value in sorted(_model_values(_merged_preset()).items())
        if value not in GOVCLOUD_VERIFIED_MODELS
        and GOVCLOUD_UNAVAILABLE_MODEL_EXEMPT.get(path) != value
    }
    assert not offenders, (
        f"After merging {_PRESET_PATH.relative_to(_CONFIG_LIBRARY.parent)} with the "
        f"system defaults, these settings name a model outside the GovCloud-verified "
        f"list {sorted(GOVCLOUD_VERIFIED_MODELS)}: {offenders}. A setting the preset "
        f"omits inherits the system default, and nothing rewrites it for a GovCloud "
        f"region. Pin a verified model for it in the preset. Do not add a model to "
        f"the verified list without a successful Converse call in us-gov-west-1."
    )


@pytest.mark.unit
@pytest.mark.parametrize("path", sorted(GOVCLOUD_UNAVAILABLE_MODEL_EXEMPT))
def test_each_exempt_value_is_still_needed(path):
    """A dead entry is stale: it would excuse whatever next lands at that path."""
    merged = _model_values(_merged_preset())
    excused = GOVCLOUD_UNAVAILABLE_MODEL_EXEMPT[path]
    assert merged.get(path) == excused, (
        f"{path} is excused for {excused!r} but the merged GovCloud preset now holds "
        f"{merged.get(path)!r}. The entry no longer describes the configuration; "
        f"delete it (and the matching note in the preset) or re-read its reason "
        f"against the new value."
    )
    assert excused not in GOVCLOUD_VERIFIED_MODELS


@pytest.mark.unit
def test_the_verified_list_is_exactly_what_the_preset_pins():
    pinned = set(_model_values(_raw_preset()).values())
    assert pinned == set(GOVCLOUD_VERIFIED_MODELS), (
        f"The preset pins {sorted(pinned)}; GOVCLOUD_VERIFIED_MODELS lists "
        f"{sorted(GOVCLOUD_VERIFIED_MODELS)}. A model pinned in the preset but not "
        f"listed here has not been recorded as verified; a model listed here but "
        f"no longer pinned should be removed so the list stays a statement about "
        f"what ships."
    )
