# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Payload robustness (fuzz) tests for the activation handler.

Goal: **no hostile payload may crash the service or elicit a 5xx.** The endpoint
admits any authenticated AWS principal, so every input here is reachable by an
attacker who merely owns an AWS account.

This is the class of bug that was actually present: a one-byte body (`0`) produced
an unhandled `AttributeError` → 502 and a stack trace. A corpus like this one
would have caught it immediately, which is why it is now a permanent test rather
than a note.

Deliberately at the **handler** level rather than over HTTP, because:
  * it runs offline in CI on every commit — no deploy, no credentials, no cost;
  * a DAST scanner cannot reach this code at all. Every unsigned request is
    rejected by API Gateway's `AWS_IAM` authorizer with 403 before the Lambda is
    invoked, so an unauthenticated scanner can only ever observe 403s. Reaching
    the parser requires SigV4, which is what these tests simulate directly.
The deployed stage gets the same corpus over signed HTTP via
`dynamic_activation_test.py`.

Invariants asserted for every payload:
  1. the handler never raises;
  2. it returns a well-formed response (int status, JSON body);
  3. the status is never 5xx;
  4. attacker-controlled input is never reflected into the response body.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_LAMBDA = Path(__file__).resolve().parents[1] / "lambdas" / "activate" / "index.py"

# Statuses the handler is allowed to produce. Anything else — especially 5xx — is
# a finding.
_ALLOWED = {200, 400, 401, 403, 413}


@pytest.fixture
def mod(monkeypatch):
    monkeypatch.setenv(
        "PRODUCT_REGISTRY_JSON",
        json.dumps({"prod-paid": {"productCode": "c", "allowFreeTier": False}}),
    )
    monkeypatch.setenv("SIGNING_KEY_ARN", "arn:aws:kms:us-east-1:1:key/abc")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("ALLOWED_ACCOUNTS", "")
    spec = importlib.util.spec_from_file_location("robustness_activate", _LAMBDA)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # No real AWS. Entitlement always denied, so we exercise the parsing and
    # refusal paths without needing Marketplace.
    monkeypatch.setattr(module, "_has_active_agreement", lambda p, b: (False, "denied"))
    monkeypatch.setattr(module, "_ACTIVATIONS_TABLE", "")
    return module


def _event(body: str, account_id: str = "111111111111") -> dict:
    return {
        "requestContext": {"identity": {"accountId": account_id}},
        "body": body,
    }


def _assert_well_behaved(mod, body: str, label: str) -> dict:
    """The four invariants, for one payload."""
    try:
        resp = mod.handler(_event(body), None)
    except Exception as exc:  # noqa: BLE001 - that IS the failure
        pytest.fail(
            f"{label}: handler raised {type(exc).__name__}: {exc}. Any unhandled "
            "exception is a 502 plus a stack trace, triggerable by any AWS account."
        )
    assert isinstance(resp, dict), f"{label}: response is not a dict"
    status = resp.get("statusCode")
    assert isinstance(status, int), f"{label}: statusCode is not an int: {status!r}"
    assert status < 500, f"{label}: server error {status} — hostile input must not 5xx"
    assert status in _ALLOWED, f"{label}: unexpected status {status}"
    json.loads(resp["body"])  # must be valid JSON
    return resp


# ---------------------------------------------------------------------------
# Malformed / wrong-type bodies
# ---------------------------------------------------------------------------

_MALFORMED = {
    "empty string": "",
    "whitespace": "   ",
    "not json": "not json at all",
    "truncated json": '{"productId":',
    "json array": "[1,2,3]",
    "json string": '"prod-paid"',
    "json number": "0",
    "json null": "null",
    "json true": "true",
    "nested array": "[[[[[1]]]]]",
    "empty object": "{}",
    "null productId": '{"productId": null}',
    "numeric productId": '{"productId": 12345}',
    "list productId": '{"productId": ["prod-paid"]}',
    "dict productId": '{"productId": {"a": "b"}}',
    "bool productId": '{"productId": true}',
    "duplicate keys": '{"productId": "prod-paid", "productId": "prod-other"}',
    "unicode escape": '{"productId": "\\ud83d\\ude00"}',
    "bom prefix": '﻿{"productId": "prod-paid"}',
    "trailing garbage": '{"productId": "prod-paid"} trailing',
}


@pytest.mark.parametrize("label,body", sorted(_MALFORMED.items()))
def test_malformed_bodies_never_5xx(mod, label, body):
    _assert_well_behaved(mod, body, label)


# ---------------------------------------------------------------------------
# Hostile productId values. The registry is an allowlist, so these should all be
# refused — but they must be refused CLEANLY, and must not reach DynamoDB keys,
# CloudWatch dimensions, or the response body.
# ---------------------------------------------------------------------------

_HOSTILE_IDS = {
    "sql injection": "prod-x' OR '1'='1",
    "nosql injection": '{"$ne": null}',
    "path traversal": "../../../../etc/passwd",
    "command substitution": "$(whoami)",
    "backticks": "`id`",
    "shell metachars": "prod-x; rm -rf /",
    "null byte": "prod-x\x00admin",
    "crlf log forging": "prod-x\r\nINFO: forged log line",
    "newline log forging": "prod-x\nGranted: everything",
    "format specifiers": "%s%s%s%n",
    "python format": "{0.__class__}",
    "jinja template": "{{ 7*7 }}",
    "xss": "<script>alert(1)</script>",
    "xxe": '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>',
    # The invisible/confusable payloads are written as escapes rather than literal
    # characters. The runtime string is byte-identical, but the source stays
    # readable and honest: a literal U+202E makes the file *display* differently
    # from how it executes ("Trojan Source"), which is a hazard for whoever reviews
    # this corpus next and is flagged as HIGH by Bandit B613. Escaping is the fix,
    # not a suppression.
    "unicode rtl override": "prod-\u202egnimda",  # RIGHT-TO-LEFT OVERRIDE
    "zero width": "prod-\u200bpaid",  # ZERO WIDTH SPACE
    "homoglyph": "prod-\u0440\u0430id",  # Cyrillic er/a, confusable with p/a
    # Long, but under the 4 KB body cap — otherwise this exercises the size
    # guard (413) instead of the productId path it is meant to test.
    "long": "prod-" + "a" * 2000,
    "ddb reserved": "prod-x#:.",
    "json in string": '{"productId": "x"}',
    "emoji": "prod-🔓",
    "control chars": "prod-\x01\x02\x03",
    "tab": "prod-\tpaid",
    "only whitespace": "     ",
    "negative": "-1",
}


@pytest.mark.parametrize("label,product_id", sorted(_HOSTILE_IDS.items()))
def test_hostile_product_ids_are_refused_cleanly(mod, label, product_id):
    resp = _assert_well_behaved(mod, json.dumps({"productId": product_id}), label)
    # None of these is in the registry, so all must be refused.
    assert resp["statusCode"] in (400, 403), f"{label}: unexpectedly accepted"


@pytest.mark.parametrize("label,product_id", sorted(_HOSTILE_IDS.items()))
def test_hostile_product_ids_are_not_reflected_in_the_response(mod, label, product_id):
    """No echo of attacker input.

    Reflection is how a payload reaches a downstream consumer — a log viewer, a
    dashboard, or an extension author's debug output. The refusal body is a fixed
    string precisely so nothing crosses back.
    """
    resp = mod.handler(_event(json.dumps({"productId": product_id})), None)
    body = resp["body"]
    # Compare on a distinctive slice; short/degenerate inputs can appear by chance.
    probe = product_id.strip()[:24]
    if len(probe) >= 6:
        assert probe not in body, (
            f"{label}: input reflected into the response body: {body[:200]}"
        )


# ---------------------------------------------------------------------------
# Structural abuse
# ---------------------------------------------------------------------------


def test_deeply_nested_json_does_not_blow_the_stack(mod):
    """A recursive-descent parser can hit the recursion limit; that must surface
    as a 400, not an unhandled RecursionError."""
    body = "[" * 400 + "]" * 400
    _assert_well_behaved(mod, body, "deeply nested")


def test_huge_body_is_refused_before_parsing(mod):
    body = json.dumps({"productId": "prod-paid", "pad": "x" * 100_000})
    resp = _assert_well_behaved(mod, body, "huge body")
    assert resp["statusCode"] == 413


def test_many_keys_does_not_degrade(mod):
    """A body full of keys must be bounded by the size cap, not by key count."""
    body = json.dumps({f"k{i}": i for i in range(5000)})
    _assert_well_behaved(mod, body, "many keys")


def test_huge_numeric_values(mod):
    _assert_well_behaved(mod, json.dumps({"productId": 10**500}), "huge int")
    _assert_well_behaved(mod, '{"productId": 1e309}', "float overflow")


def test_missing_request_context_is_401_not_a_crash(mod):
    """A malformed invocation (no identity) must refuse, never mint."""
    for event in (
        {},
        {"requestContext": {}},
        {"requestContext": {"identity": {}}},
        {"requestContext": None, "body": "{}"},
        {"requestContext": {"identity": None}, "body": "{}"},
    ):
        try:
            resp = mod.handler(event, None)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"handler raised on {event!r}: {type(exc).__name__}: {exc}")
        assert resp["statusCode"] in (400, 401), f"{event!r} -> {resp['statusCode']}"
        assert "token" not in resp["body"]


def test_body_absent_entirely(mod):
    resp = mod.handler({"requestContext": {"identity": {"accountId": "1" * 12}}}, None)
    assert resp["statusCode"] in (400, 403)


def test_metric_dimensions_never_carry_attacker_input(mod, monkeypatch, caplog):
    """CloudWatch dimension values must come from the registry, not the request.

    An unknown product is refused *before* any metric is emitted, so a hostile
    productId can never become an EMF dimension value.
    """
    import logging as _logging

    emitted: list = []
    monkeypatch.setattr(
        mod, "_emit_activation_metric", lambda pid, outcome: emitted.append(pid)
    )
    with caplog.at_level(_logging.INFO):
        for product_id in _HOSTILE_IDS.values():
            mod.handler(_event(json.dumps({"productId": product_id})), None)
    assert emitted == [], f"hostile productId reached a metric dimension: {emitted[:3]}"
