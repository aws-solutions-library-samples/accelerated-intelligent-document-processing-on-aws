# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The `Status` listing in `idp_common/README.md` names every enum member.

The README reproduces the enum as a code block, and that block had fallen behind
the enum by four members (`PENDING_UPLOAD`, `IN_PROGRESS`,
`RULE_VALIDATION_POLICY_CLASSIFICATION`, `REJECTED`). A reader looking up which
statuses a document can carry had no way to tell the list was short, so the block
is compared with `idp_common.models.Status` member by member, in order.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from idp_common.models import Status

pytestmark = pytest.mark.unit

_README = pathlib.Path(__file__).resolve().parents[2] / "idp_common" / "README.md"
_MEMBER_LINE = re.compile(r'^\s+([A-Z][A-Z0-9_]*) = "([^"]*)"', re.MULTILINE)


def _documented_members() -> list[tuple[str, str]]:
    text = _README.read_text(encoding="utf-8")
    start = text.index("class Status(Enum):")
    end = text.index("```", start)
    return _MEMBER_LINE.findall(text[start:end])


def test_readme_status_listing_matches_the_enum():
    documented = _documented_members()
    # An empty parse would compare equal to nothing useful; the enum is never empty.
    assert documented, "no Status members parsed from the README code block"
    assert documented == [(member.name, member.value) for member in Status]
