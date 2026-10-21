# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared JSON-Schema traversal helpers for class schemas.

Class schemas routinely put groups and list-item shapes in ``$defs`` and
reference them (``{"$ref": "#/$defs/Signatures"}``) — that is what the UI's
schema editor emits for every group. Any consumer that reads ``type`` or
``description`` straight off such a property sees neither, so it silently
treats a group as an untyped leaf.

This module owns the single dereferencing helper those consumers share, and
:func:`find_class_schema`, the one lookup of a class's schema by document type.
They live under ``config`` because that is where ``schema_constants`` — the only
thing they depend on — already lives, and because every caller already imports
from this package, so they add no dependency edge any caller did not already have.
The lookup must not live under ``assessment``: importing anything from that package
runs its ``__init__``, which loads the assessment service and Pillow, and
review-queue routing runs in a Lambda that does not install the ``image`` extra. The
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

from .schema_constants import X_AWS_IDP_DOCUMENT_TYPE

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


def find_class_schema(doc_class: str, classes: Any) -> dict[str, Any] | None:
    """Look up a document class's JSON Schema by ``x-aws-idp-document-type``.

    Shared by every caller that needs a class schema from a ``classes`` list —
    the standalone assessment service, the BDA processresults Lambda and
    review-queue routing all route through here so the lookup (and its guards)
    cannot drift between them. It lives here rather than under ``assessment``
    because importing anything from that package runs its ``__init__``, which
    loads the assessment service and Pillow; the Pipeline ProcessResults Lambda
    does not install the ``image`` extra, so routing must not reach it.

    Args:
        doc_class: The document class name (e.g. ``"w2"``). Matched
            case-insensitively.
        classes: The config's ``classes`` list (each entry a JSON Schema dict).
            Tolerates ``None`` and non-dict entries.

    Returns:
        The matching class schema, or ``None`` when there is no match.

    Note:
        Entries whose ``x-aws-idp-document-type`` is not a string are skipped.
        Legacy→schema migration sets that key to the boolean ``True`` as a
        marker (see ``config/migration.py``), and calling ``.lower()`` on it
        would raise ``AttributeError`` — which on the BDA HITL path is not
        wrapped and would fail the whole segment.
    """
    if not doc_class:
        return None
    for schema in classes or []:
        if not isinstance(schema, dict):
            continue
        declared = schema.get(X_AWS_IDP_DOCUMENT_TYPE, "")
        if isinstance(declared, str) and declared.lower() == doc_class.lower():
            return schema
    return None
