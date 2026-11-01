# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What loading a handler by path leaves in ``sys.modules`` afterwards (#1708).

The helper used to run each handler inside ``patch.dict("sys.modules")``, which
removes every module first imported during the call. Such a module stays bound
inside the handler, so the next import of it builds a second copy, and a later
test file's ``patch("idp_common...")`` patched the copy while the code under test
ran the original — ``test_assessment_service.py`` went red when it ran after a
file that loaded the assessment handler.

The synthetic packages below have names no other test imports, so "first
imported during the call" is a fact about the test rather than about test order.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.unit._handler_loading import exec_with_stubs

PKG_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A fresh package name on ``sys.path``, and every module of it unloaded after."""
    name = f"lh_probe_{uuid.uuid4().hex[:10]}"
    stub = f"{name}_stub"
    monkeypatch.syspath_prepend(str(tmp_path))

    def write(relative: str, body: str) -> Path:
        path = tmp_path / relative.format(pkg=name, stub=stub)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body.format(pkg=name, stub=stub)))
        return path

    yield name, stub, write
    for module in [m for m in sys.modules if m.startswith(name)]:
        del sys.modules[module]


def test_a_module_first_imported_by_the_handler_stays_loaded(tree):
    pkg, stub, write = tree
    write("{pkg}/__init__.py", "")
    write("{pkg}/clean.py", "VALUE = object()\n")
    handler = write(
        "handler_clean.py",
        """
        import {stub}
        from {pkg} import clean
        from {pkg}.clean import VALUE
        """,
    )
    assert f"{pkg}.clean" not in sys.modules

    module = exec_with_stubs("lh_handler_clean", handler, {stub: MagicMock()})

    # One copy: the module the handler bound is the one a later import gets.
    assert sys.modules[f"{pkg}.clean"] is module.clean
    assert sys.modules[f"{pkg}.clean"].VALUE is module.VALUE
    __import__(f"{pkg}.clean")
    assert sys.modules[f"{pkg}.clean"] is module.clean


def test_modules_reaching_a_stub_are_removed_and_the_stub_is_gone(tree):
    pkg, stub, write = tree
    write("{pkg}/__init__.py", "")
    write("{pkg}/clean.py", "")
    write("{pkg}/direct.py", "import {stub} as dep\n")
    write("{pkg}/fromstub.py", "from {stub} import thing\n")
    write("{pkg}/viaattr.py", "from {stub}.sub import thing\n")
    write(
        "{pkg}/dynamic.py",
        "import importlib\ndep = importlib.import_module('{stub}')\n",
    )
    write("{pkg}/transitive.py", "from {pkg}.direct import dep\n")
    write("{pkg}/relative.py", "from . import transitive\n")
    write("{pkg}/tpkg/__init__.py", "import {stub}\n")
    write("{pkg}/tpkg/child.py", "")
    handler = write(
        "handler_taint.py",
        """
        import {pkg}.clean, {pkg}.direct, {pkg}.fromstub, {pkg}.viaattr
        import {pkg}.dynamic, {pkg}.relative, {pkg}.tpkg.child
        """,
    )
    stubs = {stub: MagicMock(), f"{stub}.sub": MagicMock()}

    exec_with_stubs("lh_handler_taint", handler, stubs)

    assert stub not in sys.modules and f"{stub}.sub" not in sys.modules
    removed = ["direct", "fromstub", "viaattr", "dynamic", "transitive", "relative"]
    for leaf in [*removed, "tpkg", "tpkg.child"]:
        assert f"{pkg}.{leaf}" not in sys.modules, leaf
    # The package stays, and no longer hands out a removed submodule as attribute.
    assert f"{pkg}.clean" in sys.modules
    for leaf in [*removed, "tpkg"]:
        assert not hasattr(sys.modules[pkg], leaf), leaf


def test_a_stub_name_already_loaded_is_put_back(tree):
    pkg, stub, write = tree
    handler = write("handler_prior.py", "import {stub}\n")
    original = object()
    sys.modules[stub] = original  # type: ignore[assignment]
    try:
        exec_with_stubs("lh_handler_prior", handler, {stub: MagicMock()})
        assert sys.modules[stub] is original
    finally:
        del sys.modules[stub]


def test_the_assessment_handler_leaves_one_copy_of_what_it_imports():
    """The #1693 shape, in a fresh interpreter so the first import is the call's."""
    probe = textwrap.dedent(
        """
        import sys
        from tests.unit.lambdas._goldens import load_handler
        assert "idp_common.assessment.degradation" not in sys.modules
        h = load_handler("lh_assessment", "assessment_function/index.py")
        mod = sys.modules["idp_common.assessment.degradation"]
        assert mod.degrade_section_to_no_confidence is h.degrade_section_to_no_confidence
        from unittest.mock import NonCallableMock
        for name in ("aws_xray_sdk", "aws_xray_sdk.core"):
            assert not isinstance(sys.modules.get(name), NonCallableMock), name
        print("ONE-COPY-OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=PKG_ROOT,
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(PKG_ROOT), *sys.path])},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "ONE-COPY-OK" in result.stdout, result.stderr[-3000:]


def test_a_module_already_loaded_is_never_left_removed(tree):
    pkg, stub, write = tree
    write("{pkg}/__init__.py", "")
    write("{pkg}/pre.py", "")
    __import__(f"{pkg}.pre")
    before = sys.modules[f"{pkg}.pre"]
    handler = write("handler_drop.py", "import sys\nsys.modules.pop('{pkg}.pre')\n")

    exec_with_stubs("lh_handler_drop", handler, {stub: MagicMock()})

    assert sys.modules[f"{pkg}.pre"] is before


def test_a_module_binding_a_patched_target_is_removed(tree):
    """``load_lambda``'s ``patches`` count as stubs for the removal rule."""
    pkg, stub, write = tree
    write("{pkg}/__init__.py", "")
    write("{pkg}/dep.py", "def client():\n    return 'real'\n")
    write("{pkg}/binds.py", "from {pkg}.dep import client\n")
    write("{pkg}/reads.py", "from {pkg} import dep\n")
    handler = write("handler_patched.py", "import {pkg}.binds, {pkg}.reads\n")
    __import__(f"{pkg}.dep")

    exec_with_stubs("lh_handler_patched", handler, {}, taint=(f"{pkg}.dep.client",))

    assert f"{pkg}.binds" not in sys.modules
    # Importing the module that owns the target is not binding the target.
    assert f"{pkg}.reads" in sys.modules


def test_a_module_whose_name_only_starts_like_a_stub_is_kept(tree):
    pkg, stub, write = tree
    write("{pkg}/__init__.py", "")
    write("{stub}2.py", "")
    write("{pkg}/near.py", "import {stub}2\n")
    handler = write("handler_near.py", "import {stub}, {pkg}.near\n")

    # ``<stub>2`` shares the stub's name as a string prefix but is not inside it.
    exec_with_stubs("lh_handler_near", handler, {stub: MagicMock()})

    assert f"{stub}2" in sys.modules
    assert f"{pkg}.near" in sys.modules
