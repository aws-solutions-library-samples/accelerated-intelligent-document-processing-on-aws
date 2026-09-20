# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Guard against drift between the chat processor sources and the committed
vendored copies the chat_stream_processor package builds from.

The vendored copies exist because SAM's makefile builder runs against an
isolated copy of the function's CodeUri and cannot reach sibling Lambda
directories at build time. If a processor changes without re-running
scripts/sync_chat_stream_vendored.sh, this test fails.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "src" / "lambda" / "chat_stream_processor").is_dir():
            return parent
    raise RuntimeError("repo root not found")


def test_vendored_modules_match_source():
    root = _repo_root()
    pairs = [
        (
            root / "src/lambda/chat_with_document_processor/index.py",
            root / "src/lambda/chat_stream_processor/vendored/chat_with_document_processor.py",
        ),
        (
            root / "src/lambda/agent_chat_processor/index.py",
            root / "src/lambda/chat_stream_processor/vendored/agent_chat_processor.py",
        ),
    ]
    for source, vendored in pairs:
        assert vendored.exists(), f"missing vendored copy: {vendored}"
        # Compare BYTES, not decoded text. `cp` makes a byte copy, so that is what
        # "in sync" has to mean here; comparing `read_text()` would accept a copy
        # that differs by a BOM or an encoding change, which is exactly the
        # divergence a reader would never spot in a diff.
        assert source.read_bytes() == vendored.read_bytes(), (
            f"{vendored.name} is out of sync with {source}. "
            f"Run scripts/sync_chat_stream_vendored.sh."
        )
