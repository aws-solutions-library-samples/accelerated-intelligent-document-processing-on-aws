# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every input to ``build_separately_from_main_stack`` is derived from the publisher.

``gate_premises.build_input_paths()`` answers "does one publish run build this" by
unioning the publisher's component-dependency map with its bundled-feature list. This
file asserts that those two are *sufficient*: that no
``build_and_package_template(..., force_rebuild=True)`` call site names a directory
neither covers.

**Why it exists.** There was briefly a third source — a hand-written
``EXPLICIT_BUILD_DIRS`` tuple mirroring the one call site that passes a literal
directory, carrying a comment claiming "the test below pins it against the source so a
new call site cannot be added without this being updated". No such test existed.
Emptying the tuple to ``()`` left the whole suite green.

That is this repository's recurring defect appearing in the code written to fix it: a
control asserted to exist, cited as the reason a hand-maintained list is safe, and never
written. It was harmless only by accident — the tuple's single member is also a nested
stack of the parent, so the sibling predicate returns ``False`` for it regardless.

Writing the missing test produced a better answer than pinning: the tuple was
**redundant**, because its member is already a key in the component map. So it is gone,
and what remains is the property it was standing in for, checked against the publisher's
source rather than mirrored from it. A genuinely new call site fails here.
"""

from __future__ import annotations

import ast

import gate_premises
import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = gate_premises.REPO_ROOT
PUBLISH = REPO_ROOT / gate_premises._PUBLISH_PACKAGE / "idp_sdk/_core/publish.py"

#: The publisher method whose ``force_rebuild=True`` calls must all be covered.
_BUILD_CALL = "build_and_package_template"


def _string_locals(func: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, str]:
    """``{name: value}`` for simple ``name = "literal"`` assignments in one function."""
    found: dict[str, str] = {}
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                found[target.id] = node.value.value
    return found


def _forced_rebuild_arguments() -> list[tuple[str, object]]:
    """``(enclosing function, resolved argument)`` per forced-rebuild build call.

    The resolved argument is the literal directory where the call site passes one (a
    string constant, or a local bound to one), and ``None`` where it passes a computed
    value — the bundled-feature loop passes ``str(feature_dir)``, which is derived from
    ``extensions-oss.yaml`` and is covered by :func:`gate_premises.bundled_feature_dirs`.
    """
    tree = ast.parse(PUBLISH.read_text(encoding="utf-8"), filename=str(PUBLISH))
    results: list[tuple[str, object]] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        literals = _string_locals(func)
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name != _BUILD_CALL:
                continue
            forced = any(
                kw.arg == "force_rebuild"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in node.keywords
            )
            if not forced or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                results.append((func.name, first.value))
            elif isinstance(first, ast.Name) and first.id in literals:
                results.append((func.name, literals[first.id]))
            else:
                results.append((func.name, None))
    return results


def test_the_publisher_still_has_forced_rebuild_call_sites() -> None:
    """Guard the discovery — a silent zero would make the pin below vacuous."""
    found = _forced_rebuild_arguments()
    assert len(found) >= 2, (
        f"expected at least 2 {_BUILD_CALL}(force_rebuild=True) call sites in "
        f"{PUBLISH.relative_to(REPO_ROOT)}, found {found}. If the publisher changed, "
        "update _forced_rebuild_arguments() — a parser that silently matches nothing "
        "would make the check below vacuous, which is the shape of the defect this file "
        "exists for."
    )


def test_every_forced_rebuild_directory_is_a_known_build_input() -> None:
    """The claim the deleted constant used to make, now made true.

    A ``force_rebuild=True`` call passing a literal directory must be covered by one of
    the two sources ``build_input_paths()`` unions. A third call site therefore fails
    here, which is what the constant's comment promised and nothing delivered.

    The remedy when it fails is to DERIVE the new directory, not to reintroduce a literal
    list. If a literal is genuinely unavoidable, this file is where its pin goes — a
    comment promising one is what was wrong the first time.
    """
    covered = gate_premises.build_input_paths()
    unaccounted = [
        f"{func}() builds {directory!r}"
        for func, directory in _forced_rebuild_arguments()
        if isinstance(directory, str)
        and not any(
            directory == known or directory.startswith(known.rstrip("/") + "/")
            for known in covered
        )
    ]
    assert not unaccounted, (
        "these publisher call sites force a rebuild of a directory that "
        f"gate_premises.build_input_paths() does not cover: {unaccounted}. "
        "built_separately_from_main_stack() would call that directory independently "
        "built, which is how five bundled features were wrongly certified as "
        "independent. Derive it from whatever the publisher reads, or — only if that is "
        "impossible — add a literal list here together with its pin."
    )
