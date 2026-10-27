# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""No Lambda in this repository installs ``cfnresponse`` from a package index.

CloudFormation supplies the ``cfn-response`` module only to a function whose code
is inline in the template (``ZipFile``). A function packaged from a ``CodeUri``
has to bring its own, and the custom-resource Lambdas here used to do that by
listing ``cfnresponse`` in ``requirements.txt`` by bare name and unpinned, so each
build installed whatever the public index served under that name.

Each of them now carries a vendored ``cfnresponse.py`` beside its handler. The
canonical copy, and the tests of what ``send`` puts on the wire, are in
``nested/bedrockkb/src/s3_vectors_manager`` (``tests/test_cfnresponse.py``). This
file holds the repository-wide half, over every tracked or untracked-but-not-ignored
file, so a Lambda added anywhere is covered without being listed:

- every directory whose code imports ``cfnresponse`` has ``cfnresponse.py`` in
  it, and, when a template packages that directory, it is the root of the
  ``CodeUri`` that does so (the root is what Lambda puts on ``sys.path``), and a
  ``BuildMethod: makefile`` recipe there copies the module into the artifact;
- every ``cfnresponse.py`` is byte-identical to the canonical one, so a fix to one
  copy cannot silently miss another;
- no dependency manifest names it outside a comment, in any spelling pip would
  normalise to the same project or to the hyphenated ``cfn-response``;
- no build script, Dockerfile or document runs a ``pip install`` naming it.

What it does not match: an install command spelled other than ``pip install``
(``uv add``, a ``$(PIP)`` variable), a requirements file whose name does not say
so, and a module name assembled at runtime. SAM's default Python builder reads
only ``requirements.txt``, which is covered.

To add a Lambda that needs the module, copy the canonical file beside its handler.
"""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
CANONICAL = REPO / "nested/bedrockkb/src/s3_vectors_manager/cfnresponse.py"
MODULE = "cfnresponse"

# Any spelling of the project name: pip normalises runs of "-", "_" and "." and
# ignores case, and "cfn-response" is matched as well because it reads the same.
NAME = re.compile(r"(?<![A-Za-z0-9])cfn[-_.]*response(?![A-Za-z0-9])", re.I)
# A pip invocation: "pip install", "pip3 install", "python -m pip install",
# "uv pip install". Assembled so this file's own text does not match it.
PIP_INSTALL = re.compile(r"\bpip3?\s+" + "install" + r"\b", re.I)

MANIFEST_NAMES = re.compile(
    r"(requirements|constraints)[^/]*\.(txt|in)$"
    r"|(^|/)(pyproject\.toml|setup\.cfg|setup\.py|Pipfile)$",
    re.I,
)
BUILD_SCRIPT_NAMES = re.compile(
    r"(^|/)(Makefile|[^/]*\.mk|Dockerfile[^/]*|[^/]*\.sh|buildspec[^/]*\.ya?ml"
    r"|[^/]*\.gitlab-ci\.yml|[^/]*\.md|\.github/workflows/[^/]*\.ya?ml)$",
    re.I,
)


def _files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO,
        check=True,
        capture_output=True,
    ).stdout.decode()
    paths = [REPO / p for p in out.split("\0") if p]
    return [p for p in paths if p.is_file()]


FILES = _files()


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _imports_module(tree: ast.AST) -> bool:
    """Whether the module imports ``cfnresponse`` by any statement or call."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == MODULE for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or "").split(".")[0] == MODULE:
                return True
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            first = node.args[0]
            if (
                name in ("import_module", "__import__")
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value.split(".")[0] == MODULE
            ):
                return True
    return False


def _importing_files() -> list[Path]:
    found = []
    for path in FILES:
        if path.suffix != ".py" or path.name == f"{MODULE}.py":
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if MODULE not in source:
            continue
        if _imports_module(ast.parse(source, filename=str(path))):
            found.append(path)
    return found


IMPORTERS = _importing_files()
COPIES = [p for p in FILES if p.name == f"{MODULE}.py"]


def _strip_comment(line: str) -> str:
    """Drop a ``#`` comment that starts the line or follows whitespace, as pip
    and TOML read it; a ``#`` inside a URL fragment (``#egg=``) is kept."""
    return re.split(r"(?:^|\s)#", line, maxsplit=1)[0]


def _logical_lines(text: str) -> list[str]:
    """Lines with backslash continuations joined, as a shell or make reads them."""
    return re.sub(r"\\\r?\n", " ", text).splitlines()


def test_the_derivation_finds_the_lambdas_it_is_about():
    """Non-vacuity: both trees that carry a copy are seen by the derivation."""
    dirs = {_rel(p.parent) for p in IMPORTERS}
    assert "nested/bedrockkb/src/s3_vectors_manager" in dirs, sorted(dirs)
    assert "src/lambda/update_configuration" in dirs, sorted(dirs)
    assert CANONICAL in COPIES
    assert len(COPIES) >= len(dirs) >= 2


def test_every_importer_has_the_vendored_module_beside_it():
    assert IMPORTERS
    missing = sorted(
        _rel(p) for p in IMPORTERS if not (p.parent / f"{MODULE}.py").is_file()
    )
    assert not missing, (
        f"these files import {MODULE} with no {MODULE}.py beside them, so the "
        f"Lambda would have to install it; copy {_rel(CANONICAL)} into each "
        f"directory: {missing}"
    )


def _code_uris() -> set[Path]:
    """Every directory a template in the tree names as a function's ``CodeUri``."""
    templates = subprocess.run(
        ["bash", "scripts/discover_templates.sh", "cfn"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert templates, "template discovery found nothing"
    found = set()
    for template in templates:
        path = REPO / template
        for match in CODE_URI.finditer(path.read_text(encoding="utf-8")):
            target = (path.parent / match.group(1)).resolve()
            if target.is_dir() and target.is_relative_to(REPO):
                found.add(target)
    return found


CODE_URI = re.compile(r"^\s*CodeUri:\s*['\"]?([^'\"\s#]+)", re.M)


def test_every_packaged_importer_is_at_the_root_of_its_code_uri():
    """``import cfnresponse`` resolves from the ``CodeUri`` root and nowhere else,
    so a copy beside an importer in a subdirectory of it is never found."""
    roots = _code_uris()
    packaged = 0
    wrong = []
    for path in IMPORTERS:
        enclosing = [r for r in roots if r == path.parent or r in path.parents]
        if not enclosing:
            continue  # packaged by no template in the tree
        packaged += 1
        nearest = max(enclosing, key=lambda r: len(r.parts))
        if nearest != path.parent:
            wrong.append(f"{_rel(path)} (CodeUri {_rel(nearest)})")
            continue
        makefile = nearest / "Makefile"
        if makefile.is_file() and not MAKEFILE_COPIES.search(makefile.read_text()):
            wrong.append(f"{_rel(makefile)} does not copy {MODULE}.py")
    assert packaged >= 2, "no importer is packaged by any template; check discovery"
    assert not wrong, wrong


MAKEFILE_COPIES = re.compile(r"\bcp\b[^\n]*(\*\.py|cfnresponse\.py)")


def test_every_vendored_copy_is_byte_identical_to_the_canonical_one():
    canonical = hashlib.sha256(CANONICAL.read_bytes()).hexdigest()
    drifted = sorted(
        _rel(p)
        for p in COPIES
        if hashlib.sha256(p.read_bytes()).hexdigest() != canonical
    )
    assert len(COPIES) >= 2
    assert not drifted, (
        f"these copies differ from {_rel(CANONICAL)}; change the canonical copy "
        f"and copy it over each of them: {drifted}"
    )


def test_no_dependency_manifest_names_cfnresponse():
    manifests = [p for p in FILES if MANIFEST_NAMES.search(_rel(p))]
    assert len(manifests) > 10, "manifest discovery found almost nothing"
    hits = []
    for path in manifests:
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            if NAME.search(_strip_comment(line)):
                hits.append(f"{_rel(path)}:{number}: {line.strip()}")
    assert not hits, (
        f"vendor {MODULE}.py beside the handler instead of installing it: {hits}"
    )


def test_no_build_script_or_document_pip_installs_cfnresponse():
    scripts = [
        p
        for p in FILES
        if p != Path(__file__).resolve() and BUILD_SCRIPT_NAMES.search(_rel(p))
    ]
    assert len(scripts) > 10, "build-script discovery found almost nothing"
    hits = []
    for path in scripts:
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in _logical_lines(text):
            code = line if path.suffix == ".md" else _strip_comment(line)
            if PIP_INSTALL.search(code) and NAME.search(code):
                hits.append(f"{_rel(path)}: {line.strip()}")
    assert not hits, (
        f"vendor {MODULE}.py beside the handler instead of installing it: {hits}"
    )
