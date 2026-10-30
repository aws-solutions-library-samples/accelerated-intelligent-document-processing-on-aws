# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The RBAC harness waits out Cognito's propagation of a new auth flow.

`enable_admin_auth` adds ALLOW_ADMIN_USER_PASSWORD_AUTH with UpdateUserPoolClient,
which is eventually consistent: AdminInitiateAuth can answer "Auth flow not
enabled for this client" for a few seconds afterwards. The 2026-10-29 nightly
failed Step 12 on the first token it minted, before any authorization assertion
ran. These tests pin the retry to that one message and keep it bounded.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CTX = {"user_pool": "us-east-1_pool", "client": "client123", "region": "us-east-1"}
NOT_PROPAGATED = (
    "aws cognito-idp admin-initiate-auth failed: An error occurred "
    "(InvalidParameterException) when calling the AdminInitiateAuth operation: "
    "Auth flow not enabled for this client"
)


@pytest.fixture
def rbac_common():
    path = Path(__file__).resolve().parents[2] / "rbac_common.py"
    spec = importlib.util.spec_from_file_location("rbac_common_propagation", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scripted(mod, monkeypatch, outcomes):
    calls = []

    def fake_aws(*args, region=None):
        calls.append(args)
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    sleeps = []
    monkeypatch.setattr(mod, "aws", fake_aws)
    monkeypatch.setattr(mod.time, "sleep", sleeps.append)
    return calls, sleeps


def test_the_id_token_is_minted_once_the_flow_propagates(rbac_common, monkeypatch):
    calls, sleeps = _scripted(
        rbac_common,
        monkeypatch,
        [RuntimeError(NOT_PROPAGATED), RuntimeError(NOT_PROPAGATED), "x" * 200],
    )
    assert rbac_common.get_id_token(CTX, "a@example.invalid", "pw") == "x" * 200
    assert len(calls) == 3
    assert sleeps == list(rbac_common.AUTH_FLOW_RETRY_DELAYS[:2])
    assert calls[0][-2:] == ("--output", "text")
    assert "AuthenticationResult.IdToken" in calls[0]


def test_the_full_auth_result_retries_the_same_way(rbac_common, monkeypatch):
    calls, _ = _scripted(
        rbac_common, monkeypatch, [RuntimeError(NOT_PROPAGATED), {"IdToken": "t"}]
    )
    assert rbac_common.get_auth_result(CTX, "a@example.invalid", "pw") == {
        "IdToken": "t"
    }
    assert len(calls) == 2


def test_any_other_error_fails_at_once(rbac_common, monkeypatch):
    """A wrong password or a missing user is a real failure, not propagation."""
    calls, sleeps = _scripted(
        rbac_common,
        monkeypatch,
        [RuntimeError("(NotAuthorizedException) Incorrect username or password.")],
    )
    with pytest.raises(RuntimeError, match="Incorrect username"):
        rbac_common.get_id_token(CTX, "a@example.invalid", "pw")
    assert len(calls) == 1
    assert sleeps == []


def test_a_flow_that_never_takes_effect_still_fails(rbac_common, monkeypatch):
    calls, sleeps = _scripted(rbac_common, monkeypatch, [RuntimeError(NOT_PROPAGATED)])
    with pytest.raises(RuntimeError, match="Auth flow not enabled"):
        rbac_common.get_id_token(CTX, "a@example.invalid", "pw")
    assert len(calls) == len(rbac_common.AUTH_FLOW_RETRY_DELAYS) + 1
    assert sum(sleeps) <= 90, "the wait must stay bounded"


# The federated sign-in live check enables the same flow and authenticates
# straight after, through boto3, which does not retry InvalidParameterException.


class _InvalidParameter(Exception):
    pass


class _FakeCognito:
    class exceptions:
        InvalidParameterException = _InvalidParameter

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def admin_initiate_auth(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def federated(monkeypatch):
    live = Path(__file__).resolve().parents[2] / "security" / "live_checks"
    monkeypatch.syspath_prepend(str(live))
    spec = importlib.util.spec_from_file_location(
        "verify_federated_signin_propagation", live / "verify_federated_signin.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", sleeps.append)
    return mod, sleeps


def test_the_federated_check_waits_out_the_same_race(federated):
    mod, sleeps = federated
    cog = _FakeCognito(
        [_InvalidParameter("Auth flow not enabled for this client"), {"ok": 1}]
    )
    assert mod.admin_password_auth(cog, "pool", "client") == {"ok": 1}
    assert cog.calls == 2
    assert sleeps == [mod.AUTH_FLOW_RETRY_DELAYS[0]]


def test_the_federated_check_fails_other_errors_at_once(federated):
    mod, sleeps = federated
    cog = _FakeCognito([_InvalidParameter("USERNAME is required")])
    with pytest.raises(_InvalidParameter):
        mod.admin_password_auth(cog, "pool", "client")
    assert cog.calls == 1
    assert sleeps == []
