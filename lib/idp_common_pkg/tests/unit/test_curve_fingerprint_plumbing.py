# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#698: the confidence fingerprint is stamped on the run item by the test runner
and copied onto draft labels by the harvest — the two records the three curve
call sites read it from."""

import importlib.util
import json
import os
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_RUNNER = os.path.join(
    os.path.dirname(__file__),
    "../../../../nested/api-resolvers/src/lambda/test_runner/index.py",
)


@pytest.fixture(scope="module")
def runner():
    with patch.dict(
        os.environ,
        {
            "TRACKING_TABLE": "t",
            "CONFIG_TABLE": "c",
            "FILE_COPY_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/1/q",
            "AWS_REGION": "us-east-1",
        },
    ):
        with patch("boto3.client"), patch("boto3.resource"):
            spec = importlib.util.spec_from_file_location(
                "runner_fp_under_test", _RUNNER
            )
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            yield mod


CONFIG_A = {
    "extraction": {"model": "us.anthropic.claude-sonnet-4-6", "temperature": 0.0},
    "assessment": {"model": "us.amazon.nova-lite-v1:0"},
    "classification": {"task_prompt": "v1"},
}


def test_runner_stamps_the_fingerprint_of_the_captured_config(runner):
    from idp_common.config.revisions import confidence_fingerprint

    class Table:
        def __init__(self):
            self.items = []

        def put_item(self, Item, ConditionExpression=None):  # noqa: N803 — boto3 kwarg names
            # The runner reserves the run id with a conditional write (#879);
            # the fake only records the item.
            assert ConditionExpression == "attribute_not_exists(PK)"
            self.items.append(Item)

    table = Table()
    # The writer takes a table NAME and resolves it through the module resource.
    with patch.object(runner.dynamodb, "Table", lambda _name: table):
        runner._store_test_run_metadata(
            "tracking",
            "run-1",
            "ts1",
            "Set",
            {"Config": CONFIG_A},
            [],
            None,
            0,
            config_version="prof-A",
        )
    item = table.items[0]
    assert item["ConfidenceFingerprint"] == confidence_fingerprint(CONFIG_A)
    assert item["ConfigVersion"] == "prof-A"


def test_prompt_only_edits_keep_the_fingerprint_a_model_swap_changes_it(runner):
    fp_a = runner._confidence_fingerprint_of({"Config": CONFIG_A})
    prompt_edit = json.loads(json.dumps(CONFIG_A))
    prompt_edit["classification"]["task_prompt"] = "v2"
    swap = json.loads(json.dumps(CONFIG_A))
    swap["extraction"]["model"] = "us.anthropic.claude-opus-4-8"
    assert runner._confidence_fingerprint_of({"Config": prompt_edit}) == fp_a
    assert runner._confidence_fingerprint_of({"Config": swap}) != fp_a


def test_missing_or_broken_config_yields_no_fingerprint_and_no_failure(runner):
    assert runner._confidence_fingerprint_of(None) is None
    assert runner._confidence_fingerprint_of({"Config": {}}) is None
    assert runner._confidence_fingerprint_of({"Config": "not a dict"}) is None


def test_decimalised_body_hashes_like_the_float_body(runner):
    """The runner stores a Decimal-ised revision body; the curve key must not
    depend on which numeric type the body arrived with (#758 normalization)."""
    from decimal import Decimal

    from idp_common.config.revisions import confidence_fingerprint

    dec_body = json.loads(json.dumps(CONFIG_A), parse_float=Decimal)
    assert confidence_fingerprint(dec_body) == confidence_fingerprint(CONFIG_A)
    assert runner._confidence_fingerprint_of(
        {"Config": dec_body}
    ) == confidence_fingerprint(CONFIG_A)
