# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""HITL and review status values are spelled in exactly one place (FR-14).

``idp_common/review_queues/statuses.py`` declares them (vendored verbatim into
the two layer-free document-list resolvers as ``review_statuses.py``). Every
writer and reader elsewhere goes through those enums, so a new spelling can't
creep in. The scan is over source ASTs, so comments and docstrings are free to
name the values.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[5]

SCAN_ROOTS = [
    REPO_ROOT / "src" / "lambda",
    REPO_ROOT / "patterns" / "unified" / "src",
    REPO_ROOT / "lib" / "idp_common_pkg" / "idp_common",
    *sorted((REPO_ROOT / "nested").glob("*/src/lambda")),
]

CANONICAL = REPO_ROOT / "lib/idp_common_pkg/idp_common/review_queues/statuses.py"
VENDORED_NAME = "review_statuses.py"

#: Spellings that mean a HITL status wherever they appear.
DISTINCTIVE_HITL_LITERALS = frozenset(
    {
        "PendingReview",
        "Review Pending",
        "ReviewInProgress",
        "Review Completed",
        "Review Skipped",
        "in_review",
    }
)

#: Spellings that mean a HITL status only next to ``hitl_status`` / ``HITLStatus``.
CONTEXTUAL_HITL_LITERALS = frozenset({"InProgress", "Completed", "Skipped"})
CONTEXT_MARKERS = ("hitl_status", "HITLStatus")


def _python_files():
    # Tracked files only, so build output (.aws-sam, build, node_modules) and
    # caches are left out by git rather than by a list of directory names.
    tracked = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--",
            *(str(r.relative_to(REPO_ROOT)) for r in SCAN_ROOTS),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    for rel in tracked:
        if not rel.endswith(".py"):
            continue
        path = REPO_ROOT / rel
        if path.name.startswith("test_") or path.name == VENDORED_NAME:
            continue
        if path == CANONICAL:
            continue
        yield path


def _contextual_nodes(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Compare)):
            yield node
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "eq"
        ):
            yield node


def find_literals(source: str) -> list[tuple[int, str]]:
    """Return ``(line, literal)`` for every banned literal in ``source``."""
    tree = ast.parse(source)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in DISTINCTIVE_HITL_LITERALS
        ):
            hits.append((node.lineno, node.value))
    for node in _contextual_nodes(tree):
        text = ast.unparse(node)
        if not any(marker in text for marker in CONTEXT_MARKERS):
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Constant)
                and isinstance(sub.value, str)
                and sub.value in CONTEXTUAL_HITL_LITERALS
            ):
                hits.append((sub.lineno, sub.value))
    return sorted(set(hits))


@pytest.mark.unit
class TestStatusLiteralsDeclaredOnce:
    def test_scan_roots_exist(self):
        assert all(root.is_dir() for root in SCAN_ROOTS)
        assert len(list(_python_files())) > 100

    def test_no_literal_outside_the_declaration(self):
        offenders = []
        for path in _python_files():
            try:
                source = path.read_text()
                hits = find_literals(source)
            except (SyntaxError, UnicodeDecodeError):
                continue
            offenders += [
                f"{path.relative_to(REPO_ROOT)}:{line}: {value!r}"
                for line, value in hits
            ]
        assert offenders == [], (
            "HITL status literals outside idp_common/review_queues/statuses.py; use "
            "ReviewStatus / LegacyHitlStatus / normalize_hitl_status instead:\n  "
            + "\n  ".join(offenders)
        )

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ('x = "PendingReview"', [(1, "PendingReview")]),
            ('doc.hitl_status = "Completed"', [(1, "Completed")]),
            ('if item["HITLStatus"] == "Skipped": pass', [(1, "Skipped")]),
            ('Attr("HITLStatus").eq("InProgress")', [(1, "InProgress")]),
            ('status = "Completed"', []),
            ('# "PendingReview" in a comment', []),
        ],
    )
    def test_detector(self, source, expected):
        assert find_literals(source) == expected
