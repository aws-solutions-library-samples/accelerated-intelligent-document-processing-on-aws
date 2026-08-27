#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Instrumented browser session exposed to a Strands agent as tools.

ASYNC BY NECESSITY: Strands executes tools inside an asyncio event loop, and
Playwright's SYNC api cannot be driven from one — doing so tears the browser down
mid-run with `TargetClosedError: Target page, context or browser has been closed`.
So this uses playwright.async_api throughout and the tools are coroutines.

DESIGN RULE 5 (see agentic_uat_design.md): difficulty is MEASURED, never
self-reported. An agent asked "how many clicks did you take?" estimates, and is
wrong. So the session owns a Playwright BrowserContext with an init script that
counts clicks / field edits / navigations in a capture-phase listener — the same
approach as src/ui/e2e/fixtures/test-base.ts, and for the same reason: it sees every
click the agent causes, including the extra ones a Cloudscape dropdown or modal forces.

The agent is given a deliberately SMALL tool surface. It sees the page as an
accessibility snapshot (roles + names), not pixels: cheaper, and it forces the agent
to navigate by the same semantics a screen-reader user would.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Page, async_playwright

# Mirrors the counter in src/ui/e2e/fixtures/test-base.ts. `input` (not keydown) is
# what programmatic fills dispatch, so field edits are counted from `input`.
# NOTE: must be an IIFE. Playwright's Python add_init_script() takes JS *source* and
# executes it; a bare `() => {...}` expression evaluates to a function value that is
# then discarded, so the listeners are never attached and every counter reads 0.
# (The JS API accepts a function object and calls it — the Python API does not.)
INSTRUMENT_JS = """
(() => {
  const prior = (() => { try {
    return JSON.parse(sessionStorage.getItem('__uat_metrics') || 'null');
  } catch { return null; } })();
  window.__uat = prior || { clicks: 0, fieldEdits: 0, navigations: 0, routes: [] };
  const persist = () => { try {
    sessionStorage.setItem('__uat_metrics', JSON.stringify(window.__uat));
  } catch {} };
  const route = () => {
    const r = location.hash || '#/';
    if (window.__uat.routes[window.__uat.routes.length - 1] !== r) {
      window.__uat.routes.push(r);
    }
    persist();
  };
  document.addEventListener('click', () => { window.__uat.clicks++; persist(); }, true);
  document.addEventListener('input', () => { window.__uat.fieldEdits++; persist(); }, true);
  window.addEventListener('hashchange', () => { window.__uat.navigations++; route(); });
  window.addEventListener('popstate', () => { window.__uat.navigations++; route(); });
  route();
})();
"""


@dataclass
class Stage:
    """One observable step of the agent's attempt.

    `agent_saw` is the tool result the model actually consumed — usually an
    accessibility tree, never an image. The screenshot is for the HUMAN reviewer only;
    conflating the two would misrepresent what the agent had to work with.
    """

    index: int
    tool: str
    args: str
    agent_saw: str
    url: str
    clicks_so_far: int
    elapsed_ms: int
    screenshot: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "tool": self.tool,
            "args": self.args,
            "agent_saw": self.agent_saw,
            "url": self.url,
            "clicks_so_far": self.clicks_so_far,
            "elapsed_ms": self.elapsed_ms,
            "screenshot": self.screenshot,
        }


@dataclass
class Measured:
    clicks: int = 0
    field_edits: int = 0
    navigations: int = 0
    routes: list[str] = field(default_factory=list)
    tool_calls: int = 0
    vision_calls: int = 0
    page_errors: list[str] = field(default_factory=list)
    collect_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "clicks": self.clicks,
            "field_edits": self.field_edits,
            "navigations": self.navigations,
            "routes": self.routes,
            "tool_calls": self.tool_calls,
            "vision_calls": self.vision_calls,
            "page_errors": self.page_errors,
            "collect_error": self.collect_error,
        }


class BrowserSession:
    """A single agent's browser. One session per worker, never shared: a warm run
    must not inherit a cold run's cookies, storage or counters."""

    def __init__(
        self,
        base_url: str,
        storage_state: str | None = None,
        headless: bool = True,
        capture_dir: str | None = None,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self._storage_state = storage_state
        self._headless = headless
        self.measured = Measured()
        self.stages: list[Stage] = []
        self._capture_dir = Path(capture_dir) if capture_dir else None
        if self._capture_dir:
            self._capture_dir.mkdir(parents=True, exist_ok=True)
        self._t0 = time.monotonic()
        self._pw = None
        self._browser = None
        self._ctx = None
        self.page: Page | None = None

    async def __aenter__(self) -> BrowserSession:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self._headless)
        self._ctx = await self._browser.new_context(
            base_url=self.base_url,
            storage_state=self._storage_state,
            viewport={"width": 1440, "height": 900},
        )
        # add_init_script survives reloads and same-document navigation.
        await self._ctx.add_init_script(INSTRUMENT_JS.strip())
        self.page = await self._ctx.new_page()
        self.page.on("pageerror", lambda e: self.measured.page_errors.append(str(e)))
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._collect()
        for closer in (self._ctx, self._browser):
            try:
                await closer.close()  # type: ignore[union-attr]
            except Exception:
                pass
        try:
            await self._pw.stop()  # type: ignore[union-attr]
        except Exception:
            pass

    async def sync_metrics(self) -> None:
        """Snapshot the in-browser counters into `measured`.

        Called after EVERY action, not only at exit: the counters live in page context,
        so if the final action leaves a navigation in flight (or closes the page) a
        single read at teardown loses the whole run's metrics and the report shows a
        friction-free zero. Incremental sync means the last good reading survives.
        """
        await self._collect()

    async def _collect(self) -> None:
        """Read the in-browser counters.

        Records WHY collection failed rather than silently reporting zeros — a metric
        that quietly reads 0 is worse than one that says it broke, because a run with
        no instrumentation looks like a run with no friction.
        """
        try:
            m = await self.page.evaluate("() => window.__uat || null")  # type: ignore[union-attr]
        except Exception as exc:
            self.measured.collect_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            return
        if m is None:
            self.measured.collect_error = "window.__uat was undefined at collection time"
            return
        # Monotonic: never let a mid-navigation read (fresh page, counters back at 0)
        # overwrite a higher accumulated value.
        self.measured.clicks = max(self.measured.clicks, int(m.get("clicks", 0) or 0))
        self.measured.field_edits = max(self.measured.field_edits, int(m.get("fieldEdits", 0) or 0))
        self.measured.navigations = max(self.measured.navigations, int(m.get("navigations", 0) or 0))
        routes = m.get("routes") or []
        if len(routes) >= len(self.measured.routes):
            self.measured.routes = routes
        self.measured.collect_error = None

    async def _capture(self, tool: str, args: str, agent_saw: str) -> None:
        """Record a stage: viewport screenshot for the human, plus the exact tool
        result the agent received. Failures here must never break the run — a missing
        frame is a worse report, not a failed test."""
        idx = len(self.stages) + 1
        shot: str | None = None
        if self._capture_dir is not None:
            name = f"stage-{idx:02d}-{tool}.png"
            try:
                # Viewport (not full_page): faster, and matches what a user sees at once.
                await self.page.screenshot(path=str(self._capture_dir / name))  # type: ignore[union-attr]
                shot = name
            except Exception:
                shot = None
        try:
            url = self.page.url  # type: ignore[union-attr]
        except Exception:
            url = "(unavailable)"
        self.stages.append(
            Stage(
                index=idx,
                tool=tool,
                args=args,
                agent_saw=agent_saw if len(agent_saw) <= 4000 else agent_saw[:4000] + " …[truncated]",
                url=url,
                clicks_so_far=self.measured.clicks,
                elapsed_ms=int((time.monotonic() - self._t0) * 1000),
                screenshot=shot,
            )
        )

    # ---------------------------------------------------------------- tools ----
    # Each returns a short string the agent can reason over. Errors are returned as
    # text, never raised: a tool that throws ends the agent turn, and "that click
    # did nothing" is exactly the signal we want recorded rather than fatal.

    async def snapshot(self, max_chars: int = 6000) -> str:
        """Accessibility-tree snapshot of the current page."""
        self.measured.tool_calls += 1
        try:
            snap = await self.page.locator("body").aria_snapshot()  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - defensive
            return f"ERROR taking snapshot: {exc}"
        url = self.page.url  # type: ignore[union-attr]
        if len(snap) > max_chars:
            snap = snap[:max_chars] + f"\n… [truncated, {len(snap)} chars total]"
        out = f"URL: {url}\n\n{snap}"
        await self._capture("snapshot", "", out)
        return out

    async def goto(self, path: str) -> str:
        """Navigate. Use a RELATIVE path (e.g. '#/documents'): a leading slash
        discards the baseURL path, which breaks APIGateway-hosted stacks served
        under /api/."""
        self.measured.tool_calls += 1
        try:
            await self.page.goto(path.lstrip("/") or "./", wait_until="domcontentloaded")  # type: ignore[union-attr]
            await self.page.wait_for_timeout(1500)  # type: ignore[union-attr]
            await self.sync_metrics()
            out = f"navigated to {self.page.url}"  # type: ignore[union-attr]
            await self._capture("navigate", path, out)
            return out
        except Exception as exc:
            out = f"ERROR navigating to {path}: {exc}"
            await self._capture("navigate", path, out)
            return out

    async def click(self, role: str, name: str, exact: bool = False) -> str:
        """Click by accessible role + name, e.g. role='button', name='Run Test'."""
        self.measured.tool_calls += 1
        try:
            loc = self.page.get_by_role(role, name=name, exact=exact)  # type: ignore[union-attr]
            n = await loc.count()
            if n == 0:
                out = f"NOT FOUND: no {role} named '{name}'. Take a snapshot to see what exists."
                await self._capture("click", f"{role}={name!r}", out)
                return out
            if n > 1:
                out = (
                    f"AMBIGUOUS: {n} elements match {role} '{name}'. "
                    "Retry with exact=true, or pick a more specific name."
                )
                await self._capture("click", f"{role}={name!r}", out)
                return out
            await loc.first.click(timeout=15000)
            await self.page.wait_for_timeout(1200)  # type: ignore[union-attr]
            await self.sync_metrics()
            out = f"clicked {role} '{name}'; now at {self.page.url}"  # type: ignore[union-attr]
            await self._capture("click", f"{role}={name!r}", out)
            return out
        except Exception as exc:
            out = f"ERROR clicking {role} '{name}': {str(exc)[:300]}"
            await self._capture("click", f"{role}={name!r}", out)
            return out

    async def fill(self, label: str, value: str) -> str:
        """Type into a field found by its accessible label or placeholder."""
        self.measured.tool_calls += 1
        try:
            loc = self.page.get_by_label(label)  # type: ignore[union-attr]
            if await loc.count() == 0:
                loc = self.page.get_by_placeholder(label)  # type: ignore[union-attr]
            if await loc.count() == 0:
                return f"NOT FOUND: no field labelled '{label}'."
            await loc.first.fill(value, timeout=15000)
            await self.sync_metrics()
            out = f"filled '{label}'"
            await self._capture("fill", f"{label}={value!r}", out)
            return out
        except Exception as exc:
            out = f"ERROR filling '{label}': {str(exc)[:300]}"
            await self._capture("fill", f"{label}={value!r}", out)
            return out

    async def screenshot_bytes(self) -> bytes | None:
        """Raw PNG of the viewport, for the vision tool. Returns None on failure so a
        broken frame degrades to a text-only step rather than ending the run."""
        self.measured.tool_calls += 1
        self.measured.vision_calls += 1
        try:
            png = await self.page.screenshot()  # type: ignore[union-attr]
        except Exception:
            return None
        await self._capture("look", "(vision)", "[returned a screenshot to the model]")
        return png

    async def read_text(self, pattern: str | None = None) -> str:
        """Visible text of the page, optionally only lines matching a regex."""
        self.measured.tool_calls += 1
        try:
            txt = await self.page.locator("body").inner_text(timeout=15000)  # type: ignore[union-attr]
        except Exception as exc:
            return f"ERROR reading text: {exc}"
        if pattern:
            try:
                rx = re.compile(pattern, re.I)
            except re.error as exc:
                return f"ERROR bad regex: {exc}"
            lines = [ln for ln in txt.splitlines() if rx.search(ln)]
            return "\n".join(lines[:80]) or f"(no lines matching /{pattern}/)"
        return txt[:6000]


def summarise(measured: Measured) -> str:
    return json.dumps(measured.as_dict(), indent=2)
