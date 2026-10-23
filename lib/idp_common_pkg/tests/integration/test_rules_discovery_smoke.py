# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Smoke test: run RulesDiscovery with the shipped defaults in
base-rule-discovery.yaml against the medicare_respiratory_pa_packet.pdf
sample and assert a real model's extracted rules reach the rule-validation
runtime intact.

This test is marked 'integration' because it actually calls Bedrock. Skip
it in CI unless AWS_REGION and Bedrock access are available; the
TestClient sets up its own session.

Where the assertions live: the config loading and the consumability check are in
``tests.unit.discovery.rule_discovery_contract``, shared with
``tests/unit/discovery/test_rule_discovery_shipped_defaults.py``, which runs the
same sequence offline with Bedrock stubbed. The two tiers therefore make the
same claim about the same artifact and differ only in whether the model is real,
so a break in the path itself surfaces in ``make test`` instead of only in the
nightly that holds credentials. What is left here, and only here, is the part
that needs a model: that a real policy PDF yields a useful *number* of real
rules.

That split exists because this test used to be unsatisfiable. It checked the
result of ``discovery_rules_from_document_local`` — the persisted
``policy_classes`` shape, keyed on ``x-aws-idp-policy-type`` — with
``_validate_rules_response``, which validates raw LLM output keyed on
``x-aws-idp-rule-type``. No model output could satisfy that, so the two
"independent reproductions" recorded in
[#1305](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1305)
were reproductions of this assertion rather than of a defect in the defaults. It
also assigned raw YAML values onto an already-constructed model, which bypasses
pydantic's ``mode="before"`` validators and sent ``max_tokens=""`` to Bedrock;
``rule_discovery_contract`` records both.

Run with:
    pytest -m integration lib/idp_common_pkg/tests/integration/test_rules_discovery_smoke.py -s
"""

# The sentinel credentials ``tests/conftest.py`` sets for the unit suite
# (``AWS_ACCESS_KEY_ID=testing`` and friends) have to be out of the way before this
# test signs a real Bedrock call. That is done by the autouse
# ``real_aws_credentials_for_integration_tests`` fixture in ``tests/conftest.py``,
# which swaps in real credentials for the duration of any ``integration``-marked
# test and puts the sentinels back afterwards. It lives in the TOP-LEVEL conftest
# and keys on the marker rather than on this directory, because two
# integration-marked tests live under ``tests/unit/`` and a directory-scoped
# version never reached them (#1307).
#
# It must NOT be done here, at module scope. Every unit run collects this file —
# ``-m "not integration"`` deselects tests *after* collection has imported the
# module — and ``tests/integration`` sorts first, so an import-time deletion took
# the credentials away from the entire remaining session. A boto3 client freezes
# the session's credentials object at construction, so on a runner with no shared
# credentials file and no reachable instance metadata service, every client any
# later test module built at import time was frozen with ``credentials=None`` and
# could never sign. Five tests in ``tests/unit/test_test_set_resolver.py`` failed
# that way, with ``AttributeError: 'NoneType' object has no attribute
# 'access_key'`` out of botocore's presigner, while passing on any developer
# machine — which still has one of those two sources. See #988.

import os
from pathlib import Path

import pytest

from idp_common.discovery.rules_discovery import RulesDiscovery
from tests.unit.discovery.rule_discovery_contract import (
    assert_discovered_rules_are_runtime_consumable,
    load_shipped_rule_discovery_config,
)

REPO_ROOT = Path(__file__).resolve().parents[3].parent
SAMPLE_PDF = (
    REPO_ROOT / "samples" / "rule-validation" / "medicare_respiratory_pa_packet.pdf"
)


@pytest.mark.integration
def test_rules_discovery_with_defaults_against_medicare_sample():
    """Confirm base-rule-discovery.yaml defaults produce valid rules for a real
    medicare PA packet sample."""
    assert SAMPLE_PDF.exists(), f"Sample PDF missing: {SAMPLE_PDF}"
    cfg = load_shipped_rule_discovery_config()

    # Bypass DynamoDB config load by passing config directly. input_bucket /
    # input_prefix are required by the constructor but unused by the
    # _local path; use placeholder values.
    discovery = RulesDiscovery(
        input_bucket="placeholder",
        input_prefix=SAMPLE_PDF.name,
        region=os.environ.get("AWS_REGION", "us-east-1"),
        config=cfg,
    )

    result = discovery.discovery_rules_from_document_local(str(SAMPLE_PDF))

    assert result["status"] == "SUCCESS", f"Unexpected status: {result.get('status')}"
    rules = result["rules"]

    # Same contract the offline tier asserts: every discovered rule must survive
    # into what RuleValidationService would actually evaluate.
    total_rules = assert_discovered_rules_are_runtime_consumable(rules, cfg)

    # The model-dependent half, which is why this tier exists. Medicare policy
    # manuals typically yield dozens of rules; anything under 10 suggests the
    # prompt or model defaults regressed.
    print(
        f"\nSmoke test result: {len(rules)} rule class(es), {total_rules} total rules"
    )
    assert total_rules >= 10, (
        f"Only {total_rules} rules extracted from medicare PA packet; "
        "the default prompt/model may have regressed."
    )
