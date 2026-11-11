# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Checks on the Python the analytics agent sends to the Code Interpreter.

The analytics agent writes Python to turn ``query_results.csv`` into a chart, a table
or a short text answer. :func:`check_sandbox_code` refuses code that does anything
else that the code's own text makes visible:

- code that does not parse as Python (which also refuses notebook-style shell lines
  such as ``!command``);
- an ``import`` of any module outside :data:`IMPORTABLE_MODULES`, a data and charting
  set that leaves out networking, HTTP, AWS SDK and process modules (``socket``,
  ``urllib``, ``http``, ``requests``, ``httpx``, ``aiohttp``, ``boto3``,
  ``botocore``, ``subprocess``, ``os`` and the rest);
- the built-ins that import, evaluate or look up names dynamically
  (:data:`DYNAMIC_NAMES`), and any attribute whose name starts with ``_``, which is
  how module internals (``random._os``) and object internals (``__class__``) are
  reached, and any attribute named like a refused module (:data:`MODULE_ATTRIBUTES`,
  ``typing.sys``);
- a string literal containing ``://`` (a URL in any scheme);
- a ``read_*`` function, or one of :data:`PATH_OPENERS` or :data:`PATH_WRITERS`
  (``open``, ``np.loadtxt``, ``df.to_csv``, ``plt.savefig``, ...), used other than by
  a direct call, called with unpacked arguments, or called with a first argument that
  is not a literal file name in the working directory (a writer may also target an
  empty ``io.BytesIO()``/``StringIO()``).

⚠️ **This is a check on the text and syntax tree of the code, not a boundary.** It
cannot see what a value will be at run time, so it refuses the forms above rather
than proving the code cannot reach anything: a determined construction it does not
recognise would still run. What the sandbox itself can reach is set by the Code
Interpreter, not by this module.
"""

from __future__ import annotations

import ast

#: Top-level modules the code may import. Everything else is refused.
IMPORTABLE_MODULES = frozenset(
    {
        # Data and charting.
        "pandas",
        "numpy",
        "matplotlib",
        "seaborn",
        "scipy",
        # Standard-library computation, formatting and data structures.
        "json",
        "math",
        "cmath",
        "statistics",
        "decimal",
        "fractions",
        "numbers",
        "random",
        "datetime",
        "time",
        "calendar",
        "zoneinfo",
        "collections",
        "itertools",
        "functools",
        "operator",
        "heapq",
        "bisect",
        "copy",
        "re",
        "string",
        "textwrap",
        "csv",
        "io",
        "base64",
        "typing",
        "dataclasses",
        "enum",
        "warnings",
    }
)

#: Names that import, evaluate or look up attributes by a computed name. Each one
#: would let the code reach something its text does not spell out.
DYNAMIC_NAMES = frozenset(
    {
        "__import__",
        "__builtins__",
        "__loader__",
        "__spec__",
        "eval",
        "exec",
        "compile",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "breakpoint",
        "importlib",
    }
)


#: Functions that open a file or a URL by the path given as their first argument,
#: besides every ``read_*`` function. Their first argument must be a literal file
#: name in the working directory, and they may only be called, not passed around.
PATH_OPENERS = frozenset(
    {
        "open",
        "FileIO",
        "urlopen",
        "loadtxt",
        "genfromtxt",
        "load",
        "fromfile",
        "memmap",
        "DataSource",
        "ExcelFile",
        "HDFStore",
        "imread",
    }
)

#: Functions and methods that write to the path given as their first argument. The
#: same rule applies, except that an empty in-memory buffer is also accepted.
PATH_WRITERS = frozenset(
    {
        "to_csv",
        "to_excel",
        "to_parquet",
        "to_json",
        "to_feather",
        "to_hdf",
        "to_pickle",
        "to_stata",
        "to_html",
        "to_latex",
        "to_markdown",
        "to_xml",
        "to_orc",
        "to_sql",
        "to_string",
        "save",
        "savez",
        "savez_compressed",
        "savetxt",
        "tofile",
        "savefig",
        "imsave",
    }
)

#: Keyword names under which those functions take their path.
PATH_KEYWORDS = frozenset(
    {"file", "filepath_or_buffer", "path", "io", "path_or_buf", "buf", "fname", "fp"}
)

#: In-memory buffers a writer may target.
IN_MEMORY_BUFFERS = frozenset({"BytesIO", "StringIO"})


#: Module names that are refused as attributes too, because an allowed module that
#: imported one exposes it as an attribute (``typing.sys``, ``pd.io.common.os``).
MODULE_ATTRIBUTES = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "ssl",
        "urllib",
        "urllib3",
        "http",
        "requests",
        "httpx",
        "aiohttp",
        "boto3",
        "botocore",
        "shutil",
        "pathlib",
        "importlib",
        "builtins",
        "ctypes",
        "multiprocessing",
        "threading",
        "asyncio",
        "ftplib",
        "smtplib",
        "pty",
        "signal",
        "platform",
        "fsspec",
    }
)


class SandboxCodeNotAllowedError(ValueError):
    """The code uses something outside what the analytics sandbox code may use."""


def _is_local_path_literal(node: ast.AST) -> bool:
    """A string literal naming a file in the working directory: relative, with no
    scheme, no parent reference and no home or absolute prefix."""
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    value = node.value
    return (
        bool(value)
        and "://" not in value
        and not value.startswith(("/", "~", "\\"))
        and ".." not in value.replace("\\", "/").split("/")
    )


def _is_in_memory_buffer(node: ast.AST) -> bool:
    """``io.BytesIO()`` / ``StringIO()`` called with no arguments."""
    return (
        isinstance(node, ast.Call)
        and _call_name(node.func) in IN_MEMORY_BUFFERS
        and not node.args
        and not node.keywords
    )


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _takes_a_path(name: str) -> bool:
    return name in PATH_OPENERS or name in PATH_WRITERS or name.startswith("read_")


def _findings(tree: ast.AST) -> list[str]:
    found: list[str] = []
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    # Names bound only ever to an empty in-memory buffer (b = io.BytesIO()).
    bound: dict[str, bool] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            bound.setdefault(n.id, False)
        if isinstance(n, ast.Assign):
            for target in n.targets:
                if isinstance(target, ast.Name):
                    ok = _is_in_memory_buffer(n.value)
                    bound[target.id] = bound.get(target.id, ok) and ok
    buffers = {name for name, ok in bound.items() if ok}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in IMPORTABLE_MODULES:
                    found.append(f"import of {alias.name!r}")
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if node.level or top not in IMPORTABLE_MODULES:
                found.append(f"import from {node.module or '.'!r}")
            for alias in node.names:
                if _takes_a_path(alias.name) or alias.name.startswith("_"):
                    found.append(f"import of {alias.name!r}")
        elif isinstance(node, ast.Name):
            if node.id in DYNAMIC_NAMES:
                found.append(f"use of {node.id!r}")
            elif _takes_a_path(node.id) and id(node) not in called:
                found.append(f"{node.id!r} used other than by calling it")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr in MODULE_ATTRIBUTES:
                # Private and double-underscore attributes reach module internals
                # (random._os, pd.io.common's imports) and object internals.
                found.append(f"attribute {node.attr!r}")
            elif _takes_a_path(node.attr) and id(node) not in called:
                found.append(f"{node.attr!r} used other than by calling it")
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            text = (
                node.value
                if isinstance(node.value, str)
                else node.value.decode("latin-1")
            )
            if "://" in text:
                found.append("a URL in a string literal")
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if not _takes_a_path(name):
                continue
            if any(kw.arg is None for kw in node.keywords) or any(
                isinstance(a, ast.Starred) for a in node.args
            ):
                found.append(f"{name}() with unpacked arguments")
                continue
            first = node.args[0] if node.args else None
            if first is None:
                first = next(
                    (kw.value for kw in node.keywords if kw.arg in PATH_KEYWORDS),
                    None,
                )
            if first is None:
                continue
            if _is_local_path_literal(first) or (
                name in PATH_WRITERS
                and (
                    _is_in_memory_buffer(first)
                    or (isinstance(first, ast.Name) and first.id in buffers)
                )
            ):
                continue
            found.append(f"{name}() of a path that is not a local file name")
    return found


def check_sandbox_code(code: str) -> None:
    """Raise :class:`SandboxCodeNotAllowedError` if ``code`` uses anything this
    module refuses. The message is written for the agent, so it can rewrite the code.
    """
    if not isinstance(code, str) or not code.strip():
        raise SandboxCodeNotAllowedError("The code is empty.")
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise SandboxCodeNotAllowedError(
            f"The code is not valid Python ({e.msg}, line {e.lineno}). Shell and "
            "notebook commands are not available."
        ) from None
    findings = _findings(tree)
    if findings:
        unique = sorted(set(findings))
        raise SandboxCodeNotAllowedError(
            "The code was not run because it uses: "
            + "; ".join(unique)
            + ". Analytics code reads only 'query_results.csv' and may import only: "
            + ", ".join(sorted(IMPORTABLE_MODULES))
            + "."
        )
