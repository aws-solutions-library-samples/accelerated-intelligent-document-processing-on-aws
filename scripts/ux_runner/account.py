# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The account guard, and the narrow AWS sessions minted for ``claude`` and render.

Two jobs, both about which AWS identity does what.

The account guard
-----------------
The expected account id arrives masked in ``UX_TEST_AWS_ACCOUNT_ID`` and is
compared with ``sts:GetCallerIdentity``. A mismatch means the job is holding
somebody else's credentials, or the right credentials for the wrong account, and
in either case nothing should be driven against whatever stack those reach.
:class:`AccountMismatch` reports **only the last four digits** of each id, because
the message lands in a job log and an account id names a target. Nothing in this
module prints. The account alias is not read to make the message friendlier,
because the job's role has no ``iam:ListAccountAliases``.

Sessions minted at the moment of use
------------------------------------
The job's own credentials are a chained role session minted before the job
starts, and AWS caps a chained session at one hour, which a long UX job can
outlive: ``claude`` runs for up to 25 minutes and render comes after it.
:func:`scoped_credentials` therefore has the job's role re-assume itself at the
moment each consumer needs a session, which ``credential-vendor.yml`` already
permits (``sts:AssumeRole`` and ``sts:TagSession`` on the role itself, for the
integration-test monitor's refresh). Each re-assume carries a session policy, and
a session policy can only narrow what the role allows:

* ``claude`` gets :data:`BEDROCK_ACTIONS` and nothing else, so a session talked
  into misusing its credential has no S3, no CloudFormation and no IAM to reach,
  as long as the re-assume works;
* render gets :data:`POLLY_ACTIONS`, for the narration and nothing else.

When the re-assume fails (no role ARN in the identity, a refused assume, a
throttle), the two consumers part ways, and ``credential_scope`` holds one entry
for each. ``claude`` falls back to the ambient credentials, the summary records
``'ambient'`` for it, and the orchestrator says so before the session starts, so
the summary says which property did not hold rather than the run failing over it.
Render does not: it handles the most MR-influenced data of the run, so it renders
without narration and holds no credential at all (``'unavailable'``). On a
developer's host the fallback is the usual case, because an SSO permission-set
role or IAM-user keys cannot re-assume themselves (see the bound below). Each
session reaches its consumer as three environment variables: ``claude`` and
render are both child processes, and neither is handed anything else of the
job's.

Bounds worth knowing
--------------------
:func:`role_arn_for` maps an assumed-role ARN back to its role ARN, and an
assumed-role ARN carries no role path, so a role created under a path maps to an
ARN that does not exist and the re-assume falls back as above. The CI role has
no path. An SSO permission-set role lives under
``/aws-reserved/sso.amazonaws.com/`` and trusts only its SAML provider, and
IAM-user keys map to ``None``, so a local run on either gets no scoped session.
Every ARN here is built from the partition of the identity it came from, never
written out, so the same code is right in every partition.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

#: What the ``claude`` session's credential may do: invoke a Bedrock model.
BEDROCK_ACTIONS: tuple[str, ...] = (
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
)
#: What render's credential may do: synthesise the narration.
POLLY_ACTIONS: tuple[str, ...] = ("polly:SynthesizeSpeech",)

_ASSUMED_ROLE_ARN = re.compile(
    r"^arn:(?P<partition>aws[a-z-]*):sts::(?P<account>[0-9]{12}):"
    r"assumed-role/(?P<role>[\w+=,.@-]{1,64})/(?P<session>[\w+=,.@-]{2,64})$"
)


class AccountMismatch(Exception):
    """The credentials belong to a different account than the one expected."""


@dataclass(frozen=True)
class Identity:
    """The caller identity the job's credentials resolve to."""

    account: str
    arn: str


def sts_client(region: str) -> Any:
    """An STS client for ``region``. boto3 is imported here, not at module level."""
    import boto3

    return boto3.client("sts", region_name=region)


def caller_identity(sts: Any) -> Identity:
    """Who the job's credentials are, from ``sts:GetCallerIdentity``."""
    response = sts.get_caller_identity()
    return Identity(account=str(response["Account"]), arn=str(response["Arn"]))


def _ending(account: str) -> str:
    """The last four characters of an account id: all of it a message may show."""
    return account[-4:] if account else "(none)"


def check_account(identity: Identity, expected: str) -> None:
    """Raise :class:`AccountMismatch` unless ``identity`` is in ``expected``.

    The message names each account by its last four digits only.
    """
    wanted = expected.strip()
    actual = identity.account.strip()
    if not wanted:
        raise AccountMismatch(
            f"no expected account id was given to compare with the credentials' "
            f"account ending in {_ending(actual)}"
        )
    if actual != wanted:
        raise AccountMismatch(
            f"the credentials are for the account ending in {_ending(actual)}, not "
            f"the expected account ending in {_ending(wanted)}"
        )


def role_arn_for(identity: Identity) -> str | None:
    """The role ARN behind an assumed-role session ARN, in the same partition.

    ``arn:<p>:sts::<acct>:assumed-role/<role>/<session>`` maps to
    ``arn:<p>:iam::<acct>:role/<role>``. Any other shape (an IAM user, a federated
    user, the root user, a malformed ARN) gives ``None``, which the caller treats
    as "no re-assume possible".
    """
    match = _ASSUMED_ROLE_ARN.match(identity.arn)
    if match is None:
        return None
    return f"arn:{match['partition']}:iam::{match['account']}:role/{match['role']}"


def session_policy(actions: Sequence[str]) -> str:
    """The session policy allowing exactly ``actions`` on every resource.

    ``Resource`` is ``*`` because a session policy cannot grant anything the role's
    own policy does not; the role's policy decides the resources and this decides
    which of the role's actions the session keeps.
    """
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": list(actions), "Resource": "*"}
            ],
        }
    )


def scoped_credentials(
    sts: Any,
    role_arn: str | None,
    actions: Sequence[str],
    session_name: str,
    duration: int = 3600,
) -> dict[str, str] | None:
    """A fresh session of ``role_arn`` narrowed to ``actions``, as environment keys.

    Returns ``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY`` and
    ``AWS_SESSION_TOKEN``, or ``None`` when ``role_arn`` is ``None`` or the assume
    fails for any reason. What a ``None`` costs is the caller's to decide per
    consumer, as the module docstring says. One hour is the most a chained assume
    may ask for.
    """
    if role_arn is None:
        return None
    try:
        response = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            Policy=session_policy(actions),
            DurationSeconds=duration,
        )
        issued = response["Credentials"]
        return {
            "AWS_ACCESS_KEY_ID": str(issued["AccessKeyId"]),
            "AWS_SECRET_ACCESS_KEY": str(issued["SecretAccessKey"]),
            "AWS_SESSION_TOKEN": str(issued["SessionToken"]),
        }
    except Exception:
        return None
