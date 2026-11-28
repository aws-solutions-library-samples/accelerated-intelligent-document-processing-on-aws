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
  5. It runs only on the read-only role, in an allowlisted environment, with the
     session-scope flags, and its report is redacted before it is returned; the
     single-shot summary is redacted the same way.

What the CLI does with the argv is measured separately, against the real CLI,
in `test_failure_agent_session_scope.py`.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import failure_agent as fa  # noqa: E402

# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

# AWS's documented placeholder shapes: every value carries EXAMPLE.
_ASSUMED = {
    "AWS_ACCESS_KEY_ID": "ASIASCOPEDKEYEXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "AWS_SESSION_TOKEN": "assumed-session-token-EXAMPLE-value",
}

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


def test_agent_returns_none_when_cli_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(fa, "AGENT_ENABLED", True)
    monkeypatch.setattr(fa, "WORKDIR", str(tmp_path))
    monkeypatch.setattr(fa, "_readonly_credentials", lambda: dict(_ASSUMED))
    monkeypatch.setattr(fa, "fetch_full_build_log", lambda: "log")
    monkeypatch.setattr(fa, "ensure_claude_code", lambda: None)
    assert fa.run_failure_agent("stk", "err", []) is None


def test_agent_swallows_unexpected_errors(monkeypatch):
    monkeypatch.setattr(fa, "AGENT_ENABLED", True)
    monkeypatch.setattr(fa, "_readonly_credentials", lambda: dict(_ASSUMED))

    def _boom():
        raise RuntimeError("cloudwatch is down")

    monkeypatch.setattr(fa, "fetch_full_build_log", _boom)
    # Diagnostics failing must never be what fails a build.
    assert fa.run_failure_agent("stk", "err", []) is None


def test_preamble_is_stripped():
    # Observed in the first live run: the model narrated before the header.
    leaked = (
        "I have enough to establish the root cause decisively from the source.\n"
        "Let me write up the finding.\n\n"
        "ROOT CAUSE\n  Step 11 timed out."
    )
    cleaned = fa._strip_preamble(leaked)
    assert cleaned.startswith("ROOT CAUSE")
    assert "Let me write up" not in cleaned


def test_preamble_strip_keeps_text_that_ignored_the_format():
    # Better a badly formatted report than no report.
    assert fa._strip_preamble("no header here") == "no header here"
    # Already compliant: unchanged apart from trimming.
    assert fa._strip_preamble("ROOT CAUSE\n  x\n") == "ROOT CAUSE\n  x"


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


# --------------------------------------------------------------------------- #
# 5: credentials, environment, session scope and redaction
# --------------------------------------------------------------------------- #


class _FailingSts:
    def assume_role(self, **_kwargs):
        raise RuntimeError("AccessDenied: not authorized to perform sts:AssumeRole")


@pytest.mark.parametrize("role_arn", ["", "arn:aws:iam::123456789012:role/ro"])
def test_without_the_read_only_role_the_agent_does_not_run(
    monkeypatch, tmp_path, role_arn
):
    """Unset, or set and the assume fails: skipped, never the job's credentials.

    The stand-ins record rather than raise, because run_failure_agent swallows
    every exception and a raising stand-in would pass either way.
    """
    import boto3

    reached = []
    monkeypatch.setattr(fa, "AGENT_ENABLED", True)
    monkeypatch.setattr(fa, "AGENT_ROLE_ARN", role_arn)
    monkeypatch.setattr(fa, "WORKDIR", str(tmp_path / "work"))
    monkeypatch.setattr(fa, "AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(boto3, "client", lambda service, **_: _FailingSts())
    monkeypatch.setattr(fa, "fetch_full_build_log", lambda: reached.append("log") or "")
    monkeypatch.setattr(
        fa, "ensure_claude_code", lambda: reached.append("cli") or "/bin/claude"
    )
    monkeypatch.setattr(fa.subprocess, "run", lambda *a, **k: reached.append("run"))

    assert fa._readonly_credentials() is None
    assert fa.run_failure_agent("stk", "err", []) is None
    assert reached == []


def test_the_child_environment_is_an_allowlist(tmp_path):
    job = {
        "PATH": "/usr/bin",
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "job-secret-EXAMPLE",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/EXAMPLE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.2/EXAMPLE",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN": "container-token-EXAMPLE",
        "AWS_PROFILE": "job-profile",
        "GITLAB_TOKEN": "glpat-EXAMPLEEXAMPLE",
        "IDP_FAILURE_SNS_TOPIC": "unrelated",
        "CODEBUILD_BUILD_ID": "unrelated",
        "HOME": "/root",
    }
    env = fa.child_env(_ASSUMED, str(tmp_path), job)

    assert not [key for key in env if key.startswith("AWS_CONTAINER_")]
    for key in (
        "AWS_PROFILE",
        "GITLAB_TOKEN",
        "IDP_FAILURE_SNS_TOPIC",
        "CODEBUILD_BUILD_ID",
    ):
        assert key not in env, key
    for key, value in _ASSUMED.items():
        assert env[key] == value, f"{key} is not the assumed role's"
    assert env["HOME"] == str(tmp_path)
    assert env["PATH"] == "/usr/bin"
    assert env["AWS_REGION"] == env["AWS_DEFAULT_REGION"] == "us-east-1"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    # Nothing beyond the passed names and the ones child_env sets itself.
    set_here = {
        "HOME",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_EC2_METADATA_DISABLED",
        "CLAUDE_CODE_USE_BEDROCK",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CI",
        "DISABLE_TELEMETRY",
        "DISABLE_AUTOUPDATER",
    }
    assert set(env) <= set(fa._PASSED_ENV_KEYS) | set_here | set(_ASSUMED)


def test_the_argv_carries_the_session_scope_flags():
    argv = fa.claude_command("claude", "prompt", "model-id", "/logs")

    for flag in (
        *fa.SESSION_SCOPE_FLAGS,
        "--strict-mcp-config",
        "--no-session-persistence",
    ):
        assert flag in argv, flag
    assert set(fa.SESSION_SCOPE_FLAGS) == {"--restricted", "--safe-mode"}

    def value(flag):
        return argv[argv.index(flag) + 1]

    assert value("--permission-mode") == "manual"
    assert value("--permission-prompts") == "none"
    assert value("--add-dir") == "/logs"
    assert value("--tools") == "Read,Grep,Glob,Bash"
    assert value("--model") == "model-id"
    denied = argv[argv.index("--disallowedTools") + 1 :]
    assert denied == fa._DISALLOWED_TOOLS


def test_the_option_forms_that_write_or_redirect_are_denied():
    """The deny rules exist and each one covers the spellings it is for.

    Matched here with the CLI's wildcard reading of a rule (``*`` is any run of
    characters); the CLI itself is measured in
    test_failure_agent_session_scope.py, where removing these rules lets every
    one of the commands below run.
    """
    import fnmatch

    rules = [
        rule[len("Bash(") : -1]
        for rule in fa._DISALLOWED_TOOLS
        if rule.startswith("Bash(")
    ]
    refused = [
        "git log -1 --output=/tmp/w",
        "git log -1 --output /tmp/w",
        "git show --output=/tmp/w HEAD",
        "git diff --output=/tmp/w",
        "aws s3 ls --endpoint-url http://127.0.0.1:9",
        "aws logs get-log-events --log-group-name g --endpoint-url=http://x",
    ]
    for command in refused:
        assert any(fnmatch.fnmatchcase(command, rule) for rule in rules), command
    for command in ("git log -1 --format=%H", "aws sts get-caller-identity"):
        assert not any(fnmatch.fnmatchcase(command, rule) for rule in rules)


def test_the_agent_runs_with_the_built_environment_and_its_report_is_redacted(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(fa, "AGENT_ENABLED", True)
    monkeypatch.setattr(fa, "WORKDIR", str(tmp_path / "work"))
    monkeypatch.setattr(fa, "AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(fa, "_readonly_credentials", lambda: dict(_ASSUMED))
    monkeypatch.setattr(fa, "fetch_full_build_log", lambda: "log line")
    monkeypatch.setattr(fa, "ensure_claude_code", lambda: "/bin/claude")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/EXAMPLE")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    seen = {}

    class _Done:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "result": (
                    "Thinking aloud first.\nROOT CAUSE\n  Role "
                    "arn:aws:iam::123456789012:role/x failed for "
                    "someone@example.com with key AKIAIOSFODNN7EXAMPLE and "
                    f"secret {_ASSUMED['AWS_SECRET_ACCESS_KEY']}."
                )
            }
        )

    def run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        return _Done()

    monkeypatch.setattr(fa.subprocess, "run", run)
    report = fa.run_failure_agent("stk", "err", [])

    assert report is not None and report.startswith("ROOT CAUSE")
    for leaked in (
        "123456789012",
        "someone@example.com",
        "AKIAIOSFODNN7EXAMPLE",
        _ASSUMED["AWS_SECRET_ACCESS_KEY"],
    ):
        assert leaked not in report, leaked
    assert "arn:aws:iam::[account]:role/x" in report
    assert "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" not in seen["env"]
    assert seen["env"]["AWS_SESSION_TOKEN"] == _ASSUMED["AWS_SESSION_TOKEN"]
    assert seen["env"]["HOME"] == str(tmp_path / "home")
    assert "--restricted" in seen["argv"]
    assert seen["argv"][seen["argv"].index("--add-dir") + 1] == str(tmp_path / "work")


def test_redaction_covers_the_jobs_own_values():
    environ = {
        "AWS_SESSION_TOKEN": "job-session-token-EXAMPLE",
        "CI_SERVER_HOST": "forge.example.internal",
        "SLACK_WEBHOOK_URL": "https://hooks.example.com/services/EXAMPLE",
    }
    text = (
        "token job-session-token-EXAMPLE on forge.example.internal, posted to "
        "https://hooks.example.com/services/EXAMPLE"
    )
    redacted = fa.redact_report(text, environ)
    for value in environ.values():
        assert value not in redacted


def test_the_single_shot_summary_is_redacted_before_it_is_published(cbd, monkeypatch):
    """Every return of generate_deployment_summary that a model wrote is redacted.

    main() hands that string to publish_summary_to_s3, send_failure_notification
    and, through the S3 copy, the GitLab artifact and the chat post, so redacting
    it here is redacting all four.
    """
    import io

    written = (
        "🚀 DEPLOYMENT RESULTS\n• arn:aws:lambda:us-east-1:123456789012:function:f "
        "failed; contact someone@example.com; key AKIAIOSFODNN7EXAMPLE"
    )

    class _Runtime:
        def invoke_model(self, modelId, body):
            payload = {"content": [{"type": "text", "text": written}]}
            return {"body": io.BytesIO(json.dumps(payload).encode())}

    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(cbd.boto3, "client", lambda *a, **k: _Runtime())
    monkeypatch.setattr(cbd, "run_failure_agent", lambda *a: None)
    monkeypatch.setattr(cbd, "fetch_full_build_log", lambda: "log")

    result = {
        "success": False,
        "failure_type": "test",
        "error": "Step 7 failed",
        "workflow_failures": [],
    }
    summary = cbd.generate_deployment_summary(result, "stk", "https://t")

    for leaked in ("123456789012", "someone@example.com", "AKIAIOSFODNN7EXAMPLE"):
        assert leaked not in summary, leaked
    assert "function:f" in summary
