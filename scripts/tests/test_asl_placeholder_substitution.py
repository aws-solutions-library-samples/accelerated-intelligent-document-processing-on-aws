# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No ASL placeholder substitution may corrupt a task ``Resource``.

Several suites read a Step Functions definition (``patterns/unified/statemachine/
workflow.asl.json`` and its three siblings) and resolve the CloudFormation
``DefinitionSubstitutions`` placeholders so the document can be parsed as JSON. One
numeric field — ``TimeoutSeconds`` — carries its placeholder UNQUOTED so it becomes an
integer at deploy time, which makes the raw file invalid JSON, so a substitution is
unavoidable.

The naive way to write it is to anchor on a colon::

    re.compile(r":\\s*\\$\\{[^}]+\\}")   # replaced with ": 1"

That is wrong, and wrong silently. The nine Lambda task resources are strings of the
form ``"arn:${Partition}:states:::lambda:invoke"``, which contain a colon immediately
before ``${``. The naive pattern therefore matches *inside a quoted value* and rewrites
all nine to ``"arn: 1:states:::lambda:invoke"``. Every gate built on that parse is then
asserting against a document that is not what deploys.

Restricting the placeholder NAME does not help: ``Partition`` is alphanumeric, so
``:\\s*\\$\\{[A-Za-z0-9_]+\\}`` corrupts the same nine values. Two spellings in this
repo were fixed only after the corruption was measured; a third had been fixed by
anchoring on the *trailing* delimiter instead. Three different spellings, two of them
broken, and nothing could tell a reader which parses to trust.

So this gate does not check how the substitution is *spelled* — a spelling check is
defeated by a rename or a rewrite. It checks what each substitution *does*: it extracts
every ``(pattern, replacement)`` pair from each enumerated file and applies it to a
probe document containing the ARN shapes, then asserts the ARNs survive. A further
spelling that corrupts them fails here regardless of how it is written.

Enumeration is by content over the source tree, not a hardcoded path list, so a new file
that grows the same idiom is covered without editing this one.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve()

# Directories that are build output, vendored third-party code, or caches. Skipped so
# the walk stays fast and cannot be tripped by a copy of a test inside .aws-sam/.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        ".aws-sam",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "workshop",
    }
)

# A reference to a Step Functions definition file. The unified workflow and the two
# `src/lambda/**` state machines are `*.asl.json`; the fine-tuning one is
# `definition.json`, so a single `.asl.json` rule would miss it.
_DEFINITION_REF = re.compile(
    r"(?:\.asl\.json|finetuning_state_machine/definition\.json)"
)

# The escaped form a `${...}` placeholder takes inside a regex literal. Plain `${` would
# also match f-strings and shell snippets; the backslashes mean "this is a regex".
_REGEX_PLACEHOLDER = r"\$\{"

# A probe standing in for the shapes that actually appear in these definitions:
# an unquoted numeric placeholder (the reason a substitution is needed at all), a
# partition-templated task ARN, its `.waitForTaskToken` variant, and a wholly-quoted
# placeholder resource. Any substitution correct for the real files is correct here.
_PROBE = """{
  "Comment": "probe",
  "TimeoutSeconds": ${WorkflowExecutionTimeoutSeconds},
  "StartAt": "A",
  "States": {
    "A": {
      "Type": "Task",
      "Resource": "arn:${Partition}:states:::lambda:invoke",
      "TimeoutSeconds": ${BDACallbackTimeoutSeconds},
      "Next": "B"
    },
    "B": {
      "Type": "Task",
      "Resource": "arn:${Partition}:states:::lambda:invoke.waitForTaskToken",
      "Next": "C"
    },
    "C": {
      "Type": "Task",
      "Resource": "${OCRFunctionArn}",
      "End": true
    }
  }
}"""

# What a surviving Lambda-invoke ARN looks like. `\\S+` is the load-bearing part: the
# corruption replaces the partition with " 1", so `arn: 1:states:::lambda:invoke`
# contains a space and fails, while `arn:${Partition}:…` and `arn:Partition:…` — the two
# faithful outcomes the existing spellings produce — both pass.
_SURVIVING_ARN = re.compile(r"\Aarn:\S+:states:::lambda:invoke(?:\.waitForTaskToken)?\Z")


def _iter_source_files():
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if _SKIP_DIRS & set(rel.parts):
            continue
        yield path


def _substitution_sites() -> dict[Path, str]:
    """Every source file that resolves `${...}` placeholders in a state machine."""
    sites: dict[Path, str] = {}
    for path in _iter_source_files():
        # This file documents the broken spelling in prose and builds the probe, so it
        # matches its own predicate. Excluding it by resolved path (not by name) keeps
        # the gate from grading its own docstring.
        if path.resolve() == SELF:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _DEFINITION_REF.search(text) and _REGEX_PLACEHOLDER in text:
            sites[path] = text
    return sites


_SITES = _substitution_sites()
_SITE_IDS = [str(p.relative_to(REPO_ROOT)) for p in _SITES]


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _substitution_pairs(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Extract ``(pattern, replacement)`` pairs, in source order.

    Also returns the names of substitutions that could not be evaluated statically (a
    computed pattern, or a callable replacement). Those are reported rather than
    ignored: silently skipping an unanalyzable substitution is how a guard like this
    becomes evadable.
    """
    tree = ast.parse(text)
    # `NAME = re.compile("<literal>")`, at any scope — a function-local compile is just
    # as capable of corrupting the parse as a module-level one.
    compiled: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        call = node.value
        if not isinstance(target, ast.Name) or not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "compile"
            and isinstance(func.value, ast.Name)
            and func.value.id == "re"
        ):
            continue
        if call.args and (lit := _const_str(call.args[0])) is not None:
            compiled[target.id] = lit

    pairs: list[tuple[str, str]] = []
    unresolved: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "sub" or not node.args:
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id == "re":
            # `re.sub(pattern, repl, text)`
            pattern = _const_str(node.args[0]) if node.args else None
            repl = _const_str(node.args[1]) if len(node.args) > 1 else None
            label = f"re.sub@line{node.lineno}"
        elif isinstance(owner, ast.Name) and owner.id in compiled:
            # `COMPILED_NAME.sub(repl, text)`
            pattern = compiled[owner.id]
            repl = _const_str(node.args[0])
            label = f"{owner.id}.sub@line{node.lineno}"
        else:
            continue
        if pattern is None or repl is None or _REGEX_PLACEHOLDER not in pattern:
            if pattern is None or repl is None:
                unresolved.append(label)
            continue
        if (pattern, repl) not in pairs:
            pairs.append((pattern, repl))
    return pairs, unresolved


def test_enumeration_is_not_vacuous():
    """The walk must actually find the known substitution sites.

    A floor, not an exact count, so adding a suite that reads a state machine does not
    require editing this number — but low enough that a broken predicate (a renamed
    directory, a changed definition filename) cannot quietly reduce every test below to
    a no-op. Four sites exist today.
    """
    assert len(_SITES) >= 4, (
        f"expected at least 4 ASL substitution sites, found {len(_SITES)}: {_SITE_IDS}. "
        "If a suite was legitimately retired, lower the floor; if the predicate stopped "
        "matching, fix the predicate — every test in this file is vacuous without it."
    )


@pytest.mark.parametrize("site", _SITE_IDS)
def test_every_substitution_is_statically_analyzable(site):
    """A substitution this gate cannot read is a substitution it cannot police."""
    path = REPO_ROOT / site
    pairs, unresolved = _substitution_pairs(_SITES[path])
    assert not unresolved, (
        f"{site}: could not statically evaluate {unresolved}. This gate proves ASL task "
        "ARNs survive placeholder substitution by applying the pattern to a probe, which "
        "needs a literal pattern and a literal replacement. Keep them literals, or "
        "extend this gate to cover the new form."
    )
    assert pairs, (
        f"{site}: references a state machine definition and contains a regex `${{...}}` "
        "placeholder, but no (pattern, replacement) pair could be extracted. Either the "
        "substitution moved to a form this gate cannot see — fix that — or the file no "
        "longer substitutes and should stop matching the predicate."
    )


@pytest.mark.parametrize("site", _SITE_IDS)
def test_substitution_preserves_task_resource_arns(site):
    """The defect this file exists for: substitution must not rewrite a task ARN.

    Applies the file's own substitutions, in source order, to a probe document and
    checks the result. Order matters — one site resolves in two passes (numeric literal
    first, then collapse every remaining placeholder to its own name) and only the pair
    is correct.
    """
    path = REPO_ROOT / site
    pairs, _ = _substitution_pairs(_SITES[path])
    result = _PROBE
    for pattern, repl in pairs:
        result = re.sub(pattern, repl, result)

    # The whole point of substituting is to get a parseable document.
    try:
        doc = json.loads(result)
    except json.JSONDecodeError as exc:  # pragma: no cover - failure path
        pytest.fail(
            f"{site}: its substitutions do not make the probe parseable as JSON "
            f"({exc}). Applied, in order: {pairs}\n---\n{result}"
        )

    states = doc["States"]
    for name in ("A", "B"):
        resource = states[name]["Resource"]
        assert _SURVIVING_ARN.match(resource), (
            f"{site}: substitution corrupted the task Resource of state {name!r} to "
            f"{resource!r}. A `${{...}}` substitution anchored on a bare colon also "
            "matches INSIDE quoted values, and every task resource is "
            '"arn:${Partition}:states:::lambda:invoke" — a colon sits immediately '
            "before `${`. Anchor on the key's closing quote instead:\n"
            "    re.compile(r'\"\\s*:\\s*\\$\\{[^}]+\\}')   # replaced with '\": 1'\n"
            "Nothing may assert on Resource today, but a parse that silently differs "
            f"from what deploys is not a parse anyone can build on. Applied: {pairs}"
        )

    # A wholly-quoted placeholder resource must survive as a non-empty string too: it is
    # how several gates recognise a Lambda task.
    assert states["C"]["Resource"].strip(), (
        f"{site}: substitution emptied a quoted placeholder Resource. Applied: {pairs}"
    )
