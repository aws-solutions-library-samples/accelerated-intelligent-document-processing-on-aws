#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Refuse to end a backlog-loop turn that has nothing tracked.

Registered as a ``Stop`` hook in ``.claude/settings.json``. It reads the hook
payload on stdin and, when a backlog run is live, blocks the end of the turn
unless the run state records a child that is demonstrably still going.

**The failure this exists for.** ``.claude/skills/work-the-backlog.md`` states
the invariant in its ground rules: a turn that ends with no live subagent, no
``run_in_background`` command and no scheduled wakeup **is the end of the run**,
because nothing re-invokes the session and it idles until a human types. That has
happened twice in one run -- once for 5 h 32 m, while the CI the loop believed it
was waiting for had gone green inside the first 28 minutes, and once for 101
minutes during a promotion. In both cases every code-level gate was green and the
loop had simply stopped producing.

The skill's own remedy is a self-restarting heartbeat child, and the skill is
candid that it is "deliberately structural rather than discretionary, because the
failure it prevents is a coordinator not following a rule it had itself just
written down". A background child is still a thing the coordinator has to
remember to restart. This hook is the half that does not depend on remembering:
it refuses the turn.

**What it can and cannot verify, stated plainly, because a liveness check that
overstates its reach is worse than none.** A ``Stop`` hook is a short-lived
subprocess. It cannot call ``ListAgents``, so it cannot see subagents at all, and
it cannot enumerate the harness's background tasks. So the state file has to
carry the claim and this hook verifies what is verifiable:

* ``kind: "background-bash"`` with a ``pid`` -- **verified**, via ``os.kill(pid,
  0)``. A dead pid blocks.
* ``kind: "wakeup"`` with an ISO-8601 ``ref`` -- **verified** against the clock. A
  wakeup whose time has passed blocks.
* ``kind: "subagent"`` with an agent id -- **not verifiable here**. Allowed, and
  the hook says so on stderr rather than implying it checked. This is the honest
  residual: an agent that died without a notification looks alive to this hook.

So the guarantee is "the coordinator recorded a live child, and if that record
was checkable it checked out" -- not "a child is alive". The recording is the
part that was missing in both measured incidents, which is why this is worth
having anyway.

**It is scoped to a live backlog run and silent otherwise**, because blocking
every turn of every ordinary session would be intolerable and would train
whoever hit it to delete the hook. Three conditions have to hold together, and
the last two are what make "live" mean live:

* ``scratch/backlog-run-state.json`` exists, ``halted`` is null, and there is
  work outstanding (a non-empty ``inFlight`` or ``ranked``).
* **This session owns the run**: the file's ``sessionId`` equals the hook
  payload's ``session_id``. A run belongs to one coordinator, so a state file
  another session wrote must never refuse this one's turn.
* **The file is fresh**: written within ``MAX_STATE_AGE_SECONDS``. A live run
  rewrites it before and after every dispatch, merge and check-in, so a file
  nobody has touched for hours describes a run that is over.

⚠️ **The ownership and freshness tests carry the scoping, and without them this
guard is a repository-wide tripwire.** ``scratch/`` is gitignored but it is not
cleaned up, so a state file outlives the run that wrote it: a run abandoned
without emptying ``inFlight`` leaves a file that claims live work forever, and a
guard that blocks every turn in a repository until someone deletes a file is
worse than no guard, because the remedy people reach for is removing the hook.
Measured here on a two-day-old file from another session.

Both tests are needed and neither subsumes the other. ``sessionId`` is the
precise one, but it is absent from any state file a coordinator wrote without it,
and treating "no sessionId" as "not mine" would make the guard silently vacuous
for exactly the runs most likely to need it. So a file with no ``sessionId``
falls back to freshness alone, and a stale file is disregarded whoever wrote it.

**``stop_hook_active`` is honoured first**, before anything else is read. The
harness sets it when a ``Stop`` hook has already blocked and the model is being
given another turn; returning success while it is true is what stops a blocking
hook from becoming an infinite loop. ``CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`` bounds
it on the harness side as well.

Override: ``ALLOW_IDLE_TURN=1`` in the environment, for the legitimate case of
deliberately parking a run mid-flight. An honoured override prints a line to
stderr naming what it disabled, for the same reason the sibling hooks do.

**What this does not cover.** It keeps the *loop* running; it does not keep the
*session* alive. Nothing here survives the process dying, an interrupt, or the
user typing a prompt (which also kills every background agent). It cannot
distinguish a turn that ended because the work is done from one that ended by
mistake, except through the state file -- which is exactly why the state file is
the input.

Run the tests with ``make test-hooks``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

STATE_PATH = Path("scratch/backlog-run-state.json")

OVERRIDE = "ALLOW_IDLE_TURN"

AFFIRMATIVE = {"1", "true", "yes", "y", "on"}

#: How stale a liveness record may be before it stops counting as this turn's.
#: The heartbeat child in the skill returns every 30 minutes, so a record older
#: than that describes a child that has already come back.
MAX_RECORD_AGE_SECONDS = 35 * 60

#: How stale the state *file* may be before the run it describes is treated as
#: over. A live run rewrites it before and after every dispatch, merge and
#: check-in, so this is generous by a wide margin; it exists to stop an abandoned
#: file refusing turns forever, not to time a healthy run out.
MAX_STATE_AGE_SECONDS = 4 * 60 * 60

REMEDY = """\
Name the live child before ending the turn, then end it. Any one of these
satisfies the invariant, and each is two lines:

  1. A backgrounded Bash command, whose exit re-invokes you. Record its pid:
       {"kind": "background-bash", "ref": "<pid>", "recordedAt": "<iso8601>"}
  2. A booked wakeup via ScheduleWakeup. Record the time it fires:
       {"kind": "wakeup", "ref": "<iso8601 when it fires>", "recordedAt": "<iso8601>"}
  3. A live subagent. Record its id (this hook cannot verify it):
       {"kind": "subagent", "ref": "<agent id>", "recordedAt": "<iso8601>"}

Write it to the "liveness" key of scratch/backlog-run-state.json.

If the run is genuinely finished, say so in the state file -- empty "inFlight"
and "ranked", or set "halted" -- and this check goes quiet. If you are parking
the run deliberately, re-run with ALLOW_IDLE_TURN=1 in the environment."""


def _affirmative(value: str | None) -> bool:
    return value is not None and value.strip().lower() in AFFIRMATIVE


def _load_state(root: Path) -> tuple[dict[str, Any], float] | None:
    """Return the parsed state and its mtime, or ``None`` if unusable."""
    path = root / STATE_PATH
    try:
        with path.open(encoding="utf-8") as handle:
            state = json.load(handle)
        mtime = path.stat().st_mtime
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    return state, mtime


def _run_is_live(
    state: dict[str, Any],
    mtime: float,
    session_id: Any,
    now: dt.datetime,
) -> bool:
    """Whether this session owns a backlog run that is still going.

    Three independent reasons to say no, and the ownership and freshness ones
    are the fix for the defect described in the module docstring: a state file
    outlives its run, so without them an abandoned file refuses every turn in
    the repository until somebody deletes it.
    """
    if state.get("halted"):
        return False
    if not (state.get("inFlight") or state.get("ranked")):
        return False

    # Ownership, when the file records it. A run belongs to one coordinator
    # session; another session's file is not this turn's business.
    owner = state.get("sessionId")
    if isinstance(owner, str) and owner.strip():
        return bool(session_id) and owner.strip() == str(session_id).strip()

    # No recorded owner: fall back to freshness alone. Treating this as "not
    # mine" would make the guard vacuous for every state file written before it
    # existed, which are the runs most likely to need it.
    age = now.timestamp() - mtime
    return age <= MAX_STATE_AGE_SECONDS


def _parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _pid_alive(ref: Any) -> bool:
    try:
        pid = int(str(ref).strip())
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive and owned by somebody else. Counting it as alive is the
        # conservative direction for a check that blocks.
        return True
    except OSError:
        return False
    return True


def evaluate(
    state: dict[str, Any],
    now: dt.datetime,
    mtime: float = 0.0,
    session_id: Any = None,
) -> tuple[str | None, list[str]]:
    """Return a block reason, or ``None`` to allow, plus stderr notices."""
    notices: list[str] = []

    if not _run_is_live(state, mtime, session_id, now):
        return None, notices

    record = state.get("liveness")
    if not isinstance(record, dict):
        return (
            "No live child recorded for this backlog run, so ending the turn "
            "ends the run.\n\n" + REMEDY
        ), notices

    recorded_at = _parse_iso(record.get("recordedAt"))
    if recorded_at is None:
        return (
            'The "liveness" record carries no readable "recordedAt", so it '
            "cannot be told apart from a stale one.\n\n" + REMEDY
        ), notices

    age = (now - recorded_at).total_seconds()
    if age > MAX_RECORD_AGE_SECONDS:
        minutes = int(age // 60)
        return (
            f'The "liveness" record is {minutes} minutes old, so the child it '
            "names has already come back.\n\n" + REMEDY
        ), notices

    kind = record.get("kind")
    ref = record.get("ref")

    if kind == "background-bash":
        if not _pid_alive(ref):
            return (
                f"The recorded background command (pid {ref}) is no longer "
                "running, so nothing will re-invoke you.\n\n" + REMEDY
            ), notices
        return None, notices

    if kind == "wakeup":
        fires_at = _parse_iso(ref)
        if fires_at is None:
            return (
                'The recorded wakeup has no readable fire time in "ref".\n\n' + REMEDY
            ), notices
        if fires_at <= now:
            return (
                f"The recorded wakeup was due at {ref} and has already fired, "
                "so nothing is booked.\n\n" + REMEDY
            ), notices
        return None, notices

    if kind == "subagent":
        notices.append(
            f"check-loop-liveness: allowing on a recorded subagent ({ref}). "
            "This hook cannot verify a subagent is alive -- it has no access to "
            "ListAgents -- so run the sweep in section 0 if the run has been "
            "quiet."
        )
        return None, notices

    return (
        f'The "liveness" record has an unrecognised kind ({kind!r}). Expected '
        '"background-bash", "wakeup" or "subagent".\n\n' + REMEDY
    ), notices


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return 0

    if not isinstance(payload, dict):
        return 0

    # First, and before reading anything else: a hook that has already blocked
    # must not block again, or the turn cannot end at all.
    if payload.get("stop_hook_active"):
        return 0

    if _affirmative(os.environ.get(OVERRIDE)):
        print(
            f"check-loop-liveness: disabled by {OVERRIDE}=1. A turn ending with "
            "nothing tracked ends the run; nothing will re-invoke this session.",
            file=sys.stderr,
        )
        return 0

    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or ".")

    try:
        loaded = _load_state(root)
        if loaded is None:
            return 0
        state, mtime = loaded
        reason, notices = evaluate(
            state,
            dt.datetime.now(dt.timezone.utc),
            mtime=mtime,
            session_id=payload.get("session_id"),
        )
    except Exception:  # noqa: BLE001 - a guard must not wedge the session
        return 0

    for line in notices:
        print(line, file=sys.stderr)

    if reason is None:
        return 0

    json.dump({"continue": False, "stopReason": reason}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
