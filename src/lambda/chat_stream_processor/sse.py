# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Dependency-free helpers for the chat streaming endpoint.

Kept separate from ``app.py`` (which imports FastAPI and the processor modules)
so the SSE framing can be unit-tested without those heavier runtime dependencies.

The caller's identity is not derived here. The SigV4 principal the Function URL
forwards is the same for every user of the Identity Pool — measured on a live
commercial deployment, every set of browser credentials carries the role session
name ``CognitoIdentityCredentials`` (499 ``AssumeRoleWithWebIdentity`` events
across three identity pools, no exception) — so it names no particular user.
``app.py`` takes the caller from the verified Cognito ID token instead.
"""

from __future__ import annotations

import json
from datetime import datetime


def now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sse(payload: dict) -> str:
    """Encode one event as a Server-Sent-Events frame: ``data: {json}\\n\\n``."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
