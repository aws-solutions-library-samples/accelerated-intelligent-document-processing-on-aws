# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The resolvers' local ``ReportedServerError`` classes carry the canonical name.

Only ``errorType`` and the message cross ``lambda:Invoke``, so the dispatcher
recognises a resolver's reported 500 by the exception's class **name**. A resolver
that does not import ``idp_common`` declares its own class of that name, and nothing
but this test ties that spelling to ``idp_common.api_adapter.ReportedServerError``:
a rename on either side would turn every reported 500 into the generic unexpected
error, with each side's own tests still green.

The resolvers and the dispatcher are read with ``ast`` rather than imported: each
resolver directory defines a module named ``index``, and importing one creates its
AWS clients at module scope.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from idp_common import api_adapter

pytestmark = pytest.mark.unit

CANONICAL_REFERENCE = "idp_common.api_adapter.ReportedServerError"

#: The resolvers known to declare a local copy today. Required to be found, so the
#: derived set below cannot pass by being empty.
RESOLVERS_WITH_A_LOCAL_COPY = {
    "get_agent_chat_messages_resolver",
    "get_file_contents_resolver",
    "get_sample_document_resolver",
    "list_agent_chat_sessions_resolver",
}


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "nested" / "api-resolvers").is_dir():
            return parent
    raise RuntimeError("Could not locate repo root containing nested/api-resolvers")


LAMBDA_ROOT = _repo_root() / "nested" / "api-resolvers" / "src" / "lambda"
DISPATCHER = LAMBDA_ROOT / "http_api_dispatcher" / "index.py"


def _local_twins() -> list[tuple[str, str]]:
    """Every class under the resolvers that stands in for the canonical one.

    A class counts when it has the canonical name **or** its docstring names the
    canonical class, so a local copy renamed away from the canonical spelling is
    still found and reported rather than dropping out of the comparison.
    """
    twins = []
    for path in sorted(LAMBDA_ROOT.rglob("*.py")):
        if (
            path.name.startswith("test_")
            or "tests" in path.relative_to(LAMBDA_ROOT).parts
        ):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            doc = ast.get_docstring(node) or ""
            if node.name == "ReportedServerError" or CANONICAL_REFERENCE in doc:
                twins.append((path.relative_to(LAMBDA_ROOT).parts[0], node.name))
    return twins


def _dispatcher_match_names() -> list[str]:
    """The ``err_type == "<name>"`` strings whose branch raises the canonical class."""
    tree = ast.parse(DISPATCHER.read_text(encoding="utf-8"), filename=str(DISPATCHER))
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        raises_reported = any(
            isinstance(stmt, ast.Raise)
            and isinstance(stmt.exc, ast.Call)
            and isinstance(stmt.exc.func, ast.Name)
            and stmt.exc.func.id == "ReportedServerError"
            for stmt in node.body
        )
        test = node.test
        if (
            raises_reported
            and isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "err_type"
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
        ):
            names.append(test.comparators[0].value)
    return names


def test_every_local_copy_carries_the_canonical_name():
    canonical = api_adapter.ReportedServerError.__name__
    twins = _local_twins()
    assert RESOLVERS_WITH_A_LOCAL_COPY <= {resolver for resolver, _ in twins}, twins
    mismatched = [(resolver, name) for resolver, name in twins if name != canonical]
    assert not mismatched, (
        f"the dispatcher matches {canonical!r} by name; these local copies differ: "
        f"{mismatched}"
    )


def test_the_dispatcher_matches_the_canonical_name():
    assert _dispatcher_match_names() == [api_adapter.ReportedServerError.__name__]
