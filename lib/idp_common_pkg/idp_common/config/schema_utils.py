# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared JSON-Schema traversal helpers for class schemas.

Class schemas routinely put groups and list-item shapes in ``$defs`` and
reference them (``{"$ref": "#/$defs/Signatures"}``) — that is what the UI's
schema editor emits for every group. Any consumer that reads ``type`` or
``description`` straight off such a property sees neither, so it silently
treats a group as an untyped leaf.

This module owns the single dereferencing helper those consumers share. It
deliberately lives under ``config`` (next to ``schema_constants``) rather than
under ``assessment``: the classification service and the assessment batcher
both need it, and neither can import the assessment service without dragging
in Bedrock/S3 clients it has no use for.

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

    Anything that is not a resolvable local ``#/$defs/<name>`` reference — a
    remote ``$ref``, a dangling name, a non-dict node — is returned as-is, so
    unresolvable schemas degrade to the un-dereferenced behavior rather than
    raising.

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
        logger.debug(f"Unsupported non-local $ref '{ref}'; using it as-is")
        return node

    seen = _seen or set()
    if ref in seen:
        logger.warning(f"Circular $ref '{ref}' in class schema; stopping resolution")
        return node
    seen.add(ref)

    target = root.get(DEFS_FIELD, {}).get(ref[len(prefix) :])
    if not isinstance(target, dict):
        logger.warning(f"Dangling $ref '{ref}' in class schema; using it as-is")
        return node

    # Sibling keys on the referencing node win over the definition's.
    merged = {**target, **{k: v for k, v in node.items() if k != REF_FIELD}}
    return deref_schema(merged, root, seen) if REF_FIELD in target else merged
