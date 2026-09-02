# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``idp_common.bedrock`` re-exports bound methods; callers use them as functions.

``bedrock/__init__.py`` ends with a handful of hand-maintained lines of the form
``extract_text_from_response = default_client.extract_text_from_response``. Code
throughout the package then calls ``bedrock.extract_text_from_response(...)`` at
module level. A method that exists on ``BedrockClient`` but is missing from that
list therefore raises ``AttributeError`` at the call site — at RUNTIME, in the
Lambda, on whichever code path first needs it.

That is not hypothetical. ``extract_tool_use_from_response`` shipped absent from
the list, so forced tool use (WS-05) raised ``AttributeError`` and failed every
section it was enabled for. It passed review and a 22-test helper suite because
nothing exercised the module-level name.

This file closes the hole from both ends: every name the package calls as
``bedrock.<name>(...)`` must resolve, and every re-export must still point at a
real client method.
"""

from __future__ import annotations

import ast
import os

import pytest

import idp_common.bedrock as bedrock
from idp_common.bedrock.client import BedrockClient

PKG = os.path.dirname(os.path.dirname(os.path.abspath(bedrock.__file__)))

#: Attribute accesses of the form ``bedrock.<name>`` that are NOT calls into the
#: re-export surface. Kept explicit so the sweep stays honest.
_NOT_CALLS = {"__version__", "__all__", "__file__", "__name__"}


def _module_level_bedrock_calls():
    """Every ``bedrock.<name>(...)`` call in the package, with its location.

    Parsed rather than grepped so a name inside a string or comment cannot make
    this test pass or fail for the wrong reason.
    """
    found: list[tuple[str, str, int]] = []
    for root, _dirs, files in os.walk(PKG):
        if "__pycache__" in root:
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            with open(path) as fh:
                try:
                    tree = ast.parse(fh.read(), filename=path)
                except SyntaxError:  # pragma: no cover - not our problem here
                    continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "bedrock"
                    and node.func.attr not in _NOT_CALLS
                ):
                    found.append(
                        (os.path.relpath(path, PKG), node.func.attr, node.lineno)
                    )
    return found


@pytest.mark.unit
def test_the_sweep_finds_call_sites():
    """Guard-the-guard: an empty sweep would make the next test vacuous."""
    calls = _module_level_bedrock_calls()
    assert len(calls) >= 5, f"expected several bedrock.<name>() calls, found {calls}"


@pytest.mark.unit
def test_every_module_level_call_resolves():
    """The WS-05 regression. A missing re-export is an AttributeError in prod."""
    missing = sorted(
        {
            f"{name} (called at {path}:{line})"
            for path, name, line in _module_level_bedrock_calls()
            if not hasattr(bedrock, name)
        }
    )
    assert not missing, (
        "idp_common.bedrock is called as a module but does not export:\n  "
        + "\n  ".join(missing)
        + "\nAdd a re-export line to idp_common/bedrock/__init__.py."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "extract_text_from_response",
        "extract_tool_use_from_response",
        "generate_embedding",
        "format_prompt",
        "invoke_model",
    ],
)
def test_the_reexport_is_callable_and_bound(name):
    """Each re-export must be callable, and (for the client methods) must still
    be bound to a real ``BedrockClient`` method — a rename on the class that
    leaves this file untouched would otherwise pass silently until called."""
    attr = getattr(bedrock, name, None)
    assert callable(attr), f"bedrock.{name} is not callable"
    if name != "invoke_model":  # invoke_model is a module function, not a method
        assert hasattr(BedrockClient, name), (
            f"bedrock.{name} is re-exported but BedrockClient has no such method"
        )
