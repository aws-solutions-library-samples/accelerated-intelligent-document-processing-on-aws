# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Dependency-free helpers for the chat streaming endpoint.

Kept separate from ``app.py`` (which imports FastAPI and the processor modules)
so the pure logic — SSE framing and caller-identity resolution — can be
unit-tested without those heavier runtime dependencies.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime


def now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sse(payload: dict) -> str:
    """Encode one event as a Server-Sent-Events frame: ``data: {json}\\n\\n``."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class CallerIdentityConflict(Exception):
    """A client-asserted caller identity contradicts the verified one.

    ``app.py`` translates this into a 403. It is a distinct exception (rather
    than the HTTP one) so the decision itself stays here, testable without
    FastAPI.
    """


def history_key(identity: Mapping | None) -> str:
    """The key a verified caller's chat history is filed under.

    ``email``, else ``cognito:username``, else ``sub``: the value the REST path
    puts in ``identity.username`` (``normalize_event`` in
    ``idp_common.api_adapter``) and its chat read resolvers key on, so a turn
    streamed here and one sent over REST belong to the same history.
    """
    claims = (identity or {}).get("claims") or {}
    return str(
        claims.get("email") or claims.get("cognito:username") or claims.get("sub") or ""
    )


def resolve_caller_sub(verified: str, claimed: str) -> str:
    """Resolve the caller identity a turn is attributed to.

    ``verified`` is the :func:`history_key` of the verified ID token; ``claimed``
    is whatever the request body asserted. The verified value always wins, and a
    claimed value that differs from it raises ``CallerIdentityConflict``: a
    client-supplied identity that disagrees with the token is never legitimate,
    so the request is refused rather than resolved in either direction.
    """
    if not verified:
        raise CallerIdentityConflict("no verified caller identity to attribute to")
    if claimed and claimed != verified:
        raise CallerIdentityConflict(
            "body-supplied caller identity does not match the verified ID token"
        )
    return verified
