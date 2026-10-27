# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A stand-in ``ux-story`` server on stdio for ``record.py``: one tool, no journal.

It answers ``initialize``, ``ping``, ``tools/list`` with the real server's
``report_done`` definition and ``tools/call`` with the real server's first reply to
it, so the recorded session holds one tool call and one tool result exactly as a
watched story's would, and nothing about any product.
"""

from __future__ import annotations

import json
import sys
from typing import Any

REPORT_DONE = {
    "name": "report_done",
    "description": "Report that the task is done, with what on screen shows it.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "evidence": {"type": "string", "maxLength": 400},
            "stopped_at_commit_point": {"type": "boolean"},
        },
        "required": ["evidence", "stopped_at_commit_point"],
        "additionalProperties": False,
    },
}
RECORDED = "Recorded. Now write a short factual summary of what you saw, then stop."


def answer(message: dict[str, Any]) -> dict[str, Any] | None:
    """The reply to one JSON-RPC message, or None for a notification."""
    if "id" not in message:
        return None
    method = message.get("method")
    if method == "initialize":
        version = message.get("params", {}).get("protocolVersion", "2025-06-18")
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "ux-story", "version": "1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [REPORT_DONE]}
    elif method == "tools/call":
        reply = {"ok": True, "recorded": RECORDED}
        result = {
            "content": [{"type": "text", "text": json.dumps(reply)}],
            "isError": False,
        }
    else:
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -32601, "message": "Method not found"},
        }
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = answer(json.loads(line))
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
