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
# `ConfigRevisionStore.body_key` writes.
_REVISION_KEY_RE = re.compile(r"^config_revisions/([^/]+)/")

# The S3 prefix the revision store writes under, on its own, for the near-miss check
# in `_assert_canonical_key`.
_REVISION_PREFIX = "config_revisions"

# The character class `ConfigRevisionStore._safe_profile` enforces on every profile
# name it writes into a key (`_SAFE_PROFILE_RE` in `idp_common/config/revisions.py`).
# Duplicated as a literal rather than imported because this module ships in a bundle
# with no `idp_common` layer — and deliberately re-stated rather than widened: a
# captured profile segment outside this class is not a name that store ever wrote, so
# it is not a revision body and must not be treated as one.
_SAFE_PROFILE_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

# Every Test Set bucket key is `<test_set_id>/…`; the id is the first path segment
# and the bucket holds nothing at its root. A key with no `/` names no test set, and
# is refused rather than read as unscoped — see `scope_subject`.
#
# No character class is asserted on the id, unlike the profile above: a test set id is
# derived from a user-supplied name (spaces to hyphens, lowercased) and is not
# restricted to a safe set, so a class would refuse legitimate sets. The canonical-form
# check below is what bounds this axis instead, and it is enough, because the id is
# only ever *matched* against `allowedTestSets` — never used to build a path.
_TEST_SET_KEY_RE = re.compile(r"^([^/]+)/")

# Segments that make a key non-canonical: an empty one (from a leading `/` or an
# internal `//`), or a relative-path segment.
_NON_CANONICAL_SEGMENTS = frozenset({"", ".", ".."})

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


def _assert_canonical_key(key: str) -> None:
    """Refuse a key in a partitioned bucket that is not in canonical form.

    Raises ``ValueError`` — a 400 — for a leading ``/``, an internal ``//``, or a ``.``
    or ``..`` segment.

    **Why this is not merely tidiness.** Those spellings all *fail to match* the
    prefixes below, so without this they would return "not scope-bearing" and the read
    would proceed unchecked: ``//config_revisions/claims/000001.json.gz`` and
    ``/config_revisions/claims/…`` and ``./config_revisions/claims/…`` all name the
    same profile to a human and none of them matches ``^config_revisions/``. Today each
    ends in a 400 anyway, because S3 keys are opaque byte strings and no such object
    exists — but that makes the soundness of the whole check rest on nothing in the
    path ever collapsing those forms. ``getFilePresignedUrl`` hands the signed key to a
    client this deployment does not control, and any normalising intermediary — an HTTP
    library collapsing ``//``, a proxy placed in front of the URL later — would turn
    one of them into a real bypass, silently, because the resolver's own log line said
    400.

    ⚠️ **What this does and does not make self-contained.** For the spellings it names,
    the refusal is decided here and needs nothing of the path below it. It is **not** a
    general guarantee that no rewriting anywhere can change which subject a key resolves
    to: a form nobody has thought of is by definition not in the list. Two facts bound
    the residual rather than this function doing so — botocore percent-encodes a literal
    ``%`` to ``%25`` on both the proxy and the presign path, so a ``%2f`` in the key
    never reaches S3 as a separator; and a signature covers the canonical request, so an
    intermediary that rewrote the path would invalidate it and the fetch fails closed.
    Those are properties of the AWS SDK and of SigV4, not of this module.

    Percent sequences are therefore not rejected wholesale: S3 does not decode them and
    a Test-Set key's tail can legitimately contain ``%`` (a test set holds documents, and
    a document name may). Where the guarantee *can* be made precisely it is: a profile
    name can never contain ``%``, so a Configuration-bucket key gets both
    ``_SAFE_PROFILE_RE`` on its profile segment and a near-miss check on its first
    segment — see :func:`scope_subject`.
    """
    for segment in key.split("/"):
        if segment in _NON_CANONICAL_SEGMENTS:
            logger.warning(
                "Refusing a read in a per-user-partitioned bucket for a key that is "
                "not in canonical form (empty, '.' or '..' path segment)."
            )
            raise ValueError("Invalid S3 URI: key is not in canonical form")


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

    ``ValueError`` — a 400, not a refusal — for a key in a partitioned bucket that is
    not in canonical form, for a Test-Set-bucket key with no path segment at all, and
    for a near-miss of the revision prefix. None of those names an object this
    deployment ever wrote, so there is no scope question to answer and saying the
    argument is malformed discloses nothing about what exists.
    """
    if configuration_bucket and bucket == configuration_bucket:
        # A well-formed revision body is resolved FIRST, before the generic
        # canonical-form scan, because the two overlap on a real case: `.` and `..` are
        # inside `_SAFE_PROFILE_RE`, so `ConfigRevisionStore` can and will write
        # `config_revisions/./000001.json.gz` for a profile actually named `.`. Nothing
        # forbids creating one — `validate_version_name` adds only a length cap and one
        # reserved name — so refusing those keys would deny a scoped caller their own
        # profile's history, and the generic scan (which rejects `.` and `..` segments
        # anywhere) does exactly that if it runs first.
        #
        # Resolving them here is safe, and safe for a reason specific to this bucket:
        # the prefix anchors the key, so a normalising intermediary that collapsed
        # `config_revisions/./x` or `config_revisions/../x` lands on
        # `config_revisions/x` or `x` — neither of which is another profile's revision
        # body. The Test Set branch below cannot make the same trade, because there the
        # id IS the first segment and collapsing `../ts-b/…` would shift the read to a
        # different test set than the one authorised.
        match = _REVISION_KEY_RE.match(key)
        if match:
            profile = match.group(1)
            if not _SAFE_PROFILE_RE.match(profile):
                # Inside the revision prefix but carrying a profile segment the store
                # cannot have written. Refused rather than passed to the matcher: a
                # scope entry is matched with `fnmatchcase`, so handing it a segment
                # containing glob metacharacters or a percent sequence would be
                # matching something other than a profile name.
                logger.warning(
                    "Refusing a configuration-revision read: the key's profile "
                    "segment is outside the character class the revision store "
                    "writes."
                )
                raise ValueError("Invalid S3 URI: key is not in canonical form")
            return CONFIG_PROFILE, profile

        _assert_canonical_key(key)

        # Anything else under the revision prefix is not a revision body — a key of the
        # wrong depth, or the prefix with nothing after it. Refused rather than read as
        # unpartitioned: it is inside the store this axis governs, and no writer here
        # produces it.
        if key.startswith(_REVISION_PREFIX + "/"):
            logger.warning(
                "Refusing a Configuration-bucket read under the revision prefix for a "
                "key that is not a revision body."
            )
            raise ValueError("Invalid S3 URI: key is not in canonical form")

        # A near-miss of the prefix in the first segment: a case variant, a
        # percent-encoded or backslash separator, or the prefix with a suffix glued on.
        # None of these names a revision body and none is a key any writer produces, so
        # reading one as merely "unpartitioned" is the shape this function exists to
        # avoid. ⚠️ This also refuses a hypothetical future prefix whose name *contains*
        # `config_revisions` (say `config_revisions_archive/`): if one is ever added, it
        # has to decide its own scope rule here rather than inherit "unscoped" by
        # default.
        if _REVISION_PREFIX in key.split("/", 1)[0].lower():
            logger.warning(
                "Refusing a Configuration-bucket read for a near-miss of the "
                "revision prefix that names no revision body."
            )
            raise ValueError("Invalid S3 URI: key is not in canonical form")

        # `config_library/`, `samples/` and anything else in this bucket is not
        # partitioned by profile. The bucket allow-list remains the control there,
        # exactly as before.
        return None

    if test_set_bucket and bucket == test_set_bucket:
        _assert_canonical_key(key)
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
