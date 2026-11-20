#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Check the solution profile in ``solution-profile/`` (``make solution-profile-check``).

Two halves, and the first is the contract's own:

1. **The reference validator**, vendored unchanged at
   ``solution-profile/schema/v1/validate_profile.py``, run with ``--version-file``
   and, at a release, ``--release``.
2. **Cross-checks against the code**, so the profile cannot quietly fall behind the
   tree. Each derives its universe from the code and fails, naming the missing item
   and where it belongs, when the code has something the profile does not cover — or
   when the profile still tags something the code no longer has:

   * every pipeline hook point in ``idp_common/config/hook_point_reachability.py`` has
     exactly one extension point tagged ``hook-point:<point>``, and its ``modes`` are
     the modes that reach the point;
   * every preset directory in ``config_library/unified/`` is tagged
     ``preset:<dir>`` on a capability;
   * every file extension ``OcrService._detect_file_type`` routes is tagged
     ``format:<ext>`` on a capability, or on a limitation for a format that is routed
     and then refused;
   * every model a tracked template offers is tagged ``model:<id>`` on a capability;
   * the ``cloudformation`` deployment option's ``regions`` equal the Regions
     ``scripts/aws-release.sh`` publishes;
   * the latest releases in ``CHANGELOG.md`` each have a ``changes.yaml`` entry, and a
     release whose section carries an action item records at least one.

**The version.** The contract's version pattern is ``X.Y.Z`` with an optional
``-suffix`` or ``+suffix``. ``VERSION`` holds a PEP 440 version, which on ``develop``
is a development or release-candidate build such as ``0.6.16.dev1``, and the contract
rejects that form. So the validator is given the contract form of ``VERSION``
(``0.6.16-dev1``) in a temporary file; a final release is already in contract form
and is passed as ``VERSION`` itself. ``make version`` writes the same conversion into
the manifest through :func:`stamp_manifest_version`, so the two cannot disagree.

**Release rules.** ``--release`` is applied when ``VERSION`` is a final release
(``X.Y.Z``), when ``--release`` is given, or when ``SOLUTION_PROFILE_RELEASE`` is set
to an affirmative value. The release workflow sets it explicitly.

``--contract-only`` runs the first half alone, which is what the release workflow
does: the cross-checks have already run on every pull request by then. Both halves
need only PyYAML, jsonschema and ``git``, and every universe is read from the
``--repo-root`` given, never from the checkout this script happens to live in.
"""

from __future__ import annotations

import argparse
import ast
import functools
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = "solution-profile"
VALIDATOR = "solution-profile/schema/v1/validate_profile.py"
HOOK_REACHABILITY = "lib/idp_common_pkg/idp_common/config/hook_point_reachability.py"
PRESETS_DIR = "config_library/unified"
OCR_SERVICE = "lib/idp_common_pkg/idp_common/ocr/service.py"
RELEASE_SCRIPT = "scripts/aws-release.sh"
CHANGELOG = "CHANGELOG.md"
UPDATE_SKILL = ".claude/skills/update-solution-profile.md"

#: How many of the newest CHANGELOG releases ``changes.yaml`` must cover. The
#: contract says "keep at least the last ten releases".
RELEASES_COVERED = 10

#: Service-tier and context-window suffixes that select a variant of one model rather
#: than a different model, so the profile names the model once.
MODEL_VARIANT_SUFFIX = re.compile(r"(?::(?:flex|priority|1m))+$")

PEP440 = re.compile(r"^(\d+\.\d+\.\d+)(?:\.?([0-9A-Za-z][0-9A-Za-z.]*))?$")
AFFIRMATIVE = {"1", "true", "yes", "y", "on"}


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------


def contract_version(pep440: str) -> str:
    """The contract's form of a PEP 440 version: ``0.6.16.dev1`` -> ``0.6.16-dev1``.

    A final release is returned unchanged. Raises ``ValueError`` for anything that is
    not ``X.Y.Z`` optionally followed by a suffix, so a malformed ``VERSION`` is
    reported rather than converted into something that happens to validate.
    """
    match = PEP440.match(pep440.strip())
    if not match:
        raise ValueError(f"not an X.Y.Z[suffix] version: {pep440.strip()!r}")
    base, suffix = match.groups()
    return base if not suffix else f"{base}-{suffix}"


def is_final_release(pep440: str) -> bool:
    """True for ``X.Y.Z`` with no development, pre-release or post-release suffix."""
    return contract_version(pep440) == pep440.strip()


def stamp_manifest_version(manifest: Path, pep440: str) -> bool:
    """Rewrite the manifest's top-level ``version:`` line; return whether it changed.

    A text edit rather than a YAML round trip, so the manifest's comments and key
    order survive. Exactly one ``version:`` line must exist.
    """
    text = manifest.read_text(encoding="utf-8")
    new, count = re.subn(
        r"^version: .*$",
        f"version: {contract_version(pep440)}",
        text,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise ValueError(
            f"{manifest}: expected one top-level 'version:' line, found {count}"
        )
    manifest.write_text(new, encoding="utf-8")
    return new != text


# ---------------------------------------------------------------------------
# Universes derived from the code
# ---------------------------------------------------------------------------


def hook_point_modes(repo_root: Path) -> dict[str, set[str]]:
    """hook point -> the processing modes that reach it.

    Loads the generated module by file path, so the answer is about this checkout and
    does not depend on which ``idp_common`` the interpreter would import.
    """
    path = repo_root / HOOK_REACHABILITY
    spec = importlib.util.spec_from_file_location("_hook_point_reachability", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    modes: dict[str, set[str]] = {point: set() for point in module.ALL_HOOK_POINTS}
    for mode, points in module.HOOK_POINTS_BY_MODE.items():
        for point in points:
            modes.setdefault(point, set()).add(mode)
    return modes


def preset_names(repo_root: Path) -> set[str]:
    """Every configuration preset: a directory under ``config_library/unified/``."""
    root = repo_root / PRESETS_DIR
    return {
        p.name for p in root.iterdir() if p.is_dir() and (p / "config.yaml").is_file()
    }


def routed_file_extensions(repo_root: Path) -> set[str]:
    """Every extension ``OcrService._detect_file_type`` routes by name.

    Read from the function's AST: each string constant compared against ``ext`` with
    ``==`` or ``in``. That is the function's whole extension vocabulary, so a format
    added there is seen here without anyone listing it.
    """
    tree = ast.parse((repo_root / OCR_SERVICE).read_text(encoding="utf-8"))
    func = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_detect_file_type"
        ),
        None,
    )
    if func is None:
        raise RuntimeError(f"{OCR_SERVICE}: _detect_file_type not found")
    found: set[str] = set()
    for node in ast.walk(func):
        if not (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)):
            continue
        if node.left.id != "ext":
            continue
        for comparator in node.comparators:
            items = (
                comparator.elts
                if isinstance(comparator, (ast.List, ast.Tuple, ast.Set))
                else [comparator]
            )
            for item in items:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    found.add(item.value)
    if not found:
        raise RuntimeError(
            f"{OCR_SERVICE}: _detect_file_type compares no extension constants"
        )
    return found


def release_regions(repo_root: Path) -> set[str]:
    """The Regions the release script publishes, from its ``--region`` arguments."""
    regions: set[str] = set()
    for line in (repo_root / RELEASE_SCRIPT).read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        regions.update(re.findall(r"--region\s+([a-z0-9-]+)", line))
    if not regions:
        raise RuntimeError(f"{RELEASE_SCRIPT}: no --region arguments found")
    return regions


# A Bedrock model or inference-profile id as the templates write them: an optional
# Region/geo prefix, then ``provider.model``, where the provider starts with a letter
# and the model contains one (so ``0.7`` is not an id). The same shape the
# model-surface gate (scripts/tests/test_model_surface_consistency.py) uses;
# test_solution_profile.py asserts the two walks agree on this tree.
MODEL_ID = re.compile(
    r"^(?:(?:us|eu|apac|ca|global|us-gov)\.)?"
    r"[a-z][a-z0-9-]+\."
    r"(?=[a-z0-9.:_-]*[a-z])[a-z0-9][a-z0-9.:_-]*$"
)
RETIRED_MODELS_MODULE = "lib/idp_common_pkg/idp_common/config/retired_models.py"


def _cfn_tag(loader: Any, tag_suffix: str, node: Any) -> dict[str, Any]:
    """Read a CloudFormation short-form tag (``!Ref``, ``!Sub`` ...) as plain data."""
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


# libyaml when available: parsing every template in pure Python is most of this
# check's run time.
_CfnBase: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _CfnLoader(_CfnBase):  # type: ignore[misc,valid-type]
    """A safe loader that tolerates CloudFormation short-form tags."""


_CfnLoader.add_multi_constructor("!", _cfn_tag)


def tracked_templates(repo_root: Path) -> list[str]:
    """CloudFormation templates tracked in ``repo_root``, discovered by content."""
    out = subprocess.run(  # noqa: S603 - fixed git argv
        ["git", "ls-files", "*.yaml", "*.yml"],  # noqa: S607 - git from PATH
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = []
    for rel in out.splitlines():
        path = repo_root / rel
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "AWSTemplateFormatVersion" in text:
            found.append(rel)
    return found


def _offered(node: Any, in_parameters: bool, acc: set[str]) -> None:
    """Collect model ids a template offers: enum/AllowedValues entries, and the
    ``Default`` of a CloudFormation Parameter (a model received without choosing)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("enum", "AllowedValues") and isinstance(value, list):
                acc.update(v for v in value if isinstance(v, str) and MODEL_ID.match(v))
            if (
                key == "Default"
                and in_parameters
                and isinstance(value, str)
                and MODEL_ID.match(value)
            ):
                acc.add(value)
            _offered(value, in_parameters or key == "Parameters", acc)
    elif isinstance(node, list):
        for item in node:
            _offered(item, in_parameters, acc)


@functools.lru_cache(maxsize=None)
def selectable_models(repo_root: Path) -> frozenset[str]:
    """Every model a template tracked in ``repo_root`` offers, as a base id.

    The Region/geo prefix is stripped with ``base_model_id`` from ``repo_root``'s own
    ``idp_common/config/retired_models.py`` (loaded by path, so the answer is about
    that checkout), and service-tier and context-window suffixes are dropped.
    """
    spec = importlib.util.spec_from_file_location(
        "_retired_models", repo_root / RETIRED_MODELS_MODULE
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {RETIRED_MODELS_MODULE}")
    retired = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(retired)
    offered: set[str] = set()
    for rel in tracked_templates(repo_root):
        text = (repo_root / rel).read_text(encoding="utf-8")
        doc = yaml.load(text, Loader=_CfnLoader)  # noqa: S506  # nosec B506 - a SafeLoader
        _offered(doc, False, offered)
    return frozenset(
        MODEL_VARIANT_SUFFIX.sub("", retired.base_model_id(m)) for m in offered
    )


def changelog_releases(repo_root: Path) -> list[tuple[str, str]]:
    """``(version, section text)`` for each ``## [x.y.z]`` section, newest first."""
    text = (repo_root / CHANGELOG).read_text(encoding="utf-8")
    heads = list(re.finditer(r"^## \[(\d+\.\d+\.\d+)\]\s*$", text, flags=re.MULTILINE))
    out = []
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append((head.group(1), text[head.end() : end]))
    return out


ACTION_MARKER = re.compile(r"\*\*Action(?: Required)?[^*]*\*\*")


def has_action_item(section: str) -> bool:
    """Whether a CHANGELOG section carries an action that is not simply "none".

    "**Action:** none." is the CHANGELOG's way of saying nothing is needed, so it does
    not count; "none for existing stacks, except …" names an exception and does.
    """
    for marker in ACTION_MARKER.finditer(section):
        inline = marker.group(0).strip("*").split(":", 1)
        after = (inline[1] if len(inline) > 1 else "") + section[
            marker.end() : marker.end() + 400
        ]
        after = after.lstrip(" *:").lower()
        sentence = re.split(r"(?<=[.;])\s", after, maxsplit=1)[0]
        if not (after.startswith("none") and "except" not in sentence):
            return True
    return False


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


@dataclass
class Profile:
    """The parsed profile files a cross-check reads."""

    capabilities: list[dict[str, Any]] = field(default_factory=list)
    extension_points: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[dict[str, Any]] = field(default_factory=list)
    deployment_options: list[dict[str, Any]] = field(default_factory=list)
    releases: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, profile_dir: Path) -> Profile:
        def read(name: str, key: str) -> list[dict[str, Any]]:
            path = profile_dir / name
            if not path.is_file():
                return []
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            items = data.get(key) or []
            return [item for item in items if isinstance(item, dict)]

        return cls(
            capabilities=read("capabilities.yaml", "capabilities"),
            extension_points=read("extension-points.yaml", "extension_points"),
            limitations=read("limitations.yaml", "limitations"),
            deployment_options=read("deployment.yaml", "options"),
            releases=read("changes.yaml", "releases"),
        )


def _tagged(items: Iterable[dict[str, Any]], prefix: str) -> dict[str, list[str]]:
    """``value -> [item ids]`` for every ``<prefix><value>`` tag on ``items``."""
    out: dict[str, list[str]] = {}
    for item in items:
        for tag in item.get("tags") or []:
            if isinstance(tag, str) and tag.startswith(prefix):
                out.setdefault(tag[len(prefix) :], []).append(str(item.get("id")))
    return out


def check_hook_points(profile: Profile, universe: dict[str, set[str]]) -> list[str]:
    problems = []
    tagged = _tagged(profile.extension_points, "hook-point:")
    by_id = {str(e.get("id")): e for e in profile.extension_points}
    for point in sorted(universe):
        ids = tagged.get(point, [])
        if not ids:
            problems.append(
                f"extension-points.yaml: no extension point is tagged 'hook-point:{point}'. "
                f"{HOOK_REACHABILITY} lists '{point}' as a pipeline hook point; add a "
                f"category 'hook' item for it with modes {sorted(universe[point])}"
            )
            continue
        if len(ids) > 1:
            problems.append(
                f"extension-points.yaml: 'hook-point:{point}' is tagged on more than one item: {ids}"
            )
        for item_id in ids:
            item = by_id[item_id]
            modes = set(item.get("modes") or [])
            if modes != universe[point]:
                problems.append(
                    f"extension-points.yaml: {item_id}: modes {sorted(modes)} do not match the modes that "
                    f"reach hook point '{point}' in {HOOK_REACHABILITY}: {sorted(universe[point])}"
                )
            if item.get("category") != "hook":
                problems.append(
                    f"extension-points.yaml: {item_id}: a hook point's category must be 'hook'"
                )
    for point in sorted(set(tagged) - set(universe)):
        problems.append(
            f"extension-points.yaml: {tagged[point]} tagged 'hook-point:{point}', which "
            f"{HOOK_REACHABILITY} does not list; retire the item or fix the tag"
        )
    return problems


def check_presets(profile: Profile, universe: set[str]) -> list[str]:
    tagged = _tagged(profile.capabilities, "preset:")
    problems = [
        f"capabilities.yaml: no capability is tagged 'preset:{name}'. {PRESETS_DIR}/{name} is a "
        f"configuration preset; add the tag to capability configuration-presets (and to the "
        f"capability it demonstrates)"
        for name in sorted(universe - set(tagged))
    ]
    problems += [
        f"capabilities.yaml: {tagged[name]} tagged 'preset:{name}', but {PRESETS_DIR}/{name} does not exist"
        for name in sorted(set(tagged) - universe)
    ]
    return problems


def check_file_formats(profile: Profile, universe: set[str]) -> list[str]:
    supported = _tagged(profile.capabilities, "format:")
    refused = _tagged(profile.limitations, "format:")
    problems = [
        f"capabilities.yaml: no capability is tagged 'format:{ext}'. {OCR_SERVICE} "
        f"_detect_file_type routes '.{ext}'; tag it on capability input-file-formats, or, "
        f"if the format is routed and then refused, on a limitation"
        for ext in sorted(universe - set(supported) - set(refused))
    ]
    problems += [
        f"'format:{ext}' is tagged both as supported {supported[ext]} and as refused {refused[ext]}"
        for ext in sorted(set(supported) & set(refused))
    ]
    problems += [
        f"'format:{ext}' is tagged on {(supported.get(ext, []) + refused.get(ext, []))}, but "
        f"_detect_file_type in {OCR_SERVICE} does not route '.{ext}'"
        for ext in sorted((set(supported) | set(refused)) - universe)
    ]
    return problems


def check_models(profile: Profile, universe: set[str]) -> list[str]:
    tagged = _tagged(profile.capabilities, "model:")
    problems = [
        f"capabilities.yaml: no capability is tagged 'model:{model}'. A tracked template offers "
        f"it as a selectable model; add the tag to capability bedrock-model-choice"
        for model in sorted(universe - set(tagged))
    ]
    problems += [
        f"capabilities.yaml: {tagged[model]} tagged 'model:{model}', which no tracked template offers any more"
        for model in sorted(set(tagged) - universe)
    ]
    return problems


def check_regions(profile: Profile, universe: set[str]) -> list[str]:
    option = next(
        (o for o in profile.deployment_options if o.get("id") == "cloudformation"), None
    )
    if option is None:
        return ["deployment.yaml: no option with id 'cloudformation'"]
    listed = set(option.get("regions") or [])
    problems = []
    if universe - listed:
        problems.append(
            f"deployment.yaml: cloudformation: regions lack {sorted(universe - listed)}, which "
            f"{RELEASE_SCRIPT} publishes a release to"
        )
    if listed - universe:
        problems.append(
            f"deployment.yaml: cloudformation: regions list {sorted(listed - universe)}, which "
            f"{RELEASE_SCRIPT} does not publish to"
        )
    return problems


def check_changes(profile: Profile, releases: list[tuple[str, str]]) -> list[str]:
    entries = {str(r.get("version")): r for r in profile.releases}
    problems = []
    for version, section in releases[:RELEASES_COVERED]:
        entry = entries.get(version)
        if entry is None:
            problems.append(
                f"changes.yaml: no entry for {version}, one of the latest {RELEASES_COVERED} releases in {CHANGELOG}"
            )
        elif has_action_item(section) and not entry.get("action_required"):
            problems.append(
                f"changes.yaml: {version}: {CHANGELOG} lists an action item for this release, "
                f"but action_required is empty"
            )
    return problems


MARKDOWN_LINKS = "scripts/check_markdown_links.py"


def _evidence(node: Any, item_id: str = "") -> Iterable[tuple[str, str]]:
    """Yield ``(enclosing item id, evidence entry)`` for every ``evidence`` list."""
    if isinstance(node, dict):
        here = str(node.get("id", item_id))
        for key, value in node.items():
            if key == "evidence" and isinstance(value, list):
                yield from ((here, v) for v in value if isinstance(v, str))
            else:
                yield from _evidence(value, here)
    elif isinstance(node, list):
        for item in node:
            yield from _evidence(item, item_id)


@functools.lru_cache(maxsize=None)
def _anchors_of(repo_root: Path, rel: str) -> frozenset[str]:
    """The fragment ids ``rel`` exposes, read by the Markdown link gate's own reader."""
    name = "_check_markdown_links"
    links = sys.modules.get(name)
    if links is None:
        spec = importlib.util.spec_from_file_location(name, repo_root / MARKDOWN_LINKS)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {MARKDOWN_LINKS}")
        links = importlib.util.module_from_spec(spec)
        sys.modules[name] = links  # its dataclasses resolve their module through it
        spec.loader.exec_module(links)
    return frozenset(links.anchors((repo_root / rel).read_text(encoding="utf-8")))


def check_evidence_anchors(repo_root: Path, profile_dir: Path) -> list[str]:
    """Every ``<path>.md#<anchor>`` evidence entry names a heading that file has.

    The validator checks that an evidence path exists, not that its anchor does, and
    an anchor that resolves nowhere sends a reader to the top of a long page. Slugs
    come from ``scripts/check_markdown_links.py``, which matches github-slugger (what
    GitHub and the docs site use), so there is one slugifier in the repository.
    """
    problems = []
    for path in sorted(profile_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for item_id, entry in _evidence(data):
            if entry.startswith("https://") or "#" not in entry:
                continue
            rel, fragment = entry.split("#", 1)
            if not rel.endswith(".md") or not (repo_root / rel).is_file():
                continue
            headings = _anchors_of(repo_root, rel)
            if fragment not in headings and unquote(fragment) not in headings:
                problems.append(
                    f"{path.name}: {item_id}: evidence anchor '#{fragment}' matches "
                    f"no heading in {rel}"
                )
    return problems


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

Universe = Callable[[Path], Any]
CROSS_CHECKS: list[tuple[str, Universe, Callable[[Profile, Any], list[str]]]] = [
    ("hook points", hook_point_modes, check_hook_points),
    ("presets", preset_names, check_presets),
    ("file formats", routed_file_extensions, check_file_formats),
    ("selectable models", selectable_models, check_models),
    ("launch Regions", release_regions, check_regions),
    ("releases", changelog_releases, check_changes),
]


def cross_check(repo_root: Path, profile_dir: Path) -> list[str]:
    profile = Profile.load(profile_dir)
    problems: list[str] = []
    for _name, derive, check in CROSS_CHECKS:
        problems += check(profile, derive(repo_root))
    return problems + check_evidence_anchors(repo_root, profile_dir)


@functools.lru_cache(maxsize=None)
def _validator(path: Path) -> Any:
    """The vendored reference validator, loaded once per process from its file."""
    spec = importlib.util.spec_from_file_location("_validate_profile", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_validator(
    repo_root: Path, profile_dir: Path, version_file: Path, release: bool
) -> int:
    """Run the reference validator's own ``main`` on the profile; return its status.

    In process rather than as a subprocess: importing jsonschema costs most of a
    second, and the check runs in every lint and in several tests.
    """
    argv = [
        str(profile_dir),
        "--repo-root",
        str(repo_root),
        "--version-file",
        str(version_file),
    ]
    if release:
        argv.append("--release")
    status = _validator(repo_root / VALIDATOR).main(argv)
    sys.stdout.flush()
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--profile-dir", type=Path, help="default: <repo-root>/solution-profile"
    )
    parser.add_argument(
        "--release", action="store_true", help="apply the contract's release rules"
    )
    parser.add_argument(
        "--contract-only", action="store_true", help="run the validator only"
    )
    parser.add_argument(
        "--check-version",
        metavar="PEP440",
        help="exit non-zero unless this version has a contract form; writes nothing (make version runs it first)",
    )
    parser.add_argument(
        "--stamp-version",
        metavar="PEP440",
        help="write this version, in contract form, into the manifest and exit (used by `make version`)",
    )
    args = parser.parse_args(argv)
    repo_root: Path = args.repo_root.resolve()
    profile_dir: Path = (args.profile_dir or repo_root / PROFILE_DIR).resolve()

    if args.check_version:
        try:
            print(f"solution profile version: {contract_version(args.check_version)}")
        except ValueError as exc:
            print(
                f"ERROR: the solution profile cannot carry this version: {exc}. Use X.Y.Z with an optional suffix, e.g. 0.6.16 or 0.6.16.dev1."
            )
            return 1
        return 0

    if args.stamp_version:
        try:
            changed = stamp_manifest_version(
                profile_dir / "solution-profile.yaml", args.stamp_version
            )
        except ValueError as exc:
            print(f"ERROR: could not stamp the solution profile version: {exc}")
            return 1
        print(
            f"solution-profile.yaml: version {contract_version(args.stamp_version)}{'' if changed else ' (unchanged)'}"
        )
        return 0

    pep440 = (repo_root / "VERSION").read_text(encoding="utf-8").strip()
    try:
        version = contract_version(pep440)
    except ValueError as exc:
        print(f"VERSION: {exc}")
        return 1
    release = (
        args.release
        or os.environ.get("SOLUTION_PROFILE_RELEASE", "").strip().lower() in AFFIRMATIVE
        or is_final_release(pep440)
    )
    print(
        f"Solution profile: VERSION {pep440} -> profile version {version}; "
        f"release rules {'ON' if release else 'off (development version)'}",
        flush=True,
    )

    with tempfile.TemporaryDirectory() as tmp:
        if version == pep440:
            version_file = repo_root / "VERSION"
        else:
            version_file = Path(tmp) / "VERSION"
            version_file.write_text(version + "\n", encoding="utf-8")
        status = run_validator(repo_root, profile_dir, version_file, release)
    if status != 0 and release:
        print(
            f"\nThis is a release build (VERSION {pep440}). Follow {UPDATE_SKILL}: review the "
            f"curated files in {PROFILE_DIR}/ against the CHANGELOG section for {pep440}, add a "
            f"changes.yaml entry for {pep440}, and set reviewed_for_version: {pep440} in "
            f"{PROFILE_DIR}/solution-profile.yaml."
        )
    if args.contract_only:
        return status

    problems = cross_check(repo_root, profile_dir)
    for problem in problems:
        print(problem)
    print(
        "cross-checks: " + ("passed" if not problems else f"{len(problems)} problem(s)")
    )
    return 1 if status != 0 or problems else 0


if __name__ == "__main__":
    sys.exit(main())
