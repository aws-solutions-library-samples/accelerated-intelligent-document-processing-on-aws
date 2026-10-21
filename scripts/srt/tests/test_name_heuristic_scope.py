# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one exemption in the SRT gate that is a scope decision rather than a list.

``NAME_HEURISTIC_EXEMPT`` stops Bandit's B105/B106 identifier-name heuristics from
gating in test code that no deployment artifact is built from, and nowhere else
(#1086). Its members are not a list of paths somebody wrote down; they are whatever
``ci_paths.is_test_only_path`` answers True for, which is why the ratchet here is
universe closure rather than staleness: there is no entry to go stale, and the
question worth asking is whether any path is demoted that should not be.

**The assertion that carries that ratchet has to be able to fail, and the obvious
way of writing it cannot.** Filtering the demoted set through a predicate derived
from the classifier makes the result empty by construction rather than by the state
of the tree: it holds for any tree, so it measures nothing. The closure assertion
below is therefore stated against an *independent* fact — the `CodeUri` and
`ContentUri` directories declared by the templates, parsed from the templates — and
it fails today against the shape-only classifier, which is what makes it a test.

That independence is the substance of the fix, not a technicality. `sam build`
copies a `CodeUri` directory verbatim; nothing in this tree prunes it. So
``src/lambda/queue_processor/test_reconcile_counter.py`` and
``src/lambda/test_file_copier/test_index.py`` ship into a customer's account
alongside the handlers beside them, and a classifier reading path shape alone
demoted 99 such files while correctly keeping their `index.py` neighbours gating.

Why a per-resource closure assertion was added
----------------------------------------------
The closure assertion described above is stated over *demoted paths*, and it
passes whenever the packaged set happens to contain the trees that hold a
test-shaped file. That made it green on a tree where the packaged set was missing
15 of the 16 Lambda source directories in ``patterns/unified/src/``, because none
of those 15 held a test-shaped ``.py`` — correct behaviour resting on a premise
that was false for the directories it was most needed on
([#1389](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1389)).

The assertions at the bottom of this module close that from the other side:
the universe is every function and layer **resource** every content-discovered
template declares, and each one whose code comes from a local directory must have
at least one such directory in the packaged set. A count pin could not have caught
the gap — the count was never wrong, the set was — which is why this is a closure
assertion and the pin above is kept only as a non-vacuity floor.

The parses here are deliberately a second implementation rather than a call into
``ci_paths``. Attribution computed with the module's own helpers would hold for any
tree and measure nothing, the same vacuity the paragraph above describes. Whether
each **template** names a source tree the **buildspec** also names is a different
question, and it is asserted in
``patterns/unified/tests/test_expected_images_match_the_buildspec.py``.
"""

from __future__ import annotations

import functools
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

SRT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SRT_DIR.parent.parent
sys.path.insert(0, str(SRT_DIR))

from ci_paths import (  # noqa: E402
    NAME_HEURISTIC_EXEMPT,
    is_test_only_path,
    packaged_source_dirs,
    partition_by_name_heuristic_scope,
)

pytestmark = pytest.mark.unit

#: How many directories the templates declare as a packaged source today. Pinned so
#: that a discovery regression — a changed key name, a parse that silently yields
#: nothing, a template that stops being found — fails here instead of quietly
#: restoring the shape-only behaviour this exemption was narrowed to avoid. Bump it
#: deliberately when a Lambda is added or removed.
#:
#: It is a non-vacuity floor on the discovery and nothing more: it stayed correct at
#: 120 throughout the period when the set was missing 15 container-built source
#: trees, because the count was never wrong — the set was. The per-resource closure
#: assertions at the bottom of this module are what hold the set itself.
EXPECTED_PACKAGED_DIRS = 138

#: CloudFormation resource types whose code comes from somewhere, and so whose
#: packaging this gate has an opinion about.
FUNCTION_RESOURCE_TYPES = frozenset(
    {
        "AWS::Serverless::Function",
        "AWS::Lambda::Function",
        "AWS::Serverless::LayerVersion",
        "AWS::Lambda::LayerVersion",
    }
)

#: The shell assignment a container buildspec uses to name one image's source tree.
#: Unanchored, because these lines are YAML list items and a pattern anchored to
#: leading whitespace plus ``export`` matches none of them and reports zero.
_EXPORT_RE = re.compile(r'export\s+FUNCTION_[A-Za-z0-9_]+="(?P<path>[^"]+)"')

#: The ``-f`` argument of a ``docker build`` / ``docker buildx build`` command.
_DOCKER_BUILD_FILE_RE = re.compile(
    r"docker\s+(?:buildx\s+)?build\b[^\n]*?-f\s+(?P<path>[^\s\\]+)"
)

#: A ``Dockerfile`` instruction naming paths from the build context.
_DOCKERFILE_COPY_RE = re.compile(r"^\s*COPY\s+(?P<args>.+)$", re.IGNORECASE)

#: This repository's single CloudFormation-tolerant YAML loader, read by path for the
#: three reasons ``test_expected_images_match_the_buildspec.py`` gives: an editable
#: install can resolve ``idp_sdk`` to a different checkout, ``idp_sdk._core`` is a
#: banned direct import outside a listed set of directories, and a repository gate
#: should not need an editable install to run at all.
_CFN_YAML = PROJECT_ROOT / "lib" / "idp_sdk" / "idp_sdk" / "_core" / "cfn_yaml.py"


def _tracked(pattern: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", pattern],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return [p for p in result.stdout.split("\0") if p]


@pytest.fixture(scope="module")
def packaged() -> frozenset[str]:
    dirs = packaged_source_dirs(PROJECT_ROOT)
    assert dirs is not None, "packaged source directories could not be established"
    return dirs


#: Shipped code whose path *looks* like test code. The first three are handlers; the
#: last two are test files that `sam build` copies into the artifact beside one.
SHIPPED_BUT_TEST_SHAPED = (
    "src/lambda/test_file_copier/index.py",
    "nested/api-resolvers/src/lambda/test_runner/index.py",
    "nested/api-resolvers/src/lambda/test_set_resolver/index.py",
    "src/lambda/queue_processor/test_reconcile_counter.py",
    "src/lambda/test_file_copier/test_index.py",
)

#: Test code in each shape the classifier recognises, none of it packaged.
TEST_ONLY_SAMPLES = (
    "lib/idp_common_pkg/tests/unit/test_utils.py",
    "lib/idp_common_pkg/tests/conftest.py",
    "scripts/srt/tests/test_name_heuristic_scope.py",
)


def test_the_packaged_set_is_discovered_and_its_size_is_pinned(
    packaged: frozenset[str],
) -> None:
    """Non-vacuity for the discovery itself: an empty set would demote everything."""
    assert len(packaged) == EXPECTED_PACKAGED_DIRS, (
        f"templates declare {len(packaged)} packaged source directories, pinned at "
        f"{EXPECTED_PACKAGED_DIRS}. If a Lambda was added or removed, bump the pin. "
        "If the number collapsed, the discovery is broken and every test-shaped "
        "file inside a Lambda directory is being demoted again."
    )
    for rel in packaged:
        assert (PROJECT_ROOT / rel).is_dir(), f"{rel} is not a directory"


def test_an_unknowable_packaged_set_demotes_nothing() -> None:
    """The fail-closed direction. `None` means "cannot tell", never "nothing ships"."""
    assert not is_test_only_path("lib/idp_common_pkg/tests/unit/test_utils.py", None)


@pytest.mark.parametrize("rel", SHIPPED_BUT_TEST_SHAPED)
def test_a_test_shaped_path_that_ships_is_not_demoted(
    rel: str, packaged: frozenset[str]
) -> None:
    """Either half of the rule alone gets one of these wrong."""
    assert (PROJECT_ROOT / rel).is_file(), f"fixture path moved: {rel}"
    assert not is_test_only_path(rel, packaged), (
        f"{rel} is copied into a deployment artifact by `sam build` and would stop "
        "gating on a hardcoded credential if it were classified as test-only."
    )


@pytest.mark.parametrize("rel", TEST_ONLY_SAMPLES)
def test_each_recognised_test_shape_is_demoted(
    rel: str, packaged: frozenset[str]
) -> None:
    assert (PROJECT_ROOT / rel).is_file(), f"fixture path moved: {rel}"
    assert is_test_only_path(rel, packaged)


def test_nothing_demoted_is_inside_a_packaged_source_directory(
    packaged: frozenset[str],
) -> None:
    """The closure assertion, stated against the templates rather than the classifier.

    This is the one that fails on a shape-only classifier, and the reason it can
    fail is that neither side of the comparison is derived from the other: the
    demoted set comes from `is_test_only_path`, the packaged set from parsing
    `CodeUri`/`ContentUri` out of every content-discovered template.
    """
    tracked_python = _tracked("*.py")
    assert tracked_python, "git ls-files returned no Python files"

    demoted = [rel for rel in tracked_python if is_test_only_path(rel, packaged)]
    offenders = sorted(
        rel
        for rel in demoted
        if any(rel == d or rel.startswith(d.rstrip("/") + "/") for d in packaged)
    )
    assert not offenders, (
        f"{len(offenders)} demoted path(s) sit inside a directory a deployment "
        "artifact is built from, so a hardcoded credential in them would reach a "
        "customer's account without gating:\n  " + "\n  ".join(offenders[:20])
    )


def test_both_sides_of_the_classification_are_populated(
    packaged: frozenset[str],
) -> None:
    """A classifier that answers one way for everything is not a scope decision.

    All-False is indistinguishable from having no exemption; all-True is the
    repo-wide blinding this exemption exists to avoid. Both are measured against the
    tree rather than asserted.
    """
    tracked_python = _tracked("*.py")
    demoted = [rel for rel in tracked_python if is_test_only_path(rel, packaged)]
    gating = [rel for rel in tracked_python if not is_test_only_path(rel, packaged)]

    assert demoted, "no tracked file is demoted; the exemption is dead"
    assert gating, "every tracked file is demoted; the check is blinded"
    assert len(demoted) + len(gating) == len(tracked_python)

    # The library and the Lambda sources are what ships. Whatever the classifier
    # says about shape, nothing under a packaged tree may be demoted, and the
    # library's own module code must gate.
    assert not [
        rel for rel in demoted if rel.startswith("lib/idp_common_pkg/idp_common/")
    ]


def test_the_partition_demotes_only_these_checks_and_only_unpackaged_test_code(
    packaged: frozenset[str],
) -> None:
    """Nothing else may ride the exemption, and nothing is dropped."""
    issues = [
        {"check_id": "B105", "path": "lib/idp_common_pkg/tests/unit/test_x.py"},
        {"check_id": "b106", "path": "lib/idp_common_pkg/tests/conftest.py"},
        # Same checks, shipped path: still gating.
        {"check_id": "B105", "path": "lib/idp_common_pkg/idp_common/bedrock/client.py"},
        # Test-shaped, but inside a Lambda's CodeUri: still gating.
        {"check_id": "B105", "path": "src/lambda/test_file_copier/test_index.py"},
        # Test path, different check: still gating. A fixture that disables TLS
        # verification is not an identifier-name match.
        {"check_id": "B501", "path": "lib/idp_common_pkg/tests/unit/test_x.py"},
        # No path at all — a repo-wide observation. Fails closed.
        {"check_id": "B105", "path": None},
    ]

    gating, demoted = partition_by_name_heuristic_scope(issues, PROJECT_ROOT)

    assert [i["check_id"] for i in demoted] == ["B105", "b106"]
    assert len(gating) == 4
    assert len(gating) + len(demoted) == len(issues), "a finding was dropped"
    assert {i["path"] for i in demoted} == {
        "lib/idp_common_pkg/tests/unit/test_x.py",
        "lib/idp_common_pkg/tests/conftest.py",
    }
    assert packaged  # the fixture is what made the fourth case gate


def test_only_the_two_promoted_checks_are_in_scope() -> None:
    """The exemption covers the promotion, so it covers exactly what is promoted.

    B107 (a hardcoded password as a function *default*) is the third member of
    Bandit's hardcoded-password family and SRT does **not** promote it, so it
    arrives at its own LOW severity and never reaches the gate. Adding it here
    would exempt something nothing was gating on.
    """
    assert set(NAME_HEURISTIC_EXEMPT) == {"B105", "B106"}


# --------------------------------------------------------------------------- #
# per-resource closure: every resource whose code is a local directory has one
#
# Independent of `ci_paths` by construction. Every path below is resolved by the
# helpers in this module, so an attribution that agrees with the packaged set only
# because both came from the same parse cannot happen.
# --------------------------------------------------------------------------- #


@functools.lru_cache(maxsize=None)
def _cfn_loader() -> Any:
    spec = importlib.util.spec_from_file_location("_cfn_yaml_for_srt_gate", _CFN_YAML)
    assert spec is not None and spec.loader is not None, f"cannot load {_CFN_YAML}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _join(base: str, value: str) -> str | None:
    """``value`` resolved against ``base`` as a repo-relative POSIX path."""
    parts: list[str] = []
    for piece in f"{base}/{value}".split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(piece)
    return "/".join(parts) or None


def _candidates(value: str, base: str) -> list[str]:
    """The two places a build-relative path can mean, repository root first.

    A CodeBuild project's source is a zip whose root is this repository's root for
    ``patterns/unified`` and the extension's own directory for the data generator, so
    both spellings occur and whichever exists wins.
    """
    found: list[str] = []
    for candidate in (_join("", value), _join(base, value)):
        if candidate and candidate not in found:
            found.append(candidate)
    return found


@functools.lru_cache(maxsize=None)
def _tracked_set() -> frozenset[str]:
    return frozenset(_tracked("*"))


@functools.lru_cache(maxsize=None)
def _discovered_templates() -> tuple[str, ...]:
    """Every tracked file declaring ``AWSTemplateFormatVersion``, by content.

    The same rule ``scripts/discover_templates.sh`` uses for the cfn-lint and
    ARN-partition gates, and the same one ``packaged_source_dirs`` uses, so a new
    template is covered here without being named.
    """
    found = []
    for rel in sorted(_tracked_set()):
        if not rel.endswith((".yaml", ".yml", ".json", ".template")):
            continue
        try:
            text = (PROJECT_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "AWSTemplateFormatVersion" in text:
            found.append(rel)
    return tuple(found)


@functools.lru_cache(maxsize=None)
def _document(rel: str) -> dict:
    text = (PROJECT_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
    return _cfn_loader().load_cfn_yaml(text) or {}


def _values_for_key(node: Any, key: str) -> list[Any]:
    """Every value the document associates with ``key``, at any depth.

    Strings are not walked, so a ``BuildSpec:`` line inside an inline buildspec
    document cannot be mistaken for structure.
    """
    found: list[Any] = []
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                found.append(value)
            found.extend(_values_for_key(value, key))
    elif isinstance(node, list):
        for item in node:
            found.extend(_values_for_key(item, key))
    return found


def _buildspec_documents(template_rel: str) -> list[tuple[str, dict]]:
    """``(working directory, parsed buildspec)`` for each buildspec a template runs.

    Two shapes are live in this repository and both are read. A ``BuildSpec`` naming
    a file is loaded from disk and its commands run with that file's directory as
    the build root; a ``BuildSpec`` written inline is already a document, and its
    commands run with the template's own directory as the build root. Anything the
    loader does not yield as a string — a ``!Sub``, a mapping — names no file this
    gate can read and contributes nothing.
    """
    base = Path(template_rel).parent.as_posix()
    found: list[tuple[str, dict]] = []
    for value in _values_for_key(_document(template_rel), "BuildSpec"):
        if not isinstance(value, str):
            continue
        if "\n" in value:
            document = yaml.safe_load(value)
            root = base
        else:
            resolved = next(
                (
                    candidate
                    for candidate in _candidates(value, base)
                    if candidate in _tracked_set()
                    and (PROJECT_ROOT / candidate).is_file()
                ),
                None,
            )
            if resolved is None:
                continue
            document = yaml.safe_load(
                (PROJECT_ROOT / resolved).read_text(encoding="utf-8")
            )
            root = Path(resolved).parent.as_posix()
        if isinstance(document, dict):
            found.append((root, document))
    return found


def _commands(document: dict) -> list[str]:
    commands: list[str] = []
    for phase in (document.get("phases") or {}).values():
        if not isinstance(phase, dict):
            continue
        for command in phase.get("commands") or []:
            if isinstance(command, str):
                commands.append(command)
    return commands


def _dockerfile_context_dirs(dockerfile_rel: str) -> set[str]:
    """Directories a ``Dockerfile``'s ``COPY`` instructions read from the context.

    Read for **attribution only**, never added to the packaged set, and the
    difference is load-bearing. Both container Dockerfiles here ``COPY
    lib/idp_common_pkg`` into a *builder* stage, pip-install it, and then copy only
    the installed site-packages and the handler's own ``*.py`` into the final image —
    so that tree is a build input and not a shipped one, and putting it in the
    packaged set would make every file under ``lib/idp_common_pkg/tests/`` gate on an
    identifier name for no reason.
    """
    found: set[str] = set()
    try:
        text = (PROJECT_ROOT / dockerfile_rel).read_text(encoding="utf-8")
    except OSError:
        return found
    for line in text.splitlines():
        match = _DOCKERFILE_COPY_RE.match(line)
        if not match:
            continue
        args = [a for a in match.group("args").split() if not a.startswith("--")]
        # The last argument is the destination inside the image.
        for arg in args[:-1]:
            if arg.startswith(("/", "$")):
                continue
            value = arg.rstrip("/")
            if "*" in value or "?" in value:
                value = Path(value).parent.as_posix()
            candidate = _join("", value)
            if candidate and (PROJECT_ROOT / candidate).is_dir():
                found.add(candidate)
    return found


@functools.lru_cache(maxsize=None)
def _declared_image_trees(template_rel: str) -> frozenset[str]:
    """The Lambda source trees a template's container build declares it builds.

    One tree per image, named by the buildspec's ``FUNCTION_<name>`` exports. This is
    the set ``ci_paths`` reads, so it is the set the directory-granular assertion is
    stated over — every member of it ships inside an image.
    """
    found: set[str] = set()
    for root, document in _buildspec_documents(template_rel):
        for command in _commands(document):
            for match in _EXPORT_RE.finditer(command):
                value = match.group("path")
                if "$" in value:
                    continue
                for candidate in _candidates(value, root):
                    if (PROJECT_ROOT / candidate).is_dir():
                        found.add(candidate)
                        break
    return frozenset(found)


@functools.lru_cache(maxsize=None)
def _image_build_inputs(template_rel: str) -> frozenset[str]:
    """Every directory a template's container build reads, for attribution only.

    The exports above, plus the ``COPY`` sources of any ``Dockerfile`` named on a
    ``docker build`` command line — two routes, because the two live builds declare
    their inputs differently. The unified pattern passes each exported tree to one
    shared Dockerfile as a build argument, so the variables are the only statement of
    which trees are built; multi-doc discovery builds a single image whose handler
    tree is named only in its Dockerfile.

    A member of this set is **not** necessarily a tree that ships, which is why it is
    never added to the packaged set and why the directory-granular assertion below is
    stated over :func:`_declared_image_trees` instead. Both container Dockerfiles here
    ``COPY lib/idp_common_pkg`` into a *builder* stage, pip-install it, and copy only
    the installed site-packages and the handler's own ``*.py`` into the final image.
    """
    found = set(_declared_image_trees(template_rel))
    for root, document in _buildspec_documents(template_rel):
        for command in _commands(document):
            for match in _DOCKER_BUILD_FILE_RE.finditer(command):
                value = match.group("path")
                if "$" in value:
                    continue
                for candidate in _candidates(value, root):
                    if (PROJECT_ROOT / candidate).is_file():
                        found |= _dockerfile_context_dirs(candidate)
                        break
    return frozenset(found)


def _local_source_value(properties: dict) -> str | None:
    """The local directory a resource declares its code lives in, if it does.

    ``None`` is the computed answer for a resource that names no local directory at
    all: ``InlineCode`` carries the code in the template, and a ``Code`` or
    ``Content`` mapping names an S3 object. Neither is a decision written down
    anywhere — it is read off each resource.
    """
    for key in ("CodeUri", "ContentUri"):
        value = properties.get(key)
        if isinstance(value, str) and not value.startswith("s3://"):
            return value
    return None


def _function_resources() -> list[tuple[str, str, dict]]:
    """``(template, logical id, properties)`` for every function and layer resource."""
    found: list[tuple[str, str, dict]] = []
    for rel in _discovered_templates():
        resources = _document(rel).get("Resources")
        if not isinstance(resources, dict):
            continue
        for logical_id, body in resources.items():
            if not isinstance(body, dict):
                continue
            if body.get("Type") not in FUNCTION_RESOURCE_TYPES:
                continue
            properties = body.get("Properties")
            found.append(
                (rel, logical_id, properties if isinstance(properties, dict) else {})
            )
    return found


def test_the_resource_universe_holds_both_packaging_types() -> None:
    """Non-vacuity for the closure assertion below.

    Every way of making it vacuous is a quiet one: a loader that yields no
    ``Resources``, a type name that stops matching, a template discovery that finds
    nothing. Each would leave the assertion passing over an empty universe, which is
    exactly the state the gap it exists to catch would then be back in.
    """
    resources = _function_resources()
    assert len(_discovered_templates()) >= 10, _discovered_templates()
    assert len(resources) >= 100, f"only {len(resources)} function resources found"

    image = [r for r in resources if r[2].get("PackageType") == "Image"]
    local = [
        r
        for r in resources
        if r[2].get("PackageType") != "Image" and _local_source_value(r[2])
    ]
    assert len(image) >= 10, f"only {len(image)} image-built resources found: {image}"
    assert len(local) >= 50, f"only {len(local)} directory-sourced resources found"


def test_every_resource_whose_code_is_a_local_directory_has_one_packaged(
    packaged: frozenset[str],
) -> None:
    """The closure assertion, per resource rather than per demoted path.

    A resource reaches this assertion when its code comes from a directory in this
    checkout, which is computed from the resource and not listed anywhere. For a
    ``CodeUri``/``ContentUri`` resource that directory is named in the template, so
    attribution is exact. For a ``PackageType: Image`` resource the template names an
    ECR tag and nothing else, so attribution is **template-scoped**: the assertion is
    that the container build this template declares contributes at least one tree to
    the packaged set, not which of them this particular image came from. That bound is
    the reason the figures below differ — 16 image resources in the unified pattern
    share 15 source trees, because the shard runtime runs the extraction image.
    """
    unattributed: list[str] = []
    for template_rel, logical_id, properties in _function_resources():
        if properties.get("PackageType") == "Image":
            build_dirs = _image_build_inputs(template_rel)
            if not any(d in packaged for d in build_dirs):
                # The build's own trees are reported once per template by the
                # directory-granular assertion below; naming all of them against
                # each of its resources buries the resource names.
                unattributed.append(
                    f"{template_rel} {logical_id} (Image; none of the "
                    f"{len(build_dirs)} directory/-ies its container build reads "
                    "is in the packaged set)"
                )
            continue

        value = _local_source_value(properties)
        if value is None:
            continue
        resolved = _join(Path(template_rel).parent.as_posix(), value)
        if resolved not in packaged:
            unattributed.append(
                f"{template_rel} {logical_id} (declares {value!r}, resolved to "
                f"{resolved!r})"
            )

    assert not unattributed, (
        f"{len(unattributed)} function resource(s) take their code from a directory "
        "in this checkout that `ci_paths.packaged_source_dirs` does not know about. "
        "A test-shaped file inside one of those directories satisfies both halves of "
        "`is_test_only_path`, so a hardcoded-credential finding in it stops gating "
        "the SRT scan while still shipping to a customer's account:\n  "
        + "\n  ".join(unattributed)
    )


def test_every_tree_a_container_build_names_is_in_the_packaged_set(
    packaged: frozenset[str],
) -> None:
    """The same gap at directory granularity, which is how it was measured.

    Stated separately from the per-resource assertion because it is the stronger of
    the two where image resources are concerned: that one is satisfied by any one of
    a build's trees being packaged, while this one fails on each tree that is not.
    Fifteen of the sixteen trees under ``patterns/unified/src/`` failed it before
    ``packaged_source_dirs`` learned to read the buildspec.
    """
    missing: list[str] = []
    named: set[str] = set()
    for template_rel in _discovered_templates():
        for build_dir in sorted(_declared_image_trees(template_rel)):
            named.add(build_dir)
            if build_dir not in packaged:
                missing.append(f"{build_dir} (named by {template_rel}'s build)")

    assert named, (
        "no container build in this repository names a source tree this gate can "
        "resolve, so the assertion below holds over an empty set and measures "
        "nothing. Either the `BuildSpec` property stopped resolving or the "
        "`export FUNCTION_` parse did."
    )
    assert not missing, (
        f"{len(missing)} directory/-ies a container image is built from are absent "
        "from the packaged set, so a test-shaped file in any of them would be "
        "demoted out of the SRT gate while shipping inside the image:\n  "
        + "\n  ".join(missing)
    )
