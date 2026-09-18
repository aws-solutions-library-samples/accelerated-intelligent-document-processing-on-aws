# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Dependency-free helpers for the chat streaming endpoint.

Kept separate from ``app.py`` (which imports FastAPI and the processor modules)
so the pure logic — SSE framing, Cognito-sub extraction and caller-identity
resolution — can be unit-tested without those heavier runtime dependencies.
"""

from __future__ import annotations

import json
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
# This matters because Lambda Function URLs do not forward the Cognito identity
# separately: `requestContext.authorizer.iam.cognitoIdentity` is documented as
# "Function URLs don't use this parameter. Lambda sets this to null or excludes
# this from the JSON"
# (https://docs.aws.amazon.com/lambda/latest/dg/urls-invocation.html), so the
# assumed-role ARN is the only identity signal available on this transport.
_GENERIC_IDENTITY_POOL_SESSION_NAMES = frozenset(
    {
        "CognitoIdentityCredentials",  # cognito-identity:GetCredentialsForIdentity
        "CognitoIdentityCredentialsUnauthenticated",
    }
)


def is_user_specific_identity(caller_sub: str) -> bool:
    """True when ``caller_sub`` identifies one particular user.

    ``caller_sub_from_request_context`` returns whatever the SigV4 principal
    yields. Under the Cognito Identity Pool enhanced flow that is a pool-wide
    constant rather than a per-user value, and comparing a pool-wide constant
    against a per-user value supplied by the client would reject every request.
    Callers that want to *compare* a claimed identity against the verified one
    must gate the comparison on this predicate.
    """
    return bool(caller_sub) and caller_sub not in _GENERIC_IDENTITY_POOL_SESSION_NAMES


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
    claimed value would leave the turn unattributable.
    """
    if not is_user_specific_identity(verified):
        return claimed or verified
    if claimed and claimed != verified:
        raise CallerIdentityConflict(
            "body-supplied caller identity does not match the identity "
            "verified by the transport"
        )
    return verified


def caller_sub_from_request_context(raw_request_context: str | None) -> str:
    """Extract the Cognito ``sub`` from a Function URL request context header.

    Lambda Function URLs with ``AuthType=AWS_IAM`` forward the SigV4 caller
    identity. With the Lambda Web Adapter the original Lambda event's
    ``requestContext`` is available on the ``x-amzn-request-context`` header. For
    Cognito Identity Pool credentials the assumed-role ARN's session name holds
    the caller's identity (``...:assumed-role/<role>/<session-name>``).

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
