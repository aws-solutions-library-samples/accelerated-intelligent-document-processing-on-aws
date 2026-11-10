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
  (:data:`DYNAMIC_NAMES`), and any double-underscore attribute such as
  ``__class__`` or ``__globals__``;
- a string literal containing ``://`` (a URL in any scheme);
- a call to a ``read_*`` or ``to_*`` function (``pd.read_csv``, ``df.to_csv``, ...)
  or to one of :data:`PATH_OPENERS` (``open``, ``np.loadtxt``, ``plt.savefig``, ...)
  whose first argument is not a string literal naming a local file.

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
#: besides every ``read_*`` function. Their first argument must be a literal local
#: file name.
PATH_OPENERS = frozenset(
    {
        "open",
        "loadtxt",
        "genfromtxt",
        "load",
        "fromfile",
        "DataSource",
        "imread",
        # Writers, alongside every ``to_*`` method (``df.to_csv(...)``).
        "save",
        "savez",
        "savetxt",
        "tofile",
        "savefig",
        "imsave",
    }
)


class SandboxCodeNotAllowedError(ValueError):
    """The code uses something outside what the analytics sandbox code may use."""


def _is_local_path_literal(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "://" not in node.value
    )


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _findings(tree: ast.AST) -> list[str]:
    found: list[str] = []
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
        elif isinstance(node, ast.Name) and node.id in DYNAMIC_NAMES:
            found.append(f"use of {node.id!r}")
        elif isinstance(node, ast.Attribute) and (
            node.attr.startswith("__") and node.attr.endswith("__")
        ):
            found.append(f"attribute {node.attr!r}")
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
            if (
                name in PATH_OPENERS
                or name.startswith("read_")
                or name.startswith("to_")
            ):
                first = node.args[0] if node.args else None
                if first is None:
                    first = next(
                        (
                            kw.value
                            for kw in node.keywords
                            if kw.arg
                            in (
                                "file",
                                "filepath_or_buffer",
                                "path",
                                "io",
                                "path_or_buf",
                            )
                        ),
                        None,
                    )
                if first is not None and not _is_local_path_literal(first):
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
