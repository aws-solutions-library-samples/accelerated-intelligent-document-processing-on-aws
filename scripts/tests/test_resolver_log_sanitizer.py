# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The api-resolver Lambdas redact logs with the CANONICAL denylist, not a copy.

``lib/idp_common_pkg/idp_common/utils/log_sanitizer.py`` is the one redactor. Ten
resolvers under ``nested/api-resolvers/src/lambda/`` used to hand-copy a shortened
version of its key denylist into their own ``index.py``, and those copies drifted
eight keys behind the canonical list — nothing compared them, so nothing noticed.
A denylist that is duplicated by hand is a denylist that is eventually wrong, and
the failure is silent: the redactor still runs, still looks correct in review, and
just passes the newer key names straight through.

So the split is now mechanical, and derived here from the template rather than from
a list of names kept in a comment:

* A resolver that carries an ``IDPCommon*Layer`` imports
  ``idp_common.utils.log_sanitizer`` directly — there is nothing to copy.
* A resolver that carries no layer cannot import the library at runtime at all
  (SAM packages each function from its own ``CodeUri``, so it cannot reach a
  sibling directory either). Those get a **byte-identical** committed copy of the
  module as ``log_sanitizer.py``, kept in step by
  ``scripts/sync_resolver_log_sanitizer.sh``. The module is stdlib-only, so the
  copy costs a few KB where attaching the base layer — Pillow, pypdfium2,
  requests — would cost tens of MB on functions that need none of it.

This is the same guarded-vendoring shape as
``src/lambda/chat_stream_processor/vendored/`` and its
``test_vendored_in_sync.py``.

What each test below forbids:

* **logging the invocation event without sanitizing it** — see
  ``test_no_resolver_logs_the_raw_invocation_event``. This is the one that closes
  the actual defect class of #921; every other test here proves the *copies* are
  consistent, which is a different and weaker property. A resolver can pass all of
  them while writing ``identity.claims`` to CloudWatch in full, because it simply
  never mentions the sanitizer at all. Two resolvers did exactly that
  (``finetuning_jobs_resolver``, ``list_documents_range_resolver``) and the first
  round of these guards was green with both in the tree.
* reintroducing a hand-rolled key list anywhere under the resolver tree, under any
  variable name (the original copies were all called ``_LOG_SENSITIVE_KEYS``, but
  renaming one must not buy an exemption);
* a vendored copy drifting from the canonical module by even one byte;
* a resolver importing ``idp_common`` for the sanitizer while carrying no layer to
  provide it — that is an ImportError at cold start, not a lint nit;
* the sync script's target list drifting from the set of resolvers that actually
  import the vendored copy.

The canonical module is loaded **by path**, not via ``import idp_common``: an
editable install in the environment may resolve to a different checkout entirely,
which would make this test assert against someone else's file.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL = REPO_ROOT / "lib/idp_common_pkg/idp_common/utils/log_sanitizer.py"
RESOLVER_ROOT = REPO_ROOT / "nested/api-resolvers/src/lambda"
TEMPLATE = REPO_ROOT / "nested/api-resolvers/template.yaml"
# Most resolver functions are declared in the nested stack, but not all of them:
# FinetuningJobsResolverFunction lives in the PARENT template and points its
# CodeUri back into nested/api-resolvers/src/lambda. Scanning only the nested
# template left it looking layer-free when it in fact carries IDPCommonBaseLayer,
# so adding the canonical import there — the correct thing to do — would have
# failed this suite and told the author to vendor a copy instead.
TEMPLATES = (TEMPLATE, REPO_ROOT / "template.yaml")
SYNC_SCRIPT = REPO_ROOT / "scripts/sync_resolver_log_sanitizer.sh"

VENDORED_NAME = "log_sanitizer.py"
VENDORED_MODULE = "log_sanitizer"
CANONICAL_IMPORT = "idp_common.utils.log_sanitizer"

# The keys the hand-copies were missing. Asserted by name so the specific
# regression that motivated this guard cannot come back unnoticed, even if the
# canonical list is reorganized around it.
PREVIOUSLY_MISSING_KEYS = frozenset(
    {
        "access_key",
        "accesskey",
        "passwd",
        "private_key",
        "privatekey",
        "secret_key",
        "secretkey",
        "x-api-key",
    }
)

# A collection literal of strings this many canonical keys deep is a denylist copy,
# not a coincidence. Three is low enough to catch a partial copy and high enough
# that an unrelated tuple (e.g. ("token", "cursor")) does not trip it.
_DENYLIST_MATCH_THRESHOLD = 3


def _load_module_by_path(path: Path, name: str):
    """Import ``path`` as a standalone module, ignoring any installed copy."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_deny_keys() -> frozenset[str]:
    module = _load_module_by_path(CANONICAL, "_canonical_log_sanitizer")
    return frozenset(module._DEFAULT_DENY_KEY_SUBSTRINGS)


def _resolver_dirs() -> list[Path]:
    return sorted(p for p in RESOLVER_ROOT.iterdir() if p.is_dir())


def _index_source(resolver: Path) -> str:
    index = resolver / "index.py"
    return index.read_text(encoding="utf-8") if index.exists() else ""


def _layers_by_code_uri() -> dict[str, list[str]]:
    """Map resolver directory name to the IDPCommon layer refs its function declares.

    Parsed with regex rather than a YAML loader: the templates are full of short-form
    intrinsics (``!Ref``, ``!Sub``, ``!If``) that a plain ``yaml.safe_load`` refuses,
    and all this needs is which resource block names which layer. Resource blocks
    start at exactly two spaces of indentation.

    Three things this deliberately does NOT assume, each of which used to drop a
    resolver silently — and a resolver missing from this map reads as "carries no
    layer", the direction that produces wrong advice:

    * a leading ``./`` on the CodeUri (``ListDocumentsGSIResolverFunction`` omits it);
    * that the function is declared in the nested template (see ``TEMPLATES``);
    * that the layer parameter name ends in ``Arn`` — the nested stack receives
      ``IDPCommonBaseLayerArn`` as a parameter, while the parent declares the layer
      resource itself and refers to it as ``IDPCommonBaseLayer``.

    CodeUri is resolved to a real path and kept only when it lands directly inside
    ``RESOLVER_ROOT``, so the parent template's own ``src/lambda/<name>`` functions
    (relative to the repo root, a different tree) cannot collide with a resolver of
    the same name.
    """
    layers: dict[str, list[str]] = {}
    for template in TEMPLATES:
        lines = template.read_text(encoding="utf-8").splitlines()
        starts = [
            i for i, line in enumerate(lines) if re.match(r"^  [A-Za-z0-9]+:\s*$", line)
        ]
        starts.append(len(lines))
        for start, end in zip(starts, starts[1:]):
            block = "\n".join(lines[start:end])
            match = re.search(r"CodeUri:\s*(\S+)", block)
            if not match:
                continue
            code_dir = (template.parent / match.group(1)).resolve()
            if code_dir.parent != RESOLVER_ROOT:
                continue
            layers[code_dir.name] = re.findall(
                r"!Ref (IDPCommon\w*Layer(?:Arn)?)\b", block
            )
    return layers


def _imported_modules(resolver: Path) -> dict[str, set[str]]:
    """Map module -> names imported from it, for REAL imports in ``index.py``.

    AST-parsed, not substring-matched. A comment or docstring that quotes an import
    line is not an import, and the resolvers are full of exactly such prose: every
    vendored ``log_sanitizer.py`` carries the canonical module's ``Usage::`` block,
    whose body line reads ``from idp_common.utils.log_sanitizer import ...``. The
    substring form of this check passed only because this change happened to delete
    the old comment blocks that quoted the same path; re-adding one such comment to
    a layer-free resolver would have failed the suite for no real reason.

    On an ``index.py`` Python cannot parse, fall back to a line-anchored text match
    so an unreadable file cannot silently read as importing nothing.
    """
    source = _index_source(resolver)
    if not source:
        return {}
    try:
        tree = ast.parse(source, filename=str(resolver / "index.py"))
    except SyntaxError:
        found: dict[str, set[str]] = {}
        for module in (CANONICAL_IMPORT, VENDORED_MODULE):
            pattern = rf"^\s*(?:from {re.escape(module)} import|import {re.escape(module)}\b)"
            if re.search(pattern, source, re.M):
                found[module] = {"sanitize_event_for_logging"}
        return found
    imports: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                continue  # explicit relative import; neither of the two routes
            imports.setdefault(node.module or "", set()).update(
                alias.name for alias in node.names
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.setdefault(alias.name, set())
    return imports


def _imports_canonical_sanitizer(resolver: Path) -> bool:
    """The resolver imports the sanitizer from ``idp_common`` — so it needs a layer."""
    for module, names in _imported_modules(resolver).items():
        if module == CANONICAL_IMPORT or module.startswith(f"{CANONICAL_IMPORT}."):
            return True
        if module == "idp_common.utils" and VENDORED_MODULE in names:
            return True
    return False


def _imports_vendored_sanitizer(resolver: Path) -> bool:
    """The resolver imports its own committed copy as a top-level sibling module."""
    return VENDORED_MODULE in _imported_modules(resolver)


def _imports_the_sanitizer(resolver: Path) -> bool:
    return _imports_canonical_sanitizer(resolver) or _imports_vendored_sanitizer(
        resolver
    )


# Collection constructors that wrap a literal: `frozenset({...})`, `set([...])`,
# `tuple([...])`, `list((...))`. A denylist written this way is still a denylist,
# and the previous scan — which required the assigned value to be a bare literal —
# could not see one. `PREVIOUSLY_MISSING_KEYS` above is itself a `frozenset({...})`,
# so the scan was blind to precisely the shape its own module uses.
_LITERAL_WRAPPERS = frozenset({"frozenset", "set", "tuple", "list"})
_MAX_WRAPPER_DEPTH = 3


def _string_literals(node: ast.AST, depth: int = 0) -> list[str] | None:
    """Return the string members of a collection literal, else None.

    Recognises a tuple/list/set literal, the keys of a dict literal, and any of
    those wrapped in a single-argument collection constructor.
    """
    if isinstance(node, ast.Call) and depth < _MAX_WRAPPER_DEPTH:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in _LITERAL_WRAPPERS and len(node.args) == 1 and not node.keywords:
            return _string_literals(node.args[0], depth + 1)
        return None
    if isinstance(node, ast.Dict):
        elements: list[ast.expr] = [k for k in node.keys if k is not None]
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        elements = list(node.elts)
    else:
        return None
    values = []
    for element in elements:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        values.append(element.value)
    return values


# --- does the resolver actually sanitize what it logs? ---------------------------
#
# Everything above proves the copies are consistent with each other. None of it
# proves any resolver *uses* one. This does.
#
# The check is on the DATAFLOW, not on the spelling. It would have been much
# shorter to forbid the literal text `json.dumps(event)`, and that version is worse
# than useless: it pins the exact shape of the line that happened to be wrong this
# time, so the identical leak walks straight past as `f"...{event}..."`, as
# `logger.info("%s", event)`, or as `str(event)`. A scanner that only recognises the
# shape you already found is a scanner that can only catch the bug you already
# fixed.
#
# So: find every log call, then ask of each argument "can the whole invocation event
# object reach here?" — descending through f-strings, `%` formatting, nested calls,
# and containers alike, and stopping at exactly two things: a narrowing operation
# (`event["x"]`, `event.get("x")`, `event.foo`, which yield a leaf, not the event),
# and `sanitize_event_for_logging(...)`, which is the point of the exercise.

_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)
SANITIZER_FUNC = "sanitize_event_for_logging"
EVENT_PARAM = "event"


def _callee_name(func: ast.expr) -> str | None:
    """The bare name being called: ``f`` for ``f()``, ``g`` for ``mod.g()``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_log_call(node: ast.expr) -> bool:
    """A ``logger.info(...)``-shaped call, or a bare ``print(...)``.

    ``print`` counts: in Lambda it lands in the same CloudWatch stream as the
    logger, so it leaks identically.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "print":
        return True
    if not isinstance(func, ast.Attribute) or func.attr not in _LOG_METHODS:
        return False
    # Receiver must look like a logger, so `session.info(...)` or
    # `response.get(...)`-adjacent calls are not dragged in.
    receiver = func.value
    if isinstance(receiver, ast.Name):
        return "log" in receiver.id.lower()
    if isinstance(receiver, ast.Attribute):
        return "log" in receiver.attr.lower()
    return False


def _whole_event_reaches(node: ast.AST) -> bool:
    """Can the *whole* object bound to the name ``event`` flow into ``node``?

    True for the bare name and for anything that carries it along unchanged:
    ``json.dumps(event)``, ``f"{event}"``, ``"%s" % event``, ``str(event)``,
    ``[event]``, ``json.dumps(event, default=str)``.

    False in exactly two situations, which is where all the judgement lives:

    * **Narrowed.** ``event["arguments"]``, ``event.get("fieldName")``,
      ``event.identity`` each evaluate to a piece of the event, not the event.
      Resolvers log field names and operation names constantly and that is fine —
      flagging it would make this test unusable and get it deleted.
    * **Sanitized.** The name appears only inside a ``sanitize_event_for_logging``
      call, which is the required form.
    """
    if isinstance(node, ast.Name):
        return node.id == EVENT_PARAM
    if isinstance(node, ast.Call):
        if _callee_name(node.func) == SANITIZER_FUNC:
            return False  # the whole point: redacted before it reaches the log
        # Recurse into the arguments only. The callee expression itself
        # (`json.dumps`) cannot be the event.
        children: list[ast.AST] = list(node.args)
        children += [kw.value for kw in node.keywords]
        # `event.get(...)` — receiver is narrowed away, but a nested
        # `foo(event).bar()` still has to be followed.
        if isinstance(node.func, ast.Attribute) and not (
            isinstance(node.func.value, ast.Name)
            and node.func.value.id == EVENT_PARAM
        ):
            children.append(node.func.value)
        return any(_whole_event_reaches(child) for child in children)
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id == EVENT_PARAM:
            return False  # `event.identity` — a piece of it
        return _whole_event_reaches(node.value)
    if isinstance(node, ast.Subscript):
        if isinstance(node.value, ast.Name) and node.value.id == EVENT_PARAM:
            return False  # `event["arguments"]` — a piece of it
        return any(
            _whole_event_reaches(child)
            for child in (node.value, node.slice)
        )
    return any(_whole_event_reaches(child) for child in ast.iter_child_nodes(node))


def _rebinds_event(node: ast.AST) -> bool:
    """Does this statement bind the name ``event`` to something else?

    Needed because ``get_stepfunction_execution_resolver`` does
    ``for event in all_events:`` *inside* ``lambda_handler`` — from there on the
    name refers to a Step Functions execution-history record, not the invocation
    event, and treating those as leaks produced four false positives that would
    have had to be suppressed one by one.
    """
    targets: list[ast.expr] = []
    if isinstance(node, (ast.For, ast.AsyncFor)):
        targets = [node.target]
    elif isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        targets = [i.optional_vars for i in node.items if i.optional_vars is not None]
    elif isinstance(node, ast.ExceptHandler):
        return node.name == EVENT_PARAM
    for target in targets:
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name) and sub.id == EVENT_PARAM:
                return True
    return False


def _event_log_violations_in_body(body: list[ast.stmt]) -> list[ast.expr]:
    """Log calls in ``body`` that leak the event, honouring rebinding.

    Walks statement lists in order so that a rebinding of ``event`` suppresses the
    check for everything after it — Python leaks a ``for`` target into the enclosing
    scope, so that is the real semantics, not a convenience.
    """
    found: list[ast.expr] = []
    for statement in body:
        if _rebinds_event(statement):
            # From here on `event` is something else. Stop, rather than continue
            # with a name that no longer means what this test is about.
            return found
        for node in ast.walk(statement):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue  # handled as its own scope by the caller
            if _is_log_call(node):
                assert isinstance(node, ast.Call)
                args: list[ast.AST] = list(node.args)
                args += [kw.value for kw in node.keywords]
                if any(_whole_event_reaches(arg) for arg in args):
                    found.append(node)
    return found


def _event_handling_functions(tree: ast.AST) -> list[ast.FunctionDef]:
    """Functions whose FIRST positional parameter is named ``event``.

    That is the Lambda handler convention, and it is what separates the real
    handlers (and the helpers they hand the event to, e.g. ``_get_caller_info``)
    from unrelated locals that happen to be called ``event``. Without it,
    ``parse_execution_history(events)`` — which loops ``for event in events`` over
    Step Functions history and logs freely — reads as three leaks.
    """
    handlers = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        positional = node.args.posonlyargs + node.args.args
        if positional and positional[0].arg == EVENT_PARAM:
            handlers.append(node)
    return handlers


def _raw_event_log_hits(path: Path) -> list[str]:
    """``file:line`` for every log call in ``path`` that can reach the raw event."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []
    for handler in _event_handling_functions(tree):
        for call in _event_log_violations_in_body(handler.body):
            hits.append(f"{path.name}:{call.lineno} in {handler.name}()")
    return hits


def test_no_resolver_logs_the_raw_invocation_event():
    """A resolver that logs its event sanitizes it. This is the #921 defect itself.

    The invocation event carries ``identity.claims`` — Cognito ``sub``, ``email``
    and group membership — on every authenticated call, which is precisely what the
    canonical denylist exists to redact. Writing it to CloudWatch in full is the
    leak; having a tidy, consistent, byte-identical copy of the redactor sitting
    unused in the same directory does not help.

    No allowlist, deliberately. If a resolver genuinely must log a raw event, that
    is a decision worth making in a review, not a name added to a list here.
    """
    offenders = []
    for resolver in _resolver_dirs():
        index = resolver / "index.py"
        if not index.exists():
            continue
        for hit in _raw_event_log_hits(index):
            offenders.append(f"{resolver.name}/{hit}")
    assert not offenders, (
        "These log calls can write the unredacted invocation event — including "
        "identity.claims (Cognito sub, email, groups) — to CloudWatch. Wrap the "
        f"logged value in {SANITIZER_FUNC}(...), imported from {CANONICAL_IMPORT} "
        "if the function carries an idp-common layer or from the vendored "
        "`log_sanitizer` module if it does not:\n  " + "\n  ".join(offenders)
    )


comprehension_types = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _denylist_positions(tree: ast.AST) -> dict[int, str]:
    """Describe where each expression node sits, for the failure message.

    Only cosmetic — the scan itself considers every position — but "default of
    _my_sanitize()" is a far more useful report than a bare line number.
    """
    where: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            where[id(node.value)] = f"{names[0] if names else '<unnamed>'} ="
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target = node.target
            name = target.id if isinstance(target, ast.Name) else "<unnamed>"
            where[id(node.value)] = f"{name} ="
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            label = f"default argument of {getattr(node, 'name', '<lambda>')}()"
            for default in node.args.defaults:
                where[id(default)] = label
            for default in node.args.kw_defaults:
                if default is not None:
                    where[id(default)] = label
        elif isinstance(node, ast.Call):
            label = f"argument to {_callee_name(node.func) or '<call>'}()"
            for arg in node.args:
                where[id(arg)] = label
            for kw in node.keywords:
                where[id(kw.value)] = label
        elif isinstance(node, ast.Compare):
            for comparator in node.comparators:
                where[id(comparator)] = "comparison operand (`k in (...)`)"
        elif isinstance(node, ast.Return) and node.value is not None:
            where[id(node.value)] = "returned literal"
        elif isinstance(node, comprehension_types):
            for generator in node.generators:
                where[id(generator.iter)] = "iterated in a comprehension"
    return where


def test_no_resolver_defines_its_own_denylist():
    """No hand-rolled sensitive-key list anywhere under the resolver tree.

    Detected structurally — any collection literal of strings that overlaps the
    canonical denylist — so renaming ``_LOG_SENSITIVE_KEYS`` to something else does
    not slip past. Byte-identical vendored copies are the one allowed home for the
    list; ``test_vendored_copies_match_canonical`` proves they are identical.

    Scanned in **every expression position**, not just on the right-hand side of an
    assignment. The assignment-only version of this scan missed the two shapes a
    person is most likely to actually write::

        def _my_sanitize(obj, keys=("password", "secret", "token", "cookie")): ...
        if any(k in ("password", "secret", "token") for k in obj): ...

    Neither one ever binds the list to a name, and both were invisible: dropping
    either into a resolver left this suite green.
    """
    canonical_keys = {k.lower() for k in _canonical_deny_keys()}
    offenders = []
    for path in sorted(RESOLVER_ROOT.rglob("*.py")):
        if path.name == VENDORED_NAME:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        positions = _denylist_positions(tree)
        # `ast.walk` is breadth-first, so a wrapper such as `frozenset({...})` is
        # visited before the literal it wraps; keeping the first match per
        # (line, key set) reports the outermost node once rather than twice.
        seen: set[tuple[int, tuple[str, ...]]] = set()
        for node in ast.walk(tree):
            literals = _string_literals(node)
            if literals is None:
                continue
            hits = {s.lower() for s in literals} & canonical_keys
            if len(hits) < _DENYLIST_MATCH_THRESHOLD:
                continue
            key = (getattr(node, "lineno", -1), tuple(sorted(hits)))
            if key in seen:
                continue
            seen.add(key)
            offenders.append(
                f"{path.relative_to(REPO_ROOT)}:{key[0]} "
                f"{positions.get(id(node), '<expression>')} {sorted(hits)}"
            )
    assert not offenders, (
        "These files define their own log-redaction denylist instead of using the "
        "canonical one. Import sanitize_event_for_logging — from "
        f"{CANONICAL_IMPORT} if the function carries an idp-common layer, or from "
        "the vendored `log_sanitizer` module (add it with "
        "scripts/sync_resolver_log_sanitizer.sh) if it does not:\n  "
        + "\n  ".join(offenders)
    )


def test_vendored_copies_match_canonical():
    """Every vendored copy is byte-identical to the canonical module."""
    copies = sorted(RESOLVER_ROOT.rglob(VENDORED_NAME))
    assert copies, "expected at least one vendored log_sanitizer.py"
    canonical_text = CANONICAL.read_text(encoding="utf-8")
    for copy in copies:
        assert copy.read_text(encoding="utf-8") == canonical_text, (
            f"{copy.relative_to(REPO_ROOT)} has drifted from "
            f"{CANONICAL.relative_to(REPO_ROOT)}. Edit the canonical file only, "
            "then run scripts/sync_resolver_log_sanitizer.sh."
        )


def test_vendored_denylist_is_exactly_canonical():
    """The denylist these resolvers actually apply is the canonical one.

    Byte-identity already implies this, but assert it on the loaded module too: this
    is the property that matters at runtime, and it is what fails if a future key is
    added to the canonical list without the copies being re-synced.
    """
    canonical_keys = _canonical_deny_keys()
    assert PREVIOUSLY_MISSING_KEYS <= canonical_keys, (
        "The canonical denylist no longer covers keys it is required to cover: "
        f"{sorted(PREVIOUSLY_MISSING_KEYS - canonical_keys)}"
    )
    for index, copy in enumerate(sorted(RESOLVER_ROOT.rglob(VENDORED_NAME))):
        module = _load_module_by_path(copy, f"_vendored_log_sanitizer_{index}")
        assert frozenset(module._DEFAULT_DENY_KEY_SUBSTRINGS) == canonical_keys, (
            f"{copy.relative_to(REPO_ROOT)} applies a different denylist than "
            "the canonical module. Run scripts/sync_resolver_log_sanitizer.sh."
        )


def test_vendored_importers_have_a_copy_and_vice_versa():
    """A resolver importing the vendored module has one, and no copy is orphaned.

    An orphaned copy is dead weight in the package; a missing one is an ImportError
    at cold start.
    """
    importers, holders = set(), set()
    for resolver in _resolver_dirs():
        if _imports_vendored_sanitizer(resolver):
            importers.add(resolver.name)
        if (resolver / VENDORED_NAME).exists():
            holders.add(resolver.name)
    assert importers == holders, (
        "Vendored log_sanitizer.py copies and the resolvers importing them are out "
        f"of step. Importing without a copy: {sorted(importers - holders)}; "
        f"carrying an unused copy: {sorted(holders - importers)}. Fix the target "
        "list in scripts/sync_resolver_log_sanitizer.sh and re-run it."
    )


def test_canonical_importers_carry_an_idp_common_layer():
    """Importing idp_common for the sanitizer requires a layer that provides it.

    Without one the import raises at cold start and every invocation fails, so this
    is the pairing that makes the two-route split safe: drop a resolver's layer and
    this test tells you to vendor the module instead.
    """
    layers = _layers_by_code_uri()
    broken = []
    for resolver in _resolver_dirs():
        if not _imports_canonical_sanitizer(resolver):
            continue
        if not layers.get(resolver.name):
            broken.append(resolver.name)
    assert not broken, (
        f"These resolvers import {CANONICAL_IMPORT} but their function in "
        f"{' or '.join(str(t.relative_to(REPO_ROOT)) for t in TEMPLATES)} declares "
        "no IDPCommon layer, so the import fails at cold start. Either attach the "
        "layer or vendor the module with scripts/sync_resolver_log_sanitizer.sh: "
        f"{sorted(broken)}"
    )


def test_every_resolver_directory_is_found_in_a_template():
    """Every resolver directory maps to a declared function, in either template.

    This is the guard on the guard. A resolver the template scan cannot find reads
    as "declares no IDPCommon layer", which is the unsafe direction: the layer test
    above would reject a correct canonical import and steer the author into
    vendoring a module the function could have imported from the layer it already
    carries. That is exactly what happened to finetuning_jobs_resolver (declared in
    the parent template) and list_documents_gsi_resolver (CodeUri without a
    leading `./`).
    """
    found = _layers_by_code_uri()
    missing = [r.name for r in _resolver_dirs() if r.name not in found]
    assert not missing, (
        "These directories under nested/api-resolvers/src/lambda have no matching "
        f"CodeUri in {' or '.join(str(t.relative_to(REPO_ROOT)) for t in TEMPLATES)}, "
        "so the layer scan cannot tell whether they carry an IDPCommon layer and "
        f"will assume they do not: {sorted(missing)}"
    )


def test_sync_script_targets_the_layerless_resolvers():
    """The sync script's target list is exactly the set that cannot import idp_common."""
    script = SYNC_SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"targets=\(\n(.*?)\n\)", script, re.S)
    assert block, f"could not find the targets=( ... ) list in {SYNC_SCRIPT}"
    listed = {line.strip() for line in block.group(1).splitlines() if line.strip()}

    layers = _layers_by_code_uri()
    expected = {
        resolver.name
        for resolver in _resolver_dirs()
        if _imports_the_sanitizer(resolver) and not layers.get(resolver.name)
    }
    assert listed == expected, (
        "scripts/sync_resolver_log_sanitizer.sh does not target the right "
        f"resolvers. Missing from the script: {sorted(expected - listed)}; listed "
        f"but no longer layerless: {sorted(listed - expected)}."
    )


# --- the scanners' own behaviour -------------------------------------------------
#
# All three scanners above are the kind of check that fails open: if
# `_string_literals` does not recognise a shape, a reintroduced denylist simply is
# not reported; if `_whole_event_reaches` does not follow a form of interpolation,
# a raw-event log is not reported; and if `_imported_modules` reads prose as an
# import, correct code is rejected. None of those failures is visible from the
# suite passing, so assert the behaviour directly.

# The whole value of the raw-event scan is that it is not a text match, so the
# forms below are the test. Each is the SAME defect as `json.dumps(event)` written
# differently, and a scanner that catches only the first is a scanner that catches
# only the bug we already fixed.
_RAW_EVENT_LOGS_THAT_MUST_BE_SEEN = {
    "json.dumps in an f-string": 'logger.info(f"e: {json.dumps(event)}")',
    "json.dumps as a lazy arg": 'logger.info("e: %s", json.dumps(event))',
    "bare name in an f-string": 'logger.info(f"e: {event}")',
    "bare name as a lazy arg": 'logger.info("e: %s", event)',
    "bare name, sole argument": "logger.info(event)",
    "str() coercion": "logger.info(str(event))",
    "repr() coercion": 'logger.debug(f"{event!r}")',
    "percent formatting": 'logger.info("e: %s" % event)',
    "str.format": 'logger.info("e: {}".format(event))',
    "concatenation": 'logger.info("e: " + str(event))',
    "inside a container": 'logger.info("e: %s", {"event": event})',
    "json.dumps with kwargs": 'logger.info(json.dumps(event, default=str))',
    "pprint.pformat": "logger.info(pprint.pformat(event))",
    "debug level": 'logger.debug(f"{json.dumps(event)}")',
    "exception level": 'logger.exception(f"failed on {event}")',
    "print instead of logger": "print(json.dumps(event))",
    "logger.log with a level": 'logger.log(logging.INFO, f"{event}")',
    "module-level logging alias": 'LOG.info("%s", event)',
    "attribute logger": 'self.log.info("%s", event)',
    "keyword argument": 'logger.info("x", extra={"raw": event})',
}

# Narrowing an event down to a field is normal and must stay usable: resolvers log
# the operation name and argument keys on nearly every call. Flagging these would
# make the test unusable, and an unusable test gets deleted rather than fixed.
_RAW_EVENT_LOGS_THAT_MUST_NOT_TRIP = {
    "sanitized, f-string": 'logger.info(f"e: {json.dumps(sanitize_event_for_logging(event))}")',
    "sanitized, lazy arg": 'logger.info("e: %s", sanitize_event_for_logging(event))',
    "sanitized with kwargs": 'logger.info(json.dumps(sanitize_event_for_logging(event), default=str))',
    "subscript": 'logger.info(f"{event[\'fieldName\']}")',
    "get() call": 'logger.info(f"{event.get(\'fieldName\')}")',
    "chained get()": 'logger.info(f"{event.get(\'arguments\', {}).get(\'id\')}")',
    "attribute access": 'logger.info(f"{event.foo}")',
    "no event at all": 'logger.info("resolver invoked")',
    "a different name": 'logger.info(f"{json.dumps(other)}")',
    "len() of a field": 'logger.info("%s", len(event["arguments"]))',
}

_DENYLIST_SHAPES_THAT_MUST_BE_SEEN = {
    "set literal": '_K = {"password", "secret", "token"}\n',
    "tuple literal": '_K = ("password", "secret", "token")\n',
    "list literal": '_K = ["password", "secret", "token"]\n',
    "annotated assign": '_K: set = {"password", "secret", "token"}\n',
    "frozenset of a set": '_K = frozenset({"password", "secret", "token"})\n',
    "frozenset of a list": '_K = frozenset(["password", "secret", "token"])\n',
    "set of a list": '_K = set(["password", "secret", "token"])\n',
    "tuple of a list": '_K = tuple(["password", "secret", "token"])\n',
    "dict keys": '_K = {"password": "x", "secret": "x", "token": "x"}\n',
    "frozenset of dict keys": '_K = frozenset({"password": 1, "secret": 1, "token": 1})\n',
    # Never assigned to anything. These are the shapes an assignment-only scan
    # could not see, and they are not exotic — a default argument on a helper is
    # how a person actually writes this.
    "default argument": (
        'def _my_sanitize(obj, keys=("password", "secret", "token", "cookie")):\n'
        "    return obj\n"
    ),
    "keyword-only default": (
        'def _my_sanitize(obj, *, keys={"password", "secret", "token"}):\n'
        "    return obj\n"
    ),
    "lambda default": '_f = lambda o, k=("password", "secret", "token"): o\n',
    "inline membership test": (
        'if any(k in ("password", "secret", "token") for k in obj):\n    pass\n'
    ),
    "comparison operand": 'if key in ("password", "secret", "token"):\n    pass\n',
    "call argument": '_redact(obj, ["password", "secret", "token"])\n',
    "call keyword argument": '_redact(obj, keys=["password", "secret", "token"])\n',
    "bare expression statement": '("password", "secret", "token")\n',
    "returned literal": (
        'def _keys():\n    return ("password", "secret", "token")\n'
    ),
}

_SHAPES_THAT_MUST_NOT_TRIP = {
    "two keys only": '_K = ("token", "cursor")\n',
    "unrelated strings": '_K = ("alpha", "beta", "gamma", "delta")\n',
    "non-literal": "_K = frozenset(some_other_module.KEYS)\n",
    "two keys in a default arg": "def f(o, k=(\"token\", \"cursor\")):\n    return o\n",
}

# Known residual gaps, not asserted either way: a denylist spelled as keyword
# arguments (`dict(password="x", ...)`), built by a comprehension, or assembled with
# `|=`/`.add()` across statements still escapes this scan. Each is a stranger way to
# write a constant than the shapes above, and closing them means evaluating
# arbitrary expressions.
#
# Which test is load-bearing for which failure, because it is easy to get this
# backwards (an earlier version of this comment did):
#
# * A vendored copy silently drifting from the canonical module —
#   `test_vendored_copies_match_canonical` is the guarantee.
# * A resolver hand-rolling a NEW local denylist in its own `index.py` — byte
#   identity of the copies says nothing whatsoever about that.
#   `test_no_resolver_defines_its_own_denylist` is the only guard, so its gaps are
#   real gaps and not merely defence in depth.
# * A resolver logging the raw event while using no denylist at all —
#   `test_no_resolver_logs_the_raw_invocation_event`. This is the one that maps to
#   the original defect; the other two are about consistency.


def _scan_snippet(log_call: str, *, param: str = EVENT_PARAM) -> list[str]:
    """Run the raw-event scan over ``log_call`` placed in a handler body."""
    source = f"def handler({param}, context):\n    {log_call}\n"
    tree = ast.parse(source)
    hits = []
    for handler in _event_handling_functions(tree):
        for call in _event_log_violations_in_body(handler.body):
            hits.append(f"{handler.name}:{call.lineno}")
    return hits


@pytest.mark.parametrize("label", sorted(_RAW_EVENT_LOGS_THAT_MUST_BE_SEEN))
def test_the_raw_event_scan_sees_every_way_of_logging_the_event(label):
    """The same leak, spelled twenty ways, is still the same leak.

    This is the test that makes the guard worth having. A check that forbade the
    literal string ``json.dumps(event)`` would pass every one of the nineteen other
    entries here while the event still lands in CloudWatch verbatim — the scanner
    would be pinned to the shape of the line that happened to be wrong in #921.
    """
    hits = _scan_snippet(_RAW_EVENT_LOGS_THAT_MUST_BE_SEEN[label])
    assert hits, (
        f"the raw-event scan cannot see `{label}`: "
        f"`{_RAW_EVENT_LOGS_THAT_MUST_BE_SEEN[label]}` would leak the unredacted "
        "invocation event and this suite would stay green"
    )


@pytest.mark.parametrize("label", sorted(_RAW_EVENT_LOGS_THAT_MUST_NOT_TRIP))
def test_the_raw_event_scan_allows_sanitized_and_narrowed_logging(label):
    """Sanitized events, and single fields pulled out of an event, are fine.

    False positives here are not harmless: resolvers log the operation name on
    nearly every call, so a scan that flagged ``event.get("fieldName")`` would need
    dozens of suppressions and would be deleted instead of fixed.
    """
    snippet = _RAW_EVENT_LOGS_THAT_MUST_NOT_TRIP[label]
    assert not _scan_snippet(snippet), (
        f"the raw-event scan wrongly flagged `{label}`: `{snippet}` does not log "
        "the whole unredacted event"
    )


def test_the_raw_event_scan_ignores_an_unrelated_local_named_event():
    """``for event in all_events`` rebinds the name; those are not the invocation event.

    ``get_stepfunction_execution_resolver`` loops over Step Functions
    execution-history records under the name ``event`` — once inside
    ``lambda_handler`` itself, and again in two helpers. A scan that matched on the
    name alone reported four leaks there, all false. Suppressing four lines by hand
    is how a guard stops being trusted.
    """
    # Rebound by a for-loop inside the handler: not a hit.
    rebound = (
        "def lambda_handler(event, context):\n"
        "    for event in event['history']:\n"
        "        logger.warning(f'bad: {json.dumps(event)}')\n"
    )
    tree = ast.parse(rebound)
    hits = [
        call
        for handler in _event_handling_functions(tree)
        for call in _event_log_violations_in_body(handler.body)
    ]
    assert not hits, "a for-loop rebinding of `event` was treated as the event"

    # A helper whose first parameter is NOT `event` is not an event handler.
    helper = (
        "def parse_history(events):\n"
        "    for event in events:\n"
        "        logger.warning(f'{json.dumps(event)}')\n"
    )
    assert not _event_handling_functions(ast.parse(helper))

    # But a helper that IS handed the event is checked like the handler, because
    # `_get_caller_info(event)` in the stepfunction resolver is exactly that.
    assert _scan_snippet("logger.info(json.dumps(event))", param="event")
    assert not _scan_snippet("logger.info(json.dumps(event))", param="record")


def _denylist_key_hits(source: str) -> set[str]:
    """Every canonical key the real scan would match in ``source``.

    Uses exactly the same traversal as ``test_no_resolver_defines_its_own_denylist``
    — every expression position, not just assignments — so these self-tests cannot
    pass while the real scan is narrower.
    """
    canonical_keys = {k.lower() for k in _canonical_deny_keys()}
    hits: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        literals = _string_literals(node)
        if literals:
            hits |= {s.lower() for s in literals} & canonical_keys
    return hits


@pytest.mark.parametrize("label", sorted(_DENYLIST_SHAPES_THAT_MUST_BE_SEEN))
def test_the_denylist_scan_sees_every_collection_shape(label):
    """A denylist is a denylist in whatever container it is written in.

    `frozenset({...})` and a dict literal both escaped the original scan, which
    required the assigned value to be a bare tuple/list/set literal. The irony was
    that PREVIOUSLY_MISSING_KEYS in this very file is a `frozenset({...})`.

    The later entries here are never assigned at all — a default argument, a
    membership test, a call argument. Those escaped the scan even after the
    container shapes were fixed, because it still only inspected ``ast.Assign`` and
    ``ast.AnnAssign``.
    """
    hits = _denylist_key_hits(_DENYLIST_SHAPES_THAT_MUST_BE_SEEN[label])
    assert len(hits) >= _DENYLIST_MATCH_THRESHOLD, (
        f"the denylist scan cannot see a {label}; a hand-copied denylist written "
        f"that way would be reintroduced silently (matched only {sorted(hits)})"
    )


@pytest.mark.parametrize("label", sorted(_SHAPES_THAT_MUST_NOT_TRIP))
def test_the_denylist_scan_does_not_trip_on_ordinary_collections(label):
    hits = _denylist_key_hits(_SHAPES_THAT_MUST_NOT_TRIP[label])
    assert len(hits) < _DENYLIST_MATCH_THRESHOLD, (
        f"the denylist scan flagged an ordinary collection ({label}): {sorted(hits)}"
    )


def test_the_import_scan_ignores_prose_that_quotes_an_import(tmp_path):
    """A docstring or comment naming the canonical module is not an import of it.

    Every vendored copy carries the canonical ``Usage::`` docstring, one line of
    which is a real-looking ``from idp_common.utils.log_sanitizer import ...``.
    """
    resolver = tmp_path / "prose_resolver"
    resolver.mkdir()
    (resolver / "index.py").write_text(
        '"""Handler.\n'
        "\n"
        "Usage::\n"
        "\n"
        f"    from {CANONICAL_IMPORT} import sanitize_event_for_logging\n"
        '"""\n'
        f"# byte-identical vendored copy of {CANONICAL_IMPORT}\n"
        f"from {VENDORED_MODULE} import sanitize_event_for_logging\n",
        encoding="utf-8",
    )
    assert not _imports_canonical_sanitizer(resolver)
    assert _imports_vendored_sanitizer(resolver)
    assert _imports_the_sanitizer(resolver)


def test_the_import_scan_sees_a_real_import_wherever_it_sits(tmp_path):
    """Including indented inside a function or a try block."""
    resolver = tmp_path / "indented_resolver"
    resolver.mkdir()
    (resolver / "index.py").write_text(
        "def handler(event, context):\n"
        f"    from {CANONICAL_IMPORT} import sanitize_event_for_logging\n"
        "    return sanitize_event_for_logging(event)\n",
        encoding="utf-8",
    )
    assert _imports_canonical_sanitizer(resolver)
    assert not _imports_vendored_sanitizer(resolver)
