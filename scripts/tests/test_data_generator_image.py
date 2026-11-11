# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Test Set Generator's AgentCore image installs what its README describes.

``feature-platform/idp-data-generator/Dockerfile`` builds the runtime that runs
SEED. Its README records SEED's per-agent tool inventory for one release, and the
synthesis adapter's render policy needs an ``xhtml2pdf`` that has
``ResourceAccessPolicy``. These tests read the Dockerfile as text, because the
image itself is built only at install time by CodeBuild.
"""

import ast
import re
import shlex
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
EXT = ROOT / "feature-platform" / "idp-data-generator"
DOCKERFILE = EXT / "Dockerfile"
README = EXT / "README.md"
RUNTIME_DIR = EXT / "agent-source" / "runtime"

# xhtml2pdf releases read for the render policy: 0.2.27 and 0.2.28 ship
# xhtml2pdf/config/resources.py with ResourceAccessPolicy, 0.2.17 does not.
FIRST_XHTML2PDF_WITH_POLICY = (0, 2, 27)


def _instructions():
    """Dockerfile instructions with comments dropped and continuations joined."""
    text = re.sub(r"\\\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        out.append((keyword.upper(), rest.strip()))
    return out


def _pip_requirements():
    """Every requirement passed to a ``pip install`` in a RUN instruction."""
    reqs = []
    for keyword, rest in _instructions():
        if keyword != "RUN":
            continue
        for command in re.split(r"&&|;", rest):
            words = shlex.split(command)
            if words[:2] == ["pip", "install"]:
                reqs.extend(w for w in words[2:] if not w.startswith("-"))
    return reqs


def _pin(name):
    pattern = re.compile(rf"^{re.escape(name)}(\[[^\]]*\])?==([\w.]+)$")
    matches = [m for r in _pip_requirements() if (m := pattern.match(r))]
    assert len(matches) == 1, f"expected one exact pin for {name}, got {matches}"
    return matches[0].group(1) or "", matches[0].group(2)


def test_seed_is_pinned_exactly_with_the_augment_extra():
    extras, _ = _pin("seed-data")
    assert "augment" in extras.strip("[]").split(",")


def test_readme_inventory_names_the_pinned_seed_release():
    _, version = _pin("seed-data")
    assert f"`seed-data` **{version}**" in README.read_text(encoding="utf-8")


def test_xhtml2pdf_is_pinned_to_a_release_with_the_access_policy():
    _, version = _pin("xhtml2pdf")
    assert tuple(int(p) for p in version.split(".")) >= FIRST_XHTML2PDF_WITH_POLICY


def test_app_directory_is_not_handed_to_the_runtime_user():
    for keyword, rest in _instructions():
        if keyword == "RUN":
            assert "chown" not in rest, rest
            assert "chmod" not in rest, rest
        if keyword in ("COPY", "ADD"):
            assert "--chown" not in rest and "--chmod" not in rest, rest
    users = [rest for keyword, rest in _instructions() if keyword == "USER"]
    assert users and users[-1] not in ("root", "0")


def test_script_directory_is_kept_off_the_import_path():
    envs = [rest for keyword, rest in _instructions() if keyword == "ENV"]
    assert any(re.search(r"\bPYTHONSAFEPATH=1\b", e) for e in envs), envs


def test_runtime_entrypoint_imports_no_sibling_module():
    """PYTHONSAFEPATH=1 drops /app from sys.path, which only works while this holds."""
    siblings = {p.stem for p in RUNTIME_DIR.glob("*.py")}
    for path in RUNTIME_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, (path.name, "relative import")
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            assert not siblings.intersection(names), (path.name, names)
