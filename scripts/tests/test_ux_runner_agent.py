# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the UX session's tool sandbox and its ``claude`` invocation.

This module is the ratchet evidence for three registry entries in
``scripts/tests/gate_exemptions.json``:

* ``tool_profiles.EXPERT_ALLOWED_TOOLS`` (universe closure): every tool the pinned
  chrome-devtools-mcp serves is either granted or denied by name, so a version
  bump that adds a tool fails here as an unaccounted tool until somebody decides;
* ``tool_profiles.EXPERT_DISALLOWED_TOOLS`` (count pinned): the deny list is an
  audited count, so changing it is a deliberate edit to the pin;
* ``agent.CHILD_ENV_ALLOWLIST`` (count pinned): the environment ``claude`` gets
  is built from a parent holding every ``ai_mr_review.SECRET_ENV_KEYS`` name, the
  ``UX_TEST_*`` variables and the proxy variables, and an unaccounted key fails.

``ai_mr_review``'s lists are read from its source with ``ast.literal_eval`` rather
than imported, because ``scripts/ux_runner/gitlab_api.py`` is the single import
point for that module. Everything else pins the argv, the MCP server definitions,
the config file's mode and the parsing of Claude Code's JSON result, with fake
runners: nothing here starts ``claude``, node or a browser.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import os
import stat
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import agent, paths, processes, tool_profiles  # noqa: E402

AI_MR_REVIEW = SCRIPTS / "sdlc" / "ai_mr_review.py"
DEVTOOLS = tool_profiles.CHROME_DEVTOOLS_SERVER
RECORDER = tool_profiles.RECORDER_SERVER


def _ai_mr_review_literal(name: str) -> Any:
    """A module-level literal of scripts/sdlc/ai_mr_review.py, read from source."""
    tree = ast.parse(AI_MR_REVIEW.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        if name in names and node.value is not None:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not a module-level literal of {AI_MR_REVIEW}")


def _session(key_id: str, key: str, session: str) -> dict[str, str]:
    """The three environment keys of an AWS session, from positional values."""
    names = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
    return dict(zip(names, (key_id, key, session), strict=True))


def _completed(
    stdout: str = "", returncode: int = 0, timed_out: bool = False
) -> processes.Completed:
    return processes.Completed(
        returncode=returncode,
        stdout=stdout,
        stderr="",
        timed_out=timed_out,
        elapsed=1.0,
    )


def _private_cwd(tmp_path: Path, name: str = "cwd") -> Path:
    """An empty directory only its owner can enter, as the orchestrator makes one."""
    path = tmp_path / name
    path.mkdir()
    os.chmod(path, 0o700)
    return path


def _servers(search_path: str = agent.DEFAULT_SEARCH_PATH) -> dict[str, Any]:
    return agent.mcp_servers(
        toolchain=paths.Toolchain(Path("/opt/ux-runner")),
        recorder_adapter=Path("/work/ux-harness/scripts/ux_recorder_mcp.py"),
        recorder_out=Path("/run/ux/recordings"),
        python="/usr/local/bin/python3",
        home=Path("/run/ux/agent/home"),
        fs_root=Path("/run/ux/agent/mcp-files"),
        search_path=search_path,
    )


@pytest.mark.unit
def test_the_grant_is_exactly_twelve_browser_tools_and_four_recorder_tools() -> None:
    granted = {
        "mcp__chrome-devtools__navigate_page",
        "mcp__chrome-devtools__wait_for",
        "mcp__chrome-devtools__click",
        "mcp__chrome-devtools__hover",
        "mcp__chrome-devtools__fill",
        "mcp__chrome-devtools__fill_form",
        "mcp__chrome-devtools__type_text",
        "mcp__chrome-devtools__press_key",
        "mcp__chrome-devtools__drag",
        "mcp__chrome-devtools__handle_dialog",
        "mcp__chrome-devtools__take_snapshot",
        "mcp__chrome-devtools__take_screenshot",
        "mcp__ux-recorder__mark",
        "mcp__ux-recorder__note",
        "mcp__ux-recorder__pause",
        "mcp__ux-recorder__resume",
    }
    assert set(tool_profiles.EXPERT_ALLOWED_TOOLS) == granted
    assert len(tool_profiles.EXPERT_ALLOWED_TOOLS) == len(granted), "a duplicate grant"


@pytest.mark.unit
def test_nothing_granted_is_bash_or_any_other_built_in() -> None:
    for tool in tool_profiles.EXPERT_ALLOWED_TOOLS:
        assert not tool.startswith("Bash"), tool
        assert tool.startswith("mcp__"), f"{tool} is a Claude Code built-in"


@pytest.mark.unit
def test_every_tool_of_the_pinned_server_is_granted_or_denied_by_name() -> None:
    allowed = set(tool_profiles.EXPERT_ALLOWED_TOOLS)
    denied = set(tool_profiles.EXPERT_DISALLOWED_TOOLS)
    universe = tool_profiles.DEVTOOLS_TOOLS_AT_PIN
    assert universe, "an empty universe would make the closure vacuous"
    for bare in universe:
        tool = tool_profiles.mcp_tool(DEVTOOLS, bare)
        assert (tool in allowed) != (tool in denied), (
            f"unaccounted tool {tool}: the pinned chrome-devtools-mcp serves it, so "
            "it must be in exactly one of EXPERT_ALLOWED_TOOLS and "
            "EXPERT_DISALLOWED_TOOLS"
        )
    assert not allowed & denied, (
        f"granted and denied at once: {sorted(allowed & denied)}"
    )

    prefix = f"mcp__{DEVTOOLS}__"
    served = {tool_profiles.mcp_tool(DEVTOOLS, bare) for bare in universe}
    named = {tool for tool in allowed | denied if tool.startswith(prefix)}
    assert named <= served, (
        f"unaccounted tool names the pinned server does not serve: {sorted(named - served)}"
    )
    recorder_granted = {
        tool for tool in allowed if tool.startswith(f"mcp__{RECORDER}__")
    }
    assert recorder_granted == {
        tool_profiles.mcp_tool(RECORDER, bare) for bare in tool_profiles.RECORDER_TOOLS
    }
    assert allowed <= served | recorder_granted, (
        f"unaccounted grant outside both servers: {sorted(allowed - served - recorder_granted)}"
    )


@pytest.mark.unit
def test_the_pinned_universe_is_the_thirty_tools_of_chrome_devtools_mcp_1_10_1() -> (
    None
):
    universe = tool_profiles.DEVTOOLS_TOOLS_AT_PIN
    assert len(universe) == 30, (
        "DEVTOOLS_TOOLS_AT_PIN is pinned to chrome-devtools-mcp 1.10.1's tools/list "
        "under default flags; a version bump is audited when it changes this"
    )
    assert list(universe) == sorted(set(universe)), "sorted, no duplicates"
    assert tool_profiles.RECORDER_TOOLS == ("mark", "note", "pause", "resume")


@pytest.mark.unit
def test_the_deny_list_is_an_audited_count_with_no_duplicates() -> None:
    audited = 33
    denied = tool_profiles.EXPERT_DISALLOWED_TOOLS
    assert len(denied) == audited, (
        f"EXPERT_DISALLOWED_TOOLS has {len(denied)} entries; the pinned count is "
        f"{audited}. The deny list is audited when it changes: confirm it still "
        "denies every built-in ai_mr_review denies and every devtools tool that "
        "could act outside the recorded tab, then update the pinned count."
    )
    assert len(set(denied)) == len(denied), "a duplicate denial"


@pytest.mark.unit
def test_the_deny_list_holds_the_dangerous_devtools_tools_and_every_review_denial() -> (
    None
):
    denied = set(tool_profiles.EXPERT_DISALLOWED_TOOLS)
    for bare in (
        "evaluate_script",
        "lighthouse_audit",
        "upload_file",
        "new_page",
        "close_page",
    ):
        assert tool_profiles.mcp_tool(DEVTOOLS, bare) in denied, bare
    review_denials = _ai_mr_review_literal("DISALLOWED_TOOLS")
    assert review_denials, "ai_mr_review.DISALLOWED_TOOLS read back empty"
    missing = sorted(set(review_denials) - denied)
    assert not missing, (
        f"ai_mr_review denies these and the UX session does not: {missing}"
    )


@pytest.mark.unit
def test_is_granted_needs_the_grant_and_no_denial() -> None:
    assert tool_profiles.is_granted("mcp__chrome-devtools__click")
    assert tool_profiles.is_granted("mcp__ux-recorder__mark")
    assert not tool_profiles.is_granted("mcp__chrome-devtools__evaluate_script")
    assert not tool_profiles.is_granted("Bash")
    assert not tool_profiles.is_granted("mcp__chrome-devtools__click_at")
    assert tool_profiles.mcp_tool("a-b", "c_d") == "mcp__a-b__c_d"


@pytest.mark.unit
def test_the_argv_is_exactly_the_pinned_command_line() -> None:
    claude = Path("/opt/ux-runner/node_modules/.bin/claude")
    config = Path("/run/ux/agent/mcp.json")
    argv = agent.claude_argv(claude, config, max_turns=150, max_budget_usd=10.0)
    assert argv == [
        str(claude),
        "-p",
        "--bare",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--mcp-config",
        str(config),
        "--permission-mode",
        "manual",
        "--tools",
        "",
        "--allowedTools",
        *tool_profiles.EXPERT_ALLOWED_TOOLS,
        "--disallowedTools",
        *tool_profiles.EXPERT_DISALLOWED_TOOLS,
        "--max-turns",
        "150",
        "--max-budget-usd",
        "10.00",
    ]


@pytest.mark.unit
def test_the_argv_names_its_flags_in_place_and_carries_no_payload() -> None:
    config = Path("/run/ux/agent/mcp.json")
    argv = agent.claude_argv(
        Path("/opt/claude"), config, max_turns=7, max_budget_usd=2.5
    )
    for flag in ("-p", "--bare", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv, flag
    assert argv[argv.index("--mcp-config") + 1] == str(config)
    assert argv[argv.index("--permission-mode") + 1] == "manual"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--max-budget-usd") + 1] == "2.50"
    start = argv.index("--allowedTools") + 1
    granted = tuple(argv[start : start + len(tool_profiles.EXPERT_ALLOWED_TOOLS)])
    assert granted == tool_profiles.EXPERT_ALLOWED_TOOLS
    start = argv.index("--disallowedTools") + 1
    denied = tuple(argv[start : start + len(tool_profiles.EXPERT_DISALLOWED_TOOLS)])
    assert denied == tool_profiles.EXPERT_DISALLOWED_TOOLS
    assert "--model" not in argv, "the model travels in ANTHROPIC_MODEL"
    for part in argv:
        assert len(part) <= 4096 and "\n" not in part, part[:80]


@pytest.mark.unit
def test_the_expert_profile_is_the_two_expert_lists_themselves() -> None:
    """The profile carries the audited lists by identity, so the pins above on
    ``EXPERT_ALLOWED_TOOLS`` and ``EXPERT_DISALLOWED_TOOLS`` are pins on it."""
    expert = tool_profiles.EXPERT
    assert [f.name for f in dataclasses.fields(tool_profiles.ToolProfile)] == [
        "name",
        "allowed",
        "disallowed",
    ]
    assert expert.name == "expert"
    assert expert.allowed is tool_profiles.EXPERT_ALLOWED_TOOLS
    assert expert.disallowed is tool_profiles.EXPERT_DISALLOWED_TOOLS
    with pytest.raises(dataclasses.FrozenInstanceError):
        expert.allowed = ()


@pytest.mark.unit
def test_the_argv_takes_the_expert_profile_unless_told_otherwise() -> None:
    """Every existing caller passes no profile and gets the pinned command line."""
    claude = Path("/opt/ux-runner/node_modules/.bin/claude")
    config = Path("/run/ux/agent/mcp.json")
    parameter = inspect.signature(agent.claude_argv).parameters["profile"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is tool_profiles.EXPERT
    assert agent.claude_argv(
        claude, config, max_turns=150, max_budget_usd=10.0
    ) == agent.claude_argv(
        claude,
        config,
        max_turns=150,
        max_budget_usd=10.0,
        profile=tool_profiles.EXPERT,
    )


@pytest.mark.unit
def test_another_profile_changes_the_two_tool_lists_and_nothing_else() -> None:
    claude, config = Path("/opt/claude"), Path("/run/ux/agent/mcp.json")
    probe = tool_profiles.ToolProfile(
        name="probe", allowed=("mcp__probe__look",), disallowed=("Bash", "Read")
    )

    def parts(argv: list[str]) -> tuple[list[str], ...]:
        granted = argv.index("--allowedTools") + 1
        denied = argv.index("--disallowedTools")
        tail = argv.index("--max-turns")
        return (
            argv[:granted],
            argv[granted:denied],
            argv[denied + 1 : tail],
            argv[tail:],
        )

    expert = parts(agent.claude_argv(claude, config, max_turns=7, max_budget_usd=2.5))
    other = parts(
        agent.claude_argv(
            claude, config, max_turns=7, max_budget_usd=2.5, profile=probe
        )
    )
    assert (other[0], other[3]) == (expert[0], expert[3])
    assert tuple(other[1]) == probe.allowed
    assert tuple(other[2]) == probe.disallowed


@pytest.mark.unit
def test_the_prompt_reaches_the_runner_only_on_stdin(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    answer = {
        "type": "result",
        "subtype": "success",
        "result": "  the report  ",
        "total_cost_usd": 1.5,
        "num_turns": 12,
        "duration_ms": 61000,
        "is_error": False,
    }

    def runner(
        argv: list[str], *, input_text: str, env: Any, cwd: Path, timeout: float
    ) -> processes.Completed:
        calls.append({"argv": list(argv), "input_text": input_text, "cwd": cwd})
        assert timeout == 1500
        return _completed(json.dumps(answer))

    prompt = "MARKER-OF-THE-PROMPT\n" + "diff --git a/x b/x\n" * 20_000
    argv = agent.claude_argv(
        Path("/opt/claude"), Path("/run/mcp.json"), max_turns=1, max_budget_usd=1
    )
    cwd = _private_cwd(tmp_path)
    result, completed = agent.run_agent(
        prompt, argv=argv, cwd=cwd, env={}, timeout=1500, runner=runner
    )
    assert len(calls) == 1
    assert calls[0]["input_text"] == prompt
    assert calls[0]["cwd"] == cwd
    assert not [part for part in calls[0]["argv"] if "MARKER-OF-THE-PROMPT" in part]
    assert result == agent.AgentResult(
        text="the report",
        cost_usd=1.5,
        num_turns=12,
        duration_s=61.0,
        stop="end_turn",
        is_error=False,
    )
    assert completed.returncode == 0


@pytest.mark.unit
def test_run_agent_gives_no_result_for_a_timeout_or_a_failed_exit(
    tmp_path: Path,
) -> None:
    cwd = _private_cwd(tmp_path)
    timed_out = _completed(returncode=-15, timed_out=True)
    result, completed = agent.run_agent(
        "p",
        argv=["claude"],
        cwd=cwd,
        env={},
        timeout=1,
        runner=lambda *a, **k: timed_out,
    )
    assert result is None and completed is timed_out

    crashed = _completed("Error: node is too old", returncode=1)
    result, completed = agent.run_agent(
        "p",
        argv=["claude"],
        cwd=cwd,
        env={},
        timeout=1,
        runner=lambda *a, **k: crashed,
    )
    assert result is None and completed is crashed

    garbled = _completed("not json at all", returncode=0)
    with pytest.raises(agent.AgentError):
        agent.run_agent(
            "p",
            argv=["claude"],
            cwd=cwd,
            env={},
            timeout=1,
            runner=lambda *a, **k: garbled,
        )


@pytest.mark.unit
@pytest.mark.parametrize("returncode", [0, 1])
def test_an_answer_cut_at_the_capture_bound_is_an_error_naming_the_cut(
    tmp_path: Path, returncode: int
) -> None:
    """``run_in_group`` keeps the first part of a stream past its bound, which for
    ``claude`` is the head of a JSON object: not a result, whatever the exit."""
    head = json.dumps({"type": "result", "subtype": "success", "result": "x" * 99})
    cut = processes.Completed(
        returncode, head[:60], "", False, 1.0, stdout_dropped=2048
    )
    with pytest.raises(agent.AgentError) as raised:
        agent.run_agent(
            "p",
            argv=["claude"],
            cwd=_private_cwd(tmp_path),
            env={},
            timeout=1,
            runner=lambda *a, **k: cut,
        )
    assert str(raised.value) == (
        "claude's answer was cut 2048 bytes short at the capture bound, so it "
        "cannot be read"
    )


@pytest.mark.unit
def test_run_agent_defaults_to_running_in_a_process_group() -> None:
    runner = inspect.signature(agent.run_agent).parameters["runner"]
    assert runner.default is processes.run_in_group


def _parsed(**fields: Any) -> agent.AgentResult:
    return agent.parse_output(json.dumps({"type": "result", **fields}))


@pytest.mark.unit
def test_parse_output_maps_each_subtype_to_why_the_session_stopped() -> None:
    assert _parsed(subtype="success", result="ok").stop == "end_turn"
    assert _parsed(subtype="error_max_turns").stop == "max_turns"
    assert _parsed(subtype="error_max_budget_usd").stop == "budget"
    assert _parsed(subtype="error_during_execution").stop == "error"
    assert _parsed().stop == "error"


@pytest.mark.unit
def test_parse_output_keeps_is_error_and_only_numeric_measurements() -> None:
    failed = _parsed(subtype="error_during_execution", is_error=True, result=None)
    assert failed.is_error is True
    assert failed.text == ""
    bare = _parsed(subtype="success", result="r")
    assert (bare.cost_usd, bare.num_turns, bare.duration_s) == (None, None, None)
    assert bare.is_error is False
    odd = _parsed(
        subtype="success", total_cost_usd="1.5", num_turns=True, duration_ms="9"
    )
    assert (odd.cost_usd, odd.num_turns, odd.duration_s) == (None, None, None)
    whole = _parsed(subtype="success", total_cost_usd=2, num_turns=3, duration_ms=1500)
    assert (whole.cost_usd, whole.num_turns, whole.duration_s) == (2.0, 3, 1.5)


@pytest.mark.unit
def test_parse_output_reads_the_result_message_out_of_a_message_list() -> None:
    stdout = json.dumps(
        [
            {"type": "system", "subtype": "init"},
            {"type": "result", "subtype": "success", "result": "from the list"},
        ]
    )
    assert agent.parse_output(stdout).text == "from the list"
    with pytest.raises(agent.AgentError):
        agent.parse_output(json.dumps([{"type": "assistant"}]))
    with pytest.raises(agent.AgentError):
        agent.parse_output("42")


@pytest.mark.unit
def test_parse_output_shows_the_first_400_characters_of_output_that_is_not_json() -> (
    None
):
    stdout = "Error: something went wrong " + "y" * 1000
    with pytest.raises(agent.AgentError) as caught:
        agent.parse_output(stdout)
    message = str(caught.value)
    assert repr(stdout[:400]) in message
    assert "y" * 400 not in message


@pytest.mark.unit
def test_child_env_passes_the_allowlisted_names_fixed_settings_and_session_only(
    tmp_path: Path,
) -> None:
    review_credentials = _ai_mr_review_literal("SECRET_ENV_KEYS")
    assert review_credentials, "ai_mr_review.SECRET_ENV_KEYS read back empty"
    marker = "ux-parent-marker"
    source = {name: f"{marker}-{name.lower()}" for name in review_credentials}
    random_names = [f"UNRELATED_{uuid.uuid4().hex.upper()}" for _ in range(2)]
    source.update({name: marker for name in random_names})
    source.update(
        {
            "UX_TEST_PERSONAS": f"{marker}-personas",
            "UX_TEST_UI_ENV": f"{marker}-ui-env",
            "UX_TEST_AWS_ACCOUNT_ID": "123456789012",
            "HTTPS_PROXY": f"http://{marker}:3128",
            "https_proxy": f"http://{marker}:3128",
            "AWS_PROFILE": f"{marker}-profile",
            "AWS_ACCESS_KEY_ID": f"{marker}-ambient-id",
            "AWS_SECRET_ACCESS_KEY": f"{marker}-ambient-key",
            "AWS_SESSION_TOKEN": f"{marker}-ambient-session",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "TZ": "UTC",
        }
    )
    session = _session("scoped-id", "scoped-key", "scoped-session")
    env = agent.child_env(
        home=tmp_path / "home",
        tmpdir=tmp_path / "tmp",
        model="us.anthropic.claude-opus-5",
        region="us-west-2",
        aws=session,
        source=source,
    )
    expected = {
        "PATH",
        "LANG",
        "TZ",
        "HOME",
        "TMPDIR",
        "CLAUDE_CODE_USE_BEDROCK",
        "ANTHROPIC_MODEL",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_EC2_METADATA_DISABLED",
        "CI",
        "DISABLE_AUTOUPDATER",
        "DISABLE_TELEMETRY",
        "DISABLE_ERROR_REPORTING",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
    unaccounted = sorted(set(env) - expected)
    assert not unaccounted, (
        f"unaccounted keys reached claude's environment: {unaccounted}"
    )
    assert set(env) == expected
    leaked = sorted(name for name, value in env.items() if marker in value)
    assert not leaked, f"values from the job's environment leaked through: {leaked}"
    assert {name: env[name] for name in session} == session
    assert (env["HOME"], env["TMPDIR"]) == (
        str(tmp_path / "home"),
        str(tmp_path / "tmp"),
    )
    assert env["ANTHROPIC_MODEL"] == "us.anthropic.claude-opus-5"
    assert (env["AWS_REGION"], env["AWS_DEFAULT_REGION"]) == ("us-west-2", "us-west-2")
    assert (env["CLAUDE_CODE_USE_BEDROCK"], env["CI"]) == ("1", "true")
    assert env["AWS_EC2_METADATA_DISABLED"] == "true", (
        "with no key material the SDK would otherwise fall through to instance "
        "metadata while the summary still said the session was ambient"
    )


@pytest.mark.unit
def test_child_env_reads_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GITLAB_REVIEW_TOKEN", "from-the-process")
    monkeypatch.setenv("LANG", "C.UTF-8")
    env = agent.child_env(
        home=tmp_path, tmpdir=tmp_path, model="m", region="us-east-1", aws={}
    )
    assert "GITLAB_REVIEW_TOKEN" not in env
    assert env["LANG"] == "C.UTF-8"
    assert env.get("PATH") == os.environ.get("PATH")


@pytest.mark.unit
def test_the_environment_allowlist_is_pinned_to_its_audited_members() -> None:
    assert agent.CHILD_ENV_ALLOWLIST == ("PATH", "LANG", "LC_ALL", "TZ"), (
        "CHILD_ENV_ALLOWLIST is pinned. Every name in it is copied from a job "
        "environment that holds the review token, the persona passwords and an AWS "
        "credential, so it is audited when it changes: update the pin only after "
        "confirming the new name can never carry any of those."
    )


@pytest.mark.unit
def test_an_aws_mapping_with_any_other_key_is_refused_without_showing_values(
    tmp_path: Path,
) -> None:
    hidden = "value-not-shown"
    with pytest.raises(agent.AgentError) as caught:
        agent.child_env(
            home=tmp_path,
            tmpdir=tmp_path,
            model="m",
            region="us-east-1",
            aws={"AWS_ACCESS_KEY_ID": "id", "GITLAB_REVIEW_TOKEN": hidden},
            source={},
        )
    assert "GITLAB_REVIEW_TOKEN" in str(caught.value)
    assert hidden not in str(caught.value)


@pytest.mark.unit
def test_aws_env_from_copies_only_present_aws_key_material() -> None:
    source = _session("id", "key", "")
    source.update({"AWS_PROFILE": "profile", "AWS_REGION": "us-east-1"})
    copied = agent.aws_env_from(source)
    assert set(copied) == {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
    assert (copied["AWS_ACCESS_KEY_ID"], copied["AWS_SECRET_ACCESS_KEY"]) == (
        "id",
        "key",
    )
    assert agent.aws_env_from(_session("id", "key", "s")) == _session("id", "key", "s")
    assert agent.aws_env_from({}) == {}


@pytest.mark.unit
def test_both_mcp_servers_start_under_env_minus_i_with_nothing_inherited() -> None:
    servers = _servers()
    assert set(servers) == {DEVTOOLS, RECORDER}
    for name, server in servers.items():
        assert server["type"] == "stdio", name
        assert server["command"] == "/usr/bin/env", name
        assert server["args"][0] == "-i", name
        assert server["env"] == {}, name
        assert server["args"][1] == f"PATH={agent.DEFAULT_SEARCH_PATH}", name
    assert "--remote-allow-origins" not in json.dumps(servers)
    assert _servers("/opt/node/bin:/usr/bin")[DEVTOOLS]["args"][1] == (
        "PATH=/opt/node/bin:/usr/bin"
    )


@pytest.mark.unit
def test_the_devtools_server_attaches_to_the_harness_chrome_with_the_pinned_flags() -> (
    None
):
    assert _servers()[DEVTOOLS]["args"] == [
        "-i",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "HOME=/run/ux/agent/home",
        "LANG=C.UTF-8",
        "CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS=1",
        "/opt/ux-runner/node_modules/.bin/chrome-devtools-mcp",
        "--browserUrl",
        "http://127.0.0.1:9222",
        "--redactNetworkHeaders",
        "--no-usage-statistics",
        "--no-performance-crux",
        "--no-source-maps",
        "--categoryEmulation=false",
        "--categoryPerformance=false",
        "--categoryMemory=false",
        "--javascriptEvaluation=false",
        "--no-page-id-routing",
        "--filesystemRoot=/run/ux/agent/mcp-files",
    ]
    assert agent.DEVTOOLS_FLAGS[1] == f"http://{paths.CDP_HOST}:{paths.CDP_PORT}"


@pytest.mark.unit
def test_the_devtools_server_runs_no_page_script_and_acts_on_the_one_open_page() -> (
    None
):
    """Both properties are the server's flags, not the tool lists, so both are pinned.

    Measured against chrome-devtools-mcp 1.10.1's own ``tools/list`` over stdio with
    networking denied. Under its default flags the granted ``navigate_page`` takes
    an ``initScript`` that runs on the next document and follows ``javascript:``
    and ``data:`` URLs, and every granted browser tool requires a ``pageId`` that
    only the denied ``list_pages`` reports. ``--javascriptEvaluation=false`` removes
    the parameter and refuses those URLs; ``--no-page-id-routing`` drops ``pageId``
    and acts on the selected page. The tests may not start node, so the served
    schema itself cannot be read here: what is pinned is that both flags are passed,
    that no other argument names either option and so cannot turn it back on, and
    the release they were measured against.
    """
    args = _servers()[DEVTOOLS]["args"]
    naming = [
        arg
        for arg in args
        if "javascriptevaluation" in arg.lower().replace("-", "")
        or "pageidrouting" in arg.lower().replace("-", "")
    ]
    assert naming == ["--javascriptEvaluation=false", "--no-page-id-routing"]
    manifest = json.loads(
        (SCRIPTS / "ux_runner" / "package.json").read_text(encoding="utf-8")
    )
    assert manifest["dependencies"]["chrome-devtools-mcp"] == "1.10.1", (
        "DEVTOOLS_FLAGS was measured against chrome-devtools-mcp 1.10.1, and a "
        "version bump is audited when it changes this: run tools/list under the "
        "flags and confirm navigate_page takes no initScript, evaluate_script is "
        "not served and no granted tool requires a pageId, then update the pin"
    )


@pytest.mark.unit
def test_the_recorder_server_runs_isolated_python_with_its_output_directory() -> None:
    """``-B`` rather than ``PYTHONDONTWRITEBYTECODE``, which ``-I`` ignores."""
    args = _servers()[RECORDER]["args"]
    assert args == [
        "-i",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "HOME=/run/ux/agent/home",
        "LANG=C.UTF-8",
        "/usr/local/bin/python3",
        "-I",
        "-B",
        "/work/ux-harness/scripts/ux_recorder_mcp.py",
        "--out",
        "/run/ux/recordings",
    ]
    assert args[args.index("/usr/local/bin/python3") + 1 :][:2] == ["-I", "-B"]
    assert not any(arg.startswith("PYTHON") for arg in args)
    assert args[args.index("--out") + 1] == "/run/ux/recordings"


@pytest.mark.unit
def test_the_mcp_config_is_written_readable_by_its_owner_only(tmp_path: Path) -> None:
    servers = _servers()
    fresh = agent.write_mcp_config(tmp_path / "mcp.json", servers)
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600
    assert json.loads(fresh.read_text(encoding="utf-8")) == {"mcpServers": servers}

    existing = tmp_path / "existing.json"
    existing.write_text("{}", encoding="utf-8")
    existing.chmod(0o644)
    agent.write_mcp_config(existing, servers)
    assert stat.S_IMODE(existing.stat().st_mode) == 0o600
    assert json.loads(existing.read_text(encoding="utf-8")) == {"mcpServers": servers}


@pytest.mark.unit
def test_the_mcp_config_refuses_to_follow_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "mcp.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        agent.write_mcp_config(link, _servers())
    assert target.read_text(encoding="utf-8") == "{}"


@pytest.mark.unit
def test_run_agent_starts_nothing_in_a_working_directory_that_is_not_private_and_empty(
    tmp_path: Path,
) -> None:
    """claude's working directory is a root chrome-devtools-mcp may write files
    under (1.10.1 writes under its --filesystemRoot and every root the client
    reports), so moving it into the harness or the artifacts must fail."""
    ran: list[Path] = []

    def runner(argv: list[str], **kwargs: Any) -> processes.Completed:
        ran.append(kwargs["cwd"])
        return _completed(json.dumps({"type": "result", "subtype": "success"}))

    holding = _private_cwd(tmp_path, "holding")
    (holding / "report.md").write_text("x")
    shared = _private_cwd(tmp_path, "shared")
    os.chmod(shared, 0o755)
    linked = tmp_path / "linked"
    linked.symlink_to(_private_cwd(tmp_path, "target"))
    a_file = tmp_path / "a-file"
    a_file.write_text("x")
    for cwd in (holding, shared, linked, a_file, tmp_path / "absent"):
        with pytest.raises(agent.AgentError, match="working directory"):
            agent.run_agent(
                "p", argv=["claude"], cwd=cwd, env={}, timeout=1, runner=runner
            )
    assert ran == []
    private = _private_cwd(tmp_path, "private")
    result, _ = agent.run_agent(
        "p", argv=["claude"], cwd=private, env={}, timeout=1, runner=runner
    )
    assert ran == [private] and result is not None and result.stop == "end_turn"


@pytest.mark.unit
def test_the_devtools_flags_say_the_working_directory_is_a_write_root() -> None:
    source = (SCRIPTS / "ux_runner" / "agent.py").read_text(encoding="utf-8")
    comment = source.split("DEVTOOLS_FLAGS: tuple[str, ...] = (")[0].rsplit("\n\n", 1)[
        -1
    ]
    prose = " ".join(line.lstrip("#: ") for line in comment.splitlines())
    assert "--filesystemRoot" in prose and "union" in prose, prose
    assert "run_agent" in prose


@pytest.mark.unit
def test_the_built_in_denials_are_the_reviews_and_seven_more_and_claim_no_more() -> (
    None
):
    """The built-in half is belt and braces: ``--tools ''`` is what removes every
    built-in, including ones a CLI release adds that no list here names, so the
    docs must not say the list covers every built-in."""
    built_ins = {
        tool
        for tool in tool_profiles.EXPERT_DISALLOWED_TOOLS
        if not tool.startswith("mcp__")
    }
    review_denials = set(_ai_mr_review_literal("DISALLOWED_TOOLS"))
    assert built_ins == review_denials | {
        "Read",
        "Glob",
        "Grep",
        "Agent",
        "TodoWrite",
        "AskUserQuestion",
        "Skill",
    }
    argv = agent.claude_argv(
        Path("/opt/claude"), Path("/run/mcp.json"), max_turns=1, max_budget_usd=1
    )
    assert argv[argv.index("--tools") + 1] == ""
    docstring = " ".join((tool_profiles.__doc__ or "").split())
    assert "every Claude Code built-in" not in docstring
    assert "--tools ''" in docstring
    source = (SCRIPTS / "ux_runner" / "tool_profiles.py").read_text(encoding="utf-8")
    comment = source.split("EXPERT_DISALLOWED_TOOLS: tuple[str, ...] = (")[0]
    comment = comment.rsplit("\n\n", 1)[-1]
    assert "allowlist" in comment, "the prose route that discovers this list"
    assert "--tools ''" in comment and "ai_mr_review" in comment


@pytest.mark.unit
def test_parse_output_keeps_the_four_priced_token_counts_of_usage() -> None:
    """The cost cap counts the tokens at pinned rates as well as the CLI's own
    figure, so the four priced counts are kept as whole numbers and nothing else
    of ``usage`` is."""
    usage = {
        "input_tokens": 8123,
        "output_tokens": 6810,
        "cache_read_input_tokens": 1240000,
        "cache_creation_input_tokens": 50210,
        "server_tool_use": {"web_search_requests": 0},
        "service_tier": "standard",
    }
    parsed = _parsed(subtype="success", result="r", usage=usage)
    assert parsed.usage == {
        "input_tokens": 8123,
        "output_tokens": 6810,
        "cache_read_input_tokens": 1240000,
        "cache_creation_input_tokens": 50210,
    }
    assert list(agent.USAGE_KEYS) == list(parsed.usage)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("usage", "kept"),
    [
        (None, None),
        ("8123", None),
        ({}, None),
        ({"input_tokens": "8"}, None),
        ({"input_tokens": True, "output_tokens": 3}, {"output_tokens": 3}),
        ({"input_tokens": -1, "output_tokens": 2.5}, None),
        ({"cache_read_input_tokens": 0}, {"cache_read_input_tokens": 0}),
    ],
)
def test_a_usage_count_that_is_not_a_whole_number_is_unknown_not_zero(
    usage: object, kept: dict[str, int] | None
) -> None:
    assert agent.usage_from(usage) == kept
    fields: dict[str, Any] = {"subtype": "success", "result": "r"}
    if usage is not None:
        fields["usage"] = usage
    assert _parsed(**fields).usage == kept


@pytest.mark.unit
def test_an_answer_without_usage_parses_as_it_did_before() -> None:
    result = _parsed(subtype="success", result="r", total_cost_usd=1.5)
    assert result.usage is None
    assert [f.name for f in dataclasses.fields(agent.AgentResult)][-1] == "usage"


@pytest.mark.unit
def test_an_effort_level_is_the_last_flag_and_adds_nothing_when_absent() -> None:
    claude, config = Path("/opt/claude"), Path("/run/mcp.json")
    plain = agent.claude_argv(claude, config, max_turns=50, max_budget_usd=4.0)
    pinned = agent.claude_argv(
        claude, config, max_turns=50, max_budget_usd=4.0, effort="high"
    )
    assert pinned == [*plain, "--effort", "high"]
    parameter = inspect.signature(agent.claude_argv).parameters["effort"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None
    assert agent.EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")


@pytest.mark.unit
@pytest.mark.parametrize("effort", ["", "High", "extreme", "max "])
def test_an_effort_the_pinned_cli_does_not_take_is_refused(effort: str) -> None:
    with pytest.raises(agent.AgentError, match="--effort takes one of"):
        agent.claude_argv(
            Path("/opt/claude"),
            Path("/run/mcp.json"),
            max_turns=1,
            max_budget_usd=1,
            effort=effort,
        )
