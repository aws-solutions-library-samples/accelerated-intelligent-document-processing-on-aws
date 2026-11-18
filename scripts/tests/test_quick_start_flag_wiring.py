# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``EnableQuickStartWidget`` reaches every function that handles a Quick Start turn.

The resolver and both chat processors refuse a Quick Start message when
``QUICK_START_ENABLED`` is ``false``, and read an unset variable as the parameter's
default. So a function that handles a Quick Start turn without the variable
silently offers Quick Start again on a stack that turned it off.

The functions are derived, not listed: every Lambda in a tracked template whose own
tracked, non-test code compares a ``method`` with ``"quick_start"``. That finds a
new route, or a new vendored copy of the processor, as well as the three that
exist. Each must read the variable from the parameter, and the parameter must reach
the nested stack that declares it.
"""

from __future__ import annotations

import re
from pathlib import Path

import gate_premises
import pytest
from repo_files import tracked_paths

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PARAMETER = "EnableQuickStartWidget"
VARIABLE = "QUICK_START_ENABLED"
FUNCTION_TYPES = {"AWS::Serverless::Function", "AWS::Lambda::Function"}
HANDLES_QUICK_START = re.compile(r"""method["')\]]*\s*[!=]=\s*["']quick_start["']""")
KNOWN_HANDLERS = {
    ("template.yaml", "AgentChatProcessorFunction"),
    ("template.yaml", "ChatStreamProcessorFunction"),
    ("nested/api-resolvers/template.yaml", "AgentChatResolverFunction"),
}
PARENT = gate_premises.load_template("template.yaml")
API_STACK = gate_premises.load_template("nested/api-resolvers/template.yaml")
_TRACKED_PY = tracked_paths(REPO_ROOT, "*.py")


def _ref(node: object) -> str | None:
    """The parameter a ``Ref`` names, in either the short or the long form."""
    if isinstance(node, dict) and len(node) == 1:
        key, value = next(iter(node.items()))
        if key in ("Ref", "Fn::Ref") and isinstance(value, str):
            return value
    return None


def _source(directory: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in _TRACKED_PY
        if directory in path.parents
        and "tests" not in path.relative_to(directory).parts
        and not path.name.startswith(("test_", "conftest"))
    )


def _handlers() -> list[tuple[str, str, dict, dict]]:
    """``(template, function, its resource, its template)`` for every handler."""
    found = []
    for path in tracked_paths(REPO_ROOT, "*.yaml", "*.yml"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "AWSTemplateFormatVersion" not in text:
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        template = gate_premises.load_template(relative)
        for name, body in (template.get("Resources") or {}).items():
            if not isinstance(body, dict) or body.get("Type") not in FUNCTION_TYPES:
                continue
            code = (body.get("Properties") or {}).get("CodeUri")
            if not isinstance(code, str) or code.startswith("s3://"):
                continue
            directory = (path.parent / code).resolve()
            if directory.is_dir() and HANDLES_QUICK_START.search(_source(directory)):
                found.append((relative, name, body, template))
    return found


HANDLERS = _handlers()


def test_the_derivation_finds_the_known_handlers():
    """A floor under the derived set. A member the derivation no longer finds fails
    here, whether the signal stopped matching or the function was renamed, so the
    floor cannot go stale beside a narrowed derivation."""
    found = {(template, name) for template, name, _, _ in HANDLERS}
    missing = sorted(KNOWN_HANDLERS - found)
    assert not missing, (
        f"the derivation no longer finds {missing}; it has lost its signal, or the "
        "function moved and KNOWN_HANDLERS should say where"
    )


@pytest.mark.parametrize(
    ("line", "handles"),
    [
        ('if event.get("method") == "quick_start":', True),
        ('surface = "quick_start" if method == "quick_start" else "chat"', True),
        ('if method != "quick_start" and needs_new_session(', True),
        ('(event.get("arguments") or {}).get("method") == "quick_start"', True),
        ('# Scope to a surface (Companion "chat" vs "quick_start")', False),
        ('surface="quick_start"', False),
    ],
)
def test_a_quick_start_turn_is_recognised_by_its_method(line, handles):
    assert bool(HANDLES_QUICK_START.search(line)) is handles


@pytest.mark.parametrize(
    ("template", "name", "body", "document"),
    HANDLERS,
    ids=[f"{template}:{name}" for template, name, _, _ in HANDLERS],
)
def test_every_handler_reads_the_stack_parameter(template, name, body, document):
    variables = ((body.get("Properties") or {}).get("Environment") or {}).get(
        "Variables"
    ) or {}
    assert _ref(variables.get(VARIABLE)) == PARAMETER, (
        f"{template}: {name} handles a Quick Start turn but does not set "
        f"{VARIABLE}: !Ref {PARAMETER}, so it would accept Quick Start on a stack "
        "deployed with it off"
    )
    assert PARAMETER in (document.get("Parameters") or {}), template


def test_the_parent_passes_its_parameter_to_the_api_stack():
    parameters = PARENT["Resources"]["APIRESOLVERSTACK"]["Properties"]["Parameters"]
    assert _ref(parameters.get(PARAMETER)) == PARAMETER


def test_the_api_stack_parameter_matches_the_parents():
    parent, nested = PARENT["Parameters"][PARAMETER], API_STACK["Parameters"][PARAMETER]
    assert nested["AllowedValues"] == parent["AllowedValues"]
    assert nested["Default"] == parent["Default"]


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        ({"Ref": PARAMETER}, PARAMETER),
        ({"Fn::Ref": PARAMETER}, PARAMETER),
        ("true", None),
        ({"Fn::GetAtt": PARAMETER}, None),
    ],
)
def test_a_ref_is_read_in_either_form(node, expected):
    assert _ref(node) == expected
