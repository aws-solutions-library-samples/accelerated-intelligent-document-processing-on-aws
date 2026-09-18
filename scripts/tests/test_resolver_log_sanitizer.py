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


def test_no_resolver_defines_its_own_denylist():
    """No hand-rolled sensitive-key list anywhere under the resolver tree.

    Detected structurally — any collection literal of strings that overlaps the
    canonical denylist — so renaming ``_LOG_SENSITIVE_KEYS`` to something else does
    not slip past. Byte-identical vendored copies are the one allowed home for the
    list; ``test_vendored_copies_match_canonical`` proves they are identical.
    """
    canonical_keys = {k.lower() for k in _canonical_deny_keys()}
    offenders = []
    for path in sorted(RESOLVER_ROOT.rglob("*.py")):
        if path.name == VENDORED_NAME:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            literals = _string_literals(value)
            if literals is None:
                continue
            hits = {s.lower() for s in literals} & canonical_keys
            if len(hits) >= _DENYLIST_MATCH_THRESHOLD:
                names = [t.id for t in targets if isinstance(t, ast.Name)] or [
                    "<unnamed>"
                ]
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                    f"{names[0]} = {sorted(hits)}"
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
# Both scanners above are the kind of check that fails open: if `_string_literals`
# does not recognise a shape, a reintroduced denylist simply is not reported, and if
# `_imported_modules` reads prose as an import, correct code is rejected. Neither
# failure is visible from the suite passing, so assert the behaviour directly.

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
}

_SHAPES_THAT_MUST_NOT_TRIP = {
    "two keys only": '_K = ("token", "cursor")\n',
    "unrelated strings": '_K = ("alpha", "beta", "gamma", "delta")\n',
    "non-literal": "_K = frozenset(some_other_module.KEYS)\n",
}

# Known residual gaps, not asserted either way: a denylist spelled as keyword
# arguments (`dict(password="x", ...)`), built by a comprehension, or assembled with
# `|=`/`.add()` across statements still escapes this scan. Each is a stranger way to
# write a constant than the ten shapes above, and closing them means evaluating
# arbitrary expressions. `test_vendored_copies_match_canonical` remains the
# load-bearing guarantee; this scan is defence in depth.


@pytest.mark.parametrize("label", sorted(_DENYLIST_SHAPES_THAT_MUST_BE_SEEN))
def test_the_denylist_scan_sees_every_collection_shape(label):
    """A denylist is a denylist in whatever container it is written in.

    `frozenset({...})` and a dict literal both escaped the original scan, which
    required the assigned value to be a bare tuple/list/set literal. The irony was
    that PREVIOUSLY_MISSING_KEYS in this very file is a `frozenset({...})`.
    """
    source = _DENYLIST_SHAPES_THAT_MUST_BE_SEEN[label]
    canonical_keys = {k.lower() for k in _canonical_deny_keys()}
    tree = ast.parse(source)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            value = node.value
        else:
            continue
        literals = _string_literals(value)
        if literals:
            hits |= {s.lower() for s in literals} & canonical_keys
    assert len(hits) >= _DENYLIST_MATCH_THRESHOLD, (
        f"the denylist scan cannot see a {label}; a hand-copied denylist written "
        f"that way would be reintroduced silently (matched only {sorted(hits)})"
    )


@pytest.mark.parametrize("label", sorted(_SHAPES_THAT_MUST_NOT_TRIP))
def test_the_denylist_scan_does_not_trip_on_ordinary_collections(label):
    source = _SHAPES_THAT_MUST_NOT_TRIP[label]
    canonical_keys = {k.lower() for k in _canonical_deny_keys()}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign):
            continue
        literals = _string_literals(node.value) or []
        hits = {s.lower() for s in literals} & canonical_keys
        assert len(hits) < _DENYLIST_MATCH_THRESHOLD, (
            f"the denylist scan flagged an ordinary collection ({label}): "
            f"{sorted(hits)}"
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
