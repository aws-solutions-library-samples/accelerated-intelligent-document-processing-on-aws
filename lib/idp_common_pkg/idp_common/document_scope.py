# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Configuration-scope checks for operations that act on one document by key.

A document belongs to the Configuration Profile it was processed under — the
``ConfigVersion`` on its tracking row — and a scoped caller may act only on
documents whose profile their ``allowedConfigVersions`` allows. The document-list
resolvers, the object-read path and the chat processor already apply that rule.
This module is the shared form of it for every other operation that takes an
object key: reading the tracking row, the document's versions, its review
actions, deleting it, aborting its workflow, copying it to a baseline.

The rule, which every helper below applies identically:

* **An Admin is unrestricted** and is never looked up, matching every other
  consumer of the scope (``config_scope`` explains why that is the consumer's job).
* **Anyone else is looked up**, failing CLOSED: a scope that cannot be evaluated is
  refused, never read as unrestricted (``ScopeLookupError``). Under
  ``TenantIsolationMode=Strict`` an unscoped caller resolves to a scope that
  matches nothing, so this module refuses them every document without a branch of
  its own.
* **A scoped caller is refused** a document whose profile the scope does not allow,
  a document with no profile recorded, and a key no document is tracked under. The
  three are refused with one message, so the refusal is not an existence oracle.

Every refusal is a ``PermissionError`` with an ``Unauthorized`` prefix, which the
dispatcher maps to HTTP 403, logged at WARNING with a "Denied" verb and no stack
trace, per the API authorization rules.

This module reads one item per document (``GetItem`` on ``PK = doc#<key>``,
``SK = none``), projected to the profile attributes. It deliberately does not walk
key prefixes the way the object-read path's ``document_scope`` does: every caller
here names a document by its own key, never an object underneath it.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, List, Mapping, MutableMapping, Optional

from idp_common.config_scope import (
    SCOPE_CACHE_TTL_SECONDS,
    ScopeLookupError,
    caller_email_from_claims,
    caller_sub_from_claims,
    resolve_allowed_config_versions,
    scope_allows,
)

logger = logging.getLogger(__name__)

# The ``Unauthorized`` prefix is what maps the refusal to a 403 alongside the class.
REFUSAL = "Unauthorized: you are not permitted to act on that document."

# Never configuration-scoped. The same set the object-read path and the
# configuration resolver use; a caller one path serves and another refuses would be
# two enforcements of one rule disagreeing.
UNSCOPED_GROUPS = ("Admin",)

# ``ConfigurationVersion`` is the older spelling, still on rows written before the
# rename. Treating such a row as unstamped would refuse a scoped caller a document
# that is in their scope.
CONFIG_VERSION_ATTRIBUTES = ("ConfigVersion", "ConfigurationVersion")

DOCUMENT_PK_PREFIX = "doc#"
DOCUMENT_SK = "none"

#: Returned in place of a profile when the tracking table has no row for the key.
ABSENT = object()


def caller_claims(event: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The verified claims on ``event``, or a refusal when there are none.

    An event with no identity is not an unrestricted caller. A resolver that also
    serves service-to-service invocations must decide that BEFORE calling this
    module, from its own direct-invoke rule, rather than have this module wave an
    absent identity through.
    """
    identity = (event or {}).get("identity")
    claims = identity.get("claims") if isinstance(identity, Mapping) else None
    if not isinstance(claims, Mapping):
        logger.warning(
            "Denied a document operation: the request carries no verified identity, "
            "so no configuration scope can be evaluated for it."
        )
        raise PermissionError(REFUSAL)
    return claims


def caller_groups(claims: Mapping[str, Any]) -> List[str]:
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return [str(g) for g in groups]


def is_unscoped_caller(claims: Mapping[str, Any]) -> bool:
    return any(group in UNSCOPED_GROUPS for group in caller_groups(claims))


def resolve_caller_scope(
    claims: Mapping[str, Any],
    *,
    users_table_name: str,
    dynamodb: Any,
    cache: Optional[MutableMapping[str, Any]] = None,
    cache_ttl: float = SCOPE_CACHE_TTL_SECONDS,
) -> Optional[List[str]]:
    """The caller's scope, ``None`` for unrestricted, or a refusal.

    ``dynamodb`` may be a ``boto3.resource("dynamodb")`` or a zero-argument callable
    returning one; the callable form lets an Admin request return before any client
    is built.
    """
    if is_unscoped_caller(claims):
        return None
    resource = dynamodb() if callable(dynamodb) else dynamodb
    try:
        return resolve_allowed_config_versions(
            caller_email_from_claims(claims),
            caller_sub=caller_sub_from_claims(claims),
            users_table_name=users_table_name,
            dynamodb=resource,
            cache=cache,
            cache_ttl=cache_ttl,
        )
    except ScopeLookupError as exc:
        logger.warning(
            "Denied a document operation: the caller's allowedConfigVersions could "
            "not be resolved (%s)",
            exc,
        )
        raise PermissionError(REFUSAL) from exc


def tracked_config_version(tracking_table: Any, object_key: str) -> Any:
    """The profile on ``object_key``'s tracking row, ``None`` when it records none,
    or :data:`ABSENT` when there is no row."""
    response = tracking_table.get_item(
        Key={"PK": f"{DOCUMENT_PK_PREFIX}{object_key}", "SK": DOCUMENT_SK},
        ProjectionExpression="PK, " + ", ".join(CONFIG_VERSION_ATTRIBUTES),
    )
    return config_version_of_item(response.get("Item"))


def config_version_of_item(item: Optional[Mapping[str, Any]]) -> Any:
    """The profile recorded on a tracking row already in hand, in the same three-way
    form as :func:`tracked_config_version` — for a caller that has read the row for
    its own purposes and must not read it twice."""
    if not item:
        return ABSENT
    for attribute in CONFIG_VERSION_ATTRIBUTES:
        value = item.get(attribute)
        if value:
            return str(value)
    return None


def is_absent(value: Any) -> bool:
    return value is ABSENT


def assert_profile_in_scope(
    scope: Optional[Iterable[str]], profile: Optional[str], *, what: str = "document"
) -> None:
    """Refuse unless ``scope`` (``None`` = unrestricted) allows ``profile``."""
    if scope is None:
        return
    if not scope_allows(list(scope), profile):
        logger.warning(
            "Denied a %s operation: its configuration profile is outside the "
            "caller's allowedConfigVersions.",
            what,
        )
        raise PermissionError(REFUSAL)


def refuse_if_scoped(scope: Optional[Iterable[str]], *, operation: str) -> None:
    """Refuse an operation that cannot honour a scope to any caller who has one.

    For reads that return rows across every profile with nothing to filter them
    on. ``None`` (unrestricted: an Admin, or an unscoped user in ``Off`` mode) is
    served; under strict isolation every non-admin has a scope, so such an
    operation becomes Admin-only.
    """
    if scope is None:
        return
    logger.warning(
        "Denied %s: the caller has a configuration scope, and this operation "
        "returns rows across every profile.",
        operation,
    )
    raise PermissionError(
        f"Unauthorized: {operation} is not available to a caller with a "
        f"configuration scope."
    )


def assert_document_in_scope(
    object_key: str,
    scope: Optional[Iterable[str]],
    *,
    tracking_table: Any = None,
    config_version_of: Optional[Callable[[str], Any]] = None,
) -> None:
    """Refuse unless ``scope`` allows the document tracked under ``object_key``.

    A no-op, with no read, for an unrestricted caller (``scope is None``).
    ``tracking_table`` may be a table or a zero-argument callable returning one, so
    a caller whose table name comes from the environment does not need it set for
    an unrestricted request. Otherwise the profile comes from
    ``config_version_of(key)`` when given (it must return
    :data:`ABSENT`, ``None`` or a name, as :func:`tracked_config_version` does)
    or from a ``GetItem`` on ``tracking_table``. A read failure propagates: the
    caller is a resolver whose error handling already turns it into a 5xx, which is
    a refusal, and swallowing it here could only ever be an allow.
    """
    if scope is None:
        return
    if config_version_of is None:
        if tracking_table is None:
            raise RuntimeError(
                "assert_document_in_scope needs a tracking table or a lookup for a "
                "scoped caller"
            )
        table = tracking_table() if callable(tracking_table) else tracking_table
        version = tracked_config_version(table, object_key)
    else:
        version = config_version_of(object_key)
    if is_absent(version):
        logger.warning(
            "Denied a document operation: no tracked document has that key, so its "
            "configuration profile cannot be determined."
        )
        raise PermissionError(REFUSAL)
    assert_profile_in_scope(scope, version)


def require_document_scope(
    event: Optional[Mapping[str, Any]],
    object_keys: Iterable[str],
    *,
    users_table_name: str,
    dynamodb: Any,
    tracking_table: Any = None,
    config_version_of: Optional[Callable[[str], Any]] = None,
    cache: Optional[MutableMapping[str, Any]] = None,
    cache_ttl: float = SCOPE_CACHE_TTL_SECONDS,
) -> Optional[List[str]]:
    """Resolve the caller's scope and check every key against it, in one call.

    Returns the scope (``None`` for unrestricted) so a caller that also filters a
    listing can reuse it. Raises ``PermissionError`` on the first refused key; a
    batch with one out-of-scope key is refused whole, so it cannot be used to probe
    which keys are in scope one at a time.
    """
    claims = caller_claims(event)
    scope = resolve_caller_scope(
        claims,
        users_table_name=users_table_name,
        dynamodb=dynamodb,
        cache=cache,
        cache_ttl=cache_ttl,
    )
    if scope is None:
        return None
    if tracking_table is None and config_version_of is None:
        raise RuntimeError("require_document_scope needs a tracking table")
    for key in object_keys:
        assert_document_in_scope(
            key,
            scope,
            tracking_table=tracking_table,
            config_version_of=config_version_of,
        )
    return scope
