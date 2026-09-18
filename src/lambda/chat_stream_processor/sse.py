# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Dependency-free helpers for the chat streaming endpoint.

Kept separate from ``app.py`` (which imports FastAPI and the processor modules)
so the pure logic — SSE framing, Cognito-sub extraction and caller-identity
resolution — can be unit-tested without those heavier runtime dependencies.
"""

from __future__ import annotations

import json
import re
from datetime import datetime


def now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sse(payload: dict) -> str:
    """Encode one event as a Server-Sent-Events frame: ``data: {json}\\n\\n``."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# Role session names that Amazon Cognito itself chooses when it exchanges an
# identity for temporary credentials. They are the SAME string for every user of
# the pool, so a request context bearing one of these carries a verified
# *transport* principal but NOT a per-user identity.
#
# MEASURED, NOT DOCUMENTED. On a live commercial deployment every set of
# credentials the browser signs with carries the session name
# "CognitoIdentityCredentials": 499 CloudTrail
# `AssumeRoleWithWebIdentity` events across three identity pools and three roles,
# all 499 with that same `requestParameters.roleSessionName`, zero
# counter-examples. `userIdentity.type` is `WebIdentityUser` with
# `identityProvider: cognito-identity.amazonaws.com` and an `aws-internal` user
# agent, so the value is chosen by AWS and the client has no channel to influence
# it. The per-user identity does exist in the STS record
# (`subjectFromWebIdentityToken`, e.g. `us-west-2:<uuid>`) but never reaches the
# role ARN. Nothing logs a Function URL `userArn` on this deployment, so that
# field itself was not observed directly; Lambda documents it as the caller
# identity's ARN, from which the trailing segment follows.
#
# AWS documents no `RoleSessionName` for the enhanced flow anywhere — the Cognito
# IAM-roles page enumerates what it puts in the background
# `AssumeRoleWithWebIdentity` call and omits it — so there is NO compatibility
# commitment on this string. If it ever became user-specific, a check that
# treated it as an identity to compare against would refuse every request (the
# browser sends an email, which can never equal a session name). That is why the
# predicate below is a POSITIVE shape test rather than a denylist: an
# unrecognised future value falls through to the body-supplied fallback instead
# of denying every turn. See the residual note on GAP-07 in
# scripts/api_rbac_expectations.yaml.
#
# Lambda Function URLs also do not forward the Cognito identity separately:
# `requestContext.authorizer.iam.cognitoIdentity` is documented as "Function URLs
# don't use this parameter. Lambda sets this to null or excludes this from the
# JSON" (https://docs.aws.amazon.com/lambda/latest/dg/urls-invocation.html) —
# note "or excludes", i.e. the key may be ABSENT rather than null. Nothing here
# reads it, so no code path has to distinguish the two; the assumed-role ARN is
# the only identity signal available on this transport.
_GENERIC_IDENTITY_POOL_SESSION_NAMES = frozenset(
    {
        "CognitoIdentityCredentials",  # cognito-identity:GetCredentialsForIdentity
        "CognitoIdentityCredentialsUnauthenticated",
    }
)

# Shapes that ARE a per-user Cognito identifier: a bare User Pool `sub` (UUID) or
# an Identity Pool identity id (`<region>:<uuid>`).
_COGNITO_SUB_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_COGNITO_IDENTITY_ID_RE = re.compile(
    r"^[a-z]{2}(?:-[a-z]+)+-\d:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def is_user_specific_identity(caller_sub: str) -> bool:
    """True when ``caller_sub`` identifies one particular user.

    ``caller_sub_from_request_context`` returns whatever the SigV4 principal
    yields. Under the Cognito Identity Pool enhanced flow that is a pool-wide
    constant (measured: ``CognitoIdentityCredentials``), and comparing a
    pool-wide constant against a per-user value supplied by the client would
    reject every request. Callers that want to *compare* a claimed identity
    against the verified one must gate the comparison on this predicate.

    Deliberately a POSITIVE test: only the two shapes Cognito uses for a per-user
    identifier count. Anything else — the two known generic session names, and
    equally any value AWS might substitute for them in future, since the string
    is undocumented — is treated as *not* per-user, so it falls through to the
    body-supplied fallback rather than turning every turn into a 403. That trades
    a hypothetical comparison against an unrecognised shape for availability, and
    it is the safe direction here because the fallback path never grants anything
    a caller could not already reach: the transport has already authenticated
    them, and the value only decides which chat history the turn is filed under.
    """
    if not caller_sub or caller_sub in _GENERIC_IDENTITY_POOL_SESSION_NAMES:
        return False
    return bool(
        _COGNITO_SUB_RE.match(caller_sub)
        or _COGNITO_IDENTITY_ID_RE.match(caller_sub)
    )


class CallerIdentityConflict(Exception):
    """A client-asserted caller identity contradicts the verified one.

    ``app.py`` translates this into a 403. It is a distinct exception (rather
    than the HTTP one) so the decision itself stays here, testable without
    FastAPI.
    """


def resolve_caller_sub(verified: str, claimed: str) -> str:
    """Resolve the caller identity, preferring the one the transport verified.

    ``verified`` is derived from the SigV4 principal; ``claimed`` is whatever the
    request body asserted.

    When the verified value identifies a particular user it wins outright, and a
    claimed value that CONTRADICTS it raises ``CallerIdentityConflict``: a
    client-supplied identity that disagrees with the one the transport proved is
    never legitimate, so the request is refused rather than silently resolved in
    either direction.

    When it does not — a pool-wide Cognito session name says *a* signed-in user
    of this pool is calling, not *which* one — there is nothing to contradict, so
    the claimed value is used as a fallback. Comparing a pool-wide constant with
    a per-user value would refuse every legitimate request, and discarding the
    claimed value would leave the turn unattributable. **This is the live branch
    on the deployed commercial transport**, where ``verified`` is measured to be
    the constant ``CognitoIdentityCredentials``: the body value is what the turn
    is attributed to today.

    The refusal is also gated on the claimed value being an identifier of the
    same kind. A claim that is not even shaped like a Cognito identity (the web
    UI sends an email) is not a *contradicting* identity, it is a different kind
    of name, so it is discarded in favour of the proven one rather than treated
    as an attack. Without that, a future AWS change to a per-user session name
    would turn every agent-chat turn into a 403; with it, the worst case is that
    attribution switches to the proven identifier.
    """
    if not is_user_specific_identity(verified):
        return claimed or verified
    if claimed and is_user_specific_identity(claimed) and claimed != verified:
        raise CallerIdentityConflict(
            "body-supplied caller identity does not match the identity "
            "verified by the transport"
        )
    return verified


def caller_sub_from_request_context(raw_request_context: str | None) -> str:
    """Extract the Cognito ``sub`` from a Function URL request context header.

    Lambda Function URLs with ``AuthType=AWS_IAM`` forward the SigV4 caller
    identity. With the Lambda Web Adapter the original Lambda event's
    ``requestContext`` is available on the ``x-amzn-request-context`` header. The
    assumed-role ARN's session name is the only identity signal on this transport
    (``...:assumed-role/<role>/<session-name>``).

    It is NOT necessarily a per-user value, despite the function name: under the
    Cognito Identity Pool enhanced flow the session name is measured to be the
    pool-wide constant ``CognitoIdentityCredentials``. The ``userId`` fallback is
    no better — for an assumed role it is ``<role-unique-id>:<session-name>``,
    e.g. ``AROAEXAMPLEID:CognitoIdentityCredentials``, which is pool-wide too.
    Use ``is_user_specific_identity`` before treating the result as an identity
    to compare against; ``resolve_caller_sub`` does.

    Returns an empty string when the context is missing or unparseable.
    """
    if not raw_request_context:
        return ""
    try:
        ctx = json.loads(raw_request_context)
    except (TypeError, ValueError):
        return ""
    identity = (ctx.get("authorizer") or {}).get("iam") or {}
    user_arn = identity.get("userArn") or ""
    if ":assumed-role/" in user_arn:
        # arn:aws:sts::acct:assumed-role/<role>/<session-name>
        return user_arn.rsplit("/", 1)[-1]
    return identity.get("userId") or ""
