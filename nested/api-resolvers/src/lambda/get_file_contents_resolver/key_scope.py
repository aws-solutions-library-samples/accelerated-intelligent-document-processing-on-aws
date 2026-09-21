# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Key-level authorization for the object-read path.

``s3_targets`` bounds *which bucket* a caller-supplied ``s3Uri`` may name. That is
not the whole question, because two of the allow-listed buckets are **partitioned
per user** by the product's two scope axes, and the partition is expressed in the
object key:

* The Configuration bucket holds one configuration-profile revision body per key,
  ``config_revisions/<profile>/<nnnnnn>.json.gz``. Which profiles a caller may see
  is their ``allowedConfigVersions`` (``config_scope``).
* The Test Set bucket is keyed ``<test_set_id>/…`` throughout — source documents
  under ``<id>/input/``, ground-truth bodies under ``<id>/baseline/``, published
  snapshots under ``<id>/versions/<n>/baseline/``. Which test sets an Annotator may
  see is their ``allowedTestSets`` (``testset_scope``).

A bucket-only check therefore reads, for those two buckets, as "any caller who may
read *some* of this bucket may read *all* of it" — which is the exact negation of
both scope axes, for precisely the callers the axes exist to restrict. The
configuration resolver states the profile rule as "there is only one place to get it
right"; that is true of the *metadata* path it guards, and this module is the same
rule applied to the bytes.

**This module answers "whose is this key", not "does the scope match".** The
matching is `config_scope.scope_allows` and `testset_scope.assert_can_access_test_set`,
imported and called unchanged — a scope matcher that differs between call sites
admits somewhere what it denies elsewhere, so there is deliberately no second
implementation of either rule here. What is new, and local to this path, is the
derivation of the scope *subject* from an object key.

Why it lives beside the read resolver rather than in ``s3_targets``: the write paths
that share that module constrain *where a write may land*, which is a different
question with a different answer for the same key (a revision body is write-once and
read-many). This resolver is the only path that presigns or proxies a
Configuration- or Test-Set-bucket object from a caller-supplied key —
``get_sample_document_resolver`` presigns the Configuration bucket too but only under
``samples/``, which is bundled content with no per-user partition. If a second
caller-supplied-key read path appears, this belongs in ``s3_targets`` next to the
bucket rule so the two cannot drift; until then a shared module with one consumer
would be the appearance of a shared rule rather than the fact of one.

Three properties this module keeps, each of which has its own test:

* **Unknown keys are not scope-bearing, and are left exactly as they were.** Only
  the two prefixes above are partitioned per user. Treating an unrecognised
  Configuration-bucket key as denied would break ``config_library/`` and ``samples/``
  reads that no scope axis governs; treating one as *allowed* is the status quo and
  is what the bucket allow-list already decided.
* **Fail closed.** An absent claim, an unresolvable caller or a failed UsersTable
  read denies. ``config_scope.resolve_allowed_config_versions`` raises
  ``ScopeLookupError`` for all three and this module turns that into a refusal, never
  into "unrestricted" — the AUTH.T07 shape.
* **A refusal does not disclose existence.** Every denial raises the same
  ``PermissionError`` text, which names neither the bucket, the key, the profile, the
  test set, nor whether any of them exist. The object is never read, so S3 is never
  asked a question whose answer could be timed.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Mapping, Optional, Tuple

import boto3
import config_scope
import testset_scope

logger = logging.getLogger(__name__)

# The two scope axes, as opaque markers. Named constants rather than bare strings so
# a typo is an AttributeError at import rather than a subject that matches no branch
# and is silently treated as unscoped.
CONFIG_PROFILE = "config-profile"
TEST_SET = "test-set"

# `config_revisions/<profile>/…` — the profile is the second path segment. Anchored,
# and the profile segment excludes `/`, so the captured name is exactly the one
# `ConfigRevisionStore.body_key` writes. `_SAFE_PROFILE_RE` in that module restricts
# a profile name to `[a-zA-Z0-9._-]`, which is why no traversal form can reach this.
_REVISION_KEY_RE = re.compile(r"^config_revisions/([^/]+)/")

# Every Test Set bucket key is `<test_set_id>/…`; the id is the first path segment
# and the bucket holds nothing at its root. A key with no `/` names no test set, and
# is refused rather than read as unscoped — see `scope_subject`.
_TEST_SET_KEY_RE = re.compile(r"^([^/]+)/")

# The one refusal message, for every denial on either axis. Deliberately says nothing
# about which bucket, key, profile or test set was asked for, and nothing about
# whether it exists: the read path's own tests assert that a refusal is not an
# existence oracle, and a scope refusal that named the profile it declined would be a
# profile-enumeration oracle for exactly the caller being restricted.
_REFUSAL = "Unauthorized: you are not permitted to read that object."

# Groups that are never configuration-profile scoped. Mirrors the configuration
# resolver, which resolves no scope at all for an Admin (`if not caller["is_admin"]`).
# The two must agree: a caller the metadata path serves and the byte path refuses is
# a divergence between two enforcements of one rule, which is the defect class
# `config_scope` exists to prevent.
CONFIG_UNSCOPED_GROUPS = ("Admin",)

# Per-container scope cache, same shape and TTL as the configuration resolver's.
_user_scope_cache: dict = {}
_USER_SCOPE_CACHE_TTL = 60  # seconds

_dynamodb = None


def _dynamodb_resource():
    """The DynamoDB resource, built on first use.

    Not at import time: the module is imported by a test suite that installs moto
    around it, and a resource built before the mock is in place talks to the real
    endpoint.
    """
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def scope_subject(
    bucket: str,
    key: str,
    *,
    configuration_bucket: str = "",
    test_set_bucket: str = "",
) -> Optional[Tuple[str, str]]:
    """What must be authorized to read ``key``, or ``None`` if nothing must be.

    Returns ``(axis, name)`` — ``(CONFIG_PROFILE, "<profile>")`` or
    ``(TEST_SET, "<id>")``.

    Bucket names are passed in rather than read from the environment here so the
    caller resolves them once and so a test can state them explicitly. An **empty**
    bucket name matches nothing: an unset ``CONFIGURATION_BUCKET`` cannot make every
    bucket the configuration bucket, which is what `bucket == os.environ.get(...)`
    would do for a request naming a bucket the allow-list somehow admitted with no
    name configured.

    ``ValueError`` — a 400, not a refusal — for a Test-Set-bucket key with no path
    segment at all. Such a key names no test set and no object (the bucket stores
    nothing at its root), so there is no scope question to answer and nothing to
    disclose by saying the argument is malformed.
    """
    if configuration_bucket and bucket == configuration_bucket:
        match = _REVISION_KEY_RE.match(key)
        if match:
            return CONFIG_PROFILE, match.group(1)
        # `config_library/`, `samples/` and anything else in this bucket is not
        # partitioned by profile. The bucket allow-list remains the control there,
        # exactly as before.
        return None

    if test_set_bucket and bucket == test_set_bucket:
        match = _TEST_SET_KEY_RE.match(key)
        if match:
            return TEST_SET, match.group(1)
        raise ValueError("Invalid S3 URI: key is required")

    return None


def _claims(event: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The claims the transport verified, or raise if there are none.

    ⚠️ **A missing identity denies here, rather than being read as a trusted
    service-to-service invoke.** That marker is real elsewhere in this tree
    (``testset_scope.is_direct_invoke``), but it is an *absence*, and an absence is
    only safe to read as trust where a trusted caller actually exists. This function
    has exactly one invoker — ``http_api_dispatcher``, via the single
    ``FIELD_FUNCTION_MAP`` entry that serves both ``getFileContents`` and its
    ``getFilePresignedUrl`` alias — and it always builds the identity from the
    authorizer's verified claims. So there is no invoke this branch would refuse that
    anything makes today, and if one is ever added it must carry its own explicit
    marker: reinstating "absent means trusted" here would make every scope check on
    this path removable by dropping a key from the event.
    """
    identity = (event or {}).get("identity")
    if not isinstance(identity, Mapping):
        logger.warning(
            "Refusing a scope-bearing object read: the event carries no verified "
            "identity, so no scope can be evaluated for it."
        )
        raise PermissionError(_REFUSAL)
    claims = identity.get("claims")
    if not isinstance(claims, Mapping):
        logger.warning(
            "Refusing a scope-bearing object read: the event's identity carries no "
            "claims, so no scope can be evaluated for it."
        )
        raise PermissionError(_REFUSAL)
    return claims


def _caller_groups(claims: Mapping[str, Any]) -> list:
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return list(groups)


def _assert_config_profile_allowed(
    event: Optional[Mapping[str, Any]], profile: str
) -> None:
    """Refuse unless ``profile`` is in the caller's ``allowedConfigVersions``."""
    claims = _claims(event)
    if any(group in _caller_groups(claims) for group in CONFIG_UNSCOPED_GROUPS):
        return

    try:
        allowed = config_scope.resolve_allowed_config_versions(
            config_scope.caller_email_from_claims(claims),
            caller_sub=config_scope.caller_sub_from_claims(claims),
            users_table_name=os.environ.get("USERS_TABLE_NAME", ""),
            dynamodb=_dynamodb_resource(),
            cache=_user_scope_cache,
            cache_ttl=_USER_SCOPE_CACHE_TTL,
        )
    except config_scope.ScopeLookupError as exc:
        # Fails CLOSED. A scope that cannot be *evaluated* is not a caller with no
        # restriction, and reading it as one serves every profile's full
        # configuration history — prompts and few-shot examples included — to a
        # caller entitled to a subset, on nothing more than a DynamoDB blip.
        logger.error(
            "Refusing a configuration-revision read: the caller's "
            "allowedConfigVersions could not be resolved: %s",
            exc,
        )
        raise PermissionError(_REFUSAL) from exc

    if not config_scope.scope_allows(allowed, profile):
        logger.warning(
            "Refusing a configuration-revision read: the requested profile is "
            "outside the caller's allowedConfigVersions."
        )
        raise PermissionError(_REFUSAL)


def _assert_test_set_allowed(
    event: Optional[Mapping[str, Any]], test_set_id: str
) -> None:
    """Refuse unless the caller may touch ``test_set_id``.

    ``assert_can_access_test_set`` is the whole rule and is called unchanged: Admin
    and Author own test sets, an Annotator must name this one in ``allowedTestSets``
    (and an Annotator with no scope is denied rather than unrestricted), and every
    other role is refused. That last clause is what makes this consistent with the
    metadata path — ``getTestSetDocuments`` is ``[Admin, Author, Annotator]``, so a
    Viewer or Reviewer has no route to a Test-Set-bucket key to begin with.

    The identity is required first, for the reason in :func:`_claims`: that helper's
    rule and this one's differ on a null identity, and the stricter one governs here.
    """
    _claims(event)
    try:
        testset_scope.assert_can_access_test_set(event, test_set_id)
    except testset_scope.TestSetAccessDenied as exc:
        # Re-raised as `PermissionError` with the shared text. The original message
        # names the test set, which is right for an operation the caller addressed by
        # id and wrong for one where the id came out of a key they supplied: it would
        # confirm the id exists in this deployment.
        logger.warning(
            "Refusing a test-set object read: the caller is not scoped to the test "
            "set the key belongs to."
        )
        raise PermissionError(_REFUSAL) from exc


def assert_key_in_scope(
    bucket: str,
    key: str,
    event: Optional[Mapping[str, Any]],
    *,
    configuration_bucket: str = "",
    test_set_bucket: str = "",
) -> None:
    """Refuse the read unless the caller's scope covers this key.

    A no-op for every key no scope axis partitions, which is the majority of them.
    Raises ``PermissionError`` (HTTP 403 ``Unauthorized``) otherwise, with a message
    that discloses nothing about the object.
    """
    subject = scope_subject(
        bucket,
        key,
        configuration_bucket=configuration_bucket,
        test_set_bucket=test_set_bucket,
    )
    if subject is None:
        return
    axis, name = subject
    if axis == CONFIG_PROFILE:
        _assert_config_profile_allowed(event, name)
    elif axis == TEST_SET:
        _assert_test_set_allowed(event, name)
    else:  # pragma: no cover - unreachable while the two axes are the only ones
        raise PermissionError(_REFUSAL)
