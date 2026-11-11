# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the ID-token verification behind the chat streaming endpoint.

Tokens are signed with a key generated here and verified through a stand-in for
the JWKS client, so nothing reaches Cognito.
"""

from __future__ import annotations

import os
import sys
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import id_token  # noqa: E402

pytestmark = pytest.mark.unit

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_EXAMPLE"
CLIENT_ID = "exampleclientid"
SUB = "11111111-2222-3333-4444-555555555555"
USE_CLAIM = "token_use"


class _SigningKey:
    def __init__(self, key):
        self.key = key


class _Jwks:
    def __init__(self, public_key):
        self.public_key = public_key
        self.calls = 0

    def get_signing_key_from_jwt(self, token):
        self.calls += 1
        return _SigningKey(self.public_key)


@pytest.fixture(scope="module")
def keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, other


def _claims(**overrides):
    now = int(time.time())
    claims = {
        "sub": SUB,
        "iss": ISSUER,
        "aud": CLIENT_ID,
        USE_CLAIM: "id",
        "iat": now,
        "exp": now + 3600,
        "email": "analyst@example.com",
        "cognito:username": "analyst",
        "cognito:groups": ["Author"],
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _token(private, **overrides):
    return jwt.encode(_claims(**overrides), private, algorithm="RS256")


def _verify(token, keys, **kwargs):
    private, _ = keys
    return id_token.verify_id_token(
        token,
        issuer=kwargs.pop("issuer", ISSUER),
        client_id=kwargs.pop("client_id", CLIENT_ID),
        jwks_client=_Jwks(private.public_key()),
    )


def test_a_token_from_this_pool_and_client_is_accepted(keys):
    claims = _verify(_token(keys[0]), keys)
    assert claims["sub"] == SUB
    assert claims["cognito:groups"] == ["Author"]


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("another app client", {"aud": "someotherclient"}),
        ("another pool", {"iss": ISSUER.replace("EXAMPLE", "OTHER")}),
        ("expired", {"exp": int(time.time()) - 60}),
        ("an access token", {USE_CLAIM: "access"}),
        ("no token_use", {USE_CLAIM: None}),
        ("no sub", {"sub": None}),
    ],
)
def test_a_token_that_is_not_this_pools_id_token_is_refused(keys, label, overrides):
    with pytest.raises(id_token.InvalidIdToken):
        _verify(_token(keys[0], **overrides), keys)


def test_a_token_signed_by_another_key_is_refused(keys):
    _, other = keys
    with pytest.raises(id_token.InvalidIdToken):
        _verify(_token(other), keys)


def test_a_token_with_an_altered_payload_is_refused(keys):
    header, _, signature = _token(keys[0]).split(".")
    forged = jwt.encode(_claims(**{"cognito:groups": ["Admin"]}), keys[0], "RS256")
    with pytest.raises(id_token.InvalidIdToken):
        _verify(".".join([header, forged.split(".")[1], signature]), keys)


def test_an_unsigned_or_symmetric_token_is_refused(keys):
    unsigned = jwt.encode(_claims(), key=None, algorithm="none")
    symmetric = jwt.encode(
        _claims(), "a-shared-secret-long-enough-for-hs256", algorithm="HS256"
    )
    for token in (unsigned, symmetric):
        with pytest.raises(id_token.InvalidIdToken):
            _verify(token, keys)


@pytest.mark.parametrize("token", ["", "not-a-jwt"])
def test_a_missing_or_malformed_token_is_refused(keys, token):
    with pytest.raises(id_token.InvalidIdToken):
        _verify(token, keys)


@pytest.mark.parametrize(("issuer", "client_id"), [("", CLIENT_ID), (ISSUER, "")])
def test_verification_without_configuration_refuses_rather_than_accepts(
    keys, issuer, client_id
):
    with pytest.raises(id_token.InvalidIdToken, match="not configured"):
        _verify(_token(keys[0]), keys, issuer=issuer, client_id=client_id)


def test_the_jwks_client_failing_is_a_refusal(keys):
    class _Down:
        def get_signing_key_from_jwt(self, token):
            raise jwt.PyJWKClientConnectionError("unreachable")

    with pytest.raises(id_token.InvalidIdToken):
        id_token.verify_id_token(
            _token(keys[0]), issuer=ISSUER, client_id=CLIENT_ID, jwks_client=_Down()
        )


def test_the_identity_carries_the_claims_the_processors_read(keys):
    jwks = _Jwks(keys[0].public_key())
    identity = id_token.verified_identity(
        _token(keys[0]),
        environ={"COGNITO_ISSUER": ISSUER, "COGNITO_APP_CLIENT_ID": CLIENT_ID},
        jwks_client=jwks,
    )
    assert identity == {
        "claims": {
            "cognito:groups": ["Author"],
            "sub": SUB,
            "email": "analyst@example.com",
            "cognito:username": "analyst",
        }
    }
    assert jwks.calls == 1


def test_a_single_group_claim_string_becomes_a_list():
    identity = id_token.identity_from_claims({"sub": SUB, "cognito:groups": "Viewer"})
    assert identity["claims"]["cognito:groups"] == ["Viewer"]


def test_an_absent_email_is_left_absent_rather_than_substituted():
    identity = id_token.identity_from_claims({"sub": SUB, "cognito:username": "u"})
    assert "email" not in identity["claims"]
    assert identity["claims"]["cognito:groups"] == []


def test_the_jwks_url_is_the_pools_published_key_set():
    id_token._jwks_clients.clear()
    client = id_token._jwks_client(ISSUER)
    assert client.uri == f"{ISSUER}/.well-known/jwks.json"
    assert id_token._jwks_client(ISSUER) is client
    id_token._jwks_clients.clear()
