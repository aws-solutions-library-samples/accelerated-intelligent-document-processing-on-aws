# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One derivation of where a run starts, and one of the steps it does.

``idp_common.run_plan.restart.decide_restart`` derives the start step from what
changed, and ``idp_common.run_plan.planned`` lists the steps a run does. Every
surface uses those results. This module reads the source of the surfaces that
used to hold a derivation of their own and fails when one comes back.
"""

import ast
from pathlib import Path

import pytest

from idp_common.run_plan import steps

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[5]
_PROCESS_CHANGES = (
    _REPO / "nested/api-resolvers/src/lambda/process_changes_resolver/index.py"
)
_FINALIZE = _REPO / "lib/idp_common_pkg/idp_common/review_queues/finalize.py"
_CLI = _REPO / "lib/idp_cli_pkg/idp_cli/cli.py"


def _function_source(path: Path, name: str, class_name: str = "") -> str:
    """The source text of a module-level function, or of a method of a class."""
    source = path.read_text(encoding="utf-8")
    scope: list[ast.stmt] = ast.parse(source).body
    if class_name:
        scope = next(
            node.body
            for node in scope
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
    found = [
        node
        for node in scope
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    assert len(found) == 1, f"{path.name}: expected one {class_name} {name}"
    segment = ast.get_source_segment(source, found[0])
    assert segment
    return segment


def _called_names(source: str) -> set[str]:
    """The names a piece of source calls: ``f(...)`` and ``module.f(...)``."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


class TestProcessChanges:
    def test_its_plan_takes_the_start_from_the_restart_decision(self):
        source = _function_source(_PROCESS_CHANGES, "_run_plan_for")

        assert "decide_restart" in _called_names(source)

    def test_its_plan_does_not_look_for_missing_results_itself(self):
        """Which sections have no result is the restart decision's to read."""
        source = _function_source(_PROCESS_CHANGES, "_run_plan_for")

        assert "extraction_result_uri" not in source

    def test_the_resolver_imports_the_decision_from_the_library(self):
        tree = ast.parse(_PROCESS_CHANGES.read_text(encoding="utf-8"))
        imported = {
            (node.module, alias.name)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }

        assert ("idp_common.run_plan.restart", "decide_restart") in imported


class TestReviewCompletion:
    def test_the_finalize_plan_reads_the_start_stored_on_the_item(self):
        source = _function_source(_FINALIZE, "plan_for", class_name="FinalizeSender")

        assert "restart_step" in source


class TestTheCli:
    def test_the_reprocess_description_has_no_skip_rule_of_its_own(self):
        source = _CLI.read_text(encoding="utf-8")

        assert "_is_skipped" not in source

    def test_the_reprocess_description_is_the_planned_steps(self):
        source = _function_source(_CLI, "_describe_reprocess")

        assert "describe_start" in _called_names(source)
        tree = ast.parse(source)
        assert any(
            isinstance(node, ast.ImportFrom)
            and node.module == "idp_common.run_plan.planned"
            and "describe_start" in {alias.name for alias in node.names}
            for node in ast.walk(tree)
        )


class TestTheStepList:
    def test_it_does_not_describe_a_start(self):
        """``planned.describe_start`` is the one description of a start."""
        assert not hasattr(steps, "describe_start")
