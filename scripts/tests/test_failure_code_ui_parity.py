# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every backend "the stage RAISED" code must be one the UI renders as Failed.

``ProcessingIssue`` carries two kinds of error-severity code and the difference is
the one an operator acts on first: a code that FLAGS a result the pipeline still
accepted (``extraction_rows_below_ocr_estimate``, ``assessment_incomplete``) versus
one that says the stage **raised** and there is no result to flag
(``extraction_failed`` and the three added by ``idp_common.document_failure``).
Severity cannot tell them apart — both are ``error`` — so the Sections panel
decides by code, against a hand-written literal set in
``src/ui/src/components/common/processing-issues-utils.ts``.

That set is in a different language from the Python that produces the codes, so
nothing about adding a code on the backend would make it appear there. The failure
mode is quiet in the worst way: the issue still renders, the tooltip still shows
the text, and the status column reads the milder **Incomplete** for a section that
actually failed. No test on either side would notice, because each is internally
consistent.

So this test derives both sets from source and fails in **both** directions — a
backend code missing from the UI, and a UI entry no backend code produces (which
would be a dead literal pre-approving whatever next takes that name).

Read from **source** rather than imported, on the Python side too: that keeps the
gate runnable without the package installed, and it means the assertion is about
what is committed rather than about whatever an editable install happens to
resolve to.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The UI's decision table.
UI_MODULE = REPO_ROOT / "src/ui/src/components/common/processing-issues-utils.ts"

#: Backend declarations of codes that mean "the stage raised". Each entry is
#: (path, name of the module-level constant holding the code or the set of them).
#: ``document_failure`` declares a set; ``extraction.failure`` declares one code,
#: and it stays where it is because that module owns extraction's vocabulary.
BACKEND_SOURCES = (
    (
        Path("lib/idp_common_pkg/idp_common/document_failure.py"),
        "FAILURE_CODES",
    ),
    (
        Path("lib/idp_common_pkg/idp_common/extraction/failure.py"),
        "EXTRACTION_FAILED_CODE",
    ),
)


def _module_assignments(tree: ast.Module) -> dict[str, ast.expr]:
    """Module-level ``NAME = <expr>`` bindings, in source order."""
    bindings: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if node.value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings[target.id] = node.value
    return bindings


def _resolve_codes(expr: ast.expr, bindings: dict[str, ast.expr]) -> set[str]:
    """Every code string reachable from ``expr``, following module-level names.

    ``FAILURE_CODES`` is a ``frozenset`` of *name references*, not of literals, so
    a reader that collected only ``ast.Constant`` nodes comes back empty — and
    empty passes a "nothing is missing" assertion. That is what
    ``test_both_sides_are_non_empty`` is here to catch.
    """
    codes: set[str] = set()
    for child in ast.walk(expr):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            codes.add(child.value)
        elif isinstance(child, ast.Name) and child.id in bindings:
            referenced = bindings[child.id]
            if isinstance(referenced, ast.Constant) and isinstance(
                referenced.value, str
            ):
                codes.add(referenced.value)
    return codes


def _backend_codes() -> set[str]:
    codes: set[str] = set()
    for relative, constant in BACKEND_SOURCES:
        path = REPO_ROOT / relative
        assert path.is_file(), f"{relative} has moved; update BACKEND_SOURCES"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bindings = _module_assignments(tree)
        assert constant in bindings, (
            f"{relative} no longer declares a module-level `{constant}`. If the "
            "failure-code vocabulary moved, point BACKEND_SOURCES at its new home "
            "rather than deleting this gate."
        )
        codes |= _resolve_codes(bindings[constant], bindings)
    return codes


def _ui_codes() -> set[str]:
    source = UI_MODULE.read_text(encoding="utf-8")
    match = re.search(
        r"const\s+FAILURE_CODES\s*=\s*new\s+Set\(\s*(\[[^\]]*\])\s*\)",
        source,
        re.DOTALL,
    )
    assert match, (
        f"{UI_MODULE.relative_to(REPO_ROOT)} no longer declares "
        "`const FAILURE_CODES = new Set([...])`. That set is what renders a raised "
        "stage as Failed rather than Incomplete; if it was restructured, update "
        "this gate to read the new shape rather than removing it."
    )
    # A JS array of single-quoted strings with a trailing comma is not JSON.
    entries = re.findall(r"'([^']+)'|\"([^\"]+)\"", match.group(1))
    return {single or double for single, double in entries}


@pytest.mark.unit
def test_every_backend_failure_code_is_rendered_as_failed():
    missing = sorted(_backend_codes() - _ui_codes())
    assert missing == [], (
        "these codes mean the stage raised, but the UI's FAILURE_CODES set does "
        "not list them, so the Sections panel will show them as the milder "
        "'Incomplete': " + json.dumps(missing) + f"\nAdd them to "
        f"{UI_MODULE.relative_to(REPO_ROOT)}."
    )


@pytest.mark.unit
def test_the_ui_lists_no_failure_code_the_backend_does_not_produce():
    """A dead entry pre-approves whatever next occupies the name."""
    unknown = sorted(_ui_codes() - _backend_codes())
    assert unknown == [], (
        "the UI's FAILURE_CODES set lists codes no backend module declares: "
        + json.dumps(unknown)
        + "\nEither the code was renamed on the backend, or the entry is dead and "
        "should be removed."
    )


@pytest.mark.unit
def test_both_sides_are_non_empty():
    """Non-vacuity: a regex that silently matched nothing would pass both above."""
    assert len(_backend_codes()) >= 4
    assert len(_ui_codes()) >= 4
