# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which deployed Lambdas save a configuration profile, derived from source (#1778).

A profile save cuts a revision only when the process has ``CONFIGURATION_BUCKET``;
without it the save rewrites the profile head while every new document stays pinned
to the previously published revision. Whether a Lambda needs the bucket is therefore
a property of its code, so the set is computed here rather than listed.

Two passes, both over ``ast`` and neither importing anything:

1. **Library pass.** Inside ``lib/idp_common_pkg/idp_common``, a definition is a
   *writer* if it reaches a call to one of :data:`SEED_METHODS` that does not pass
   ``cut_revision=False`` (an internal rewrite that deliberately cuts nothing) and
   does not visibly save a non-profile record (``CONFIG_TYPE_CUSTOM_PRICING``).
   Calls are resolved class-qualified, never by bare name: ``self.m()`` is the
   enclosing class's ``m``; ``f()`` is the module's or an imported ``f``; ``C()`` is
   ``C.__init__``; ``x.m()`` and ``self.x.m()`` use the class ``x`` was assigned
   from (``x = C(...)``, ``self.x = given or C(...)``), falling back to the classes
   the body names. Resolving by bare name was measured first and pulled in
   ``__init__``, ``get_config`` and ``__call__`` through unrelated same-named calls.

   Two rules cover dispatch that is not a call. A writer **referenced as a value**
   (a Strands tool list, a ``creator_func=``) makes the referencing body a writer.
   A module-level object a writer is **registered into** at import time
   (``agent_factory.register_agent(creator_func=create_quick_start_agent)``) is a
   *carrier*, and a ``create*`` call on a carrier reaches the writer: that is how
   the Quick Start agent's ``create_config_version`` tool reaches the two chat
   processors and the agent processor.
2. **Lambda pass.** A Lambda's own files are resolved the same way against the
   library's writers, plus any direct seed call regardless of receiver (a handler
   that builds its manager through a helper still reaches the seed). An image that
   bundles several handlers from one directory is read from the handler its
   ``ImageConfig.Command`` names and the sibling modules that imports
   (:func:`entry_closure`), since only that handler runs in the function.

A save inside a ``try`` whose ``RevisionNotRecordedError`` handler only logs is
**refusable** (:func:`_refusable_calls`) and counts as no save at all: without the
bucket it is refused before writing, so it cannot cause a divergence. That is the
read-path write-back in ``get_merged_configuration`` (#1782), which every unpinned
reader runs.

What it does not see: a call through ``getattr``, a registry filled inside a
function rather than at import time, or a writer reached through a third-party
module. Each would make the derived set smaller than the true one, which the floor
in the test (``KNOWN_WRITERS`` there) is what catches; a carrier ``create*`` call
that creates something other than the registered writer would make it larger,
which costs a grant rather than a missed save.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
LIBRARY = REPO_ROOT / "lib/idp_common_pkg/idp_common"
LIBRARY_PACKAGE = "idp_common"

#: The ``ConfigurationManager`` methods that write a profile body and cut its revision.
SEED_METHODS = frozenset(
    {"save_configuration", "save_raw_configuration", "restore_revision"}
)

Symbol = Tuple[str, str]  # (dotted module, qualified name: "f" or "Class.method")


#: The refusal a save raises when it cannot record a profile's revision.
REFUSAL = "RevisionNotRecordedError"


def _only_logs(body: List[ast.stmt]) -> bool:
    """A handler body that records the refusal and carries on: logging, `pass`."""
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            continue
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute)
            and isinstance(stmt.value.func.value, ast.Name)
            and stmt.value.func.value.id == "logger"
        ):
            continue
        return False
    return True


def _refusable_calls(node: ast.AST) -> Set[int]:
    """Calls whose refusal is caught, logged and dropped: opportunistic saves.

    A save inside a `try` with a `RevisionNotRecordedError` handler that only logs
    is one the caller is content to lose. Without the bucket it is refused before
    it writes, so it cannot make a profile diverge, and the process that made it
    needs no bucket on its account. The write-back in `get_merged_configuration`
    is the case this exists for (#1782): every unpinned reader runs it, and
    counting it would make every reader a writer. A handler that re-raises,
    returns an error or does anything else besides logging is not refusable,
    because then the save is one the caller depends on.
    """
    out: Set[int] = set()
    for try_node in ast.walk(node):
        if not isinstance(try_node, ast.Try):
            continue
        for handler in try_node.handlers:
            caught = handler.type
            names = caught.elts if isinstance(caught, ast.Tuple) else [caught]
            if any(
                (isinstance(n, ast.Name) and n.id == REFUSAL)
                or (isinstance(n, ast.Attribute) and n.attr == REFUSAL)
                for n in names
            ) and _only_logs(handler.body):
                for stmt in try_node.body:
                    out.update(id(c) for c in ast.walk(stmt) if isinstance(c, ast.Call))
    return out


def _is_seed_call(node: ast.Call) -> bool:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else None
    if name not in SEED_METHODS:
        return False
    for kw in node.keywords:
        if (
            kw.arg == "cut_revision"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is False
        ):
            return False
    # `save_configuration(CONFIG_TYPE_CUSTOM_PRICING, ...)` saves a non-profile
    # record, which never cuts a revision. Only a constant that visibly names
    # another type is excluded; anything else (a variable, "Config") counts.
    if name == "save_configuration" and node.args:
        first = node.args[0]
        spelled = (
            first.id
            if isinstance(first, ast.Name)
            else (first.attr if isinstance(first, ast.Attribute) else None)
        )
        if (
            spelled
            and spelled.startswith("CONFIG_TYPE_")
            and spelled != ("CONFIG_TYPE_CONFIG")
        ):
            return False
    return True


@dataclass
class _Scope:
    """One body to resolve: a function, or a whole Lambda source file."""

    module: str
    cls: Optional[str]
    node: ast.AST
    imports: Dict[str, Symbol] = field(default_factory=dict)


def _module_name(path: Path) -> str:
    rel = path.relative_to(LIBRARY.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_from(module: str, level: int, target: Optional[str], is_pkg: bool) -> str:
    if level == 0:
        return target or ""
    base = module.split(".")
    if not is_pkg:
        base = base[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)]
    return ".".join(base + ([target] if target else []))


def _imports_of(tree: ast.AST, module: str, is_pkg: bool) -> Dict[str, Symbol]:
    """Local name -> (module, name) for every ``from X import Y`` into idp_common."""
    out: Dict[str, Symbol] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            source = _resolve_from(module, node.level, node.module, is_pkg)
            if source != LIBRARY_PACKAGE and not source.startswith(
                LIBRARY_PACKAGE + "."
            ):
                continue
            for alias in node.names:
                out[alias.asname or alias.name] = (source, alias.name)
    return out


def _target_key(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in ("self", "cls")
    ):
        return f"self.{node.attr}"
    return None


def _constructed(value: ast.AST, local_classes: Dict[str, Symbol]) -> Optional[Symbol]:
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        return local_classes.get(value.func.id)
    if isinstance(value, ast.BoolOp):
        for operand in value.values:
            found = _constructed(operand, local_classes)
            if found:
                return found
    if isinstance(value, ast.IfExp):
        return _constructed(value.body, local_classes) or _constructed(
            value.orelse, local_classes
        )
    return None


def _assigned_types(
    node: ast.AST, local_classes: Dict[str, Symbol]
) -> Dict[str, Symbol]:
    out: Dict[str, Symbol] = {}
    for n in ast.walk(node):
        targets = []
        if isinstance(n, ast.Assign):
            targets, value = n.targets, n.value
        elif isinstance(n, ast.AnnAssign) and n.value is not None:
            targets, value = [n.target], n.value
        else:
            continue
        sym = _constructed(value, local_classes)
        if not sym:
            continue
        for target in targets:
            key = _target_key(target)
            if key:
                out.setdefault(key, sym)
    return out


class _Library:
    def __init__(self, root: Path = LIBRARY):
        self.defs: Dict[Symbol, _Scope] = {}
        self.classes: Dict[str, Set[str]] = {}  # module -> class names defined there
        self.reexports: Dict[Symbol, Symbol] = {}
        # Module-level statements, for registries filled at import time.
        self.module_level: Dict[str, Tuple[List[ast.stmt], Dict[str, Symbol]]] = {}
        for path in sorted(root.rglob("*.py")):
            if "tests" in path.parts:
                continue
            module = _module_name(path)
            is_pkg = path.name == "__init__.py"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = _imports_of(tree, module, is_pkg)
            for local, target in imports.items():
                self.reexports[(module, local)] = target
            self.classes[module] = set()
            self.module_level[module] = (
                [
                    n
                    for n in tree.body
                    if not isinstance(
                        n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    )
                ],
                imports,
            )
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.defs[(module, node.name)] = _Scope(module, None, node, imports)
                elif isinstance(node, ast.ClassDef):
                    self.classes[module].add(node.name)
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            self.defs[(module, f"{node.name}.{item.name}")] = _Scope(
                                module, node.name, item, imports
                            )
        self.writers: Set[Symbol] = set()
        #: Module-level objects a writer is registered into at import time, e.g.
        #: ``agent_factory.register_agent(creator_func=create_quick_start_agent)``.
        self.carriers: Set[Symbol] = set()
        self._fixed_point()

    def canonical(self, symbol: Symbol, _seen: int = 0) -> Symbol:
        """Follow ``from .x import Y`` re-exports to where ``Y`` is defined."""
        module, name = symbol
        head = name.split(".", 1)
        while _seen < 20 and (module, head[0]) in self.reexports:
            module, head[0] = self.reexports[(module, head[0])]
            _seen += 1
        if (module, head[0]) not in self.defs and head[0] not in self.classes.get(
            module, set()
        ):
            # `from idp_common.pkg import mod` names a submodule, not a symbol.
            sub = f"{module}.{head[0]}"
            if sub in self.classes and len(head) > 1:
                return self.canonical((sub, head[1]))
        return (module, ".".join(head))

    def reaches_writer(self, scope: _Scope) -> bool:
        local_classes = {
            local: self.canonical(target) for local, target in scope.imports.items()
        }
        if scope.module in self.classes:
            for c in self.classes[scope.module]:
                local_classes.setdefault(c, (scope.module, c))
        named = {n.id for n in ast.walk(scope.node) if isinstance(n, ast.Name)}
        # `x = C(...)`, `self.x = C(...)`, `x = given or C(...)`: what `x` holds.
        typed = dict(self._attribute_types(scope, local_classes))
        typed.update(_assigned_types(scope.node, local_classes))
        refusable = _refusable_calls(scope.node)
        for node in ast.walk(scope.node):
            # A writer passed as a value (a tool list, a creator_func) is reached
            # by whoever calls what it is passed to.
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                sym = local_classes.get(node.id, (scope.module, node.id))
                if self.canonical(sym) in self.writers:
                    return True
            if not isinstance(node, ast.Call) or id(node) in refusable:
                continue
            if _is_seed_call(node):
                return True
            func = node.func
            candidates: Iterable[Symbol] = ()
            if isinstance(func, ast.Name) and func.id in local_classes:
                module, name = local_classes[func.id]
                candidates = [(module, name), (module, f"{name}.__init__")]
            elif isinstance(func, ast.Name):
                candidates = [(scope.module, func.id)]
            elif isinstance(func, ast.Attribute):
                receiver = func.value
                if (
                    isinstance(receiver, ast.Name)
                    and receiver.id in ("self", "cls")
                    and scope.cls
                ):
                    candidates = [(scope.module, f"{scope.cls}.{func.attr}")]
                elif _target_key(receiver) in typed:
                    module, name = typed[_target_key(receiver)]
                    candidates = [(module, f"{name}.{func.attr}")]
                elif (
                    isinstance(receiver, ast.Name)
                    and func.attr.startswith("create")
                    and self.canonical(
                        local_classes.get(receiver.id, (scope.module, receiver.id))
                    )
                    in self.carriers
                ):
                    return True
                elif isinstance(receiver, ast.Name) and receiver.id in local_classes:
                    module, name = local_classes[receiver.id]
                    # `mod.f()` on an imported submodule, or `Cls.m()` on a class.
                    candidates = [
                        (f"{module}.{name}", func.attr),
                        (module, f"{name}.{func.attr}"),
                    ]
                else:
                    candidates = [
                        (module, f"{name}.{func.attr}")
                        for local, (module, name) in local_classes.items()
                        if local in named
                    ]
            if any(self.canonical(c) in self.writers for c in candidates):
                return True
        return False

    def _attribute_types(
        self, scope: _Scope, local_classes: Dict[str, Symbol]
    ) -> Dict[str, Symbol]:
        """``self.x`` types assigned anywhere in the enclosing class."""
        if not scope.cls:
            return {}
        out: Dict[str, Symbol] = {}
        for (module, qual), other in self.defs.items():
            if module == scope.module and qual.startswith(f"{scope.cls}."):
                for key, sym in _assigned_types(other.node, local_classes).items():
                    if key.startswith("self."):
                        out.setdefault(key, sym)
        return out

    def _fixed_point(self) -> None:
        changed = True
        while changed:
            changed = False
            for symbol, scope in self.defs.items():
                if symbol not in self.writers and self.reaches_writer(scope):
                    self.writers.add(symbol)
                    changed = True
            for module, (statements, imports) in self.module_level.items():
                for stmt in statements:
                    call = stmt.value if isinstance(stmt, ast.Expr) else None
                    if not (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name)
                    ):
                        continue
                    carrier = (module, call.func.value.id)
                    if carrier in self.carriers:
                        continue
                    if self.reaches_writer(_Scope(module, None, call, imports)):
                        self.carriers.add(carrier)
                        changed = True


_LIBRARY_CACHE: Dict[Path, _Library] = {}


def library(root: Path = LIBRARY) -> _Library:
    if root not in _LIBRARY_CACHE:
        _LIBRARY_CACHE[root] = _Library(root)
    return _LIBRARY_CACHE[root]


def _excluded(path: Path, root: Path) -> bool:
    rel = path.relative_to(root).parts
    return any(p in ("tests", "test", "vendor", "idp_common_pkg") for p in rel) or (
        path.name.startswith("test_") or path.name == "conftest.py"
    )


def lambda_files(source_dir: Path) -> List[Path]:
    """Every non-test ``.py`` file of a Lambda packaged from ``source_dir``."""
    return [p for p in sorted(source_dir.rglob("*.py")) if not _excluded(p, source_dir)]


def entry_closure(source_dir: Path, entry_module: str) -> List[Path]:
    """``entry_module`` plus the sibling modules it imports, transitively.

    For an image that bundles several handlers from one directory, only the
    handler a function's ``ImageConfig.Command`` names runs in that function.
    """
    seen: List[Path] = []
    pending = [entry_module]
    while pending:
        path = source_dir / f"{pending.pop()}.py"
        if not path.is_file() or path in seen:
            continue
        seen.append(path)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                pending.extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                pending.append(node.module.split(".")[0])
    return seen


def files_save_a_profile(files: Iterable[Path], lib: Optional[_Library] = None) -> bool:
    """True if any of ``files`` reaches a profile save."""
    lib = lib or library()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scope = _Scope("<lambda>", None, tree, _imports_of(tree, "<lambda>", False))
        if lib.reaches_writer(scope):
            return True
    return False
