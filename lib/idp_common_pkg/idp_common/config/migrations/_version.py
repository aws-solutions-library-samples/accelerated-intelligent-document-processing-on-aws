# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared ``config_format_version`` comparison for the migration chain.

Every migration asks the same two questions of a stored stamp: is it OLDER than
my target (so my transform is needed), and is it NEWER than my target (so I must
keep my hands off the stamp). Both need a numeric comparison — a string compare
gets ``"0.10" < "0.7"`` wrong, which is a bug waiting for the tenth format
version rather than a hypothetical.
"""

from __future__ import annotations

from typing import Any, Tuple


def parse_version(value: Any) -> Tuple[int, ...] | None:
    """Parse a ``config_format_version`` stamp into a comparable tuple.

    Returns ``None`` for an absent, blank or unparseable stamp — which callers
    treat as "predates versioning", the correct reading for a config written
    before the field existed.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return None


def is_newer_than(value: Any, target: str) -> bool:
    """True when ``value`` is a stamp strictly NEWER than ``target``.

    A migration must not rewrite the stamp in this case. It happens on
    **rollback**: an older release reads a configuration written by a newer one.
    Stamping it back down to this migration's target would erase the only record
    that the stored config came from a newer format — so a later roll-forward
    would skip the migration it does need. An unparseable or absent stamp is not
    newer (there is nothing to preserve).
    """
    parsed = parse_version(value)
    if parsed is None:
        return False
    return parsed > parse_version(target)  # type: ignore[operator]


def is_at_or_after(value: Any, target: str) -> bool:
    """True when ``value`` is a stamp at ``target`` or newer.

    Used as the "transform not needed" test, so a newer config is not put
    through an older release's reshaping. The legacy-marker check still runs
    independently — a hybrid dict stamped current but carrying a legacy-shaped
    sparse delta must still be relocated.
    """
    parsed = parse_version(value)
    if parsed is None:
        return False
    return parsed >= parse_version(target)  # type: ignore[operator]
