# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Verify a Cognito user-pool ID token and return its claims.

The REST API never needs this: API Gateway's Cognito authorizer verifies the
token before the dispatcher runs and hands it the claims. A Lambda Function URL
has no such authorizer — ``AuthType=AWS_IAM`` proves the request was signed by
credentials from the Identity Pool, and those credentials are the same for every
user of the pool — so an endpoint served from one verifies the caller's ID token
itself, with the checks the authorizer makes:

* the signature, against the user pool's published JSON Web Key Set (RS256 only);
* ``iss`` is the user pool;
* ``aud`` is the app client the web UI signs in with;
* ``token_use`` is ``id`` (an access token has no ``aud`` and no ``email``);
* ``exp`` is in the future, and ``sub`` is present.

The key set is fetched from the pool's ``/.well-known/jwks.json`` and cached per
process, so a warm Lambda fetches it once per hour at most.
"""

import threading
from typing import Any, Dict, Optional

#: How long a fetched key set is reused, in seconds. Cognito rotates signing keys
#: rarely and publishes the new key before signing with it.
_JWKS_CACHE_SECONDS = 3600

_clients: Dict[str, Any] = {}
_clients_lock = threading.Lock()


class IdTokenRejected(Exception):
    """The presented token is not a valid ID token for this user pool."""


def jwks_url(issuer: str) -> str:
    """The JSON Web Key Set URL Cognito publishes for ``issuer``."""
    return f"{issuer.rstrip('/')}/.well-known/jwks.json"


def _jwks_client(issuer: str):
    import jwt

    url = jwks_url(issuer)
    with _clients_lock:
        client = _clients.get(url)
        if client is None:
            client = jwt.PyJWKClient(
                url, cache_keys=True, lifespan=_JWKS_CACHE_SECONDS, timeout=5
            )
            _clients[url] = client
        return client


def verify_id_token(
    token: str,
    *,
    issuer: str,
    audience: str,
    jwks_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Return the claims of ``token`` after verifying it, or raise ``IdTokenRejected``.

    Args:
        token: the compact JWT the browser presented.
        issuer: ``https://cognito-idp.<region>.amazonaws.com/<user-pool-id>``.
        audience: the app client id the token must have been issued to.
        jwks_client: a ``jwt.PyJWKClient``-compatible object; the cached client
            for ``issuer`` when omitted. Tests pass one built from a local key.
    """
    import jwt

    if not token:
        raise IdTokenRejected("no token presented")
    if not issuer or not audience:
        raise IdTokenRejected("token verification is not configured")
    client = jwks_client or _jwks_client(issuer)
    try:
        signing_key = client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "token_use"]},
        )
    except jwt.PyJWTError as error:
        raise IdTokenRejected(f"token rejected: {type(error).__name__}") from error
    if claims.get("token_use") != "id":
        raise IdTokenRejected("token is not an ID token")
    if not claims.get("sub"):
        raise IdTokenRejected("token carries no subject")
    return claims
