# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Change some fields of a Cognito user-pool app client and keep all the others.

``UpdateUserPoolClient`` is a full replace, not a patch: every field the request
omits is cleared. So a change to one field (the auth flows, the callback URLs) has
to read the client with ``DescribeUserPoolClient`` and send it back.

What to send back is decided by the **same SDK model that produced the describe
response**: the request carries exactly the described fields that are members of
``UpdateUserPoolClient``'s input shape. A hand-kept list fails in one of two ways
as Cognito evolves. A list of fields to *drop* sends back any new read-only field
the describe starts returning (``ClientType`` is one, which the update rejects with
``Unknown parameter``); a list of fields to *keep* silently clears any writable
field it does not name (``RefreshTokenRotation``, ``AuthSessionValidity``,
``AnalyticsConfiguration``, ...). Reading the model avoids both for whatever
botocore version is installed.

Residual: a field the service has but the installed botocore model does not know
is invisible to both calls, so it is neither read nor preserved, and the update
clears it. Only a newer botocore closes that gap.

The module imports nothing beyond the standard library; the caller passes the
``cognito-idp`` client.
"""

from typing import Any, Dict, FrozenSet

__all__ = ["updatable_client_fields", "update_user_pool_client_preserving"]


def updatable_client_fields(cognito: Any) -> FrozenSet[str]:
    """The request fields ``UpdateUserPoolClient`` accepts, per ``cognito``'s model."""
    shape = cognito.meta.service_model.operation_model("UpdateUserPoolClient")
    return frozenset(shape.input_shape.members)


def update_user_pool_client_preserving(
    cognito: Any, user_pool_id: str, client_id: str, **changes: Any
) -> Dict[str, Any]:
    """Apply ``changes`` to an app client, sending every other writable field back.

    Reads the client, keeps each described field that is a member of the update's
    input shape and is not ``None``, applies ``changes`` on top, and calls
    ``UpdateUserPoolClient``. Returns the update response.

    Raises ``ValueError`` if a key in ``changes`` is not a field the update accepts,
    rather than letting botocore reject it after the read.
    """
    accepted = updatable_client_fields(cognito)
    unknown = sorted(set(changes) - accepted)
    if unknown:
        raise ValueError(f"UpdateUserPoolClient does not accept {', '.join(unknown)}")
    current = cognito.describe_user_pool_client(
        UserPoolId=user_pool_id, ClientId=client_id
    )["UserPoolClient"]
    request = {k: v for k, v in current.items() if k in accepted and v is not None}
    request.update(changes)
    request["UserPoolId"] = user_pool_id
    request["ClientId"] = client_id
    return cognito.update_user_pool_client(**request)
