# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The chat streaming ID-token header is the same in the endpoint, the UI and CORS.

The browser sends the user's Cognito ID token to the chat Function URL in a
custom header, and the endpoint refuses a request without it. A custom header on
a cross-origin request needs the Function URL's CORS configuration to allow it,
or the browser's preflight fails and every streamed chat turn is refused before
it is sent. Nothing else offline would notice: the endpoint's tests and the UI's
tests each use their own copy of the name.

So the name is read from both sources — ``ID_TOKEN_HEADER`` in
``src/lambda/chat_stream_processor/app.py`` and in
``src/ui/src/api/stream-client.ts`` — and required in the ``AllowHeaders`` of
every ``AWS::Lambda::Url`` whose target is the chat stream function.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gate_premises  # noqa: E402

REPO = gate_premises.REPO_ROOT


def _endpoint_header() -> str:
    tree = ast.parse((REPO / "src/lambda/chat_stream_processor/app.py").read_text())
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "ID_TOKEN_HEADER"
                for t in node.targets
            )
            and isinstance(node.value, ast.Constant)
        ):
            return str(node.value.value)
    raise AssertionError("app.py defines no ID_TOKEN_HEADER constant")


def _ui_header() -> str:
    text = (REPO / "src/ui/src/api/stream-client.ts").read_text()
    match = re.search(r"export const ID_TOKEN_HEADER = '([^']+)'", text)
    assert match, "stream-client.ts exports no ID_TOKEN_HEADER constant"
    return match.group(1)


def _stream_function_urls() -> list[dict]:
    resources = gate_premises.load_template("template.yaml").get("Resources") or {}
    urls = []
    for res in resources.values():
        if not isinstance(res, dict) or res.get("Type") != "AWS::Lambda::Url":
            continue
        target = (res.get("Properties") or {}).get("TargetFunctionArn")
        if target == {"Fn::Ref": "ChatStreamProcessorFunction"}:
            urls.append(res["Properties"])
    return urls


@pytest.mark.unit
def test_the_endpoint_and_the_ui_name_the_same_header():
    assert _endpoint_header() == _ui_header()
    assert _endpoint_header().lower() == _endpoint_header()


@pytest.mark.unit
def test_the_function_url_allows_the_header_cross_origin():
    urls = _stream_function_urls()
    assert urls, "no AWS::Lambda::Url targets ChatStreamProcessorFunction"
    header = _endpoint_header()
    for props in urls:
        allowed = [h.lower() for h in (props.get("Cors") or {}).get("AllowHeaders", [])]
        assert header in allowed, (
            f"{header} is not in the chat Function URL's Cors.AllowHeaders "
            f"({allowed}); the browser's preflight would refuse every streamed turn"
        )
