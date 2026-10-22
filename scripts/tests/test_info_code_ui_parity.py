# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Sections panel's ``info`` label must not claim a recovery that did not happen.

Four backend ``ProcessingIssue`` codes are ``info`` severity and only one reports a
repair. The other three report something **missing** — ``extraction_sparse``
("possible silent extraction loss"), ``extraction_off_schema_fields`` (fields
dropped before assessment) and ``assessment_geometry_incomplete`` (scored fields
with no bounding box) — so rendering them all as "Auto-recovered" told an operator
the opposite of what happened, on the notes most worth reading
([#1521](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1521)).

The UI now enumerates what IS a recovery, so a new backend ``info`` code defaults
to the neutral label rather than silently claiming a repair. That default is safe
but silent, which is what this test is for: it derives the backend's ``info``
universe and fails when a code belongs to neither classification, so adding one
forces the author to decide which label it should carry.

Same shape and same reason as ``test_failure_code_ui_parity.py``: the UI set is a
literal in a different language from the backend that produces the codes, so
nothing about adding a code there would make it appear here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UI_FILE = REPO / "src/ui/src/components/common/processing-issues-utils.ts"
BACKEND_ROOT = REPO / "lib/idp_common_pkg/idp_common"

#: Backend ``info`` codes that report something MISSING rather than repaired, and
#: so must not be labelled as a recovery. One entry per code, each naming what it
#: actually reports — the reason the label would be wrong is a property of the
#: individual code, not of the group.
ADVISORY_INFO_CODES = {
    "extraction_sparse": "few schema fields populated — possible silent extraction loss",
    "extraction_off_schema_fields": "fields outside the schema, dropped before assessment",
    "assessment_geometry_incomplete": "scored fields carrying no bounding box",
}


def _backend_info_codes() -> dict[str, Path]:
    """Every ``code=`` passed to a ``ProcessingIssue`` with ``severity="info"``.

    Read from source via AST rather than by importing, because the constructors sit
    inside functions whose modules pull in heavy optional dependencies.
    """
    found: dict[str, Path] = {}
    for path in BACKEND_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "ProcessingIssue":
                continue
            kw = {
                k.arg: k.value
                for k in node.keywords
                if k.arg and isinstance(k.value, ast.Constant)
            }
            sev, code = kw.get("severity"), kw.get("code")
            if sev is None or code is None:
                continue
            if sev.value == "info" and isinstance(code.value, str):
                found.setdefault(code.value, path)
    return found


def _ui_recovery_codes() -> set[str]:
    source = UI_FILE.read_text(encoding="utf-8")
    match = re.search(
        r"const\s+RECOVERY_CODES\s*=\s*new\s+Set\(\s*(\[[^\]]*\])\s*\)", source
    )
    assert match, (
        f"{UI_FILE.relative_to(REPO)} no longer declares "
        "`const RECOVERY_CODES = new Set([...])`. That set is what decides whether "
        "a section's info note renders as 'Auto-recovered' or the neutral 'Info', "
        "so this parity check cannot run without it."
    )
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_every_backend_info_code_is_classified() -> None:
    """A new ``info`` code must be declared a recovery or an advisory, not left to
    the default. The default is correct more often than not, which is exactly why
    nothing would notice it being wrong."""
    backend = _backend_info_codes()
    classified = _ui_recovery_codes() | set(ADVISORY_INFO_CODES)
    unclassified = {
        c: str(p.relative_to(REPO)) for c, p in backend.items() if c not in classified
    }
    assert not unclassified, (
        "these backend info-severity codes are classified neither as a recovery "
        "(RECOVERY_CODES in the UI) nor as an advisory (ADVISORY_INFO_CODES here), "
        f"so the Sections panel labels them by default: {unclassified}. Decide "
        "which the code is: does it report something repaired, or something missing?"
    )


def test_the_ui_claims_no_recovery_the_backend_does_not_produce() -> None:
    backend = set(_backend_info_codes())
    stale = _ui_recovery_codes() - backend
    assert not stale, (
        "the UI's RECOVERY_CODES lists codes no backend module emits at info "
        f"severity: {sorted(stale)}. A dead entry pre-approves whatever next takes "
        "that name as a recovery."
    )


def test_no_advisory_code_is_also_listed_as_a_recovery() -> None:
    overlap = _ui_recovery_codes() & set(ADVISORY_INFO_CODES)
    assert not overlap, (
        f"these codes are listed as both a recovery and an advisory: {sorted(overlap)}"
    )


def test_every_advisory_entry_still_exists_in_the_backend() -> None:
    """Staleness ratchet: an advisory entry for a code nobody emits is dead, and a
    dead entry silently pre-classifies whatever next takes that name."""
    backend = set(_backend_info_codes())
    gone = set(ADVISORY_INFO_CODES) - backend
    assert not gone, (
        f"ADVISORY_INFO_CODES names codes the backend no longer emits at info "
        f"severity: {sorted(gone)}. Delete them."
    )


@pytest.mark.parametrize("code", sorted(ADVISORY_INFO_CODES))
def test_each_advisory_code_reports_an_absence_not_a_repair(code: str) -> None:
    """Non-vacuity: each entry must be doing work. A code whose own message talks
    about recovery would belong in RECOVERY_CODES instead, and this is what keeps
    the two lists from drifting into each other."""
    assert code not in _ui_recovery_codes()
    assert ADVISORY_INFO_CODES[code].strip(), f"{code} carries no stated reason"


def test_both_sides_are_non_empty() -> None:
    assert _backend_info_codes(), "found no backend info-severity ProcessingIssue codes"
    assert _ui_recovery_codes(), "the UI lists no recovery codes"
