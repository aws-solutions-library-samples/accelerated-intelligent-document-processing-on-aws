# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Canonical rules for document class identifiers.

A document class id (``$id`` / ``x-aws-idp-document-type``) is not just a
label: it is used to compose names in downstream AWS APIs. The strictest
consumer is Bedrock Data Automation, whose ``CreateBlueprint`` requires
``blueprintName`` to match ``[a-zA-Z0-9-_]+`` — and the accelerator composes
that name as ``{stack}-{class_id}-{suffix}``. The Web UI's schema builder
already enforces the same character set (``CLASS_NAME_PATTERN`` in
``SchemaBuilder.tsx``), but classes also arrive from Discovery, the CLI and
hand-authored config, which bypass it.

This module is the one place that defines the rule, so the write paths and the
name-composing paths cannot drift apart.
"""

import copy
import re
from typing import Any, Dict, Final

from idp_common.config.schema_constants import ID_FIELD

# The character set every consumer of a class id accepts. Kept in sync with
# BDA's blueprintName constraint and the UI's CLASS_NAME_PATTERN.
CLASS_NAME_PATTERN: Final = re.compile(r"^[a-zA-Z0-9_-]+$")

#: Longest class id discovery gives a class it named from a model's label. BDA's
#: ``blueprintName`` is at most 128 characters and is composed as
#: ``{stack}-{class_id}-{suffix}``, so a class id leaves room for the other parts.
MAX_DISCOVERED_CLASS_NAME_CHARS: Final = 64

_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
_HYPHEN_RUN = re.compile(r"-+")


def is_valid_class_name(name: str) -> bool:
    """Return True if ``name`` is usable as a class id everywhere downstream."""
    return bool(name) and bool(CLASS_NAME_PATTERN.match(name))


def sanitize_class_name(name: str) -> str:
    """Reduce ``name`` to the character set every consumer accepts.

    Disallowed characters become hyphens, runs of hyphens collapse, and
    leading/trailing hyphens are trimmed. Underscores are **preserved** —
    they are legal in every consumer, so a class id that already works keeps
    the exact same name and no existing resource derived from it is orphaned.

    Returns an empty string when nothing usable remains (e.g. ``"???"``);
    callers decide whether that is an error or a skip, since inventing a name
    would silently mislabel the class.
    """
    if not name:
        return ""
    sanitized = _INVALID_CHARS.sub("-", name)
    sanitized = _HYPHEN_RUN.sub("-", sanitized)
    return sanitized.strip("-")


# Keywords whose values are instance data rather than subschemas. A ``"$id"``
# key inside one of them is data and must not be rewritten.
_INSTANCE_DATA_KEYWORDS: Final = frozenset({"const", "default", "enum", "examples"})


def schema_id_as_uri_reference(value: str) -> str:
    """Return ``value`` percent-encoded so it is a valid RFC 3986 URI-reference.

    JSON Schema requires ``$id`` to be a URI-reference, but a class's ``$id`` is
    also its user-visible name and may contain spaces (``"Bank Statement"``).
    Every character outside the unreserved set and ``/`` is percent-encoded, so
    the result is always a valid relative reference, and a name made only of
    letters, digits, ``-``, ``_``, ``.``, ``~`` and ``/`` comes back unchanged.
    """
    from urllib.parse import quote

    return quote(value, safe="/")


def with_uri_safe_schema_ids(schema: Any) -> Any:
    """Return a deep copy of ``schema`` whose every ``$id`` is a URI-reference.

    For handing a class schema to a consumer that checks it against the JSON
    Schema metaschema (Stickler's model builder, ``check_schema``). Whether that
    check enforces ``format: uri-reference`` depends on whether an optional
    package is installed, so a ``$id`` such as ``"Bank Statement"`` is accepted
    in one environment and rejected in another. The authored schema is not
    modified: the class name stays what the author wrote everywhere it is shown
    or stored.
    """
    if isinstance(schema, dict):
        copied: Dict[str, Any] = {}
        for key, val in schema.items():
            if key == ID_FIELD and isinstance(val, str):
                copied[key] = schema_id_as_uri_reference(val)
            elif key in _INSTANCE_DATA_KEYWORDS:
                copied[key] = copy.deepcopy(val)
            else:
                copied[key] = with_uri_safe_schema_ids(val)
        return copied
    if isinstance(schema, list):
        return [with_uri_safe_schema_ids(item) for item in schema]
    # A JSON scalar is immutable, so it needs no copy.
    return schema
