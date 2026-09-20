# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The vendored `s3_targets` copies must never drift from the canonical module.

Three resolvers constrain a caller-supplied S3 location: one read path and two write
paths. They must apply the *same* rule — a bucket allow-list that differs between
call sites admits, somewhere, what it denies elsewhere, and that divergence is
invisible in review because each file looks correct on its own.

Two of the three carry no `idp_common` layer, so they cannot import the library at
runtime (SAM packages each function from its own `CodeUri`, so they cannot reach a
sibling directory either). Those get a byte-identical committed copy, the same
guarded-vendoring shape as `config_scope.py` and `log_sanitizer.py`. The third carries
the layer and imports the module directly, so it has nothing to copy.

This test is the guard, and it derives the expected split from the templates rather
than from a list of names in a comment: a function that gains or loses the layer
changes which side of the split it belongs on, and that has to be noticed.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "lib" / "idp_common_pkg").is_dir():
            return parent
    raise RuntimeError("repo root not found")


REPO = _repo_root()
CANONICAL = REPO / "lib/idp_common_pkg/idp_common/s3_targets.py"
RESOLVER_TREE = REPO / "nested/api-resolvers/src/lambda"
TEMPLATE = REPO / "nested/api-resolvers/template.yaml"


def _template_resources():
    """`Resources` from the nested template, with CFN short-form tags left opaque.

    Resolving `!Ref`/`!Sub` is not the point — only which CodeUri a resource has and
    whether it lists an IDPCommon layer.
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
    return doc["Resources"]


def _consumers():
    """Resolver directories whose index.py uses the module, and how."""
    vendored, imported = set(), set()
    for index in sorted(RESOLVER_TREE.glob("*/index.py")):
        text = index.read_text()
        # A layer-carrying function imports by module path — the convention
        # `config_scope` follows, and the one the package's lazy `__getattr__` does
        # not cover. A layerless one imports the committed sibling copy.
        if re.search(r"^(import idp_common\.s3_targets|from idp_common\.s3_targets|from idp_common import .*\bs3_targets\b)", text, re.M):
            imported.add(index.parent.name)
        elif re.search(r"^import s3_targets\b", text, re.M):
            vendored.add(index.parent.name)
    return vendored, imported


def test_the_canonical_module_exists():
    assert CANONICAL.exists(), f"canonical module missing: {CANONICAL}"


def test_there_is_at_least_one_consumer_of_each_kind():
    """Otherwise the two assertions below are vacuous."""
    vendored, imported = _consumers()
    assert vendored, "no resolver vendors s3_targets — is this test still needed?"
    assert imported, "no resolver imports s3_targets from the layer"


def test_every_vendored_copy_is_byte_identical():
    vendored, _ = _consumers()
    canonical = CANONICAL.read_bytes()
    for name in sorted(vendored):
        copy = RESOLVER_TREE / name / "s3_targets.py"
        assert copy.exists(), f"{name} imports s3_targets but has no copy of it"
        assert copy.read_bytes() == canonical, (
            f"{name}/s3_targets.py has drifted from {CANONICAL.relative_to(REPO)}. "
            "A bucket allow-list that differs between call sites admits somewhere "
            "what it denies elsewhere. Re-copy the canonical module."
        )


def test_the_split_matches_which_functions_carry_the_layer():
    """A function that vendors the module must NOT carry the layer, and vice versa.

    This is the part that rots on its own: attaching the base layer to a function
    that has a committed copy leaves two importable modules with the same name and
    resolution depending on sys.path order.
    """
    resources = _template_resources()
    vendored, imported = _consumers()

    def _has_layer(directory: str) -> bool:
        # Parsed, not sliced. A text window around a matched CodeUri line picks up
        # whichever resource header happens to precede it, which is how this check
        # reported the wrong answer for a function that plainly carries the layer.
        matches = [
            r
            for r in resources.values()
            if str(r.get("Properties", {}).get("CodeUri", "")).rstrip("/").endswith(
                f"src/lambda/{directory}"
            )
        ]
        assert matches, f"no resource in the template has CodeUri src/lambda/{directory}"
        layers = matches[0].get("Properties", {}).get("Layers") or []
        return any("IDPCommon" in str(layer) for layer in layers)

    for name in sorted(vendored):
        assert not _has_layer(name), (
            f"{name} carries an IDPCommon layer AND a committed copy of "
            "s3_targets.py — two importable modules of the same name. Drop the copy "
            "and import from the layer."
        )
    for name in sorted(imported):
        assert _has_layer(name), (
            f"{name} imports s3_targets from idp_common but its template resource "
            "carries no IDPCommon layer, so the import fails at runtime."
        )
        assert not (RESOLVER_TREE / name / "s3_targets.py").exists(), (
            f"{name} imports from the layer but also has a committed copy"
        )
