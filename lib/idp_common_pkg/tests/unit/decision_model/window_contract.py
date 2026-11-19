# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The decision model's input window, as the fake decision-model Lambdas enforce it.

The decider refuses a request whose state, images and longest question do not fit
its window (``CONTEXT_WINDOW_TOKENS``, less ``PROMPT_RESERVE_TOKENS`` for its own
prompt around them). A fake that answers a request of any size let callers send
whole sections and pass every test while half of a benchmark set lost its
confidence scores (#2032). So every fake decision-model Lambda in the unit tests
calls :func:`window_refusal` on each payload and, when it returns a response,
returns that instead of an answer: a ``ValueError`` function error, which the real
client raises as ``DecisionModelRefused``, as it would for the real decider.

Sizes are estimated the way ``idp_common.decision_model.budget`` estimates them,
so this checks that callers cut to the budget they claim to cut to. It does not
check that the budget's constants match the deployed model; that needs the
endpoint and is a live probe's job (#2053).

A refusal is also recorded in :data:`OVERSIZE`, because a caller may turn a refusal
into a failed section that a test does not look at. Each module with a fake binds
:func:`no_oversize_requests` (an autouse fixture) by importing it, so an oversize request fails
the test that sent it whatever the product code did with the refusal. A test that
sends one on purpose clears :data:`OVERSIZE` after asserting on it.
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional, Union

import pytest

from idp_common.decision_model import IMAGE_TOKENS, estimate_tokens
from idp_common.decision_model.budget import PROMPT_RESERVE_TOKENS
from idp_common.decision_model.wire import CONTEXT_WINDOW_TOKENS

#: What a request's state, images and longest question may take together: the
#: window, less what the decider's own prompt around them takes.
WINDOW_INPUT_TOKENS = CONTEXT_WINDOW_TOKENS - PROMPT_RESERVE_TOKENS

#: Every oversize request a fake refused since the last check, as
#: ``(estimated tokens, request)``.
OVERSIZE: List[tuple] = []


def _text(content: Any) -> str:
    """The text the decider reads from a state or instructions value."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def question_tokens(question: Dict[str, Any]) -> float:
    """A question's instructions and every criterion, names and descriptions."""
    criteria = question.get("criteria") or {}
    if isinstance(criteria, dict):
        parts = [f"{name} {_text(text)}" for name, text in criteria.items()]
    else:
        parts = [_text(level) for level in criteria]
    return estimate_tokens("\n".join([_text(question.get("instructions")), *parts]))


def request_tokens(request: Dict[str, Any]) -> float:
    """The estimated size the window must hold: state, images, longest question."""
    questions = request.get("questions") or {}
    longest = max((question_tokens(q) for q in questions.values()), default=0.0)
    images = len(request.get("images") or [])
    return (
        estimate_tokens(_text(request.get("state"))) + images * IMAGE_TOKENS + longest
    )


def window_refusal(
    payload: Union[bytes, str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """The decider's refusal of ``payload`` if it overflows the window, else None."""
    request = json.loads(payload) if isinstance(payload, (bytes, str)) else payload
    tokens = request_tokens(request)
    if tokens <= WINDOW_INPUT_TOKENS:
        return None
    OVERSIZE.append((tokens, request))
    error = {
        "errorType": "ValueError",
        "errorMessage": (
            f"prompt is about {tokens:.0f} tokens, over the {WINDOW_INPUT_TOKENS} "
            "the context window holds beside the decider's own prompt"
        ),
    }
    return {
        "StatusCode": 200,
        "FunctionError": "Unhandled",
        "Payload": io.BytesIO(json.dumps(error).encode()),
    }


@pytest.fixture(autouse=True)
def no_oversize_requests():
    """Fail the test if any fake refused a request over the window during it."""
    OVERSIZE.clear()
    yield
    sizes = [round(tokens) for tokens, _ in OVERSIZE]
    OVERSIZE.clear()
    assert not sizes, (
        f"{len(sizes)} decision-model request(s) over the {WINDOW_INPUT_TOKENS}-token "
        f"window were sent, estimated at {sizes} tokens"
    )


#: Dense page text in three shapes, each well over the window on its own: a table
#: of figures (every digit a token), short lines of CJK text (a token a
#: character), and long lines of plain words. Every shape has line breaks, as OCR
#: text does. (The cut backs off to the last line break, so text whose only one
#: is near its start keeps almost nothing; that fits the window, so it is not
#: what this module checks.)
DENSE_STYLES = ("figures", "cjk", "words")


def dense_page(style: str, page: int) -> str:
    """One page of ``style`` text, about 4,000-5,000 estimated tokens."""
    if style == "figures":
        return "\n".join(
            f"{page:02d}-{i:05d} 2026-11-{i % 28 + 1:02d} "
            f"{i * 7919 % 100000:>9,}.{i % 100:02d} | {i * 31 % 997:03d}"
            for i in range(150)
        )
    if style == "cjk":
        return "\n".join(
            f"第{page}頁 {i}行 請求書の合計金額と明細行" * 3 for i in range(70)
        )
    return "\n".join(
        f"Page {page} line {i}: " + "statement carried forward balance " * 12
        for i in range(24)
    )
