# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the UX runner's account guard and scoped sessions.

Two properties matter enough to pin. The guard never shows a whole account id,
because its message lands in a job log, so a mismatch is reported by the last four
digits of each side and the test checks that neither full id appears. And the
sessions handed to ``claude`` and to render are narrowed by a session policy and
fall back to ``None`` (the ambient credentials) on any failure rather than taking
the run down with them. STS is a fake that records what it is asked, and boto3 is
replaced in ``sys.modules`` for the client, so no client is built and nothing
leaves the process.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import account  # noqa: E402


class FakeSts:
    """Answers the two STS calls the module makes and records what it was asked."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.assumed: list[dict[str, Any]] = []

    def get_caller_identity(self) -> dict[str, str]:
        return {
            "UserId": "AROAEXAMPLEID:ux-test",
            "Account": "123456789012",
            "Arn": "arn:aws:sts::123456789012:assumed-role/idp-sdlc-GitLab/ux-test",
        }

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.assumed.append(kwargs)
        if self.fail:
            raise RuntimeError("AccessDenied: not authorized to perform sts:AssumeRole")
        return {
            "Credentials": {
                "AccessKeyId": "ASIAIOSFODNN7EXAMPLE",
                "SecretAccessKey": "example-scoped-key-value",
                "SessionToken": "example-scoped-session-value",
                "Expiration": "2026-10-15T12:00:00Z",
            }
        }


def _identity(arn: str, number: str = "123456789012") -> account.Identity:
    return account.Identity(account=number, arn=arn)


def _session(key_id: str, key: str, session: str) -> dict[str, str]:
    """The three environment keys of an AWS session, from positional values."""
    names = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
    return dict(zip(names, (key_id, key, session), strict=True))


@pytest.mark.unit
def test_caller_identity_reads_the_account_and_arn() -> None:
    identity = account.caller_identity(FakeSts())
    assert identity == account.Identity(
        account="123456789012",
        arn="arn:aws:sts::123456789012:assumed-role/idp-sdlc-GitLab/ux-test",
    )


@pytest.mark.unit
def test_the_guard_passes_when_the_account_is_the_expected_one() -> None:
    account.check_account(_identity("arn:aws:sts::x"), "123456789012")
    account.check_account(_identity("arn:aws:sts::x"), " 123456789012\n")


@pytest.mark.unit
def test_a_mismatch_names_each_account_by_its_last_four_digits_only() -> None:
    with pytest.raises(account.AccountMismatch) as caught:
        account.check_account(
            _identity("arn:aws:sts::x", number="111122223333"), "123456789012"
        )
    message = str(caught.value)
    assert "9012" in message and "3333" in message
    assert "123456789012" not in message
    assert "111122223333" not in message


@pytest.mark.unit
def test_an_empty_expected_account_is_a_mismatch_that_still_hides_the_actual_one() -> (
    None
):
    with pytest.raises(account.AccountMismatch) as caught:
        account.check_account(_identity("arn:aws:sts::x"), "  ")
    assert "123456789012" not in str(caught.value)
    assert "9012" in str(caught.value)


@pytest.mark.unit
def test_an_assumed_role_session_maps_to_its_role_in_the_same_partition() -> None:
    commercial = _identity(
        "arn:aws:sts::123456789012:assumed-role/idp-sdlc-GitLab/session-1"
    )
    assert account.role_arn_for(commercial) == (
        "arn:aws:iam::123456789012:role/idp-sdlc-GitLab"
    )
    govcloud = _identity(
        "arn:aws-us-gov:sts::111122223333:assumed-role/idp-sdlc-GitLab/ux-test-812"
    )
    assert account.role_arn_for(govcloud) == (
        "arn:aws-us-gov:iam::111122223333:role/idp-sdlc-GitLab"
    )


@pytest.mark.unit
def test_any_other_identity_shape_has_no_role_to_re_assume() -> None:
    for arn in (
        "arn:aws:iam::123456789012:user/alice",
        "arn:aws:sts::123456789012:federated-user/bob",
        "arn:aws:iam::123456789012:root",
        "arn:aws:sts::123456789012:assumed-role/only-a-role",
        "arn:aws:sts::12345:assumed-role/role/session",
        "not an arn",
        "",
    ):
        assert account.role_arn_for(_identity(arn)) is None, arn


@pytest.mark.unit
def test_the_session_policy_allows_exactly_the_given_actions() -> None:
    policy = json.loads(account.session_policy(account.BEDROCK_ACTIONS))
    assert policy == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": "*",
            }
        ],
    }
    polly = json.loads(account.session_policy(account.POLLY_ACTIONS))
    assert polly["Statement"][0]["Action"] == ["polly:SynthesizeSpeech"]


@pytest.mark.unit
def test_scoped_credentials_re_assume_the_role_under_the_session_policy() -> None:
    sts = FakeSts()
    role = "arn:aws:iam::123456789012:role/idp-sdlc-GitLab"
    issued = account.scoped_credentials(
        sts, role, account.BEDROCK_ACTIONS, "ux-test-812"
    )
    assert issued == _session(
        "ASIAIOSFODNN7EXAMPLE",
        "example-scoped-key-value",
        "example-scoped-session-value",
    )
    assert len(sts.assumed) == 1
    asked = sts.assumed[0]
    assert asked["RoleArn"] == role
    assert asked["RoleSessionName"] == "ux-test-812"
    assert asked["DurationSeconds"] == 3600
    assert json.loads(asked["Policy"])["Statement"][0]["Action"] == list(
        account.BEDROCK_ACTIONS
    )


@pytest.mark.unit
def test_scoped_credentials_fall_back_to_none_on_failure_or_without_a_role() -> None:
    failing = FakeSts(fail=True)
    assert (
        account.scoped_credentials(
            failing, "arn:aws:iam::123456789012:role/r", account.POLLY_ACTIONS, "s1"
        )
        is None
    )
    assert len(failing.assumed) == 1

    unused = FakeSts()
    assert account.scoped_credentials(unused, None, account.POLLY_ACTIONS, "s2") is None
    assert unused.assumed == [], "no assume without a role ARN"


@pytest.mark.unit
def test_the_actions_are_the_bedrock_invoke_pair_and_polly_synthesis() -> None:
    assert account.BEDROCK_ACTIONS == (
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
    )
    assert account.POLLY_ACTIONS == ("polly:SynthesizeSpeech",)


@pytest.mark.unit
def test_the_sts_client_is_built_lazily_for_the_given_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[tuple[str, dict[str, Any]]] = []
    fake_boto3 = types.ModuleType("boto3")

    def client(service: str, **kwargs: Any) -> str:
        built.append((service, kwargs))
        return "sts-client"

    setattr(fake_boto3, "client", client)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    assert account.sts_client("eu-west-1") == "sts-client"
    assert built == [("sts", {"region_name": "eu-west-1"})]
