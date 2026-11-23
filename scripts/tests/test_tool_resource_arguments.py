# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A model-facing tool argument that names a resource goes through a validator first.

An agent tool's arguments are chosen by the model. When an argument names something
the tool will read or run (an S3 location, a key, a URL, a path, a SQL statement, a
function name), the value is model output, so the tool has to check it before using
it. This gate finds every Strands tool in first-party code and, for each parameter
whose name says it is such a reference, requires one of:

* the parameter's first use in the function body is as a direct argument of a
  registered validator (:data:`VALIDATORS`), called unconditionally (a statement of
  the function body, or of a ``try`` block that is one); or
* its first use is as a direct argument of another tool in the universe that itself
  passes for the parameter it receives (a wrapper delegating to a checked tool); or
* the parameter is annotated ``bool``, ``int`` or ``float`` (:data:`SCALAR_ANNOTATIONS`),
  so it holds a flag or a number whatever it is called; or
* the parameter is named in :data:`RESOURCE_ARGUMENTS_PERMITTED_UNVALIDATED` with a
  reason specific to that one parameter.

"First use" is the first load of the name in source order, so a value logged,
reformatted or passed anywhere before the validator sees it fails. A tool that never
reads the parameter passes, since it cannot act on it.

A variadic parameter (``*args``, ``**kwargs``) on a tool always needs a listed
reason. A validator counts only when the tool's module imports it from the module
:data:`VALIDATORS` registers, and a ``try`` around it counts as unconditional only
when every handler returns or raises.

What this does **not** establish: that a validator is *sufficient* for its argument
(each validator's own tests say what it accepts), or anything about a value the tool
derives from a parameter whose name does not look like a reference. The name
vocabulary in :func:`_names_a_resource` is the reach, and it is a reach by spelling:
a parameter called ``location`` or ``code``, or a ``dict`` parameter carrying a
location, is outside it. Tools that are not Strands tools (the MCP handler's
``IDPTool`` classes) are outside the universe.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: First-party trees whose tools are checked. Tests are not part of the universe.
TOOL_TREES = ("lib", "src", "nested", "patterns", "feature-platform")

#: Name parts that make a parameter a reference to something a tool reads or runs.
RESOURCE_NAME_PARTS = frozenset(
    {"uri", "url", "bucket", "key", "path", "sql", "query", "arn"}
)

#: A parameter annotated with one of these holds a flag or a number, which cannot
#: name a resource whatever the parameter is called (``return_full_query_results``).
SCALAR_ANNOTATIONS = frozenset({"bool", "int", "float"})

#: Validators a resource argument may be passed to first, with the module that
#: defines each. The module must exist and define the name.
VALIDATORS: Dict[str, str] = {
    "validate_read_only_query": (
        "lib/idp_common_pkg/idp_common/agents/analytics/query_validation.py"
    ),
}

#: ``<path>::<function>::<parameter>`` for tool parameters that name a resource and
#: are used without a validator, each with the reason for that one parameter.
RESOURCE_ARGUMENTS_PERMITTED_UNVALIDATED: Dict[str, str] = {
    "lib/idp_common_pkg/idp_common/agents/error_analyzer/tools/dynamodb_tool.py"
    "::fetch_document_record::object_key": (
        "Used as the partition key of one GetItem on the stack's own tracking table "
        "(doc#<object_key>), so it can only select a tracking record, never another "
        "table or bucket. Which documents a caller may name is a configuration-scope "
        "question for the agent's caller, and no validator for a document key exists "
        "yet."
    ),
    "lib/idp_common_pkg/idp_common/extraction/agentic_idp.py"
    "::view_buffer_data_section::path": (
        "A JSON Pointer into the agent's own in-memory extraction buffer "
        "(agent.state); the function makes no AWS or HTTP call, which "
        "test_in_memory_members_make_no_service_call checks."
    ),
}

#: Members of the mapping above whose reason rests on the function calling no
#: service. Checked by reading the function body.
IN_MEMORY_MEMBERS = (
    "lib/idp_common_pkg/idp_common/extraction/agentic_idp.py"
    "::view_buffer_data_section::path",
)

#: Tools known to exist when this gate was written, so the universe derivation is
#: checked against something it did not produce.
MEASURED_TOOLS = {
    (
        "lib/idp_common_pkg/idp_common/agents/analytics/tools/athena_tool.py",
        "run_athena_query",
    ),
    (
        "lib/idp_common_pkg/idp_common/agents/analytics/agent.py",
        "run_athena_query_with_config",
    ),
    (
        "lib/idp_common_pkg/idp_common/agents/analytics/tools/code_interpreter_tools.py",
        "write_query_results_to_code_sandbox",
    ),
}


def _names_a_resource(param: str) -> bool:
    parts = param.lower().split("_")
    return param == "function_name" or any(p in RESOURCE_NAME_PARTS for p in parts)


def _tool_aliases(module: ast.Module) -> set:
    """Names a module binds to the Strands ``tool`` decorator: ``tool`` itself and
    any ``from strands... import tool as <alias>`` or ``<alias> = ...tool``."""
    names = {"tool"}
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "strands"
        ):
            for alias in node.names:
                if alias.name == "tool" and alias.asname:
                    names.add(alias.asname)
        # agent_tool = tool / agent_tool = strands.tool
        if isinstance(node, ast.Assign) and (
            (isinstance(node.value, ast.Name) and node.value.id == "tool")
            or (isinstance(node.value, ast.Attribute) and node.value.attr == "tool")
        ):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _is_tool_decorator(node: ast.expr, aliases: set) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id in aliases
    if isinstance(target, ast.Attribute):
        return target.attr == "tool"
    return False


@dataclass(frozen=True)
class Tool:
    path: str
    name: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    module: ast.Module

    def params(self) -> List[str]:
        args = self.node.args
        names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
        return [n for n in names if n not in ("self", "cls")]

    def annotation(self, param: str) -> str:
        args = self.node.args
        for a in args.posonlyargs + args.args + args.kwonlyargs:
            if a.arg == param and a.annotation is not None:
                return ast.unparse(a.annotation)
        return ""


def _tracked_python(trees: Tuple[str, ...]) -> List[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", *trees],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [
        p
        for p in out
        if p.endswith(".py")
        and "/tests/" not in f"/{p}"
        and not Path(p).name.startswith("test_")
        and (REPO_ROOT / p).is_file()
    ]


def _universe() -> List[Tool]:
    tools = []
    for rel in _tracked_python(TOOL_TREES):
        try:
            tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        aliases = _tool_aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                _is_tool_decorator(d, aliases) for d in node.decorator_list
            ):
                tools.append(Tool(rel, node.name, node, tree))
    return tools


_UNIVERSE: Optional[List[Tool]] = None


def universe() -> List[Tool]:
    global _UNIVERSE
    if _UNIVERSE is None:
        _UNIVERSE = _universe()
    return _UNIVERSE


def _handler_is_safe(handler: ast.ExceptHandler, param: str) -> bool:
    """A handler ends in ``return`` or ``raise`` and passes ``param`` to no call."""
    if not handler.body or not isinstance(handler.body[-1], (ast.Return, ast.Raise)):
        return False
    for node in ast.walk(handler):
        if isinstance(node, ast.Call):
            args = list(node.args) + [k.value for k in node.keywords]
            if any(isinstance(a, ast.Name) and a.id == param for a in args):
                return False
    return True


def _unconditional_statements(fn: ast.AST, param: str) -> List[ast.stmt]:
    """Statements that run on every call up to the first exception: the function
    body, and the body of any ``try`` among them, recursively -- but only a ``try``
    that has at least one handler, whose every handler ends in ``return`` or
    ``raise`` without passing ``param`` to a call, and whose ``finally`` (if any)
    contains no ``return``, ``break`` or ``continue``. Otherwise a validator that
    raised could be stepped past."""
    out: List[ast.stmt] = []
    pending = list(getattr(fn, "body", []))
    while pending:
        stmt = pending.pop(0)
        out.append(stmt)
        if (
            isinstance(stmt, ast.Try)
            and stmt.handlers
            and all(_handler_is_safe(h, param) for h in stmt.handlers)
            and not any(
                isinstance(n, (ast.Return, ast.Break, ast.Continue))
                for f in stmt.finalbody
                for n in ast.walk(f)
            )
        ):
            pending[:0] = stmt.body
    return out


def _resolve_import(tool_path: str, node: ast.ImportFrom) -> List[str]:
    """Repository paths an ``ImportFrom`` can name: relative imports resolved from
    the tool's file, absolute ones under each ``lib/*`` package root."""
    parts = (node.module or "").split(".") if node.module else []
    if node.level:
        base = Path(tool_path).parent
        for _ in range(node.level - 1):
            base = base.parent
        return [str(base.joinpath(*parts)) + ".py"]
    return [
        str(root.relative_to(REPO_ROOT).joinpath(*parts)) + ".py"
        for root in sorted((REPO_ROOT / "lib").glob("*"))
        if root.is_dir()
    ]


def _imports_validator(tool: "Tool", name: str) -> bool:
    """The tool's module imports ``name`` from the module :data:`VALIDATORS`
    registers for it, and does not define a function of that name itself."""
    imported = any(
        isinstance(n, ast.ImportFrom)
        and VALIDATORS[name] in _resolve_import(tool.path, n)
        and any(a.name == name and a.asname in (None, name) for a in n.names)
        for n in ast.walk(tool.module)
    )
    redefined = any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        and getattr(n, "name", None) == name
        for n in ast.walk(tool.module)
    ) or any(
        isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store)
        for n in ast.walk(tool.module)
    )
    return imported and not redefined


def _first_load(fn: ast.AST, param: str) -> Optional[ast.Name]:
    loads = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Name) and n.id == param and isinstance(n.ctx, ast.Load)
    ]
    return min(loads, key=lambda n: (n.lineno, n.col_offset), default=None)


def _call_taking(fn: ast.AST, name_node: ast.Name) -> Optional[Tuple[ast.Call, int]]:
    """The call that takes ``name_node`` as a direct argument, and its position
    (``-1`` for a keyword argument)."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        for i, arg in enumerate(node.args):
            if arg is name_node:
                return node, i
        for kw in node.keywords:
            if kw.value is name_node:
                return node, -1
    return None


def _callee_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _verdict(tool: Tool, param: str, depth: int = 0) -> Tuple[bool, str]:
    if param == "*":
        return False, "a variadic parameter cannot be checked by name"
    first = _first_load(tool.node, param)
    if first is None:
        return True, "never read"
    taking = _call_taking(tool.node, first)
    if taking is None:
        return False, f"first used at line {first.lineno} outside any call"
    call, position = taking
    callee = _callee_name(call)
    if callee in VALIDATORS:
        if not _imports_validator(tool, callee):
            return False, f"{callee} is not the one imported from {VALIDATORS[callee]}"
        top_level_calls = {
            id(s.value)
            for s in _unconditional_statements(tool.node, param)
            if isinstance(s, (ast.Expr, ast.Assign, ast.AnnAssign, ast.Return))
            and isinstance(getattr(s, "value", None), ast.Call)
        }
        if id(call) in top_level_calls:
            return True, f"validated by {callee}"
        return False, f"{callee} is called conditionally (line {call.lineno})"
    targets = [t for t in universe() if t.name == callee]
    if targets and depth < 3:
        target = targets[0]
        target_params = target.params()
        if position >= 0 and position < len(target_params):
            forwarded = target_params[position]
        else:
            kw = next(k for k in call.keywords if k.value is first)
            forwarded = kw.arg or ""
        ok, why = _verdict(target, forwarded, depth + 1)
        return ok, f"forwarded to tool {callee}({forwarded}): {why}"
    return False, f"first passed to {callee or 'a call'}() at line {call.lineno}"


def _tool_cases(t: Tool) -> List[Tuple[str, Tool, str]]:
    cases = [
        (f"{t.path}::{t.name}::{p}", t, p)
        for p in t.params()
        if _names_a_resource(p) and t.annotation(p) not in SCALAR_ANNOTATIONS
    ]
    # A variadic parameter can carry any argument under any name, so it is a case
    # whatever it is called, and it can never be validated by name.
    for variadic in (t.node.args.vararg, t.node.args.kwarg):
        if variadic is not None:
            cases.append((f"{t.path}::{t.name}::*{variadic.arg}", t, "*"))
    return cases


def _cases() -> List[Tuple[str, Tool, str]]:
    return [case for t in universe() for case in _tool_cases(t)]


@pytest.mark.unit
def test_the_universe_is_derived_and_non_empty():
    found = {(t.path, t.name) for t in universe()}
    assert found, "no Strands tool was found in any first-party tree"
    missing = sorted(MEASURED_TOOLS - found)
    assert not missing, f"tools the derivation no longer finds: {missing}"
    assert _cases(), "no tool parameter names a resource; the gate checks nothing"


@pytest.mark.unit
def test_every_validator_is_defined_where_it_is_registered():
    for name, rel in VALIDATORS.items():
        tree = ast.parse((REPO_ROOT / rel).read_text())
        defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef,))}
        assert name in defined, f"{name} is not defined in {rel}"


@pytest.mark.unit
@pytest.mark.parametrize(
    "member, tool, param", _cases(), ids=[c[0].split("/")[-1] for c in _cases()]
)
def test_resource_arguments_are_validated_before_use(member, tool, param):
    ok, why = _verdict(tool, param)
    if member in RESOURCE_ARGUMENTS_PERMITTED_UNVALIDATED:
        assert not ok, (
            f"{member} is listed in RESOURCE_ARGUMENTS_PERMITTED_UNVALIDATED but now "
            f"passes ({why}). Remove the stale entry."
        )
        return
    assert ok, (
        f"{member}: the tool argument names a resource and is not validated before "
        f"use ({why}). Pass it to a validator in VALIDATORS first, or list it in "
        "RESOURCE_ARGUMENTS_PERMITTED_UNVALIDATED with the reason for that parameter."
    )


@pytest.mark.unit
def test_every_listed_member_still_exists():
    members = {c[0] for c in _cases()}
    vanished = sorted(set(RESOURCE_ARGUMENTS_PERMITTED_UNVALIDATED) - members)
    assert not vanished, (
        f"listed members no longer in the universe (stale entries): {vanished}"
    )


_SERVICE_NAMES = re.compile(
    r"\b(boto3|botocore|httpx|requests|urllib|client|session)\b"
)


@pytest.mark.unit
@pytest.mark.parametrize("member", IN_MEMORY_MEMBERS)
def test_in_memory_members_make_no_service_call(member):
    assert member in RESOURCE_ARGUMENTS_PERMITTED_UNVALIDATED
    path, name, _ = member.split("::")
    tool = next(t for t in universe() if t.path == path and t.name == name)
    body = ast.unparse(tool.node)
    assert not _SERVICE_NAMES.search(body), (
        f"{member} is listed because it calls no service, and its body now names "
        f"{_SERVICE_NAMES.search(body).group(0)!r}"
    )


#: A file beside the validator, so a relative import of it resolves.
_SYNTHETIC_PATH = "lib/idp_common_pkg/idp_common/agents/analytics/tools/synthetic.py"

_HEADER = (
    "from strands import tool\n"
    "from ..query_validation import validate_read_only_query\n"
)


def _synthetic(source: str, header: str = _HEADER) -> Tool:
    module = ast.parse(header + source)
    aliases = _tool_aliases(module)
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and any(
            _is_tool_decorator(d, aliases) for d in node.decorator_list
        ):
            return Tool(_SYNTHETIC_PATH, node.name, node, module)
    raise AssertionError("the synthetic source defines no tool")


@pytest.mark.unit
@pytest.mark.parametrize(
    "source, header, passes",
    [
        pytest.param(
            "@tool\ndef t(query: str):\n    query = validate_read_only_query(query)\n"
            "    return run(query)\n",
            _HEADER,
            True,
            id="validated-first",
        ),
        pytest.param(
            "@tool\ndef t(query: str):\n    try:\n"
            "        validate_read_only_query(query)\n"
            "    except ValueError:\n        return None\n    return run(query)\n",
            _HEADER,
            True,
            id="validated-in-try-whose-handler-returns",
        ),
        pytest.param(
            "@tool\ndef t(query: str):\n    try:\n"
            "        validate_read_only_query(query)\n"
            "    except ValueError:\n        pass\n    return run(query)\n",
            _HEADER,
            False,
            id="handler-falls-through",
        ),
        pytest.param(
            "@tool\ndef t(query: str):\n    try:\n"
            "        validate_read_only_query(query)\n"
            "    except ValueError:\n        return None\n"
            "    finally:\n        return run(query)\n",
            _HEADER,
            False,
            id="finally-returns",
        ),
        pytest.param(
            "@tool\ndef t(query: str):\n    try:\n"
            "        validate_read_only_query(query)\n"
            "    except ValueError:\n        return run(query)\n",
            _HEADER,
            False,
            id="handler-uses-the-argument",
        ),
        pytest.param(
            "@tool\ndef t(query: str):\n    validate_read_only_query(query)\n",
            "from strands import tool\n"
            "from elsewhere.query_validation import validate_read_only_query\n",
            False,
            id="validator-from-another-module",
        ),
        pytest.param(
            "def validate_read_only_query(q):\n    return q\n"
            "@tool\ndef t(query: str):\n    validate_read_only_query(query)\n",
            _HEADER,
            False,
            id="validator-redefined",
        ),
        pytest.param(
            "@tool\ndef t(query: str):\n    if query:\n"
            "        validate_read_only_query(query)\n",
            _HEADER,
            False,
            id="validator-conditional",
        ),
    ],
)
def test_the_validation_rule_on_synthetic_tools(source, header, passes):
    """The rule's branches, each driven by a synthetic tool, because the tree has
    no tool exercising most of them and a rule no input reaches cannot go red."""
    ok, why = _verdict(_synthetic(source, header), "query")
    assert ok is passes, why


@pytest.mark.unit
def test_aliased_decorators_are_found():
    for header in (
        "from strands import tool as agent_tool\n",
        "import strands\nagent_tool = strands.tool\n",
    ):
        tool = _synthetic("@agent_tool\ndef t(query: str):\n    pass\n", header)
        assert tool.name == "t"


@pytest.mark.unit
def test_a_variadic_parameter_is_always_a_case():
    for signature in ("**extra", "*rest", "location: str, **extra"):
        tool = _synthetic(f"@tool\ndef t({signature}):\n    pass\n")
        variadic = [c for c in _tool_cases(tool) if c[2] == "*"]
        assert len(variadic) == 1, signature
        assert _verdict(tool, "*")[0] is False
