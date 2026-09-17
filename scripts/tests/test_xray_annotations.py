# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Assert X-Ray annotation values are scalars, not collection literals.

Four pipeline Lambdas wrote `xray_recorder.put_annotation("document_id",
{document.id})`. The braces make a one-element *set*, not the string. X-Ray
annotations must be a string, number or boolean, so the value was dropped or
stringified as `{'abc-123'}` — meaning trace filtering by document id did not
work on exactly the four functions that do the expensive work (OCR,
classification, extraction, assessment). The same call was already correct in
the two rule-validation functions, which is how it reads as copy-paste drift
rather than intent.

Nothing could have caught this on the way in:

* The four files live under `patterns/`, which `ruff.toml` excludes wholesale,
  so no ruff rule runs on them at all.
* Even with ruff enabled there, `B018` (useless-expression) does not fire. B018
  flags a useless expression *statement*; here the set literal is an argument,
  which is a legitimate position for a set. Verified empirically against these
  files before this test was written.
* basedpyright cannot see it either: `patterns/*/src` is in pyrightconfig's
  `exclude`, and `reportArgumentType` is "none" repo-wide.

So the guard has to be a static assertion of its own, which is this file. It
parses the AST rather than grepping, so it also catches the list and dict
spellings and is not confused by formatting or line breaks.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Where X-Ray instrumentation lives. Kept broad on purpose: the defect spread by
# copy-paste, so the check must cover anywhere a handler could be added.
SEARCH_ROOTS = ("patterns", "src/lambda", "nested", "feature-platform", "options")

SKIP_DIR_NAMES = {
    "__pycache__",
    "node_modules",
    ".aws-sam",
    "build",
    "dist",
    ".venv",
    "vendor",
}

# put_annotation(key, value) — value must be a scalar.
# put_metadata(key, value) legitimately accepts dicts, so it is not checked.
CHECKED_METHOD = "put_annotation"

BAD_VALUE_NODES = (ast.Set, ast.SetComp, ast.List, ast.ListComp, ast.Dict, ast.Tuple)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in SEARCH_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if any(
                part in SKIP_DIR_NAMES for part in path.relative_to(REPO_ROOT).parts
            ):
                continue
            files.append(path)
    return sorted(files)


def _annotation_calls(path: Path) -> list[ast.Call]:
    """Every `<something>.put_annotation(...)` call in the file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not our concern
        return []
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == CHECKED_METHOD
    ]


pytestmark = pytest.mark.unit


def test_the_scan_finds_the_instrumentation() -> None:
    """Guard against this file passing because it searched the wrong place.

    If the roots list or the AST match ever stops finding put_annotation calls,
    every other assertion here becomes vacuous and silent.
    """
    total = sum(len(_annotation_calls(p)) for p in _python_files())
    assert total >= 10, (
        f"expected to find the X-Ray put_annotation calls, found {total}. "
        "SEARCH_ROOTS or the AST matcher is probably stale — fix the scan before "
        "trusting this file."
    )


def test_no_put_annotation_value_is_a_collection_literal() -> None:
    findings: list[str] = []
    for path in _python_files():
        for call in _annotation_calls(path):
            if len(call.args) < 2:
                continue
            value = call.args[1]
            if isinstance(value, BAD_VALUE_NODES):
                rel = path.relative_to(REPO_ROOT)
                findings.append(
                    f"{rel}:{value.lineno} passes "
                    f"{type(value).__name__} as the annotation value"
                )

    assert not findings, (
        "X-Ray annotation values must be a string, number or boolean. These calls "
        "pass a collection literal, which X-Ray drops or stringifies, breaking "
        "trace filtering:\n  " + "\n  ".join(findings) + "\n"
        "This is almost always a stray pair of braces: "
        'put_annotation("document_id", {document.id}) should be '
        'put_annotation("document_id", document.id).'
    )
