#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``HERMETIC_AWS`` expressed once in Python, for the callers that are not ``make``.

``HERMETIC_AWS`` in ``make/hermetic_aws.mk`` is the definition of "the AWS
environment a CI runner has", and every pytest invocation written as a ``make``
recipe goes through it. Two callers cannot: ``scripts/run_all_tests.py`` and
``scripts/coverage_all.py`` build their own subprocess environments in Python and
spawn pytest themselves, so for them the stripping has to be a function.

Having that function in one place is the point of this module. It was written twice
before it was written once, and each copy was missed in turn:

* ``scripts/coverage_all.py`` ran every tree with the machine's AWS environment
  intact until #1377. The figures it produced were a ratchet's input, and leaving
  the environment readable made them **nondeterministic** — ``boto3.DEFAULT_SESSION``
  caches the credentials the first bare client in a process resolved, so whether a
  branch guarding a live AWS call is covered depended on which test ran first in
  each xdist worker.
* ``scripts/run_all_tests.py`` — what ``make test`` invokes, and the command a
  developer actually types — was missed, and ran until #1398 with nothing stripped
  at all. Measured over the registered roots in a bare environment, with botocore's
  real send replaced by a recorder: requests that would have been sent reached
  CloudWatch ``PutMetricData``, DynamoDB, S3 and Secrets Manager, signed with
  whatever credentials the machine could resolve, and most of them were **writes**.
  Every CI recipe wrapped its pytest in ``$(PYTEST_HERMETIC)``, so CI never saw any
  of it and the gates were green and correct — ``make test`` was simply not
  equivalent to what CI runs, in a dimension nobody was tracking.

Both of those are the same defect, and the shape of it is that each caller was
*outside the universe of the control* rather than carved out of it: nobody decided
they should be unprotected, the question was never asked about them. So the lists
below are spelled out here and **derived from the makefile in the test** —
``scripts/tests/test_offline_suites_are_hermetic.py`` reads ``HERMETIC_AWS`` out of
``make/hermetic_aws.mk`` and fails if this mapping stops covering it, and it also
intercepts each caller's own ``subprocess.run`` to check that the function is
actually applied to every invocation rather than merely available.

What this does **not** do is stop a request. Stripping the credentials means
botocore raises ``NoCredentialsError`` at signing time for a client built from the
neutralised environment, which is loud and local; but a suite whose ``conftest.py``
reinstates placeholder credentials — several here do, deliberately, because their
handlers build a client at import time — signs with the placeholder and still sends
the request to a real endpoint. Refusing the send is a separate control, and it
lives in the suites: the autouse ``no_outbound_aws`` fixture in the repository-root
``conftest.py`` and the equivalents in ``lib/idp_cli_pkg`` and
``lib/idp_feature_sdk``.
"""

from __future__ import annotations

import os

__all__ = ["HERMETIC_AWS_SET", "HERMETIC_AWS_UNSET", "hermetic_aws_env"]

#: Every AWS environment variable ``HERMETIC_AWS``'s ``-u`` list removes.
#:
#: Kept in the makefile's order so the two can be read side by side. The test derives
#: the makefile's list and fails if this one stops covering it, so the direction that
#: matters cannot happen quietly: a variable added to ``HERMETIC_AWS`` because it
#: turned out to be a credential source, and not added here, would leave both Python
#: callers reading that source while every ``make`` recipe no longer does.
HERMETIC_AWS_UNSET = (
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

#: The sources no amount of unsetting reaches: the shared config files, and IMDS.
#:
#: A developer machine's ``[default]`` profile is a resolvable credential with no
#: ``AWS_*`` variable set at all, and a developer box on EC2 can reach the instance
#: metadata service, which a runner cannot. Disabling IMDS is also what stops a
#: credential lookup from stalling for seconds per client when these run on EC2.
HERMETIC_AWS_SET = {
    "AWS_CONFIG_FILE": "/dev/null",
    "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
    "AWS_EC2_METADATA_DISABLED": "true",
}


def hermetic_aws_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` with the ambient AWS environment neutralised, as ``HERMETIC_AWS`` does.

    Mutates and returns ``env``, so it composes with a caller's other environment
    builders in either order.
    """
    for name in HERMETIC_AWS_UNSET:
        env.pop(name, None)
    env.update(HERMETIC_AWS_SET)
    return env


def hermetic_os_environ() -> dict[str, str]:
    """A neutralised copy of this process's own environment.

    The shape both callers want: they start from ``os.environ`` and hand the result
    to ``subprocess.run``. ``os.environ`` itself is never modified.
    """
    return hermetic_aws_env(dict(os.environ))
