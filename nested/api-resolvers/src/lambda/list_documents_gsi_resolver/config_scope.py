# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Server-side matching of per-user configuration-profile scope
(``allowedConfigVersions``).

A non-admin user may optionally be restricted to a set of Configuration Profiles.
That scope decides two things: which profiles they can read or edit, and which
*documents* they can see (a document is stamped with the profile it was processed
under). Both are security boundaries.

Lives in ``idp_common`` because the same rule is enforced in half a dozen
separate deploy artifacts — the configuration resolver, both document-list
resolvers, the reprocess resolver, the BDA sync resolver, and the document chat
processor. A scope check that drifts between them is a privilege-escalation bug,
which is exactly what happened before this module existed: the document-list
resolvers admitted any document whose ``ConfigVersion`` was absent, while the
configuration resolver denied an absent version.

Two rules, both deliberate:

- **An empty or unset scope means unrestricted.** This is the default for most
  users and must stay that way; scoping is opt-in per user.
- **A set scope fails closed.** A document or profile with no name to match
  against is denied, not admitted. An unnamed object cannot be proven in scope,
  and "cannot prove" must not mean "allow" on a security boundary.

Entries may be exact names (``lending``) or glob patterns (``lending-*``,
``uc?-prod``). Patterns exist because deployments predating revision history
encode lineage in the *name* (``usecaseA_v1``, ``usecaseA_v2``, …), so scoping a
user to a use case otherwise means re-granting on every iteration. Only an admin
can set a scope entry and only an admin can create a profile, so a pattern
cannot be used to widen one's own access.
"""

from __future__ import annotations

import logging
from fnmatch import fnmatchcase
from typing import Any, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Characters that make a scope entry a glob rather than a literal name.
_GLOB_CHARS = ("*", "?", "[")


def normalize_scope(raw: Any) -> Optional[List[str]]:
    """
    Coerce a raw ``allowedConfigVersions`` attribute into a scope list.

    Returns None for "unrestricted" (absent, empty, or not a usable sequence) so
    callers can use a single ``if scope:`` test, and drops blank entries — a
    stray empty string must not become a rule that matches nothing.
    """
    if not raw:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        logger.warning(
            f"Ignoring unusable allowedConfigVersions of type {type(raw).__name__}"
        )
        return None
    entries = [str(entry).strip() for entry in raw if str(entry).strip()]
    return entries or None


def is_pattern(entry: str) -> bool:
    """True when a scope entry is a glob rather than a literal profile name."""
    return any(char in entry for char in _GLOB_CHARS)


def scope_allows(scope: Optional[Sequence[str]], profile_name: Optional[str]) -> bool:
    """
    Whether a scope permits a Configuration Profile (or a document stamped with
    one).

    Args:
        scope: The caller's ``allowedConfigVersions``, or None/empty for
            unrestricted.
        profile_name: The profile name to test. An empty or missing name is
            **denied** whenever a scope is set — see the module docstring.
    """
    entries = normalize_scope(scope)
    if not entries:
        return True
    if not profile_name:
        return False
    name = str(profile_name)
    for entry in entries:
        if entry == name:
            return True
        if is_pattern(entry) and fnmatchcase(name, entry):
            return True
    return False


def filter_profiles(scope: Optional[Sequence[str]], names: Iterable[str]) -> List[str]:
    """Reduce an iterable of profile names to those the scope permits."""
    entries = normalize_scope(scope)
    if not entries:
        return list(names)
    return [name for name in names if scope_allows(entries, name)]
