# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Refuse an outbound AWS request from a unit suite, rather than sending it.

One definition, loaded by path from the `conftest.py` of every suite that needs it —
the same arrangement `scripts/tests/first_party_provenance.py` uses, and for the same
reason: the alternative is a copy per suite, and a list of copies drifts.

## What this is for, measured

`make test` and a bare `pytest` issued requests signed with the developer's own AWS
credentials. Measured over the 69 registered test roots with
`botocore.endpoint.Endpoint._send` replaced by a recorder that appends the request
and then raises — a seam strictly below moto's `before-send` short-circuit, so a
record is exactly an escape, and below credential resolution, so the signing
credential can be classified by shape without transmitting it:

| root | would-be requests | signed with a real-shaped key |
|---|---|---|
| `lib/idp_common_pkg/tests` | 343 | 1 (Secrets Manager) |
| `nested/.../reprocess_document_resolver` | 18 | 18 (CloudWatch) |
| `benchmarks/tests` | 10 | 10 (S3) |
| `src/lambda/queue_sender` | 9 | 9 (CloudWatch) |
| `patterns/unified/tests` | 8 | 8 (CloudWatch) |
| `src/lambda/save_reporting_data` | 2 | 2 (DynamoDB) |

Not one test failed and nothing reported any of it, because every one of those calls
sits inside a production `except Exception` that logs and carries on. Issue #1398.

## Why stripping the environment is a different control, and not a substitute

`$(HERMETIC_AWS)` — and, since #1398, `scripts/run_all_tests.py` — takes the
machine's credential sources away, so botocore raises `NoCredentialsError` at signing
time and nothing leaves the process. That closed 47 of the 48 real-signed escapes
above. It cannot close the rest, and the two reasons are properties of the suites
rather than of the wrapper:

* a suite whose `conftest.py` installs **placeholder credentials** signs with the
  placeholder and still sends a real request to a real endpoint, which is rejected
  rather than prevented. Fifteen registered roots install them, and they do it on
  purpose: a handler that builds a boto3 client at import time with nothing
  resolvable can never sign, however many credentials appear later (#988).
* `patch.dict(os.environ, ..., clear=True)` — nine test modules use it — erases
  `AWS_SHARED_CREDENTIALS_FILE`, `AWS_CONFIG_FILE` and `AWS_EC2_METADATA_DISABLED`
  along with everything else. Those three are the only defence against the two
  sources no unset can reach, so for the duration of such a test the machine's real
  `[default]` profile is resolvable again. That is where the one surviving
  real-signed escape came from.

An in-process refusal is the only one of these controls that an environment-clearing
test cannot undo, and the only one that makes the *next* instance a loud failure
naming the URL instead of a call nobody sees.

## The seam, and what it deliberately does not cover

`botocore.httpsession.URLLib3Session.send` is the single point every botocore request
passes through on its way out. `moto` short-circuits earlier, on botocore's
`before-send` event, so a `mock_aws` test never reaches this and needs no special
case; a `MagicMock` or hand-rolled stub client never reaches it either. What does
reach it is exactly the mistake worth catching.

This replaces **botocore's** transport and nothing else, so a test that fetches an
ordinary URL through `requests` or `urllib` is unaffected. Widening it to all
outbound HTTP is a different change with a different blast radius, and every escape
measured in #1398 was AWS traffic.
"""

from __future__ import annotations

__all__ = ["fixture", "install", "refuse"]

MESSAGE = (
    "This test attempted a real network request to {url}. Use moto (mock_aws) or "
    "patch the client; see scripts/tests/no_outbound_aws.py."
)


def refuse(_session, request):
    """Stand in for `URLLib3Session.send`: raise, naming the endpoint."""
    raise RuntimeError(MESSAGE.format(url=getattr(request, "url", "<unknown>")))


def install(monkeypatch) -> None:
    """Replace botocore's transport with :func:`refuse` for one test.

    `monkeypatch` scopes it to the requesting test, so a run that spans the unit and
    integration tiers — a bare `pytest` with no `-m` — restores the real transport
    between them either way.
    """
    from botocore.httpsession import URLLib3Session

    monkeypatch.setattr(URLLib3Session, "send", refuse)


def fixture():
    """An autouse pytest fixture a `conftest.py` can bind to a name.

    Autouse rather than opt-in, because the tests that most need this are the ones
    that ask for no AWS fixture at all: every escape measured in #1398 came from a
    test that did not know it was making the call.

    ``integration``-marked tests are left alone. They exist to call real AWS and are
    run by `make test-integration` / `make test-integration-all`, which deliberately
    keep the machine's credentials and region; the marker is what says "this test
    calls AWS", and `scripts/run_all_tests.py` makes the same split.
    """
    import pytest

    @pytest.fixture(autouse=True)
    def no_outbound_aws(request, monkeypatch):
        if request.node.get_closest_marker("integration") is not None:
            return
        install(monkeypatch)

    return no_outbound_aws
