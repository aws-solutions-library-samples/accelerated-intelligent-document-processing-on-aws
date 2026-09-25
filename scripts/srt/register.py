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


# --------------------------------------------------------------------------- #
# Non-vacuity: a suppression that shields nothing
# --------------------------------------------------------------------------- #

#: Sources whose absence from a scan is *evidence* that the finding is gone, mapped to
#: the scanner name :mod:`scanner_health` knows and the summary file it writes at
#: ``.srt/`` root in this register's own schema.
#:
#: Membership turns on one question and nothing else: **if this scan reported no finding
#: matching an entry, does that mean the finding no longer exists?** Bandit answers yes.
#: It runs locally over every file with rules compiled into the package, so the finding
#: set is a function of the tree alone, and it is the source the suppression register
#: accumulated 52 dead entries under. Everything else is in
#: :data:`NON_VACUITY_EXEMPT_SOURCES` with the reason absence is not evidence for it,
#: and the two sets are asserted to cover every source in the register.
WHOLE_REPO_SUMMARIES = {"Bandit": ("bandit", "bandit-summary.json")}

#: Sources whose findings this check does NOT measure, one entry, one reason. Absence of
#: a finding is not evidence for these, and a check that read it as evidence would delete
#: a live suppression on a bad day — the one failure this must not have.
#:
#: These are not "not yet done". Each names a specific mechanism by which the finding set
#: moves without the tree moving, which is exactly what makes absence uninformative.
NON_VACUITY_EXEMPT_SOURCES: dict[str, str] = {
    "security-matrix": (
        "SRT's own AWS-resource checks, evaluated per template into a per-template scan "
        "directory with no whole-repo summary. A template whose scan did not complete "
        "contributes zero findings and looks identical to a template that is clean; "
        "scanner_health.failed_checkov_scans exists because that happens routinely on "
        "the largest templates here"
    ),
    "Checkov": (
        "Per-template, same as above, and additionally sensitive to what is on disk: "
        "`sam package` re-serialises templates and drops the `# checkov:skip=` comments "
        "the source templates carry, so a built tree produces a different finding set "
        "from a clean one"
    ),
    "Semgrep": (
        "Rules come from a remote registry rather than from the installed package, so a "
        "withdrawn rule or a failed ruleset fetch removes findings with no change to "
        "this repository. The one suppressed Semgrep entry is a supply-chain rule about "
        "src/ui/.npmrc whose own reason turns on the npm version the build uses, which "
        "is not a property of the tree either"
    ),
}


def _match_key(issue: dict) -> tuple:
    """:func:`_key` with the path normalised, for comparing across two producers.

    The committed register and a scanner summary are written by different code paths, so
    a ``./`` prefix or a backslash on one side would read as "no match" — and this check
    turns "no match" into "delete the entry". Normalising here rather than in
    :func:`_key` leaves the restore path's behaviour exactly as it was.
    """
    path, resource_type, resource_name, check_id = _key(issue)
    if path:
        path = str(path).replace("\\", "/").removeprefix("./")
    return (path, resource_type, resource_name, check_id)


def vacuous_suppressions(
    committed: Iterable[dict], findings: Iterable[dict], *, sources: Iterable[str]
) -> list[dict]:
    """Committed ``suppressed`` entries for ``sources`` that ``findings`` does not contain.

    **Why a suppression that shields nothing is not merely untidy.** SRT keys a
    disposition on ``(path, resourceType, resourceName, check_id)``, so an entry whose
    finding has been fixed in the source does not go inert: it pre-suppresses whatever
    finding of that check next appears in that file. A genuine hardcoded credential
    landing in a pre-registered file is suppressed on arrival, with nothing to notice.
    That is the dead-exemption hazard this repository's gate doctrine describes —
    an exemption shielding nothing is not neutral, it is an approval waiting for a new
    occupant — and the register had no check for it while 52 entries were in that state.

    Only ``suppressed`` counts. A ``resolved`` entry records that something was fixed and
    does not shield: SRT re-opens it on re-detection, which gates. Deleting resolved
    entries for being absent would therefore remove the record that makes a regression
    visible.
    """
    wanted = set(sources)
    findings = list(findings)
    if not findings:
        # A summary with no findings at all is not a clean tree, it is a scanner that
        # produced nothing -- and reading it as evidence would delete every suppression
        # for that source at once, which is the one failure this check must not have.
        # scanner_health only asks whether the summary file is fresh, so an empty-but-
        # fresh summary passes there and would arrive here as "everything is dead".
        return []
    seen = {_match_key(f) for f in findings}
    return [
        issue
        for issue in committed
        if issue.get("source") in wanted
        and (issue.get("status") or "").lower() == "suppressed"
        and _match_key(issue) not in seen
    ]


def suppressed_sources(committed: Iterable[dict]) -> set[str]:
    """The ``source`` of every ``suppressed`` entry in the register.

    The universe the two source sets above have to cover between them. Only suppressed
    entries, because only they shield anything.
    """
    return {
        issue.get("source") or "<none>"
        for issue in committed
        if (issue.get("status") or "").lower() == "suppressed"
    }


def describe_vacuous(issues: list[dict]) -> str:
    """One line per dead suppression, for the failure message."""
    return "\n".join(
        f"  {i.get('check_id') or '?':<8} {i.get('path') or '<no path>'}:"
        f"{i.get('line', '?')}  {(i.get('issue') or '')[:60]}"
        for i in issues
    )
