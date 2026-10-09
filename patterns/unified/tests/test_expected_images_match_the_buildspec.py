# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``DockerBuildRun``'s ``ExpectedImages`` against the images the build pushes.

The custom resource waits for every tag in ``ExpectedImages`` to appear in ECR
before the Lambda functions that pull them are created. The tags are produced by
a loop in the buildspec the project points at, and the two lists are maintained
in different files with nothing connecting them.

An entry in ``ExpectedImages`` that the buildspec does not build therefore waits
forever. That is not a hypothetical shape: it is exactly the end state of issue
[#1310](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1310)
(a build that reported success without pushing a tag) and of
[#1336](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1336)
(an unbounded scan wait) -- an hour of polling ending in ``CloudFormation did not
receive a response from your Custom Resource``, which names the custom resource
and not the missing image. Both of those were fixed at their own cause; a rename
or a one-sided addition here reproduces the same hang through a third route, and
the scan wait's bound does not help because presence polling is deliberately
unbounded.

Only the subset direction is asserted. An image the buildspec builds and
``ExpectedImages`` does not name is harmless: nothing waits for it, and two such
images exist today by design.

Both lists are parsed from the files rather than restated here, and both parses
are checked for non-vacuity, because a parse that silently finds nothing would
make the subset assertion trivially true.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_PATTERN_DIR = Path(__file__).resolve().parents[1]
_TEMPLATE = _PATTERN_DIR / "template.yaml"


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def _template_text() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


def _buildspec_path() -> Path:
    """The buildspec the DockerBuildProject actually points at.

    Read from the template rather than hardcoded, so that switching the project
    to one of the sibling buildspecs moves this test with it instead of leaving
    it asserting against a file nothing runs.
    """
    matches = re.findall(r"^\s*BuildSpec:\s*(\S+)\s*$", _template_text(), re.MULTILINE)
    assert matches, "no BuildSpec property found in patterns/unified/template.yaml"
    assert len(set(matches)) == 1, f"more than one BuildSpec in the template: {matches}"

    repo_root = _PATTERN_DIR.parents[1]
    path = repo_root / matches[0]
    assert path.is_file(), f"BuildSpec names {matches[0]}, which does not exist"
    return path


def _expected_images() -> list[str]:
    """The ``ExpectedImages`` list from the DockerBuildRun resource.

    Parsed as text rather than through a YAML loader: the template is full of
    CloudFormation short tags, and adding another ``!``-aware loader to this
    repository would put it in scope for the safety tripwire in
    ``scripts/sdlc/tests/test_cfn_loader_safety.py``. The block is a plain list of
    quoted scalars, so a narrow scan is enough and the non-vacuity assertions
    below cover a parse that drifts.
    """
    lines = _template_text().splitlines()
    for position, line in enumerate(lines):
        match = re.match(r"^(\s*)ExpectedImages:\s*$", line)
        if not match:
            continue
        indent = len(match.group(1))
        images = []
        for following in lines[position + 1 :]:
            if not following.strip():
                continue
            item = re.match(r'^(\s*)-\s*"?([A-Za-z0-9._-]+)"?\s*$', following)
            if item and len(item.group(1)) > indent:
                images.append(item.group(2))
                continue
            break
        return images
    pytest.fail("no ExpectedImages list found in patterns/unified/template.yaml")


def _buildspec_commands(buildspec: Path) -> list[str]:
    """Every command string in every phase of the buildspec.

    ``yaml.safe_load`` is correct here: a buildspec carries no CloudFormation
    intrinsics, so no custom loader is involved.
    """
    document = yaml.safe_load(buildspec.read_text(encoding="utf-8"))
    commands: list[str] = []
    for phase in (document.get("phases") or {}).values():
        for command in phase.get("commands") or []:
            if isinstance(command, str):
                commands.append(command)
    return commands


def _image_name_for(function_path: str) -> str:
    """The tag the buildspec derives: ``basename | sed 's/_/-/g'``."""
    return Path(function_path).name.replace("_", "-")


def _exported_functions(commands: list[str]) -> dict[str, str]:
    exports: dict[str, str] = {}
    for command in commands:
        for name, path in re.findall(
            r'export\s+(FUNCTION_[A-Za-z0-9_]+)="([^"]+)"', command
        ):
            exports[name] = path
    return exports


def _looped_functions(commands: list[str]) -> list[str]:
    """The FUNCTION_* variables the build loop iterates over.

    An export that the loop does not name is not built, so the loop -- not the
    export list -- is the universe of images this build produces.
    """
    looped: list[str] = []
    for command in commands:
        for body in re.findall(
            r"for\s+func_var\s+in\s+(.+?);\s*do", command, re.DOTALL
        ):
            looped.extend(re.findall(r"FUNCTION_[A-Za-z0-9_]+", body))
    return looped


def _built_images() -> set[str]:
    commands = _buildspec_commands(_buildspec_path())
    exports = _exported_functions(commands)
    return {
        _image_name_for(exports[name])
        for name in _looped_functions(commands)
        if name in exports
    }


# --------------------------------------------------------------------------- #
# the parses are not vacuous
# --------------------------------------------------------------------------- #


def test_the_expected_images_list_parses_non_empty() -> None:
    images = _expected_images()
    assert len(images) >= 3, f"ExpectedImages parsed as {images}"
    assert all(name.endswith("-function") for name in images), images
    assert len(set(images)) == len(images), f"duplicate entries: {images}"


def test_the_built_image_set_parses_non_empty() -> None:
    built = _built_images()
    assert len(built) >= 3, f"built images parsed as {built}"
    assert all(name.endswith("-function") for name in built), built


def test_every_variable_the_loop_names_is_exported() -> None:
    """An unexported FUNCTION_* yields an empty path, so a tag of just the version.

    Same consequence as the subset failure below -- the expected tag never
    appears -- reached inside the buildspec instead of across the two files.
    """
    commands = _buildspec_commands(_buildspec_path())
    exports = _exported_functions(commands)
    looped = _looped_functions(commands)

    assert looped, "no build loop over FUNCTION_* variables found in the buildspec"
    missing = sorted(name for name in looped if name not in exports)
    assert not missing, f"the build loop names unexported variables: {missing}"


# --------------------------------------------------------------------------- #
# the assertion
# --------------------------------------------------------------------------- #


def test_every_expected_image_is_actually_built() -> None:
    expected = _expected_images()
    built = _built_images()

    unbuilt = sorted(set(expected) - built)
    assert not unbuilt, (
        "DockerBuildRun's ExpectedImages names images that "
        f"{_buildspec_path().name} does not build: {unbuilt}. The custom resource "
        "polls for every expected tag, and presence polling is unbounded, so each "
        "of these waits out CloudFormation's one-hour custom-resource limit and "
        "fails the stack with 'CloudFormation did not receive a response from your "
        "Custom Resource' -- a message that names neither the image nor the build. "
        f"Images the buildspec does build: {sorted(built)}"
    )
