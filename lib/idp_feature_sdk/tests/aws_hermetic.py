# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Make this suite's AWS environment hermetic, from inside the suite.

``HERMETIC_AWS`` in ``make/hermetic_aws.mk`` strips the machine's AWS environment
from every pytest invocation the gated recipes make, so an offline suite that
reaches a real endpoint fails loudly rather than quietly transacting against
whichever account the developer happens to be signed in to. A bare ``pytest``
typed in this directory goes through no such wrapper, and this module is the same
control applied in-process so that the two runs agree.

A ``monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")`` is not that control, and
the gap is not a matter of degree — it leaves three of the four sources standing:

* **the shared config and credentials files**, which no amount of unsetting
  reaches. A developer machine's ``[default]`` profile is a resolvable credential
  with no ``AWS_*`` variable set at all, so they are pointed at ``os.devnull``.
* **the instance metadata service**, which a developer box on EC2 can reach and a
  CI runner cannot. Clearing environment variables does nothing to it.
* **the rest of the environment**, chiefly ``AWS_PROFILE`` and
  ``AWS_SESSION_TOKEN``. A stale session token left beside a fake access key does
  not make a request unsendable; it makes it a signed request to a real endpoint
  that happens to be rejected.

* **``boto3.DEFAULT_SESSION``**, which is not a credential source but defeats all
  three. It is module-global, it is created by the first bare
  ``boto3.client(...)`` anywhere in the process, and it **freezes the credentials
  it resolved at that moment**. A fake credential applied afterwards is ignored by
  every later bare client in the same process, so which key a test signs with
  becomes a function of what ran before it in that worker — and under ``pytest
  -n`` the partition decides that afresh on every run. Nulling it around each test
  is what stops a session outliving the environment it was built for.

The placeholder credentials are reinstated after the stripping, which is the
division of labour ``make/hermetic_aws.mk`` describes: the wrapper takes the
machine's real sources away, and a suite that needs credentials to exist at all
supplies its own fake ones.
"""

from __future__ import annotations

import os

import boto3

#: Every AWS variable ``HERMETIC_AWS`` removes. Kept in the same order as the
#: makefile so the two can be read side by side; ``scripts/tests/
#: test_offline_suites_are_hermetic.py`` is what holds the makefile itself honest.
HERMETIC_UNSET = (
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
)

#: The three ``HERMETIC_AWS`` pins: the two file-based sources, and the metadata
#: service. ``os.devnull`` rather than a literal ``/dev/null`` so this reads the
#: same on any platform botocore runs on.
HERMETIC_SET = {
    "AWS_CONFIG_FILE": os.devnull,
    "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    "AWS_EC2_METADATA_DISABLED": "true",
}

#: Placeholder credentials, reinstated after the stripping. ``AWS_REGION`` is
#: deliberately left unset: botocore prefers it over ``AWS_DEFAULT_REGION``, and
#: pinning both would make a test that asserts how a region is *resolved* pass for
#: the wrong reason.
FAKE_CREDENTIALS = {
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
}


def reset_default_session() -> None:
    """Discard any cached module-global boto3 session.

    Separate from :func:`apply` because it is the half that has nothing to do
    with the environment: the next bare ``boto3.client(...)`` resolves
    credentials afresh only if there is no session left holding the previous
    answer.
    """
    boto3.DEFAULT_SESSION = None


def apply(monkeypatch) -> None:
    """Strip the ambient AWS environment and install the placeholders.

    ``monkeypatch`` scopes every change to the requesting test, so the
    environment a developer runs under is restored at teardown.
    """
    for name in HERMETIC_UNSET:
        monkeypatch.delenv(name, raising=False)
    for name, value in HERMETIC_SET.items():
        monkeypatch.setenv(name, value)
    for name, value in FAKE_CREDENTIALS.items():
        monkeypatch.setenv(name, value)
    reset_default_session()
