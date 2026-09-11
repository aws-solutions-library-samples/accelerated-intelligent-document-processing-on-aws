#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Dynamic (live) security test for a DEPLOYED Seller Entitlement Service.

Not a unit test — it needs a real deployed stack and real credentials, so it lives
outside the pytest suite and is run by hand (or from `make stacktest-seller`).

Why this exists in addition to the static tests
----------------------------------------------
`tests/test_template_security.py` asserts what the *template* says. This asserts
what the *deployed stage* actually does. The two can differ: a console edit, a
stage-level authorizer override, or a partially-applied change set can leave
`AWS_IAM` in the template and anonymous access in production. The single most
valuable assertion here is the first one — an **unsigned request must be refused** —
and only a live call can make it.

What it checks
--------------
  1. Unsigned request                        -> 403 (authorizer is really on)
  2. Signed, NOT entitled                    -> 403 not_entitled, no internal detail
  3. Unknown productId                       -> byte-identical to (2), no oracle
  4. Malformed body (`0`)                    -> 400, not 502
  5. Oversized body                          -> 413, not 502
  5b. 15 hostile payloads (injection, CRLF,   -> 4xx, never 5xx or a hang
      null bytes, deep nesting, wrong types)
  6. Signed, entitled (optional)             -> 200; token verifies against the
                                                published public key, is bound to
                                                the calling account, carries `kid`,
                                                and expires in the future

What it CANNOT check
--------------------
Whether the service is actually configured to serve your product. Check (3) is the
reason: "unknown product" is deliberately byte-identical to "not entitled", so a
service whose product registry is empty or mangled passes every check here
perfectly while refusing every paying customer. That happened for real — SAM
truncated the registry parameter to `{` — and this suite reported all-green
against a completely non-functional endpoint.

The registry is therefore verified where it can be seen rather than inferred:
`idp-feature-cli seller-service deploy` reads it back off the deployed function
and fails the deploy if it did not survive (`verify_deployed_registry`). A useful
second signal is the roster — a *known* product that is merely unsubscribed gets
recorded with reason "no active agreement", whereas an unknown product is refused
before anything is written, so `seller-service activations` staying empty after a
real attempt means the product is not registered.

Usage:
    # (2)-(5) with credentials for an UNSUBSCRIBED account:
    python dynamic_activation_test.py \\
        --endpoint https://xxxx.execute-api.us-east-1.amazonaws.com/prod/activate \\
        --product-id prod-a5ee62vs2xa72

    # add (6) by also giving it a SUBSCRIBED account's profile:
    python dynamic_activation_test.py --endpoint ... --product-id ... \\
        --entitled-profile my-subscribed-account \\
        --public-key-file seller-public-key.der

Exits non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from typing import Optional

try:
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
except ImportError:  # pragma: no cover
    sys.exit("boto3/botocore are required: pip install boto3")

GREEN, RED, YELLOW, NC = "\033[0;32m", "\033[0;31m", "\033[1;33m", "\033[0m"

_failures: list[str] = []


def ok(msg: str) -> None:
    print(f"{GREEN}✓{NC} {msg}")


def bad(msg: str) -> None:
    _failures.append(msg)
    print(f"{RED}✗ {msg}{NC}")


def warn(msg: str) -> None:
    print(f"{YELLOW}! {msg}{NC}")


def _post(url: str, body: str, session: Optional[object], region: str):
    """POST `body`, SigV4-signed when a session is given. Returns (status, text)."""
    request = AWSRequest(
        method="POST",
        url=url,
        data=body.encode(),
        headers={"Content-Type": "application/json"},
    )
    if session is not None:
        credentials = session.get_credentials()  # type: ignore[attr-defined]
        SigV4Auth(credentials, "execute-api", region).add_auth(request)
    req = urllib.request.Request(
        url, data=body.encode(), headers=dict(request.headers), method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310 - SigV4-signed POST to the deployed service URL under test
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except urllib.error.URLError as exc:
        return 0, f"connection error: {exc}"


def check_unsigned_is_refused(url: str, product_id: str, region: str) -> None:
    status, text = _post(url, json.dumps({"productId": product_id}), None, region)
    if status == 403:
        ok("unsigned request refused (403) — the AWS_IAM authorizer is live")
    elif status == 200:
        bad(
            "UNSIGNED REQUEST WAS ACCEPTED (200). The endpoint is anonymous: the "
            "Lambda reads the buyer account from requestContext.identity, which "
            "API Gateway only populates for a verified SigV4 caller, so tokens "
            "are being minted for unauthenticated callers."
        )
    else:
        bad(f"unsigned request returned {status} (expected 403): {text[:200]}")


def check_not_entitled(url: str, product_id: str, session, region: str) -> str:
    status, text = _post(url, json.dumps({"productId": product_id}), session, region)
    if status != 403:
        bad(f"unentitled account got {status}, expected 403: {text[:200]}")
        return text
    for leak in ("AccessDenied", "ValidationException", "arn:aws", "Traceback"):
        if leak in text:
            bad(f"403 body leaks internal detail ({leak!r}) to an arbitrary caller")
            return text
    ok("unentitled account refused (403) with no internal detail leaked")
    return text


def check_no_product_oracle(url: str, refused_body: str, session, region: str) -> None:
    status, text = _post(
        url, json.dumps({"productId": "prod-definitely-not-real"}), session, region
    )
    if status == 403 and text == refused_body:
        ok("unknown product is byte-identical to not-entitled — no existence oracle")
    else:
        bad(
            f"unknown product is distinguishable (status={status}). A caller can "
            f"enumerate which products exist, including listings still in Limited "
            f"state whose productId is not public. Body: {text[:160]}"
        )


def check_malformed_body(url: str, session, region: str) -> None:
    status, text = _post(url, "0", session, region)
    if status == 400:
        ok("malformed body rejected (400)")
    elif status in (500, 502):
        bad(
            f"malformed one-byte body caused a {status} — the handler is crashing on "
            "attacker-controlled input (json.loads returns a non-dict)."
        )
    else:
        bad(f"malformed body returned {status} (expected 400): {text[:160]}")


def check_oversized_body(url: str, product_id: str, session, region: str) -> None:
    body = json.dumps({"productId": product_id, "pad": "x" * 50000})
    status, text = _post(url, body, session, region)
    if status == 413:
        ok("oversized body rejected (413) before parsing")
    elif status in (500, 502):
        bad(f"oversized body caused a {status} — it should be refused, not crash")
    else:
        warn(f"oversized body returned {status} (expected 413): {text[:120]}")


# A trimmed version of the offline corpus in test_payload_robustness.py. Run
# against the DEPLOYED stage so the assertion covers API Gateway's own parsing and
# limits, not just the handler's.
_HOSTILE_PAYLOADS = [
    ("json number", "0"),
    ("json null", "null"),
    ("json array", "[1,2,3]"),
    ("not json", "not json at all"),
    ("truncated", '{"productId":'),
    ("numeric productId", '{"productId": 12345}'),
    ("bool productId", '{"productId": true}'),
    ("list productId", '{"productId": ["x"]}'),
    ("deep nesting", "[" * 300 + "]" * 300),
    ("crlf in productId", json.dumps({"productId": "prod-x\r\nforged"})),
    ("null byte", json.dumps({"productId": "prod-x\u0000admin"})),
    ("xss", json.dumps({"productId": "<script>alert(1)</script>"})),
    ("sql-ish", json.dumps({"productId": "x' OR '1'='1"})),
    ("format specifiers", json.dumps({"productId": "%s%s%n"})),
    ("emoji", json.dumps({"productId": "prod-\U0001f512"})),
]


def check_hostile_payloads(url: str, session, region: str) -> None:
    """No hostile payload may 5xx or hang. This is the class of bug that WAS
    present: a one-byte body crashed the handler into a 502."""
    problems = []
    for label, body in _HOSTILE_PAYLOADS:
        status, text = _post(url, body, session, region)
        if status == 0:
            problems.append(f"{label}: connection failed ({text[:60]})")
        elif status >= 500:
            problems.append(f"{label}: {status}")
        elif status not in (400, 403, 413):
            problems.append(f"{label}: unexpected {status}")
    if problems:
        for problem in problems:
            bad(f"hostile payload not handled cleanly — {problem}")
    else:
        ok(f"{len(_HOSTILE_PAYLOADS)} hostile payloads all refused cleanly (no 5xx)")


def check_entitled(
    url: str, product_id: str, session, region: str, public_key_file: Optional[str]
) -> None:
    status, text = _post(url, json.dumps({"productId": product_id}), session, region)
    if status != 200:
        bad(f"entitled account got {status}, expected 200: {text[:200]}")
        return
    body = json.loads(text)
    claims = json.loads(base64.b64decode(body["token"]))
    account = session.client("sts").get_caller_identity()["Account"]

    if claims.get("buyerAccountId") != account:
        bad(
            f"token is bound to {claims.get('buyerAccountId')} but the caller is "
            f"{account} — a token usable by the wrong account"
        )
    else:
        ok(f"token bound to the calling account ({account})")

    if not claims.get("kid"):
        bad("token has no `kid` — the signing key cannot be rotated")
    else:
        ok("token carries a `kid` for key rotation")

    import time

    if claims.get("exp", 0) <= time.time():
        bad("token is already expired on issue")
    else:
        ok(f"token expires at {body['expiresAt']}")

    if not public_key_file:
        warn(
            "signature NOT verified — pass --public-key-file (DER from "
            "`aws kms get-public-key`) to check it cryptographically"
        )
        return
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        warn("`cryptography` not installed — skipping signature verification")
        return

    from cryptography.hazmat.primitives.asymmetric import rsa

    with open(public_key_file, "rb") as fh:
        public_key = serialization.load_der_public_key(fh.read())
    # The service signs with RSASSA_PSS_SHA_256, so the key must be RSA. Checking
    # rather than assuming: an ECC key here would otherwise fail with a confusing
    # signature error instead of "you gave me the wrong key".
    if not isinstance(public_key, rsa.RSAPublicKey):
        bad(f"{public_key_file} is not an RSA public key ({type(public_key).__name__})")
        return
    try:
        public_key.verify(
            base64.b64decode(body["signature"]),
            base64.b64decode(body["token"]),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256(),
        )
        ok("token signature verifies against the published public key")
    except Exception as exc:  # noqa: BLE001
        bad(f"token signature did NOT verify: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="Full /activate URL")
    parser.add_argument("--product-id", required=True, help="A registered prod-… id")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--profile",
        default=None,
        help="Profile for an UNSUBSCRIBED account (checks 2-5)",
    )
    parser.add_argument(
        "--entitled-profile",
        default=None,
        help="Profile for a SUBSCRIBED account (adds check 6)",
    )
    parser.add_argument(
        "--public-key-file",
        default=None,
        help="DER public key, to verify the signature",
    )
    args = parser.parse_args()

    print(f"Target: {args.endpoint}\n")

    check_unsigned_is_refused(args.endpoint, args.product_id, args.region)

    session = (
        boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    )
    caller = session.client("sts").get_caller_identity()["Account"]
    print(f"\nSigned checks as account {caller}:")
    refused_body = check_not_entitled(
        args.endpoint, args.product_id, session, args.region
    )
    check_no_product_oracle(args.endpoint, refused_body, session, args.region)
    check_malformed_body(args.endpoint, session, args.region)
    check_oversized_body(args.endpoint, args.product_id, session, args.region)
    check_hostile_payloads(args.endpoint, session, args.region)

    if args.entitled_profile:
        entitled_session = boto3.Session(profile_name=args.entitled_profile)
        entitled_account = entitled_session.client("sts").get_caller_identity()[
            "Account"
        ]
        print(f"\nEntitled checks as account {entitled_account}:")
        check_entitled(
            args.endpoint,
            args.product_id,
            entitled_session,
            args.region,
            args.public_key_file,
        )
    else:
        warn(
            "\nno --entitled-profile given — the positive path (token issued and "
            "verifiable) was NOT tested"
        )

    print()
    if _failures:
        print(f"{RED}{len(_failures)} check(s) failed:{NC}")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print(f"{GREEN}All checks passed.{NC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
