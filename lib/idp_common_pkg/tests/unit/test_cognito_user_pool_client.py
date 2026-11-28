# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``update_user_pool_client_preserving`` sends back exactly what the update accepts.

The client under test is a real botocore ``cognito-idp`` client with a ``Stubber``,
so the installed service model both chooses the fields and validates the request:
a field the update does not accept fails here with the same ``ParamValidationError``
Cognito's callers saw in production, and a writable field that is dropped fails the
expected-params comparison.
"""

from typing import Any, Dict

import boto3
import pytest
from botocore.exceptions import ParamValidationError
from botocore.stub import Stubber

from idp_common.cognito_user_pool_client import (
    updatable_client_fields,
    update_user_pool_client_preserving,
)

pytestmark = pytest.mark.unit

POOL = "us-east-1_abc123"
CLIENT = "clientid123"

#: Read-only fields describe returns that the update does not accept.
READ_ONLY = {
    "ClientSecret": "not-a-real-secret-placeholder",
    "CreationDate": "2026-01-01T00:00:00Z",
    "LastModifiedDate": "2026-01-02T00:00:00Z",
}


def _cognito():
    return boto3.client(
        "cognito-idp",
        region_name="us-east-1",
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        aws_secret_access_key="1234abcd" * 5,
    )


def _sample(shape) -> Any:
    """A valid value for a botocore shape, deterministic per shape name."""
    t = shape.type_name
    if t == "structure":
        return {name: _sample(member) for name, member in shape.members.items()}
    if t == "list":
        return [_sample(shape.member)]
    if t == "map":
        return {"k": _sample(shape.value)}
    if t == "boolean":
        return True
    if t in ("integer", "long"):
        return int(shape.metadata.get("min", 1))
    if t in ("double", "float"):
        return 1.0
    if t == "timestamp":
        return "2026-01-01T00:00:00Z"
    if shape.enum:
        return shape.enum[0]
    if shape.name.endswith("ArnType") or "Arn" in shape.name:
        return "arn:aws:iam::123456789012:role/example"
    return "x" * max(1, int(shape.metadata.get("min", 1)))


def _every_writable_field(cognito) -> Dict[str, Any]:
    """A described client carrying every field the update accepts, plus read-only."""
    input_shape = cognito.meta.service_model.operation_model(
        "UpdateUserPoolClient"
    ).input_shape
    described = {name: _sample(member) for name, member in input_shape.members.items()}
    described.update(UserPoolId=POOL, ClientId=CLIENT, **READ_ONLY)
    return described


def _stub_round_trip(cognito, described, expected_update):
    stub = Stubber(cognito)
    stub.add_response(
        "describe_user_pool_client",
        {"UserPoolClient": described},
        {"UserPoolId": POOL, "ClientId": CLIENT},
    )
    stub.add_response(
        "update_user_pool_client", {"UserPoolClient": {}}, expected_update
    )
    return stub


def test_every_writable_field_is_sent_back_and_read_only_ones_are_not():
    cognito = _cognito()
    described = _every_writable_field(cognito)
    expected = {k: v for k, v in described.items() if k not in READ_ONLY}
    expected["ExplicitAuthFlows"] = ["ALLOW_USER_SRP_AUTH"]
    # The fixture is not vacuous: it reaches the fields a hand-kept list missed.
    for field in (
        "RefreshTokenRotation",
        "AuthSessionValidity",
        "AnalyticsConfiguration",
    ):
        if field in updatable_client_fields(cognito):
            assert field in expected
    with _stub_round_trip(cognito, described, expected) as stub:
        update_user_pool_client_preserving(
            cognito, POOL, CLIENT, ExplicitAuthFlows=["ALLOW_USER_SRP_AUTH"]
        )
        stub.assert_no_pending_responses()


def test_a_described_field_the_update_does_not_model_is_not_sent():
    """``ClientType`` is the live case: newer models return it from describe and
    the update rejects it. Simulated by a describe that answers with a field the
    installed update shape lacks, through a real client so the request is still
    validated against the real model."""
    cognito = _cognito()
    assert "ClientType" not in updatable_client_fields(cognito)
    real_describe = cognito.describe_user_pool_client

    def describe_with_client_type(**kwargs):
        response = real_describe(**kwargs)
        response["UserPoolClient"]["ClientType"] = "PUBLIC"
        return response

    cognito.describe_user_pool_client = describe_with_client_type  # type: ignore[method-assign]
    described = {"ClientName": "web", "ExplicitAuthFlows": [], **READ_ONLY}
    expected = {
        "UserPoolId": POOL,
        "ClientId": CLIENT,
        "ClientName": "web",
        "ExplicitAuthFlows": ["ALLOW_ADMIN_USER_PASSWORD_AUTH"],
    }
    with _stub_round_trip(cognito, described, expected) as stub:
        update_user_pool_client_preserving(
            cognito,
            POOL,
            CLIENT,
            ExplicitAuthFlows=["ALLOW_ADMIN_USER_PASSWORD_AUTH"],
        )
        stub.assert_no_pending_responses()


def test_the_old_denylist_shape_is_rejected_by_the_real_model():
    """Red half: echoing ClientType back, as the read-only denylist did, is the
    ParamValidationError the ca-central-1 run failed with."""
    cognito = _cognito()
    stub = Stubber(cognito)
    # A queued response, so the call gets past the stub to the model's validation.
    stub.add_response("update_user_pool_client", {"UserPoolClient": {}})
    with stub, pytest.raises(ParamValidationError, match="ClientType"):
        cognito.update_user_pool_client(
            UserPoolId=POOL, ClientId=CLIENT, ClientType="PUBLIC"
        )


def test_none_values_are_dropped_and_the_change_wins():
    cognito = _cognito()
    real_describe = cognito.describe_user_pool_client

    def describe_with_none(**kwargs):
        response = real_describe(**kwargs)
        response["UserPoolClient"]["WriteAttributes"] = None
        return response

    cognito.describe_user_pool_client = describe_with_none  # type: ignore[method-assign]
    described = {
        "ClientName": "web",
        "CallbackURLs": ["https://old.example.com/"],
        "ExplicitAuthFlows": ["ALLOW_USER_SRP_AUTH"],
    }
    expected = {
        "UserPoolId": POOL,
        "ClientId": CLIENT,
        "ClientName": "web",
        "CallbackURLs": ["https://new.example.com/"],
        "ExplicitAuthFlows": ["ALLOW_USER_SRP_AUTH"],
    }
    with _stub_round_trip(cognito, described, expected) as stub:
        update_user_pool_client_preserving(
            cognito, POOL, CLIENT, CallbackURLs=["https://new.example.com/"]
        )
        stub.assert_no_pending_responses()


def test_a_change_the_update_does_not_accept_is_refused_before_any_call():
    cognito = _cognito()
    with Stubber(cognito) as stub, pytest.raises(ValueError, match="ClientSecret"):
        update_user_pool_client_preserving(cognito, POOL, CLIENT, ClientSecret="nope")
    stub.assert_no_pending_responses()
