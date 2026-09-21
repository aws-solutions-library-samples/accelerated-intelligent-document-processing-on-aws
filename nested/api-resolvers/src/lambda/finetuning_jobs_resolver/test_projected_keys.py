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


def test_no_projected_value_is_an_s3_location():
    """The row carries `s3://` URIs; none of them may reach the response."""
    out = index._format_job_for_graphql(_row_with_everything())
    leaked = sorted(k for k, v in out.items() if isinstance(v, str) and "s3://" in v)
    assert not leaked, (
        f"{leaked} carried an s3:// URI out of a `groups: ANY` operation. The "
        "dataset location is on the job row and must stay unprojected while any "
        "authenticated caller — including one in no group — may call these two."
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
