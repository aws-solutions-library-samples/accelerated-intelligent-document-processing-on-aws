# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A stand-in for chrome-devtools-mcp 1.10.1 on stdio, answering from the recordings.

The relay suite starts it through the relay's own command line, under ``env -i``,
so a wrapper script the test writes names the scenario file on its command line
ahead of the flags the relay passes. It answers ``initialize`` and ``tools/list``
with what the pinned server answered in ``session.json`` and ``tools_list.json``,
and each ``tools/call`` with the next answer the scenario lists for that tool, the
last one repeating, or a recorded default. Every message it receives is appended to
the scenario's ``record`` file as one JSON line, after a first line holding its
argv, its environment and its working directory.

An answer is ``{"label": <a call in session.json>}``, ``{"result": ...}`` or
``{"error": ...}``, and may also carry ``send_first`` (messages written before the
answer, a string written as the line itself), ``delay`` (seconds), ``image_bytes`` (the size of the synthetic JPEG an
image item carries), ``hang`` (never answered), ``exit`` (the code to exit with
instead), or ``oversize`` (a line of that many bytes written instead).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
JPEG_START = b"\xff\xd8\xff\xe0"
JPEG_END = b"\xff\xd9"
DEFAULT_IMAGE_BYTES = 2048
DEFAULT_LABELS = {
    "navigate_page": "navigate_documents",
    "wait_for": "wait_for",
    "click": "click_refresh",
    "hover": "hover_reprocess",
    "fill": "fill_filter",
    "fill_form": "fill_form",
    "type_text": "type_text",
    "press_key": "press_key",
    "drag": "drag",
    "handle_dialog": "handle_alert_accept",
    "take_snapshot": "snapshot_documents",
    "take_screenshot": "screenshot_jpeg",
}


def synthetic_jpeg(size: int) -> str:
    """Base64 of ``size`` bytes that begin and end the way a JPEG does."""
    body = max(size - len(JPEG_START) - len(JPEG_END), 0)
    return base64.b64encode(JPEG_START + b"\x00" * body + JPEG_END).decode("ascii")


def recorded(session: dict[str, Any], label: str) -> dict[str, Any]:
    """The recorded JSON-RPC answer of one call, without its id."""
    answer = dict(session["calls"][label]["answer"])
    answer.pop("id", None)
    answer.pop("jsonrpc", None)
    return answer


class Fake:
    """One scripted server, writing to stdout and recording to the scenario's file."""

    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario
        self.record = Path(scenario["record"])
        self.session = json.loads((HERE / "session.json").read_text(encoding="utf-8"))
        self.tools = json.loads((HERE / "tools_list.json").read_text(encoding="utf-8"))
        self.answers = {
            tool: list(listed) for tool, listed in scenario.get("calls", {}).items()
        }

    def log(self, entry: dict[str, Any]) -> None:
        with self.record.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def send(self, message: object) -> None:
        line = message if isinstance(message, str) else json.dumps(message)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def answer_for(self, tool: str) -> dict[str, Any]:
        listed = self.answers.get(tool)
        if listed:
            return listed.pop(0) if len(listed) > 1 else listed[0]
        return {"label": DEFAULT_LABELS.get(tool, "click_refresh")}

    def body(self, spec: dict[str, Any]) -> dict[str, Any]:
        if "label" in spec:
            body = recorded(self.session, spec["label"])
        elif "error" in spec:
            body = {"error": spec["error"]}
        else:
            body = {"result": spec.get("result", {"content": []})}
        result = body.get("result")
        if isinstance(result, dict):
            size = int(spec.get("image_bytes", DEFAULT_IMAGE_BYTES))
            for item in result.get("content", []):
                if isinstance(item, dict) and item.get("type") == "image":
                    item["data"] = synthetic_jpeg(size)
        return body

    def call(self, message: dict[str, Any]) -> int | None:
        """Answer one tools/call as the scenario says; an exit code ends the fake."""
        params = message.get("params") or {}
        spec = self.answer_for(str(params.get("name")))
        for first in spec.get("send_first", ()):
            self.send(first)
        if "delay" in spec:
            time.sleep(float(spec["delay"]))
        if "exit" in spec:
            return int(spec["exit"])
        if spec.get("hang"):
            return None
        if "oversize" in spec:
            sys.stdout.write("x" * int(spec["oversize"]) + "\n")
            sys.stdout.flush()
            return None
        self.send({"jsonrpc": "2.0", "id": message.get("id"), **self.body(spec)})
        return None

    def serve(self) -> int:
        for line in sys.stdin:
            if not line.strip():
                continue
            message = json.loads(line)
            self.log({"in": message})
            method = message.get("method")
            request_id = message.get("id")
            if method == "initialize":
                result = self.scenario.get("initialize", self.session["initialize"])
                self.send({"jsonrpc": "2.0", "id": request_id, "result": result})
            elif method == "notifications/initialized":
                for queued in self.scenario.get("after_initialized", ()):
                    self.send(queued)
            elif method == "tools/list":
                result = self.scenario.get("tools", self.tools)
                self.send({"jsonrpc": "2.0", "id": request_id, "result": result})
            elif method == "ping":
                self.send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            elif method == "tools/call":
                code = self.call(message)
                if code is not None:
                    return code
        return 0


def main(argv: list[str]) -> int:
    position = argv.index("--scenario")
    scenario = json.loads(Path(argv[position + 1]).read_text(encoding="utf-8"))
    fake = Fake(scenario)
    fake.log(
        {
            "argv": argv[position + 2 :],
            "environ": dict(os.environ),
            "cwd": os.getcwd(),
        }
    )
    if scenario.get("exit_at_start") is not None:
        return int(scenario["exit_at_start"])
    return fake.serve()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
