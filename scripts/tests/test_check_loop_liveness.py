# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the Stop hook in ``scripts/hooks/check_loop_liveness.py``.

Two properties carry this guard and each is tested in both directions. It must
block a live backlog run whose turn has nothing tracked -- the failure that cost
5 h 32 m once and 101 minutes again -- and it must stay completely silent
everywhere else, because a Stop hook that blocks ordinary turns gets deleted and
then protects nothing.

The verifiable half is also tested as verifiable: a recorded background pid that
is dead must block, and a recorded wakeup already in the past must block. A guard
that accepted any record at all would pass a suite that only checked for the
record's presence.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "check_loop_liveness.py"


def iso(offset_seconds: float = 0.0) -> str:
    moment = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_seconds)
    return moment.isoformat().replace("+00:00", "Z")


def run_hook(
    tmp_path: Path,
    state: dict[str, Any] | None,
    *,
    stop_hook_active: bool = False,
    env_extra: dict[str, str] | None = None,
    session_id: str = "s1",
) -> subprocess.CompletedProcess[str]:
    if state is not None:
        scratch = tmp_path / "scratch"
        scratch.mkdir(exist_ok=True)
        (scratch / "backlog-run-state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )

    payload: dict[str, Any] = {"session_id": session_id}
    if stop_hook_active:
        payload["stop_hook_active"] = True

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CLAUDE_PROJECT_DIR": str(tmp_path),
    }
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def blocked(result: subprocess.CompletedProcess[str]) -> bool:
    """A block is ``continue: false`` on stdout, not a non-zero exit."""
    if not result.stdout.strip():
        return False
    decision = json.loads(result.stdout)
    return decision.get("continue") is False


def live_run(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "runId": "2026-10-20-1",
        "sessionId": "s1",
        "halted": None,
        "inFlight": [{"agent": "a1", "issues": [1146], "pr": 1197}],
        "ranked": [{"issues": [1150], "score": 16}],
    }
    state.update(overrides)
    return state


# --------------------------------------------------------------------------
# The block
# --------------------------------------------------------------------------


def test_a_live_run_with_no_liveness_record_is_blocked(tmp_path: Path) -> None:
    result = run_hook(tmp_path, live_run())
    assert blocked(result), result.stdout
    reason = json.loads(result.stdout)["stopReason"]
    assert "ends the run" in reason


def test_the_block_reason_carries_all_three_remedies(tmp_path: Path) -> None:
    """The message must be actionable in the moment it appears.

    A refusal that only says "no" costs the run the time it was meant to save,
    so each of the three ways to satisfy the invariant is spelled out with the
    JSON to write.
    """
    result = run_hook(tmp_path, live_run())
    reason = json.loads(result.stdout)["stopReason"]
    assert "background-bash" in reason
    assert "wakeup" in reason
    assert "subagent" in reason
    assert "scratch/backlog-run-state.json" in reason
    assert "ALLOW_IDLE_TURN=1" in reason


def test_a_dead_background_pid_is_blocked(tmp_path: Path) -> None:
    """The verifiable half must actually verify.

    pid 2**22 is above the default pid_max on Linux, so it cannot be running.
    """
    state = live_run(
        liveness={
            "kind": "background-bash",
            "ref": str(2**22),
            "recordedAt": iso(-30),
        }
    )
    result = run_hook(tmp_path, state)
    assert blocked(result), result.stdout
    assert "no longer running" in json.loads(result.stdout)["stopReason"]


def test_a_wakeup_already_in_the_past_is_blocked(tmp_path: Path) -> None:
    state = live_run(
        liveness={"kind": "wakeup", "ref": iso(-60), "recordedAt": iso(-120)}
    )
    result = run_hook(tmp_path, state)
    assert blocked(result), result.stdout
    assert "already fired" in json.loads(result.stdout)["stopReason"]


def test_a_stale_record_is_blocked(tmp_path: Path) -> None:
    """A record older than the heartbeat interval describes a child that returned."""
    state = live_run(
        liveness={
            "kind": "background-bash",
            "ref": str(os.getpid()),
            "recordedAt": iso(-40 * 60),
        }
    )
    result = run_hook(tmp_path, state)
    assert blocked(result), result.stdout
    assert "minutes old" in json.loads(result.stdout)["stopReason"]


@pytest.mark.parametrize(
    "liveness",
    [
        {"kind": "background-bash", "ref": "123"},  # no recordedAt
        {"kind": "background-bash", "ref": "123", "recordedAt": "not a date"},
        {"kind": "wakeup", "ref": "not a date", "recordedAt": iso(-5)},
        {"kind": "something-else", "ref": "x", "recordedAt": iso(-5)},
    ],
)
def test_an_unusable_record_is_blocked(
    tmp_path: Path, liveness: dict[str, Any]
) -> None:
    result = run_hook(tmp_path, live_run(liveness=liveness))
    assert blocked(result), result.stdout


# --------------------------------------------------------------------------
# The other direction: what must NOT be blocked
# --------------------------------------------------------------------------


def test_a_session_with_no_state_file_is_never_blocked(tmp_path: Path) -> None:
    """This is the ordinary case: every session that is not running the loop."""
    result = run_hook(tmp_path, None)
    assert not blocked(result)
    assert result.returncode == 0


def test_a_live_background_pid_is_allowed(tmp_path: Path) -> None:
    state = live_run(
        liveness={
            "kind": "background-bash",
            "ref": str(os.getpid()),
            "recordedAt": iso(-30),
        }
    )
    result = run_hook(tmp_path, state)
    assert not blocked(result), result.stdout


def test_a_future_wakeup_is_allowed(tmp_path: Path) -> None:
    state = live_run(
        liveness={"kind": "wakeup", "ref": iso(1200), "recordedAt": iso(-5)}
    )
    result = run_hook(tmp_path, state)
    assert not blocked(result), result.stdout


def test_a_recorded_subagent_is_allowed_and_the_limit_is_stated(
    tmp_path: Path,
) -> None:
    """The unverifiable case must say it did not check, not imply it did."""
    state = live_run(
        liveness={"kind": "subagent", "ref": "a1b2c3", "recordedAt": iso(-5)}
    )
    result = run_hook(tmp_path, state)
    assert not blocked(result), result.stdout
    assert "cannot verify" in result.stderr


def test_a_halted_run_is_not_blocked(tmp_path: Path) -> None:
    state = live_run(halted="memory pressure that could not be relieved")
    result = run_hook(tmp_path, state)
    assert not blocked(result), result.stdout


def test_a_finished_run_is_not_blocked(tmp_path: Path) -> None:
    """Empty ``inFlight`` and ``ranked`` is how a run says it is done."""
    state = live_run(inFlight=[], ranked=[])
    result = run_hook(tmp_path, state)
    assert not blocked(result), result.stdout


def test_an_unparseable_state_file_is_not_blocked(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "backlog-run-state.json").write_text("{ not json", encoding="utf-8")
    result = run_hook(tmp_path, None)
    assert not blocked(result)
    assert result.returncode == 0


# --------------------------------------------------------------------------
# The two things that stop this becoming a wedge
# --------------------------------------------------------------------------


def test_stop_hook_active_is_honoured_before_anything_else(tmp_path: Path) -> None:
    """Without this a blocking Stop hook is an infinite loop.

    The harness sets it once this hook has already blocked, and returning
    success while it is true is what lets the turn eventually end.
    """
    result = run_hook(tmp_path, live_run(), stop_hook_active=True)
    assert not blocked(result)
    assert result.returncode == 0
    assert result.stderr == ""


def test_the_override_allows_the_turn_and_says_what_it_disabled(
    tmp_path: Path,
) -> None:
    result = run_hook(tmp_path, live_run(), env_extra={"ALLOW_IDLE_TURN": "1"})
    assert not blocked(result)
    assert "disabled by ALLOW_IDLE_TURN=1" in result.stderr
    assert "nothing will re-invoke this session" in result.stderr


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_a_non_affirmative_override_leaves_the_guard_on(
    tmp_path: Path, value: str
) -> None:
    result = run_hook(tmp_path, live_run(), env_extra={"ALLOW_IDLE_TURN": value})
    assert blocked(result), result.stdout


def test_an_unreadable_payload_allows_the_turn(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "backlog-run-state.json").write_text(
        json.dumps(live_run()), encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "CLAUDE_PROJECT_DIR": str(tmp_path),
        },
        check=False,
    )
    assert result.returncode == 0
    assert not result.stdout.strip()


# --------------------------------------------------------------------------
# Ownership and freshness -- the fix for a defect this hook shipped with
# --------------------------------------------------------------------------


def test_another_sessions_state_file_does_not_block_this_turn(
    tmp_path: Path,
) -> None:
    """The defect: a run belongs to one coordinator, and this is not it.

    Caught by the hook refusing the first unrelated turn it ever saw, on a state
    file written two days earlier by a different session.
    """
    result = run_hook(tmp_path, live_run(sessionId="someone-else"))
    assert not blocked(result), result.stdout


def test_this_sessions_state_file_still_blocks(tmp_path: Path) -> None:
    """The other direction, or the ownership test would make the guard vacuous."""
    result = run_hook(tmp_path, live_run(sessionId="s1"), session_id="s1")
    assert blocked(result), result.stdout


def test_a_stale_unowned_state_file_does_not_block(tmp_path: Path) -> None:
    """``scratch/`` is not cleaned up, so an abandoned file must expire.

    Without this, a state file with no ``sessionId`` -- every file written before
    this hook existed -- refuses every turn in the repository until somebody
    deletes it, and the remedy people reach for is removing the hook.
    """
    state = live_run()
    state.pop("sessionId")
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    target = scratch / "backlog-run-state.json"
    target.write_text(json.dumps(state), encoding="utf-8")
    old = dt.datetime.now(dt.timezone.utc).timestamp() - 6 * 60 * 60
    os.utime(target, (old, old))

    result = run_hook(tmp_path, None)
    assert not blocked(result), result.stdout


def test_a_fresh_unowned_state_file_still_blocks(tmp_path: Path) -> None:
    """The freshness fallback must not become a way to never block.

    A file with no recorded owner that was written minutes ago is a live run by
    an older coordinator, and that is exactly the case the guard is for.
    """
    state = live_run()
    state.pop("sessionId")
    result = run_hook(tmp_path, state)
    assert blocked(result), result.stdout


def test_the_real_leftover_state_file_in_this_checkout_does_not_block(
    tmp_path: Path,
) -> None:
    """Regression test keyed on the actual shape that tripped the hook.

    Reproduced rather than described: ``halted`` null, work outstanding, no
    ``sessionId``, last written days ago. If this ever blocks again, every
    session in this repository is blocked.
    """
    state = {
        "runId": "2026-10-18-1",
        "startedAt": "2026-10-18T12:40:00Z",
        "halted": None,
        "inFlight": [{"agent": "x", "issues": [1499]}],
        "ranked": [{"issues": [1500]}, {"issues": [1501]}],
    }
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    target = scratch / "backlog-run-state.json"
    target.write_text(json.dumps(state), encoding="utf-8")
    two_days = dt.datetime.now(dt.timezone.utc).timestamp() - 48 * 60 * 60
    os.utime(target, (two_days, two_days))

    result = run_hook(tmp_path, None)
    assert not blocked(result), result.stdout
