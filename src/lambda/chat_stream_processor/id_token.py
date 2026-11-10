# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Verify the Cognito ID token the web UI presents to the chat streaming endpoint.

The Function URL authenticates a request by SigV4 with the Identity Pool role,
which proves the caller is a signed-in user of this deployment but not which one,
or in which groups. The browser therefore also sends its User Pool ID token in
``ID_TOKEN_HEADER``, and this module verifies it: RS256 signature against the
pool's published JWKS, the pool as ``iss``, the web UI's app client as ``aud``,
``token_use`` of ``id``, and an unexpired ``exp``.

``COGNITO_ISSUER`` and ``COGNITO_APP_CLIENT_ID`` come from the function's
environment. A token from another app client of the same pool carries another
``aud`` and is refused.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

import jwt
from jwt import PyJWKClient

ID_TOKEN_HEADER = "x-idp-id-token"

_REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub", "token_use"]
_JWKS_TIMEOUT_SECONDS = 5
_JWKS_LIFESPAN_SECONDS = 3600

_jwks_clients: dict[str, PyJWKClient] = {}


class InvalidIdToken(Exception):
    """The request carries no ID token this endpoint can verify."""


def _jwks_client(issuer: str) -> PyJWKClient:
    client = _jwks_clients.get(issuer)
    if client is None:
        client = PyJWKClient(
            f"{issuer}/.well-known/jwks.json",
            cache_keys=True,
            lifespan=_JWKS_LIFESPAN_SECONDS,
            timeout=_JWKS_TIMEOUT_SECONDS,
        )
        _jwks_clients[issuer] = client
    return client


def verify_id_token(
    token: str, *, issuer: str, client_id: str, jwks_client=None
) -> dict:
    """Return the verified claims of ``token``, or raise :class:`InvalidIdToken`."""
    if not token:
        raise InvalidIdToken("no ID token was presented")
    if not issuer or not client_id:
        raise InvalidIdToken("ID token verification is not configured")
    try:
        signing_key = (jwks_client or _jwks_client(issuer)).get_signing_key_from_jwt(
            token
        )
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=issuer,
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as exc:
        raise InvalidIdToken(f"ID token rejected: {exc}") from exc
    if claims.get("token_use") != "id":
        raise InvalidIdToken("the presented token is not an ID token")
    return claims


def identity_from_claims(claims: Mapping) -> dict:
    """The ``identity`` object the chat processors read, built from verified claims.

    The same shape the REST resolvers receive. ``email`` is the raw claim with no
    substitute, because Chat-with-Document's scope lookup joins on it.
    """
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    out = {"cognito:groups": list(groups), "sub": claims["sub"]}
    for key in ("email", "cognito:username"):
        if claims.get(key):
            out[key] = claims[key]
    return {"claims": out}


def verified_identity(
    token: str, environ: Mapping[str, str] = os.environ, jwks_client=None
) -> dict:
    """Verify ``token`` against this deployment's pool and return the identity."""
    claims = verify_id_token(
        token,
        issuer=environ.get("COGNITO_ISSUER", ""),
        client_id=environ.get("COGNITO_APP_CLIENT_ID", ""),
        jwks_client=jwks_client,
    )
    return identity_from_claims(claims)
