# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the strands / boto3 binding check in ``tests/conftest.py`` (#1756).

Three test modules replaced ``strands.tool`` with a passthrough when they were
imported and never put it back, and a fourth did the same to the name
``table_parser`` bound from it. Collection imports every module before the
first test runs, so every Strands tool built later in that process was a plain
function an Agent could not register, and tests built on those tools passed
while every tool call answered "tool not found".

The check in conftest compares what the names refer to after each module is
imported and after each test, so these tests drive it both directly and through
a real pytest run.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import boto3
import pytest

decorator = pytest.importorskip(
    "strands.tools.decorator",
    reason="strands-agents not installed (a conftest stub does not count)",
)

import strands  # noqa: E402

conftest = importlib.import_module("tests.conftest")

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def _passthrough(fn):
    return fn


@pytest.mark.unit
def test_the_snapshot_holds_the_real_objects():
    """Non-vacuity: an empty snapshot would make every check below pass."""
    snapshot = conftest._BINDING_SNAPSHOT
    assert snapshot[("strands", "tool")] is decorator.tool
    assert snapshot[("strands.tools.decorator", "tool")] is decorator.tool
    assert snapshot[("strands.tools", "tool")] is decorator.tool
    assert snapshot[("strands", "Agent")] is strands.Agent
    assert snapshot[("boto3", "client")] is boto3.client


@pytest.mark.unit
def test_first_party_bindings_are_read_from_the_modules_imports():
    from idp_common.extraction import agentic_idp
    from idp_common.extraction.tools import table_parser

    assert conftest.binding_problems() == []
    bindings = conftest._FIRST_PARTY_BINDINGS
    assert bindings[(table_parser.__name__, "tool")] == ("strands", "tool")
    assert bindings[(agentic_idp.__name__, "tool")] == ("strands", "tool")
    assert bindings[(agentic_idp.__name__, "Agent")] == ("strands", "Agent")


@pytest.mark.unit
def test_a_replaced_strands_tool_is_reported_and_put_back():
    original = strands.tool
    strands.tool = _passthrough
    try:
        problems = conftest.binding_problems()
    finally:
        strands.tool = original
    assert any(p.startswith("strands.tool is ") for p in problems), problems
    assert conftest.binding_problems() == []


@pytest.mark.unit
def test_restore_puts_the_original_back():
    original = strands.tool
    strands.tool = _passthrough
    try:
        conftest.binding_problems(restore=True)
        assert strands.tool is original
    finally:
        strands.tool = original


@pytest.mark.unit
def test_a_replaced_first_party_binding_is_reported():
    """The ``table_parser.tool = ...`` shape, which leaves strands itself intact."""
    from idp_common.extraction.tools import table_parser

    original = table_parser.tool
    table_parser.tool = _passthrough
    try:
        problems = conftest.binding_problems()
        assert table_parser.tool is original, "the binding was not put back"
    finally:
        table_parser.tool = original
    assert problems == [
        f"{table_parser.__name__}.tool (bound by `from strands import tool`) is "
        f"{_passthrough!r}, not {original!r}"
    ]


@pytest.mark.unit
def test_a_replaced_boto3_client_is_reported():
    original = boto3.client
    boto3.client = _passthrough
    try:
        problems = conftest.binding_problems()
    finally:
        boto3.client = original
    assert any(p.startswith("boto3.client is ") for p in problems), problems


@pytest.mark.unit
def test_an_undone_monkeypatch_is_not_reported(monkeypatch):
    monkeypatch.setattr(strands, "tool", _passthrough)
    assert conftest.binding_problems(restore=False)
    monkeypatch.undo()
    assert conftest.binding_problems() == []


_SYNTHETIC_FIRST_PARTY = """
import functools

import strands
import strands as s
from strands import tool as t
from strands import tools
from strands.tools import decorator
from strands.tools.decorator import tool

assigned = strands.tool


def _wrap(fn):
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        return fn(*args, **kwargs)

    return inner


@t
def aliased(x: str) -> str:
    \"\"\"Echo.\"\"\"
    return x


@s.tool
def via_module_alias(x: str) -> str:
    \"\"\"Echo.\"\"\"
    return x


@tool(name="called_form")
def called(x: str) -> str:
    \"\"\"Echo.\"\"\"
    return x


@tools.tool
def via_tools_package(x: str) -> str:
    \"\"\"Echo.\"\"\"
    return x


@decorator.tool
def via_decorator_module(x: str) -> str:
    \"\"\"Echo.\"\"\"
    return x


@assigned
def via_assigned_alias(x: str) -> str:
    \"\"\"Echo.\"\"\"
    return x


if True:

    @strands.tool
    def in_an_if_block(x: str) -> str:
        \"\"\"Echo.\"\"\"
        return x


try:

    @strands.tool
    def in_a_try_block(x: str) -> str:
        \"\"\"Echo.\"\"\"
        return x

except ImportError:
    pass


class Holder:
    @strands.tool
    def in_a_class(self, x: str) -> str:
        \"\"\"Echo.\"\"\"
        return x


@_wrap
@tool
def tool_not_outermost(x: str) -> str:
    \"\"\"Echo.\"\"\"
    return x


def not_a_tool(x: str) -> str:
    return x


rel = None
if False:  # never executed: a relative import is not the strands package
    from .strands import tool as rel  # noqa: F811
"""

#: Every definition above whose outermost decorator is the strands decorator.
_SYNTHETIC_TOOL_PATHS = {
    ("aliased",),
    ("via_module_alias",),
    ("called",),
    ("via_tools_package",),
    ("via_decorator_module",),
    ("via_assigned_alias",),
    ("in_an_if_block",),
    ("in_a_try_block",),
    ("Holder", "in_a_class"),
}


@pytest.fixture
def synthetic_first_party(tmp_path, monkeypatch):
    """A first-party module the scan has never seen, removed afterwards."""
    path = tmp_path / "binding_probe.py"
    path.write_text(_SYNTHETIC_FIRST_PARTY)
    # A fresh name per test: the scan reads a module once, by name.
    name = f"idp_common._binding_probe_{tmp_path.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    yield module


@pytest.mark.unit
def test_aliased_imports_and_module_level_tools_are_read(synthetic_first_party):
    name = synthetic_first_party.__name__
    assert conftest.binding_problems() == []
    assert conftest._FIRST_PARTY_BINDINGS[(name, "t")] == ("strands", "tool")
    found = {path for owner, path in conftest._FIRST_PARTY_TOOLS if owner == name}
    assert found == _SYNTHETIC_TOOL_PATHS


@pytest.mark.unit
def test_a_replaced_aliased_binding_is_reported(synthetic_first_party):
    conftest.binding_problems()
    synthetic_first_party.t = _passthrough
    problems = conftest.binding_problems()
    assert synthetic_first_party.t is decorator.tool
    assert problems == [
        f"{synthetic_first_party.__name__}.t (bound by `from strands import tool`) "
        f"is {_passthrough!r}, not {decorator.tool!r}"
    ]


@pytest.mark.unit
def test_a_tool_left_undecorated_is_reported_and_redecorated(synthetic_first_party):
    """The shape a scoped replacement can still leave behind: the module was
    imported while the decorator was replaced, so its tools are plain functions
    although every name now refers to the right object."""
    conftest.binding_problems()
    plain = synthetic_first_party.via_module_alias.__wrapped__
    synthetic_first_party.via_module_alias = plain
    problems = conftest.binding_problems()
    assert len(problems) == 1, problems
    assert (
        "via_module_alias is decorated with the strands tool decorator" in problems[0]
    )
    assert isinstance(
        synthetic_first_party.via_module_alias, decorator.DecoratedFunctionTool
    )


@pytest.mark.unit
def test_a_class_body_tool_left_undecorated_is_reported(synthetic_first_party):
    conftest.binding_problems()
    holder = synthetic_first_party.Holder
    plain = holder.__dict__["in_a_class"].__wrapped__
    holder.in_a_class = plain
    problems = conftest.binding_problems()
    assert len(problems) == 1, problems
    assert "Holder.in_a_class is decorated with the strands tool" in problems[0]
    assert isinstance(holder.__dict__["in_a_class"], decorator.DecoratedFunctionTool)


@pytest.mark.unit
def test_redecoration_reaches_other_first_party_holders(
    synthetic_first_party, monkeypatch
):
    """A package re-export or ``from x import tool_fn`` holds the same plain
    function; putting the tool back only where it was defined would leave those
    plain, and nothing after would report them."""
    import types

    conftest.binding_problems()
    plain = synthetic_first_party.called.__wrapped__
    synthetic_first_party.called = plain
    holder = types.ModuleType("idp_common._binding_probe_holder_for_tests")
    holder.reexported = plain
    monkeypatch.setitem(sys.modules, holder.__name__, holder)
    assert conftest.binding_problems()
    assert isinstance(holder.reexported, decorator.DecoratedFunctionTool)
    assert holder.reexported is synthetic_first_party.called


@pytest.mark.unit
def test_tools_are_resolved_after_their_bindings_are_put_back(synthetic_first_party):
    """A module imported under ``from strands import tool`` pollution holds both a
    replaced name and plain tools. The decorator has to be evaluated after the
    name is put back, or ``@t`` evaluates to the replacement and the plain tool
    is never recognised as one."""
    plain = synthetic_first_party.aliased.__wrapped__
    synthetic_first_party.t = _passthrough
    synthetic_first_party.aliased = plain
    problems = conftest.binding_problems()
    assert any(".t (bound by `from strands import tool`)" in p for p in problems)
    assert any(".aliased is decorated with the strands tool" in p for p in problems)
    assert isinstance(synthetic_first_party.aliased, decorator.DecoratedFunctionTool)


_PLANTED_CONFTEST = """
from tests.conftest import *  # noqa: F401,F403
from tests.conftest import pytest_make_collect_report, pytest_runtest_teardown  # noqa: F401
"""

_PLANTED_MODULES = {
    "test_at_import.py": """
        import strands

        strands.tool = lambda fn: fn

        def test_runs():
            pass
    """,
    "test_first_party_at_import.py": """
        from idp_common.extraction.tools import table_parser

        table_parser.tool = lambda fn: fn

        def test_runs():
            pass
    """,
    "test_inside_a_test.py": """
        import boto3

        def test_leaves_it_replaced():
            boto3.client = lambda *a, **k: None
    """,
    "test_imported_under_a_scoped_patch.py": """
        import strands

        def test_imports_a_tool_module_while_patched(monkeypatch):
            monkeypatch.setattr(strands, "tool", lambda fn: fn)
            import idp_common.agents.quick_start.tools.bootstrap_tools  # noqa: F401
    """,
    "test_scoped.py": """
        import strands

        def test_monkeypatched(monkeypatch):
            monkeypatch.setattr(strands, "tool", lambda fn: fn)
    """,
}


@pytest.mark.unit
def test_the_hooks_fail_the_module_or_test_that_left_a_replacement(tmp_path):
    """End to end through pytest: the hooks are what turn a problem into red."""
    (tmp_path / "conftest.py").write_text(_PLANTED_CONFTEST)
    for name, body in _PLANTED_MODULES.items():
        (tmp_path / name).write_text(textwrap.dedent(body))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(_PACKAGE_ROOT), env.get("PYTHONPATH", "")])
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "-p",
            "no:xdist",
            "--continue-on-collection-errors",
            "-rA",
            "-q",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = run.stdout + run.stderr
    assert run.returncode != 0, out
    assert "ERROR test_at_import.py" in out, out
    assert "ERROR test_first_party_at_import.py" in out, out
    assert "ERROR test_inside_a_test.py::test_leaves_it_replaced" in out, out
    assert "PASSED test_scoped.py::test_monkeypatched" in out, out
    assert "strands.tool is " in out, out
    assert "table_parser.tool (bound by `from strands import tool`)" in out, out
    assert "boto3.client is " in out, out
    assert (
        "ERROR test_imported_under_a_scoped_patch.py::"
        "test_imports_a_tool_module_while_patched" in out
    ), out
    assert "bootstrap_tools.search_catalog is decorated with the strands" in out, out
    assert "Import the module before replacing the decorator" in out, out
