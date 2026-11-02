# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared helpers for the ProcessResults feature-off golden parity tests.

The goldens under ``goldens/`` were captured from the handlers **before** the
review-queues change touched them (IDP-0002 Step 0.2), so a test comparing a run
against them proves the feature-off path still produces exactly what it did.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from tests.unit._handler_loading import load_with_xray_stubbed

SRC = Path(__file__).resolve().parents[5] / "patterns" / "unified" / "src"
GOLDENS = Path(__file__).resolve().parent / "goldens"


def load_handler(module_name: str, relative_path: str, extra_modules=None):
    """Load a deployed handler by path, with X-Ray (and optional extras) stubbed.

    See ``tests.unit._handler_loading`` for what is and is not left in
    ``sys.modules`` afterwards.
    """
    os.environ.setdefault("AWS_REGION", "us-east-1")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    return load_with_xray_stubbed(module_name, SRC / relative_path, extra_modules)


def normalise(result: Any) -> Any:
    """Round-trip through JSON so the comparison is the stored form."""
    return json.loads(json.dumps(result, sort_keys=True, default=str))


def snapshot(document) -> dict:
    """``Document.to_dict()`` plus the HITL fields it omits when falsy.

    ``to_dict`` drops ``hitl_sections_completed = []``, so the difference between
    "cleared" and "never set" would otherwise be invisible to the comparison.
    """
    return {
        "document": document.to_dict(),
        "hitl": {
            "status": document.hitl_status,
            "triggered": document.hitl_triggered,
            "sections_pending": document.hitl_sections_pending,
            "sections_completed": document.hitl_sections_completed,
        },
    }


def golden_path(handler_dir: str, case_id: str) -> Path:
    return GOLDENS / handler_dir / f"{case_id}.json"


def read_golden(handler_dir: str, case_id: str) -> Any:
    return json.loads(golden_path(handler_dir, case_id).read_text())
