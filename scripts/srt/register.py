#!/usr/bin/env python3
"""Restore the committed SRT disposition register before a scan.

Why this exists
---------------
SRT keeps its working state in the gitignored ``.srt/issues.json``. The
dispositions the team has agreed on — ``suppressed`` / ``resolved`` HIGH findings
with a written reason — live in the **committed** ``scripts/srt/issues.json``.
CI restores the committed file over the live one (``make srt-setup``) before it
scans, so the scanner sees every disposition and the gate reads green.

``make srt-scan`` on its own did not. It merged the new scan into whatever
``.srt/issues.json`` already held. On a working tree whose live state predated
suppressions committed later, the scanner met those findings "for the first
time" and opened them: the v0.6.8 release validation saw **10 false open HIGH
findings** from exactly this. Worse, the same mechanism runs the other way — a
live file carrying a *stale* ``suppressed`` that was since removed from the
committed register would hide a real finding locally.

So every scan now starts from the committed register. The one thing that must
not happen is silently discarding a disposition someone made locally but has
not yet saved: ``srt-fix`` copies back automatically, but a hand-edit or a raw
``./srt fix`` does not. Those are detected and the scan refuses until the
operator either saves them (``make srt-fix``) or opts to drop them.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

# The tuple SRT keys a disposition on. An entry whose key matches keeps its
# status across scans; anything else is a new finding.
_KEY_FIELDS = ("path", "resourceType", "resourceName", "check_id")


def _key(issue: dict) -> tuple:
    return tuple(issue.get(f) for f in _KEY_FIELDS)


def _is_high(issue: dict) -> bool:
    return (issue.get("priority") or "").upper() == "HIGH"


def _is_dispositioned(issue: dict) -> bool:
    return (issue.get("status") or "").lower() not in ("", "open")


def _load(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


@dataclass
class RestoreResult:
    """What ``restore_committed_register`` did, for the caller to print."""

    action: str  # "restored" | "no-committed-register" | "refused"
    committed_count: int = 0
    # Dispositioned HIGH findings present in the live file but absent from the
    # committed register — the ones a plain overwrite would have thrown away.
    unsynced: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.action != "refused"


def unsynced_dispositions(
    live: Iterable[dict],
    committed: Iterable[dict],
    is_ci_visible: Callable[[dict], bool] | None = None,
) -> list[dict]:
    """Live HIGH dispositions the committed register does not know about.

    Only what ``fix.py`` would itself persist counts: HIGH priority, not Open,
    and (when a predicate is given) in a file CI checks out — a suppression on
    a gitignored build artifact can never reach the committed register, so its
    absence there is not "unsynced", it is by design.
    """
    known = {_key(i) for i in committed}
    out = []
    for issue in live:
        if not (_is_high(issue) and _is_dispositioned(issue)):
            continue
        if is_ci_visible is not None and not is_ci_visible(issue):
            continue
        if _key(issue) not in known:
            out.append(issue)
    return out


def restore_committed_register(
    committed_path: Path,
    live_path: Path,
    *,
    is_ci_visible: Callable[[dict], bool] | None = None,
    discard_local: bool = False,
) -> RestoreResult:
    """Copy the committed register over the live one, unless that would lose work.

    - No committed register: do nothing (a fresh checkout with no baseline).
    - Live file holds HIGH dispositions the committed one lacks and
      ``discard_local`` is False: refuse, returning them, so the caller can
      tell the operator to save them first.
    - Otherwise: overwrite live with committed (``shutil.copy2``, same as
      ``setup.py``) and report how many entries were restored.
    """
    if not committed_path.exists():
        return RestoreResult(action="no-committed-register")

    committed = _load(committed_path)
    live = _load(live_path) if live_path.exists() else []

    unsynced = unsynced_dispositions(live, committed, is_ci_visible)
    if unsynced and not discard_local:
        return RestoreResult(
            action="refused", committed_count=len(committed), unsynced=unsynced
        )

    live_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(committed_path, live_path)
    return RestoreResult(action="restored", committed_count=len(committed))


def describe_unsynced(issues: list[dict]) -> str:
    """One line per unsynced disposition, for the refusal message."""
    lines = []
    for i in issues:
        lines.append(
            f"  {i.get('status'):<10} {i.get('check_id') or '?':<14} "
            f"{i.get('path') or '<no path>'}  ({i.get('resourceName') or '-'})"
        )
    return "\n".join(lines)
