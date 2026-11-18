# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every place a model holds tools, or its output leaves the system, has an entry.

``scripts/tests/agent_capabilities.yaml`` is the manifest. Each entry names one
surface (an agent, a tool set, an MCP tool, a ``claude -p`` harness) and declares
three things about it:

* ``reads`` -- what the code around the model can read on its behalf;
* ``inputs`` -- what text reaches the model, and where that text comes from;
* ``outputs`` -- where the model's output can go.

Each term is defined once in the manifest's ``vocabulary``: a read term says whether
it is ``private``, an input term gives its ``origin`` (``user``, ``document``,
``external``, ``repository`` or ``none``) and an output term says whether it is
``external``. Where code limits one of the three, the entry names the ``control``
symbol and the ``test`` that exercises it.

What this gate checks:

* **Universe closure.** The surfaces are derived from source on every run, by AST:
  Strands ``@tool`` functions, ``Agent``/``BedrockModel``/``CodeInterpreter``/
  ``MCPClient`` constructions and ``tool(fn)`` wrappers whose names are imported
  from ``strands`` or ``bedrock_agentcore``, ``register_agent`` calls, the MCP
  handler's ``TOOLS`` table, and argv lists under ``scripts/`` that run Claude Code
  headless. Each derived surface must be claimed by exactly one entry, and a claimed
  surface that is no longer derived is a stale entry.
* **Declared surfaces.** A surface the derivation cannot see (a single model call
  whose output the web UI renders) is listed under ``declared`` as a
  ``path::qualified.name`` that must still exist.
* **The combination rule.** When an entry's ``inputs`` include document-derived or
  external text and its ``reads`` include private data, each of its external
  ``outputs`` must be limited by a ``control`` (``{symbol, limits}``), or listed in a
  ``finding`` that says nothing limits it. A control limiting one channel does not
  stand in for another. Findings are counted, and :data:`PENDING_CONTROL_FINDINGS` pins
  them one by one, so the set can only shrink by a deliberate edit.
* **Controls and tests resolve.** A ``control`` must name a symbol that exists (a
  Python definition found by AST, or a TypeScript declaration). A ``test`` must be a
  node pytest or vitest would collect, in a directory a CI recipe runs
  (``test_src_lambda_tests_in_ci.py`` derives that set).
* **Roles.** An entry's ``roles`` name the Lambda functions whose credentials the
  model's tools run with. Each must exist in its template and have a declaration
  under the manifest's ``roles`` section, which ``test_analytics_role_grants.py``
  reads to bound those roles' grants.

**The derivation's reach is by spelling.** It sees ``tool``, ``Agent`` and the other
constructors when they are imported by name (aliases included) from a ``strands`` or
``bedrock_agentcore`` module, or reached as an attribute of a module bound by
``import strands``. It does not see ``@tools.tool`` through ``from strands import
tools``, ``t = strands.tool`` followed by ``@t``, ``strands.agent.Agent(...)``, a
subclass of ``Agent`` constructed under its own name, an undecorated function added
to an existing ``tools=[...]`` list, or a ``claude`` argv spelled ``--print`` or
assembled without the flags in :data:`CLAUDE_FLAGS` (the interactive pair session in
``scripts/ux_runner/run_in_docker.py`` is one). None of those forms creates a surface
in the tree today except that pair session, which is a person's own session rather
than an unattended one.

⚠️ **This gate checks declarations, not truth.** It cannot tell whether an entry's
``reads`` are complete, whether a ``control`` actually limits what the entry says it
limits, or whether a cited test asserts anything about it. Those are the questions
``.claude/skills/agent-capability-review.md`` asks of the code, one surface at a
time. What this gate guarantees is that a new surface cannot arrive without an entry,
that an entry cannot outlive its surface, and that a declaration naming a control
names one that exists.
"""

from __future__ import annotations

import ast
import functools
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = Path(__file__).resolve().parent / "agent_capabilities.yaml"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_src_lambda_tests_in_ci as ci_coverage  # noqa: E402

#: The open findings the manifest records, one id per member: an entry with a
#: ``finding`` is ``entry:<name>``, and a role grant declared ``unused`` is
#: ``role:<function>:<kind or "invoke">:<bucket and key, or resource>``. The reason for
#: each member is the manifest's own text beside it. Pinned in both directions, so a
#: new finding is a deliberate edit here, and a fixed one has to be removed.
PENDING_CONTROL_FINDINGS = (
    "entry:failure-agent-harness",
    "role:ExtractionFunction:delete:OutputBucket/*",
    "role:ShardRuntimeFunction:delete:WorkingBucket/*",
    "role:ShardRuntimeFunction:delete:OutputBucket/*",
    "role:MultiDocDiscoveryAnalyzeFunction:write:DiscoveryBucket/*",
    "role:MultiDocDiscoveryAnalyzeFunction:delete:DiscoveryBucket/*",
)

#: Constructions that put a model, a sandbox or a remote tool server in reach, by the
#: name a module imports from one of :data:`AGENT_PACKAGES`.
CONSTRUCTORS = frozenset({"Agent", "BedrockModel", "CodeInterpreter", "MCPClient"})

#: Packages whose imports make a constructor or ``tool`` name count.
AGENT_PACKAGES = ("strands", "bedrock_agentcore")

#: The MCP handler's tool table.
MCP_TOOLS_MODULE = "src/lambda/agentcore_mcp_handler/tools/__init__.py"

#: Where ``claude -p`` harnesses live.
HARNESS_TREE = "scripts"

#: An argv list runs Claude Code headless when it carries ``-p`` and one of these.
CLAUDE_FLAGS = frozenset(
    {"--output-format", "--allowedTools", "--permission-mode", "--mcp-config"}
)

ORIGINS = frozenset({"user", "document", "external", "repository", "none"})
UNTRUSTED_ORIGINS = frozenset({"document", "external"})

#: TypeScript declarations a ``control`` may name.
_TS_DECLARATION = r"(?:export\s+)?(?:default\s+)?(?:const|let|function|class|interface|type)\s+{name}\b"

#: The first argument of a vitest ``describe``/``it``/``test`` call.
_VITEST_TITLE = r"\b(?:describe|it|test)(?:\.\w+)*\(\s*(['\"`]){title}\1"


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def _tracked_python() -> Tuple[str, ...]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return tuple(
        p
        for p in out
        if "/tests/" not in f"/{p}"
        and not Path(p).name.startswith("test_")
        and Path(p).name != "conftest.py"
        and (REPO_ROOT / p).is_file()
    )


@functools.lru_cache(maxsize=None)
def _parse(rel: str) -> Optional[ast.Module]:
    try:
        return ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None


def _walk_with_qualname(tree: ast.AST) -> Iterator[Tuple[ast.AST, str]]:
    """Every node with the dotted name of the def or class enclosing it."""

    def visit(node: ast.AST, scope: List[str]) -> Iterator[Tuple[ast.AST, str]]:
        for child in ast.iter_child_nodes(node):
            yield child, ".".join(scope)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                yield from visit(child, [*scope, child.name])
            else:
                yield from visit(child, scope)

    yield from visit(tree, [])


def _imported_names(tree: ast.Module) -> Dict[str, str]:
    """Local name -> imported name, for imports from :data:`AGENT_PACKAGES`."""
    names: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            AGENT_PACKAGES
        ):
            for alias in node.names:
                names[alias.asname or alias.name] = alias.name
    return names


def _package_modules(tree: ast.Module) -> Set[str]:
    """Local names bound to ``import strands`` and the like."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in AGENT_PACKAGES:
                    out.add(alias.asname or alias.name.split(".")[0])
    return out


def _agent_name(node: ast.expr, imported: Dict[str, str], modules: Set[str]) -> str:
    """The imported name a call target or decorator resolves to, or ``""``."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return imported.get(target.id, "")
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id in modules
    ):
        return target.attr
    return ""


# ---------------------------------------------------------------------------
# The derived universe
# ---------------------------------------------------------------------------


def _python_surfaces(rel: str, tree: ast.Module) -> Set[str]:
    imported = _imported_names(tree)
    modules = _package_modules(tree)
    found = set()
    for node, scope in _walk_with_qualname(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(
                _agent_name(d, imported, modules) == "tool" for d in node.decorator_list
            ):
                qual = f"{scope}.{node.name}" if scope else node.name
                found.add(f"tool:{rel}::{qual}")
        elif isinstance(node, ast.Call):
            name = _agent_name(node, imported, modules)
            if name in CONSTRUCTORS or name == "tool":
                found.add(f"construct:{rel}::{scope or '<module>'}::{name}")
            elif (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "register_agent"
            ):
                found.add(f"agent:{_registered_agent_id(node)}")
    return found


def _registered_agent_id(call: ast.Call) -> str:
    for kw in call.keywords:
        if kw.arg == "agent_id":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
            creator = next(
                (k.value for k in call.keywords if k.arg == "creator_func"), None
            )
            return "<dynamic: %s>" % (ast.unparse(creator) if creator else "?")
    return "<no agent_id>"


def _mcp_tool_surfaces() -> Set[str]:
    tree = _parse(MCP_TOOLS_MODULE)
    assert tree is not None, f"{MCP_TOOLS_MODULE} does not parse"
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        if (
            isinstance(target, ast.Name)
            and target.id == "TOOLS"
            and isinstance(value, ast.Dict)
        ):
            return {
                f"mcp:{k.value}"
                for k in value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
    raise AssertionError(f"{MCP_TOOLS_MODULE} defines no TOOLS table")


def _harness_surfaces(rel: str, tree: ast.Module) -> Set[str]:
    found = set()
    for node, scope in _walk_with_qualname(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        strings = {
            e.value
            for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        }
        if "-p" in strings and strings & CLAUDE_FLAGS:
            found.add(f"harness:{rel}::{scope or '<module>'}")
    return found


@functools.lru_cache(maxsize=None)
def derived_surfaces() -> Set[str]:
    found = set(_mcp_tool_surfaces())
    for rel in _tracked_python():
        tree = _parse(rel)
        if tree is None:
            continue
        found |= _python_surfaces(rel, tree)
        if rel.startswith(f"{HARNESS_TREE}/"):
            found |= _harness_surfaces(rel, tree)
    return found


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def manifest() -> Dict[str, Any]:
    return yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))


def _entries() -> Dict[str, Dict[str, Any]]:
    return manifest()["entries"]


def _vocabulary(field: str) -> Dict[str, Dict[str, Any]]:
    return manifest()["vocabulary"][field]


def _list(entry: Dict[str, Any], field: str) -> List[Any]:
    value = entry.get(field) or []
    return value if isinstance(value, list) else [value]


def needs_control(entry: Dict[str, Any]) -> bool:
    """Untrusted text in, private data read, and an external channel out."""
    inputs = _vocabulary("inputs")
    reads = _vocabulary("reads")
    outputs = _vocabulary("outputs")
    return (
        any(inputs[t]["origin"] in UNTRUSTED_ORIGINS for t in _list(entry, "inputs"))
        and any(reads[t]["private"] for t in _list(entry, "reads"))
        and any(outputs[t]["external"] for t in _list(entry, "outputs"))
    )


def takes_document_input(entry: Dict[str, Any]) -> bool:
    inputs = _vocabulary("inputs")
    return any(inputs[t]["origin"] == "document" for t in _list(entry, "inputs"))


def document_input_roles() -> Set[Tuple[str, str]]:
    """``(template, function)`` for every role named by an entry that takes
    document-derived input. ``test_analytics_role_grants.py`` reads this."""
    declared = manifest()["roles"]
    out = set()
    for entry in _entries().values():
        if takes_document_input(entry):
            for role in _list(entry, "roles"):
                out.add((declared[role]["template"], role))
    return out


def roles_with_output(output: str) -> Set[str]:
    return {
        role
        for entry in _entries().values()
        if output in _list(entry, "outputs")
        for role in _list(entry, "roles")
    }


def open_findings() -> List[str]:
    found = [
        f"entry:{name}" for name, entry in _entries().items() if entry.get("finding")
    ]
    for role, decl in manifest()["roles"].items():
        for item in decl.get("object_access", []):
            if item.get("unused"):
                found.append(
                    f"role:{role}:{item['kind']}:{item['bucket']}{item['key']}"
                )
        for item in decl.get("invoke", []):
            if item.get("unused"):
                found.append(f"role:{role}:invoke:{item['resource']}")
    return found


def claimed_surfaces() -> Dict[str, List[str]]:
    claims: Dict[str, List[str]] = {}
    for name, entry in _entries().items():
        for surface in _list(entry, "surfaces"):
            claims.setdefault(surface, []).append(name)
    return claims


# ---------------------------------------------------------------------------
# Symbol and test-node resolution
# ---------------------------------------------------------------------------


def _python_symbol_exists(rel: str, qualname: str) -> bool:
    tree = _parse(rel)
    if tree is None:
        return False
    parts = qualname.split(".")
    scope: List[ast.stmt] = list(tree.body)
    for i, part in enumerate(parts):
        match = None
        for node in scope:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == part
            ):
                match = node
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if any(isinstance(t, ast.Name) and t.id == part for t in targets):
                    match = node
        if match is None:
            return False
        if i < len(parts) - 1:
            if not isinstance(
                match, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                return False
            scope = list(match.body)
    return True


def symbol_exists(ref: str) -> bool:
    """``path::name`` names a definition: a Python def, class or assignment (dotted
    for nesting), or a TypeScript declaration."""
    rel, _, name = ref.partition("::")
    path = REPO_ROOT / rel
    if not name or not path.is_file():
        return False
    if rel.endswith(".py"):
        return _python_symbol_exists(rel, name)
    if rel.endswith((".ts", ".tsx")):
        pattern = _TS_DECLARATION.format(name=re.escape(name))
        return re.search(pattern, path.read_text(encoding="utf-8")) is not None
    return False


@functools.lru_cache(maxsize=None)
def _ci_covered_dirs() -> Set[str]:
    return set(ci_coverage.ci_run_dirs())


def _run_by_ci(rel: str) -> bool:
    """The test file's directory is one a CI recipe runs pytest against."""
    return ci_coverage._covered_by(Path(rel).parent.as_posix(), _ci_covered_dirs())


def _pytest_node_collects(rel: str, names: List[str]) -> bool:
    """pytest's default collection rules, read statically: a ``test_*.py`` file, a
    ``Test*`` class with no ``__init__``, a ``test*`` function in it."""
    if not Path(rel).name.startswith("test_") or not names:
        return False
    tree = _parse(rel)
    if tree is None:
        return False
    *classes, func = names
    func = func.split("[", 1)[0]
    body: List[ast.stmt] = list(tree.body)
    for cls in classes:
        node = next(
            (n for n in body if isinstance(n, ast.ClassDef) and n.name == cls), None
        )
        if node is None or not cls.startswith("Test"):
            return False
        if any(
            isinstance(n, ast.FunctionDef) and n.name == "__init__" for n in node.body
        ):
            return False
        body = list(node.body)
    return func.startswith("test") and any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func
        for n in body
    )


def node_collects(node: str) -> bool:
    rel, *names = node.split("::")
    path = REPO_ROOT / rel
    if not path.is_file():
        return False
    if rel.endswith(".py"):
        return _run_by_ci(rel) and _pytest_node_collects(rel, names)
    if re.search(r"\.test\.tsx?$", rel) and rel.startswith("src/ui/"):
        if len(names) != 1:
            return False
        pattern = _VITEST_TITLE.format(title=re.escape(names[0]))
        return re.search(pattern, path.read_text(encoding="utf-8")) is not None
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Measured:
    surface: str
    why: str


#: Surfaces that existed when this gate was written, so the derivation is checked
#: against something it did not produce. One per derivation rule.
MEASURED_SURFACES = (
    _Measured(
        "tool:lib/idp_common_pkg/idp_common/agents/analytics/tools/athena_tool.py"
        "::run_athena_query",
        "a module-level @tool",
    ),
    _Measured(
        "tool:lib/idp_common_pkg/idp_common/agents/analytics/tools/"
        "code_interpreter_tools.py::CodeInterpreterTools.execute_python",
        "a @tool method",
    ),
    _Measured(
        "tool:lib/idp_common_pkg/idp_common/agents/quick_start/tools/"
        "bootstrap_tools.py::search_catalog",
        "a @strands.tool",
    ),
    _Measured(
        "tool:lib/idp_common_pkg/idp_common/discovery/discovery_agent.py"
        "::DiscoveryAgent._create_agent.get_document_image",
        "a @tool nested in a method, imported inside the function",
    ),
    _Measured(
        "construct:lib/idp_common_pkg/idp_common/agents/orchestrator/agent.py"
        "::create_orchestrator_agent.create_tool_function::tool",
        "a tool(fn) wrapper",
    ),
    _Measured(
        "construct:lib/idp_common_pkg/idp_common/agents/code_intelligence/agent.py"
        "::create_code_intelligence_agent::MCPClient",
        "an MCPClient construction",
    ),
    _Measured(
        "construct:lib/idp_common_pkg/idp_common/agents/analytics/tools/"
        "code_interpreter_tools.py::CodeInterpreterTools._get_code_interpreter_client"
        "::CodeInterpreter",
        "a CodeInterpreter construction",
    ),
    _Measured(
        "construct:lib/idp_common_pkg/idp_common/agents/analytics/agent.py"
        "::create_analytics_agent::Agent",
        "a strands.Agent construction through the module",
    ),
    _Measured("agent:Analytics-Agent", "a registered agent"),
    _Measured("mcp:get_results", "an MCP handler tool"),
    _Measured("harness:scripts/sdlc/ai_mr_review.py::claude_command", "a harness"),
)


@pytest.mark.unit
@pytest.mark.parametrize("measured", MEASURED_SURFACES, ids=lambda m: m.why)
def test_the_derivation_finds_each_kind_of_surface(measured: _Measured) -> None:
    assert measured.surface in derived_surfaces(), (
        f"{measured.why} is no longer derived: {measured.surface}. Either the "
        "surface moved (update this list and the manifest) or a derivation rule "
        "regressed."
    )


@pytest.mark.unit
def test_every_derived_surface_has_one_entry() -> None:
    claims = claimed_surfaces()
    unclaimed = sorted(s for s in derived_surfaces() if s not in claims)
    assert not unclaimed, (
        "these surfaces have no entry in scripts/tests/agent_capabilities.yaml; add "
        "each to the `surfaces` of the entry for the agent or channel it belongs to, "
        "or a new entry declaring its reads, inputs and outputs:\n  "
        + "\n  ".join(unclaimed)
    )
    twice = {s: n for s, n in claims.items() if len(n) > 1}
    assert not twice, f"claimed by more than one entry: {twice}"


@pytest.mark.unit
def test_no_entry_claims_a_surface_that_is_gone() -> None:
    stale = sorted(s for s in claimed_surfaces() if s not in derived_surfaces())
    assert not stale, (
        f"stale manifest surfaces (no longer derived from source): {stale}. Remove "
        "them, or move a surface the derivation cannot see to `declared`."
    )


@pytest.mark.unit
def test_every_declared_surface_exists() -> None:
    missing = [
        (name, ref)
        for name, entry in _entries().items()
        for ref in _list(entry, "declared")
        if not symbol_exists(ref)
    ]
    assert not missing, f"declared surfaces that no longer exist: {missing}"


@pytest.mark.unit
def test_every_entry_names_a_surface() -> None:
    empty = [
        name
        for name, entry in _entries().items()
        if not _list(entry, "surfaces") and not _list(entry, "declared")
    ]
    assert not empty, f"entries with no surface: {empty}"


@pytest.mark.unit
def test_the_vocabulary_is_well_formed_and_used() -> None:
    vocab = manifest()["vocabulary"]
    for term, spec in vocab["inputs"].items():
        assert spec.get("origin") in ORIGINS, f"input {term}: origin not in {ORIGINS}"
    for term, spec in vocab["reads"].items():
        assert isinstance(spec.get("private"), bool), f"read {term}: private?"
    for term, spec in vocab["outputs"].items():
        assert isinstance(spec.get("external"), bool), f"output {term}: external?"
    for field in ("reads", "inputs", "outputs"):
        used = {t for e in _entries().values() for t in _list(e, field)}
        unknown = sorted(used - set(vocab[field]))
        assert not unknown, f"{field} terms used but not defined: {unknown}"
        unused = sorted(set(vocab[field]) - used)
        assert not unused, f"{field} terms defined but used by no entry: {unused}"


def _terms(entry: Dict[str, Any]) -> Set[str]:
    return {t for f in ("reads", "inputs", "outputs") for t in _list(entry, f)}


def _external_outputs(entry: Dict[str, Any]) -> List[str]:
    outputs = _vocabulary("outputs")
    return [t for t in _list(entry, "outputs") if outputs[t]["external"]]


def unlimited_outputs(entry: Dict[str, Any]) -> List[str]:
    """External outputs of an entry that needs a control which no control limits
    and no finding lists."""
    if not needs_control(entry):
        return []
    limited = {c["limits"] for c in _list(entry, "control")}
    recorded = set((entry.get("finding") or {}).get("outputs") or [])
    return [t for t in _external_outputs(entry) if t not in limited | recorded]


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_entries()))
def test_each_external_output_of_an_entry_that_needs_it_is_limited(name: str) -> None:
    entry = _entries()[name]
    missing = unlimited_outputs(entry)
    assert not missing, (
        f"{name} takes untrusted text and reads private data, and these external "
        f"outputs have no control that limits them: {missing}. Name the `control` "
        "(`{symbol, limits}`), or record a `finding` listing them under `outputs` "
        "with what was found."
    )


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_entries()))
def test_controls_and_findings_name_the_entry_s_own_terms(name: str) -> None:
    entry = _entries()[name]
    terms = _terms(entry)
    for control in _list(entry, "control"):
        assert set(control) == {"symbol", "limits"}, (
            f"{name}: a control is {{symbol, limits}}: {control}"
        )
        assert control["limits"] in terms, (
            f"{name}: {control['symbol']} limits {control['limits']!r}, which is "
            "not one of this entry's reads, inputs or outputs"
        )
    finding = entry.get("finding")
    if finding:
        assert set(finding) == {"outputs", "text"} and finding["text"].strip(), (
            f"{name}: a finding is {{outputs, text}}: {finding}"
        )
        stray = sorted(set(finding["outputs"]) - set(_external_outputs(entry)))
        assert not stray, (
            f"{name}: the finding lists outputs that are not external outputs of the entry: {stray}"
        )
        assert needs_control(entry), (
            f"{name} records a finding but does not combine untrusted input, private "
            "reads and an external output, so there is nothing for it to record"
        )


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_entries()))
def test_controls_and_tests_resolve(name: str) -> None:
    entry = _entries()[name]
    controls = [c["symbol"] for c in _list(entry, "control")]
    tests = _list(entry, "test")
    missing = [c for c in controls if not symbol_exists(c)]
    assert not missing, f"{name}: control symbols that do not exist: {missing}"
    uncollected = [t for t in tests if not node_collects(t)]
    assert not uncollected, (
        f"{name}: test nodes that would not be collected, or sit in a directory no "
        f"CI recipe runs: {uncollected}"
    )
    if controls:
        assert tests, f"{name} names a control and no test that exercises it"


@pytest.mark.unit
def test_open_findings_are_pinned() -> None:
    found = open_findings()
    new = sorted(set(found) - set(PENDING_CONTROL_FINDINGS))
    fixed = sorted(set(PENDING_CONTROL_FINDINGS) - set(found))
    assert not new, (
        f"the manifest records findings PENDING_CONTROL_FINDINGS does not pin: {new}. "
        "Adding one is a deliberate edit to that tuple."
    )
    assert not fixed, (
        f"PENDING_CONTROL_FINDINGS pins findings the manifest no longer records: "
        f"{fixed}. Remove them."
    )
    assert len(found) == len(set(found)), f"a finding is recorded twice: {found}"


@pytest.mark.unit
def test_roles_are_declared() -> None:
    """Every role an entry names has a declaration, and every declaration is named.
    ``test_analytics_role_grants.py`` checks each one is a function in its template
    and bounds its grants."""
    declared = manifest()["roles"]
    named = {r for e in _entries().values() for r in _list(e, "roles")}
    assert not sorted(named - set(declared)), (
        f"roles named by an entry with no declaration: {sorted(named - set(declared))}"
    )
    assert not sorted(set(declared) - named), (
        f"role declarations no entry names: {sorted(set(declared) - named)}"
    )
    for role, decl in declared.items():
        for item in [*decl.get("object_access", []), *decl.get("invoke", [])]:
            assert bool(item.get("uses")) != bool(item.get("unused")), (
                f"{role}: each grant says either the code path that `uses` it or "
                f"that it is `unused`: {item}"
            )
