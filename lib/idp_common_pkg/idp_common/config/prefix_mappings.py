# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which Configuration Profile a document processes under, decided by where it lands.

An admin may declare **config prefix mappings**: an S3 key prefix in the Input bucket
→ a Configuration Profile, optionally a pinned revision. A document arriving under a
mapped prefix is assigned that configuration, so the *destination* decides the
configuration rather than every producer having to stamp
``x-amz-meta-config-version``. Producers are frequently things an admin does not
control — an S3 replication rule, a partner's ``aws s3 cp``, a scanner appliance — so
"just set the metadata" is not available to them, and without mappings every such
document silently processes under the one globally active profile.

This module is in ``idp_common`` for the reason ``config_scope`` is: the rule runs in
four separate deploy artifacts (``queue_sender``, the reprocess resolver, the upload
resolver, the configuration resolver's dry run), and a resolution rule that drifts
between call sites is exactly the defect that module's docstring was written about.

Two halves, deliberately separate:

* :class:`PrefixMappingStore` is the DynamoDB half.
* :func:`resolve_config_assignment` is the decision, and it takes **no** boto3 client
  and reads **no** environment. Its only seams are three injected callables
  (``active_profile``, ``published_revision``, ``profile_exists``), and every
  caller must supply all three — see
  ``scripts/tests/test_prefix_mapping_call_sites.py`` for what a caller that
  omits one gets wrong. That is
  what makes the whole precedence matrix testable as a table with no AWS, which is
  the only way a precedence rule of this shape stays verifiable.

Storage
-------

One item in the existing ``ConfigurationTable``, keyed
``Configuration = "ConfigPrefixMap#__index"``, holding ``Mappings`` (a list of entries)
and ``IndexSeq`` (an optimistic-concurrency counter). Three properties of that choice
are load-bearing:

* **The key prefix does not match ``begins_with(Configuration, "Config#")``.** That
  expression is what the scope-filtered profile listing and ``queue_sender``'s
  fallback scan use, so a mapping item cannot appear as a phantom profile — the same
  argument :mod:`idp_common.config.revisions` makes for ``ConfigRevIndex#``.
* **One item, read with one ``GetItem``.** The mapping set is consulted for *every
  document queued*. This repository has twice shipped a filtered ``Scan`` on that
  path; the comment in ``src/lambda/queue_sender/index.py`` is the write-up of issue
  #599, where a scan that missed its target silently processed documents under the
  wrong configuration. A single-item read has no such failure mode.
* **``ConsistentRead=True``, and no in-container cache.** A cache saves little against
  one small item and produces the worst possible admin experience: a mapping that
  looks saved but does not apply, for an interval nothing in the UI explains. Without
  the consistent read, "takes effect immediately" is simply not true. Do not add a
  cache here without arguing against this paragraph.

``MAX_MAPPINGS`` is enforced at write time rather than left to DynamoDB.
``revisions.py`` needs no such guard because ``DEFAULT_REVISION_CAP`` bounds its list;
nothing bounds this one, and a ``ValidationException`` at entry 201 would be an
**ingest-path outage**, because the aggregate item is what ingest reads.

Matching
--------

Every rule below is a decision, not an accident, and each is pinned by a test:

1. **Matching is case-sensitive**, because S3 keys are. ``Invoices/`` and
   ``invoices/`` are distinct keys and only one of them can be mapped.
2. **The trailing slash is the mode selector, and nothing is normalized onto it.** An
   entry ending in ``/`` is a prefix match; an entry without one matches that one
   exact key. The two are mutually exclusive, so a UI must not helpfully append a
   slash — doing so makes exact-key mode unexpressible. The segment-boundary
   behaviour that stops ``acme/inv`` capturing ``acme/invoices/`` comes from the admin
   writing ``acme/inv/``.
3. **An exact-key entry outranks every prefix entry**, however long.
4. **Most specific wins** = the longest ``prefix`` by character length. ``prefix`` is
   the entry's identity, so two entries cannot tie.
5. **The root is refused** at the API. A catch-all would change every unmapped upload
   in the deployment, and the active profile already means that.
6. **A disabled entry never matches** but is retained, so a mapping can be turned off
   without a delete/recreate audit gap.
7. **Keys are canonicalized before matching**, and callers must apply
   :func:`canonical_key` to any caller-supplied prefix before building a key from it.
   ``s3://bucket//finance/x.pdf`` is a legal, *distinct* key that no mapping on
   ``finance/`` matches, so without this a leading slash is a one-character bypass of
   ``reject`` mode.

Precedence, and why an agreeing tag is not a conflict
-----------------------------------------------------

See :func:`resolve_config_assignment`. The case worth calling out here is that
metadata which *agrees* with the mapping is not recorded as a conflict — otherwise
``reject`` mode would fail every document the SDK stamped correctly, and the conflict
metric would fire constantly on the harmless case.

⚠️ **When the resolved profile differs from the one the metadata named,
``config_revision`` is cleared** unless the mapping pins one of its own. Revision
numbers are per profile, so carrying the metadata's ``r7`` onto a different profile
reads a configuration that corresponds to nothing anybody asked for — and
``queue_processor`` will not correct it, because its backfill only fires when the
revision is absent.

Internal producers are exempt
-----------------------------

An object carrying ``x-amz-meta-submission-source`` is exempt from mappings entirely.
Three of this repository's own components write into the Input bucket with a
deliberately chosen configuration, and for two of them an override is not a
preference but a defect:

* The **PII anonymizer** copies a redacted document back into the Input bucket beside
  the original, stamped with a *companion* profile that has no preprocessing hook.
  Overriding that stamp runs the copy under whatever hooks the mapped profile
  registers instead of under none. Under ``reject`` it is far worse than a
  misconfiguration: in ``redactcopy_and_stop`` the hook returns ``halt=true`` and the
  host then deletes the original, so a reject on that prefix loses the original *and*
  never processes the copy — unrecoverable loss of the document, from one mapping an
  admin typed. (The redaction loop itself is not at risk: that is stopped by the
  ``_is_redacted_key`` filename check, which does not depend on the configuration the
  copy runs under.)
* **Test Studio** stamps the profile and revision a run is defined by. Overriding it
  does not fail the run; it produces accuracy and confidence numbers describing a
  configuration other than the one recorded, which is worse than a failure.

The **Jobs/batch API** is deliberately *not* exempt: it stamps metadata only when its
caller chose a profile, so a job with a choice is adjudicated like any other upload
and a job without one is mapped. That is the intended behaviour, not an oversight.

Failing open, which is a departure worth defending
--------------------------------------------------

If the mapping index cannot be read, resolution falls through to today's behaviour and
the caller emits ``PrefixMappingLookupFailed``. :mod:`idp_common.config_scope` argues
that "cannot evaluate" must never read as "allow" on a visibility boundary, and that
argument does apply here — failing open means a transient DynamoDB error bypasses every
``reject`` mapping in the deployment. It is still the right call, because the
alternative is halting document ingest for the whole deployment when a *routing* table
is unreadable, and every one of those objects processes under the active profile today.
The metric is alarmed so the window is visible rather than silent. This is the one
place this module knowingly departs from the repository's fail-closed posture.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from botocore.exceptions import ClientError

from idp_common.config_scope import scope_allows
from idp_common.ddb_numbers import coerce_int

logger = logging.getLogger(__name__)

# DynamoDB key for the single aggregate item. Chosen so it does NOT match
# begins_with(Configuration, "Config#") — see the module docstring.
PREFIX_MAP_INDEX_PREFIX = "ConfigPrefixMap"
PREFIX_MAP_INDEX_KEY = f"{PREFIX_MAP_INDEX_PREFIX}#__index"

# The attribute holding the entry list, and the concurrency counter beside it.
MAPPINGS_ATTRIBUTE = "Mappings"
SEQ_ATTRIBUTE = "IndexSeq"

# Hard ceiling on the number of mappings, enforced at write time. See the module
# docstring: the aggregate item is on the ingest path, so overflowing DynamoDB's
# 400 KB item limit would be an ingest outage rather than a failed admin write.
MAX_MAPPINGS = 200

_MAX_DESCRIPTION_LEN = 500

# What a mapping does when the object also carries conflicting upload metadata.
#
# Named for *metadata* rather than for "tags": the value being adjudicated is S3 user
# metadata, read back via ``head_object()["Metadata"]``. This repository never calls
# GetObjectTagging, and an S3 object tag is a different thing with a different API, so
# calling this "tagPrecedence" would send every later reader to the wrong place.
PRECEDENCE_MAPPING = "mapping"
PRECEDENCE_METADATA = "metadata"
PRECEDENCE_REJECT = "reject"
PRECEDENCE_VALUES = (PRECEDENCE_MAPPING, PRECEDENCE_METADATA, PRECEDENCE_REJECT)
DEFAULT_PRECEDENCE = PRECEDENCE_MAPPING

MATCH_PREFIX = "prefix"
MATCH_EXACT = "exact"

# Where a document's configuration came from. Recorded on the document so that "why
# did this process under lending r7?" is answerable from the tracking row rather than
# from log archaeology across two Lambdas. There were already three sources before
# mappings existed and the document recorded none of them.
SOURCE_METADATA = "metadata"
SOURCE_PREFIX_MAPPING = "prefix-mapping"
SOURCE_ACTIVE_PROFILE = "active-profile"
SOURCE_DOCUMENT_PIN = "document-pin"
SOURCE_EXPLICIT_REQUEST = "explicit-request"
SOURCE_INTERNAL_PRODUCER = "internal-producer"
SOURCE_REJECTED = "rejected"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class PrefixMappingConflict(RuntimeError):
    """A concurrent write won the aggregate item and the caller's write did not land.

    Raised rather than returned because the alternative — the ``False`` that
    ``ConfigRevisionStore._rewrite_index`` returns on the same condition — is
    trivially mistaken for success by a resolver that only logs it. A mapping the
    admin believes they created, which does not exist, is the worst outcome available
    here.
    """


# ---------------------------------------------------------------------------
# Key and prefix canonicalization
# ---------------------------------------------------------------------------


def canonical_key(key: str) -> str:
    """The form of ``key`` that matching is defined against.

    S3 treats ``finance/x.pdf``, ``/finance/x.pdf``, ``finance//x.pdf`` and
    ``finance/./x.pdf`` as four *distinct* keys, and a mapping written as
    ``finance/`` or ``finance/x.pdf`` matches only the first. Each of the other three
    is therefore a one-character route around a ``reject`` mapping, which is the
    whole point of normalising here rather than matching the key as typed:

    * leading and repeated slashes are collapsed;
    * ``.`` segments are dropped, because they name the same object S3 serves for the
      key without them and ``prefix_rejection_reason`` already refuses them in a
      *mapping*, so accepting them in a *key* is the asymmetry a client exploits;
    * ``..`` segments are **left alone**, deliberately. S3 keys are opaque strings
      with no parent directory, so ``a/b/../c`` is a real, distinct object and
      resolving it the way a filesystem would would make this function claim a key
      that S3 would serve from somewhere else. Refusing them in a mapping and
      matching them literally in a key is consistent: such an object simply has no
      mapping unless one names it literally.

    Trailing slashes are preserved, because they are the prefix/exact mode selector.
    """
    if not key:
        return ""
    trailing = key.endswith("/")
    segments = [segment for segment in key.split("/") if segment and segment != "."]
    canonical = "/".join(segments)
    if trailing and canonical:
        canonical += "/"
    return canonical


def prefix_rejection_reason(prefix: str) -> Optional[str]:
    """Why ``prefix`` is not a usable mapping prefix, or ``None`` if it is.

    Returns prose intended to reach the admin verbatim. A mapping that can never
    match, or one whose stored form differs from what was typed, is worse than a
    refusal: it reads as configured and does nothing.
    """
    if prefix is None or not prefix.strip():
        return "A mapping prefix is required."
    if prefix != prefix.strip():
        return (
            "A mapping prefix cannot begin or end with whitespace — S3 keys preserve "
            "it, so the mapping would never match."
        )
    if prefix in ("/", "//"):
        return (
            "A root mapping is not allowed: it would change the configuration of "
            "every unmapped upload in this deployment. The active Configuration "
            "Profile already serves that purpose."
        )
    if prefix.startswith("/"):
        return (
            "A mapping prefix must not start with '/'. S3 keys do not, so the "
            "mapping would never match."
        )
    if "//" in prefix:
        return "A mapping prefix must not contain '//'."
    segments = prefix.split("/")
    if any(segment in (".", "..") for segment in segments):
        return "A mapping prefix must not contain '.' or '..' path segments."
    if canonical_key(prefix) == "":
        return (
            "A root mapping is not allowed: it would change the configuration of "
            "every unmapped upload in this deployment."
        )
    return None


def match_kind(prefix: str) -> str:
    """``MATCH_PREFIX`` when ``prefix`` ends in ``/``, else ``MATCH_EXACT``.

    The trailing slash *is* the selector and nothing normalizes it in either
    direction. Appending one for the admin would make exact-key mappings
    unexpressible; stripping one would make every mapping an exact key.
    """
    return MATCH_PREFIX if prefix.endswith("/") else MATCH_EXACT


def normalize_entry(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """One stored mapping entry, with every field at its declared type.

    Applied on read as well as on write, because DynamoDB returns numbers as
    ``Decimal`` and a revision compared against an ``int`` elsewhere would silently
    never be equal.
    """
    prefix = str(entry.get("prefix") or "")
    revision = entry.get("configRevision")
    precedence = str(entry.get("metadataPrecedence") or DEFAULT_PRECEDENCE)
    return {
        "prefix": prefix,
        "matchKind": match_kind(prefix),
        "configProfile": str(entry.get("configProfile") or ""),
        "configRevision": coerce_int(revision) if revision is not None else None,
        "metadataPrecedence": (
            precedence if precedence in PRECEDENCE_VALUES else DEFAULT_PRECEDENCE
        ),
        "enabled": bool(entry.get("enabled", True)),
        "description": (str(entry.get("description") or ""))[:_MAX_DESCRIPTION_LEN]
        or None,
        "createdAt": entry.get("createdAt"),
        "createdBy": entry.get("createdBy"),
        "updatedAt": entry.get("updatedAt"),
        "updatedBy": entry.get("updatedBy"),
    }


def sort_key(entry: Mapping[str, Any]):
    """Order entries most-specific-first — the order resolution uses.

    Exact-key entries sort ahead of every prefix entry, then longer prefixes ahead of
    shorter. Returned as a sort key rather than applied inline so the admin UI can
    present the table in the same order the decision procedure evaluates it; a
    longest-prefix rule shown in an arbitrary order is very hard to read.
    """
    prefix = str(entry.get("prefix") or "")
    return (0 if match_kind(prefix) == MATCH_EXACT else 1, -len(prefix), prefix)


def sort_entries(entries: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """``entries`` normalized and ordered most-specific-first."""
    return sorted((normalize_entry(e) for e in entries), key=sort_key)


def find_match(
    object_key: str, entries: Sequence[Mapping[str, Any]]
) -> Optional[Dict[str, Any]]:
    """The one mapping that governs ``object_key``, or ``None``.

    ``entries`` need not be sorted; this sorts defensively rather than trusting a
    caller to have done it, because the cost is trivial beside a wrong answer.
    """
    key = canonical_key(object_key)
    if not key:
        return None
    for entry in sort_entries(entries):
        if not entry["enabled"] or not entry["configProfile"]:
            continue
        prefix = entry["prefix"]
        if not prefix:
            continue
        if entry["matchKind"] == MATCH_EXACT:
            if key == canonical_key(prefix):
                return entry
        elif key.startswith(canonical_key(prefix)):
            return entry
    return None


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigAssignment:
    """The configuration a document will process under, and why.

    ``reason`` is one sentence written for a human: it reaches the ingest log, the
    rejected document's tracking row, and the upload panel's warning, so it must read
    as an explanation on its own rather than as a log fragment.
    """

    profile: Optional[str] = None
    revision: Optional[int] = None
    source: str = SOURCE_ACTIVE_PROFILE
    mapping_prefix: Optional[str] = None
    conflict: bool = False
    rejected: bool = False
    scope_denied: bool = False
    unresolvable: bool = False
    reason: str = ""


def _resolve_revision(
    profile: Optional[str],
    revision: Optional[int],
    published_revision: Optional[Callable[[str], Optional[int]]],
) -> Optional[int]:
    """``revision`` if pinned, else the profile's published revision.

    An unpinned mapping therefore behaves exactly like "process under profile X": the
    published revision is what the active profile runs, so promoting a revision moves
    every unpinned mapping through the existing publish/rollback control rather than
    through a second mechanism.
    """
    if revision is not None:
        return coerce_int(revision)
    if not profile or published_revision is None:
        return None
    try:
        resolved = published_revision(profile)
    except Exception as e:  # noqa: BLE001 - a routing lookup must not strand ingest
        logger.warning(f"Could not resolve the published revision of {profile!r}: {e}")
        return None
    return coerce_int(resolved) if resolved is not None else None


def resolve_config_assignment(
    object_key: str,
    *,
    metadata_profile: Optional[str] = None,
    metadata_revision: Optional[int] = None,
    submission_source: Optional[str] = None,
    mappings: Optional[Sequence[Mapping[str, Any]]] = None,
    active_profile: Optional[Callable[[], Optional[str]]] = None,
    published_revision: Optional[Callable[[str], Optional[int]]] = None,
    profile_exists: Optional[Callable[[str], bool]] = None,
    allowed_profiles: Optional[Sequence[str]] = None,
) -> ConfigAssignment:
    """Decide the Configuration Profile and revision for one object.

    Takes no boto3 client and reads no environment. ``active_profile``,
    ``published_revision`` and ``profile_exists`` are injected callables — the
    deliberate seams — and the two lookups are *lazy* because each costs a DynamoDB
    read on a per-document path and most documents need neither.

    The order, which is the whole contract:

    1. ``submission_source`` set → **mappings are not consulted at all**. These are
       this deployment's own producers and their routing is already decided; for two
       of them an override is a defect rather than a preference (see the module
       docstring).
    2. No mapping matches → the metadata's profile if it named one, else the active
       profile. Byte-for-byte today's behaviour, which is what makes mappings purely
       additive.
    3. A mapping matches and there is no metadata → the mapping.
    4. Both, and they agree → the mapping, and **not** flagged as a conflict.
    5. Both, and they disagree → ``metadataPrecedence`` decides: ``mapping`` (the
       default) ignores the metadata, ``metadata`` honours it, ``reject`` fails the
       document rather than guessing.

    ``allowed_profiles`` is the caller's configuration scope, and is only meaningful
    where a caller exists. Pass ``None`` from ``queue_sender``, which handles an S3
    event and has no caller to scope. Where it is passed, a resolved profile outside
    it sets ``scope_denied`` — the resolver reports, the caller refuses, because what
    a refusal should look like differs between a presigned POST and a reprocess.
    """
    assignment = _decide(
        object_key,
        metadata_profile=metadata_profile,
        metadata_revision=metadata_revision,
        submission_source=submission_source,
        mappings=mappings,
        active_profile=active_profile,
        published_revision=published_revision,
        profile_exists=profile_exists,
    )

    # Scope is applied in exactly one place, over whatever was decided. Checking it
    # per branch is how the metadata route — a caller naming a profile directly, with
    # no mapping involved — ends up unchecked, which is the pre-existing gap on
    # uploadDocument that this control closes.
    #
    # ⚠️ The test is on the MAPPED profile as well as the resolved one, and that is
    # not belt-and-braces. Two outcomes name a profile the caller may not be
    # entitled to while `assignment.profile` is something else entirely:
    #
    # * a **rejection** sets `profile=None`, so a guard predicated on
    #   `assignment.profile` skips it — and `reason` names the mapped profile and
    #   its pinned revision, which is the whole secret;
    # * **metadata precedence** resolves to the caller's OWN profile, which is in
    #   scope by construction, while `reason` explains that it beat the mapping's —
    #   naming it.
    #
    # Either one hands a scoped caller the name of a profile outside their scope,
    # one key at a time, through an operation Author and Viewer can both call. That
    # is the oracle `getConfigVersions` is scope-filtered to prevent, so the
    # subject of the check is "any profile this answer would disclose", not "the
    # profile this answer selected".
    # Not computed for an internal producer: `_decide` does not consult mappings at
    # all on that branch (rule 1), so a mapping that does not govern the document
    # must not be able to deny it. Unreachable today -- the only caller passing
    # `submission_source` passes no scope -- but a fifth passing both would get an
    # internal submission refused by a mapping it had explicitly bypassed, which
    # contradicts this module's own precedence contract. Also skips a sort of up to
    # MAX_MAPPINGS entries on a per-document path.
    matched = None if submission_source else find_match(object_key, mappings or [])
    disclosed = [
        p
        for p in (assignment.profile, matched["configProfile"] if matched else None)
        if p
    ]
    if allowed_profiles is not None and any(
        not scope_allows(allowed_profiles, p) for p in disclosed
    ):
        return ConfigAssignment(
            # Nothing that could describe a profile survives: not the profile, not
            # the revision (a pinned number is an attribute of a profile the caller
            # cannot see), not `reason`, and not `mapping_prefix`.
            #
            # The prefix looks like the caller's own input and is not. The caller
            # supplied a KEY; the prefix is the mapping that governs it, so
            # returning it for `a/b/c/d/x.pdf` discloses that the boundary sits at
            # `a/b/` — a refinement of the input, and one probe at a time it walks
            # out the routing policy that `listConfigPrefixMappings` is Admin-only
            # to protect. The caller still learns the actionable part, which is that
            # the destination is not theirs.
            source=assignment.source,
            conflict=assignment.conflict,
            unresolvable=assignment.unresolvable,
            scope_denied=True,
            reason=(
                "That destination is governed by a Configuration Profile outside "
                "your allowed configuration scope."
            ),
        )
    return assignment


def _decide(
    object_key: str,
    *,
    metadata_profile: Optional[str],
    metadata_revision: Optional[int],
    submission_source: Optional[str],
    mappings: Optional[Sequence[Mapping[str, Any]]],
    active_profile: Optional[Callable[[], Optional[str]]],
    published_revision: Optional[Callable[[str], Optional[int]]],
    profile_exists: Optional[Callable[[str], bool]],
) -> ConfigAssignment:
    """The precedence rules, with no scope applied. See the caller."""
    entries = list(mappings or [])

    def _active() -> ConfigAssignment:
        profile = None
        if active_profile is not None:
            try:
                profile = active_profile()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Could not resolve the active profile: {e}")
        return ConfigAssignment(
            profile=profile,
            revision=_resolve_revision(profile, None, published_revision),
            source=SOURCE_ACTIVE_PROFILE,
            reason=(
                f"No prefix mapping matched and the upload named no profile, so the "
                f"active profile {profile!r} was used."
                if profile
                else "No prefix mapping matched, the upload named no profile, and no "
                "profile is active, so the default configuration was used."
            ),
        )

    def _from_metadata(reason: str) -> ConfigAssignment:
        # The revision is passed through as given rather than resolved to the
        # published one, because that is today's behaviour: an absent revision is
        # backfilled later by queue_processor, which is the only place that pin has
        # ever been made.
        return ConfigAssignment(
            profile=metadata_profile,
            revision=coerce_int(metadata_revision)
            if metadata_revision is not None
            else None,
            source=SOURCE_METADATA,
            reason=reason,
        )

    # (1) This deployment's own producers route themselves.
    if submission_source:
        if metadata_profile:
            return ConfigAssignment(
                profile=metadata_profile,
                revision=coerce_int(metadata_revision)
                if metadata_revision is not None
                else None,
                source=SOURCE_INTERNAL_PRODUCER,
                reason=(
                    f"Submitted by {submission_source!r}, which pins its own "
                    f"configuration ({metadata_profile!r}); prefix mappings do not "
                    f"apply to internal submissions."
                ),
            )
        return _active()

    match = find_match(object_key, entries)

    # (2) Nothing matched — today's behaviour, unchanged.
    if match is None:
        if metadata_profile:
            return _from_metadata(
                f"No prefix mapping matched; the upload named profile "
                f"{metadata_profile!r}."
            )
        return _active()

    prefix = match["prefix"]
    mapped_profile = match["configProfile"]
    precedence = match["metadataPrecedence"]

    # A mapping naming a profile that no longer exists must not strand the document.
    # It falls through to the next rule and the caller emits
    # PrefixMappingUnresolvable, because a routing table that has gone stale is an
    # operator problem rather than a reason to stop processing documents.
    if profile_exists is not None:
        try:
            exists = profile_exists(mapped_profile)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not confirm profile {mapped_profile!r} exists: {e}")
            exists = True
        if not exists:
            fallback = (
                _from_metadata(
                    f"Prefix mapping {prefix!r} names profile {mapped_profile!r}, "
                    f"which no longer exists; the upload's own profile "
                    f"{metadata_profile!r} was used instead."
                )
                if metadata_profile
                else _active()
            )
            return ConfigAssignment(
                profile=fallback.profile,
                revision=fallback.revision,
                source=fallback.source,
                mapping_prefix=prefix,
                unresolvable=True,
                reason=(
                    fallback.reason
                    if metadata_profile
                    else (
                        f"Prefix mapping {prefix!r} names profile "
                        f"{mapped_profile!r}, which no longer exists; the active "
                        f"profile was used instead."
                    )
                ),
            )

    mapped_revision = _resolve_revision(
        mapped_profile, match["configRevision"], published_revision
    )

    def _from_mapping(conflict: bool, reason: str) -> ConfigAssignment:
        return ConfigAssignment(
            profile=mapped_profile,
            # Note the revision never comes from the metadata here. Revision numbers
            # are per profile, so the metadata's r7 means nothing against a different
            # profile, and queue_processor will not correct it because its backfill
            # only fires when the revision is absent.
            revision=mapped_revision,
            source=SOURCE_PREFIX_MAPPING,
            mapping_prefix=prefix,
            conflict=conflict,
            reason=reason,
        )

    # (3) A mapping matched and the upload asked for nothing.
    if not metadata_profile:
        assignment = _from_mapping(
            False,
            f"Prefix mapping {prefix!r} assigned profile {mapped_profile!r}"
            + (f" r{mapped_revision}." if mapped_revision is not None else "."),
        )
    else:
        metadata_effective_revision = _resolve_revision(
            metadata_profile, metadata_revision, published_revision
        )
        agrees = (
            metadata_profile == mapped_profile
            and metadata_effective_revision == mapped_revision
        )
        if agrees:
            # (4) Agreement is not a conflict. Without this, `reject` would fail every
            # document the SDK stamped correctly and the conflict metric would fire
            # constantly on the harmless case.
            assignment = _from_mapping(
                False,
                f"Prefix mapping {prefix!r} and the upload both specify profile "
                f"{mapped_profile!r}"
                + (f" r{mapped_revision}." if mapped_revision is not None else "."),
            )
        elif precedence == PRECEDENCE_REJECT:
            # (5) reject
            return ConfigAssignment(
                source=SOURCE_REJECTED,
                mapping_prefix=prefix,
                conflict=True,
                rejected=True,
                reason=(
                    f"Refused: this object was uploaded to {prefix!r}, which is "
                    f"mapped to Configuration Profile {mapped_profile!r}"
                    + (f" r{mapped_revision}" if mapped_revision is not None else "")
                    + f", but it carries conflicting upload metadata naming "
                    f"{metadata_profile!r}"
                    + (
                        f" r{metadata_revision}"
                        if metadata_revision is not None
                        else ""
                    )
                    + ". That mapping is configured to refuse conflicting "
                    "submissions rather than choose between them."
                ),
            )
        elif precedence == PRECEDENCE_METADATA:
            assignment = ConfigAssignment(
                profile=metadata_profile,
                revision=metadata_effective_revision,
                source=SOURCE_METADATA,
                # The prefix is recorded even though the mapping lost, because "a
                # mapping was consulted and deferred" is a different fact from "no
                # mapping matched", and only the first explains a surprising profile.
                mapping_prefix=prefix,
                conflict=True,
                reason=(
                    f"Prefix mapping {prefix!r} assigns profile {mapped_profile!r}, "
                    f"but it is configured to defer to upload metadata, so the "
                    f"upload's profile {metadata_profile!r} was used."
                ),
            )
        else:
            assignment = _from_mapping(
                True,
                f"Prefix mapping {prefix!r} assigned profile {mapped_profile!r}"
                + (f" r{mapped_revision}" if mapped_revision is not None else "")
                + f"; the upload's own profile {metadata_profile!r} was ignored "
                f"because the mapping takes precedence.",
            )

    return assignment


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class PrefixMappingStore:
    """Reads and writes the single aggregate mapping item.

    ``table`` is a ``boto3.resource("dynamodb").Table``. Pass the region through when
    building that resource outside Lambda: a region-less client resolves a table *by
    name* in whatever region the ambient credentials land in, which writes another
    stack's routing table and reports success.
    """

    def __init__(self, table: Any):
        self.table = table

    # ----- plumbing --------------------------------------------------------

    @staticmethod
    def index_key() -> Dict[str, str]:
        return {"Configuration": PREFIX_MAP_INDEX_KEY}

    def _read_item(self, *, consistent: bool) -> Dict[str, Any]:
        """The aggregate item, or ``{}`` when it has never been written.

        ⚠️ A ``ClientError`` **propagates**, deliberately, and this is the one place
        where this store must not copy ``ConfigRevisionStore._read_index_item`` — that
        one logs and returns ``{}``. There it is harmless, because every mutation it
        feeds returns ``None`` when its target revision is absent, so nothing is
        written. Here a ``put`` appends unconditionally, so swallowing a throttle or a
        transient 5xx would turn one admin's write into "the mapping list is now
        exactly this one entry", silently deleting every other mapping, and report
        success. Read failures are handled by the *caller*, which knows whether it is
        on the ingest path (fall open, emit a metric) or the admin path (refuse).
        """
        response = self.table.get_item(Key=self.index_key(), ConsistentRead=consistent)
        return response.get("Item") or {}

    # ----- reads -----------------------------------------------------------

    def list(self, *, consistent: bool = True) -> List[Dict[str, Any]]:
        """Every mapping, normalized and ordered most-specific-first.

        Consistent by default: the ingest path must see an admin's change
        immediately, and an admin reading back their own write must see it. Raises on
        a read failure — see :meth:`_read_item`.
        """
        item = self._read_item(consistent=consistent)
        return sort_entries(item.get(MAPPINGS_ATTRIBUTE) or [])

    def get(self, prefix: str) -> Optional[Dict[str, Any]]:
        """One mapping by its prefix, which is its identity."""
        for entry in self.list():
            if entry["prefix"] == prefix:
                return entry
        return None

    # ----- writes ----------------------------------------------------------

    def _rewrite(self, mutate: Callable[[List[Dict[str, Any]]], Any]) -> None:
        """Read-modify-write the entry list, guarded by ``IndexSeq`` with one retry.

        ``mutate(entries)`` returns the new list, or ``None`` to abort without
        writing. Raises :class:`PrefixMappingConflict` when a concurrent write wins
        twice, rather than returning a boolean a caller might only log.
        """
        for attempt in (1, 2):
            item = self._read_item(consistent=True)
            entries = [normalize_entry(e) for e in item.get(MAPPINGS_ATTRIBUTE) or []]
            seq = coerce_int(item.get(SEQ_ATTRIBUTE))
            updated = mutate(entries)
            if updated is None:
                return
            if len(updated) > MAX_MAPPINGS:
                raise ValueError(
                    f"This deployment allows at most {MAX_MAPPINGS} configuration "
                    f"prefix mappings and already has {len(entries)}. Delete one "
                    f"before adding another."
                )
            try:
                self.table.update_item(
                    Key=self.index_key(),
                    UpdateExpression=(
                        f"SET {MAPPINGS_ATTRIBUTE} = :m, UpdatedAt = :ts, "
                        f"{SEQ_ATTRIBUTE} = :next"
                    ),
                    ConditionExpression=(
                        f"attribute_not_exists({SEQ_ATTRIBUTE}) OR "
                        f"{SEQ_ATTRIBUTE} = :seq"
                    ),
                    ExpressionAttributeValues={
                        ":m": updated,
                        ":ts": _now(),
                        ":seq": seq,
                        ":next": seq + 1,
                    },
                )
                return
            except ClientError as e:
                if (
                    e.response.get("Error", {}).get("Code")
                    != "ConditionalCheckFailedException"
                ):
                    raise
                logger.info(
                    "The configuration prefix mapping index changed underneath us "
                    f"(attempt {attempt}); retrying"
                )
        raise PrefixMappingConflict(
            "Another administrator changed the configuration prefix mappings while "
            "this change was being saved. Reload and try again."
        )

    def put(
        self,
        prefix: str,
        config_profile: str,
        *,
        config_revision: Optional[int] = None,
        metadata_precedence: str = DEFAULT_PRECEDENCE,
        enabled: bool = True,
        description: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create or replace the mapping for ``prefix``. Returns the stored entry.

        ``prefix`` is stored exactly as given — see :func:`match_kind`; the trailing
        slash is the prefix/exact mode selector, so normalizing it would silently
        change what the admin asked for. Validate with
        :func:`prefix_rejection_reason` first.
        """
        rejection = prefix_rejection_reason(prefix)
        if rejection:
            raise ValueError(rejection)
        if not config_profile:
            raise ValueError("A Configuration Profile is required.")
        if metadata_precedence not in PRECEDENCE_VALUES:
            raise ValueError(
                f"Unknown conflict mode {metadata_precedence!r}; expected one of "
                f"{', '.join(PRECEDENCE_VALUES)}."
            )

        stored: Dict[str, Any] = {}

        def mutate(entries: List[Dict[str, Any]]):
            previous = next((e for e in entries if e["prefix"] == prefix), None)
            entry = normalize_entry(
                {
                    "prefix": prefix,
                    "configProfile": config_profile,
                    "configRevision": config_revision,
                    "metadataPrecedence": metadata_precedence,
                    "enabled": enabled,
                    "description": description,
                    # Creation provenance survives an edit; an audit trail that an
                    # update overwrites is not one.
                    "createdAt": (previous or {}).get("createdAt") or _now(),
                    "createdBy": (previous or {}).get("createdBy") or actor or "system",
                    "updatedAt": _now(),
                    "updatedBy": actor or "system",
                }
            )
            stored.update(entry)
            remaining = [e for e in entries if e["prefix"] != prefix]
            return sort_entries([*remaining, entry])

        self._rewrite(mutate)
        logger.info(
            f"Configuration prefix mapping {prefix!r} -> {config_profile!r}"
            + (
                f" r{config_revision}"
                if config_revision is not None
                else " (published)"
            )
            + f", conflict mode {metadata_precedence!r}, by {actor or 'system'}"
        )
        return stored

    def delete(self, prefix: str) -> bool:
        """Remove the mapping for ``prefix``. Returns whether one was there.

        Deliberately does **not** unpin a revision this mapping pinned. A test run may
        have pinned the same revision, and nothing records which referent set the
        flag, so unpinning here could delete a configuration another feature still
        needs. Unpinning is an admin's explicit ``deleteConfigProfileRevision``.
        """
        found = {"value": False}

        def mutate(entries: List[Dict[str, Any]]):
            remaining = [e for e in entries if e["prefix"] != prefix]
            if len(remaining) == len(entries):
                return None
            found["value"] = True
            return remaining

        self._rewrite(mutate)
        if found["value"]:
            logger.info(f"Deleted configuration prefix mapping {prefix!r}")
        return found["value"]

    def profiles_in_use(self) -> List[str]:
        """Every profile some mapping names, for the delete-a-profile guard.

        Deleting a profile drops all of its revision bodies regardless of whether a
        mapping pinned one, so a mapping must not be allowed to outlive its profile:
        it would point at something ``ConfigurationManager`` cannot resolve and the
        documents would silently run under the default configuration.
        """
        return sorted({e["configProfile"] for e in self.list() if e["configProfile"]})
