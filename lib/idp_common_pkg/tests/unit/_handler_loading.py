# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Execute a module by path with stub modules in ``sys.modules`` for the call only.

The test suites load deployed Lambda handlers by path, with X-Ray (and sometimes
another dependency) replaced in ``sys.modules`` while the handler's module body
runs. They used to do that inside ``patch.dict("sys.modules")``, which also
removes every module *first imported* during the call. Such a module stays bound
inside the handler, so the next import of it builds a second copy, and a later
test's ``patch("idp_common.x.y")`` patched that copy while the code under test ran
the original (#1708). ``exec_with_stubs`` undoes only what has to be undone.
"""

from __future__ import annotations

import builtins
import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


def xray_stubs() -> dict[str, Any]:
    """X-Ray as the handlers need it at import: a no-op ``patch_all`` and decorator."""
    recorder = MagicMock()
    recorder.capture.return_value = lambda fn: fn
    xray_core = MagicMock()
    xray_core.patch_all = lambda: None
    xray_core.xray_recorder = recorder
    return {"aws_xray_sdk": MagicMock(), "aws_xray_sdk.core": xray_core}


def load_with_xray_stubbed(module_name: str, path, extra_modules=None):
    """Load the module at ``path`` as ``module_name`` with X-Ray stubbed.

    Sets no environment: a harness that ``setdefault``s a region hides an
    import-time AWS dependency from the region-free import gate, so a caller that
    wants one sets it itself.
    """
    stubs = xray_stubs()
    stubs.update(extra_modules or {})
    return exec_with_stubs(module_name, Path(path), stubs)


def exec_with_stubs(
    module_name: str, path: Path, stubs: dict[str, Any], taint: tuple[str, ...] = ()
):
    """Execute the module at ``path`` with ``stubs`` in ``sys.modules`` for the call.

    What is undone afterwards is exactly what has to be, and nothing else:

    - every stub key goes back to its prior state (restored, or removed);
    - every module first imported during the call that imported a stub, directly
      or through another such module, is removed, because it holds the stub and
      would hand it to every later test in the process;
    - every other module first imported during the call **stays**.

    The last point is the reason this is not ``patch.dict("sys.modules")``; see the
    module docstring.

    ``taint`` names further dotted targets that count as stubs for that rule --
    the targets a caller has ``patch()``-ed for the duration. Only the spelling
    that imports the target by name is seen: a module that ran ``from boto3 import
    client`` under ``patch("boto3.client")`` is removed, but one that ran ``import
    boto3`` and called ``boto3.client(...)`` at import time is kept with whatever
    the patch returned.

    The rule is about recorded imports, not data flow. Imports are recorded
    through ``builtins.__import__`` (every ``import`` statement) and
    ``importlib.import_module`` while the call runs. A stub reaching a kept module
    any other way is not seen: an ``import_module`` reference bound before the
    call, ``importlib.__import__`` or the ``importlib._bootstrap`` functions,
    reading ``sys.modules`` directly, or the handler handing a stub to a module as
    an argument or attribute. None of these occurs in the handlers loaded today;
    a scan of every kept module's globals across the suites that use this found no
    stub or patched object left behind.
    """
    absent = object()
    saved = dict(sys.modules)
    stub_prior = {name: sys.modules.get(name, absent) for name in stubs}
    edges: dict[str, set[str]] = {}

    def record(importer: str | None, target: str, fromlist=()) -> None:
        if importer is None:
            return
        targets = edges.setdefault(importer, set())
        targets.add(target)
        targets.update(f"{target}.{item}" for item in fromlist or () if item != "*")

    real_import = builtins.__import__
    real_import_module = importlib.import_module

    def recording_import(name, globals=None, locals=None, fromlist=(), level=0):
        module = real_import(name, globals, locals, fromlist, level)
        if level:
            package = (globals or {}).get("__package__") or ""
            name = importlib.util.resolve_name("." * level + name, package)
        record((globals or {}).get("__name__"), name, fromlist)
        return module

    def recording_import_module(name, package=None):
        module = real_import_module(name, package)
        if name.startswith("."):
            name = importlib.util.resolve_name(name, package)
        record(sys._getframe(1).f_globals.get("__name__"), name)
        return module

    sys.modules.update(stubs)
    try:
        with (
            patch.object(builtins, "__import__", recording_import),
            patch.object(importlib, "import_module", recording_import_module),
        ):
            spec = importlib.util.spec_from_file_location(module_name, path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    finally:
        imported = set(sys.modules) - set(saved) - set(stubs)
        for name in imported & (
            stub_reachers(edges, {*stubs, *taint}, imported) | {module_name}
        ):
            removed = sys.modules.pop(name)
            parent, _, child = name.rpartition(".")
            if getattr(sys.modules.get(parent), child, None) is removed:
                delattr(sys.modules[parent], child)
        for name, value in stub_prior.items():
            if value is absent:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        # A module that was already loaded is never left replaced or removed.
        for name, value in saved.items():
            if name not in stubs and sys.modules.get(name, absent) is not value:
                sys.modules[name] = value
    return module


def stub_reachers(
    edges: dict[str, set[str]], stubs: set[str], imported: set[str]
) -> set[str]:
    """The modules in ``imported`` from which a stub is reachable.

    ``edges`` maps an importing module to the names it imported. A module is
    tainted when it imported a stub or a tainted module (or anything inside one),
    and so is every submodule of a tainted module, which would otherwise outlive
    the package it belongs to.
    """

    def within(name: str, roots: set[str]) -> bool:
        return any(name == root or name.startswith(f"{root}.") for root in roots)

    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name in sorted(imported - tainted):
            sources = stubs | tainted
            if within(name, tainted) or any(
                within(target, sources) for target in edges.get(name, ())
            ):
                tainted.add(name)
                changed = True
    return tainted
