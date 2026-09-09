# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One tolerant reading of a boolean-ish config flag.

A class-level ``x-aws-idp-*`` flag or a ``confidence.enabled`` value can reach the
library as a real bool, or as the string a YAML/DynamoDB/UI round-trip produced
(``"true"``, ``"False"``). Plain Python truthiness reads ``"false"`` as True, and the
web UI's Prompt Preview reads it as False — so the two surfaces disagreed on the
same stored config. Every flag consumer uses this instead. The accepted spellings
are Pydantic's (what ``IDPConfig`` itself coerces), and the UI's ``boolish`` mirrors
the same lists.
"""

from __future__ import annotations

from typing import Any

TRUE_STRINGS = frozenset({"true", "yes", "on", "1", "t", "y"})
FALSE_STRINGS = frozenset({"false", "no", "off", "0", "f", "n"})


def flag_is_true(value: Any, *, default: bool = False) -> bool:
    """``value`` read as a flag.

    ``None`` (absent) is ``default``. A bool is itself; a number is non-zero. A
    string is matched case-insensitively against Pydantic's true/false spellings,
    and an unrecognised string is ``default`` rather than "truthy" — a typo must
    not silently opt a class in.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in TRUE_STRINGS:
            return True
        if s in FALSE_STRINGS:
            return False
        return default
    return default
