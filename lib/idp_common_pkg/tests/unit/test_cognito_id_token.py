# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``idp_common.cognito_id_token.verify_id_token``.

Tokens are minted here with a locally generated RSA key. The key set is served
through the real ``jwt.PyJWKClient`` with only its network fetch replaced, so key
selection by ``kid`` and the signature check are PyJWT's own code, not a double.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from idp_common import cognito_id_token
from idp_common.cognito_id_token import IdTokenRejected, jwks_url, verify_id_token

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_EXAMPLE"
CLIENT_ID = "exampleclientid"
KID = "kid-1"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNING_KEY = _key()
OTHER_KEY = _key()


def _jwks(private_key, kid=KID):
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return {"keys": [jwk]}


def _client(private_key=SIGNING_KEY, kid=KID):
    client = jwt.PyJWKClient(jwks_url(ISSUER), cache_keys=False)
    client.fetch_data = lambda: _jwks(private_key, kid)  # type: ignore[method-assign]
    return client


def _claims(**overrides):
    now = int(time.time())
    claims = {
        "sub": "11111111-2222-3333-4444-555555555555",
        "email": "a@example.com",
        "cognito:groups": ["Author"],
        "aud": CLIENT_ID,
        "iss": ISSUER,
        "token_use": "id",
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _token(claims=None, key=SIGNING_KEY, kid=KID, alg="RS256"):
    return jwt.encode(claims or _claims(), key, algorithm=alg, headers={"kid": kid})


def _verify(token, client=None):
    return verify_id_token(
        token, issuer=ISSUER, audience=CLIENT_ID, jwks_client=client or _client()
    )


@pytest.mark.unit
class TestAccepted:
    def test_a_valid_id_token_returns_its_claims(self):
        claims = _verify(_token())
        assert claims["sub"] == "11111111-2222-3333-4444-555555555555"
        assert claims["email"] == "a@example.com"
        assert claims["cognito:groups"] == ["Author"]

    def test_the_key_set_url_is_the_pools_well_known_path(self):
        assert jwks_url(ISSUER + "/") == f"{ISSUER}/.well-known/jwks.json"


@pytest.mark.unit
class TestRejected:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"aud": "another-client"},
            {"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OTHER"},
            {"exp": int(time.time()) - 10},
            # An access token: no aud, token_use=access.
            {"aud": None, "token_use": "access", "client_id": CLIENT_ID},
            {"token_use": "access"},
            {"sub": None},
            {"sub": ""},
            {"token_use": None},
        ],
        ids=[
            "wrong-audience",
            "wrong-issuer",
            "expired",
            "access-token",
            "token-use-access",
            "no-sub",
            "empty-sub",
            "no-token-use",
        ],
    )
    def test_a_token_with_the_wrong_claims(self, overrides):
        with pytest.raises(IdTokenRejected):
            _verify(_token(_claims(**overrides)))

    def test_a_token_signed_by_another_key_under_the_pools_kid(self):
        with pytest.raises(IdTokenRejected):
            _verify(_token(key=OTHER_KEY))

    def test_a_token_naming_a_key_the_pool_does_not_publish(self):
        with pytest.raises(IdTokenRejected):
            _verify(_token(kid="unknown-kid"))

    def test_an_unsigned_token(self):
        token = jwt.encode(_claims(), None, algorithm="none", headers={"kid": KID})
        with pytest.raises(IdTokenRejected):
            _verify(token)

    def test_a_symmetrically_signed_token(self):
        # Only RS256 is accepted. An HS256 token naming the pool's kid must not
        # verify, whatever secret it was signed with.
        hs = jwt.encode(_claims(), "secret", algorithm="HS256", headers={"kid": KID})
        with pytest.raises(IdTokenRejected):
            _verify(hs)

    def test_no_token(self):
        with pytest.raises(IdTokenRejected, match="no token"):
            _verify("")

    def test_garbage(self):
        with pytest.raises(IdTokenRejected):
            _verify("not.a.jwt")

    @pytest.mark.parametrize("issuer,audience", [("", CLIENT_ID), (ISSUER, "")])
    def test_unconfigured_verification_refuses(self, issuer, audience):
        with pytest.raises(IdTokenRejected, match="not configured"):
            verify_id_token(
                _token(), issuer=issuer, audience=audience, jwks_client=_client()
            )

    def test_a_key_set_that_cannot_be_fetched_refuses(self):
        client = jwt.PyJWKClient(jwks_url(ISSUER), cache_keys=False)

        def _down():
            raise jwt.PyJWKClientConnectionError("unreachable")

        client.fetch_data = _down  # type: ignore[method-assign]
        with pytest.raises(IdTokenRejected):
            _verify(_token(), client=client)


@pytest.mark.unit
def test_the_cached_client_is_reused_per_issuer():
    cognito_id_token._clients.clear()
    a = cognito_id_token._jwks_client(ISSUER)
    b = cognito_id_token._jwks_client(ISSUER)
    assert a is b
    cognito_id_token._clients.clear()
