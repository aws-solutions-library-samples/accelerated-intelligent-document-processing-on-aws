# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The vendored scope modules in this function must never drift from the canonical ones.

This resolver enforces both per-user scope axes on the object key — the
configuration-profile axis and the test-set axis — and it carries **no**
``idp_common`` layer, so it cannot import either matcher at runtime. SAM packages each
function from its own ``CodeUri``, so it cannot reach a sibling directory either. It
therefore keeps byte-identical committed copies, the same guarded-vendoring shape
already used for ``s3_targets.py`` and ``log_sanitizer.py`` here and for
``config_scope.py`` in both document-list resolvers.

A scope matcher that differs between call sites admits somewhere what it denies
elsewhere, and that divergence is invisible in review because each copy looks correct
on its own. It is also the specific bug this vendoring already caused once: before the
canonical ``config_scope`` existed, the document-list resolvers admitted a document
whose profile was absent while the configuration resolver denied one. These tests are
the guard.

The **layer split** is asserted too, not just the bytes: attaching the base layer to
this function while the copies remain leaves two importable modules with the same
name and resolution depending on ``sys.path`` order, which is a silent behaviour
change rather than an import error.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

HERE = Path(__file__).resolve().parent


def _repo_root() -> Path:
    for parent in HERE.parents:
        if (parent / "lib" / "idp_common_pkg").is_dir():
            return parent
    raise RuntimeError("repo root not found")


REPO = _repo_root()
TEMPLATE = REPO / "nested/api-resolvers/template.yaml"

# module basename -> canonical source, for every scope module this function vendors.
VENDORED = {
    "config_scope": REPO / "lib/idp_common_pkg/idp_common/config_scope.py",
    "testset_scope": REPO / "lib/idp_common_pkg/idp_common/testset_scope.py",
}


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_the_canonical_module_exists(name):
    assert VENDORED[name].exists(), f"canonical module missing: {VENDORED[name]}"


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_the_vendored_copy_is_byte_identical(name):
    copy = HERE / f"{name}.py"
    canonical = VENDORED[name]
    assert copy.exists(), f"missing vendored copy: {copy}"
    assert copy.read_bytes() == canonical.read_bytes(), (
        f"{copy.name} has drifted from {canonical.relative_to(REPO)}. A scope rule "
        "that differs between call sites admits somewhere what it denies elsewhere. "
        f"Re-copy:\n  cp {canonical.relative_to(REPO)} "
        f"{copy.relative_to(REPO)}"
    )


def test_key_scope_imports_the_vendored_copies_and_not_the_layer():
    """`key_scope` must reach the copies that ship in this bundle.

    ``import idp_common.config_scope`` would work in a test environment that has the
    library installed and fail at runtime in a function with no layer — a difference a
    passing suite would hide entirely, since the suite runs where the library IS
    installed.

    Asserted on the module's **import statements**, parsed, rather than on the presence
    of the string ``idp_common`` anywhere in the file. The substring form was satisfied
    by a docstring: ``key_scope`` legitimately cites
    ``idp_common/config/revisions.py`` when explaining which character class a profile
    segment must satisfy, and a citation is not a runtime dependency. A gate that
    cannot tell those apart pushes the next person to delete the citation, which is the
    opposite of what it is for.
    """
    import ast

    source = (HERE / "key_scope.py").read_text()
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    for name in sorted(VENDORED):
        assert name in imported, (
            f"key_scope does not import the vendored {name} (imports: "
            f"{sorted(imported)})"
        )

    from_layer = sorted(m for m in imported if m.split(".")[0] == "idp_common")
    assert not from_layer, (
        f"key_scope imports {from_layer} from the idp_common layer, but this function "
        "carries no layer — the import would fail at runtime while passing in a test "
        "environment that has the library installed. Use the vendored copy."
    )


def test_this_function_carries_no_idp_common_layer():
    """The premise of vendoring at all, read from the template rather than assumed.

    Parsed, not sliced: a text window around a matched `CodeUri` picks up whichever
    resource header happens to precede it.
    """
    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    def _passthrough(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return f"!{tag_suffix} {node.value}"
        if isinstance(node, yaml.SequenceNode):
            return [f"!{tag_suffix}"] + loader.construct_sequence(node, deep=True)
        return {f"!{tag_suffix}": loader.construct_mapping(node, deep=True)}

    _Loader.add_multi_constructor("", _passthrough)
    doc = yaml.load(TEMPLATE.read_text(), Loader=_Loader)  # nosec B506 - SafeLoader subclass

    matches = [
        r
        for r in doc["Resources"].values()
        if str((r.get("Properties") or {}).get("CodeUri", ""))
        .rstrip("/")
        .endswith(f"src/lambda/{HERE.name}")
    ]
    assert matches, f"no resource in the template has CodeUri src/lambda/{HERE.name}"
    layers = (matches[0].get("Properties") or {}).get("Layers") or []
    assert not any("IDPCommon" in str(layer) for layer in layers), (
        f"{HERE.name} now carries an IDPCommon layer AND committed copies of "
        f"{sorted(VENDORED)} — two importable modules of the same name, resolved by "
        "sys.path order. Drop the copies and import from the layer, or drop the layer."
    )


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_the_shipped_copy_still_enforces_its_fail_closed_rule(name):
    """Smoke-test the bytes that actually ship, not an installed library."""
    spec = importlib.util.spec_from_file_location(
        f"vendored_{name}", HERE / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if name == "config_scope":
        assert module.scope_allows(None, "anything") is True
        assert module.scope_allows(["lending"], "lending") is True
        assert module.scope_allows(["lending"], "claims") is False
        # An object with no name to match cannot be proven in scope, so it is denied.
        assert module.scope_allows(["lending"], None) is False
        assert module.scope_allows(["lending-*"], "lending-2") is True
    else:
        # An Annotator with no scope is denied rather than unrestricted.
        with pytest.raises(module.TestSetAccessDenied):
            module.assert_can_access_test_set(
                {"identity": {"claims": {"cognito:groups": ["Annotator"]}}}, "ts-1"
            )
        # Admin and Author own test sets and are never scoped.
        module.assert_can_access_test_set(
            {"identity": {"claims": {"cognito:groups": ["Admin"]}}}, "ts-1"
        )
