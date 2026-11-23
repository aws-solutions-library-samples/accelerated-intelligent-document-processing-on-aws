# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every Bedrock model call applies the configured guardrail, or says why not.

A stack deployed with ``BedrockGuardrailId`` sets ``GUARDRAIL_ID_AND_VERSION`` on
the Lambdas that call models, and the guardrail is applied only where the code
that makes the call attaches it. ``BedrockClient`` and the Strands model factory
do that; a component that calls ``bedrock-runtime`` or builds a Strands
``BedrockModel`` itself does not inherit it. Agentic extraction and the chat
processor were both in that state while their Lambdas had the variable set.

Two halves, both derived rather than listed:

**Code.** Every model call in production code under ``lib/`` and ``src/lambda/``
-- found by AST, not by grep -- must be one of:

1. a call *into* ``BedrockClient`` (it passes an argument no ``bedrock-runtime``
   operation accepts, so it cannot be a raw boto3 call; see
   :func:`route_only_keywords`), or a model call *inside* a guardrail route
   (:data:`GUARDRAIL_ROUTES`), whose behaviour
   ``lib/idp_common_pkg/tests/unit/bedrock/test_guardrail_routes.py`` measures;
2. a call that attaches the guardrail itself, in its API's shape; or
3. listed in :data:`MODEL_CALL_GUARDRAIL_EXEMPT` with its own reason and a pinned
   site count.

Anything else is unaccounted and fails.

**Templates.** Every Lambda in the deployed stack (the parent template and the
nested stacks it deploys) whose role can invoke a Bedrock model sets
``GUARDRAIL_ID_AND_VERSION`` from ``BedrockGuardrailId``/``BedrockGuardrailVersion``
and holds ``bedrock:ApplyGuardrail`` on that guardrail under the same condition,
or is listed in :data:`GUARDRAIL_ENV_EXEMPT` with its own reason.

What this does not establish, so a green run is not over-read:

* It reads the five callee names ``BedrockModel``, ``converse``,
  ``converse_stream``, ``invoke_model`` and ``invoke_model_with_response_stream``,
  plus ``getattr`` with one of them as a literal. The OpenAI Responses route
  (``idp_common.bedrock.openai_responses``) posts to the bedrock-mantle endpoint
  over HTTP and carries no guardrail; it logs a warning when one is configured,
  and is outside this universe.
* A Strands ``Agent`` given a model id string, or no model, builds its own
  ``BedrockModel`` inside Strands, which this AST cannot see. Every ``Agent`` in
  the tree today is handed a model built by the factory or by agentic extraction.
* ``feature-platform/``, ``scripts/``, ``benchmarks/`` and ``samples/`` are not
  part of the main stack's Lambda code and are not read; the template half reads
  the parent template and the nested stacks it deploys, not separately deployed
  feature stacks.
* Other Bedrock APIs (``retrieve_and_generate``, ``invoke_agent``,
  ``start_async_invoke``), a ``getattr`` whose name is a variable,
  ``_make_api_call`` and ``operator.methodcaller`` are not read. The knowledge
  base resolver's ``retrieve_and_generate`` applies the guardrail with its own
  parser.
* A guardrail key stored before the call and then removed (``kw.pop(...)``), or
  stored in a branch that never runs, is still read as attached.
* Route calls are recognised by their arguments. A third-party method that takes
  ``model_id=`` and calls Bedrock would be read as a ``BedrockClient`` call; only
  defs in this tree are checked to be routes (:func:`test_callee_named_defs_are_routes`).
"""

from __future__ import annotations

import ast
import fnmatch
import functools
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import botocore.session
import gate_premises
import pytest

REPO_ROOT = gate_premises.REPO_ROOT

#: The Bedrock model-call names the universe is built from.
CALLEES = frozenset(
    {
        "BedrockModel",
        "converse",
        "converse_stream",
        "invoke_model",
        "invoke_model_with_response_stream",
    }
)
RUNTIME_CALLEES = CALLEES - {"BedrockModel"}

#: ``bedrock-runtime`` operation for each boto3 method name.
RUNTIME_OPERATIONS = {
    "converse": "Converse",
    "converse_stream": "ConverseStream",
    "invoke_model": "InvokeModel",
    "invoke_model_with_response_stream": "InvokeModelWithResponseStream",
}

#: Every tree holding deployed Lambda or library code, not only ``lib/`` and
#: ``src/lambda/``: nested stacks and the pattern keep their own Lambda sources.
UNIVERSE_PATHSPECS = (
    "lib/*.py",
    "src/lambda/*.py",
    "nested/*/src/*.py",
    "patterns/*/src/*.py",
)

CLIENT_MODULE = "lib/idp_common_pkg/idp_common/bedrock/client.py"
FACTORY_MODULE = "lib/idp_common_pkg/idp_common/agents/common/strands_bedrock_model.py"
ROUTE_TESTS = "lib/idp_common_pkg/tests/unit/bedrock/test_guardrail_routes.py"

#: The defs whose own model calls attach the guardrail, each with the test in
#: ROUTE_TESTS that measures it. ``path::qualname``.
GUARDRAIL_ROUTES = {
    f"{CLIENT_MODULE}::BedrockClient._invoke_with_retry": (
        "test_bedrock_client_sends_the_configured_guardrail"
    ),
    f"{FACTORY_MODULE}::create_strands_bedrock_model": (
        "test_the_strands_factory_applies_the_guardrail"
    ),
}

#: The def a route call lands in. A def in the universe named like a
#: ``bedrock-runtime`` method and taking named parameters must be one of these.
ROUTE_ENTRY_POINTS = frozenset({f"{CLIENT_MODULE}::BedrockClient.invoke_model"})

#: Helpers whose ``**`` splat attaches the guardrail, by the callee they serve.
#: Their output is checked against botocore and Strands in ROUTE_TESTS.
GUARDRAIL_SPLAT_HELPERS = {
    "BedrockModel": "strands_guardrail_kwargs",
    "invoke_model": "guardrail_invoke_model_params",
    "invoke_model_with_response_stream": "guardrail_invoke_model_params",
}

#: The Strands ``BedrockModel`` keyword that names the guardrail.
STRANDS_GUARDRAIL_KEYWORD = "guardrail_id"

#: Model calls that do not apply the guardrail, each keyed
#: ``path::qualname::callee`` with the number of sites it covers and why.
MODEL_CALL_GUARDRAIL_EXEMPT: dict[str, dict] = {
    f"{CLIENT_MODULE}::BedrockClient._generate_embedding_with_retry::invoke_model": {
        "sites": 1,
        "reason": (
            "An embedding request: the response is a vector, not generated text, "
            "and guardrail policies are written against text a model produces or "
            "is given. The embedding path has never sent a guardrail; whether "
            "InvokeModel would accept one for an embedding model is not measured."
        ),
    },
    "lib/idp_common_pkg/idp_common/utils/bedrock_utils.py"
    "::BedrockClientWrapper.__init__::invoke_model": {
        "sites": 1,
        "reason": (
            "A reference, not a call: the wrapper decorates self.client.invoke_model "
            "with retries. Its invoke_model forwards the caller's kwargs untouched, "
            "so whether a guardrail is sent is decided at the caller's call site, "
            "which is itself in this universe and judged by the raw-call rule."
        ),
    },
    "lib/idp_common_pkg/idp_common/utils/bedrock_utils.py"
    "::BedrockClientWrapper.__init__::converse": {
        "sites": 1,
        "reason": (
            "A reference to self.client.converse wrapped with retries; the wrapper's "
            "converse forwards the caller's kwargs, so its caller's call site is "
            "where a guardrailConfig would appear, and that site is judged here."
        ),
    },
    "lib/idp_common_pkg/idp_common/utils/bedrock_utils.py"
    "::BedrockClientWrapper.__init__::converse_stream": {
        "sites": 1,
        "reason": (
            "A reference to self.client.converse_stream wrapped with retries; as "
            "with converse, the forwarded kwargs come from a caller whose own call "
            "site this gate reads."
        ),
    },
}

#: Deployed Lambdas that can invoke a model but are not given the guardrail,
#: keyed ``template::LogicalId``.
GUARDRAIL_ENV_EXEMPT: dict[str, str] = {
    "patterns/unified/template.yaml::ClassificationFunction": (
        "Applying the guardrail broke classification when guardrail support was "
        "added, and the variable was disabled for this function alone (the "
        "template comment beside it). The failure mode was not recorded and has "
        "not been re-measured; re-enabling it needs a live run on a stack with a "
        "guardrail configured."
    ),
    "patterns/unified/template.yaml::OCRFunction": (
        "With the Bedrock OCR backend the model's output is the page text itself, "
        "so an intervention would put the guardrail's blocked-output message where "
        "the page text belongs and every later step would read it as document "
        "content (inferred, not measured). Not enabled until that is decided with "
        "a measurement."
    ),
    "patterns/unified/template.yaml::AssessmentFunction": (
        "Assessment's output is a confidence JSON per attribute; an intervention "
        "would replace it with the blocked-output message, which the parser "
        "rejects (inferred, not measured). Enabling it changes pipeline behaviour "
        "for every stack with a guardrail and is left for a measured decision."
    ),
    "patterns/unified/template.yaml::EvaluationFunction": (
        "The LLM evaluation method compares an extracted value with its expected "
        "value from the baseline set and returns a match verdict; it produces "
        "nothing that reaches a user as generated text. Enabling it is a "
        "behaviour change left for a measured decision."
    ),
    "nested/multi-doc-discovery/template.yaml::MultiDocDiscoveryEmbedFunction": (
        "Its only model call is BedrockClient.generate_embedding, which sends no "
        "guardrail (see MODEL_CALL_GUARDRAIL_EXEMPT); setting the variable here "
        "would change nothing."
    ),
    "nested/api-resolvers/template.yaml::ConfigurationResolverFunction": (
        "Its model call is the rule translator's generateRuleJson, which turns a "
        "rule an administrator types while editing configuration into SMT "
        "constraints returned to that administrator for review. The text comes "
        "from the configuration editor, not from a document."
    ),
}

#: The Bedrock actions that let a role run a model.
MODEL_INVOKE_ACTIONS = (
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:Converse",
    "bedrock:ConverseStream",
)


# --------------------------------------------------------------------------- #
# Authorities
# --------------------------------------------------------------------------- #


@functools.lru_cache(maxsize=1)
def _runtime_members() -> dict[str, frozenset[str]]:
    model = botocore.session.get_session().get_service_model("bedrock-runtime")
    return {
        method: frozenset(model.operation_model(op).input_shape.members)
        for method, op in RUNTIME_OPERATIONS.items()
    }


@functools.lru_cache(maxsize=1)
def runtime_guardrail_keyword() -> dict[str, str]:
    """The member that carries the guardrail for each operation, from botocore.

    ``Converse``/``ConverseStream`` take a ``guardrailConfig`` structure;
    ``InvokeModel*`` take ``guardrailIdentifier`` (with ``guardrailVersion``) at the
    top level. The member is chosen as the one whose shape is the guardrail
    structure, or the identifier string where there is no structure.
    """
    model = botocore.session.get_session().get_service_model("bedrock-runtime")
    found: dict[str, str] = {}
    for method, op in RUNTIME_OPERATIONS.items():
        members = model.operation_model(op).input_shape.members
        structured = [
            name
            for name, shape in members.items()
            if shape.type_name == "structure" and "guardrailIdentifier" in shape.members
        ]
        found[method] = structured[0] if structured else "guardrailIdentifier"
        assert found[method] in members, (method, found[method])
    return found


@functools.lru_cache(maxsize=1)
def route_only_keywords() -> frozenset[str]:
    """Parameters of ``BedrockClient.invoke_model`` no raw operation accepts.

    Read from the def's AST, minus every ``bedrock-runtime`` input member. A raw
    boto3 call carrying one of these raises ``ParamValidationError`` ("Unknown
    parameter in input"), and botocore client methods take no positional
    arguments at all, so a call with either cannot be a working raw call.
    """
    tree = ast.parse((REPO_ROOT / CLIENT_MODULE).read_text(encoding="utf-8"))
    params: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "BedrockClient":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "invoke_model":
                    args = item.args
                    params = {
                        a.arg for a in args.posonlyargs + args.args + args.kwonlyargs
                    } - {"self"}
    raw = frozenset().union(*_runtime_members().values())
    return frozenset(params - raw)


# --------------------------------------------------------------------------- #
# The universe of model calls
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Site:
    path: str
    qualname: str
    callee: str
    lineno: int
    shape: str  # "call", "reference" or "getattr"
    node: ast.AST
    function: ast.AST | None
    #: The receiver is a name an import bound to idp_common.bedrock.
    via_route_module: bool = False
    #: Splat helpers this file imports from their real module.
    helpers: frozenset[str] = frozenset()

    @property
    def key(self) -> str:
        return f"{self.path}::{self.qualname}::{self.callee}"

    def __str__(self) -> str:
        return (
            f"{self.path}:{self.lineno} {self.callee} ({self.shape}) in {self.qualname}"
        )


def _is_production(path: str) -> bool:
    parts = Path(path).parts
    name = parts[-1]
    return not ("tests" in parts or name.startswith("test_") or name == "conftest.py")


@functools.lru_cache(maxsize=1)
def production_files() -> tuple[str, ...]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *UNIVERSE_PATHSPECS],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return tuple(sorted(p for p in out.split("\0") if p and _is_production(p)))


def _bedrock_model_aliases(tree: ast.AST) -> set[str]:
    names = {"BedrockModel"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "BedrockModel":
                    names.add(alias.asname or alias.name)
    return names


#: The module whose ``invoke_model`` is ``BedrockClient``'s (``invoke_model =
#: default_client`` at the bottom of ``bedrock/client.py``, re-exported).
ROUTE_MODULES = frozenset({"idp_common.bedrock", "idp_common.bedrock.client"})


def _route_module_aliases(tree: ast.AST) -> set[str]:
    """Names bound to a module in ROUTE_MODULES by an import statement."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                if f"{node.module}.{alias.name}" in ROUTE_MODULES:
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ROUTE_MODULES and alias.asname:
                    names.add(alias.asname)
    # A name rebound anywhere in the module (``bedrock = boto3.client(...)``) no
    # longer reliably means the module, so it is not trusted anywhere in it.
    rebound = {
        n.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))
    } | {
        a.arg
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        for a in n.args.posonlyargs + n.args.args + n.args.kwonlyargs
    }
    return names - rebound


#: The modules a splat helper must be imported from to count. A local def with
#: the same name proves nothing about what it returns.
HELPER_MODULES = frozenset(
    {
        "idp_common.bedrock.guardrail",
        "idp_common.bedrock.client",
        "idp_common.agents.common.strands_bedrock_model",
    }
)


def _imported_helpers(tree: ast.AST) -> set[str]:
    """Splat-helper names an import binds from HELPER_MODULES and nothing rebinds."""
    wanted = set(GUARDRAIL_SPLAT_HELPERS.values())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in HELPER_MODULES:
            for alias in node.names:
                if alias.name in wanted and alias.asname in (None, alias.name):
                    names.add(alias.name)
    rebound = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    } | {
        n.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))
    }
    return names - rebound


def _annotation_nodes(tree: ast.AST) -> set[int]:
    roots: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                roots.append(node.returns)
            for a in (
                node.args.posonlyargs
                + node.args.args
                + node.args.kwonlyargs
                + [node.args.vararg, node.args.kwarg]
            ):
                if a is not None and a.annotation is not None:
                    roots.append(a.annotation)
        elif isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
    return {id(n) for root in roots for n in ast.walk(root)}


class _Collector(ast.NodeVisitor):
    def __init__(self, path: str, tree: ast.AST) -> None:
        self.path = path
        self.scope: list[str] = []
        self.functions: list[ast.AST] = []
        self.sites: list[Site] = []
        self.aliases = _bedrock_model_aliases(tree)
        self.route_modules = _route_module_aliases(tree)
        self.helpers = frozenset(_imported_helpers(tree))
        self.annotations = _annotation_nodes(tree)
        self.call_funcs: set[int] = set()

    def _qualname(self) -> str:
        return ".".join(self.scope) or "<module>"

    def _add(
        self, callee: str, node: ast.AST, shape: str, receiver: ast.AST | None = None
    ) -> None:
        via_route_module = (
            callee in RUNTIME_CALLEES
            and isinstance(receiver, ast.Name)
            and receiver.id in self.route_modules
        )
        self.sites.append(
            Site(
                self.path,
                self._qualname(),
                callee,
                getattr(node, "lineno", 0),
                shape,
                node,
                self.functions[-1] if self.functions else None,
                via_route_module,
                self.helpers,
            )
        )

    def _scoped(self, node, is_function: bool) -> None:
        self.scope.append(node.name)
        if is_function:
            self.functions.append(node)
        self.generic_visit(node)
        if is_function:
            self.functions.pop()
        self.scope.pop()

    def visit_ClassDef(self, node):
        self._scoped(node, False)

    def visit_FunctionDef(self, node):
        self._scoped(node, True)

    def visit_AsyncFunctionDef(self, node):
        self._scoped(node, True)

    def visit_Call(self, node):
        func = node.func
        self.call_funcs.add(id(func))
        if isinstance(func, ast.Attribute) and func.attr in CALLEES:
            self._add(func.attr, node, "call", func.value)
        elif isinstance(func, ast.Name) and func.id in self.aliases:
            self._add("BedrockModel", node, "call")
        elif (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in CALLEES
        ):
            self._add(str(node.args[1].value), node, "getattr")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if (
            node.attr in CALLEES
            and isinstance(node.ctx, ast.Load)
            and id(node) not in self.call_funcs
            and id(node) not in self.annotations
        ):
            self._add(node.attr, node, "reference", node.value)
        self.generic_visit(node)

    def visit_Name(self, node):
        if (
            node.id in self.aliases
            and isinstance(node.ctx, ast.Load)
            and id(node) not in self.call_funcs
            and id(node) not in self.annotations
        ):
            self._add("BedrockModel", node, "reference")


def collect_sites(path: str, source: str) -> list[Site]:
    tree = ast.parse(source)
    collector = _Collector(path, tree)
    # visit_Call registers its func before generic_visit reaches it, so a call's
    # own callee is never also counted as a bare reference.
    collector.visit(tree)
    return collector.sites


@functools.lru_cache(maxsize=1)
def all_sites() -> tuple[Site, ...]:
    sites: list[Site] = []
    for path in production_files():
        sites.extend(
            collect_sites(path, (REPO_ROOT / path).read_text(encoding="utf-8"))
        )
    return tuple(sites)


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #


def _splat_helper_calls(call: ast.Call) -> set[str]:
    names = set()
    for kw in call.keywords:
        if kw.arg is None and isinstance(kw.value, ast.Call):
            f = kw.value.func
            names.add(
                f.id
                if isinstance(f, ast.Name)
                else f.attr
                if isinstance(f, ast.Attribute)
                else ""
            )
    return names


def _splat_names(call: ast.Call) -> set[str]:
    return {
        kw.value.id
        for kw in call.keywords
        if kw.arg is None and isinstance(kw.value, ast.Name)
    }


def _function_stores_key(
    function: ast.AST | None, name: str, key: str, before_line: int
) -> bool:
    """Whether ``function`` sets ``name[key]``, or binds ``name`` to a dict with
    ``key``, on a line before the call (a store after it cannot reach it)."""
    if function is None:
        return False
    for node in ast.walk(function):
        if getattr(node, "lineno", before_line) >= before_line:
            continue
        targets: list[ast.expr] = []
        value = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        for target in targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == name
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == key
            ):
                return True
            if (
                isinstance(target, ast.Name)
                and target.id == name
                and isinstance(value, ast.Dict)
                and any(
                    isinstance(k, ast.Constant) and k.value == key for k in value.keys
                )
            ):
                return True
    return False


def is_route_call(site: Site) -> bool:
    if site.via_route_module:
        return True
    call = site.node
    if site.shape != "call" or site.callee not in RUNTIME_CALLEES:
        return False
    assert isinstance(call, ast.Call)
    # A positional argument proves a non-boto3 callee; ``*args`` does not, since
    # ``client.converse(*args, **kwargs)`` with empty ``args`` is a working raw call.
    if any(not isinstance(a, ast.Starred) for a in call.args):
        return True
    return any(kw.arg in route_only_keywords() for kw in call.keywords)


def is_inside_route(site: Site) -> bool:
    return any(
        f"{site.path}::{site.qualname}" == route
        or f"{site.path}::{site.qualname}".startswith(route + ".")
        for route in GUARDRAIL_ROUTES
    )


def passes_guardrail(site: Site) -> bool:
    if site.shape != "call":
        return False
    call = site.node
    assert isinstance(call, ast.Call)
    # ``guardrailConfig=None`` names the guardrail and sends none.
    keywords = {
        kw.arg
        for kw in call.keywords
        if kw.arg
        and not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
    }
    helper = GUARDRAIL_SPLAT_HELPERS.get(site.callee)
    if helper and helper in site.helpers and helper in _splat_helper_calls(call):
        return True
    if site.callee == "BedrockModel":
        return STRANDS_GUARDRAIL_KEYWORD in keywords
    key = runtime_guardrail_keyword()[site.callee]
    if key in keywords:
        return True
    return any(
        _function_stores_key(site.function, name, key, site.lineno)
        for name in _splat_names(call)
    )


def verdict(site: Site) -> str:
    if is_inside_route(site):
        return "inside-route"
    if is_route_call(site):
        return "route-call"
    if passes_guardrail(site):
        return "passes-guardrail"
    if site.key in MODEL_CALL_GUARDRAIL_EXEMPT:
        return "exempt"
    return "unaccounted"


# --------------------------------------------------------------------------- #
# Code tests
# --------------------------------------------------------------------------- #


def test_the_authorities_are_non_empty():
    """An empty derivation would collect as a pass, so each is asserted."""
    assert route_only_keywords() >= {"model_id", "system_prompt", "content"}
    assert runtime_guardrail_keyword() == {
        "converse": "guardrailConfig",
        "converse_stream": "guardrailConfig",
        "invoke_model": "guardrailIdentifier",
        "invoke_model_with_response_stream": "guardrailIdentifier",
    }
    assert len(production_files()) > 100


def test_the_strands_keyword_is_what_bedrock_model_accepts():
    import typing

    strands_bedrock = pytest.importorskip("strands.models.bedrock")
    config = typing.get_type_hints(strands_bedrock.BedrockModel.BedrockConfig)
    assert STRANDS_GUARDRAIL_KEYWORD in config


def test_every_model_call_is_accounted_for():
    sites = all_sites()
    verdicts = Counter(verdict(s) for s in sites)
    # Each route the gate relies on is exercised by at least one real site, so a
    # verdict class cannot be silently empty.
    for kind in ("inside-route", "route-call", "passes-guardrail", "exempt"):
        assert verdicts[kind] > 0, (kind, verdicts)

    unaccounted = [str(s) for s in sites if verdict(s) == "unaccounted"]
    assert not unaccounted, (
        "These Bedrock model calls neither go through BedrockClient or the Strands "
        "factory, nor attach the guardrail from GUARDRAIL_ID_AND_VERSION, nor are "
        "listed in MODEL_CALL_GUARDRAIL_EXEMPT. Attach it with "
        "idp_common.bedrock.guardrail (guardrail_config_from_env for converse, "
        "guardrail_invoke_model_params for invoke_model) or "
        "strands_guardrail_kwargs for a BedrockModel, or add an entry with this "
        "site's own reason:\n  " + "\n  ".join(unaccounted)
    )


def test_the_sites_this_change_fixed_pass_the_guardrail():
    """Pin the three call sites that used to omit it, by verdict rather than count."""
    expected = {
        "lib/idp_common_pkg/idp_common/extraction/agentic_idp.py": "BedrockModel",
        "src/lambda/chat_with_document_processor/index.py": "converse_stream",
        "src/lambda/chat_stream_processor/vendored/chat_with_document_processor.py": (
            "converse_stream"
        ),
        "lib/idp_common_pkg/idp_common/rule_validation/z3/rule_translator.py": (
            "invoke_model"
        ),
    }
    for path, callee in expected.items():
        found = [s for s in all_sites() if s.path == path and s.callee == callee]
        assert found, f"no {callee} site found in {path}"
        assert {verdict(s) for s in found} == {"passes-guardrail"}, [
            (str(s), verdict(s)) for s in found
        ]


def test_exemptions_are_live_and_count_pinned():
    """A stale entry fails, and so does a new site inside an exempt key."""
    by_key = Counter(
        s.key
        for s in all_sites()
        if not is_inside_route(s) and not is_route_call(s) and not passes_guardrail(s)
    )
    problems = []
    for key, entry in MODEL_CALL_GUARDRAIL_EXEMPT.items():
        if by_key[key] != entry["sites"]:
            problems.append(
                f"{key}: shields {by_key[key]} site(s), pinned at {entry['sites']}"
            )
    assert not problems, (
        "MODEL_CALL_GUARDRAIL_EXEMPT is stale or a new site appeared under an "
        "existing entry:\n  " + "\n  ".join(problems)
    )


def test_each_exemption_has_its_own_reason():
    reasons = [e["reason"] for e in MODEL_CALL_GUARDRAIL_EXEMPT.values()] + list(
        GUARDRAIL_ENV_EXEMPT.values()
    )
    assert all(isinstance(r, str) and len(r) > 60 for r in reasons)
    assert len(set(reasons)) == len(reasons), "one reason attached to several members"


def _defs_named(names: frozenset[str]) -> list[tuple[str, str, ast.AST]]:
    found = []
    for path in production_files():
        tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))
        stack: list[str] = []

        def walk(node, stack=stack, path=path):
            for child in ast.iter_child_nodes(node):
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    stack.append(child.name)
                    if not isinstance(child, ast.ClassDef) and child.name in names:
                        found.append((path, ".".join(stack), child))
                    walk(child)
                    stack.pop()
                else:
                    walk(child)

        walk(tree)
    return found


def test_callee_named_defs_are_routes():
    """A route call can only land in ``BedrockClient``, within this tree.

    A def named like a ``bedrock-runtime`` method that takes named parameters
    could receive a call this gate reads as a ``BedrockClient`` call. Each must be
    a route entry point; a pure ``**kwargs`` pass-through forwards raw arguments
    and is judged at its caller instead.
    """
    defs = _defs_named(RUNTIME_CALLEES)
    assert any(f"{p}::{q}" in ROUTE_ENTRY_POINTS for p, q, _ in defs)
    stray = []
    for path, qualname, node in defs:
        args = node.args
        named = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
        named = [a for a in named if a not in ("self", "cls")]
        if (named or args.vararg) and f"{path}::{qualname}" not in ROUTE_ENTRY_POINTS:
            stray.append(f"{path}::{qualname}({', '.join(named)})")
    assert not stray, stray


def test_routes_contain_a_model_call_and_have_their_test():
    route_test_names = {
        n.name
        for n in ast.walk(
            ast.parse((REPO_ROOT / ROUTE_TESTS).read_text(encoding="utf-8"))
        )
        if isinstance(n, ast.FunctionDef)
    }
    for route, test_name in GUARDRAIL_ROUTES.items():
        inside = [
            s
            for s in all_sites()
            if is_inside_route(s) and route in f"{s.path}::{s.qualname}"
        ]
        assert inside, f"{route} holds no model call; the entry is stale"
        assert test_name in route_test_names, f"{test_name} missing from {ROUTE_TESTS}"


# --------------------------------------------------------------------------- #
# Collector self-checks: each shape the rule claims to read is read
# --------------------------------------------------------------------------- #

_PROBES = {
    "raw converse": ("c.converse(modelId='m', messages=[])", "unaccounted"),
    "aliased method": ("f = c.converse\nf(modelId='m')", "unaccounted"),
    "getattr": ("getattr(c, 'converse_stream')(modelId='m')", "unaccounted"),
    "aliased import": (
        "from strands.models import BedrockModel as BM\nBM(model_id='m')",
        "unaccounted",
    ),
    "module attribute": (
        "import strands.models as sm\nsm.BedrockModel(model_id='m')",
        "unaccounted",
    ),
    "raw invoke_model": ("c.invoke_model(modelId='m', body='{}')", "unaccounted"),
    "guardrail kwarg": (
        "c.converse(modelId='m', guardrailConfig=g)",
        "passes-guardrail",
    ),
    "guardrail in splatted dict": (
        "def f(c, g):\n    kw = {'modelId': 'm'}\n    kw['guardrailConfig'] = g\n"
        "    c.converse_stream(**kw)",
        "passes-guardrail",
    ),
    "splat without guardrail": (
        "def f(c):\n    kw = {'modelId': 'm'}\n    c.converse_stream(**kw)",
        "unaccounted",
    ),
    "invoke_model helper splat": (
        "from idp_common.bedrock.guardrail import guardrail_invoke_model_params\n"
        "c.invoke_model(modelId='m', **guardrail_invoke_model_params())",
        "passes-guardrail",
    ),
    "helper of the same name defined locally": (
        "def guardrail_invoke_model_params():\n    return {}\n"
        "c.invoke_model(modelId='m', **guardrail_invoke_model_params())",
        "unaccounted",
    ),
    "helper splat not imported": (
        "c.invoke_model(modelId='m', **guardrail_invoke_model_params())",
        "unaccounted",
    ),
    "starred pass-through": (
        "def f(c, *a, **k):\n    return c.converse(*a, **k)",
        "unaccounted",
    ),
    "guardrail keyword set to None": (
        "c.converse(modelId='m', guardrailConfig=None)",
        "unaccounted",
    ),
    "strands keyword set to None": (
        "BedrockModel(model_id='m', guardrail_id=None)",
        "unaccounted",
    ),
    "guardrail stored after the call": (
        "def f(c, g):\n    kw = {'modelId': 'm'}\n    c.converse_stream(**kw)\n"
        "    kw['guardrailConfig'] = g",
        "unaccounted",
    ),
    "wrong helper for the API": (
        "from idp_common.bedrock.guardrail import guardrail_invoke_model_params\n"
        "c.converse(modelId='m', **guardrail_invoke_model_params())",
        "unaccounted",
    ),
    "strands helper splat": (
        "from idp_common.agents.common.strands_bedrock_model import "
        "strands_guardrail_kwargs\n"
        "BedrockModel(model_id='m', **strands_guardrail_kwargs())",
        "passes-guardrail",
    ),
    "strands keyword": (
        "BedrockModel(model_id='m', guardrail_id='g')",
        "passes-guardrail",
    ),
    "BedrockClient call": (
        "bedrock.invoke_model(model_id='m', system_prompt='s', content=[])",
        "route-call",
    ),
    "positional call": ("x.invoke_model('m', 's', [])", "route-call"),
    "route module reference": (
        "from idp_common import bedrock\nrun(bedrock.invoke_model, 'm')",
        "route-call",
    ),
    "route module name rebound": (
        "from idp_common import bedrock\nbedrock = boto3.client('bedrock-runtime')\n"
        "bedrock.invoke_model(modelId='m', body='{}')",
        "unaccounted",
    ),
    "route module name shadowed by a parameter": (
        "from idp_common import bedrock\ndef f(bedrock):\n"
        "    bedrock.invoke_model(modelId='m', body='{}')",
        "unaccounted",
    ),
    "other module reference": (
        "import boto3\nbedrock = boto3.client('bedrock-runtime')\n"
        "run(bedrock.invoke_model, 'm')",
        "unaccounted",
    ),
}


@pytest.mark.parametrize("label", sorted(_PROBES))
def test_the_collector_reads_each_shape(label):
    source, expected = _PROBES[label]
    sites = collect_sites("probe.py", source)
    assert sites, f"{label}: no site collected"
    assert {verdict(s) for s in sites} == {expected}, [
        (str(s), verdict(s)) for s in sites
    ]


def test_an_annotation_is_not_a_site():
    source = "def f(m: BedrockModel) -> BedrockModel:\n    return m"
    assert collect_sites("probe.py", source) == []


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #

LAMBDA_TYPES = ("AWS::Serverless::Function", "AWS::Lambda::Function")


@functools.lru_cache(maxsize=1)
def deployed_templates() -> tuple[str, ...]:
    seen = [gate_premises.PARENT_TEMPLATE]
    frontier = [gate_premises.PARENT_TEMPLATE]
    while frontier:
        parent = frontier.pop()
        for child in gate_premises.nested_stack_sources(parent).values():
            if child not in seen:
                seen.append(child)
                frontier.append(child)
    return tuple(seen)


def _statements(policy_doc) -> list:
    """Every statement-like dict in a policy tree, ``Fn::If`` branches included."""
    found = []

    def walk(o, conds: tuple[str, ...]):
        if isinstance(o, dict):
            if (
                "Fn::If" in o
                and isinstance(o["Fn::If"], list)
                and len(o["Fn::If"]) == 3
            ):
                cond, yes, no = o["Fn::If"]
                walk(yes, conds + (str(cond),))
                walk(no, conds + (f"!{cond}",))
                return
            if "Action" in o and "Effect" in o:
                found.append((o, conds))
            for v in o.values():
                walk(v, conds)
        elif isinstance(o, list):
            for v in o:
                walk(v, conds)

    walk(policy_doc, ())
    return found


def _actions(statement: dict) -> list[str]:
    action = statement.get("Action")
    return [
        a
        for a in (action if isinstance(action, list) else [action])
        if isinstance(a, str)
    ]


def _role_policy_trees(resources: dict, function: dict) -> list:
    props = function.get("Properties") or {}
    trees = [props.get("Policies")]
    role = props.get("Role")
    role_id = None
    if isinstance(role, dict):
        ref = role.get("Fn::GetAtt") or role.get("Fn::Ref")
        if isinstance(ref, str):
            role_id = ref.split(".")[0]
        elif isinstance(ref, list) and ref:
            role_id = ref[0]
    if role_id and role_id in resources:
        role_props = resources[role_id].get("Properties") or {}
        trees.append(role_props.get("Policies"))
        for arn in role_props.get("ManagedPolicyArns") or []:
            ref = arn.get("Fn::Ref") if isinstance(arn, dict) else None
            if isinstance(ref, str) and ref in resources:
                trees.append(
                    (resources[ref].get("Properties") or {}).get("PolicyDocument")
                )
        for body in resources.values():
            if not isinstance(body, dict) or body.get("Type") not in (
                "AWS::IAM::Policy",
                "AWS::IAM::ManagedPolicy",
            ):
                continue
            roles = (body.get("Properties") or {}).get("Roles") or []
            if {"Fn::Ref": role_id} in roles:
                trees.append((body.get("Properties") or {}).get("PolicyDocument"))
    return trees


@dataclass(frozen=True)
class LambdaGrant:
    key: str
    can_invoke: bool
    env_condition: str | None
    env_ok: bool
    apply_guardrail_conditions: tuple[tuple[str, ...], ...]


def _guardrail_env(env_value) -> tuple[str | None, bool]:
    """``(condition, well_formed)`` for a GUARDRAIL_ID_AND_VERSION value."""
    if not isinstance(env_value, dict) or "Fn::If" not in env_value:
        return None, False
    cond, yes, no = env_value["Fn::If"]
    sub = yes.get("Fn::Sub") if isinstance(yes, dict) else None
    ok = sub == "${BedrockGuardrailId}:${BedrockGuardrailVersion}" and no == ""
    return str(cond), ok


@functools.lru_cache(maxsize=1)
def lambda_grants() -> tuple[LambdaGrant, ...]:
    grants = []
    for template in deployed_templates():
        resources = gate_premises.load_template(template).get("Resources") or {}
        for logical_id, body in resources.items():
            if not isinstance(body, dict) or body.get("Type") not in LAMBDA_TYPES:
                continue
            statements = [
                st
                for tree in _role_policy_trees(resources, body)
                for st in _statements(tree)
            ]
            can_invoke = any(
                st.get("Effect") == "Allow"
                and any(
                    fnmatch.fnmatchcase(action, pattern)
                    for pattern in _actions(st)
                    for action in MODEL_INVOKE_ACTIONS
                )
                for st, _conds in statements
            )
            env = ((body.get("Properties") or {}).get("Environment") or {}).get(
                "Variables"
            ) or {}
            cond, ok = (None, False)
            if "GUARDRAIL_ID_AND_VERSION" in env:
                cond, ok = _guardrail_env(env["GUARDRAIL_ID_AND_VERSION"])
            apply_conds = tuple(
                conds
                for st, conds in statements
                if "bedrock:ApplyGuardrail" in _actions(st)
                and "guardrail/${BedrockGuardrailId}" in str(st.get("Resource"))
            )
            grants.append(
                LambdaGrant(
                    f"{template}::{logical_id}",
                    can_invoke,
                    cond if "GUARDRAIL_ID_AND_VERSION" in env else None,
                    ok,
                    apply_conds,
                )
            )
    return tuple(grants)


def test_the_template_universe_is_non_empty():
    templates = deployed_templates()
    assert "patterns/unified/template.yaml" in templates
    assert "nested/multi-doc-discovery/template.yaml" in templates
    invokers = [g for g in lambda_grants() if g.can_invoke]
    assert len(invokers) > len(GUARDRAIL_ENV_EXEMPT)


def test_every_model_invoking_lambda_gets_the_guardrail():
    missing = []
    for g in lambda_grants():
        if not g.can_invoke or g.key in GUARDRAIL_ENV_EXEMPT:
            continue
        if g.env_condition is None:
            missing.append(f"{g.key}: GUARDRAIL_ID_AND_VERSION not set")
        elif not g.env_ok:
            missing.append(
                f"{g.key}: GUARDRAIL_ID_AND_VERSION is not "
                '!If [<cond>, !Sub "${BedrockGuardrailId}:${BedrockGuardrailVersion}", ""]'
            )
    assert not missing, (
        "These Lambdas can invoke a Bedrock model but are not given the configured "
        "guardrail. Set the variable as the other functions do, or add an entry "
        "to GUARDRAIL_ENV_EXEMPT with this function's own reason:\n  "
        + "\n  ".join(missing)
    )


def test_a_lambda_given_the_guardrail_may_apply_it():
    """Converse with ``guardrailConfig`` needs ``bedrock:ApplyGuardrail``.

    Without it every call fails AccessDenied on exactly the stacks that configured
    a guardrail, so the grant must sit under the same condition as the variable.
    """
    problems = []
    for g in lambda_grants():
        if g.env_condition is None:
            continue
        if not any(g.env_condition in conds for conds in g.apply_guardrail_conditions):
            problems.append(
                f"{g.key}: no bedrock:ApplyGuardrail on guardrail/${{BedrockGuardrailId}} "
                f"under condition {g.env_condition}"
            )
    assert not problems, "\n".join(problems)


def test_env_exemptions_are_live():
    grants = {g.key: g for g in lambda_grants()}
    stale = []
    for key in GUARDRAIL_ENV_EXEMPT:
        g = grants.get(key)
        if g is None:
            stale.append(f"{key}: no such Lambda in the deployed templates")
        elif not g.can_invoke:
            stale.append(f"{key}: holds no model-invoke grant")
        elif g.env_condition is not None:
            stale.append(f"{key}: now sets GUARDRAIL_ID_AND_VERSION")
    assert not stale, "GUARDRAIL_ENV_EXEMPT is stale:\n  " + "\n  ".join(stale)


def test_nested_stacks_receive_the_guardrail_parameters():
    """A nested template that reads the guardrail is passed it by its parent."""
    problems = []
    for parent in deployed_templates():
        resources = gate_premises.load_template(parent).get("Resources") or {}
        for logical_id, child in gate_premises.nested_stack_sources(parent).items():
            declared = gate_premises.load_template(child).get("Parameters") or {}
            needed = {"BedrockGuardrailId", "BedrockGuardrailVersion"} & set(declared)
            passed = set(
                (
                    (resources[logical_id].get("Properties") or {}).get("Parameters")
                    or {}
                )
            )
            if needed - passed:
                problems.append(
                    f"{parent}::{logical_id} does not pass {sorted(needed - passed)}"
                )
    assert not problems, problems
