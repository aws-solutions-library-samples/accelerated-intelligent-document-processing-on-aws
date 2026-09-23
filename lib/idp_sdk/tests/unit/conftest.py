# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared fixtures for the ``idp_sdk`` unit suite.

The fixtures here exist for one reason: **CI has neither AWS credentials nor a
region, and a developer machine has both.** A test that builds a boto3 client at
run time therefore passes locally and fails in CI with `NoRegionError`, and the
repository's hermeticity gate does not catch it — that gate only checks that a
suite *collects* without a region, which happens long before any client is
constructed.

`aws_credentials` pins a region and dummy static credentials for the duration of
one test, and also neutralises the ambient profile and config files so a
developer's real `~/.aws` cannot be consulted. Request it from any test that
constructs a boto3 client or resource, whether or not the call is wrapped in
`moto.mock_aws`: moto needs the credentials to be present, and code that builds a
client *outside* a moto context needs the region.

The fixture is deliberately **not** autouse. Several files in this suite predate
it and define an equivalent fixture of their own, and an autouse fixture here
would silently change the environment every existing test in the directory runs
under.
"""

import sys

# Imported for its side effect: this binds the real distribution in
# `sys.modules` before any test can stub it, which is what makes the snapshot
# below able to restore it. See `restore_stubbed_top_level_modules`.
import boto3  # noqa: F401
import pytest

# The region every fixture below pins. us-east-1 is the one region for which S3
# rejects a `CreateBucketConfiguration`, so tests that create buckets should use
# `us-west-2` (or the `aws_region` value) and pass the location constraint.
AWS_TEST_REGION = "us-east-1"

# Modules in this suite that a test replaces in `sys.modules` wholesale, so the
# replacement can be undone. See `restore_stubbed_top_level_modules` below.
_STUBBABLE_MODULES = ("boto3", "cfnresponse")
# `cfnresponse` exists only inside a Lambda runtime, so it is legitimately absent
# here; `_ABSENT` records that so the restore removes the stub rather than
# installing it permanently.
_ABSENT = object()
_REAL_MODULES = {name: sys.modules.get(name, _ABSENT) for name in _STUBBABLE_MODULES}


@pytest.fixture(autouse=True)
def restore_stubbed_top_level_modules():
    """Undo a test's wholesale replacement of `boto3` in `sys.modules`.

    `test_webui_oauth_urls_handler.py` execs an inline CloudFormation handler
    outside Lambda, and to do that it assigns a `types.ModuleType` stub over
    `sys.modules["boto3"]` (and `["cfnresponse"]`) without restoring either. The
    stub has no `DEFAULT_SESSION`, which is precisely the attribute
    `moto.mock_aws.start()` patches, so **every** later `mock_aws` test in the
    same process fails with `AttributeError: <module 'boto3'> does not have the
    attribute 'DEFAULT_SESSION'` — a message that names moto and says nothing
    about the cause.

    Nothing surfaces today only because `test_webui_*` happens to sort last in
    this directory, so no moto test follows it. That is an ordering accident, not
    a property: it breaks under `-p randomly`, under a hand-picked selection, or
    as soon as a file sorting after `test_w` uses moto. Restoring the real module
    after every test makes the ordering irrelevant.

    Restoring *after* the test is safe for the leaking test itself: the handler
    module it builds captures the stub at `exec` time and keeps its own
    reference, so putting the real `boto3` back once the test has finished
    affects nothing it still depends on.
    """
    yield
    for name, module in _REAL_MODULES.items():
        if sys.modules.get(name, _ABSENT) is module:
            continue
        if module is _ABSENT:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


@pytest.fixture
def aws_credentials(monkeypatch):
    """Pin a region and dummy credentials, and detach from any real AWS config.

    A failure of a test that forgot this fixture looks like `NoRegionError` or
    `NoCredentialsError` in CI only, so the fixture is the difference between a
    suite that means the same thing in both places and one that does not.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", AWS_TEST_REGION)
    monkeypatch.setenv("AWS_REGION", AWS_TEST_REGION)
    # A developer machine has a populated ~/.aws; CI does not. Point both files at
    # os.devnull so the two environments resolve identically.
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    return AWS_TEST_REGION


@pytest.fixture
def aws_region():
    """The region `aws_credentials` pins, for tests that must name it."""
    return AWS_TEST_REGION
