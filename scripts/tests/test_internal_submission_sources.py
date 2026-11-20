# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The internal-producer set is exactly the values this repository writes (#597).

Only a ``submission-source`` value in
``idp_common.config.prefix_mappings.INTERNAL_SUBMISSION_SOURCES`` exempts an object
from prefix mappings. That set is a literal in one module, and the values it has to
match are literals in the producers that stamp them, in other deploy artifacts. So
nothing ties the two together except this test, which derives the written values
from the tree:

* a producer added without an entry in the set would have its documents mapped
  (and refused by a ``reject`` mapping) -- the defect the PII anonymizer's
  ``redactcopy_and_stop`` mode turns into a lost document;
* an entry with no producer would be a value any S3 writer could set to skip every
  mapping, which is the route #597 closed.

The writers are found by reading source, not by importing it: every ``.py`` file in
the tree outside the test suites, for a ``"submission-source"`` key with a literal
value in a dict, a subscript assignment, or a ``submission_source=`` keyword.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from repo_files import tracked_paths

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
)

METADATA_KEY = "submission-source"
KEYWORD = "submission_source"


def _is_test_file(path: Path) -> bool:
    parts = path.relative_to(REPO_ROOT).parts
    return (
        "tests" in parts or path.name.startswith("test_") or path.name == "conftest.py"
    )


def _literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
        return node.value
    return None


def written_values(source: str) -> set[str]:
    """Every literal ``submission-source`` value ``source`` writes."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if key is not None and _literal(key) == METADATA_KEY:
                    if (literal := _literal(value)) is not None:
                        found.add(literal)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and _literal(target.slice) == METADATA_KEY
                    and (literal := _literal(node.value)) is not None
                ):
                    found.add(literal)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == KEYWORD and (literal := _literal(keyword.value)):
                    found.add(literal)
    return found


def _tree_values() -> dict[str, set[Path]]:
    by_value: dict[str, set[Path]] = {}
    for path in tracked_paths(REPO_ROOT, "*.py"):
        if _is_test_file(path):
            continue
        try:
            values = written_values(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for value in values:
            by_value.setdefault(value, set()).add(path.relative_to(REPO_ROOT))
    return by_value


def _declared() -> frozenset[str]:
    sys.path.insert(0, str(REPO_ROOT / "lib" / "idp_common_pkg"))
    from idp_common.config.prefix_mappings import INTERNAL_SUBMISSION_SOURCES

    return INTERNAL_SUBMISSION_SOURCES


@pytest.mark.unit
def test_the_declared_set_is_exactly_what_the_producers_write():
    written = _tree_values()
    assert written, "found no submission-source writer at all; the scan is broken"
    assert set(written) == set(_declared()), (
        f"producers write {sorted(written)} (at {written}) but "
        f"INTERNAL_SUBMISSION_SOURCES is {sorted(_declared())}"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "source,expected",
    [
        ('m = {"submission-source": "a"}', {"a"}),
        ('m["submission-source"] = "b"', {"b"}),
        ('copy(x, submission_source="c")', {"c"}),
        ('m = {"submission-source": value}', set()),
        ('m = {"config-version": "d"}', set()),
    ],
)
def test_the_scan_recognises_each_writing_shape(source, expected):
    """Each shape the detector claims to read, read by the real detector."""
    assert written_values(source) == expected
