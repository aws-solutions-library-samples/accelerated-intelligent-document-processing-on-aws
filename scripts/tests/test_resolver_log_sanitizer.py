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
SYNC_SCRIPT = REPO_ROOT / "scripts/sync_resolver_log_sanitizer.sh"

VENDORED_NAME = "log_sanitizer.py"
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
    """Map ``src/lambda/<dir>`` to the IDPCommon layer refs its function declares.

    Parsed with regex rather than a YAML loader: the template is full of short-form
    intrinsics (``!Ref``, ``!Sub``, ``!If``) that a plain ``yaml.safe_load`` refuses,
    and all this needs is which resource block names which layer. Resource blocks
    start at exactly two spaces of indentation.
    """
    lines = TEMPLATE.read_text(encoding="utf-8").splitlines()
    starts = [
        i for i, line in enumerate(lines) if re.match(r"^  [A-Za-z0-9]+:\s*$", line)
    ]
    starts.append(len(lines))
    layers: dict[str, list[str]] = {}
    for start, end in zip(starts, starts[1:]):
        block = "\n".join(lines[start:end])
        match = re.search(r"CodeUri:\s*\./src/lambda/([A-Za-z0-9_]+)/?", block)
        if not match:
            continue
        layers[match.group(1)] = re.findall(r"!Ref (IDPCommon\w*LayerArn)", block)
    return layers


def _string_literals(node: ast.AST) -> list[str] | None:
    """Return the string members of a tuple/list/set literal, else None."""
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return None
    values = []
    for element in node.elts:
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
        if re.search(r"^from log_sanitizer import ", _index_source(resolver), re.M):
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
        if CANONICAL_IMPORT not in _index_source(resolver):
            continue
        if not layers.get(resolver.name):
            broken.append(resolver.name)
    assert not broken, (
        f"These resolvers import {CANONICAL_IMPORT} but their function in "
        f"{TEMPLATE.relative_to(REPO_ROOT)} declares no IDPCommon layer, so the "
        "import fails at cold start. Either attach the layer or vendor the module "
        f"with scripts/sync_resolver_log_sanitizer.sh: {sorted(broken)}"
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
        if "sanitize_event_for_logging" in _index_source(resolver)
        and not layers.get(resolver.name)
    }
    assert listed == expected, (
        "scripts/sync_resolver_log_sanitizer.sh does not target the right "
        f"resolvers. Missing from the script: {sorted(expected - listed)}; listed "
        f"but no longer layerless: {sorted(listed - expected)}."
    )
