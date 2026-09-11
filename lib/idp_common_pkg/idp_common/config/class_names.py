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

import re
from typing import Final

# The character set every consumer of a class id accepts. Kept in sync with
# BDA's blueprintName constraint and the UI's CLASS_NAME_PATTERN.
CLASS_NAME_PATTERN: Final = re.compile(r"^[a-zA-Z0-9_-]+$")

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
