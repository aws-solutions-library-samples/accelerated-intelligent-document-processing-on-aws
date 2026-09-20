# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which S3 bucket and key an API resolver may act on.

Several UI operations take an S3 location from the request: a read path that proxies
an object's bytes, and two write paths that mint a presigned upload or copy a bundled
sample. In each case the resolver's execution role is broader than any one request
needs — it holds read or write on every bucket the deployment uses — so the argument
has to be constrained by the resolver, and the constraint has to be the same one
everywhere. Two controls, deliberately separate because they answer different
questions:

**The bucket allow-list** bounds *which* bucket. Without it any of these operations
is a generic S3 gadget for whatever its role can reach, including a bucket in another
deployment that a cross-account policy happens to permit. An empty allow-list fails
CLOSED: the resolver code and the environment variables that configure it are one
CloudFormation resource, deployed together, so an empty set is a template fault, and
reading a template fault as "allow every bucket this role can reach" turns the fault
into the gadget the list exists to prevent.

**The write-once key rule** bounds *where in* the bucket a write may land, and applies
to write paths only. Some prefixes hold objects whose integrity comes from the key
rather than from anything checked at read time: the object at a given key is written
exactly once, and every later reader treats it as the record of what happened. A write
that could land on one of those keys could rewrite history that is not re-derivable,
which is a different failure from writing somewhere merely inconvenient. Reads of
those prefixes are legitimate and are not restricted — the configuration UI reads
revision bodies, the version viewer reads run manifests — so only the write paths
consult this.

``WRITE_ONCE_KEY_RULES`` is the class, not a list of instances: add an entry when a new
store's integrity depends on its keys never being rewritten, and cite where that
property is documented so the entry can be checked rather than trusted.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, Mapping, Optional, Set

# Environment variables that name a bucket this deployment owns. A name that is
# unset simply contributes nothing — the set is the intersection of what the code
# knows about and what the template wired.
BUCKET_ENV_NAMES = frozenset(
    {
        "INPUT_BUCKET",
        "OUTPUT_BUCKET",
        "CONFIGURATION_BUCKET",
        "EVALUATION_BASELINE_BUCKET",
        "REPORTING_BUCKET",
        "TEST_SET_BUCKET",
        "DISCOVERY_BUCKET",
        "WORKING_BUCKET",
    }
)


def resolve_allowed_buckets(
    env: Optional[Mapping[str, str]] = None,
    names: Iterable[str] = BUCKET_ENV_NAMES,
) -> Set[str]:
    """The bucket names this deployment wired, as a set.

    Blank values are dropped, whitespace included: a variable set to ``""`` or to
    ``"   "`` is not a wired bucket. Without the strip, a whitespace-only value makes
    the set non-empty, so the fail-closed branch does not fire and the log line claims
    a configured allow-list — no real bucket would match it, but the operator is told
    the wrong thing about why.
    """
    source = os.environ if env is None else env
    return {source[name].strip() for name in names if (source.get(name) or "").strip()}


def assert_bucket_allowed(
    bucket: str,
    allowed: Set[str],
    *,
    logger=None,
) -> None:
    """Refuse ``bucket`` unless it is one this deployment owns.

    Raises ``PermissionError``, which ``http_api_dispatcher`` maps to **HTTP 403**
    with ``errorType: "Unauthorized"`` — the marker the UI keys on. It must be
    ``PermissionError`` and its message must keep the ``Unauthorized`` prefix: the
    dispatcher chooses a status from the exception's class name, falling back to an
    anchored message prefix, and a refusal that loses both arrives as a 500. Neither
    message names the bucket, the allow-list, or whether the bucket exists, so
    fixing a status code does not build an enumeration oracle; the allow-list goes to
    the log for whoever triages the 403.
    """
    if not allowed:
        if logger is not None:
            # ERROR, not WARNING: an empty allow-list is a build fault rather than a
            # policy decision, the same reasoning as authz.py's DENY_ALL_MARKER.
            logger.error(
                "No bucket allow-list configured (none of %s are set); refusing "
                "every request. This is a deployment fault — the template must set "
                "the bucket environment variables.",
                sorted(BUCKET_ENV_NAMES),
            )
        raise PermissionError(
            "Unauthorized: S3 access is not configured for this deployment."
        )
    if bucket not in allowed:
        if logger is not None:
            logger.warning(
                "Rejecting request for bucket %r (not in allow-list %s).",
                bucket,
                sorted(allowed),
            )
        raise PermissionError(
            "Unauthorized: requested bucket is not accessible from this deployment."
        )


# Keys whose integrity comes from the key itself: written once, then read as the
# record of what happened. Each entry cites where that property is documented, so a
# reviewer can check the claim rather than take the pattern on trust.
#
#   config_revisions/<profile>/<nnnnnn>.json.gz
#       "Because the revision number is part of the key, each object is write-once —
#       immutability comes from the key" (idp_common/config/revisions.py).
#
#   <document-key>/runs/<run_id>/manifest.json
#       One manifest per completed processing run, pinning the S3 object versions
#       that run produced; `run_id` is unique per execution
#       (idp_common/document_versions.py). It is what the version viewer and the
#       version comparison read to say what a past run produced. The pattern allows
#       any depth between `runs/` and the manifest because `list_run_ids` does: it
#       takes everything between the two as the run id, so a rule that insisted on one
#       segment would leave a deeper key writable AND readable back as a run.
#
#   <test_set_id>/versions/<n>/baseline/**
#       The labels a published test-set version was scored against, snapshotted so
#       that "the version number now refers to bytes" rather than to a DynamoDB row
#       (test_set_resolver/index.py). test_file_copier prefers this snapshot over the
#       live baseline whenever it exists, so a run stamped with that version is scored
#       against it. Not re-derivable: the live baseline moves on as annotation
#       continues. Guarded at DIRECTORY scope, because unlike the two above it holds
#       many objects under arbitrary sub-keys.
WRITE_ONCE_KEY_RULES = (
    (
        re.compile(r"^config_revisions/"),
        "configuration revision bodies",
    ),
    (
        re.compile(r"(^|/)runs/.+/manifest\.json$"),
        "document processing-run manifests",
    ),
    (
        re.compile(r"^[^/]+/versions/[0-9]+/baseline/"),
        "published test-set version baselines",
    ),
)

# Why the run rule names the manifest object and not the whole `runs/<run_id>/`
# directory: `manifest.json` is the only object written there, and a document key can
# legitimately contain `runs` as a path segment (`2026/runs/january/report.pdf`).
# Widening to the directory would refuse those uploads to protect keys that nothing
# writes — a control whose false positives are the live path it is meant to guard. If
# a second object is ever written under a run prefix, widen this and say so here.


# S3's POST-policy form treats this token specially: a key containing it is signed
# not as an exact `{"key": ...}` condition but as `["starts-with", "$key", <the text
# before it>]`, i.e. a grant over a whole prefix rather than one object. So the key as
# WRITTEN is not the key S3 will accept, and any rule evaluated against the written
# form is evaluated against the wrong thing.
_FILENAME_VARIABLE = "${filename}"


def effective_key(key: str):
    """What S3 will actually accept for ``key``: ``("exact", k)`` or ``("prefix", p)``.

    Derived from S3's own substitution rather than pattern-matching the written key,
    so the check below sees the grant that will really be signed.
    """
    if _FILENAME_VARIABLE in key:
        return "prefix", key.split(_FILENAME_VARIABLE, 1)[0]
    return "exact", key


def write_once_reason(key: str) -> Optional[str]:
    """What write-once store ``key`` belongs to, or ``None`` if it belongs to none."""
    for pattern, what in WRITE_ONCE_KEY_RULES:
        if pattern.search(key):
            return what
    return None


def assert_key_writable(key: str, *, logger=None) -> None:
    """Refuse a write that would land on a write-once key.

    Write paths only. Reads of these prefixes are legitimate — the configuration UI
    reads revision bodies and the version viewer reads run manifests — so the read
    path deliberately does not call this.
    """
    kind, value = effective_key(key)
    if kind != "exact":
        # The request asks for a grant over a prefix rather than for one object, so
        # there is no definite key to evaluate the rules against and every key under
        # `value` would be permitted. Every caller of these operations names a
        # concrete object, so this is refused rather than analysed: supporting it would
        # mean deciding, for each rule, whether the requested prefix can reach it.
        if logger is not None:
            logger.warning(
                "Rejecting a write that would grant a whole prefix rather than one "
                "object (effective prefix %r)",
                value,
            )
        raise PermissionError("Unauthorized: an upload must name a single object.")
    what = write_once_reason(value)
    if what is None:
        return
    if logger is not None:
        logger.warning("Rejecting upload to a write-once key (%s): %r", what, value)
    raise PermissionError(
        "Unauthorized: that location is reserved and cannot be written to."
    )


def assert_write_target_allowed(
    bucket: str,
    key: str,
    allowed: Set[str],
    *,
    logger=None,
) -> None:
    """Both controls, in the order a write path wants them.

    One call so a write path cannot acquire the bucket check and forget the key rule.
    """
    assert_bucket_allowed(bucket, allowed, logger=logger)
    assert_key_writable(key, logger=logger)
