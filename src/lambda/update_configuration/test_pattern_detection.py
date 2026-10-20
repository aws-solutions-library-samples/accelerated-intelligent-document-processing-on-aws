# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`detect_pattern_from_config` decides which system defaults a stored
configuration is merged against, so getting it wrong deploys a configuration the
operator never wrote: a pipeline config merged onto the BDA defaults gains a
`bda` section and loses the OCR and classification settings that do apply.

⚠️ **The flag has to be read through `flag_is_true`.** YAML, DynamoDB and the UI
can all deliver a boolean as the *string* `"false"`, which plain truthiness reads
as True — and that is the direction that hurts, because it sends a pipeline
configuration to the BDA defaults. `"yes please"` covers the other half of the
rule: an unrecognised string is not truthy either, so a typo cannot opt a
deployment into the wrong pattern.

Detection reads `use_bda`, the flag the pipeline itself branches on. Until config
format 0.8 it sniffed `classification.classificationMethod == "bda"` — a third
value that key never legitimately held (its enum was
multimodalPageLevel/textbasedHolistic), which nothing in the product wrote.
"""

from typing import Any

import pytest

# The handler, already loaded with ``cfnresponse`` stubbed. pytest puts this
# directory on ``sys.path`` before importing either module, so the sibling import
# is collection-order independent — the same pattern ``test_canada_region.py``
# uses, and the reason there is one stubbing site here rather than three.
from test_model_mappings import index as _index


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "pattern-1"),
        (False, "pattern-2"),
        ("true", "pattern-1"),
        ("True", "pattern-1"),
        # ⚠️ The case plain truthiness gets wrong, in the direction that hurts.
        ("false", "pattern-2"),
        ("False", "pattern-2"),
        ("yes please", "pattern-2"),
        (1, "pattern-1"),
        (0, "pattern-2"),
    ],
)
def test_the_pattern_follows_use_bda_however_the_flag_is_spelled(
    value: Any, expected: str
):
    assert _index.detect_pattern_from_config({"use_bda": value}) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "config",
    [
        {},
        {"classification": {"granularity": "page"}},
        {"classification": {"granularity": "packet"}},
        # The removed key, in case a configuration reaches this function without
        # having been migrated: it must not resurrect the old sniff.
        {"classification": {"classificationMethod": "bda"}},
    ],
)
def test_a_configuration_with_no_use_bda_is_the_pipeline_pattern(config: dict):
    assert _index.detect_pattern_from_config(config) == "pattern-2"
