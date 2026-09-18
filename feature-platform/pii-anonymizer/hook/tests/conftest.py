# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Make this suite runnable on a machine with no AWS configuration at all.

Neither test file needs AWS — S3 is faked at the boundary and Bedrock is never
called — but `handler.py` constructs `boto3.client("s3")`,
`boto3.resource("dynamodb")` and `boto3.client("bedrock-runtime")` at **module
scope**, so simply importing it resolves a region. On a developer machine that
region comes silently from `~/.aws/config`; in CI there is no config file and no
`AWS_REGION`, so every test in this suite died at import with
`botocore.exceptions.NoRegionError: You must specify a region` the moment the
suite was added to `make test-packages-cicd` (#974).

Pinning a region and static fake credentials here fixes that for the whole
suite, and pins it in the other direction too: a developer whose shell carries a
real profile now gets the same construction as CI rather than a client wired to
their own account. Nothing in the suite makes a request, so the values are
arbitrary — they only have to exist.

The module-scope binding itself is the underlying defect and is left for a
follow-up: the tests patch methods **on** those client objects
(`monkeypatch.setattr(mod._s3, "copy_object", ...)`), so making the bindings lazy
the way `feature-api/handler.py` now does needs those call sites reworked as
well. The companion feature API suite had the same shape and a worse symptom —
the module-scope resource was bound before `mock_aws` started, which made a
DynamoDB test fail depending on test order and on the developer's AWS profile.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _aws_environment_is_not_the_developers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
