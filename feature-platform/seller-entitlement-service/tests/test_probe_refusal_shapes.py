# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The live probe must tell the service's 403 from API Gateway's.

Issue #889: run in any region other than the probe's default, every SigV4 request
was signed for the wrong region and API Gateway answered 403 before the Lambda
ran. Three refusal assertions expected a 403 and accepted that one, so the probe
passed on requests that never reached the code under test. These tests pin the
classifier those assertions now use.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dynamic_activation_test import (  # noqa: E402
    is_edge_refusal,
    is_service_refusal,
)

SERVICE_BODY = json.dumps(
    {
        "error": "not_entitled",
        "detail": "No active AWS Marketplace subscription for this account.",
    }
)


@pytest.mark.unit
def test_the_service_body_is_a_service_refusal():
    assert is_service_refusal(SERVICE_BODY)
    assert not is_edge_refusal(SERVICE_BODY)


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        '{"message":"Credential should be scoped to a valid region. "}',
        '{"message":"Missing Authentication Token"}',
        '{"message":"The security token included in the request is invalid."}',
        '{"message":"Forbidden"}',
        '{"message":"Signature expired: 20260911T230000Z is now earlier than ..."}',
    ],
)
def test_api_gateway_bodies_are_edge_refusals_not_service_ones(body):
    assert is_edge_refusal(body)
    assert not is_service_refusal(body)


@pytest.mark.unit
@pytest.mark.parametrize("body", ["", "0", "not json", "[]", '{"error":"other"}'])
def test_anything_else_is_neither(body):
    assert not is_service_refusal(body)
    # Only the specific API Gateway markers count as an edge refusal.
    assert not is_edge_refusal(body)


# --------------------------------------------------------------------------- #
# A probe that did not complete is not a probe that passed
#
# `_post` answers `(0, "connection error: ...")` when the request never reached
# the endpoint. Every check in dynamic_activation_test routes status 0 into
# `bad()` — except `check_oversized_body`, which fell through to `warn()`, and
# `warn()` does not append to `_failures`. So an unreachable stage reported the
# oversized-body refusal as nothing to worry about.
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_an_unreachable_endpoint_fails_the_oversized_body_check(monkeypatch):
    import dynamic_activation_test as dyn

    monkeypatch.setattr(dyn, "_post", lambda *a, **k: (0, "connection error: reset"))
    monkeypatch.setattr(dyn, "_failures", [])

    dyn.check_oversized_body("https://x.invalid/activate", "p", None, "us-east-1")

    assert dyn._failures, "an unreachable endpoint left the probe green"
    assert "did not complete" in dyn._failures[0]


@pytest.mark.unit
def test_a_real_413_still_passes(monkeypatch):
    """The control: without it, "always fails" would satisfy the test above."""
    import dynamic_activation_test as dyn

    monkeypatch.setattr(dyn, "_post", lambda *a, **k: (413, ""))
    monkeypatch.setattr(dyn, "_failures", [])

    dyn.check_oversized_body("https://x.invalid/activate", "p", None, "us-east-1")

    assert dyn._failures == []
