# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The fine-tuning projection's key set is pinned, because the policy rests on it.

`listFinetuningJobs` and `getFinetuningJob` are declared `groups: ANY` in
`scripts/api_rbac_expectations.yaml` — any authenticated caller, including one in no
group, which self-service sign-up produces. The recorded reason is that the response
is platform state and carries no document location. That is true of the projection
below and false of the DynamoDB row it reads from: `finetuning_data_generator` and
`finetuning_merge_data` both write `trainingDataUri` and `validationDataUri` onto the
same item, and neither is projected. The three `*DataConfig` keys that *are*
projected are Bedrock `create_model_customization_job` request parameters and are
never written to DynamoDB under those names, so they resolve to `None`.

So the whole of the `ANY` justification is a property of one dict literal, and a
caller cannot widen it — the projection is server-side and takes no field selection,
since the dispatcher returns the resolver's dict verbatim. One added line in
`_format_job_for_graphql` would turn an `ANY` operation into a dataset-location
disclosure with nothing to notice. This test is the thing that notices: it pins the
key set and names the two attributes that must stay unprojected while the policy is
`ANY`.

If you are adding a field deliberately, add it to `EXPECTED_KEYS` *and* re-read the
`listFinetuningJobs` note in the expectations file. If the field names a location in
S3, the policy is the thing to change, not this list.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TRACKING_TABLE", "IDP-TrackingTable")

pytestmark = pytest.mark.unit


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "finetuning_jobs_index_keys", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["finetuning_jobs_index_keys"] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()

# Every key `_format_job_for_graphql` returns today. Job, model, status, deployment
# and metrics state — no object key, no bucket, no `s3://` URI.
EXPECTED_KEYS = {
    "jobId",
    "jobName",
    "testSetId",
    "testSetName",
    "baseModelId",
    "customModelName",
    "customModelArn",
    "customModelDeploymentArn",
    "status",
    "createdAt",
    "updatedAt",
    "completedAt",
    "errorMessage",
    "trainingMetrics",
    "hyperparameters",
    "trainingDataConfig",
    "validationDataConfig",
    "outputDataConfig",
    "deploymentId",
    "deploymentStatus",
    "deploymentEndpoint",
    "provisionedModelArn",
}

# Written onto the job row by the data generator and the merge step, and not
# projected. These are the dataset locations the `ANY` policy assumes are absent.
UNPROJECTED_DATASET_ATTRIBUTES = ("trainingDataUri", "validationDataUri")

_REPO = Path(__file__).resolve().parents[5]


def _row_with_everything() -> dict:
    """A job row carrying every attribute either writer can put on it."""
    row = dict.fromkeys(EXPECTED_KEYS, "x")
    row["id"] = "job-1"
    row["baseModel"] = "amazon.nova-lite-v1:0:300k"
    row["trainingMetrics"] = {"loss": 1}
    for attr in UNPROJECTED_DATASET_ATTRIBUTES:
        row[attr] = f"s3://output-bucket/finetuning/job-1/{attr}.jsonl"
    return row


def test_projection_returns_exactly_the_pinned_key_set():
    got = set(index._format_job_for_graphql(_row_with_everything()))
    assert got == EXPECTED_KEYS, (
        "the fine-tuning projection's key set changed. Both operations reading it "
        "are declared `groups: ANY`, and that declaration rests on this dict "
        f"carrying no document location.\n  added: {sorted(got - EXPECTED_KEYS)}\n"
        f"  removed: {sorted(EXPECTED_KEYS - got)}\n"
        "Update EXPECTED_KEYS and re-read the listFinetuningJobs note in "
        "scripts/api_rbac_expectations.yaml before doing so."
    )


# Keys a Bedrock payload puts a dict in AND that reach the response still a container,
# so descending into them is what protects them. All three `*DataConfig` slots are here,
# not just the two input ones: `outputDataConfig` is `{"s3Uri": ...}` in the
# `CreateModelCustomizationJob` request, i.e. an S3 location by definition, so naming a
# subset would leave out the member that most needs descending into.
CONTAINER_VALUED_KEYS = (
    "hyperparameters",
    "trainingDataConfig",
    "validationDataConfig",
    "outputDataConfig",
)

# `trainingMetrics` is a dict on the row and is deliberately NOT above:
# `_format_job_for_graphql` runs `json.dumps` over it before projecting, so what reaches
# the response is a **string** and a nested URI inside it is already visible to a
# top-level string test. Its case below asserts that serialisation rather than the
# descent, because a case that passes whether or not the descent exists proves nothing
# about the descent — and if the `json.dumps` is ever dropped, the key belongs in
# `CONTAINER_VALUED_KEYS` and this is the only thing that would say so.
SERIALISED_KEYS = ("trainingMetrics",)


def _top_level_strings(value: object) -> list[str]:
    """The regression `_strings_under` must beat: a top-level-only string check.

    Used by the cases below to prove the descent is load-bearing for a key, rather
    than asserting that it is.
    """
    return [value] if isinstance(value, str) else []


def _strings_under(value: object) -> list[str]:
    """Every string anywhere inside a projected value.

    A top-level ``isinstance(v, str)`` test reads straight past every key in
    ``CONTAINER_VALUED_KEYS``, which would have made this check pass on exactly the
    shape it exists to refuse — a dataset location one level down.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings_under(v)] + [
            k for k in value if isinstance(k, str)
        ]
    if isinstance(value, (list, tuple, set)):
        return [s for v in value for s in _strings_under(v)]
    return []


def test_no_projected_value_is_an_s3_location():
    """The row carries `s3://` URIs; none may reach the response, at any depth."""
    out = index._format_job_for_graphql(_row_with_everything())
    leaked = sorted(
        k for k, v in out.items() if any("s3://" in s for s in _strings_under(v))
    )
    assert not leaked, (
        f"{leaked} carried an s3:// URI out of a `groups: ANY` operation. The "
        "dataset location is on the job row and must stay unprojected while any "
        "authenticated caller — including one in no group — may call these two."
    )


@pytest.mark.parametrize("key", CONTAINER_VALUED_KEYS)
def test_the_descent_is_load_bearing_for_each_container_valued_key(key: str):
    """Each case must FAIL under the regression it exists to catch, not merely pass.

    Parametrised because "the descent protects this key" is a claim about each key
    individually. Asserting only that `_strings_under` finds the URI is not enough:
    a key the projection serialises to a string is found by a top-level check too, so
    its case would keep passing with the descent deleted. Every key here is therefore
    required to reach the response as a container and to be **invisible** to
    `_top_level_strings`.
    """
    row = _row_with_everything()
    row[key] = {"s3Uri": f"s3://output-bucket/finetuning/{key}.jsonl"}
    out = index._format_job_for_graphql(row)
    assert key in out, (
        f"{key} is named in CONTAINER_VALUED_KEYS but the projection no longer returns "
        "it; drop it from that tuple and from EXPECTED_KEYS in the same change"
    )
    assert not isinstance(out[key], str), (
        f"the projection now returns {key} as a string, so the descent is not what "
        "protects it. Move it to SERIALISED_KEYS, whose case asserts the "
        "serialisation instead."
    )
    assert not any("s3://" in s for s in _top_level_strings(out[key])), (
        f"{key}'s URI is visible to a top-level string check, so this case would pass "
        "with the descent deleted and proves nothing about it"
    )
    assert any("s3://" in s for s in _strings_under(out[key])), (
        f"_strings_under no longer reaches a nested value under {key}, so a dataset "
        "location one level down would pass the check above unnoticed"
    )


@pytest.mark.parametrize("key", SERIALISED_KEYS)
def test_a_serialised_key_reaches_the_response_as_a_string(key: str):
    """The serialisation is the control for these keys, so assert the serialisation.

    If `_format_job_for_graphql` stops JSON-encoding this value, a nested URI under it
    is protected by the descent rather than by a plain string test — which is a
    different claim, checked by a different case. Failing here is the instruction to
    move the key into `CONTAINER_VALUED_KEYS`.
    """
    row = _row_with_everything()
    row[key] = {"s3Uri": f"s3://output-bucket/finetuning/{key}.jsonl"}
    out = index._format_job_for_graphql(row)
    assert isinstance(out[key], str), (
        f"{key} no longer reaches the response as a JSON string. Move it to "
        "CONTAINER_VALUED_KEYS so the descent is what is proven for it."
    )
    assert any("s3://" in s for s in _top_level_strings(out[key])), (
        f"a URI inside {key} is no longer visible in the serialised value, so neither "
        "this case nor the descent case covers it"
    )


def test_the_policy_this_pin_protects_is_still_any():
    """If the policy is tightened, this pin is no longer load-bearing.

    A pin whose premise has moved is worse than no pin: it keeps asserting a
    constraint for a reason that has gone, and the next reader trusts the reason.
    """
    spec = yaml.safe_load(
        (_REPO / "scripts" / "api_rbac_expectations.yaml").read_text(encoding="utf-8")
    )
    ops = spec["operations"]
    policies = {
        name: ops[name]["groups"] for name in ("listFinetuningJobs", "getFinetuningJob")
    }
    assert set(policies.values()) == {"ANY"}, (
        "the fine-tuning reads are no longer both `ANY` "
        f"({policies}). This module's whole premise is that an unvetted caller may "
        "call them, so revisit its docstring — and the notes in the expectations "
        "file — rather than leaving a stale justification in place."
    )
