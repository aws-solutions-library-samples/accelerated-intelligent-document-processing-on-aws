"""Every resolver Lambda that imports ``idp_common`` must carry the layer.

Background — the bug this suite exists to prevent
-------------------------------------------------
`idp_common` is delivered to Lambda as a **layer**, not vendored into each
function's bundle. Several resolvers are deliberately layer-free (they are on hot
UI paths and kept dependency-free), so adding an `idp_common` import to one of
them produces a function that imports a module the runtime does not have.

That failure is nastier than it sounds, because these resolvers wrap their work
in broad `except Exception` handlers so a partial failure still returns a usable
response. The `ImportError` is therefore swallowed and logged, and the feature the
import was added for silently does nothing.

That is exactly what happened while wiring configuration-revision pinning: the
test runner gained a `ConfigurationManager` import to capture a pinned revision's
configuration and mark it exempt from retention pruning. Both no-op'd against a
real deployment — the run recorded the right revision *number* while capturing the
wrong configuration body, and the revision it depended on stayed prunable. No unit
test could catch it: they mock `ConfigurationManager`, so they pass either way.

This test compares source imports against the deployed template instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

# scripts/sdlc/tests/<this file> -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]

TEMPLATES = (
    REPO_ROOT / "nested/api-resolvers/template.yaml",
    REPO_ROOT / "template.yaml",
    # Feature-platform resolvers run the same risk: applyFeatureConfigPreset now
    # uses idp_common's ConfigurationManager, and an ImportError there would leave
    # the preset applied but its revision silently unrecorded.
    REPO_ROOT / "feature-platform/main-stack-extensions/template.yaml",
)

# Any layer that ships idp_common. The package is split into feature layers
# (base / reporting / agents / multi-document-discovery); a function only needs
# one of them for `import idp_common` to resolve.
LAYER_REFS = ("IDPCommon",)

# Imports that do NOT require the layer: the two document-list resolvers vendor
# `config_scope` verbatim as a sibling module (guarded by its own drift test), so
# `from config_scope import ...` is satisfied from the function's own bundle.
VENDORED_MODULES = ("config_scope",)


class _Loader(yaml.SafeLoader):
    """SafeLoader (never the unsafe yaml.Loader) plus CFN short-form tags.

    The intrinsic content is PRESERVED rather than nulled: this test inspects
    which layer a function references, so `!Ref IDPCommonBaseLayer` has to survive
    parsing as something inspectable.
    """


def _intrinsic(loader, tag_suffix, node):
    key = tag_suffix if tag_suffix in ("Ref", "Condition") else f"Fn::{tag_suffix}"
    if isinstance(node, yaml.ScalarNode):
        return {key: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {key: loader.construct_sequence(node)}
    return {key: loader.construct_mapping(node)}


_Loader.add_multi_constructor("!", _intrinsic)


def _parsed(template_path: Path) -> dict:
    with template_path.open() as handle:
        return yaml.load(handle, Loader=_Loader) or {}


def _functions(template_path: Path) -> dict[str, dict]:
    with template_path.open() as handle:
        loader = _Loader(handle)
        try:
            doc = loader.get_single_data()
        finally:
            loader.dispose()
    return {
        name: body
        for name, body in (doc.get("Resources") or {}).items()
        if body.get("Type") == "AWS::Serverless::Function"
    }


def _code_dir(template_path: Path, body: dict) -> Path | None:
    code_uri = (body.get("Properties") or {}).get("CodeUri")
    if not isinstance(code_uri, str):
        return None  # container image, or an intrinsic we cannot resolve
    return (template_path.parent / code_uri).resolve()


def _imports_idp_common(code_dir: Path) -> list[str]:
    """Python files under `code_dir` that import idp_common at module scope."""
    hits = []
    for path in code_dir.rglob("*.py"):
        if any(part in {"tests", "__pycache__", ".aws-sam"} for part in path.parts):
            continue
        if path.name.startswith("test_"):
            continue
        text = path.read_text(errors="ignore")
        if re.search(r"^\s*(from|import)\s+idp_common\b", text, re.M):
            hits.append(str(path.relative_to(REPO_ROOT)))
    return hits


def _has_layer(body: dict) -> bool:
    layers = (body.get("Properties") or {}).get("Layers")
    if not layers:
        return False
    rendered = str(layers)
    return any(ref in rendered for ref in LAYER_REFS)


# The idp_common layers are built on the x86_64 build host and declare no
# CompatibleArchitectures, which Lambda reads as x86_64-only.
LAYER_ARCH = "x86_64"


def _globals_architectures(template: dict) -> list:
    """Default architectures a template's Globals apply to every function."""
    return (
        ((template.get("Globals") or {}).get("Function") or {}).get("Architectures")
        or []
    )


def _cases():
    for template_path in TEMPLATES:
        for name, body in _functions(template_path).items():
            code_dir = _code_dir(template_path, body)
            if code_dir is None or not code_dir.is_dir():
                continue
            yield template_path, name, body, code_dir


@pytest.mark.unit
@pytest.mark.parametrize(
    "template_path,name,body,code_dir",
    list(_cases()),
    ids=lambda v: v.name if isinstance(v, Path) else (v if isinstance(v, str) else ""),
)
def test_function_importing_idp_common_has_the_layer(template_path, name, body, code_dir):
    importers = _imports_idp_common(code_dir)
    if not importers:
        return
    assert _has_layer(body), (
        f"{name} ({template_path.relative_to(REPO_ROOT)}) imports idp_common in "
        f"{importers} but declares no idp_common layer. At runtime the import "
        f"raises ImportError, which these resolvers swallow — so the feature "
        f"silently does nothing. Add the layer that carries what it imports "
        f"(base / reporting / agents / multi-document-discovery), or drop the import."
    )


@pytest.mark.unit
def test_the_layer_free_resolvers_only_use_vendored_modules():
    """
    The document-list resolvers are intentionally layer-free. This asserts they
    stay that way honestly: no idp_common import, and their sibling imports are
    the vendored ones.
    """
    template_path = REPO_ROOT / "nested/api-resolvers/template.yaml"
    functions = _functions(template_path)
    for name in ("ListDocumentsGSIResolverFunction", "ListDocumentsByDateRangeResolverFunction"):
        body = functions[name]
        code_dir = _code_dir(template_path, body)
        assert code_dir is not None and code_dir.is_dir(), name
        assert not _imports_idp_common(code_dir), (
            f"{name} is deliberately layer-free (it is on the hottest UI query). "
            f"Vendor what you need instead — see config_scope.py and its drift test."
        )
        for module in VENDORED_MODULES:
            assert (code_dir / f"{module}.py").is_file(), (
                f"{name} is missing its vendored {module}.py"
            )


@pytest.mark.unit
@pytest.mark.parametrize(
    "template_path,name,body,code_dir",
    list(_cases()),
    ids=lambda v: v.name if isinstance(v, Path) else (v if isinstance(v, str) else ""),
)
def test_a_function_with_an_idp_common_layer_matches_the_layer_architecture(
    template_path, name, body, code_dir
):
    """
    A layer built for one architecture on a function of another deploys CLEANLY and
    then fails at import: `No module named 'pydantic_core._pydantic_core'`, because
    pydantic_core is a compiled extension. Nothing in CloudFormation or cfn-lint
    objects — the layer declares no CompatibleArchitectures, so Lambda accepts the
    attachment and the mismatch only shows up at runtime.

    Found live: the feature-platform template's Globals default every function to
    arm64, and applyFeatureConfigPreset inherited that while carrying the x86_64
    base layer. The preset applied but its revision was silently not recorded.
    """
    if not _has_layer(body):
        return
    declared = (body.get("Properties") or {}).get("Architectures")
    effective = declared or _globals_architectures(_parsed(template_path)) or [LAYER_ARCH]
    assert effective == [LAYER_ARCH], (
        f"{name} ({template_path.relative_to(REPO_ROOT)}) attaches an idp_common "
        f"layer but runs on {effective}; the layers are built {LAYER_ARCH}. Pin "
        f"`Architectures: [{LAYER_ARCH}]` on the function — it will otherwise "
        f"deploy and then fail at import."
    )
