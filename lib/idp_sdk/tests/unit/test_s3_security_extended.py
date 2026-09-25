# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The two fallback paths in ``idp_sdk._core.s3_security``.

``s3_security`` applies the ``EnforceSSLOnly`` bucket policy — deny ``s3:*`` when
``aws:SecureTransport`` is false — to buckets the CLIs create imperatively, since
those get no ``AWS::S3::BucketPolicy`` from CloudFormation.
``tests/unit/test_s3_enforce_ssl_only.py`` covers the statement shape, the merge
behaviour and the call sites. Two regions of the module are outside its reach,
and both are the *unhappy* branch of a two-branch decision, which is why neither
was exercised by a test written against the working path:

**The partition fallback** (``get_partition``, the ``except Exception`` arm).
``boto3.Session().get_partition_for_region`` is the primary answer, and on the
botocore in use it resolves ``us-gov-west-1`` and ``cn-north-1`` correctly — so
the existing parametrised test never enters the fallback. The fallback exists for
an older botocore, or a region name a current one does not know, and what it must
not do is answer ``"aws"`` for a GovCloud region: that yields
``arn:aws:s3:::bucket`` in a GovCloud account, an ARN naming no resource, so the
deny statement applies to nothing and the bucket silently accepts plaintext
traffic. The tests below reach the branch by making the primary lookup raise.

**The unreadable-existing-policy branch** (``apply_enforce_ssl_only``, the
``if "NoSuchBucketPolicy" not in str(exc): raise``). The existing suite covers a
denied ``PutBucketPolicy``; this is a denied or failing ``GetBucketPolicy``, which
is a different situation with a much worse wrong answer available. The module
merges its statement into whatever policy is already there. If the read fails and
the failure were treated as "no policy", the merge would start from empty and the
subsequent write would **replace an operator's entire bucket policy with one
statement** — deleting grants while reporting hardening as successful. So the
assertion that matters is not just that it raises: it is that nothing is written.

Both regions are reachable, and neither test needs a real failure injected into
botocore — one replaces the ``boto3`` name ``s3_security`` itself holds, the other
replaces one method on a real moto client, the pattern the existing file already
uses. Note which of those the first one is: the substitution is made on the
*module under test*, not on ``boto3``, so it cannot be observed by anything else
running in the same process. See ``_Boto3Shim``.
"""

from __future__ import annotations

import json

import boto3
import botocore.exceptions
import pytest
from moto import mock_aws

from idp_sdk._core.s3_security import (
    ENFORCE_SSL_SID,
    apply_enforce_ssl_only,
    get_partition,
)

pytestmark = pytest.mark.unit

REGION = "us-east-1"


# ---------------------------------------------------------------------------
# get_partition: the fallback when botocore cannot answer
# ---------------------------------------------------------------------------


class _UnhelpfulSession:
    """A ``boto3.Session`` whose partition lookup is unavailable.

    Stands in for an older botocore (no ``get_partition_for_region`` at all) and
    for a current one handed a region it does not know — both surface here as an
    exception from that call, which is the single branch condition.
    """

    def get_partition_for_region(self, region: str) -> str:
        raise AttributeError("get_partition_for_region is not available")


class _Boto3Shim:
    """Stands in for the ``boto3`` module, for this one module only.

    ``get_partition`` calls ``boto3.Session()``, so the obvious patch target is
    ``"idp_sdk._core.s3_security.boto3.Session"`` — but that path resolves
    *through* the module's ``boto3`` attribute and sets the name on the **shared
    ``boto3`` module object**, replacing ``Session`` for every caller in the
    process while the test runs. Patching the module's own ``boto3`` attribute
    with this shim keeps the substitution where it belongs; nothing outside
    ``idp_sdk._core.s3_security`` can observe it.
    """

    def __init__(self, session_factory):
        self._session_factory = session_factory

    def Session(self, *args, **kwargs):  # noqa: N802 - mirrors boto3's own name
        return self._session_factory()


def _patch_boto3(monkeypatch, session_factory) -> None:
    from idp_sdk._core import s3_security

    monkeypatch.setattr(s3_security, "boto3", _Boto3Shim(session_factory))


@pytest.fixture
def no_partition_lookup(monkeypatch):
    """Force ``get_partition`` onto its documented-prefix fallback."""
    _patch_boto3(monkeypatch, _UnhelpfulSession)


@pytest.mark.parametrize(
    ("region", "expected"),
    [
        ("us-gov-west-1", "aws-us-gov"),
        ("us-gov-east-1", "aws-us-gov"),
        ("cn-north-1", "aws-cn"),
        ("cn-northwest-1", "aws-cn"),
        ("us-east-1", "aws"),
        ("eu-west-3", "aws"),
        # A region newer than the installed botocore's endpoint data, which is
        # the case the fallback was written for.
        ("ap-southeast-7", "aws"),
    ],
)
def test_the_partition_fallback_reads_the_region_prefix(
    no_partition_lookup, region, expected
):
    """A GovCloud or China region must not fall back to the commercial partition.

    ``arn:aws:s3:::bucket`` in a GovCloud account names no resource, so the deny
    statement would cover nothing and ``apply_enforce_ssl_only`` would still
    return ``True``: a bucket reported as hardened that accepts plaintext.
    """
    assert get_partition(region) == expected


def test_no_region_answers_without_consulting_botocore(monkeypatch):
    """``None`` short-circuits ahead of the lookup, so it cannot fail there.

    ``region`` is optional throughout this module, and a session construction on
    the ``None`` path would be both wasted work and a new failure mode for a
    question with a settled answer.
    """
    _patch_boto3(
        monkeypatch, lambda: pytest.fail("a session was built for region=None")
    )

    assert get_partition(None) == "aws"


def test_the_fallback_partition_reaches_the_statement_arns(no_partition_lookup):
    """The fallback has to change the ARNs, not just the returned string.

    The statement builder is the only consumer of ``get_partition``, so this is
    the assertion that ties the fallback to an observable effect.
    """
    from idp_sdk._core.s3_security import enforce_ssl_only_statement

    statement = enforce_ssl_only_statement("gov-bucket", "us-gov-west-1")

    assert statement["Resource"] == [
        "arn:aws-us-gov:s3:::gov-bucket",
        "arn:aws-us-gov:s3:::gov-bucket/*",
    ]


# ---------------------------------------------------------------------------
# apply_enforce_ssl_only: an existing policy that cannot be read
# ---------------------------------------------------------------------------


OPERATOR_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "OperatorReadGrant",
            "Effect": "Allow",
            "Principal": {"AWS": "arn:aws:iam::123456789012:role/Analyst"},
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::guarded-bucket/*",
        }
    ],
}


def _s3_with_unreadable_policy(error_code: str = "AccessDenied"):
    """A real moto client whose ``GetBucketPolicy`` fails with ``error_code``.

    Only that one method is replaced, so ``put_bucket_policy`` stays real — which
    is what lets the test observe whether a write happened.
    """
    client = boto3.client("s3", region_name=REGION)

    def _boom(**_):
        raise botocore.exceptions.ClientError(
            {"Error": {"Code": error_code, "Message": "denied"}}, "GetBucketPolicy"
        )

    client.get_bucket_policy = _boom  # type: ignore[method-assign]
    return client


def _stored_policy(bucket: str) -> dict | None:
    """The bucket's policy as moto actually holds it, or ``None``."""
    reader = boto3.client("s3", region_name=REGION)
    try:
        return json.loads(reader.get_bucket_policy(Bucket=bucket)["Policy"])
    except botocore.exceptions.ClientError:
        return None


def test_an_unreadable_policy_does_not_get_overwritten(aws_credentials):
    """The load-bearing assertion: a failed read must write nothing.

    ``merge_enforce_ssl_only(None, ...)`` returns a policy containing only
    ``EnforceSSLOnly``. So if a ``GetBucketPolicy`` failure were swallowed as "no
    policy here", the following ``PutBucketPolicy`` would replace the operator's
    grants with that single statement — a silent removal of access, reported as a
    successful hardening. The read error is re-raised precisely to prevent that,
    and the operator's statement surviving is the proof.
    """
    with mock_aws():
        writer = boto3.client("s3", region_name=REGION)
        writer.create_bucket(Bucket="guarded-bucket")
        writer.put_bucket_policy(
            Bucket="guarded-bucket", Policy=json.dumps(OPERATOR_POLICY)
        )
        blinded = _s3_with_unreadable_policy()

        with pytest.raises(RuntimeError) as exc:
            apply_enforce_ssl_only(blinded, "guarded-bucket", REGION)

        assert _stored_policy("guarded-bucket") == OPERATOR_POLICY
        message = str(exc.value)
        assert ENFORCE_SSL_SID in message
        assert "add it manually" in message


def test_an_unreadable_policy_is_reported_as_a_failure_not_a_success(aws_credentials):
    """``raise_on_error=False`` must still answer ``False``.

    That flag is used for pre-existing buckets the caller may not own, and the
    return value is the caller's only signal. Returning ``True`` here would report
    a bucket as TLS-only when its policy was never read, let alone written.
    """
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(Bucket="guarded-bucket")
        blinded = _s3_with_unreadable_policy()

        result = apply_enforce_ssl_only(
            blinded, "guarded-bucket", REGION, raise_on_error=False
        )

        assert result is False
        assert _stored_policy("guarded-bucket") is None


def test_the_failure_is_logged_when_it_is_not_raised(aws_credentials, caplog):
    """Suppressed is not silent — otherwise an unhardened bucket leaves no trace."""
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(Bucket="guarded-bucket")
        blinded = _s3_with_unreadable_policy()

        apply_enforce_ssl_only(blinded, "guarded-bucket", REGION, raise_on_error=False)

        assert ENFORCE_SSL_SID in caplog.text
        assert "guarded-bucket" in caplog.text


@pytest.mark.parametrize(
    "error_code", ["AccessDenied", "NoSuchBucket", "MethodNotAllowed"]
)
def test_only_a_genuinely_absent_policy_is_treated_as_absent(
    aws_credentials, error_code
):
    """The branch matches on ``"NoSuchBucketPolicy"`` in the error text.

    That is a substring test over ``str(exc)``, so what it admits is worth
    stating: every other S3 error — a denied read, a bucket that is gone, a read
    the bucket type does not allow — is a failure rather than an empty policy.
    ``NoSuchBucket`` is the one worth calling out, since "the bucket does not
    exist" and "the bucket has no policy" are easy to conflate and only the
    second is safe to merge from.
    """
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(Bucket="guarded-bucket")
        blinded = _s3_with_unreadable_policy(error_code)

        with pytest.raises(RuntimeError):
            apply_enforce_ssl_only(blinded, "guarded-bucket", REGION)


def test_a_bucket_with_no_policy_is_still_hardened(aws_credentials):
    """The control for the tests above: ``NoSuchBucketPolicy`` is not a failure.

    Every bucket the CLIs create is in this state, so treating the absent-policy
    error as an error would make this module fail on its main path. Asserted here
    so the "only a genuinely absent policy" boundary is pinned from both sides.
    """
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(Bucket="fresh-bucket")
        s3 = boto3.client("s3", region_name=REGION)

        assert apply_enforce_ssl_only(s3, "fresh-bucket", REGION) is True

        policy = _stored_policy("fresh-bucket")
        assert policy is not None
        assert [s["Sid"] for s in policy["Statement"]] == [ENFORCE_SSL_SID]
