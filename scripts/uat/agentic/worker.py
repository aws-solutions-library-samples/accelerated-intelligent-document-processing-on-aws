#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A single agentic usability worker: attempt one claim, report friction.

DESIGN RULE 1 — INFORMATION BARRIER. A COLD worker gets only the product's *promise*
(a CHANGELOG claim). A WARM worker also gets the documented procedure. The barrier is
enforced by TYPE, not by prompt wording: `Flow.docs` is None for cold runs and the
prompt builder cannot reach a procedure that isn't there. If the orchestrator ever
briefs a cold worker on the steps, we stop measuring discoverability and start measuring
clickability — which the deterministic Layer-1 suite already covers.

DESIGN RULE 2 — the worker NEVER decides whether it succeeded. It reports what it
*claims* to have done, in machine-checkable terms, and verify.py (non-agentic) confirms
it. `verdict` here is the agent's self-assessment of DIFFICULTY, deliberately separate
from `accomplished`, which only the verifier may set.

DESIGN RULE 5 — clicks/field-edits/navigations come from browser instrumentation, never
from the agent. The agent is not asked to count anything.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from strands import Agent
from strands.models import BedrockModel

from browser import BrowserSession

DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


@dataclass
class Flow:
    """One testable claim. `docs` is None for a cold run — see DESIGN RULE 1."""

    flow_id: str
    claim: str
    docs: str | None = None
    documented_steps: int | None = None

    @property
    def mode(self) -> Literal["cold", "warm"]:
        return "warm" if self.docs else "cold"


class Confusion(BaseModel):
    where: str = Field(description="URL or page name where the confusion occurred")
    what_was_unclear: str = Field(description="What was ambiguous, missing or misleading")
    what_i_expected: str = Field(description="What you expected to happen instead")


class WorkerReport(BaseModel):
    """The agent's report. Note what is ABSENT: no click counts (measured), and no
    authority to declare success (the verifier decides)."""

    claimed_action: str = Field(
        description=(
            "Precisely what you believe you created/changed, in checkable terms, "
            "including any name or ID you chose. If you did not complete the task, "
            "say what you got as far as."
        )
    )
    reached_goal: bool = Field(
        description="Your honest belief about whether you completed the task. May be wrong; it will be verified."
    )
    difficulty: Literal["trivial", "easy", "moderate", "hard", "impossible"] = Field(
        description="How hard was this to accomplish through the UI?"
    )
    blocked_by_precondition: bool = Field(
        description=(
            "True if you could not attempt the task because required prior state was "
            "missing (e.g. no documents exist to build a test set from). This is NOT a "
            "usability failure."
        )
    )
    confusions: list[Confusion] = Field(
        default_factory=list,
        description="Every point where the UI was unclear. This is the most valuable output.",
    )
    dead_ends: list[str] = Field(
        default_factory=list,
        description="Places you looked that turned out to be wrong, e.g. 'opened Configuration looking for test sets'",
    )
    docs_mismatch: list[str] = Field(
        default_factory=list,
        description="WARM runs only: where the documented procedure did not match the UI.",
    )
    narrative: str = Field(description="Short account of what you did, in order.")

    # Models routinely emit an explicit `null` for "nothing to report" rather than
    # omitting the key or sending []. default_factory only covers an ABSENT key, so a
    # literal null fails list validation and loses the whole report — which is how a
    # completed run ends up with no findings at all. Coerce instead.
    @field_validator("confusions", "dead_ends", "docs_mismatch", mode="before")
    @classmethod
    def _none_to_empty(cls, v: object) -> object:
        return [] if v is None else v


SYSTEM_COLD = """You are evaluating whether a software feature is USABLE, by trying to
use it as a competent but first-time user would.

You have never seen this application before. You have NOT read its documentation. You
know only what the release notes claim is now possible. Your job is to find out whether
a real user could achieve that, and to record honestly every point at which you were
confused, misled, or had to guess.

Rules:
- Explore. Take a snapshot to see the page before acting.
- Do not give up at the first obstacle, but do not brute-force forever.
- If the UI is confusing, that is a FINDING, not your failure. Record it.
- Never invent success. If you could not do it, say so.
- If required prior data is missing (no documents, no configuration), set
  blocked_by_precondition — that is not a usability problem.
- Prefer clicking what a user would click. Do not navigate by typing URLs unless a real
  user plausibly would.
- You have `look()`, which shows the page as an image. Use it when the accessibility
  snapshot is not enough to judge the experience: layout, visual hierarchy, whether a
  control looks clickable or disabled, truncated or overlapping text. Use it sparingly.
- If you can SEE a control in the image but cannot target it by role and name, say so
  explicitly in confusions — a control with no accessible name is a real defect.
"""

SYSTEM_WARM = """You are evaluating whether a software feature's DOCUMENTATION is
accurate and sufficient.

You have the documented procedure. Follow it literally. Where the documentation does not
match what you see — a control named differently, a step that does not exist, a missing
prerequisite it never mentions — record that in docs_mismatch. That mismatch is the
finding.

Rules:
- Follow the documented steps in order, as written.
- Take a snapshot before acting so you can compare the UI against the docs.
- Never invent success. If the documented procedure does not work, say so.
- If required prior data is missing, set blocked_by_precondition.
"""


def build_prompt(flow: Flow) -> str:
    parts = [f"# The claim to verify\n\n{flow.claim}\n"]
    if flow.docs:
        # WARM only. Unreachable when docs is None — the barrier is structural.
        parts.append(f"# The documented procedure\n\n{flow.docs}\n")
        parts.append(
            "Follow the documented procedure. Record every place the docs and the UI disagree."
        )
    else:
        parts.append(
            "You have no documentation. Work out whether this is possible, and do it. "
            "Start by looking at the page you land on."
        )
    return "\n".join(parts)


async def _run_worker_async(
    flow: Flow,
    base_url: str,
    storage_state: str | None = None,
    model_id: str = DEFAULT_MODEL,
    region: str = "us-west-2",
    max_tool_calls: int = 40,
    max_vision_calls: int = 6,
    vision: bool = True,
    headless: bool = True,
    capture_dir: str | None = None,
) -> dict:
    """Run one worker. Returns a dict ready for verify.py + the report."""
    async with BrowserSession(
        base_url, storage_state=storage_state, headless=headless, capture_dir=capture_dir
    ) as sess:
        # Land the agent on the app root so it starts where a user would.
        await sess.goto("./")

        # NOTE: do NOT wrap these in a decorator that changes the signature.
        # Strands' @tool introspects the function signature to build the tool schema;
        # a `def wrapper(*args, **kwargs)` erases the typed parameters, the generated
        # schema comes out empty, and the model then emits an unusable tool call
        # ("No valid tool use ... found in the Bedrock response"). The budget check is
        # therefore inline rather than a decorator.
        def _over_budget() -> str | None:
            if sess.measured.tool_calls >= max_tool_calls:
                return (
                    f"STOP: tool-call budget of {max_tool_calls} exhausted. Stop "
                    "exploring and summarise what you found, including that you ran "
                    "out of budget."
                )
            return None

        from strands import tool

        @tool
        async def snapshot() -> str:
            """Get an accessibility-tree snapshot of the current page: every role and
            accessible name available to click or read. Use this before acting."""
            return _over_budget() or await sess.snapshot()

        @tool
        async def navigate(path: str) -> str:
            """Navigate to a relative path within the app, e.g. '#/documents'.
            Prefer clicking links over calling this."""
            return _over_budget() or await sess.goto(path)

        @tool
        async def click(role: str, name: str) -> str:
            """Click an element by accessible role and name, e.g.
            role='button', name='Create test set'."""
            return _over_budget() or await sess.click(role, name)

        @tool
        async def fill(label: str, value: str) -> str:
            """Type a value into a form field identified by its visible label."""
            return _over_budget() or await sess.fill(label, value)

        @tool
        async def look() -> dict:
            """Look at the page as an IMAGE. Use this when the accessibility tree is not
            enough: to judge layout, visual hierarchy, whether something looks clickable
            or disabled, truncated or overlapping text, or when you are stuck and want to
            see what a person would see. Expensive — use sparingly."""
            if sess.measured.vision_calls >= max_vision_calls:
                return {
                    "status": "success",
                    "content": [
                        {
                            "text": f"Vision budget of {max_vision_calls} images "
                            "exhausted. Continue using snapshot() instead."
                        }
                    ],
                }
            png = await sess.screenshot_bytes()
            if png is None:
                return {"status": "error", "content": [{"text": "could not capture a screenshot"}]}
            return {
                "status": "success",
                "content": [
                    {"text": f"Screenshot of {sess.page.url}"},
                    {"image": {"format": "png", "source": {"bytes": png}}},
                ],
            }

        @tool
        async def read_text(pattern: str = "") -> str:
            """Read the page's visible text. Pass a regex to return only matching lines."""
            return _over_budget() or await sess.read_text(pattern or None)

        agent = Agent(
            model=BedrockModel(model_id=model_id, region_name=region, temperature=0.3),
            # ACTIONS go through the accessibility tree (role + name), never pixel
            # coordinates: that keeps every step reproducible and promotable into a
            # deterministic Playwright spec. `look` is OBSERVATION only. A control the
            # agent can see but cannot target by name then becomes an explicit finding
            # instead of being invisible.
            tools=([snapshot, navigate, click, fill, read_text, look] if vision
                   else [snapshot, navigate, click, fill, read_text]),
            system_prompt=SYSTEM_WARM if flow.docs else SYSTEM_COLD,
        )

        prompt = build_prompt(flow)
        error: str | None = None
        report: WorkerReport | None = None
        transcript = ""
        try:
            # PHASE 1 — exploration. agent(prompt) runs the tool loop. structured_output()
            # on its own does a single extraction pass and never touches the browser, so
            # calling it first yields a confident report about a page never visited.
            result = await agent.invoke_async(prompt)
            transcript = str(result)[:4000]
            # PHASE 2 — extract the structured report from what actually happened.
            report = await agent.structured_output_async(
                WorkerReport,
                "Now report on the attempt you just made, using the schema. Be honest: "
                "if you did not complete the task, say so.",
            )
        except Exception as exc:  # noqa: BLE001 - any agent failure must still yield a row
            error = f"{type(exc).__name__}: {str(exc)[:500]}"

        measured = sess.measured.as_dict()

    hit_cap = measured["tool_calls"] >= max_tool_calls
    return {
        "flow_id": flow.flow_id,
        "mode": flow.mode,
        "documented_steps": flow.documented_steps,
        "measured": measured,
        "hit_tool_cap": hit_cap,
        "vision_enabled": vision,
        "stages": [st.as_dict() for st in sess.stages],
        "agent_error": error,
        "report": json.loads(report.model_dump_json()) if report else None,
        "transcript_tail": transcript,
    }


def run_worker(*args, **kwargs) -> dict:
    """Sync entry point. The whole worker runs in ONE event loop: Playwright's async
    objects are loop-bound, so creating the session in a different loop than the tools
    run in tears the browser down mid-flight."""
    return asyncio.run(_run_worker_async(*args, **kwargs))
