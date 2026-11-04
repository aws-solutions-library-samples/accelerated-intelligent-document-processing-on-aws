# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tenant declarations: which Input-bucket prefixes and which Configuration Profiles
belong to one tenant.

A tenant is not a new access mechanism. Isolation in this deployment already comes
from two existing pieces:

* **Prefix mappings** (:mod:`idp_common.config.prefix_mappings`) decide which profile
  a document processes under from where it lands in the Input bucket.
* **Configuration scope** (:mod:`idp_common.config_scope`) decides which documents a
  user sees from the profile each document was stamped with.

Chained, those give prefix → profile → visibility. What they do not give is any
record that ``acme/`` and ``acme-*`` belong together, so nothing stops an admin
mapping ``acme/sub/`` to ``globex-x`` (most-specific-wins then quietly moves those
documents into Globex's view), scoping a user to profiles of two tenants, or an
uploader naming another tenant's profile in its metadata. A tenant declaration is
that record, and this module is the set of checks that read it.

Shape of a declaration
----------------------

``tenantId``
    Lowercase letters, digits and hyphens. An identifier, not a display name.
``prefixes``
    One or more Input-bucket prefix subtrees, each ending in ``/``. No prefix may
    equal, contain or sit inside another tenant's prefix, so every key belongs to at
    most one tenant.
``profilePattern``
    A literal **stem** ending in a separator (``-``, ``_`` or ``.``) followed by
    ``*`` — ``acme-*``. No tenant's stem may be a prefix of another's (``acme-*``
    and ``acme-eu-*`` are refused together), so every profile name belongs to at
    most one tenant. That is what makes "which tenant owns this profile?" a
    function rather than a question, and it is why a profile *create* needs no
    tenant check of its own: a new name either falls under exactly one stem or
    under none.

    A bare ``acme*`` is refused because it also matches ``acmecorp-x``. The
    separator is the boundary, the same way the trailing ``/`` is the boundary for
    a prefix.

The checks
----------

Every function below except :class:`TenantStore` is pure — no boto3, no
environment — for the reason ``prefix_mappings.resolve_config_assignment`` is: the
rules are a table, and a table is only verifiable when it can be tested as one.

* :func:`tenant_rejection_reason` — is this declaration well-formed and disjoint
  from the others?
* :func:`mapping_rejection_reason` — does a prefix mapping keep a tenant's documents
  inside that tenant's profiles? Applied when a mapping is saved, and to every
  existing mapping when a tenant is saved.
* :func:`scope_rejection_reason` — does a user's ``allowedConfigVersions`` stay
  inside one tenant?
* :func:`ingest_rejection_reason` — at ingest, in strict mode, do the key's tenant
  and the resolved profile's tenant agree?

Declaring tenants turns on the first three as **write-time** refusals in every
mode. The ingest check runs only when the stack's ``TenantIsolationMode`` is
``Strict``; see :func:`is_strict_isolation`.

Storage
-------

One item in the ``ConfigurationTable``, ``Configuration = "TenantIndex#__index"``,
with the same optimistic-concurrency counter ``PrefixMappingStore`` uses. The key
does not match ``begins_with(Configuration, "Config#")``, so the item can never be
listed as a profile.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from botocore.exceptions import ClientError

from idp_common.config.prefix_mappings import (
    MATCH_EXACT,
    canonical_key,
    description_rejection_reason,
    match_kind,
    prefix_rejection_reason,
)
from idp_common.config_scope import (  # noqa: F401 - re-exported
    ISOLATION_OFF,
    ISOLATION_STRICT,
    TENANT_ISOLATION_ENV,
    is_pattern,
    is_strict_isolation,
    normalize_scope,
)
from idp_common.ddb_numbers import coerce_int

logger = logging.getLogger(__name__)

TENANT_INDEX_PREFIX = "TenantIndex"
TENANT_INDEX_KEY = f"{TENANT_INDEX_PREFIX}#__index"
TENANTS_ATTRIBUTE = "Tenants"
SEQ_ATTRIBUTE = "IndexSeq"

# Bounds. The aggregate item is read on the ingest path in strict mode, and a write
# DynamoDB refuses for size surfaces as an opaque ValidationException rather than
# this module's own message, so the worst case is held well below the 400 KB item
# limit. One tenant at every field's bound (5 prefixes of 512 bytes, 3 grant values
# of 128 bytes, a 500-byte description, two 256-byte actor strings) serializes to
# about 4.3 KB, so 65 tenants is about 280 KB -- over a quarter of the item left for
# the key, the counter and DynamoDB's own accounting of attribute names.
# `test_the_bounds_hold_the_item_limit` measures the serialized item against that
# 300 KB budget, not against the limit.
MAX_TENANTS = 65
MAX_PREFIXES_PER_TENANT = 5
MAX_GRANTS_PER_TENANT = 3
_MAX_GRANT_LEN = 128
_MAX_ACTOR_LEN = 256

TENANT_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")
# A stem of profile-name characters (the set `validate_version_name` accepts) that
# ends in a separator, then a single trailing `*`.
PROFILE_PATTERN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,46}[-_.])\*$")


class TenantConflict(RuntimeError):
    """A concurrent write won the tenant index; the caller's write did not land."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def pattern_stem(pattern: str) -> Optional[str]:
    """The literal stem of a well-formed profile pattern (``acme-`` for ``acme-*``)."""
    match = PROFILE_PATTERN_RE.match(pattern or "")
    return match.group(1) if match else None


def normalize_tenant(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """One stored declaration, with every field at its declared type."""

    def _actor(name: str) -> Optional[str]:
        value = str(entry.get(name) or "")
        return value[:_MAX_ACTOR_LEN] or None

    prefixes = entry.get("prefixes") or []
    if isinstance(prefixes, str):
        prefixes = [prefixes]
    granted_by = entry.get("grantedBy") or []
    if isinstance(granted_by, str):
        granted_by = [granted_by]
    return {
        "tenantId": str(entry.get("tenantId") or ""),
        "prefixes": [str(p) for p in prefixes if p],
        "profilePattern": str(entry.get("profilePattern") or ""),
        "grantedBy": [str(g) for g in granted_by if g],
        "description": (str(entry.get("description") or "") or None),
        "createdAt": entry.get("createdAt"),
        "createdBy": _actor("createdBy"),
        "updatedAt": entry.get("updatedAt"),
        "updatedBy": _actor("updatedBy"),
    }


def sort_tenants(entries: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return sorted((normalize_tenant(e) for e in entries), key=lambda t: t["tenantId"])


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def _within(key: str, prefix: str) -> bool:
    """Whether canonical ``key`` lies in the subtree of a tenant ``prefix``."""
    root = canonical_key(prefix)
    return bool(root) and key.startswith(root)


def tenant_for_key(
    object_key: str, tenants: Sequence[Mapping[str, Any]]
) -> Optional[Dict[str, Any]]:
    """The tenant whose prefix subtree contains ``object_key``, or ``None``.

    The key is canonicalized first, for the reason ``prefix_mappings.canonical_key``
    gives: ``/acme/x.pdf`` and ``acme//x.pdf`` are distinct S3 keys, and an
    uncanonicalized comparison would make either one a route out of the tenant.
    Prefixes are disjoint by construction, so at most one tenant matches.
    """
    key = canonical_key(object_key)
    if not key:
        return None
    for tenant in tenants:
        t = normalize_tenant(tenant)
        if any(_within(key, p) for p in t["prefixes"]):
            return t
    return None


def tenant_for_profile(
    profile_name: Optional[str], tenants: Sequence[Mapping[str, Any]]
) -> Optional[Dict[str, Any]]:
    """The tenant whose profile pattern matches ``profile_name``, or ``None``."""
    if not profile_name:
        return None
    for tenant in tenants:
        t = normalize_tenant(tenant)
        stem = pattern_stem(t["profilePattern"])
        if stem and str(profile_name).startswith(stem):
            return t
    return None


def tenant_for_scope_entry(
    entry: str, tenants: Sequence[Mapping[str, Any]]
) -> Optional[Dict[str, Any]]:
    """The tenant one ``allowedConfigVersions`` entry is confined to, or ``None``.

    A literal name belongs to the tenant that owns that profile. A glob belongs to a
    tenant only if its literal head already carries the tenant's whole stem — then
    nothing the glob can match falls outside the tenant (``acme-eu-*`` and
    ``acme-?`` are inside ``acme-*``; ``acme*`` and ``a*`` are not).
    """
    if not is_pattern(entry):
        return tenant_for_profile(entry, tenants)
    head = re.split(r"[*?\[]", entry, maxsplit=1)[0]
    for tenant in tenants:
        t = normalize_tenant(tenant)
        stem = pattern_stem(t["profilePattern"])
        if stem and head.startswith(stem):
            return t
    return None


def _spans_tenant(entry: str, tenants: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a glob entry outside every tenant could still match a tenant profile."""
    if not is_pattern(entry):
        return False
    head = re.split(r"[*?\[]", entry, maxsplit=1)[0]
    for tenant in tenants:
        stem = pattern_stem(normalize_tenant(tenant)["profilePattern"]) or ""
        # `a*` can match `acme-x`: its head is a prefix of the stem. A bracket or `?`
        # immediately after the head can too, so any head that is a prefix of a stem
        # is treated as reaching into that tenant.
        if stem and stem.startswith(head):
            return True
    return False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def tenant_rejection_reason(
    entry: Mapping[str, Any], others: Sequence[Mapping[str, Any]]
) -> Optional[str]:
    """Why ``entry`` cannot be saved beside ``others``, or ``None``.

    ``others`` are the *other* declarations; an entry with the same ``tenantId`` is
    ignored, so this serves an edit as well as a create.
    """
    t = normalize_tenant(entry)
    tenant_id = t["tenantId"]
    if not TENANT_ID_RE.match(tenant_id):
        return (
            "A tenant ID must be 1-63 characters of lowercase letters, digits and "
            "hyphens, starting with a letter or digit."
        )
    if not t["prefixes"]:
        return "A tenant needs at least one Input-bucket prefix."
    if len(t["prefixes"]) > MAX_PREFIXES_PER_TENANT:
        return f"A tenant may declare at most {MAX_PREFIXES_PER_TENANT} prefixes."
    for prefix in t["prefixes"]:
        reason = prefix_rejection_reason(prefix)
        if reason:
            return reason
        if match_kind(prefix) == MATCH_EXACT:
            return (
                f"Tenant prefix {prefix!r} must end in '/'. A tenant owns a folder "
                f"subtree; without the slash, 'acme' would also own 'acmecorp/'."
            )
    own = [canonical_key(p) for p in t["prefixes"]]
    for i, a in enumerate(own):
        for b in own[i + 1 :]:
            if a.startswith(b) or b.startswith(a):
                return f"Prefixes {a!r} and {b!r} overlap; declare only the outer one."

    stem = pattern_stem(t["profilePattern"])
    if not stem:
        return (
            "A profile pattern must be a literal stem ending in '-', '_' or '.', "
            "followed by '*' — for example 'acme-*'. The separator is the boundary: "
            "'acme*' would also match 'acmecorp-invoices'."
        )
    reason = description_rejection_reason(t["description"])
    if reason:
        return reason
    reason = _grants_rejection_reason(t["grantedBy"])
    if reason:
        return reason

    for other in others:
        o = normalize_tenant(other)
        if o["tenantId"] == tenant_id:
            continue
        for mine in own:
            for theirs in (canonical_key(p) for p in o["prefixes"]):
                if mine.startswith(theirs) or theirs.startswith(mine):
                    return (
                        f"Prefix {mine!r} overlaps tenant {o['tenantId']!r}'s prefix "
                        f"{theirs!r}. Tenant prefixes must be disjoint so that every "
                        f"document belongs to at most one tenant."
                    )
        shared = sorted(set(t["grantedBy"]) & set(o["grantedBy"]))
        if shared:
            return (
                f"Grant value {shared[0]!r} already grants tenant {o['tenantId']!r}. "
                f"A value may grant at most one tenant, so that a user's claim "
                f"resolves to one answer."
            )
        other_stem = pattern_stem(o["profilePattern"]) or ""
        if other_stem and (stem.startswith(other_stem) or other_stem.startswith(stem)):
            return (
                f"Profile pattern {t['profilePattern']!r} overlaps tenant "
                f"{o['tenantId']!r}'s pattern {o['profilePattern']!r}: some profile "
                f"names would match both. Choose stems neither of which begins the "
                f"other."
            )
    return None


def _grants_rejection_reason(values: Sequence[str]) -> Optional[str]:
    """Why a declaration's ``grantedBy`` values cannot be stored, or ``None``.

    The values are opaque: an Entra app-role value such as ``tenant.acme``, an Okta
    group name, an entitlement name an external system pushes. Nothing here knows
    which identity provider produced them, which is what keeps the feature from
    depending on one.
    """
    if len(values) > MAX_GRANTS_PER_TENANT:
        return f"A tenant may be granted by at most {MAX_GRANTS_PER_TENANT} values."
    if len(set(values)) != len(values):
        return "A grant value is listed twice."
    for value in values:
        if value != value.strip():
            return (
                f"Grant value {value!r} begins or ends with whitespace, so it would "
                f"never equal the value an identity provider sends."
            )
        if len(value.encode("utf-8")) > _MAX_GRANT_LEN:
            return f"A grant value may be at most {_MAX_GRANT_LEN} bytes."
    return None


#: The outcome of resolving a user's grant values against the tenant declarations.
GRANT_NONE = "none"
GRANT_TENANT = "tenant"
GRANT_REFUSED = "refused"


def resolve_grants(
    values: Iterable[str], tenants: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Which tenant a user's grant values give them.

    Returns ``{"outcome", "tenant", "reason"}``. ``outcome`` is ``GRANT_TENANT`` for
    exactly one tenant, ``GRANT_NONE`` when no value grants any tenant, and
    ``GRANT_REFUSED`` when the values grant two or more distinct tenants: a user is
    confined to one tenant, so several is refused rather than resolved to the union
    or to whichever came first. Values that grant nothing are ignored, because the
    same claim also carries the user's functional roles.
    """
    wanted = {str(v) for v in values if v}
    matched = [
        normalize_tenant(t)
        for t in tenants
        if wanted & set(normalize_tenant(t)["grantedBy"])
    ]
    if not matched:
        return {"outcome": GRANT_NONE, "tenant": None, "reason": None}
    if len(matched) > 1:
        ids = ", ".join(sorted(t["tenantId"] for t in matched))
        return {
            "outcome": GRANT_REFUSED,
            "tenant": None,
            "reason": (
                f"The user is granted tenants {ids}. A user is confined to one "
                f"tenant, so no tenant is granted until all but one are removed."
            ),
        }
    return {"outcome": GRANT_TENANT, "tenant": matched[0], "reason": None}


def mapping_rejection_reason(
    prefix: str, profile: str, tenants: Sequence[Mapping[str, Any]]
) -> Optional[str]:
    """Why a prefix mapping ``prefix → profile`` breaks a tenant boundary, or ``None``.

    Two directions, both refused:

    * a mapping **inside** tenant T must name one of T's profiles, or T's documents
      are processed under — and so become visible to — another tenant's profile;
    * a mapping **outside** every tenant must not name a tenant's profile, or
      documents from outside T are placed into T's view.

    A mapping whose prefix is an *ancestor* of a tenant subtree (``clients/`` above
    ``clients/acme/``) governs keys inside that tenant wherever the tenant has no
    more specific mapping of its own. Its profile is judged as outside-every-tenant,
    and the ingest check catches a tenant key it would govern.
    """
    # The tenant whose subtree contains the mapping prefix. `tenant_for_key`
    # answers that for a folder prefix as well as an exact key, because a tenant
    # root `acme/` is a prefix of the mapping prefix `acme/sub/`.
    owner = tenant_for_key(prefix, tenants)
    profile_owner = tenant_for_profile(profile, tenants)
    if owner and (not profile_owner or profile_owner["tenantId"] != owner["tenantId"]):
        return (
            f"Prefix {prefix!r} belongs to tenant {owner['tenantId']!r}, so it must "
            f"map to one of that tenant's profiles ({owner['profilePattern']}); "
            f"{profile!r} is not one."
        )
    if not owner and profile_owner:
        return (
            f"Profile {profile!r} belongs to tenant {profile_owner['tenantId']!r}, but "
            f"prefix {prefix!r} is outside that tenant's prefixes "
            f"({', '.join(profile_owner['prefixes'])}). Mapping it would place "
            f"documents from outside the tenant into the tenant's view."
        )
    return None


def mappings_rejection_reason(
    mappings: Sequence[Mapping[str, Any]], tenants: Sequence[Mapping[str, Any]]
) -> Optional[str]:
    """The first existing mapping a tenant set would put across a boundary, or ``None``.

    Used when a tenant is saved: declaring ``acme/`` over a mapping that already
    sends ``acme/x/`` to ``globex-x`` must be refused, not left for ingest to find.
    """
    for mapping in mappings:
        reason = mapping_rejection_reason(
            str(mapping.get("prefix") or ""),
            str(mapping.get("configProfile") or ""),
            tenants,
        )
        if reason:
            return f"Existing prefix mapping conflicts with this tenant set. {reason}"
    return None


def scope_rejection_reason(
    scope: Any, tenants: Sequence[Mapping[str, Any]]
) -> Optional[str]:
    """Why ``allowedConfigVersions`` reaches beyond one tenant, or ``None``.

    With no tenants declared there is nothing to check. Otherwise every entry must be
    confined to the same tenant, or every entry to none:

    * ``["acme-*"]`` and ``["acme-invoices", "acme-eu-*"]`` — one tenant, accepted;
    * ``["acme-*", "globex-*"]`` — two tenants, refused;
    * ``["acme-x", "shared-demo"]`` — a tenant plus an untenanted profile, refused,
      because an untenanted profile is visible to every user scoped to it and so is
      a channel between tenants;
    * ``["a*"]`` — outside every tenant but able to match ``acme-x``, refused.
    """
    entries = normalize_scope(scope)
    if not entries or not tenants:
        return None
    owners = set()
    for entry in entries:
        owner = tenant_for_scope_entry(entry, tenants)
        if owner is None and _spans_tenant(entry, tenants):
            return (
                f"Scope entry {entry!r} is not confined to a single tenant: it can "
                f"match profiles belonging to a tenant without carrying that "
                f"tenant's whole stem. Use the tenant's pattern, or a narrower one."
            )
        owners.add(owner["tenantId"] if owner else None)
    if len(owners) > 1:
        named = sorted(o for o in owners if o)
        if None in owners:
            return (
                f"This scope mixes tenant {', '.join(named)} with profiles that "
                f"belong to no tenant. A user is confined to one tenant's profiles."
            )
        return (
            f"This scope spans tenants {', '.join(named)}. A user is confined to one "
            f"tenant's profiles."
        )
    return None


def stored_scopes(users_table: Any) -> List[Dict[str, Any]]:
    """Every UsersTable row that carries an ``allowedConfigVersions`` scope.

    Each entry is ``{"email": ..., "allowedConfigVersions": [...]}``. Paginated to
    the end, because a check that judges only the first page of users would report
    a tenant set as consistent while the user that breaks it sits on page two.
    Projects only the key, the email, the persona (to skip Admins) and the scope.
    ``users_table`` is a
    ``boto3.resource("dynamodb").Table``; read failures propagate, and the caller
    refuses the write it was checking.
    """
    kwargs: Dict[str, Any] = {
        "ProjectionExpression": "PK, email, persona, allowedConfigVersions",
        "FilterExpression": "attribute_exists(allowedConfigVersions)",
    }
    rows: List[Dict[str, Any]] = []
    while True:
        page = users_table.scan(**kwargs)
        for item in page.get("Items") or []:
            # A user's own row is keyed `USER#<id>`; `SUB#` pointer items carry no
            # scope of their own. Admins are skipped because an Admin is a
            # cross-tenant role by design: a stored Admin scope only ever NARROWS
            # an Admin, so it is not a tenant boundary to check. It is not inert,
            # though -- `upload_resolver` applies it to an Admin's web uploads --
            # which is why the refusal below and `idp-cli tenancy audit` say they
            # cover non-Admin scopes rather than every scope.
            if not str(item.get("PK") or "").startswith("USER#"):
                continue
            if item.get("persona") == "Admin":
                continue
            scope = normalize_scope(item.get("allowedConfigVersions"))
            if scope:
                rows.append(
                    {
                        "email": str(item.get("email") or ""),
                        "allowedConfigVersions": scope,
                    }
                )
        if "LastEvaluatedKey" not in page:
            return rows
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def stored_scopes_rejection_reason(
    scopes: Sequence[Mapping[str, Any]], tenants: Sequence[Mapping[str, Any]]
) -> Optional[str]:
    """Why a tenant set would leave existing user scopes crossing a boundary.

    ``scopes`` is what :func:`stored_scopes` returns. Used when a tenant is saved:
    declaring ``acme-*`` and ``globex-*`` over a user already scoped to both would
    otherwise leave that user reaching two tenants, because a scope is checked only
    when it is written and nothing re-checks it after.

    The refusal names how many users are affected and one of them by email. It is
    only reachable by an Admin (the tenant operations are Admin-only), who manages
    those users and needs a starting point to fix them; ``idp-cli tenancy audit``
    lists all of them.
    """
    offending = [
        (row.get("email") or "a user with no email", reason)
        for row in scopes
        if (reason := scope_rejection_reason(row.get("allowedConfigVersions"), tenants))
    ]
    if not offending:
        return None
    email, reason = offending[0]
    others = len(offending) - 1
    return (
        f"Existing user scopes conflict with this tenant set: {email}'s scope is "
        f"refused ({reason})"
        + (f", and {others} other user(s) are affected." if others else ".")
        + " Narrow those scopes first; `idp-cli tenancy audit` lists every non-Admin"
        " scope that needs it."
    )


def ingest_rejection_reason(
    object_key: str,
    profile: Optional[str],
    tenants: Sequence[Mapping[str, Any]],
    *,
    submission_source: Optional[str] = None,
    test_run_profile: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[str]:
    """Strict-mode ingest check: why this document must be refused, or ``None``.

    The rule is that **the tenant owning the key and the tenant owning the resolved
    profile must be the same tenant**, and an *external* upload that lands outside
    every tenant is refused.

    That one comparison is what makes upload metadata safe to honour. A principal
    holding ``s3:PutObject`` chooses the key, and can set ``config-version`` and
    ``submission-source`` to anything; none of that matters if the outcome must
    still land inside the key's own tenant. It needs no knowledge of who wrote the
    object, which S3 events do not carry.

    Two cases where the sides are both ``None``:

    * an internal producer (``submission_source`` set) — accepted. A forged source
      outside every tenant reaches only an untenanted profile, which no tenant user
      can be scoped to (:func:`scope_rejection_reason`);
    * anything else — refused. Strict mode does not process an external upload that
      lands outside every tenant, because nobody's scope would ever cover it and it
      would sit invisible to all but Admins.

    And one case where they differ that is legitimate: **Test Studio** copies a
    test set to ``<testRunId>/...`` at the bucket root and processes it under the
    run's profile, which may be a tenant's. That is accepted only when
    ``test_run_profile(<first key segment>)`` — a lookup of the stored test-run
    record, which no uploader can create — names that same profile.
    """
    if not tenants:
        # Strict mode with no tenants declared has no boundary to compare against;
        # what it still does is the scope half (`config_scope`).
        return None
    key_owner = tenant_for_key(object_key, tenants)
    profile_owner = tenant_for_profile(profile, tenants)
    key_tid = key_owner["tenantId"] if key_owner else None
    profile_tid = profile_owner["tenantId"] if profile_owner else None

    if key_tid == profile_tid:
        if key_tid is None and not submission_source:
            return (
                "Refused: this object is outside every tenant's prefixes, and this "
                "deployment runs in strict tenant isolation, so a document must land "
                "under a tenant prefix to be processed."
            )
        return None

    if (
        key_tid is None
        and submission_source == "test-studio"
        and test_run_profile is not None
        and profile
    ):
        run_id = canonical_key(object_key).split("/", 1)[0]
        try:
            recorded = test_run_profile(run_id)
        except Exception as e:  # noqa: BLE001 - an unverifiable claim is refused
            logger.warning(f"Could not verify test run {run_id!r}: {e}")
            recorded = None
        if recorded and recorded == profile:
            return None

    if key_tid:
        return (
            f"Refused: this object is under tenant {key_tid!r}'s prefixes but would "
            f"be processed under profile {profile!r}, which "
            + (
                f"belongs to tenant {profile_tid!r}."
                if profile_tid
                else "belongs to no tenant."
            )
            + " In strict tenant isolation a document must be processed under its "
            "own tenant's profiles."
        )
    return (
        f"Refused: profile {profile!r} belongs to tenant {profile_tid!r}, but this "
        f"object is outside that tenant's prefixes. In strict tenant isolation a "
        f"document cannot be placed into a tenant's view from outside it."
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class TenantStore:
    """Reads and writes the single tenant-index item.

    ``table`` is a ``boto3.resource("dynamodb").Table`` for the ConfigurationTable.
    Read failures propagate, for the reason ``PrefixMappingStore._read_item`` gives:
    a swallowed throttle would turn a ``put`` into "the tenant list is now this one
    entry", and on the ingest path the caller is the one that knows whether to fail
    closed.
    """

    def __init__(self, table: Any):
        self.table = table

    @staticmethod
    def index_key() -> Dict[str, str]:
        return {"Configuration": TENANT_INDEX_KEY}

    def _read_item(self, *, consistent: bool) -> Dict[str, Any]:
        response = self.table.get_item(Key=self.index_key(), ConsistentRead=consistent)
        return response.get("Item") or {}

    def list(self, *, consistent: bool = True) -> List[Dict[str, Any]]:
        return sort_tenants(
            self._read_item(consistent=consistent).get(TENANTS_ATTRIBUTE) or []
        )

    def get(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        return next((t for t in self.list() if t["tenantId"] == tenant_id), None)

    def _rewrite(self, mutate: Callable[[List[Dict[str, Any]]], Any]) -> None:
        for attempt in (1, 2):
            item = self._read_item(consistent=True)
            entries = [normalize_tenant(e) for e in item.get(TENANTS_ATTRIBUTE) or []]
            seq = coerce_int(item.get(SEQ_ATTRIBUTE))
            updated = mutate(entries)
            if updated is None:
                return
            # Only a mutation that grows the set is refused, so a deployment that
            # already holds more than the cap (it was 75 before Granted by values
            # enlarged each entry) can still edit and delete its way back under it.
            if len(updated) > MAX_TENANTS and len(updated) > len(entries):
                raise ValueError(
                    f"This deployment allows at most {MAX_TENANTS} tenants, so no "
                    "more can be added. Existing tenants can still be edited or "
                    "deleted."
                )
            try:
                self.table.update_item(
                    Key=self.index_key(),
                    UpdateExpression=(
                        f"SET {TENANTS_ATTRIBUTE} = :t, UpdatedAt = :ts, "
                        f"{SEQ_ATTRIBUTE} = :next"
                    ),
                    ConditionExpression=(
                        f"attribute_not_exists({SEQ_ATTRIBUTE}) OR {SEQ_ATTRIBUTE} = :seq"
                    ),
                    ExpressionAttributeValues={
                        ":t": updated,
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
                    f"The tenant index changed underneath us (attempt {attempt}); retrying"
                )
        raise TenantConflict(
            "Another administrator changed the tenants while this change was being "
            "saved. Reload and try again."
        )

    def put(
        self,
        tenant_id: str,
        prefixes: Sequence[str],
        profile_pattern: str,
        *,
        description: Optional[str] = None,
        granted_by: Optional[Sequence[str]] = None,
        actor: Optional[str] = None,
        validate_against: Optional[
            Callable[[List[Dict[str, Any]]], Optional[str]]
        ] = None,
    ) -> Dict[str, Any]:
        """Create or replace one declaration. Returns the stored entry.

        ``validate_against(new_tenant_set)`` is called inside the read-modify-write
        with the tenant set as it would be after this write, so a check against the
        existing prefix mappings sees the same snapshot the write is conditioned on.
        Return a reason to refuse.
        """
        stored: Dict[str, Any] = {}

        def mutate(entries: List[Dict[str, Any]]):
            previous = next((e for e in entries if e["tenantId"] == tenant_id), None)
            entry = normalize_tenant(
                {
                    "tenantId": tenant_id,
                    "prefixes": list(prefixes),
                    "profilePattern": profile_pattern,
                    "description": description,
                    "grantedBy": list(granted_by or []),
                    "createdAt": (previous or {}).get("createdAt") or _now(),
                    "createdBy": (previous or {}).get("createdBy") or actor or "system",
                    "updatedAt": _now(),
                    "updatedBy": actor or "system",
                }
            )
            others = [e for e in entries if e["tenantId"] != tenant_id]
            reason = tenant_rejection_reason(entry, others)
            if reason is None and validate_against is not None:
                reason = validate_against(sort_tenants([*others, entry]))
            if reason:
                raise ValueError(reason)
            stored.clear()
            stored.update(entry)
            return sort_tenants([*others, entry])

        self._rewrite(mutate)
        logger.info(
            f"Tenant {tenant_id!r} -> prefixes {list(prefixes)}, profiles "
            f"{profile_pattern!r}, by {actor or 'system'}"
        )
        return stored

    def delete(self, tenant_id: str) -> bool:
        """Remove one declaration. Returns whether one remained to remove."""
        found = {"value": False}

        def mutate(entries: List[Dict[str, Any]]):
            remaining = [e for e in entries if e["tenantId"] != tenant_id]
            found["value"] = len(remaining) != len(entries)
            return remaining if found["value"] else None

        self._rewrite(mutate)
        if found["value"]:
            logger.info(f"Deleted tenant {tenant_id!r}")
        return found["value"]
