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

    Empty values are dropped: a variable set to ``""`` is not a wired bucket, and
    keeping it would put the empty string in the allow-list.
    """
    source = os.environ if env is None else env
    return {source[name] for name in names if source.get(name)}


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
#       version comparison read to say what a past run produced.
WRITE_ONCE_KEY_RULES = (
    (
        re.compile(r"^config_revisions/"),
        "configuration revision bodies",
    ),
    (
        re.compile(r"(^|/)runs/[^/]+/manifest\.json$"),
        "document processing-run manifests",
    ),
)

# Why the run rule names the manifest object and not the whole `runs/<run_id>/`
# directory: `manifest.json` is the only object written there, and a document key can
# legitimately contain `runs` as a path segment (`2026/runs/january/report.pdf`).
# Widening to the directory would refuse those uploads to protect keys that nothing
# writes — a control whose false positives are the live path it is meant to guard. If
# a second object is ever written under a run prefix, widen this and say so here.


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
    what = write_once_reason(key)
    if what is None:
        return
    if logger is not None:
        logger.warning("Rejecting upload to a write-once key (%s): %r", what, key)
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
