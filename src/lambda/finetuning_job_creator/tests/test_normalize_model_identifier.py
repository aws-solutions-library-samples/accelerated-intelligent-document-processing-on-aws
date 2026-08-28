# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for `_normalize_model_identifier`'s ARN passthrough.

The guard used to be `model_id.startswith("arn:aws:bedrock:")`, so a GovCloud ARN
was not recognised as an ARN and fell through to the normalization rules below.

⚠️ Measured, not assumed: for a GovCloud ARN the old and new code return the
SAME string, because none of those downstream rules match an ARN either. So this
was a latent hazard (a guard that did not say what it meant), NOT an observable
defect — and these tests pin the invariant rather than claiming to catch a
regression. Their value is forward-looking: a future normalization rule that does
match ARN-shaped input would break them, in every partition.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from index import _normalize_model_identifier  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:bedrock:us-east-1:111122223333:custom-model/x",
        "arn:aws-us-gov:bedrock:us-gov-west-1:111122223333:custom-model/x",
        "arn:aws-cn:bedrock:cn-north-1:111122223333:custom-model/x",
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0:300k",
    ],
)
def test_arns_pass_through_unchanged_in_every_partition(arn):
    assert _normalize_model_identifier(arn) == arn


def test_govcloud_arn_is_not_mangled_as_a_bare_model_id():
    """A GovCloud ARN must never be rewritten as if it were a bare model id.

    Held before this change too (see the module docstring) — pinned so it keeps
    holding if the normalization rules grow.
    """
    arn = "arn:aws-us-gov:bedrock:us-gov-west-1:111122223333:custom-model/x"
    result = _normalize_model_identifier(arn)
    assert result.startswith("arn:aws-us-gov:"), result
    assert ":300k" not in result, "fine-tuning suffix wrongly appended to an ARN"


def test_a_non_bedrock_arn_gets_no_finetuning_suffix():
    """The guard requires ':bedrock:', so an unrelated ARN is not a model ARN.

    It still passes through unchanged because no normalization rule matches it —
    asserted exactly rather than loosely, so a future rule that starts rewriting
    arbitrary strings is caught.
    """
    other = "arn:aws:s3:::some-bucket/key"
    assert _normalize_model_identifier(other) == other


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("us.amazon.nova-lite-v1:0", "amazon.nova-lite-v1:0:300k"),
        ("amazon.nova-lite-v1:0", "amazon.nova-lite-v1:0:300k"),
    ],
)
def test_bare_model_ids_are_still_normalized(model_id, expected):
    """Pre-existing behaviour the ARN-guard change must not have disturbed."""
    assert _normalize_model_identifier(model_id) == expected
