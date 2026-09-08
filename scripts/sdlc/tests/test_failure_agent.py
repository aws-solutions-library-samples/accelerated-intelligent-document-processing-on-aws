"""Tests for the CI failure-analysis agent's deterministic pieces.

The agent itself is a subprocess call to Claude Code and is not exercised here.
What IS tested is everything the pipeline depends on being correct whether or not
the agent runs:

  1. `extract_failure_excerpts` must surface the real cause from a full build
     log. This is the regression guard for job 28666687, where the summary's
     blind `[-150:]` tail held only the concurrent teardown's bucket inventory
     while the actual traceback sat ~1,090 lines earlier.
  2. It must drop the mechanical noise classes (~45% of a real log) so the
     window is not spent on pip output and `Deleted S3 bucket` lines.
  3. `run_failure_agent` must degrade to None — never raise, never hang — so a
     broken agent can only cost the caller its fallback path, not the build.
  4. The agent must never be handed write tools.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import failure_agent as fa  # noqa: E402

# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

# The shape of the 2026-09-08 failure: the real cause buried behind a wall of
# pip output and teardown inventory, with fail-fast SIGKILL collateral after it.
_REAL_CAUSE = (
    "idp_sdk.exceptions.IDPProcessingError: Timeout waiting for test run "
    "Fake-W2-Tax-Forms-20260908-195245 to complete after 300s"
)


def _synthetic_log(noise_lines: int = 1200) -> str:
    lines = []
    lines += [f"Collecting botocore=={i}" for i in range(noise_lines // 2)]
    lines += ["Step 11: Testing test-compare command...", "Test run 1 ID: Fake-W2"]
    lines += ["Traceback (most recent call last):", '  File "testing.py", line 138']
    lines += [_REAL_CAUSE]
    lines += ["❌ Step 11: test-compare failed: Test run 1 completion failed"]
    # Post-failure teardown: what the old 150-line tail actually captured.
    lines += [
        f"Deleted S3 bucket: idp-0908-bucket-{i}" for i in range(noise_lines // 2)
    ]
    lines += ["Deleted log group: /aws/lambda/idp-0908-fn"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 1 + 2: extraction surfaces the cause and drops the noise
# --------------------------------------------------------------------------- #


def test_extraction_surfaces_cause_a_blind_tail_would_miss():
    log = _synthetic_log()
    tail_150 = "\n".join(log.split("\n")[-150:])
    assert _REAL_CAUSE not in tail_150, "fixture must reproduce the blind-tail miss"

    excerpt = fa.extract_failure_excerpts(log)
    assert _REAL_CAUSE in excerpt
    assert "Traceback (most recent call last):" in excerpt


def test_extraction_drops_noise_classes():
    excerpt = fa.extract_failure_excerpts(_synthetic_log())
    assert "Collecting botocore" not in excerpt
    assert "Deleted S3 bucket" not in excerpt
    assert "Deleted log group" not in excerpt


def test_extraction_is_far_smaller_than_the_full_log():
    log = _synthetic_log()
    excerpt = fa.extract_failure_excerpts(log)
    # Handing the raw log to a model costs ~110-125K tokens on a real build;
    # the point of the excerpt is that it does not.
    assert len(excerpt) < len(log) / 10


@pytest.mark.parametrize(
    "signal",
    [
        "Traceback (most recent call last):",
        "IDPProcessingError: boom",
        "❌ Step 4: BDA mode failed",
        "Command failed with exit code -9",
        "✗ Error: something broke",
        "ROLLBACK_IN_PROGRESS",
        "CREATE_FAILED",
    ],
)
def test_every_failure_signal_is_recognised(signal):
    log = "\n".join(["filler"] * 400 + [signal] + ["filler"] * 400)
    assert signal in fa.extract_failure_excerpts(log)


def test_unrecognised_failure_falls_back_to_a_denoised_tail():
    # No known signal at all: still return something useful rather than nothing,
    # and still not spend the window on teardown inventory.
    log = "\n".join(
        [f"Deleted S3 bucket: b{i}" for i in range(300)] + ["something odd happened"]
    )
    excerpt = fa.extract_failure_excerpts(log)
    assert "something odd happened" in excerpt
    assert "Deleted S3 bucket" not in excerpt


def test_extraction_handles_empty_log():
    assert fa.extract_failure_excerpts("") == "(no build log available)"


# --------------------------------------------------------------------------- #
# brief assembly
# --------------------------------------------------------------------------- #


def test_brief_flags_an_empty_workflow_failure_list_as_meaningful():
    # An empty list is itself evidence (no document workflow errored), and the
    # brief must say so rather than leaving the reader to guess.
    brief = fa.build_evidence_brief("stk", "Step 11 failed", [], _synthetic_log())
    assert "EMPTY list" in brief
    assert "Step 11 failed" in brief
    assert _REAL_CAUSE in brief


def test_brief_tells_the_agent_to_grep_the_log_not_read_it():
    brief = fa.build_evidence_brief(
        "stk", "err", [], _synthetic_log(), log_path="/tmp/build.log"
    )
    assert "/tmp/build.log" in brief
    assert "Grep it" in brief
    # The interleaving warning is load-bearing: 8 steps share one log stream.
    assert "interleaves" in brief


def test_brief_survives_a_missing_log():
    assert fa.build_evidence_brief("stk", "err", None, "")


# --------------------------------------------------------------------------- #
# 3: the agent degrades to None instead of failing the build
# --------------------------------------------------------------------------- #


def test_disabled_agent_returns_none_without_touching_aws(monkeypatch):
    monkeypatch.setattr(fa, "AGENT_ENABLED", False)

    def _explode(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("disabled agent must not fetch logs")

    monkeypatch.setattr(fa, "fetch_full_build_log", _explode)
    assert fa.run_failure_agent("stk", "err", []) is None


def test_agent_returns_none_when_cli_is_unavailable(monkeypatch):
    monkeypatch.setattr(fa, "AGENT_ENABLED", True)
    monkeypatch.setattr(fa, "fetch_full_build_log", lambda: "log")
    monkeypatch.setattr(fa, "ensure_claude_code", lambda: None)
    assert fa.run_failure_agent("stk", "err", []) is None


def test_agent_swallows_unexpected_errors(monkeypatch):
    monkeypatch.setattr(fa, "AGENT_ENABLED", True)

    def _boom():
        raise RuntimeError("cloudwatch is down")

    monkeypatch.setattr(fa, "fetch_full_build_log", _boom)
    # Diagnostics failing must never be what fails a build.
    assert fa.run_failure_agent("stk", "err", []) is None


def test_output_parsing_handles_json_plain_text_and_junk():
    assert fa._parse_agent_output(
        '{"result": "ROOT CAUSE\\n  x", "total_cost_usd": 1.5}'
    ) == (
        "ROOT CAUSE\n  x",
        1.5,
    )
    # Not JSON: keep the text rather than losing the analysis.
    assert fa._parse_agent_output("ROOT CAUSE\n  y") == ("ROOT CAUSE\n  y", None)
    assert fa._parse_agent_output("") == (None, None)
    assert fa._parse_agent_output('{"result": ""}') == (None, None)


# --------------------------------------------------------------------------- #
# 4: the agent is never given write access
# --------------------------------------------------------------------------- #


def test_write_tools_are_denied():
    for tool in ("Edit", "Write", "NotebookEdit"):
        assert tool in fa._DISALLOWED_TOOLS
    assert not any(t.startswith(("Edit", "Write")) for t in fa._ALLOWED_TOOLS)


def test_allowlisted_aws_verbs_are_all_read_only():
    mutating = (
        "delete",
        "create",
        "update",
        "put",
        "start-execution",
        "terminate",
        "cancel",
        "modify",
        "remove",
        "attach",
        "detach",
        "tag",
    )
    for rule in fa._ALLOWED_TOOLS:
        if not rule.startswith("Bash(aws "):
            continue
        verb = rule[len("Bash(") : -1]
        assert not any(m in verb for m in mutating), (
            f"mutating verb allowlisted: {rule}"
        )
