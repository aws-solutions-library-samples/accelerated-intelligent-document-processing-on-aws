# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The UI API helpers the SDK uses to reach a stack's web UI backend: resolving
the pool, client and API base from CloudFormation, and minting a user's token."""

from typing import Any, Dict, List

import pytest

from idp_sdk._core import ui_api  # noqa: TID251 - testing the internal module


class _Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **_):
        return iter(self.pages)


class _Cfn:
    def __init__(self, resources: Dict[str, str], outputs: List[Dict[str, str]]):
        self.resources = resources
        self.outputs = outputs

    def get_paginator(self, name):
        assert name == "list_stack_resources"
        return _Paginator(
            [
                {
                    "StackResourceSummaries": [
                        {"LogicalResourceId": k, "PhysicalResourceId": v}
                        for k, v in self.resources.items()
                    ]
                }
            ]
        )

    def describe_stacks(self, StackName):
        assert StackName == "api-stack"
        return {"Stacks": [{"Outputs": self.outputs}]}


FULL = {
    "UserPool": "pool",
    "UserPoolClient": "client",
    "APIRESOLVERSTACK": "api-stack",
    "UsersTable": "users",
}
ENDPOINT = [{"OutputKey": "HttpApiEndpoint", "OutputValue": "https://x.example/api/"}]


@pytest.mark.unit
def test_the_ui_context_is_read_from_the_stack():
    ctx = ui_api.resolve_ui_context("s", "us-east-1", cfn=_Cfn(FULL, ENDPOINT))
    assert ctx == ui_api.UiContext(
        stack="s",
        region="us-east-1",
        user_pool="pool",
        client="client",
        api_base="https://x.example/api",
        users_table="users",
    )


@pytest.mark.unit
@pytest.mark.parametrize("missing", ["UserPool", "UserPoolClient", "APIRESOLVERSTACK"])
def test_a_stack_without_the_ui_api_is_refused(missing):
    resources = {k: v for k, v in FULL.items() if k != missing}
    with pytest.raises(RuntimeError, match="no web UI user pool"):
        ui_api.resolve_ui_context("s", "us-east-1", cfn=_Cfn(resources, ENDPOINT))


@pytest.mark.unit
def test_an_api_stack_without_an_endpoint_is_refused():
    with pytest.raises(RuntimeError, match="HttpApiEndpoint"):
        ui_api.resolve_ui_context("s", "us-east-1", cfn=_Cfn(FULL, []))


class _Cognito:
    class exceptions:
        class UsernameExistsException(Exception):
            pass

    def __init__(self, *, exists=False, auth_failures: List[Exception] = ()):
        self.calls: List[tuple] = []
        self.exists = exists
        self.auth_failures = list(auth_failures)

    def admin_create_user(self, **kwargs: Any):
        self.calls.append(("create", kwargs["Username"]))
        if self.exists:
            raise self.exceptions.UsernameExistsException()

    def admin_set_user_password(self, **kwargs: Any):
        self.calls.append(("password", kwargs["Username"], kwargs["Permanent"]))

    def admin_add_user_to_group(self, **kwargs: Any):
        self.calls.append(("group", kwargs["Username"], kwargs["GroupName"]))

    def admin_initiate_auth(self, **kwargs: Any):
        self.calls.append(("auth", kwargs["AuthFlow"]))
        if self.auth_failures:
            raise self.auth_failures.pop(0)
        return {"AuthenticationResult": {"IdToken": "id-token"}}


@pytest.mark.unit
@pytest.mark.parametrize("exists", [False, True])
def test_a_user_is_created_with_a_permanent_password_and_its_group(exists):
    cognito = _Cognito(exists=exists)
    ui_api.create_cognito_user(cognito, "pool", "a@example.com", "pw", "Viewer")
    assert cognito.calls == [
        ("create", "a@example.com"),
        ("password", "a@example.com", True),
        ("group", "a@example.com", "Viewer"),
    ]


@pytest.mark.unit
def test_a_user_without_a_group_is_put_in_none():
    cognito = _Cognito()
    ui_api.create_cognito_user(cognito, "pool", "a@example.com", "pw", None)
    assert [c[0] for c in cognito.calls] == ["create", "password"]


CTX = ui_api.UiContext("s", "us-east-1", "pool", "client", "https://x/api", "users")


@pytest.mark.unit
def test_the_token_waits_for_the_auth_flow_to_propagate():
    slept: List[float] = []
    cognito = _Cognito(
        auth_failures=[RuntimeError(ui_api.AUTH_FLOW_NOT_PROPAGATED)] * 2
    )
    token = ui_api.id_token(cognito, CTX, "a@example.com", "pw", sleep=slept.append)
    assert token == "id-token"
    assert slept == list(ui_api.AUTH_FLOW_RETRY_DELAYS[:2])


@pytest.mark.unit
def test_any_other_auth_error_is_raised_at_once():
    slept: List[float] = []
    cognito = _Cognito(auth_failures=[RuntimeError("NotAuthorizedException")])
    with pytest.raises(RuntimeError, match="NotAuthorized"):
        ui_api.id_token(cognito, CTX, "a@example.com", "pw", sleep=slept.append)
    assert slept == []


@pytest.mark.unit
def test_the_auth_flow_retry_gives_up_after_its_last_delay():
    slept: List[float] = []
    failures = [RuntimeError(ui_api.AUTH_FLOW_NOT_PROPAGATED)] * (
        len(ui_api.AUTH_FLOW_RETRY_DELAYS) + 1
    )
    with pytest.raises(RuntimeError, match="not enabled"):
        ui_api.id_token(
            _Cognito(auth_failures=failures), CTX, "a", "pw", sleep=slept.append
        )
    assert slept == list(ui_api.AUTH_FLOW_RETRY_DELAYS)
