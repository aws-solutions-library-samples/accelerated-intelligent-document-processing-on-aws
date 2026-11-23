# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Sign a user in to a Cognito user pool with ``USER_SRP_AUTH``, without a browser.

The web UI app clients on both sides of a Terraform migration allow only
``ALLOW_USER_SRP_AUTH`` (plus refresh), so a test that has to prove "this user
still signs in with the old password" cannot use ``USER_PASSWORD_AUTH`` and cannot
change the client to allow it without changing the thing it is testing. This
module is the client half of Cognito's SRP-6a exchange, the same computation the
Amplify library in ``src/ui`` performs (``@aws-amplify/auth``,
``providers/cognito/utils/srp``), written with the standard library only.

The arithmetic is split from the AWS calls so it can be unit-tested offline:
:func:`password_claim` takes the challenge parameters and returns the response
parameters, and :func:`srp_sign_in` does the two ``cognito-idp`` calls around it.
Nothing here logs a password, a token or a derived key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

# RFC 5054's 3072-bit group, which is the one Cognito uses (Amplify's INIT_N).
N_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64"
    "ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6B"
    "F12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
    "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB31"
    "43DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF"
)
N = int(N_HEX, 16)
G = 2
INFO = b"Caldera Derived Key\x01"

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def pad_hex(value: int) -> str:
    """The unambiguous even-length hex of a non-negative integer: Java's
    ``BigInteger.toByteArray`` encoding, which is what Cognito hashes.

    A leading ``00`` is added when the most significant bit is set, so that the
    value cannot be read as negative.
    """
    if value < 0:
        raise ValueError("SRP values are non-negative")
    text = format(value, "x")
    if len(text) % 2:
        text = "0" + text
    if text[0] in "89abcdef":
        text = "00" + text
    return text


def hash_hex(hex_text: str) -> str:
    """SHA-256 of the bytes a hex string encodes, as 64 hex characters."""
    return hashlib.sha256(bytes.fromhex(hex_text)).hexdigest()


def multiplier() -> int:
    """``k = H(N | g)``, each padded."""
    return int(hash_hex(pad_hex(N) + pad_hex(G)), 16)


def large_a(small_a: int) -> int:
    """``A = g^a mod N``. Cognito rejects ``A mod N == 0``, as SRP requires."""
    value = pow(G, small_a, N)
    if value % N == 0:
        raise ValueError("A mod N is zero; pick another a")
    return value


def scrambler(a_value: int, b_value: int) -> int:
    """``u = H(A | B)``, each padded."""
    return int(hash_hex(pad_hex(a_value) + pad_hex(b_value)), 16)


def hkdf_key(ikm: bytes, salt: bytes) -> bytes:
    """The 16-byte HKDF-SHA256 key Cognito derives from ``S`` and ``u``."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, INFO, hashlib.sha256).digest()[:16]


def timestamp(now: Optional[datetime] = None) -> str:
    """Cognito's TIMESTAMP: ``Tue Nov 4 09:05:03 UTC 2026``.

    The day of the month is NOT zero-padded (Amplify's ``getNowString``), while
    hours, minutes and seconds are. A zero-padded day fails the signature on the
    first nine days of every month, which is the kind of bug that passes a test
    run on the 10th.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return (
        f"{_WEEKDAYS[now.weekday()]} {_MONTHS[now.month - 1]} {now.day} "
        f"{now:%H:%M:%S} UTC {now.year}"
    )


def password_authentication_key(
    pool_name: str,
    user_id: str,
    password: str,
    salt_hex: str,
    b_value: int,
    small_a: int,
) -> bytes:
    """The HKDF key both sides derive; the client proves it knows it."""
    if b_value % N == 0:
        raise ValueError("the server's B mod N is zero; refusing the challenge")
    a_value = large_a(small_a)
    u = scrambler(a_value, b_value)
    if u == 0:
        raise ValueError("u is zero; refusing the challenge")
    identity = hashlib.sha256(f"{pool_name}{user_id}:{password}".encode()).hexdigest()
    x = int(hash_hex(pad_hex(int(salt_hex, 16)) + identity), 16)
    k = multiplier()
    s = pow((b_value - k * pow(G, x, N)) % N, small_a + u * x, N)
    return hkdf_key(bytes.fromhex(pad_hex(s)), bytes.fromhex(pad_hex(u)))


def password_claim(
    pool_id: str,
    password: str,
    challenge: Dict[str, str],
    small_a: int,
    now: Optional[datetime] = None,
) -> Dict[str, str]:
    """The ``PASSWORD_VERIFIER`` response parameters for ``RespondToAuthChallenge``.

    ``challenge`` is the ``ChallengeParameters`` Cognito returned (``SRP_B``,
    ``SALT``, ``SECRET_BLOCK``, ``USER_ID_FOR_SRP``). The pool *name* in every hash
    is the part of the pool id after the underscore.
    """
    pool_name = pool_id.split("_", 1)[1]
    user_id = challenge["USER_ID_FOR_SRP"]
    key = password_authentication_key(
        pool_name,
        user_id,
        password,
        challenge["SALT"],
        int(challenge["SRP_B"], 16),
        small_a,
    )
    stamp = timestamp(now)
    secret_block = challenge["SECRET_BLOCK"]
    message = (
        pool_name.encode()
        + user_id.encode()
        + base64.b64decode(secret_block)
        + stamp.encode()
    )
    signature = base64.b64encode(
        hmac.new(key, message, hashlib.sha256).digest()
    ).decode()
    return {
        "USERNAME": user_id,
        "PASSWORD_CLAIM_SECRET_BLOCK": secret_block,
        "PASSWORD_CLAIM_SIGNATURE": signature,
        "TIMESTAMP": stamp,
    }


def new_small_a() -> int:
    """A fresh 128-byte secret exponent, below N."""
    return secrets.randbits(1024) % N


def srp_sign_in(
    cognito: Any, pool_id: str, client_id: str, username: str, password: str
) -> Tuple[Dict[str, Any], str]:
    """Sign in with ``USER_SRP_AUTH``; return ``(AuthenticationResult, username
    Cognito reports)``.

    Raises the ``ClientError`` Cognito raises (``NotAuthorizedException`` for a
    wrong password), and ``RuntimeError`` when Cognito answers with a challenge
    other than ``PASSWORD_VERIFIER`` or with no tokens, because either means the
    sign-in did not complete.
    """
    small_a = new_small_a()
    start = cognito.initiate_auth(
        ClientId=client_id,
        AuthFlow="USER_SRP_AUTH",
        AuthParameters={"USERNAME": username, "SRP_A": format(large_a(small_a), "x")},
    )
    if start.get("ChallengeName") != "PASSWORD_VERIFIER":
        raise RuntimeError(
            f"expected the PASSWORD_VERIFIER challenge, got {start.get('ChallengeName')!r}"
        )
    params = start["ChallengeParameters"]
    answer = cognito.respond_to_auth_challenge(
        ClientId=client_id,
        ChallengeName="PASSWORD_VERIFIER",
        ChallengeResponses=password_claim(pool_id, password, params, small_a),
    )
    result = answer.get("AuthenticationResult")
    if not result or not result.get("IdToken"):
        raise RuntimeError(
            f"sign-in did not complete: challenge {answer.get('ChallengeName')!r}"
        )
    return result, params["USER_ID_FOR_SRP"]
