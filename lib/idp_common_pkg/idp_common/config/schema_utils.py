# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared JSON-Schema traversal helpers for class schemas.

Class schemas routinely put groups and list-item shapes in ``$defs`` and
reference them (``{"$ref": "#/$defs/Signatures"}``) — that is what the UI's
schema editor emits for every group. Any consumer that reads ``type`` or
``description`` straight off such a property sees neither, so it silently
treats a group as an untyped leaf.

This module owns the single dereferencing helper those consumers share. It lives
under ``config`` because that is where ``schema_constants`` — the only thing it
depends on — already lives, and because every caller already imports from this
package, so it adds no dependency edge any of them did not already have. The
alternative, keeping it in ``assessment/service.py`` where it started, would
have forced ``classification`` to depend on the assessment service. (Note the
placement is about that dependency direction and about cohesion, NOT about
import weight: ``config/__init__.py`` imports boto3 at module level, so
importing this module pulls it in regardless.)

Not consolidated here: ``assessment/threshold_resolver.py`` has its own
``_deref``, whose dangling-``$ref``-returns-``{}`` and
definition-wins-over-sibling semantics are load-bearing for threshold
inheritance in ``resolve_threshold_for_path``. Switching it to
:func:`deref_schema` would change which threshold wins for a ``$ref`` property
that also carries a local ``x-aws-idp-confidence-threshold``, so it is left
alone.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def deref_schema(
    node: Any, root: Dict[str, Any], _seen: Optional[set] = None
) -> Dict[str, Any]:
    """
    Resolve a local JSON-Schema ``$ref`` against ``root``'s ``$defs``.

    Returns the referenced subschema with any sibling keys on the referencing
    node layered on top (a local ``description`` overrides the definition's),
    and follows ``$ref`` chains.

    A ``$ref`` that is not a resolvable local ``#/$defs/<name>`` reference — a
    remote ``$ref``, a dangling name, a cycle — leaves the node returned as-is,
    so unresolvable schemas degrade to the un-dereferenced behavior rather than
    raising. A node that is not a dict at all yields ``{}`` (there is nothing to
    return as-is), which lets callers ``.get()`` the result unconditionally.

    Args:
        node: The (possibly ``$ref``-bearing) subschema.
        root: The document-class schema that owns ``$defs``.
        _seen: Internal cycle guard.

    Returns:
        The dereferenced subschema dict (``{}`` for a non-dict node).
    """
    from idp_common.config.schema_constants import DEFS_FIELD, REF_FIELD

    if not isinstance(node, dict):
        return {}

    ref = node.get(REF_FIELD)
    if not isinstance(ref, str):
        return node

    prefix = f"#/{DEFS_FIELD}/"
    if not ref.startswith(prefix):
        logger.debug("Unsupported non-local $ref '%s'; using it as-is", ref)
        return node

    seen = _seen or set()
    if ref in seen:
        logger.warning("Circular $ref '%s' in class schema; stopping resolution", ref)
        return node
    seen.add(ref)

    target = root.get(DEFS_FIELD, {}).get(ref[len(prefix) :])
    if not isinstance(target, dict):
        logger.warning("Dangling $ref '%s' in class schema; using it as-is", ref)
        return node

    # Sibling keys on the referencing node win over the definition's.
    merged = {**target, **{k: v for k, v in node.items() if k != REF_FIELD}}
    return deref_schema(merged, root, seen) if REF_FIELD in target else merged
