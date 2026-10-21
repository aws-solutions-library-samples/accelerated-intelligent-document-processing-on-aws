# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the story relay (``scripts/ux_runner/story_relay.py``), relay mode of
``scripts/ux_story_mcp.py``, the STORY tool profile and ``agent.story_server``.

The browser side is a fake chrome-devtools-mcp (``fixtures/ux_story_relay/
fake_devtools.py``) that answers with what the pinned 1.10.1 answered when it was
recorded against local pages in the runner image (``fixtures/ux_story_relay/
record.py``): its ``tools/list`` under the harness's flags and under none, and every
call the suite replays. The relay starts it through its own command line, so what
reaches the fake is what would reach the real server.

This module is the ratchet evidence for two registry entries in
``scripts/tests/gate_exemptions.json``:

* ``tool_profiles.STORY_ALLOWED_TOOLS`` (universe closure): every tool of the pinned
  chrome-devtools-mcp is either granted under ``mcp__ux-story__`` or denied there by
  name, and denied by name under ``mcp__chrome-devtools__``, so a version bump that
  adds a tool fails here as an unaccounted tool until somebody decides;
* ``tool_profiles.STORY_DISALLOWED_TOOLS`` (count pinned): the deny list is an
  audited count, so changing it is a deliberate edit to the pin.

It also holds the third, ``MUTATIONS_WITHOUT_A_GUARDED_CONTROL`` (universe closure):
every mutation the web UI declares is either in ``MUTATION_CONTROLS``, with the
shipped controls that send it and the commit guard's refusal of each, or in that
list, so a mutation the UI gains fails here as an unaccounted one.
"""

from __future__ import annotations

import base64
import functools
import io
import json
import os
import re
import select
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ux_story_mcp as story  # noqa: E402
from ux_runner import (  # noqa: E402
    agent,
    chrome,
    docs_index,
    paths,
    processes,
    redact,
    story_relay,
    story_verdict,
    tool_profiles,
)

FIXTURES = SCRIPTS / "tests" / "fixtures" / "ux_story_relay"
UI_SOURCE = SCRIPTS.parent / "src" / "ui" / "src"
FAKE = FIXTURES / "fake_devtools.py"
SESSION: dict[str, Any] = json.loads(
    (FIXTURES / "session.json").read_text(encoding="utf-8")
)
TOOLS_LIST: dict[str, Any] = json.loads(
    (FIXTURES / "tools_list.json").read_text(encoding="utf-8")
)
DEFAULT_TOOLS_LIST: dict[str, Any] = json.loads(
    (FIXTURES / "tools_list_default_flags.json").read_text(encoding="utf-8")
)
SERVER = tool_profiles.STORY_SERVER
DEVTOOLS = tool_profiles.CHROME_DEVTOOLS_SERVER
RECORDER = tool_profiles.RECORDER_SERVER
DOCS = "https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws"
UNINDEXED_HELP_LINK = f"{DOCS}/web-ui/#document-list"
PLATFORM_VARIABLES = frozenset(
    {"PWD", "OLDPWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING"}
)
JPEG_MAGIC = b"\xff\xd8\xff"

START: dict[str, Any] = {
    "story": "route-folder-to-profile",
    "story_hash": "0f3a9c2e71b4",
    "series": "5d0c1e9a2b7f",
    "persona": "platform-admin",
    "group": "Admin",
    "expectation": "allowed",
    "kind": "commit_point",
    "attempt": 1,
    "docs_ref": "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d",
    "corpus_hash": "9e8d7c6b5a41",
    "harness": "f23669def653f7c3a9606841cfc178c5456c1e8f",
    "model": "us.anthropic.claude-opus-5-5",
    "effort": "high",
    "mode": "unattended",
}


def named(label: str, role: str, name: str) -> str:
    """The uid of the element with ``role`` and ``name`` in a recorded snapshot."""
    return uid_of(snapshot_of(label), role, name)


def answer_of(label: str) -> dict[str, Any]:
    return SESSION["calls"][label]["answer"]


def text_of(label: str) -> str:
    content = answer_of(label)["result"]["content"]
    return "\n".join(item["text"] for item in content if item["type"] == "text")


def section_of(label: str) -> list[str]:
    parts = story_relay.split_section(text_of(label))
    assert parts is not None, f"{label} holds no snapshot"
    return parts[1]


def snapshot_of(label: str) -> story_relay.Snapshot:
    return story_relay.cut_snapshot(section_of(label))


def uid_of(snapshot: story_relay.Snapshot, role: str, name: str) -> str:
    for node in snapshot.nodes:
        if node.role == role and node.name == name:
            return node.uid
    raise AssertionError(f"no {role} {name!r} in the snapshot")


def index_of(snapshot: story_relay.Snapshot, uid: str) -> int:
    found = snapshot.find(uid)
    assert found is not None, uid
    return found


def names(listed: dict[str, Any]) -> list[str]:
    return [tool["name"] for tool in listed["tools"]]


def schema_of(name: str) -> dict[str, Any]:
    return story_relay.relayed_tools(TOOLS_LIST)[name]["inputSchema"]


def fake_devtools(directory: Path, scenario: dict[str, Any]) -> tuple[Path, Path]:
    """A wrapper script the relay can start as chrome-devtools-mcp, and its record."""
    record = directory / "fake-record.jsonl"
    spec = directory / "scenario.json"
    spec.write_text(json.dumps({"record": str(record), **scenario}), encoding="utf-8")
    script = directory / "devtools"
    command = " ".join(
        shlex.quote(part)
        for part in (sys.executable, "-I", "-B", str(FAKE), "--scenario", str(spec))
    )
    script.write_text(f'#!/bin/sh\nexec {command} "$@"\n', encoding="utf-8")
    script.chmod(0o755)
    return script, record


def records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def journal_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def wait_until(check: Callable[[], bool], seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.02)


WIZARD_STEP = (
    'uid=1_0 RootWebArea "Test sets"',
    '  uid=6_0 dialog "Create or update test set" modal',
    '    uid=6_1 heading "Review and create" level="2"',
    '    uid=6_2 button "Previous"',
    '    uid=6_3 button "{label}"',
)


@dataclass
class Rig:
    relay: story_relay.Relay
    ledger: story_relay.Ledger
    journal: Any
    path: Path
    out: Path
    record: Path

    def call(self, name: str, **arguments: Any) -> story_relay.Reply:
        return self.relay.call(name, arguments)

    def lines(self) -> list[dict[str, Any]]:
        return [line for line in journal_lines(self.path) if line["type"] == "call"]

    def last(self) -> dict[str, Any]:
        return self.lines()[-1]

    def boot(self) -> dict[str, Any]:
        return records(self.record)[0]

    def received(self) -> list[dict[str, Any]]:
        return [entry["in"] for entry in records(self.record) if "in" in entry]

    def tool_calls(self) -> list[dict[str, Any]]:
        return [
            message["params"]
            for message in self.received()
            if message.get("method") == "tools/call"
        ]


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    made: list[Rig] = []

    def make(
        scenario: dict[str, Any] | None = None,
        *,
        budget: int = 30,
        call_seconds: float = story_relay.CALL_SECONDS,
    ) -> Rig:
        base = tmp_path / f"flow-{len(made)}"
        base.mkdir()
        script, record = fake_devtools(base, scenario or {})
        path = base / story.JOURNAL_FILE
        journal = story.JournalWriter(path, START, relay=True, budget=budget)
        ledger = story_relay.Ledger(journal, budget=budget)
        out = base / "out"
        out.mkdir(mode=0o700)
        home = base / "home"
        home.mkdir(mode=0o700)
        relay = story_relay.start(
            script,
            ledger=ledger,
            budget=budget,
            out=out,
            environ={"PATH": agent.DEFAULT_SEARCH_PATH, "HOME": str(home)},
        )
        relay.call_seconds = call_seconds
        rig = Rig(relay, ledger, journal, path, out, record)
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.relay.close()


def look_at(rig: Rig, label: str) -> story_relay.Reply:
    """Have the fake answer the next take_snapshot with a recorded snapshot."""
    rig.relay.snapshot = None
    reply = rig.relay.call("take_snapshot", {})
    assert rig.relay.snapshot is not None, (label, reply)
    return reply


def scenario_snapshot(label: str, **more: Any) -> dict[str, Any]:
    calls = {"take_snapshot": [{"label": label}], **more.pop("calls", {})}
    return {"calls": calls, **more}


def page(*lines: str) -> dict[str, Any]:
    """A scripted answer holding a snapshot of ``lines``, as the server writes one."""
    text = "\n".join([story_relay.SNAPSHOT_HEADING, *lines])
    return {"result": {"content": [{"type": "text", "text": text}]}}


def judged(*lines: str) -> story_relay.Snapshot:
    return story_relay.cut_snapshot(list(lines))


def reply_text(reply: story_relay.Reply) -> str:
    return "\n".join(
        str(item["text"]) for item in reply.content if item.get("type") == "text"
    )


def images_in(reply: story_relay.Reply) -> list[dict[str, Any]]:
    return [dict(item) for item in reply.content if item.get("type") == "image"]


@pytest.mark.unit
def test_the_story_profile_names_the_story_server_and_its_own_tools() -> None:
    assert tool_profiles.STORY_SERVER == story.SERVER_NAME
    assert tool_profiles.STORY_OWN_TOOLS == story.OWN_TOOL_NAMES


@pytest.mark.unit
def test_the_story_grant_is_twelve_browser_tools_and_seven_own_tools_under_ux_story() -> (
    None
):
    browser = tool_profiles.STORY_BROWSER_TOOLS
    own = tool_profiles.STORY_OWN_TOOLS
    assert list(tool_profiles.STORY_ALLOWED_TOOLS) == [
        tool_profiles.mcp_tool(SERVER, bare) for bare in (*browser, *own)
    ]
    assert len(tool_profiles.STORY_ALLOWED_TOOLS) == 19
    expert_browser = tuple(
        tool.removeprefix(f"mcp__{DEVTOOLS}__")
        for tool in tool_profiles.EXPERT_ALLOWED_TOOLS
        if tool.startswith(f"mcp__{DEVTOOLS}__")
    )
    assert browser == expert_browser, "the twelve browser tools EXPERT grants"
    assert tool_profiles.STORY_LOOK_TOOLS == (
        "take_snapshot",
        "take_screenshot",
        "wait_for",
    )
    assert set(tool_profiles.STORY_LOOK_TOOLS) <= set(browser)


@pytest.mark.unit
def test_every_tool_of_the_pinned_server_is_granted_or_denied_under_both_names() -> (
    None
):
    allowed = set(tool_profiles.STORY_ALLOWED_TOOLS)
    denied = set(tool_profiles.STORY_DISALLOWED_TOOLS)
    universe = tool_profiles.DEVTOOLS_TOOLS_AT_PIN
    assert universe, "an empty universe would make the closure vacuous"
    assert not allowed & denied, f"granted and denied at once: {allowed & denied}"
    for bare in universe:
        relayed = tool_profiles.mcp_tool(SERVER, bare)
        assert (relayed in allowed) != (relayed in denied), (
            f"unaccounted tool {relayed}: the pinned chrome-devtools-mcp serves "
            f"{bare}, so it must be in exactly one of STORY_ALLOWED_TOOLS and "
            "STORY_DISALLOWED_TOOLS"
        )
        direct = tool_profiles.mcp_tool(DEVTOOLS, bare)
        assert direct in denied, (
            f"unaccounted tool {direct}: chrome-devtools-mcp is never registered "
            "in a story flow, so every one of its names is denied"
        )
    for bare in tool_profiles.STORY_OWN_TOOLS:
        assert tool_profiles.mcp_tool(SERVER, bare) in allowed, bare
    for bare in tool_profiles.RECORDER_TOOLS:
        assert tool_profiles.mcp_tool(RECORDER, bare) in denied, bare
    built_ins = {tool for tool in denied if not tool.startswith("mcp__")}
    accounted = (
        {tool_profiles.mcp_tool(SERVER, bare) for bare in universe}
        | {
            tool_profiles.mcp_tool(SERVER, bare)
            for bare in tool_profiles.STORY_OWN_TOOLS
        }
        | {tool_profiles.mcp_tool(DEVTOOLS, bare) for bare in universe}
        | {
            tool_profiles.mcp_tool(RECORDER, bare)
            for bare in tool_profiles.RECORDER_TOOLS
        }
        | built_ins
    )
    unaccounted = sorted((allowed | denied) - accounted)
    assert not unaccounted, f"unaccounted names in the STORY lists: {unaccounted}"


@pytest.mark.unit
def test_the_story_deny_list_is_an_audited_count_with_no_duplicates() -> None:
    audited = 67
    denied = tool_profiles.STORY_DISALLOWED_TOOLS
    assert len(denied) == audited, (
        f"STORY_DISALLOWED_TOOLS has {len(denied)} entries; the pinned count is "
        f"{audited}: the 15 built-ins EXPERT denies, all 30 chrome-devtools-mcp "
        "names, the 18 withheld tools under mcp__ux-story__ and the 4 recorder "
        "tools. The deny list is audited when it changes: confirm it still denies "
        "every built-in and every tool that could act outside the confined tab, "
        "then update the pinned count."
    )
    assert len(set(denied)) == len(denied), "a duplicate denial"
    withheld = [tool for tool in denied if tool.startswith(f"mcp__{SERVER}__")]
    assert len(withheld) == 18
    assert sorted(tool.removeprefix(f"mcp__{SERVER}__") for tool in withheld) == sorted(
        set(tool_profiles.DEVTOOLS_TOOLS_AT_PIN)
        - set(tool_profiles.STORY_BROWSER_TOOLS)
    )


@pytest.mark.unit
def test_the_story_deny_list_holds_every_expert_built_in_and_the_grant_holds_none() -> (
    None
):
    story_built_ins = [
        tool
        for tool in tool_profiles.STORY_DISALLOWED_TOOLS
        if not tool.startswith("mcp__")
    ]
    expert_built_ins = [
        tool
        for tool in tool_profiles.EXPERT_DISALLOWED_TOOLS
        if not tool.startswith("mcp__")
    ]
    assert story_built_ins == expert_built_ins
    for tool in tool_profiles.STORY_ALLOWED_TOOLS:
        assert tool.startswith(f"mcp__{SERVER}__"), f"{tool} is not a ux-story tool"
    for dangerous in ("evaluate_script", "upload_file", "new_page", "lighthouse_audit"):
        assert tool_profiles.mcp_tool(SERVER, dangerous) in (
            tool_profiles.STORY_DISALLOWED_TOOLS
        )


@pytest.mark.unit
def test_the_story_profile_is_its_two_lists_and_they_reach_the_argv() -> None:
    profile = tool_profiles.STORY
    assert profile.name == "story"
    assert profile.allowed is tool_profiles.STORY_ALLOWED_TOOLS
    assert profile.disallowed is tool_profiles.STORY_DISALLOWED_TOOLS
    argv = agent.claude_argv(
        Path("/opt/claude"),
        Path("/run/mcp.json"),
        max_turns=110,
        max_budget_usd=4,
        profile=profile,
    )
    granted = argv[argv.index("--allowedTools") + 1 : argv.index("--disallowedTools")]
    denied = argv[argv.index("--disallowedTools") + 1 : argv.index("--max-turns")]
    assert granted == list(profile.allowed)
    assert denied == list(profile.disallowed)
    assert argv[argv.index("--tools") + 1] == ""
    expert = agent.claude_argv(
        Path("/opt/claude"), Path("/run/mcp.json"), max_turns=110, max_budget_usd=4
    )
    assert expert == [
        *argv[: argv.index("--allowedTools") + 1],
        *tool_profiles.EXPERT_ALLOWED_TOOLS,
        "--disallowedTools",
        *tool_profiles.EXPERT_DISALLOWED_TOOLS,
        *argv[argv.index("--max-turns") :],
    ]


@pytest.mark.unit
def test_the_universe_at_the_pin_is_the_recorded_server_s_own_tools_list() -> None:
    assert names(DEFAULT_TOOLS_LIST) == list(tool_profiles.DEVTOOLS_TOOLS_AT_PIN), (
        "DEVTOOLS_TOOLS_AT_PIN is pinned to what chrome-devtools-mcp 1.10.1 lists "
        "under default flags, recorded in tools_list_default_flags.json"
    )
    served = names(TOOLS_LIST)
    assert len(served) == 23, "23 of the 30 tools under agent.DEVTOOLS_FLAGS"
    assert set(served) <= set(tool_profiles.DEVTOOLS_TOOLS_AT_PIN)
    assert set(tool_profiles.STORY_BROWSER_TOOLS) <= set(served)


def _story_server(**changes: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "toolchain": paths.Toolchain(Path("/opt/ux-runner")),
        "story_adapter": Path("/harness/scripts/ux_story_mcp.py"),
        "index": Path("/run/ux/story/index.json"),
        "out": Path("/run/ux/story/out"),
        "start": Path("/run/ux/story/start.json"),
        "budget": 40,
        "python": "/usr/local/bin/python3",
        "home": Path("/run/ux/agent/home"),
        "recorder_out": Path("/run/ux/recordings"),
        **changes,
    }
    return agent.story_server(**options)


@pytest.mark.unit
def test_the_story_server_runs_isolated_python_under_env_minus_i_in_relay_mode() -> (
    None
):
    servers = _story_server()
    assert list(servers) == [SERVER]
    entry = servers[SERVER]
    assert entry["type"] == "stdio"
    assert entry["command"] == "/usr/bin/env"
    assert entry["env"] == {}
    assert entry["args"] == [
        "-i",
        f"PATH={agent.DEFAULT_SEARCH_PATH}",
        "HOME=/run/ux/agent/home",
        "LANG=C.UTF-8",
        "/usr/local/bin/python3",
        "-I",
        "-B",
        "/harness/scripts/ux_story_mcp.py",
        "--index",
        "/run/ux/story/index.json",
        "--out",
        "/run/ux/story/out",
        "--start",
        "/run/ux/story/start.json",
        "--budget",
        "40",
        "--devtools",
        "/opt/ux-runner/node_modules/.bin/chrome-devtools-mcp",
        "--recorder-out",
        "/run/ux/recordings",
    ]


@pytest.mark.unit
def test_the_story_server_starts_the_same_way_the_expert_session_s_servers_do() -> None:
    story_entry = _story_server(search_path="/opt/node/bin:/usr/bin:/bin")[SERVER]
    expert = agent.mcp_servers(
        toolchain=paths.Toolchain(Path("/opt/ux-runner")),
        recorder_adapter=Path("/harness/scripts/ux_recorder_mcp.py"),
        recorder_out=Path("/run/ux/recordings"),
        python="/usr/local/bin/python3",
        home=Path("/run/ux/agent/home"),
        fs_root=Path("/run/ux/agent/mcp-files"),
        search_path="/opt/node/bin:/usr/bin:/bin",
    )[RECORDER]
    assert story_entry["args"][:7] == expert["args"][:7]
    assert story_entry["command"] == expert["command"]


@pytest.mark.unit
def test_the_story_server_names_a_recorder_only_when_it_is_given_one() -> None:
    args = _story_server(recorder_out=None)[SERVER]["args"]
    assert "--recorder-out" not in args
    assert args[-2:] == [
        "--devtools",
        "/opt/ux-runner/node_modules/.bin/chrome-devtools-mcp",
    ]


@pytest.mark.unit
@pytest.mark.parametrize("budget", [0, -1, True, 1.5, "40"])
def test_the_story_server_refuses_a_budget_that_is_not_a_positive_whole_number(
    budget: Any,
) -> None:
    with pytest.raises(agent.AgentError, match="action budget"):
        _story_server(budget=budget)


@pytest.mark.unit
def test_the_story_server_s_arguments_are_ones_the_server_reads() -> None:
    args = _story_server()[SERVER]["args"]
    parsed = story._parser().parse_args(args[args.index("-B") + 2 :])
    assert parsed.index == Path("/run/ux/story/index.json")
    assert parsed.out == Path("/run/ux/story/out")
    assert parsed.start == Path("/run/ux/story/start.json")
    assert parsed.budget == "40"
    assert parsed.devtools == "/opt/ux-runner/node_modules/.bin/chrome-devtools-mcp"
    assert parsed.recorder_out == Path("/run/ux/recordings")


@pytest.mark.unit
def test_the_relay_serves_the_twelve_tools_the_recorded_server_lists_in_order() -> None:
    assert list(story_relay.relayed_tools(TOOLS_LIST)) == list(
        tool_profiles.STORY_BROWSER_TOOLS
    )


@pytest.mark.unit
def test_a_tool_the_child_does_not_list_or_the_profile_withholds_is_not_served() -> (
    None
):
    listed = {
        "tools": [
            tool
            for tool in TOOLS_LIST["tools"]
            if tool["name"] in ("click", "evaluate_script", "list_pages")
        ]
    }
    assert list(story_relay.relayed_tools(listed)) == ["click"]
    assert story_relay.relayed_tools({"tools": "nope"}) == {}
    assert story_relay.relayed_tools(None) == {}


@pytest.mark.unit
@pytest.mark.parametrize("name", tool_profiles.STORY_BROWSER_TOOLS)
def test_no_relayed_schema_takes_a_file_a_full_page_capture_or_a_format(
    name: str,
) -> None:
    schema = schema_of(name)
    properties = set(schema["properties"])
    assert not properties & set(story_relay.STRIPPED_ARGUMENTS)
    assert not [key for key in properties if re.search("file|path|dir", key, re.I)]
    assert schema["additionalProperties"] is False
    assert set(schema.get("required", ())) <= properties
    recorded = next(tool for tool in TOOLS_LIST["tools"] if tool["name"] == name)
    relayed = story_relay.relayed_tools(TOOLS_LIST)[name]
    assert relayed["description"] == recorded["description"]
    assert relayed["annotations"] == recorded["annotations"]
    assert properties == set(recorded["inputSchema"]["properties"]) - set(
        story_relay.STRIPPED_ARGUMENTS
    )


@pytest.mark.unit
def test_the_only_file_writing_arguments_the_relayed_tools_take_at_the_pin_are_stripped() -> (
    None
):
    """Checked against the recorded 1.10.1 tools/list: of the twelve tools, only the
    screenshot and the snapshot take a path, and both are filePath."""
    found = {
        (tool["name"], key)
        for tool in TOOLS_LIST["tools"]
        if tool["name"] in tool_profiles.STORY_BROWSER_TOOLS
        for key in tool["inputSchema"]["properties"]
        if re.search("file|path|dir", key, re.I)
    }
    assert found == {("take_screenshot", "filePath"), ("take_snapshot", "filePath")}
    assert {key for _, key in found} <= set(story_relay.STRIPPED_ARGUMENTS)
    screenshot = next(t for t in TOOLS_LIST["tools"] if t["name"] == "take_screenshot")
    assert {"fullPage", "format", "quality"} <= set(
        screenshot["inputSchema"]["properties"]
    )
    assert set(schema_of("take_screenshot")["properties"]) == {"uid"}


@pytest.mark.unit
def test_a_timeout_is_bounded_in_the_schema_and_clamped_in_the_call() -> None:
    for name in ("wait_for", "navigate_page"):
        timeout = schema_of(name)["properties"]["timeout"]
        assert (timeout["minimum"], timeout["maximum"]) == (
            0,
            story_relay.TIMEOUT_MS_MAX,
        )
    forward, journal, problem = story_relay.read_arguments(
        schema_of("wait_for"), {"text": ["Done"], "timeout": 10**15}
    )
    assert problem is None
    assert forward == {"text": ["Done"], "timeout": story_relay.TIMEOUT_MS_MAX}
    assert journal == {"text": '["Done"]', "timeout": story_relay.TIMEOUT_MS_MAX}
    forward, _, _ = story_relay.read_arguments(
        schema_of("navigate_page"), {"type": "reload", "timeout": -5}
    )
    assert forward == {"type": "reload", "timeout": 0}


@pytest.mark.unit
def test_only_the_schema_s_own_arguments_are_forwarded_or_journalled() -> None:
    forward, journal, problem = story_relay.read_arguments(
        schema_of("click"),
        {"uid": "1_16", "filePath": "/x", "bogus": 1, "includeSnapshot": True},
    )
    assert problem is None
    assert forward == {"uid": "1_16", "includeSnapshot": True}
    assert journal == forward
    forward, journal, _ = story_relay.read_arguments(
        schema_of("fill_form"),
        {"elements": [{"uid": "1_24", "value": "rvl"}]},
    )
    assert journal == {"elements": '[{"uid":"1_24","value":"rvl"}]'}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "arguments", "problem"),
    [
        ("click", {}, "uid is required"),
        ("click", {"uid": 7}, "uid must be a string"),
        ("click", {"uid": "1_1", "dblClick": "yes"}, "dblClick must be true or false"),
        (
            "handle_dialog",
            {"action": "maybe"},
            "action must be one of: accept, dismiss",
        ),
        ("wait_for", {"text": []}, "text needs at least 1 item(s)"),
        ("wait_for", {"text": [1]}, "each item of text must be a string"),
        ("wait_for", {"text": ["a"], "timeout": "5"}, "timeout must be a whole number"),
        ("fill_form", {"elements": "x"}, "elements must be a list"),
        (
            "fill_form",
            {"elements": [{"uid": "1"}]},
            "each item of elements needs a string value",
        ),
        ("fill_form", {"elements": ["x"]}, "each item of elements must be an object"),
    ],
)
def test_arguments_are_read_against_the_child_s_own_schema(
    name: str, arguments: dict[str, Any], problem: str
) -> None:
    _, _, found = story_relay.read_arguments(schema_of(name), arguments)
    assert found == problem


@pytest.mark.unit
def test_a_whole_number_too_large_for_the_journal_is_journalled_as_text() -> None:
    schema = {"properties": {"n": {"type": "integer"}, "x": {"type": "number"}}}
    _, journal, _ = story_relay.read_arguments(schema, {"n": 2**40, "x": 1.5})
    assert journal == {"n": str(2**40), "x": "1.5"}


@pytest.mark.unit
def test_a_recorded_snapshot_parses_into_elements_with_roles_names_and_parents() -> (
    None
):
    snapshot = snapshot_of("snapshot_documents")
    root = snapshot.nodes[0]
    assert (root.uid, root.role, root.name, root.depth, root.parent) == (
        "1_0",
        "RootWebArea",
        "Document List",
        0,
        None,
    )
    text = index_of(snapshot, uid_of(snapshot, "StaticText", "Upload Document(s)"))
    assert [node.role for node in snapshot.chain(text)] == [
        "StaticText",
        "link",
        "navigation",
        "RootWebArea",
    ]
    user = snapshot.nodes[index_of(snapshot, "1_4")]
    assert (user.role, user.name) == ("button", "new-analyst (Author)")
    nameless = snapshot.nodes[index_of(snapshot, "1_20")]
    assert (nameless.role, nameless.name) == ("button", "")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("line", "role", "name"),
    [
        ('uid=1_2 button "Save" focusable focused', "button", "Save"),
        ('uid=1_2 button "Say "hi" now" focusable', "button", 'Say "hi" now'),
        ('uid=1_2 link "Docs" url="https://x.example/a"', "link", "Docs"),
        ('uid=1_2 heading "A" level="2"', "heading", "A"),
        ("uid=1_2 generic", "generic", ""),
        ("uid=1_2", "", ""),
        (
            'uid=1_2 button "Pick" [selected in the DevTools Elements panel]',
            "button",
            "Pick",
        ),
    ],
)
def test_an_element_line_parses_whole_whatever_follows_the_name(
    line: str, role: str, name: str
) -> None:
    (node,) = story_relay.parse_nodes([line])
    assert (node.uid, node.role, node.name) == ("1_2", role, name)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("lines", "url"),
    [
        (['uid=1_2 link "Docs" url="https://x.example/a"'], "https://x.example/a"),
        (['uid=1_2 link url="https://x.example/a"'], "https://x.example/a"),
        (['uid=1_2 link "Docs url="https://x.example/a""'], None),
        (
            ['uid=1_2 link "Docs url="https://x.example/b"" url="https://x.example/a"'],
            None,
        ),
        (
            ['uid=1_2 link "Docs" url="https://x.example/a" url="https://x.example/b"'],
            None,
        ),
        (['uid=1_2 link "Docs" value="one', 'url="https://x.example/a""'], None),
        (
            ['uid=1_2 link "Docs" url="https://x.example/a" value="one', 'url="b""'],
            None,
        ),
        (['uid=1_2 RootWebArea "Docs url="https://x.example/a""'], None),
    ],
    ids=[
        "its-own",
        "unnamed",
        "quoted-by-its-name",
        "quoted-by-its-name-beside-its-own",
        "two-of-its-own",
        "quoted-by-a-value",
        "quoted-by-a-value-beside-its-own",
        "quoted-by-a-page-title",
    ],
)
def test_an_element_s_address_is_the_one_its_own_line_gives_after_its_name(
    lines: list[str], url: str | None
) -> None:
    """An address the element's name or value quotes is the page's text, so it gives
    the element none and takes away the one it has."""
    (node,) = story_relay.parse_nodes(lines)
    assert node.url == url


@pytest.mark.unit
def test_a_link_whose_name_quotes_a_route_is_judged_by_its_name() -> None:
    """A link with no address of its own whose name quotes one, in the side
    navigation, is no route link, so its name is judged."""
    snapshot = judged(
        f'uid=1_0 RootWebArea "Documents" url="{STACK}/#/documents"',
        '  uid=1_6 navigation "Side navigation"',
        f'    uid=1_20 link "Delete url="{STACK}/#/documents/upload""',
    )
    assert snapshot.nodes[index_of(snapshot, "1_20")].url is None
    assert snapshot.control_of("1_20") == (
        "commit_point",
        f'Delete url="{STACK}/#/documents/upload"',
    )


@pytest.mark.unit
def test_a_name_that_ends_by_quoting_an_attribute_reads_as_one() -> None:
    """The bound the relay's docstring states: the pinned server writes a name
    unescaped, so the name of a link with no address that ends in a quote and an
    address writes the line a route link has, and in the side navigation that link
    is followed."""
    forged = f'Delete" url="{STACK}/#/documents/upload'
    snapshot = judged(
        f'uid=1_0 RootWebArea "Documents" url="{STACK}/#/documents"',
        '  uid=1_6 navigation "Side navigation"',
        f'    uid=1_20 link "{forged}"',
    )
    link = snapshot.nodes[index_of(snapshot, "1_20")]
    assert (link.name, link.url) == ("Delete", f"{STACK}/#/documents/upload")
    assert snapshot.control_of("1_20") is None


@pytest.mark.unit
def test_a_line_that_continues_a_value_stays_with_its_element() -> None:
    nodes = story_relay.parse_nodes(
        [
            'uid=1_0 RootWebArea "Notes"',
            '  uid=1_1 textbox "Comment" multiline value="first',
            'second"',
            '  uid=1_2 button "Save"',
        ]
    )
    assert [node.uid for node in nodes] == ["1_0", "1_1", "1_2"]
    assert nodes[1].text.endswith('second"')
    assert nodes[2].parent == 0


@pytest.mark.unit
def test_a_snapshot_within_both_bounds_is_shown_whole() -> None:
    lines = section_of("snapshot_documents")
    snapshot = story_relay.cut_snapshot(lines)
    assert snapshot.text == "\n".join([story_relay.SNAPSHOT_HEADING, *lines])
    assert len(snapshot.shown) == snapshot.total == 43


@pytest.mark.unit
def test_a_long_snapshot_is_cut_at_220_elements_on_element_boundaries_with_a_note() -> (
    None
):
    lines = section_of("snapshot_long")
    snapshot = story_relay.cut_snapshot(lines)
    assert snapshot.total == 606
    assert len(snapshot.shown) == story_relay.SNAPSHOT_NODES == 220
    shown = snapshot.text.split("\n")
    assert shown[0] == story_relay.SNAPSHOT_HEADING
    assert shown[1:-1] == lines[:220]
    assert shown[-1] == story_relay.CUT_TEXT.format(shown=220, total=606)
    assert len(snapshot.text) <= story_relay.SNAPSHOT_CHARS + len(shown[-1]) + 30


@pytest.mark.unit
def test_the_character_bound_cuts_a_snapshot_too() -> None:
    lines = section_of("snapshot_documents")
    snapshot = story_relay.cut_snapshot(lines, chars=500)
    kept = snapshot.text.split("\n")[1:-1]
    assert sum(len(line) + 1 for line in kept) <= 500
    assert kept == lines[: len(kept)]
    assert "Snapshot cut" in snapshot.text


@pytest.mark.unit
def test_an_open_modal_is_kept_whole_when_the_page_around_it_is_cut() -> None:
    dialog = section_of("snapshot_dialog_open")
    start = next(i for i, line in enumerate(dialog) if " dialog " in line)
    modal = [line[2:] for line in dialog[start:]]
    long = section_of("snapshot_long")
    snapshot = story_relay.cut_snapshot([*long, *modal])
    shown_text = snapshot.text
    for line in modal:
        assert line in shown_text, line
    assert len(snapshot.shown) == 220
    assert snapshot.text.split("\n")[1] == long[0]


@pytest.mark.unit
def test_a_uid_repeated_in_one_snapshot_resolves_to_its_last_element() -> None:
    snapshot = snapshot_of("snapshot_documents_verbose")
    repeated = [node for node in snapshot.nodes if node.uid == "2_2"]
    assert len(repeated) >= 2
    assert snapshot.nodes[index_of(snapshot, "2_2")] is repeated[-1]


@pytest.mark.unit
def test_an_address_is_seen_only_when_a_shown_line_held_it() -> None:
    snapshot = snapshot_of("snapshot_documents")
    prefix = (
        f"{DOCS}/configuration-profiles/#prefix-mappings-assigning-a-profile-by-where-a-"
        "document-lands"
    )
    path = prefix.removeprefix("https://aws-solutions-library-samples.github.io")
    assert {prefix, UNINDEXED_HELP_LINK, path, path.lstrip("/")} <= snapshot.addresses
    long = section_of("snapshot_long")
    cut = story_relay.cut_snapshot(long)
    assert "http://localhost:3000/long.html#doc-1" in cut.addresses
    assert "http://localhost:3000/long.html#doc-150" not in cut.addresses


@pytest.mark.unit
@pytest.mark.parametrize(
    ("role", "name", "expected"),
    [
        ("button", "Refresh", None),
        ("button", "Reprocess", ("commit_point", "Reprocess")),
        ("button", "Delete", ("commit_point", "Delete")),
        ("button", "Download results", None),
        ("button", "", ("commit_point", "an unnamed button")),
        ("button", "Add prefix mapping", None),
        ("button", "Create test set", None),
        ("link", "Upload Document(s)", None),
        ("StaticText", "Upload Document(s)", None),
        ("link", "Prefix mappings", None),
        ("menuitem", "Sign out", ("session_end", "Sign out")),
        ("textbox", "S3 prefix", None),
        ("button", "Cancel", None),
        ("button", "Add another prefix", ("commit_point", "Add another prefix")),
        ("button", "Save", ("commit_point", "Save")),
        ("link", "New profile", ("commit_point", "New profile")),
        ("StaticText", "New profile", ("commit_point", "New profile")),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_the_commit_guard_reads_the_recorded_page_in_and_out_of_its_dialog(
    role: str, name: str, expected: tuple[str, str] | None
) -> None:
    snapshot = snapshot_of("snapshot_dialog_open")
    uid = uid_of(snapshot, role, name)
    assert snapshot.commit_control(index_of(snapshot, uid)) == expected


@pytest.mark.unit
def test_the_recorded_dialog_is_what_puts_its_controls_inside_one() -> None:
    snapshot = snapshot_of("snapshot_dialog_open")
    assert snapshot.in_dialog(index_of(snapshot, uid_of(snapshot, "button", "Save")))
    add = uid_of(snapshot, "button", "Add prefix mapping")
    assert not snapshot.in_dialog(index_of(snapshot, add))
    outside = snapshot_of("snapshot_documents")
    assert not any(node.role in story_relay.DIALOG_ROLES for node in outside.nodes)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "verbs", "expected"),
    [
        ("Save changes", story_relay.COMMIT_VERBS, True),
        ("Saved views", story_relay.COMMIT_VERBS, False),
        ("Run evaluation", story_relay.COMMIT_VERBS, True),
        ("Running jobs", story_relay.COMMIT_VERBS, False),
        ("Choose files", story_relay.COMMIT_VERBS, True),
        ("Re-extract", story_relay.COMMIT_VERBS, True),
        ("Restore default (All)", story_relay.COMMIT_VERBS, True),
        ("Settings", story_relay.COMMIT_VERBS, False),
        ("Download results", story_relay.COMMIT_VERBS, False),
        ("Cancel", story_relay.COMMIT_VERBS, False),
        ("Cancel Subscription", story_relay.COMMIT_VERBS, True),
        ("Complete", story_relay.COMMIT_VERBS, True),
        ("Completed", story_relay.COMMIT_VERBS, False),
        ("Take over", story_relay.COMMIT_VERBS, True),
        ("Next item", story_relay.COMMIT_VERBS, True),
        ("Next", story_relay.COMMIT_VERBS, False),
        ("Unassign (2)", story_relay.COMMIT_VERBS, True),
        ("Clear read", story_relay.COMMIT_VERBS, True),
        ("Clear filter", story_relay.COMMIT_VERBS, False),
        ("+ Add", story_relay.DIALOG_VERBS, True),
        ("Address book", story_relay.DIALOG_VERBS, False),
        ("Newest first", story_relay.DIALOG_VERBS, False),
        ("Assign to\u2026", story_relay.DIALOG_VERBS, True),
        ("Assignee", story_relay.DIALOG_VERBS, False),
        ("Move up", story_relay.DIALOG_VERBS, True),
        ("Movement", story_relay.DIALOG_VERBS, False),
        ("Save draft", story_relay.AUTOSAVE_CONTROLS, True),
        ("Save drafts", story_relay.AUTOSAVE_CONTROLS, False),
        ("Log out", story_relay.SESSION_END_VERBS, True),
        ("Sign Out", story_relay.SESSION_END_VERBS, True),
        ("Sign outline", story_relay.SESSION_END_VERBS, False),
    ],
)
def test_a_verb_matches_a_control_name_as_a_whole_word_or_phrase(
    name: str, verbs: tuple[str, ...], expected: bool
) -> None:
    assert story_relay.begins_with(story_relay.plain(name), verbs) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Claimed by you \u2014 release", "commit_point"),
        ("Labels are correct \u2014 mark reviewed", "commit_point"),
        ("Status \u2014 shown", None),
        ("Profile \u2014 sign out", "session_end"),
    ],
)
def test_a_name_is_judged_after_its_dash_as_well(
    name: str, expected: str | None
) -> None:
    snapshot = judged('uid=1_0 RootWebArea "x"', f'  uid=1_1 button "{name}"')
    found = snapshot.commit_control(1)
    assert (found[0] if found else None) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "inside", "expected"),
    [
        ("Assign to\u2026", False, None),
        ("Reassign\u2026", False, None),
        ("Move to another queue\u2026", False, None),
        ("Move up", False, None),
        ("Create queue", False, None),
        ("Clear filter", True, None),
        ("Assign", True, ("commit_point", "Assign")),
        ("Move", True, ("commit_point", "Move")),
        ("Create", True, ("commit_point", "Create")),
        ("Unassign", False, ("commit_point", "Unassign")),
        ("Clear read", False, ("commit_point", "Clear read")),
        ("Next item", False, ("commit_point", "Next item")),
    ],
)
def test_a_dialog_verb_is_refused_inside_a_dialog_and_an_opener_outside_one_is_not(
    name: str, inside: bool, expected: tuple[str, str] | None
) -> None:
    lines = ['uid=1_0 RootWebArea "Queue"', "  uid=1_1 main"]
    indent = "    "
    if inside:
        lines.append('    uid=6_0 dialog "Assign to Annotator" modal')
        indent = "      "
    lines.append(f'{indent}uid=9_1 button "{name}"')
    assert judged(*lines).control_of("9_1") == expected


@pytest.mark.unit
def test_an_unnamed_control_is_refused_only_beside_a_text_entry() -> None:
    chat = judged(
        'uid=1_0 RootWebArea "Chat"',
        "  uid=1_1 main",
        '    uid=1_2 textbox "Ask a question" multiline',
        "    uid=1_3 button",
        "      uid=1_4 image",
        "  uid=1_5 navigation",
        "    uid=1_6 button",
    )
    assert chat.commit_control(3) == ("commit_point", "an unnamed button")
    assert chat.commit_control(4) == ("commit_point", "an unnamed button")
    assert chat.commit_control(6) is None


@pytest.mark.unit
def test_every_element_carrying_a_clicked_uid_is_judged() -> None:
    forged = judged(
        'uid=1_0 RootWebArea "Notes"',
        "  uid=1_1 main",
        '    uid=1_18 button "Delete"',
        '    uid=1_19 textbox "Notes" value="notes',
        '    uid=1_18 generic "Notes"',
        'x"',
    )
    assert [forged.nodes[i].role for i in forged.indices("1_18")] == [
        "button",
        "generic",
    ]
    assert forged.control_of("1_18") == ("commit_point", "Delete")


@pytest.mark.unit
def test_a_line_of_page_text_cannot_take_a_control_out_of_its_modal() -> None:
    forged = judged(
        'uid=1_0 RootWebArea "Users"',
        '  uid=6_0 dialog "Create New User" modal',
        '    uid=6_1 textbox "Email" value="a@example.com',
        "  uid=9_9 generic",
        '"',
        '    uid=6_2 button "Create User"',
    )
    create = forged.find("6_2")
    assert create is not None and forged.nodes[create].parent == 3
    assert forged.in_dialog(create)
    assert forged.control_of("6_2") == ("commit_point", "Create User")


@pytest.mark.unit
def test_a_line_of_page_text_can_take_a_control_out_of_a_dialog_that_is_not_modal() -> (
    None
):
    """The bound the relay's docstring states: a forged line re-parents the real
    elements after it, so a refusal that rests only on where a control sits can be
    taken away. A modal is read by order instead, as the test above shows."""
    lines = [
        'uid=1_0 RootWebArea "Users"',
        '  uid=6_0 dialog "Create New User"',
        '    uid=6_1 textbox "Email" value="a@example.com"',
        '    uid=6_2 button "Create User"',
    ]
    assert judged(*lines).control_of("6_2") == ("commit_point", "Create User")
    forged = judged(
        *lines[:2],
        '    uid=6_1 textbox "Email" value="a@example.com',
        "  uid=9_9 generic",
        '"',
        lines[3],
    )
    create = forged.find("6_2")
    assert create is not None and forged.nodes[create].parent == 3
    assert not forged.in_dialog(create)
    assert forged.control_of("6_2") is None


#: The review stack's web address in the pages built below; any https origin serves.
STACK = "https://d1ux7nightly.cloudfront.net"
#: The side navigation's upload link, at the uid the guard refused it at.
UPLOAD_LINK = "1_20"
NAVIGATION_SOURCE = "components/genaiidp-layout/navigation.tsx"


def _side_navigation(upload: str = "Upload Document(s)") -> list[str]:
    """The side navigation the review stack shows the Author persona, as the pinned
    chrome-devtools-mcp writes it.

    A local dry run of the nightly set refused the click on its "Upload Document(s)"
    link, at uid 1_20, which left an allowed story stuck. The run's journal records
    the click and not the snapshot, so this is that run's screenshot of the
    navigation, with the addresses the stack's ``navigation.tsx`` gives each link:
    routes of the app's hash router, ``#``-anchors the Deployment Info items are not
    followed to, and external pages.
    """
    route = f"{STACK}/#"
    lines = [
        '  uid=1_6 navigation "Side navigation"',
        '    uid=1_7 button "Quick Start"',
        '    uid=1_8 button "Close side navigation"',
        '    uid=1_9 heading "Tools" level="2"',
        f'      uid=1_10 link "Tools" url="{route}/documents"',
        '        uid=1_11 StaticText "Tools"',
        f'    uid=1_12 link "Document List" url="{route}/documents"',
        '      uid=1_13 StaticText "Document List"',
        f'    uid={UPLOAD_LINK} link "{upload}" url="{route}/documents/upload"',
        f'      uid=1_21 StaticText "{upload}"',
        '    uid=1_22 button "Configuration" expandable expanded',
    ]
    number = 23
    for name, path in (
        ("View/Edit Configuration", "/documents/config"),
        ("Discovery", "/documents/discovery"),
        ("Custom Models", "/documents/custom-models"),
        ("Capacity Planning", "/documents/capacity-planning"),
        ("View Pricing", "/documents/pricing"),
        ("View Model Limits", "/documents/model-limits"),
        ("Test Studio", None),
        ("Test Sets", "/test-studio?tab=sets"),
        ("Test Executions", "/test-studio?tab=executions"),
        ("Extensions", None),
        ("Browse catalog", "/features"),
        ("Auto Optimizer (Beta)", "/features/auto-optimizer"),
        ("Monitor (Beta)", "/features/idp-monitor"),
        ("PII Anonymization", "/features/pii-anonymizer"),
        ("Test Set Generator", "/features/test-set-generator"),
        ("Resources", None),
        ("Blog", "https://www.amazon.com/genai-idp-accelerator"),
        ("Documentation", f"{DOCS}/"),
        ("Report an issue", f"{DOCS}/issues"),
        ("Deployment Info", None),
        ("Stack Name: idp-ux-nightly", "#stackname"),
        ("Version: 0.6.12", "#version"),
    ):
        if path is None:
            lines.append(f'    uid=1_{number} button "{name}" expandable expanded')
            number += 1
            continue
        if path.startswith("/"):
            address = f"{route}{path}"
        elif path.startswith("#"):
            address = f"{STACK}/{path}"
        else:
            address = path
        lines.append(f'    uid=1_{number} link "{name}" url="{address}"')
        lines.append(f'      uid=1_{number + 1} StaticText "{name}"')
        number += 2
    return lines


def _stack_page(page: str, *main: str, upload: str = "Upload Document(s)") -> list[str]:
    """A page of the review stack at route ``page``: its banner, the side
    navigation, and ``main``'s lines inside its main landmark."""
    return [
        f'uid=1_0 RootWebArea "GenAIIDP" url="{STACK}/#{page}"',
        "  uid=1_1 banner",
        f'    uid=1_2 link "AWS IDP Accelerator Console" url="{STACK}/#/"',
        '      uid=1_3 StaticText "AWS IDP Accelerator Console"',
        '    uid=1_4 button "Feedback" expandable haspopup="menu"',
        '    uid=1_5 button "UX Persona Author (Author)" expandable haspopup="menu"',
        *_side_navigation(upload),
        "  uid=3_0 main",
        *main,
    ]


#: The Documents page the stuck flow opened on, below the navigation.
_DOCUMENTS = (
    '    uid=3_1 navigation "Breadcrumbs"',
    f'      uid=3_2 link "Document Processing" url="{STACK}/#/documents"',
    '        uid=3_3 StaticText "Document Processing"',
    '    uid=3_4 heading "Documents (3)" level="2"',
    '    uid=3_5 button "Info"',
    '    uid=3_6 button "Load: Latest" expandable haspopup="menu"',
    '    uid=3_7 button "Refresh"',
    '    uid=3_8 button "Download" expandable haspopup="menu"',
    '    uid=3_9 button "Abort"',
    '    uid=3_10 button "Reprocess"',
    '    uid=3_11 button "Delete"',
    '    uid=3_12 searchbox "Find documents"',
    f'    uid=3_13 link "ux-fixtures-20261020-123142/lending_package.pdf" url="{STACK}/#/documents/ux-fixtures-20261020-123142%2Flending_package.pdf"',
)
#: The upload page the link opens, from ``UploadDocumentPanel.tsx``, in each source
#: mode, below the navigation: each name the page shows, and the text the source
#: builds it from.
_UPLOAD_FROM_COMPUTER = (
    '    uid=4_1 heading "Upload Documents" level="2"',
    '    uid=4_2 button "From my computer" pressed',
    '    uid=4_3 button "Sample documents"',
    '    uid=4_4 textbox "Optional folder prefix (e.g., invoices/2024)" focusable focused',
    '    uid=4_5 button "Configuration Profile" expandable haspopup="listbox"',
    '    uid=4_6 button "Choose files"',
    '    uid=4_7 StaticText "Drop files to upload"',
    '    uid=4_8 button "Upload (1 files)"',
)
_UPLOAD_SAMPLE = (
    '    uid=4_1 heading "Upload Documents" level="2"',
    '    uid=4_2 button "From my computer"',
    '    uid=4_3 button "Sample documents" pressed',
    '    uid=4_9 button "Select a sample document" expandable haspopup="listbox"',
    '    uid=4_10 checkbox "Also import and use its configuration (bank-statement-sample)"',
    '    uid=4_11 button "Process sample"',
)
_UPLOAD_SOURCES = (
    "'Choose files'",
    "'Process sample'",
    "`Upload ${",
    "Optional folder prefix (e.g., invoices/2024)",
)


@pytest.mark.unit
@pytest.mark.parametrize("upload", ["Upload Document(s)", "Upload Documents"])
def test_the_side_navigation_link_the_guard_refused_is_followed(upload: str) -> None:
    """The click that dry run refused, on the stack's label and on the merged UI's: the
    link, its own text and Enter with the link focused all open the upload page,
    and no link in the side navigation is refused."""
    snapshot = judged(*_stack_page("/documents", *_DOCUMENTS, upload=upload))
    link = index_of(snapshot, UPLOAD_LINK)
    assert snapshot.nodes[link].name == upload
    assert snapshot.followed_link(link) == link
    assert snapshot.followed_link(index_of(snapshot, "1_21")) == link
    assert snapshot.control_of(UPLOAD_LINK) is None
    assert snapshot.control_of("1_21") is None
    focused = judged(
        *(
            f"{line} focusable focused" if f"uid={UPLOAD_LINK} " in line else line
            for line in _stack_page("/documents", *_DOCUMENTS, upload=upload)
        )
    )
    assert focused.pressed("enter") is None
    navigation = index_of(snapshot, "1_6")
    refused = [
        (node.uid, node.name)
        for index, node in enumerate(snapshot.nodes)
        if navigation in snapshot._chain_indices(index)[1:]
        and snapshot.control_of(node.uid) is not None
    ]
    assert not refused, refused


@pytest.mark.unit
def test_the_merged_side_navigation_still_offers_the_upload_link_as_a_route() -> None:
    """The label the test above checks the merged UI by, and the address that makes
    it a route of the app rather than a control the page handles."""
    text = (UI_SOURCE / NAVIGATION_SOURCE).read_text(encoding="utf-8")
    assert (
        "{ type: 'link', text: 'Upload Documents', href: `#${UPLOAD_DOCUMENT_PATH}` }"
        in text
    )


@pytest.mark.unit
def test_the_upload_page_still_refuses_its_own_commit_controls() -> None:
    """Following the navigation to the upload page leaves every control there that
    sends a file refused: the file picker, the Upload button, Process sample, and
    Enter in the folder prefix. The navigation's link to the page it is on opens
    nothing, so it is judged by its name again, while its links elsewhere are
    still followed."""
    source = (
        UI_SOURCE / "components/upload-document/UploadDocumentPanel.tsx"
    ).read_text(encoding="utf-8")
    for needle in _UPLOAD_SOURCES:
        assert needle in source, needle
    local = judged(*_stack_page("/documents/upload", *_UPLOAD_FROM_COMPUTER))
    assert local.control_of(UPLOAD_LINK) == ("commit_point", "Upload Document(s)")
    assert local.control_of(uid_of(local, "link", "Browse catalog")) is None
    assert local.control_of("4_2") is None
    assert local.control_of("4_6") == ("commit_point", "Choose files")
    assert local.control_of("4_8") == ("commit_point", "Upload (1 files)")
    assert local.pressed("enter") == (
        "commit_point",
        "Enter in Optional folder prefix (e.g., invoices/2024)",
    )
    sample = judged(*_stack_page("/documents/upload", *_UPLOAD_SAMPLE))
    assert sample.control_of(UPLOAD_LINK) == ("commit_point", "Upload Document(s)")
    assert sample.control_of("4_11") == ("commit_point", "Process sample")


def _with_upload_link(line: str) -> list[str]:
    """The Documents page with the upload link's own line replaced by ``line``."""
    return [
        line if f"uid={UPLOAD_LINK} " in existing else existing
        for existing in _stack_page("/documents", *_DOCUMENTS)
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("element", "expected"),
    [
        ('link "Upload Document(s)"', "commit_point"),
        (f'link "Upload Document(s)" url="{STACK}/#"', "commit_point"),
        (f'link "Upload Document(s)" url="{STACK}/#stackname"', "commit_point"),
        (f'link "Upload Document(s)" url="{STACK}/"', "commit_point"),
        (
            'link "Upload Document(s)" url="https://elsewhere.example/#/documents/upload"',
            "commit_point",
        ),
        (
            f'link "Upload Document(s)" url="{STACK}:8443/#/documents/upload"',
            "commit_point",
        ),
        (
            f'link "Upload Document(s)" url="{STACK.replace("https", "http")}/#/up"',
            "commit_point",
        ),
        ('link "Upload Document(s)" url="javascript:void(0)"', "commit_point"),
        (
            f'link "Upload Document(s)" url="{STACK}/#/a" url="{STACK}/#/b"',
            "commit_point",
        ),
        (
            f'button "Upload Document(s)" url="{STACK}/#/documents/upload"',
            "commit_point",
        ),
        (
            f'menuitem "Upload Document(s)" url="{STACK}/#/documents/upload"',
            "commit_point",
        ),
        (f'link "Switch user" url="{STACK}/#/logout"', "session_end"),
        (f'link "Sign out" url="{STACK}/#/documents"', "session_end"),
        (f'link "Upload Document(s)" url="{STACK}/#/documents/upload"', None),
    ],
)
def test_only_a_link_with_one_address_on_a_route_of_the_app_is_followed(
    element: str, expected: str | None
) -> None:
    """A link the page handles, one that leaves the app, one that is not a link, and
    one that ends the session are judged as controls; only the route link is not."""
    snapshot = judged(*_with_upload_link(f"    uid={UPLOAD_LINK} {element}"))
    found = snapshot.control_of(UPLOAD_LINK)
    assert (found[0] if found else None) == expected, found


@pytest.mark.unit
@pytest.mark.parametrize(
    "lines",
    [
        [
            f'uid=1_0 RootWebArea "Documents" url="{STACK}/#/documents"',
            "  uid=3_0 main",
            f'    uid=1_20 link "Upload Document(s)" url="{STACK}/#/documents/upload"',
        ],
        [
            f'uid=1_0 RootWebArea "Documents" url="{STACK}/#/documents"',
            '  uid=6_0 dialog "Steps" modal',
            '    uid=6_1 navigation "Steps"',
            f'      uid=1_20 link "Upload Document(s)" url="{STACK}/#/documents/upload"',
        ],
        [
            f'uid=1_0 RootWebArea "Documents" url="{STACK}/#/documents"',
            '  uid=6_0 dialog "Confirm" modal',
            '  uid=1_6 navigation "Side navigation"',
            f'    uid=1_20 link "Upload Document(s)" url="{STACK}/#/documents/upload"',
        ],
        [
            'uid=1_0 RootWebArea "Documents"',
            '  uid=1_6 navigation "Side navigation"',
            f'    uid=1_20 link "Upload Document(s)" url="{STACK}/#/documents/upload"',
        ],
        [
            f'uid=1_6 navigation "Side navigation" url="{STACK}/#/documents"',
            f'  uid=1_20 link "Upload Document(s)" url="{STACK}/#/documents/upload"',
        ],
    ],
    ids=[
        "outside-navigation",
        "inside-a-dialog",
        "after-an-open-modal",
        "no-page-address",
        "no-page-root",
    ],
)
def test_a_route_link_is_judged_by_its_name_outside_a_navigation_landmark(
    lines: list[str],
) -> None:
    """The same route link is a control again in main content, in a dialog or after
    an open modal, and on a page whose own address is unknown: the page's address
    is its root element's, and only a root web area's."""
    snapshot = judged(*lines)
    assert snapshot.followed_link(index_of(snapshot, "1_20")) is None
    assert snapshot.control_of("1_20") == ("commit_point", "Upload Document(s)")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("landmark", "followed"),
    [
        ('navigation "Side navigation"', True),
        ('navigation "SIDE  navigation"', True),
        ('navigation "Breadcrumbs"', False),
        ('navigation "Steps"', False),
        ('navigation "Side navigation links"', False),
        ("navigation", False),
    ],
)
def test_only_the_side_navigation_has_its_route_links_followed(
    landmark: str, followed: bool
) -> None:
    """A route link is followed in the landmark the web UI names the side navigation
    and in no other: breadcrumbs, a wizard's steps or an unnamed navigation can hold
    a link whose page sends something when it is clicked, so their links are judged
    by name, clicked or pressed with Enter."""
    lines = [
        f'uid=1_0 RootWebArea "Documents" url="{STACK}/#/documents"',
        f"  uid=1_6 {landmark}",
        f'    uid=1_20 link "Complete review" url="{STACK}/#/review-queues"',
    ]
    expected = None if followed else ("commit_point", "Complete review")
    assert judged(*lines).control_of("1_20") == expected
    focused = judged(*lines[:2], f"{lines[2]} focusable focused")
    assert focused.pressed("enter") == expected


@pytest.mark.unit
def test_a_navigation_inside_the_side_navigation_has_its_links_judged() -> None:
    """The landmark that decides is the nearest one holding the link, so a list of
    steps drawn inside the side navigation is judged as one drawn anywhere else."""
    snapshot = judged(
        f'uid=1_0 RootWebArea "Documents" url="{STACK}/#/documents"',
        '  uid=1_6 navigation "Side navigation"',
        '    uid=1_7 navigation "Steps"',
        f'      uid=1_20 link "Complete review" url="{STACK}/#/review-queues"',
        f'    uid=1_21 link "Complete review" url="{STACK}/#/review-queues"',
    )
    assert snapshot.control_of("1_20") == ("commit_point", "Complete review")
    assert snapshot.control_of("1_21") is None


@pytest.mark.unit
def test_a_control_holding_a_followed_link_is_judged_by_its_own_name() -> None:
    """A click on the link presses the control around it too, so that control is
    judged as any is, and the link's text inside it with it."""
    snapshot = judged(
        f'uid=1_0 RootWebArea "Documents" url="{STACK}/#/documents"',
        '  uid=1_6 navigation "Side navigation"',
        '    uid=1_19 menuitem "Upload and process"',
        f'      uid=1_20 link "Upload Document(s)" url="{STACK}/#/documents/upload"',
        '        uid=1_21 StaticText "Upload Document(s)"',
    )
    link = index_of(snapshot, "1_20")
    assert snapshot.followed_link(link) == link
    for uid in ("1_20", "1_21"):
        assert snapshot.control_of(uid) == ("commit_point", "Upload and process")


@pytest.mark.unit
def test_a_control_inside_a_followed_link_is_judged_by_its_own_name() -> None:
    """A click lands on the nearest control, so a button inside a route link is
    pressed itself, and only the link's own text follows the link."""
    snapshot = judged(
        f'uid=1_0 RootWebArea "Documents" url="{STACK}/#/documents"',
        '  uid=1_6 navigation "Side navigation"',
        f'    uid=1_20 link "Upload Document(s)" url="{STACK}/#/documents/upload"',
        '      uid=1_21 button "Delete"',
        '      uid=1_22 StaticText "Upload Document(s)"',
    )
    assert snapshot.followed_link(index_of(snapshot, "1_21")) is None
    assert snapshot.control_of("1_21") == ("commit_point", "Delete")
    assert snapshot.control_of("1_22") is None


@pytest.mark.unit
def test_an_unnamed_route_link_beside_a_text_box_is_followed_and_a_button_is_not() -> (
    None
):
    """An icon link to a route beside the navigation's search box opens a page; the
    unnamed button beside it is still taken for the box's send control."""
    snapshot = judged(
        f'uid=1_0 RootWebArea "Documents" url="{STACK}/#/documents"',
        '  uid=1_6 navigation "Side navigation"',
        '    uid=1_7 searchbox "Find a page"',
        f'    uid=1_8 link url="{STACK}/#/documents/upload"',
        "    uid=1_9 button",
    )
    assert snapshot.control_of("1_8") is None
    assert snapshot.control_of("1_9") == ("commit_point", "an unnamed button")


@pytest.mark.unit
def test_a_forged_line_cannot_have_a_button_or_a_link_without_a_route_followed() -> (
    None
):
    """A forged navigation landmark adds nothing a control's own line does not
    give: a button, and a link whose only address its own name quotes beside its
    real one, stay refused."""
    snapshot = judged(
        f'uid=1_0 RootWebArea "Notes" url="{STACK}/#/documents"',
        "  uid=3_0 main",
        '    uid=3_1 textbox "Notes" value="notes',
        '  uid=9_0 navigation "Side navigation"',
        '"',
        '    uid=3_2 button "Delete"',
        f'    uid=3_3 link "Delete" url="{STACK}/#/x" url="{STACK}/#/documents"',
    )
    assert snapshot.control_of("3_2") == ("commit_point", "Delete")
    assert snapshot.control_of("3_3") == ("commit_point", "Delete")


@pytest.mark.unit
def test_a_forged_side_navigation_can_have_a_later_route_link_followed() -> None:
    """The bound the relay's docstring states: a forged side navigation re-parents
    the real route link after it, which is then followed as the side navigation's
    own links are. A forged landmark of any other name gives it nothing."""
    lines = [
        f'uid=1_0 RootWebArea "Notes" url="{STACK}/#/notes"',
        "  uid=3_0 main",
        '    uid=3_1 textbox "Notes" value="notes"',
        f'    uid=3_3 link "Delete all" url="{STACK}/#/documents"',
    ]
    assert judged(*lines).control_of("3_3") == ("commit_point", "Delete all")
    for landmark, expected in (
        ('navigation "Side navigation"', None),
        ("navigation", ("commit_point", "Delete all")),
        ('navigation "Breadcrumbs"', ("commit_point", "Delete all")),
    ):
        forged = judged(
            *lines[:2],
            '    uid=3_1 textbox "Notes" value="notes',
            f"  uid=9_0 {landmark}",
            '"',
            lines[3],
        )
        assert forged.control_of("3_3") == expected, landmark


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "page", "route"),
    [
        (f"{STACK}/#/documents/upload", f"{STACK}/#/documents", "/documents/upload"),
        (f"{STACK}/#/test-studio?tab=sets", f"{STACK}/#/documents", "/test-studio"),
        (f"{STACK}/#/documents", f"{STACK}/#/documents", None),
        (f"{STACK}/#/documents?tab=a", f"{STACK}/#/documents", "/documents"),
        (f"{STACK}/#/documents", f"{STACK}/#/documents?tab=a", "/documents"),
        (f"{STACK}/other/#/documents", f"{STACK}/#/documents", "/documents"),
        (f"{STACK.upper()}/#/documents", f"{STACK}/#/x", "/documents"),
        (f"{STACK}/#", f"{STACK}/#/documents", None),
        (f"{STACK}/#stackname", f"{STACK}/#/documents", None),
        (f"{STACK}/", f"{STACK}/#/documents", None),
        (f"{STACK}:443/#/documents", f"{STACK}/#/x", None),
        (
            "http://localhost:3000/upload.html",
            "http://localhost:3000/a.html",
            "/upload.html",
        ),
        ("http://localhost:3000/a.html?b=1", "http://localhost:3000/a.html", "/a.html"),
        ("http://localhost:3000/a.html", "http://localhost:3000/a.html", None),
        ("http://localhost:3000/a.html#profiles", "http://localhost:3000/a.html", None),
        ("https://elsewhere.example/#/documents", f"{STACK}/#/x", None),
        ("javascript:void(0)", f"{STACK}/#/x", None),
        ("http://[broken/path", f"{STACK}/#/x", None),
        ("https:///#/documents", "https:///#/x", None),
        (f"{STACK}/#/x", "about:blank", None),
        ("chrome://settings/x", "chrome://settings/", None),
    ],
)
def test_a_route_is_another_page_of_the_same_app(
    url: str, page: str, route: str | None
) -> None:
    assert story_relay.route_of(url, page) == route
    assert story_relay.leads_to_a_route(url, page) is (route is not None)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("route", "signs_out"),
    [
        ("/logout", True),
        ("/a/sign-out", True),
        ("/log%20out", True),
        ("/LOGOUT", True),
        ("/logouts", False),
        ("/documents/upload", False),
        ("/", False),
    ],
)
def test_a_route_names_a_sign_out_by_a_whole_segment(
    route: str, signs_out: bool
) -> None:
    assert story_relay.names_a_sign_out(route) is signs_out


@pytest.mark.unit
def test_the_relay_forwards_a_followed_link_and_refuses_the_page_it_opens(
    make_rig: Callable[..., Rig],
) -> None:
    """Through the relay: the click on the side navigation's upload link reaches the
    browser, and the upload page's Upload button, once the page is read again,
    does not."""
    documents = page(*_stack_page("/documents", *_DOCUMENTS))
    upload = page(*_stack_page("/documents/upload", *_UPLOAD_FROM_COMPUTER))
    rig = make_rig({"calls": {"take_snapshot": [documents, upload]}})
    rig.call("take_snapshot")
    assert not rig.call("click", uid=UPLOAD_LINK).is_error
    assert rig.last()["refused"] is None
    rig.call("take_snapshot")
    refused = rig.call("click", uid="4_8")
    assert reply_text(refused) == story_relay.COMMIT_POINT_TEXT
    assert (rig.last()["refused"], rig.last()["label"]) == (
        "commit_point",
        "Upload (1 files)",
    )
    clicks = [call for call in rig.tool_calls() if call["name"] == "click"]
    assert [call["arguments"]["uid"] for call in clicks] == [UPLOAD_LINK]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "pressed"),
    [
        ("Enter", "enter"),
        ("Control+Enter", "enter"),
        ("Control+Shift+R", "r"),
        ("Control++", "+"),
        (" ", " "),
        ("NumpadEnter", "numpadenter"),
        ("", ""),
    ],
)
def test_a_combination_presses_its_last_key_as_the_pinned_server_parses_it(
    key: str, pressed: str
) -> None:
    assert story_relay.pressed_key(key) == pressed


@pytest.mark.unit
@pytest.mark.parametrize(
    ("lines", "key", "expected"),
    [
        (['  uid=1_1 button "Create User" focusable focused'], "enter", None),
        (
            [
                '  uid=6_0 dialog "Create New User" modal',
                '    uid=6_1 button "Create User" focusable focused',
            ],
            " ",
            ("commit_point", "Create User"),
        ),
        (
            ['  uid=1_1 button "Save" focusable focused'],
            "enter",
            ("commit_point", "Save"),
        ),
        (['  uid=1_1 button "Save" focusable focused'], "tab", None),
        (['  uid=1_1 button "Refresh" focusable focused'], "enter", None),
        (
            ['  uid=1_1 textbox "Ask a question" focusable focused multiline'],
            "enter",
            ("commit_point", "Enter in Ask a question"),
        ),
        (
            ['  uid=1_1 textbox "Your message" focusable focused'],
            "enter",
            ("commit_point", "Enter in Your message"),
        ),
        (['  uid=1_1 textbox "Your message" focusable focused'], " ", None),
        (['  uid=1_1 textbox "Delete reason" focusable focused'], " ", None),
        (['  uid=1_1 searchbox "Remove filter" focusable focused'], "space", None),
        (
            ['  uid=1_1 textbox "Delete reason" focusable focused'],
            "enter",
            ("commit_point", "Delete reason"),
        ),
        (
            ['  uid=1_1 button "Take over" focusable focused'],
            " ",
            ("commit_point", "Take over"),
        ),
        (
            ['  uid=1_1 checkbox "Enable several documents per section" focused'],
            "space",
            ("commit_point", "Enable several documents per section"),
        ),
        (['  uid=1_1 searchbox "Find documents" focusable focused'], "enter", None),
        (
            [
                "  uid=1_1 form",
                '    uid=1_2 combobox "Profile" focusable focused',
            ],
            "enter",
            ("commit_point", "Enter in Profile"),
        ),
        (
            [
                '  uid=6_0 dialog "Add prefix mapping" modal',
                '    uid=6_1 searchbox "S3 prefix" focusable focused',
            ],
            "\n",
            ("commit_point", "Enter in S3 prefix"),
        ),
        (['  uid=1_1 button "Save" focusable'], "enter", None),
        (
            ['  uid=1_1 menuitem "Sign out" focusable focused'],
            "enter",
            ("session_end", "Sign out"),
        ),
    ],
)
def test_a_key_is_judged_by_the_element_that_has_focus(
    lines: list[str], key: str, expected: tuple[str, str] | None
) -> None:
    assert judged('uid=1_0 RootWebArea "x"', *lines).pressed(key) == expected


@pytest.mark.unit
def test_typing_is_judged_by_the_keys_it_presses() -> None:
    assert story_relay.typed_keys({"text": "x"}) == []
    assert story_relay.typed_keys({"text": "x\ny"}) == ["enter"]
    assert story_relay.typed_keys({"text": "x", "submitKey": "Tab"}) == ["tab"]
    assert story_relay.typed_keys({"text": "x", "submitKey": "Enter"}) == ["enter"]
    assert story_relay.typed_keys({"text": " "}) == [" "]
    assert story_relay.typed_keys({"text": "lending package\n"}) == ["enter", " "]
    assert story_relay.typed_keys({"text": "a\u00a0b\tc"}) == []


@dataclass(frozen=True)
class Shipped:
    """One shipped control that sends a mutation, as the guard is shown it.

    ``file`` is under ``src/ui/src`` and must hold ``source`` (the label itself
    unless the label is built at run time). ``kind`` is how the control is used: a
    ``click`` on a control named ``label``, ``enter`` in the text box named
    ``label`` (``multiline`` when it is a text area), a click on the ``unnamed``
    button beside a text box, or an ``autosave``: an edit on a page whose editor
    saves by itself, which shows a control named ``label``. A click control is
    shown with ``role``: by default, in a module the side navigation is drawn from
    (:func:`side_navigation_modules`), as its link to another page, which is how
    the side navigation shows one, and anywhere else as a button.
    """

    file: str
    label: str
    source: str | None = None
    inside_dialog: bool = False
    kind: str = "click"
    multiline: bool = False
    role: str | None = None


_AGENT_CHAT = "components/agent-chat/AgentChatLayout.tsx"
_ANNOTATE = "components/test-studio/AnnotationWorkspace.tsx"
_ASSIGN = "components/review-queues/AssignModal.tsx"
_BREAKER = "components/common/CircuitBreakerPanel.tsx"
_CONFIG = "components/configuration-layout/ConfigurationLayout.tsx"
_EDITOR = "components/review-workspace/SectionEditor.tsx"
_FRAME = "components/review-workspace/AnnotationFrame.tsx"
_INBOX = "components/inbox/InboxMessagesTable.tsx"
_MAPPINGS = "components/config-prefix-mappings/ConfigPrefixMappingsLayout.tsx"
_PRODUCTION = "components/review-workspace/ProductionAnnotation.tsx"
_QUEUE = "components/review-queues/ReviewQueuePage.tsx"
_QUEUE_SETTINGS = "components/review-queues/QueueDefinitionModal.tsx"
_REVISIONS = "components/configuration-layout/ConfigRevisionHistoryPanel.tsx"
_SUPERVISOR = "components/review-queues/SupervisorModals.tsx"
_TABLE = "components/document-list/documents-table-config.tsx"
_TEST_SET_ITEM = "components/review-workspace/TestSetAnnotation.tsx"
_UPLOAD = "components/upload-document/UploadDocumentPanel.tsx"
_WIZARD = "components/test-studio/CreateTestSetWizard.tsx"

#: Every mutation the web UI declares, with the shipped controls that send it. The
#: suite derives the declared set from ``src/ui/src`` and fails on a mutation that
#: is in neither this table nor :data:`MUTATIONS_WITHOUT_A_GUARDED_CONTROL`, and on
#: an entry whose mutation is gone, so a new mutation reaches this table as an
#: unaccounted one; every control listed must still be in its file and refused.
MUTATION_CONTROLS: dict[str, tuple[Shipped, ...]] = {
    "AbortTestRuns": (Shipped("components/test-studio/AbortTestModal.tsx", "Abort"),),
    "AbortWorkflow": (
        Shipped("components/common/AbortWorkflowModal.tsx", "Abort"),
        Shipped(_TABLE, "Abort"),
    ),
    "AddDocumentsToTestSet": (
        Shipped(
            "components/test-studio/AddDocumentsModals.tsx",
            "Add Documents",
            inside_dialog=True,
        ),
    ),
    "AddDocumentsToTestSetByKey": (
        Shipped(
            "components/test-studio/AddProcessedDocumentsModal.tsx",
            "Add 2 documents",
            source="`Add ${selectedCount}",
            inside_dialog=True,
        ),
    ),
    "AddDocumentsToTestSetFromUpload": (
        Shipped(
            "components/test-studio/AddDocumentsModals.tsx", "Upload and Add Documents"
        ),
    ),
    "AddTestSet": (Shipped(_WIZARD, "Create", source="'Create'", inside_dialog=True),),
    "AddTestSetFromUpload": (
        Shipped(_WIZARD, "Create", source="'Create'", inside_dialog=True),
    ),
    "AssignTestSetDocuments": (Shipped(_ASSIGN, "Assign", inside_dialog=True),),
    "AutoDetectSections": (
        Shipped(
            "components/discovery/PdfPageSelector.tsx", "\u2728 Auto-detect sections"
        ),
    ),
    "ClaimNextReviewItem": (Shipped(_QUEUE, "Next item"),),
    "ClaimReview": (
        Shipped(_TABLE, "Start Review"),
        Shipped("components/document-panel/DocumentPanel.tsx", "Start Review"),
    ),
    "ClaimReviewItem": (
        Shipped(_QUEUE, "Claim (2)", source="counted('Claim'"),
        Shipped(_QUEUE, "Claim", source="text: 'Claim'"),
        Shipped(_FRAME, "Claim"),
        Shipped(_FRAME, "Complete"),
        Shipped(_PRODUCTION, "Save draft", source='saveButtonText="Save draft"'),
        Shipped(
            _PRODUCTION, "Save draft", source="autosaveMs={canEdit", kind="autosave"
        ),
    ),
    "ClaimTestSetDocument": (
        Shipped(_ANNOTATE, "Claim (2)", source="`Claim (${claimable.length})`"),
        Shipped(_ANNOTATE, "Claim", source="text: 'Claim'"),
        Shipped(_FRAME, "Claim"),
    ),
    "ClearDraftLabels": (
        Shipped("components/test-studio/TestSetDetail.tsx", "Delete draft labels"),
    ),
    "ClearReadInboxMessages": (Shipped(_INBOX, "Clear read", inside_dialog=True),),
    "CompleteReviewItem": (Shipped(_FRAME, "Complete"),),
    "CompleteSectionReview": (
        Shipped("components/document-viewer/VisualEditorModal.tsx", "Save All Changes"),
        Shipped(_FRAME, "Complete"),
        Shipped(_TEST_SET_ITEM, "Save labels", source='saveButtonText="Save labels"'),
    ),
    "CopyToBaseline": (
        Shipped(
            "components/document-viewers/DocumentViewers.tsx",
            "Use as Evaluation Baseline",
        ),
    ),
    "CreateEmptyTestSet": (
        Shipped(_WIZARD, "Create", source="'Create'", inside_dialog=True),
    ),
    "CreateFinetuningJob": (
        Shipped(
            "components/custom-models/CustomModelsLayout.tsx",
            "Create Job",
            inside_dialog=True,
        ),
    ),
    "CreateReviewQueue": (
        Shipped(_QUEUE_SETTINGS, "Create", source="'Create'", inside_dialog=True),
    ),
    "CreateUser": (
        Shipped(
            "components/user-management/UserManagementLayout.tsx",
            "Create User",
            inside_dialog=True,
        ),
    ),
    "DeleteAgentJob": (
        Shipped(
            "components/document-agents-layout/AgentQueryInput.tsx",
            "Delete query: What is in this document?",
            source="`Delete query: ${displayText}`",
        ),
    ),
    "DeleteChatSession": (
        Shipped(
            "components/agent-chat/AgentChatHistoryDropdown.tsx",
            "Delete chat session: Where do I start?",
            source="`Delete chat session: ${displayText}`",
        ),
    ),
    "DeleteConfigPrefixMapping": (Shipped(_MAPPINGS, "Delete"),),
    "DeleteConfigProfileRevision": (Shipped(_REVISIONS, "Delete"),),
    "DeleteConfigVersion": (
        Shipped(
            "components/configuration-layout/ConfigurationVersionsTable.tsx", "Delete"
        ),
    ),
    "DeleteDiscoveryJob": (
        Shipped(
            "components/discovery/DiscoveryPanel.tsx", "Delete selected discovery jobs"
        ),
        Shipped(
            "components/discovery/MultiDocDiscoveryPanel.tsx",
            "Delete selected discovery jobs",
        ),
    ),
    "DeleteDocument": (
        Shipped("components/common/DeleteDocumentModal.tsx", "Delete"),
        Shipped(_TABLE, "Delete"),
    ),
    "DeleteDocumentVersion": (
        Shipped("components/document-panel/DocumentVersionsPanel.tsx", "Delete"),
    ),
    "DeleteFinetuningJob": (
        Shipped("components/custom-models/CustomModelsLayout.tsx", "Delete"),
        Shipped("components/custom-models/FinetuningJobDetail.tsx", "Delete"),
    ),
    "DeleteInboxMessages": (
        Shipped(
            "components/inbox/DeleteInboxMessagesModal.tsx",
            "Delete",
            inside_dialog=True,
        ),
    ),
    "DeleteReviewQueue": (
        Shipped(
            "components/review-queues/QueuesPage.tsx", "Delete", inside_dialog=True
        ),
    ),
    "DeleteTestSets": (Shipped("components/test-studio/TestSets.tsx", "Delete"),),
    "DeleteTests": (Shipped("components/test-studio/DeleteTestModal.tsx", "Delete"),),
    "DeleteUser": (
        Shipped("components/user-management/UserManagementLayout.tsx", "Delete"),
    ),
    "ForceReleaseReviewItem": (
        Shipped(_SUPERVISOR, "Unassign", source='label="Unassign"', inside_dialog=True),
    ),
    "GenerateDraftLabels": (
        Shipped(
            "components/test-studio/GenerateDraftLabelsModal.tsx",
            "Label 3 document(s)",
            source="`Label ${targetCount} document(s)`",
        ),
        Shipped(
            "components/test-studio/GenerateDraftLabelsModal.tsx",
            "Label every document that needs it",
        ),
    ),
    "GenerateRuleJson": (
        Shipped(
            "components/json-schema-builder/SchemaInspector.tsx", "Generate RuleJSON"
        ),
        Shipped("components/json-schema-builder/SchemaInspector.tsx", "Regenerate"),
    ),
    "LabelConfigProfileRevision": (Shipped(_REVISIONS, "Save"),),
    "MarkAllInboxMessagesRead": (
        Shipped(_INBOX, "Mark all as read"),
        Shipped("components/inbox/inbox-bell.tsx", "Mark all as read"),
    ),
    # Opening an unread message sends this too, from InboxMessageModal.tsx and from
    # the Inbox bell's recent messages in inbox-bell.tsx, and the guard lets that
    # through: no control's name says it marks anything, and all it changes is
    # whether the signed-in persona's own copy of the message reads as read. A bell
    # item is judged by its message's title, as any control is by its name.
    "MarkInboxMessages": (
        Shipped(_INBOX, "Mark as read"),
        Shipped(_INBOX, "Mark as unread"),
    ),
    "MoveReviewItem": (
        Shipped(_SUPERVISOR, "Move", source='label="Move"', inside_dialog=True),
    ),
    "OpenTestSetAnnotationDraft": (
        Shipped(_TEST_SET_ITEM, "Start annotating"),
        Shipped(_FRAME, "Complete"),
    ),
    "PauseCircuitBreaker": (
        Shipped(_BREAKER, "Pause processing"),
        Shipped(_BREAKER, "Pause", source="confirmLabel: 'Pause'"),
    ),
    "ProbeCircuitBreaker": (
        Shipped(_BREAKER, "Probe recovery"),
        Shipped(_BREAKER, "Probe", source="confirmLabel: 'Probe'"),
    ),
    "ProcessChanges": (
        Shipped("components/sections-panel/SectionsPanel.tsx", "Confirm & Process"),
        Shipped("components/pages-panel/PagesPanel.tsx", "Confirm & Process"),
    ),
    "PublishTestSetVersion": (
        Shipped("components/test-studio/PublishVersionModal.tsx", "Publish version"),
    ),
    "PutConfigPrefixMapping": (Shipped(_MAPPINGS, "Save"),),
    "ReassignReviewItem": (Shipped(_ASSIGN, "Assign", inside_dialog=True),),
    "ReextractTestSetDocument": (
        Shipped(
            "components/test-studio/GroundTruthVisualEditor.tsx",
            "Change class & re-extract",
        ),
    ),
    "ReleaseReview": (Shipped(_TABLE, "Release Review"),),
    "ReleaseReviewItem": (Shipped(_FRAME, "Skip"),),
    "RemoveDocumentsFromTestSet": (
        Shipped(
            "components/test-studio/RemoveDocumentsModal.tsx",
            "Remove 2 documents",
            source="Remove {count} {noun}",
        ),
        Shipped(
            _ANNOTATE, "Delete", source='header="Delete document"', inside_dialog=True
        ),
    ),
    "RemoveReviewItem": (
        Shipped(_SUPERVISOR, "Remove", source='label="Remove"', inside_dialog=True),
    ),
    "ReprocessDocument": (
        Shipped("components/common/ReprocessDocumentModal.tsx", "Reprocess"),
        Shipped(_TABLE, "Reprocess"),
    ),
    "ResetTestSetLabels": (
        Shipped("components/test-studio/TestSetDetail.tsx", "Reset all labels"),
    ),
    "RestoreConfigProfileRevision": (Shipped(_REVISIONS, "Restore"),),
    "RestoreDefaultModelConfigLimits": (
        Shipped(
            "components/model-config-limits-layout/ModelConfigLimitsLayout.tsx",
            "Restore Defaults",
        ),
    ),
    "RestoreDefaultPricing": (
        Shipped("components/pricing-layout/PricingLayout.tsx", "Restore Defaults"),
    ),
    "ResumeCircuitBreaker": (
        Shipped(_BREAKER, "Resume processing"),
        Shipped(_BREAKER, "Resume", source="confirmLabel: 'Resume'"),
    ),
    "SaveReviewDraft": (
        Shipped(_PRODUCTION, "Save draft", source='saveButtonText="Save draft"'),
        Shipped(
            _PRODUCTION, "Save draft", source="autosaveMs={canEdit", kind="autosave"
        ),
    ),
    "SaveSectionReviewEdits": (
        Shipped("components/document-viewer/VisualEditorModal.tsx", "Save All Changes"),
    ),
    "SaveTestSetDocumentSection": (
        Shipped(_FRAME, "Complete"),
        Shipped(_TEST_SET_ITEM, "Save labels", source='saveButtonText="Save labels"'),
    ),
    "SendAgentChatMessage": (
        Shipped(
            _AGENT_CHAT,
            "Describe the documents you want to process",
            source="event.key === 'Enter'",
            kind="enter",
            multiline=True,
        ),
        Shipped(_AGENT_CHAT, "", source='actionButtonIconName="send"', kind="unnamed"),
    ),
    "SendChatDocumentMessage": (
        Shipped("components/chat-panel/ChatPanel.tsx", "Send"),
        Shipped(
            "components/chat-panel/ChatPanel.tsx",
            "Your message",
            source="e.key === 'Enter'",
            kind="enter",
        ),
    ),
    "SetActiveVersion": (
        Shipped(
            "components/configuration-layout/ConfigurationVersionsTable.tsx", "Activate"
        ),
        Shipped(_CONFIG, "Confirm Activate"),
    ),
    "SkipAllSectionsReview": (
        Shipped("components/sections-panel/SectionsPanel.tsx", "Skip All Reviews"),
    ),
    "SkipTestSetDocument": (Shipped(_FRAME, "Skip"),),
    "StartMultiDocDiscovery": (
        Shipped(
            "components/discovery/MultiDocDiscoveryPanel.tsx",
            "\U0001f50d Start Discovery",
        ),
    ),
    "StartTestRun": (
        Shipped("components/test-studio/TestRunner.tsx", "Run Test"),
        Shipped("components/test-studio/TestResults.tsx", "Re-Run Test"),
    ),
    "SubscribeFeature": (
        Shipped("components/feature-page/FeatureStateMessages.tsx", "Subscribe"),
    ),
    "SyncBdaIdp": (
        Shipped(_CONFIG, "Sync from BDA"),
        Shipped(_CONFIG, "Confirm Sync"),
    ),
    "TakeOverReviewItem": (Shipped(_PRODUCTION, "Take over"),),
    "UnassignTestSetDocuments": (
        Shipped(
            _ANNOTATE, "Unassign (2)", source="`Unassign (${unassignable.length})`"
        ),
        Shipped(_ANNOTATE, "Unassign", source="'Unassign'"),
    ),
    "UnsubscribeFeature": (
        Shipped(
            "components/feature-page/FeatureStateMessages.tsx", "Cancel Subscription"
        ),
    ),
    "UpdateConfiguration": (
        Shipped(_CONFIG, "Save changes"),
        Shipped(
            "components/discovery/DiscoveryJobDetails.tsx",
            "Enable several documents per section",
        ),
        Shipped(
            "components/custom-models/CreateConfigVersionModal.tsx",
            "Create profile",
            inside_dialog=True,
        ),
    ),
    "UpdateDocumentSections": (
        Shipped("components/sections-panel/SectionsPanel.tsx", "Save page grouping"),
    ),
    "UpdateInboxPreferences": (
        Shipped("components/inbox/InboxPreferences.tsx", "Save", inside_dialog=True),
    ),
    "UpdateModelConfigLimits": (
        Shipped(
            "components/model-config-limits-layout/ModelConfigLimitsLayout.tsx",
            "Save changes",
        ),
    ),
    "UpdatePricing": (
        Shipped("components/pricing-layout/PricingLayout.tsx", "Save changes"),
    ),
    "UpdateReviewQueue": (
        Shipped(_QUEUE_SETTINGS, "Save", source="'Save'", inside_dialog=True),
    ),
    "UpdateTestSet": (Shipped("components/test-studio/TestSets.tsx", "Save"),),
    "UpdateTestSetDocumentSections": (
        Shipped(
            "components/test-studio/GroundTruthVisualEditor.tsx", "Save page grouping"
        ),
    ),
    "UpdateTestSetQueue": (
        Shipped(_QUEUE_SETTINGS, "Save", source="'Save'", inside_dialog=True),
    ),
    "UpdateUser": (
        Shipped("components/user-management/UserManagementLayout.tsx", "Save Scope"),
    ),
    "UploadDiscoveryDocument": (
        Shipped("components/discovery/DiscoveryPanel.tsx", "Start Discovery"),
    ),
    "UploadDocument": (
        Shipped(_UPLOAD, "Upload (1 files)", source="`Upload ${"),
        Shipped("components/pages-panel/PageTextEditorModal.tsx", "Save"),
        Shipped("components/document-viewer/MarkdownJsonViewer.tsx", "Save Changes"),
        Shipped("components/document-viewer/VisualEditorModal.tsx", "Save All Changes"),
        Shipped(_EDITOR, "Save changes", source="'Save changes'"),
    ),
    "UploadMultiDocDiscoveryZip": (
        Shipped(
            "components/discovery/MultiDocDiscoveryPanel.tsx",
            "\U0001f50d Start Discovery",
        ),
    ),
    "UploadSampleDocument": (Shipped(_UPLOAD, "Process sample"),),
}
#: Mutations the UI declares that no shipped control sends, each with what makes
#: that so; the suite checks each one has no caller outside ``src/ui/src/graphql``.
MUTATIONS_WITHOUT_A_GUARDED_CONTROL: dict[str, str] = {
    "RejectReviewItem": (
        "declared among the UI's operations and called by none of its components "
        "or hooks"
    ),
    "RenewReviewLease": (
        "declared among the UI's operations and called by none of its components "
        "or hooks"
    ),
    "UpdateChatSessionTitle": (
        "declared among the UI's operations and called by none of its components "
        "or hooks"
    ),
}
_MUTATION_RE = re.compile(r"\bmutation\s+([A-Z][A-Za-z0-9]*)\s*[({]")
_COMMENT_RE = re.compile(r"/\*.*?\*/|(?<![:\w])//[^\n]*", re.S)
_QUOTED_RE = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"|`([^`\n]*)`")
_HANDLER_RE = re.compile(r"\bon[A-Z][A-Za-z]*\s*[=:?]")
_IMPORT_RE = re.compile(r"^import\b[^;]*;", re.M)
_RELATIVE_IMPORT_RE = re.compile(
    r"^import\s+(?!type\b)(?P<names>[^;]*?)\s+from\s+'(?P<path>\.[^']*)';", re.M
)
#: The web UI's labels for every page's layout, the side navigation's name among them.
LAYOUT_LABELS_SOURCE = "components/common/labels.ts"
#: Every event handler the side navigation's modules attach, each as its source
#: begins: SideNavigation's follow handler, which defaults to the module's own, and
#: the Quick Start button's, on a button the guard judges by its name.
SIDE_NAVIGATION_HANDLERS: tuple[str, ...] = (
    "onFollowHandler?: (ev: CustomEvent<SideNavigationProps.FollowDetail>) => void;",
    "onFollowHandler = defaultOnFollowHandler }",
    "onFollow={onFollowHandler}",
    "onClick={() => window.dispatchEvent(new CustomEvent('openQuickStart'))}",
)
#: What the module's own follow handler may call: it cancels a link to an item that
#: is not a page, such as the stack's name, and logs every other.
FOLLOW_HANDLER_CALLS = frozenset({"if", "includes", "preventDefault", "log"})


def _ui_files() -> list[Path]:
    return sorted(
        path
        for path in UI_SOURCE.rglob("*")
        if path.suffix in (".graphql", ".ts", ".tsx")
        and "__tests__" not in path.parts
        and ".test." not in path.name
        and "generated" not in path.relative_to(UI_SOURCE).parts
    )


def declared_mutations() -> set[str]:
    """Every mutation ``src/ui/src`` declares, outside its tests and generated code."""
    found: set[str] = set()
    for path in _ui_files():
        found |= set(_MUTATION_RE.findall(path.read_text(encoding="utf-8")))
    return found


def _without_comments(text: str) -> str:
    return _COMMENT_RE.sub(lambda found: re.sub(r"[^\n]", " ", found.group(0)), text)


@functools.cache
def side_navigation_modules() -> tuple[str, ...]:
    """Every module under ``src/ui/src`` the side navigation is drawn from: the ones
    that name SideNavigation or its types, which render it or build its items, and
    every first-party component one of those renders, and so on."""
    found = [
        str(path.relative_to(UI_SOURCE))
        for path in _ui_files()
        if "SideNavigation" in path.read_text(encoding="utf-8")
    ]
    for module in found:
        found.extend(sorted(_rendered_modules(module) - set(found)))
    return tuple(found)


def _rendered_modules(module: str) -> set[str]:
    """The first-party modules whose components ``module`` renders."""
    path = UI_SOURCE / module
    text = _without_comments(path.read_text(encoding="utf-8"))
    rendered: set[str] = set()
    for imported in _RELATIVE_IMPORT_RE.finditer(text):
        names = re.findall(r"\b[A-Z]\w*\b", imported.group("names"))
        if not any(
            re.search(rf"<{name}\b|createElement\(\s*{name}\b", text) for name in names
        ):
            continue
        target = Path(os.path.normpath(path.parent / imported.group("path")))
        for candidate in (
            target.parent / f"{target.name}.tsx",
            target.parent / f"{target.name}.ts",
            target / "index.tsx",
            target / "index.ts",
        ):
            if candidate.is_file():
                rendered.add(str(candidate.relative_to(UI_SOURCE)))
                break
    return rendered


def _shown_as(control: Shipped) -> tuple[story_relay.Snapshot, str]:
    in_navigation = control.file in side_navigation_modules()
    role = control.role or ("link" if in_navigation else "button")
    if control.kind == "click" and in_navigation:
        address = f' url="{STACK}/#/documents/upload"' if role == "link" else ""
        return judged(
            f'uid=1_0 RootWebArea "Page" url="{STACK}/#/documents"',
            '  uid=1_6 navigation "Side navigation"',
            f'    uid=9_1 {role} "{control.label}"{address}',
        ), "9_1"
    lines = ['uid=1_0 RootWebArea "Page"', "  uid=1_1 main"]
    indent = "    "
    if control.inside_dialog:
        lines.append('    uid=6_0 dialog "Dialog" modal')
        indent = "      "
    if control.kind == "click":
        lines.append(f'{indent}uid=9_1 {role} "{control.label}"')
        return story_relay.cut_snapshot(lines), "9_1"
    if control.kind == "autosave":
        lines.append(f'{indent}uid=9_0 button "{control.label}" disableable disabled')
        lines.append(f'{indent}uid=9_1 checkbox "Signed"')
        return story_relay.cut_snapshot(lines), "9_1"
    flags = "focusable focused" + (" multiline" if control.multiline else "")
    lines.append(f'{indent}uid=9_0 textbox "{control.label}" {flags}')
    if control.kind == "unnamed":
        lines.append(f"{indent}uid=9_1 button")
    return story_relay.cut_snapshot(lines), "9_1"


@pytest.mark.unit
def test_every_mutation_the_ui_declares_is_accounted_for() -> None:
    declared = declared_mutations()
    assert len(declared) >= len(MUTATION_CONTROLS)
    listed = set(MUTATION_CONTROLS) | set(MUTATIONS_WITHOUT_A_GUARDED_CONTROL)
    unaccounted = sorted(declared - listed)
    assert not unaccounted, (
        f"{unaccounted}: a mutation the web UI declares, and the commit guard has not "
        "been checked against the control that sends it. Add each to MUTATION_CONTROLS "
        "with that control, adding a verb to story_relay.COMMIT_VERBS if the guard lets "
        "it through, or to MUTATIONS_WITHOUT_A_GUARDED_CONTROL saying why no control "
        "sends it."
    )
    stale = sorted(listed - declared)
    assert not stale, f"{stale}: no longer declared in src/ui/src; delete the entries"
    assert not set(MUTATION_CONTROLS) & set(MUTATIONS_WITHOUT_A_GUARDED_CONTROL)


@pytest.mark.unit
@pytest.mark.parametrize("mutation", sorted(MUTATIONS_WITHOUT_A_GUARDED_CONTROL))
def test_a_mutation_without_a_control_is_sent_from_nowhere(mutation: str) -> None:
    called = mutation[0].lower() + mutation[1:]
    callers = [
        path.relative_to(UI_SOURCE)
        for path in _ui_files()
        if "graphql" not in path.relative_to(UI_SOURCE).parts
        and re.search(rf"\b{called}\b", path.read_text(encoding="utf-8"))
    ]
    assert not callers, f"{mutation} is sent from {callers}; list that control"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutation", "control"),
    [
        (mutation, control)
        for mutation, controls in MUTATION_CONTROLS.items()
        for control in controls
    ],
    ids=lambda value: value if isinstance(value, str) else value.label or value.kind,
)
def test_the_guard_refuses_each_shipped_control_that_sends_a_mutation(
    mutation: str, control: Shipped
) -> None:
    text = (UI_SOURCE / control.file).read_text(encoding="utf-8")
    assert (control.source or control.label) in text, (
        f"{control.file} no longer holds {control.source or control.label!r}, the "
        f"control that sends {mutation}; find it again and update MUTATION_CONTROLS"
    )
    snapshot, uid = _shown_as(control)
    if control.kind == "enter":
        assert snapshot.pressed("enter") is not None, (mutation, control)
    elif control.kind == "autosave":
        assert snapshot.autosave_control == control.label, (mutation, control)
        assert snapshot.sets_a_value(uid) == ("commit_point", control.label)
    elif control.file in side_navigation_modules():
        assert snapshot.control_of(uid) is not None, (
            f"{control.file} builds the side navigation, whose links to another page "
            f"the guard follows whatever they are called, so it cannot refuse "
            f"{control.label!r}, which sends {mutation}: a control that sends a "
            "mutation cannot be a side navigation link"
        )
    else:
        assert snapshot.control_of(uid) is not None, (mutation, control)


@pytest.mark.unit
def test_a_control_mapped_to_the_side_navigation_is_shown_as_the_link_it_is() -> None:
    """The closure above would fail for a mutation sent from the side navigation,
    because it shows that control as the followed link it is, not as a button."""
    snapshot, uid = _shown_as(Shipped(NAVIGATION_SOURCE, "Upload Documents"))
    assert snapshot.followed_link(index_of(snapshot, uid)) is not None
    assert snapshot.control_of(uid) is None
    for control in (
        Shipped(NAVIGATION_SOURCE, "Upload Documents", role="button"),
        Shipped(_UPLOAD, "Upload Documents"),
    ):
        snapshot, uid = _shown_as(control)
        assert snapshot.control_of(uid) == ("commit_point", "Upload Documents")


@pytest.mark.unit
def test_the_side_navigation_is_drawn_from_its_builders_and_what_they_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A component the side navigation's modules render is drawn inside the side
    navigation too, so it is read with them, and so is one it renders: a link it
    holds is a side navigation link. A hook they call draws nothing there."""
    tree = tmp_path / "src"
    (tree / "components" / "nav").mkdir(parents=True)
    (tree / "components" / "nav" / "navigation.tsx").write_text(
        "import { SideNavigation } from '@cloudscape-design/components';\n"
        "import Badge from './Badge';\n"
        "import useThing from './use-thing';\n"
        "export const Nav = () => <SideNavigation items={[{ info: <Badge /> }]} />;\n",
        encoding="utf-8",
    )
    (tree / "components" / "nav" / "Badge.tsx").write_text(
        "import Inner from './inner';\nexport default () => <Inner />;\n",
        encoding="utf-8",
    )
    (tree / "components" / "nav" / "inner").mkdir()
    (tree / "components" / "nav" / "inner" / "index.tsx").write_text(
        "export default () => null;\n", encoding="utf-8"
    )
    (tree / "components" / "nav" / "use-thing.ts").write_text(
        "export default () => null;\n", encoding="utf-8"
    )
    monkeypatch.setattr(sys.modules[__name__], "UI_SOURCE", tree)
    side_navigation_modules.cache_clear()
    try:
        assert side_navigation_modules() == (
            "components/nav/navigation.tsx",
            "components/nav/Badge.tsx",
            "components/nav/inner/index.tsx",
        )
    finally:
        side_navigation_modules.cache_clear()


@pytest.mark.unit
def test_the_side_navigation_is_the_one_landmark_the_web_ui_names_so() -> None:
    """Every page's layout takes its side navigation's name from one label, which is
    the name the guard follows links in, and holds the one shared navigation there;
    no other text in the web UI is that name, so no other landmark can carry it."""
    labels = (UI_SOURCE / LAYOUT_LABELS_SOURCE).read_text(encoding="utf-8")
    label = re.search(r"^\s*navigation: '([^']*)',$", labels, re.M)
    assert label is not None, labels
    assert story_relay.plain(label.group(1)) in story_relay.SIDE_NAVIGATION_NAMES
    named = [
        str(path.relative_to(UI_SOURCE))
        for path in _ui_files()
        if any(
            story_relay.plain("".join(found)) in story_relay.SIDE_NAVIGATION_NAMES
            for found in _QUOTED_RE.findall(
                _without_comments(path.read_text(encoding="utf-8"))
            )
        )
    ]
    assert named == [LAYOUT_LABELS_SOURCE], named
    shared = (UI_SOURCE / NAVIGATION_SOURCE).with_suffix("")
    layouts = 0
    for path in _ui_files():
        if path.relative_to(UI_SOURCE) == Path(LAYOUT_LABELS_SOURCE):
            continue
        text = _IMPORT_RE.sub(
            lambda found: re.sub(r"[^\n]", " ", found.group(0)),
            _without_comments(path.read_text(encoding="utf-8")),
        )
        given = len(re.findall(r"\bappLayoutLabels\b", text))
        if not given:
            continue
        assert text.count("ariaLabels={appLayoutLabels}") == given, path
        mounted = text.count("navigation={<Navigation />}")
        assert len(re.findall(r"\bnavigation=\{", text)) == mounted >= given, path
        source = re.search(
            r"^import Navigation from '([^']+)';$",
            path.read_text(encoding="utf-8"),
            re.M,
        )
        assert source is not None, path
        resolved = Path(os.path.normpath(path.parent / source.group(1)))
        assert resolved == shared, (path, source.group(1))
        layouts += given
    pages = sum(
        len(re.findall(r"<AppLayout\b", _without_comments(path.read_text("utf-8"))))
        for path in _ui_files()
    )
    assert layouts == pages > 0, (layouts, pages)


@pytest.mark.unit
def test_a_click_on_a_side_navigation_link_runs_only_its_follow_handler() -> None:
    """What makes following a side navigation link safe, read from the web UI's
    source. The modules the side navigation is drawn from
    (:func:`side_navigation_modules`) attach no event handler but SideNavigation's
    follow handler and the Quick Start button's; the follow handler is the
    module's own, which no page replaces and which only cancels a link to an item
    that is not a page and logs; and none of those modules names a mutation the
    web UI declares. A listener a page adds to the whole document runs on every
    click the guard lets through, not only on these, and is not read here."""
    modules = side_navigation_modules()
    assert NAVIGATION_SOURCE in modules, modules
    mutations = declared_mutations()
    for module in modules:
        text = _without_comments((UI_SOURCE / module).read_text(encoding="utf-8"))
        assert "addEventListener" not in text, module
        for found in _HANDLER_RE.finditer(text):
            line = text[found.start() :].split("\n", 1)[0]
            assert line.startswith(SIDE_NAVIGATION_HANDLERS), (
                f"{module} attaches {line!r}. The guard follows a side navigation "
                "link to another page whatever it is called, so nothing in the side "
                "navigation may send anything when it is clicked: check that this "
                "handler sends nothing, then add it to SIDE_NAVIGATION_HANDLERS"
            )
        named = sorted(
            mutation
            for mutation in mutations
            if re.search(
                rf"\b(?:{mutation}|{mutation[0].lower()}{mutation[1:]})\b", text
            )
        )
        assert not named, f"{module} names {named}, which the side navigation sends"
    navigation = _without_comments(
        (UI_SOURCE / NAVIGATION_SOURCE).read_text(encoding="utf-8")
    )
    assert navigation.count("onFollowHandler = defaultOnFollowHandler }") == 2
    handler = re.search(
        r"^const defaultOnFollowHandler = \(ev: CustomEvent<SideNavigationProps"
        r"\.FollowDetail>\): void => \{\n(?P<body>.*?)\n\};$",
        navigation,
        re.M | re.S,
    )
    assert handler is not None, NAVIGATION_SOURCE
    calls = set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", handler.group("body")))
    assert calls <= FOLLOW_HANDLER_CALLS, calls
    replaced = [
        str(path.relative_to(UI_SOURCE))
        for path in _ui_files()
        if re.search(r"\bonFollowHandler\s*=\s*\{", path.read_text(encoding="utf-8"))
    ]
    assert not replaced, replaced


@pytest.mark.unit
def test_the_recorded_sign_in_form_is_signed_out_and_no_other_recorded_page_is() -> (
    None
):
    assert snapshot_of("snapshot_signin").signed_out
    for label in (
        "snapshot_account",
        "snapshot_documents",
        "snapshot_dialog_open",
        "snapshot_dialogs",
        "snapshot_long",
        "click_user_menu",
    ):
        assert not snapshot_of(label).signed_out, label


@pytest.mark.unit
@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (
            [
                'uid=1_0 RootWebArea "x"',
                "  uid=1_1 form",
                '    uid=1_2 textbox "Password *"',
                '    uid=1_3 button "Sign in with ExampleIdP"',
            ],
            True,
        ),
        (
            [
                'uid=1_0 RootWebArea "x"',
                "  uid=1_1 form",
                '    uid=1_2 textbox "Password"',
                "  uid=1_3 form",
                '    uid=1_4 button "Sign in"',
            ],
            False,
        ),
        (
            [
                'uid=1_0 RootWebArea "x"',
                '  uid=1_1 textbox "Password"',
                '  uid=1_2 link "Sign in"',
            ],
            False,
        ),
        (
            [
                'uid=1_0 RootWebArea "x"',
                '  uid=1_1 textbox "Password"',
                '  uid=1_2 button "Log in"',
            ],
            True,
        ),
        (
            [
                'uid=1_0 RootWebArea "x"',
                '  uid=1_1 textbox "Passwords"',
                '  uid=1_2 button "Sign in"',
            ],
            False,
        ),
    ],
    ids=["same-form", "two-forms", "a-link", "no-form", "another-field"],
)
def test_signed_out_needs_a_password_field_beside_a_sign_in_button(
    lines: list[str], expected: bool
) -> None:
    assert story_relay.cut_snapshot(lines).signed_out is expected


@pytest.mark.unit
def test_the_open_dialog_is_read_from_both_recorded_shapes() -> None:
    assert story_relay.open_dialog([text_of("click_discard")]) == (
        "confirm",
        "Discard the changes you made to this document?",
    )
    assert story_relay.open_dialog([text_of("snapshot_while_confirm_open")]) == (
        "confirm",
        "Discard the changes you made to this document?",
    )
    assert story_relay.open_dialog([text_of("click_rename")]) == ("prompt", "New name")
    assert story_relay.open_dialog([text_of("click_summary")]) == (
        "alert",
        "Processing finished in 4 steps.",
    )
    assert story_relay.open_dialog([text_of("handle_confirm_dismiss")]) is None


@pytest.mark.unit
def test_the_child_starts_under_env_minus_i_exactly_as_the_expert_session_starts_it(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    boot = rig.boot()
    root = rig.out / "devtools-root"
    assert boot["argv"] == [*agent.DEVTOOLS_FLAGS, f"--filesystemRoot={root}"]
    environ = {k: v for k, v in boot["environ"].items() if k not in PLATFORM_VARIABLES}
    assert environ == {
        "PATH": agent.DEFAULT_SEARCH_PATH,
        "HOME": str(rig.out.parent / "home"),
        "LANG": "C.UTF-8",
        "CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS": "1",
    }
    assert Path(boot["cwd"]).resolve() == root.resolve()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((rig.out / "screens").stat().st_mode) == 0o700
    expert = agent.mcp_servers(
        toolchain=paths.Toolchain(Path("/x")),
        recorder_adapter=Path("/r"),
        recorder_out=Path("/o"),
        python="python3",
        home=Path("/home/h"),
        fs_root=Path("/fs"),
    )[DEVTOOLS]
    assert story_relay.devtools_argv(
        paths.Toolchain(Path("/x")).devtools_mcp,
        home=Path("/home/h"),
        fs_root=Path("/fs"),
        search_path=agent.DEFAULT_SEARCH_PATH,
    ) == [expert["command"], *expert["args"]]


@pytest.mark.unit
def test_the_child_is_initialized_with_no_roots_and_listed_once(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.call("take_snapshot")
    rig.call("click", uid="1_16")
    received = rig.received()
    assert received[0]["method"] == "initialize"
    assert received[0]["params"] == {
        "protocolVersion": story_relay.PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "ux-story", "version": "1"},
    }
    assert received[1] == {"jsonrpc": "2.0", "method": "notifications/initialized"}
    methods = [message.get("method") for message in received]
    assert methods.count("tools/list") == 1
    assert [tool["name"] for tool in rig.relay.tools()] == list(
        tool_profiles.STORY_BROWSER_TOOLS
    )


@pytest.mark.unit
def test_the_child_s_ping_is_answered_its_other_requests_refused_its_notices_dropped(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(
        {
            "after_initialized": [
                {"jsonrpc": "2.0", "id": "p1", "method": "ping"},
                {"jsonrpc": "2.0", "id": "r1", "method": "roots/list"},
                {
                    "jsonrpc": "2.0",
                    "id": "s1",
                    "method": "sampling/createMessage",
                    "params": {},
                },
                {
                    "jsonrpc": "2.0",
                    "id": "e1",
                    "method": "elicitation/create",
                    "params": {},
                },
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/message",
                    "params": {"data": "x"},
                },
                {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"},
            ],
            "calls": {
                "take_snapshot": [
                    {
                        "label": "snapshot_documents",
                        "send_first": [
                            {"jsonrpc": "2.0", "id": "p2", "method": "ping"}
                        ],
                    }
                ]
            },
        }
    )
    reply = rig.call("take_snapshot")
    assert not reply.is_error
    wait_until(lambda: len([m for m in rig.received() if "method" not in m]) >= 5)
    answers = {m["id"]: m for m in rig.received() if "method" not in m}
    assert answers["p1"]["result"] == {} and answers["p2"]["result"] == {}
    for refused in ("r1", "s1", "e1"):
        assert answers[refused]["error"]["code"] == -32601, refused


@pytest.mark.unit
def test_stripped_arguments_never_reach_the_child_and_screenshots_are_jpeg_at_70(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.relay.call(
        "take_screenshot",
        {"fullPage": True, "filePath": "/etc/x.png", "format": "png", "quality": 100},
    )
    rig.relay.call("take_snapshot", {"filePath": "/etc/x.txt", "verbose": False})
    rig.relay.call("click", {"uid": "1_16", "bogus": 1})
    calls = rig.tool_calls()
    assert calls[0] == {
        "name": "take_screenshot",
        "arguments": {"format": "jpeg", "quality": 70},
    }
    assert calls[1] == {"name": "take_snapshot", "arguments": {"verbose": False}}
    assert calls[2] == {"name": "click", "arguments": {"uid": "1_16"}}
    assert calls[3] == {
        "name": "take_screenshot",
        "arguments": {"format": "jpeg", "quality": 70},
    }


@pytest.mark.unit
def test_every_action_counts_and_no_look_does(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.call("take_snapshot")
    rig.call("click", uid="1_16")
    rig.call("hover", uid="1_17")
    rig.call("wait_for", text=["Refresh"])
    rig.call("navigate_page", type="reload")
    lines = rig.lines()
    assert [
        (line["tool"], line["class"], line["counted"], line["actions_used"])
        for line in lines
    ] == [
        ("take_snapshot", "look", False, 0),
        ("click", "action", True, 1),
        ("hover", "action", True, 2),
        ("wait_for", "look", False, 2),
        ("navigate_page", "action", True, 3),
    ]
    assert rig.ledger.looks == 2


@pytest.mark.unit
def test_a_screenshot_follows_each_action_saved_private_and_attached(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.call("take_snapshot")
    reply = rig.call("click", uid="1_16")
    (image,) = images_in(reply)
    assert image["mimeType"] == "image/jpeg"
    assert reply_text(reply) == "Successfully clicked on the element"
    line = rig.last()
    assert (line["result"]["screen"], line["images_sent"], line["ok"]) == (
        "screens/001.jpg",
        1,
        True,
    )
    saved = rig.out / "screens" / "001.jpg"
    assert saved.read_bytes() == base64.b64decode(image["data"])
    assert saved.read_bytes().startswith(JPEG_MAGIC)
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600


@pytest.mark.unit
def test_no_screenshot_follows_an_action_the_browser_refused(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig({"calls": {"click": [{"label": "click_stale_uid"}]}})
    rig.call("take_snapshot")
    reply = rig.call("click", uid="1_16")
    assert reply.is_error and not images_in(reply)
    line = rig.last()
    assert (line["ok"], line["counted"], line["result"]["screen"]) == (
        False,
        True,
        None,
    )
    assert [call["name"] for call in rig.tool_calls()] == ["take_snapshot", "click"]


@pytest.mark.unit
def test_at_the_budget_actions_get_one_out_of_steps_marker_and_looks_go_on(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(budget=2)
    rig.call("take_snapshot")
    rig.call("click", uid="1_16")
    rig.call("click", uid="1_16")
    spent = rig.call("click", uid="1_16")
    again = rig.call("fill", uid="1_24", value="x")
    look = rig.call("take_snapshot")
    assert reply_text(spent) == story_relay.OUT_OF_STEPS
    assert spent.mark == {"label": "Out of steps", "say": story_relay.OUT_OF_STEPS}
    assert again.mark is None and reply_text(again) == story_relay.OUT_OF_STEPS
    assert not look.is_error
    lines = rig.lines()
    assert [(line["class"], line["refused"], line["label"]) for line in lines[3:5]] == [
        ("budget", "budget", "out_of_steps"),
        ("action", "budget", None),
    ]
    assert all(line["actions_used"] == 2 for line in lines[2:])
    assert [call["name"] for call in rig.tool_calls()].count("click") == 2


@pytest.mark.unit
def test_looks_stop_at_twice_the_budget(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(budget=1)
    assert not rig.call("take_snapshot").is_error
    assert not rig.call("wait_for", text=["Refresh"]).is_error
    refused = rig.call("take_snapshot")
    assert reply_text(refused) == story_relay.NO_LOOKS_TEXT
    assert (rig.last()["class"], rig.last()["refused"]) == ("look", "limit")


@pytest.mark.unit
def test_images_stop_at_eighteen_while_screens_are_still_saved(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(budget=30)
    rig.call("take_snapshot")
    for _ in range(story_relay.IMAGE_CAP):
        assert images_in(rig.call("click", uid="1_16"))
    past = rig.call("click", uid="1_16")
    assert not images_in(past) and not past.is_error
    assert rig.last()["result"]["screen"] == "screens/019.jpg"
    assert rig.last()["images_sent"] == story_relay.IMAGE_CAP
    refused = rig.call("take_screenshot")
    assert reply_text(refused) == story_relay.NO_SCREENSHOTS_TEXT
    assert (rig.last()["class"], rig.last()["refused"]) == ("look", "image_cap")
    assert len(list((rig.out / "screens").iterdir())) == 19


@pytest.mark.unit
def test_the_model_s_own_screenshot_is_saved_shown_and_counted(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    reply = rig.call("take_screenshot", uid="1_16")
    assert len(images_in(reply)) == 1
    line = rig.last()
    assert (line["class"], line["images_sent"], line["result"]["screen"]) == (
        "look",
        1,
        "screens/001.jpg",
    )
    assert line["args"] == {"uid": "1_16"}


@pytest.mark.unit
def test_screens_stop_at_the_budget_plus_forty(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(story_relay, "SCREENS_OVER_BUDGET", 1)
    rig = make_rig(budget=2)
    rig.call("take_snapshot")
    rig.call("take_screenshot")
    rig.call("click", uid="1_16")
    rig.call("click", uid="1_16")
    past = rig.call("take_screenshot")
    screens = [line["result"]["screen"] for line in rig.lines()]
    assert screens == [
        None,
        "screens/001.jpg",
        "screens/002.jpg",
        "screens/003.jpg",
        None,
    ]
    assert images_in(past) and not past.is_error


@pytest.mark.unit
def test_a_screenshot_over_its_size_bound_is_neither_saved_nor_shown(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(
        {
            "calls": {
                "take_screenshot": [
                    {
                        "label": "screenshot_jpeg",
                        "image_bytes": story_relay.SCREEN_BYTES + 1,
                    }
                ]
            }
        }
    )
    reply = rig.call("take_screenshot")
    assert reply.is_error and not images_in(reply)
    assert story_relay.SCREENSHOT_UNUSABLE_TEXT in reply_text(reply)
    assert (rig.last()["ok"], rig.last()["result"]["screen"]) == (False, None)
    assert not list((rig.out / "screens").iterdir())


@pytest.mark.unit
def test_a_snapshot_in_any_answer_is_cut_and_becomes_the_latest(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(
        {
            "calls": {
                "take_snapshot": [{"label": "snapshot_long"}],
                "click": [{"label": "click_refresh_with_snapshot"}],
            }
        }
    )
    reply = rig.call("take_snapshot")
    assert story_relay.CUT_TEXT.format(shown=220, total=606) in reply_text(reply)
    assert rig.relay.snapshot is not None and rig.relay.snapshot.total == 606
    refused = rig.call("click", uid="1_16")
    assert reply_text(refused) == story_relay.STALE_UID_TEXT
    rig.relay.snapshot = snapshot_of("snapshot_documents")
    clicked = rig.call("click", uid="1_16", includeSnapshot=True)
    assert reply_text(clicked).startswith(
        "Successfully clicked on the element\n## Latest"
    )
    assert rig.relay.snapshot.total == 43


@pytest.mark.unit
def test_a_click_on_a_commit_control_never_reaches_the_child(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(scenario_snapshot("snapshot_dialog_open"))
    rig.call("take_snapshot")
    reply = rig.call("click", uid=named("snapshot_dialog_open", "button", "Save"))
    assert reply.is_error and reply_text(reply) == story_relay.COMMIT_POINT_TEXT
    line = rig.last()
    assert (line["class"], line["refused"], line["label"], line["counted"]) == (
        "guard",
        "commit_point",
        "Save",
        False,
    )
    assert [call["name"] for call in rig.tool_calls()] == ["take_snapshot"]


@pytest.mark.unit
def test_a_control_relabelled_in_place_is_judged_by_its_new_name(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(
        {
            "calls": {
                "take_snapshot": [
                    page(*(line.format(label="Next") for line in WIZARD_STEP)),
                    page(*(line.format(label="Create") for line in WIZARD_STEP)),
                ]
            }
        }
    )
    rig.call("take_snapshot")
    assert not rig.call("click", uid="6_3").is_error
    refused = rig.call("click", uid="6_3")
    assert reply_text(refused) == story_relay.COMMIT_POINT_TEXT
    assert (rig.last()["class"], rig.last()["refused"], rig.last()["label"]) == (
        "guard",
        "commit_point",
        "Create",
    )
    assert [call["name"] for call in rig.tool_calls()] == [
        "take_snapshot",
        "click",
        "take_screenshot",
        "take_snapshot",
    ]
    assert [line["tool"] for line in rig.lines()] == [
        "take_snapshot",
        "click",
        "click",
    ]


@pytest.mark.unit
def test_the_page_is_read_again_only_when_a_call_ran_since_the_latest_snapshot(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.call("take_snapshot")
    rig.call("take_screenshot")
    rig.call("click", uid="1_16")
    rig.call("take_snapshot")
    rig.call("click", uid="1_16")
    assert [call["name"] for call in rig.tool_calls()] == [
        "take_snapshot",
        "take_screenshot",
        "click",
        "take_screenshot",
        "take_snapshot",
        "click",
        "take_screenshot",
    ]


@pytest.mark.unit
def test_an_element_gone_from_the_page_is_refused_as_changed(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(
        {
            "calls": {
                "take_snapshot": [
                    page(*(line.format(label="Next") for line in WIZARD_STEP)),
                    page('uid=1_0 RootWebArea "Test sets"'),
                ]
            }
        }
    )
    rig.call("take_snapshot")
    rig.call("click", uid="6_2")
    gone = rig.call("click", uid="6_3")
    assert reply_text(gone) == story_relay.PAGE_CHANGED_TEXT
    assert (rig.last()["class"], rig.last()["refused"]) == ("guard", "invalid")


@pytest.mark.unit
def test_a_page_that_cannot_be_read_again_refuses_the_click(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(
        {
            "calls": {
                "take_snapshot": [
                    {"label": "snapshot_documents"},
                    {"error": {"code": -32603, "message": "no page"}},
                ]
            }
        }
    )
    rig.call("take_snapshot")
    rig.call("hover", uid="1_17")
    refused = rig.call("click", uid="1_16")
    assert reply_text(refused) == story_relay.PAGE_UNREAD_TEXT
    assert [call["name"] for call in rig.tool_calls()].count("click") == 0


@pytest.mark.unit
def test_a_key_that_would_press_a_commit_control_never_reaches_the_child(
    make_rig: Callable[..., Rig],
) -> None:
    focused = page(
        'uid=1_0 RootWebArea "Users"',
        '  uid=6_0 dialog "Create New User" modal',
        '    uid=6_1 textbox "Email"',
        '    uid=6_2 button "Create User" focusable focused',
    )
    rig = make_rig({"calls": {"take_snapshot": [focused]}})
    rig.call("take_snapshot")
    refused = rig.call("press_key", key="Enter")
    assert reply_text(refused) == story_relay.COMMIT_POINT_TEXT
    assert (rig.last()["class"], rig.last()["refused"], rig.last()["label"]) == (
        "guard",
        "commit_point",
        "Create User",
    )
    assert not rig.call("press_key", key="Tab").is_error
    typed = rig.call("type_text", text="x@example.com", submitKey="Enter")
    assert reply_text(typed) == story_relay.COMMIT_POINT_TEXT
    sent = [call["name"] for call in rig.tool_calls()]
    assert "type_text" not in sent and sent.count("press_key") == 1


@pytest.mark.unit
def test_enter_in_a_chat_box_is_refused_and_typing_into_it_is_not(
    make_rig: Callable[..., Rig],
) -> None:
    chat = page(
        'uid=1_0 RootWebArea "Quick Start"',
        "  uid=1_1 main",
        '    uid=1_2 textbox "Describe your documents" focusable focused multiline',
        "    uid=1_3 button",
    )
    rig = make_rig({"calls": {"take_snapshot": [chat]}})
    rig.call("take_snapshot")
    assert not rig.call("type_text", text="Where do I start?").is_error
    for refused in (
        rig.call("press_key", key="Enter"),
        rig.call("type_text", text="one more line\n"),
        rig.call("click", uid="1_3"),
    ):
        assert reply_text(refused) == story_relay.COMMIT_POINT_TEXT
    assert [line["label"] for line in rig.lines()[-3:]] == [
        "Enter in Describe your documents",
        "Enter in Describe your documents",
        "an unnamed button",
    ]
    assert [call["name"] for call in rig.tool_calls()].count("type_text") == 1


REVIEW_ITEM = (
    'uid=1_0 RootWebArea "invoice.pdf" url="http://localhost:3000/#/review-queues/items/x"',
    "  uid=1_1 main",
    '    uid=1_2 button "Take over"',
    '    uid=1_3 button "Skip"',
    '    uid=1_4 button "Complete"',
    '    uid=1_5 button "Next item"',
    '  uid=6_0 dialog "Move to another queue" modal',
    '    uid=6_1 button "Move"',
    '    uid=6_2 button "Assign"',
    '    uid=6_3 button "Unassign"',
)


@pytest.mark.unit
def test_the_review_stacks_review_controls_never_reach_the_child(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig({"calls": {"take_snapshot": [page(*REVIEW_ITEM)]}})
    rig.call("take_snapshot")
    labels = []
    for uid in ("1_2", "1_3", "1_4", "1_5", "6_1", "6_2", "6_3"):
        assert reply_text(rig.call("click", uid=uid)) == story_relay.COMMIT_POINT_TEXT
        labels.append((rig.last()["refused"], rig.last()["label"]))
    assert labels == [
        ("commit_point", label)
        for label in (
            "Take over",
            "Skip",
            "Complete",
            "Next item",
            "Move",
            "Assign",
            "Unassign",
        )
    ]
    assert [call["name"] for call in rig.tool_calls()] == ["take_snapshot"]


@pytest.mark.unit
def test_a_space_typed_onto_a_relabelled_control_is_judged_as_pressing_it(
    make_rig: Callable[..., Rig],
) -> None:
    focused = [
        line.format(label="Create") + (" focusable focused" if "6_3" in line else "")
        for line in WIZARD_STEP
    ]
    rig = make_rig(
        {
            "calls": {
                "take_snapshot": [
                    page(*(line.format(label="Next") for line in WIZARD_STEP)),
                    page(*focused),
                ]
            }
        }
    )
    rig.call("take_snapshot")
    assert not rig.call("click", uid="6_3").is_error
    for refused in (rig.call("press_key", key=" "), rig.call("type_text", text=" ")):
        assert reply_text(refused) == story_relay.COMMIT_POINT_TEXT
        assert (rig.last()["refused"], rig.last()["label"]) == (
            "commit_point",
            "Create",
        )
    assert [call["name"] for call in rig.tool_calls()] == [
        "take_snapshot",
        "click",
        "take_screenshot",
        "take_snapshot",
    ]


@pytest.mark.unit
def test_a_phrase_typed_into_a_search_box_is_typed(
    make_rig: Callable[..., Rig],
) -> None:
    search = page(
        'uid=1_0 RootWebArea "Document List"',
        "  uid=1_1 main",
        '    uid=1_2 searchbox "Find documents" focusable focused',
        '    uid=1_3 button "Delete"',
    )
    rig = make_rig({"calls": {"take_snapshot": [search]}})
    rig.call("take_snapshot")
    assert not rig.call("type_text", text="lending package").is_error
    assert [call["name"] for call in rig.tool_calls()].count("type_text") == 1


def _editor(*extra: str) -> dict[str, Any]:
    return page(
        'uid=1_0 RootWebArea "invoice.pdf"',
        "  uid=1_1 main",
        '    uid=1_2 heading "Extracted fields" level="3"',
        '    uid=1_3 button "Discard changes" disableable disabled',
        '    uid=1_4 button "Save draft" disableable disabled',
        '    uid=1_5 tab "Visual Editor" selected',
        '    uid=1_6 textbox "Employer name" value="Acme" focusable focused',
        '    uid=1_7 checkbox "Signed"',
        '    uid=1_8 link "Queue Management" url="http://localhost:3000/#/review-queues"',
        *extra,
    )


@pytest.mark.unit
def test_every_edit_on_a_page_whose_editor_saves_itself_is_a_commit_point(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig({"calls": {"take_snapshot": [_editor()]}})
    rig.call("take_snapshot")
    for refused in (
        rig.call("fill", uid="1_6", value="Acme Inc"),
        rig.call("fill_form", elements=[{"uid": "1_6", "value": "Acme Inc"}]),
        rig.call("type_text", text="Inc"),
        rig.call("press_key", key="Backspace"),
        rig.call("press_key", key="Enter"),
        rig.call("drag", from_uid="1_6", to_uid="1_7"),
        rig.call("click", uid="1_7"),
    ):
        assert reply_text(refused) == story_relay.COMMIT_POINT_TEXT
        assert (rig.last()["class"], rig.last()["refused"], rig.last()["label"]) == (
            "guard",
            "commit_point",
            "Save draft",
        )
    assert [call["name"] for call in rig.tool_calls()] == ["take_snapshot"]
    for allowed in (
        rig.call("click", uid="1_5"),
        rig.call("press_key", key="Shift+Tab"),
        rig.call("press_key", key="Escape"),
        rig.call("hover", uid="1_6"),
        rig.call("click", uid="1_8"),
    ):
        assert not allowed.is_error
    assert [call["name"] for call in rig.tool_calls()].count("take_screenshot") == 5


@pytest.mark.unit
@pytest.mark.parametrize(
    ("role", "expected"),
    [
        *(
            (role, ("commit_point", "Save draft"))
            for role in (
                "checkbox",
                "switch",
                "radio",
                "option",
                "menuitemcheckbox",
                "menuitemradio",
                "slider",
                "spinbutton",
            )
        ),
        ("tab", None),
        ("link", None),
        ("button", None),
        ("textbox", None),
    ],
)
def test_a_click_sets_a_value_on_an_autosaving_page_only_through_a_value_control(
    role: str, expected: tuple[str, str] | None
) -> None:
    editor = judged(
        'uid=1_0 RootWebArea "invoice.pdf"',
        '  uid=1_1 button "Save draft" disableable disabled',
        f'  uid=1_2 {role} "Signed"',
        '    uid=1_3 StaticText "Signed"',
    )
    assert editor.sets_a_value("1_2") == expected
    assert editor.sets_a_value("1_3") == expected
    plain_page = judged('uid=1_0 RootWebArea "x"', f'  uid=1_2 {role} "Signed"')
    assert plain_page.sets_a_value("1_2") is None


@pytest.mark.unit
def test_only_a_control_named_for_saving_a_draft_marks_a_page_as_saving_itself() -> (
    None
):
    marked = judged('uid=1_0 RootWebArea "x"', '  uid=1_1 menuitem "Save draft"')
    assert marked.autosave_control == "Save draft"
    for line in (
        '  uid=1_1 StaticText "Save draft"',
        '  uid=1_1 heading "Save draft" level="2"',
        '  uid=1_1 button "Save drafts"',
        '  uid=1_1 button "Save"',
    ):
        assert judged('uid=1_0 RootWebArea "x"', line).autosave_control is None


@pytest.mark.unit
def test_an_edit_on_a_page_that_cannot_be_read_again_is_refused(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(
        {
            "calls": {
                "take_snapshot": [
                    {"label": "snapshot_documents"},
                    {"error": {"code": -32603, "message": "no page"}},
                ]
            }
        }
    )
    rig.call("take_snapshot")
    rig.call("hover", uid="1_17")
    refused = rig.call("fill", uid="1_24", value="lending")
    assert reply_text(refused) == story_relay.PAGE_UNREAD_TEXT
    assert (rig.last()["class"], rig.last()["refused"]) == ("guard", "invalid")
    assert [call["name"] for call in rig.tool_calls()].count("fill") == 0


@pytest.mark.unit
def test_an_edit_is_judged_by_the_page_it_lands_on(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(
        {
            "calls": {
                "take_snapshot": [
                    {"label": "snapshot_documents"},
                    _editor(),
                ]
            }
        }
    )
    rig.call("take_snapshot")
    assert not rig.call("fill", uid="1_24", value="lending").is_error
    rig.call("navigate_page", type="url", url="http://localhost:3000/#/x")
    refused = rig.call("fill", uid="1_6", value="Acme Inc")
    assert (rig.last()["refused"], rig.last()["label"]) == (
        "commit_point",
        "Save draft",
    )
    assert reply_text(refused) == story_relay.COMMIT_POINT_TEXT
    assert [call["name"] for call in rig.tool_calls()] == [
        "take_snapshot",
        "fill",
        "take_screenshot",
        "navigate_page",
        "take_screenshot",
        "take_snapshot",
    ]


@pytest.mark.unit
def test_actions_after_a_terminal_report_are_refused_and_looks_go_on(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.call("take_snapshot")
    rig.relay.end_actions()
    refused = rig.call("click", uid="1_16")
    assert reply_text(refused) == story_relay.REPORTED_TEXT
    assert (rig.last()["class"], rig.last()["refused"], rig.last()["counted"]) == (
        "guard",
        "terminal_done",
        False,
    )
    assert not rig.call("take_snapshot").is_error
    assert [call["name"] for call in rig.tool_calls()] == [
        "take_snapshot",
        "take_snapshot",
    ]


@pytest.mark.unit
def test_signing_out_is_refused_as_the_session_s_end(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(scenario_snapshot("snapshot_dialog_open"))
    rig.call("take_snapshot")
    reply = rig.call("click", uid=named("snapshot_dialog_open", "menuitem", "Sign out"))
    assert reply_text(reply) == story_relay.SESSION_END_TEXT
    assert (rig.last()["class"], rig.last()["refused"], rig.last()["label"]) == (
        "guard",
        "session_end",
        "Sign out",
    )


@pytest.mark.unit
def test_a_uid_absent_from_the_latest_snapshot_is_refused_and_so_is_one_before_any(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    before = rig.call("click", uid="1_16")
    rig.call("take_snapshot")
    stale = rig.call("click", uid="999_999")
    for reply in (before, stale):
        assert reply_text(reply) == story_relay.STALE_UID_TEXT
    assert [
        (line["class"], line["refused"], line["label"]) for line in rig.lines()
    ] == [
        ("guard", "invalid", None),
        ("look", None, None),
        ("guard", "invalid", None),
    ]


@pytest.mark.unit
def test_a_confirm_is_never_accepted_and_its_dismissal_is_relayed(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(
        {
            "calls": {
                "take_snapshot": [{"label": "snapshot_dialogs"}],
                "click": [{"label": "click_discard"}],
                "take_screenshot": [
                    {"label": "screenshot_while_confirm_open"},
                    {"label": "screenshot_jpeg"},
                ],
                "handle_dialog": [{"label": "handle_confirm_dismiss"}],
            }
        }
    )
    rig.call("take_snapshot")
    opened = rig.call(
        "click", uid=named("snapshot_dialogs", "button", "Discard changes")
    )
    assert not images_in(opened) and rig.last()["result"]["screen"] is None
    refused = rig.call("handle_dialog", action="accept")
    assert reply_text(refused) == story_relay.COMMIT_POINT_TEXT
    assert (rig.last()["class"], rig.last()["refused"], rig.last()["label"]) == (
        "guard",
        "commit_point",
        "Discard the changes you made to this document?",
    )
    dismissed = rig.call("handle_dialog", action="dismiss")
    assert not dismissed.is_error and rig.relay.dialog is None
    handled = [call for call in rig.tool_calls() if call["name"] == "handle_dialog"]
    assert handled == [{"name": "handle_dialog", "arguments": {"action": "dismiss"}}]


@pytest.mark.unit
def test_a_prompt_is_never_accepted_and_an_alert_is(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(
        {
            "calls": {
                "take_snapshot": [{"label": "snapshot_dialogs"}],
                "click": [{"label": "click_rename"}],
                "take_screenshot": [
                    {
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": text_of(
                                        "screenshot_while_confirm_open"
                                    ).replace(
                                        "confirm: Discard the changes you made to this document?",
                                        "prompt: New name",
                                    ),
                                }
                            ],
                            "isError": True,
                        }
                    },
                    {"label": "screenshot_jpeg"},
                ],
                "handle_dialog": [
                    {"label": "handle_prompt_dismiss"},
                    {"label": "handle_alert_accept"},
                ],
            }
        }
    )
    rig.call("take_snapshot")
    rig.call("click", uid=named("snapshot_dialogs", "button", "Rename"))
    assert rig.relay.dialog == ("prompt", "New name")
    assert reply_text(rig.call("handle_dialog", action="accept", promptText="x")) == (
        story_relay.COMMIT_POINT_TEXT
    )
    rig.call("handle_dialog", action="dismiss")
    rig.relay.dialog = ("alert", "Processing finished in 4 steps.")
    assert not rig.call("handle_dialog", action="accept").is_error


@pytest.mark.unit
def test_answering_a_dialog_nobody_was_shown_is_refused_but_dismissing_one_is_not(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig({"calls": {"handle_dialog": [{"label": "handle_no_dialog"}]}})
    refused = rig.call("handle_dialog", action="accept")
    assert reply_text(refused) == story_relay.NO_DIALOG_TEXT
    assert (rig.last()["class"], rig.last()["refused"]) == ("guard", "invalid")
    dismissed = rig.call("handle_dialog", action="dismiss")
    assert dismissed.is_error and "No open dialog found" in reply_text(dismissed)


@pytest.mark.unit
def test_the_sign_in_form_ends_the_flow_as_signed_out(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(scenario_snapshot("snapshot_signin"))
    reply = rig.call("take_snapshot")
    assert reply_text(reply) == story_relay.SIGNED_OUT_TEXT
    later = rig.call("click", uid=named("snapshot_signin", "button", "Sign in"))
    assert reply_text(later) == story_relay.SESSION_OVER_TEXT
    lines = rig.lines()
    assert [(line["class"], line["label"], line["refused"]) for line in lines] == [
        ("look", None, None),
        ("harness", "signed_out", None),
        ("action", None, "session_end"),
    ]
    assert [call["name"] for call in rig.tool_calls()] == ["take_snapshot"]


@pytest.mark.unit
def test_a_child_that_dies_mid_call_ends_the_flow_as_a_relay_error(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig({"calls": {"click": [{"exit": 3}]}})
    rig.call("take_snapshot")
    reply = rig.call("click", uid="1_16")
    assert reply_text(reply) == story_relay.BROWSER_GONE_TEXT
    after = rig.call("take_snapshot")
    assert reply_text(after) == story_relay.SESSION_OVER_TEXT
    assert [
        (line["class"], line["label"], line["counted"]) for line in rig.lines()
    ] == [
        ("look", None, False),
        ("action", None, True),
        ("harness", "child_died", False),
        ("look", None, False),
    ]
    rig.relay.settle()
    assert len([line for line in rig.lines() if line["label"] == "child_died"]) == 1


@pytest.mark.unit
def test_a_child_that_dies_while_idle_is_journalled_before_the_journal_ends(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    child = rig.relay.child
    assert child is not None
    os.killpg(child.process.pid, signal.SIGKILL)
    wait_until(lambda: not child.alive)
    rig.relay.settle()
    rig.ledger.end("eof")
    lines = journal_lines(rig.path)
    assert (lines[-2]["class"], lines[-2]["label"], lines[-2]["tool"]) == (
        "harness",
        "child_died",
        story_relay.CHILD_NAME,
    )
    assert lines[-1]["reason"] == "eof"


@pytest.mark.unit
def test_closing_the_relay_is_not_a_death(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.relay.close()
    rig.relay.settle()
    assert rig.lines() == []
    assert (
        rig.relay.child is not None and rig.relay.child.process.popen.poll() is not None
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "scenario",
    [{"exit_at_start": 1}, {"tools": "garbled"}, {"initialize": "garbled"}],
    ids=["exits", "tools-list-garbled", "initialize-garbled"],
)
def test_a_child_that_never_lists_its_tools_is_a_child_died_line_and_serves_nothing(
    make_rig: Callable[..., Rig], scenario: dict[str, Any]
) -> None:
    rig = make_rig(scenario)
    assert rig.relay.child is None and rig.relay.tools() == []
    assert rig.relay.over == "child_died"
    assert [(line["class"], line["label"]) for line in rig.lines()] == [
        ("harness", "child_died")
    ]


@pytest.mark.unit
def test_a_devtools_path_that_cannot_run_is_a_child_died_line(tmp_path: Path) -> None:
    path = tmp_path / story.JOURNAL_FILE
    journal = story.JournalWriter(path, START, relay=True, budget=5)
    ledger = story_relay.Ledger(journal, budget=5)
    said: list[str] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError(2, "No such file or directory")

    relay = story_relay.start(
        tmp_path / "absent",
        ledger=ledger,
        budget=5,
        out=tmp_path,
        environ={},
        say=said.append,
        starter=refuse,
    )
    assert relay.tools() == [] and relay.over == "child_died"
    assert said and "could not start" in said[0]
    (line,) = [line for line in journal_lines(path) if line["type"] == "call"]
    assert (line["class"], line["label"], line["tool"]) == (
        "harness",
        "child_died",
        story_relay.CHILD_NAME,
    )


@pytest.mark.unit
def test_a_cancellation_reaches_the_child_and_the_call_returns_at_once(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig({"calls": {"click": [{"hang": True}]}})
    rig.call("take_snapshot")
    replies: list[story_relay.Reply] = []
    worker = threading.Thread(
        target=lambda: replies.append(rig.call("click", uid="1_16"))
    )
    worker.start()
    wait_until(lambda: any(call["name"] == "click" for call in rig.tool_calls()))
    started = time.monotonic()
    rig.relay.cancel()
    worker.join(10)
    assert not worker.is_alive() and time.monotonic() - started < 5
    assert reply_text(replies[0]) == story_relay.CANCELLED_TEXT
    click = next(
        m for m in rig.received() if m.get("params", {}).get("name") == "click"
    )
    wait_until(
        lambda: any(
            m.get("method") == "notifications/cancelled" for m in rig.received()
        )
    )
    cancelled = next(
        m for m in rig.received() if m.get("method") == "notifications/cancelled"
    )
    assert cancelled["params"]["requestId"] == click["id"]
    assert (rig.last()["ok"], rig.last()["counted"]) == (False, True)


@pytest.mark.unit
def test_a_stop_signal_ends_the_wait_on_the_child(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig({"calls": {"wait_for": [{"hang": True}]}})
    stopping = threading.Event()
    rig.relay.stop_when(stopping.is_set)
    replies: list[story_relay.Reply] = []
    worker = threading.Thread(
        target=lambda: replies.append(rig.call("wait_for", text=["never"]))
    )
    worker.start()
    wait_until(lambda: any(call["name"] == "wait_for" for call in rig.tool_calls()))
    stopping.set()
    worker.join(10)
    assert not worker.is_alive()
    assert reply_text(replies[0]) == story_relay.CANCELLED_TEXT


@pytest.mark.unit
def test_a_call_the_child_never_answers_times_out_and_is_cancelled_there(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig({"calls": {"hover": [{"hang": True}]}}, call_seconds=0.5)
    reply = rig.call("hover", uid="1_17")
    assert reply_text(reply) == story_relay.TIMED_OUT_TEXT
    wait_until(
        lambda: any(
            m.get("method") == "notifications/cancelled" for m in rig.received()
        )
    )
    late = rig.call("take_snapshot")
    assert not late.is_error


@pytest.mark.unit
def test_an_answer_over_the_result_bound_fails_as_size(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(story_relay, "RESULT_BYTES", 200_000)
    rig = make_rig(
        {
            "calls": {
                "take_snapshot": [
                    {"oversize": 300_000},
                    {"label": "snapshot_documents"},
                ]
            }
        }
    )
    reply = rig.call("take_snapshot")
    assert reply_text(reply) == story_relay.TOO_LARGE_TEXT
    assert (rig.last()["ok"], rig.last()["refused"]) == (False, "size")
    assert not rig.call("take_snapshot").is_error


@pytest.mark.unit
def test_a_json_rpc_error_from_the_child_is_an_error_result_naming_it(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(
        {"calls": {"press_key": [{"error": {"code": -32602, "message": "bad key"}}]}}
    )
    reply = rig.call("press_key", key="Tab")
    assert reply.is_error
    assert reply_text(reply) == "The browser tool refused the call: bad key"
    assert (
        rig.last()["ok"],
        rig.last()["counted"],
        rig.last()["result"]["screen"],
    ) == (
        False,
        True,
        None,
    )


@pytest.mark.unit
def test_invalid_arguments_are_refused_before_the_child_and_journalled_cleaned(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    reply = rig.call("handle_dialog", action="maybe")
    assert reply_text(reply) == "action must be one of: accept, dismiss"
    assert (rig.last()["class"], rig.last()["refused"], rig.last()["args"]) == (
        "action",
        "invalid",
        {},
    )
    rig.relay.refuse_invalid("take_snapshot")
    assert (rig.last()["class"], rig.last()["refused"]) == ("look", "invalid")
    assert rig.tool_calls() == []


@pytest.mark.unit
def test_an_answer_s_text_is_bounded(make_rig: Callable[..., Rig]) -> None:
    long_text = "x" * (story_relay.TEXT_CHARS + 50)
    rig = make_rig(
        {
            "calls": {
                "navigate_page": [
                    {"result": {"content": [{"type": "text", "text": long_text}]}}
                ]
            }
        }
    )
    reply = rig.call("navigate_page", type="reload")
    text = reply_text(reply)
    assert text.startswith("x" * story_relay.TEXT_CHARS)
    assert text.endswith(story_relay.TEXT_CUT_TEXT.format(limit=story_relay.TEXT_CHARS))


@pytest.mark.unit
def test_every_refusal_counts_toward_the_limit_whatever_refused_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / story.JOURNAL_FILE
    journal = story.JournalWriter(path, START, relay=True, budget=3)
    ledger = story_relay.Ledger(journal, budget=3)
    ledger.call(
        tool="search_documentation",
        cls="doc",
        ok=False,
        refused="limit",
        args={"query": "q"},
    )
    ledger.call(tool="click", cls="action", ok=True, counted=True)
    ledger.count_image()
    ledger.call(
        tool="note_confusion", cls="note", ok=True, args={"what": "w", "why": "y"}
    )
    ledger.call(
        tool="report_done",
        cls="report",
        ok=True,
        args={"evidence": "e", "stopped_at_commit_point": False},
        terminal="done",
        actions_used=99,
        images_sent=99,
    )
    lines = [line for line in journal_lines(path) if line["type"] == "call"]
    assert [(line["actions_used"], line["images_sent"]) for line in lines] == [
        (0, 0),
        (1, 0),
        (1, 1),
        (1, 1),
    ]
    assert lines[-1]["after_budget"] is False
    assert ledger.refusals == 1 and not ledger.spent
    for _ in range(story_relay.REFUSAL_LIMIT - 1):
        ledger.call(tool="x", cls="harness", ok=False, refused="withheld")
    assert ledger.spent and ledger.refusals == story_relay.REFUSAL_LIMIT


@pytest.mark.unit
def test_a_relayed_journal_reads_back_and_judges_from_what_the_relay_counted(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(scenario_snapshot("snapshot_dialog_open"), budget=30)
    rig.call("take_snapshot")
    rig.call("click", uid=named("snapshot_dialog_open", "button", "Refresh"))
    rig.call("click", uid=named("snapshot_dialog_open", "button", "Save"))
    rig.ledger.call(
        tool="report_done",
        cls="report",
        ok=True,
        args={
            "evidence": "the mapping form is filled",
            "stopped_at_commit_point": True,
        },
        terminal="done",
    )
    rig.ledger.end("terminal")
    journal = story_verdict.read_journal(rig.path)
    assert journal.start.relay is True and journal.start.budget == 30
    assert [call.cls for call in journal.calls] == ["look", "action", "guard", "report"]
    assert journal.calls[1].screen == "screens/001.jpg"
    assert journal.calls[-1].after_budget is False
    context = story_verdict.FlowContext(
        key="route-folder-to-profile@platform-admin",
        story="route-folder-to-profile",
        persona="platform-admin",
        group="Admin",
        title="Make one input folder use one configuration",
        expectation="allowed",
        kind="commit_point",
        documented_steps=5,
        needs=(),
        declared_docs=(),
        series="5d0c1e9a2b7f",
        budget=30,
        audit_state="clean",
    )
    verdict = story_verdict.judge(journal, context)
    result = story_verdict.summarize(journal, context, verdict)
    assert (verdict.verdict, verdict.flags) == ("done", ("stopped_at_commit_point",))
    assert (result.actions_used, result.looks, result.commit_points) == (
        1,
        1,
        ("Save",),
    )


def _index(directory: Path) -> Path:
    root = directory / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "configuration-profiles.md").write_text(
        "# Configuration Profiles\n\nProfiles hold configuration.\n\n"
        "## Prefix mappings: assigning a profile by where a document lands\n\n"
        "A prefix mapping assigns a profile to every document under a prefix.\n",
        encoding="utf-8",
    )
    index = docs_index.build(
        root,
        source=docs_index.DocsSource(kind="worktree", ref=None, dirty=False),
        pages=["configuration-profiles.md"],
    )
    return docs_index.save(index, directory / "index.json")


@dataclass
class Served:
    proc: subprocess.Popen[bytes]
    out: Path
    record: Path
    stderr: Path
    buffer: bytes = b""

    def send(self, message: Any) -> None:
        stream = self.proc.stdin
        assert stream is not None
        line = message if isinstance(message, (bytes, str)) else json.dumps(message)
        data = line.encode() if isinstance(line, str) else line
        stream.write(data + b"\n")
        stream.flush()

    def read(self, timeout: float = 30.0) -> dict[str, Any]:
        stream = self.proc.stdout
        assert stream is not None
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            left = deadline - time.monotonic()
            assert left > 0, "no reply in time"
            ready, _, _ = select.select([stream], [], [], left)
            if not ready:
                continue
            chunk = os.read(stream.fileno(), 65536)
            assert chunk, f"the server closed its output: {self.stderr.read_text()}"
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def request(
        self, request_id: int, method: str, params: Any = None
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        while True:
            reply = self.read()
            if reply.get("id") == request_id:
                return reply

    def call(self, request_id: int, name: str, **arguments: Any) -> dict[str, Any]:
        return self.request(
            request_id, "tools/call", {"name": name, "arguments": arguments}
        )

    def journal(self) -> list[dict[str, Any]]:
        return journal_lines(self.out / story.JOURNAL_FILE)

    def finish(self, timeout: float = 30.0) -> int:
        stream = self.proc.stdin
        if stream is not None and not stream.closed:
            stream.close()
        return self.proc.wait(timeout=timeout)


@pytest.fixture
def serve(tmp_path: Path) -> Iterator[Callable[..., Served]]:
    started: list[Served] = []

    def run(scenario: dict[str, Any] | None = None, *, budget: int = 5) -> Served:
        base = tmp_path / f"server-{len(started)}"
        base.mkdir()
        script, record = fake_devtools(base, scenario or {})
        start = base / "start.json"
        start.write_text(json.dumps(START), encoding="utf-8")
        out = base / "out"
        stderr = base / "stderr.txt"
        with stderr.open("wb") as errors:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(SCRIPTS / "ux_story_mcp.py"),
                    "--index",
                    str(_index(base)),
                    "--out",
                    str(out),
                    "--start",
                    str(start),
                    "--budget",
                    str(budget),
                    "--devtools",
                    str(script),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                env={"LANG": "C.UTF-8"},
            )
        served = Served(proc, out, record, stderr)
        started.append(served)
        return served

    yield run
    for served in started:
        if served.proc.poll() is None:
            served.proc.kill()
            served.proc.wait(timeout=30)


@pytest.mark.unit
def test_the_server_in_relay_mode_lists_its_own_tools_then_the_browser_tools(
    serve: Callable[..., Served],
) -> None:
    server = serve()
    hello = server.request(1, "initialize", {"protocolVersion": "2025-06-18"})
    assert hello["result"]["serverInfo"] == {"name": "ux-story", "version": "1"}
    listed = server.request(2, "tools/list")
    assert [tool["name"] for tool in listed["result"]["tools"]] == [
        *story.OWN_TOOL_NAMES,
        *tool_profiles.STORY_BROWSER_TOOLS,
    ]
    assert server.finish() == 0
    lines = server.journal()
    assert (lines[0]["relay"], lines[0]["budget"]) == (True, 5)
    assert (lines[-1]["type"], lines[-1]["reason"]) == ("end", "eof")
    boot = records(server.record)[0]
    environ = {k: v for k, v in boot["environ"].items() if k not in PLATFORM_VARIABLES}
    assert environ["PATH"] == agent.DEFAULT_SEARCH_PATH
    assert Path(environ["HOME"]).resolve() == (server.out / "devtools-root").resolve()


@pytest.mark.unit
def test_a_relayed_flow_journals_each_call_once_and_judges_from_the_journal(
    serve: Callable[..., Served],
) -> None:
    server = serve(scenario_snapshot("snapshot_documents"))
    server.request(1, "initialize", {"protocolVersion": "2025-06-18"})
    snap = server.call(2, "take_snapshot")
    assert "## Latest page snapshot" in snap["result"]["content"][0]["text"]
    clicked = server.call(3, "click", uid="1_16")
    assert [item["type"] for item in clicked["result"]["content"]] == ["text", "image"]
    guarded = server.call(4, "click", uid="1_18")
    assert guarded["result"]["isError"] is True
    miss = server.call(5, "read_documentation", ref=UNINDEXED_HELP_LINK)
    assert (
        json.loads(miss["result"]["content"][0]["text"])["error"] == story.NO_PAGE_TEXT
    )
    done = server.call(
        6, "report_done", evidence="the list refreshed", stopped_at_commit_point=False
    )
    assert done["result"]["isError"] is False
    assert server.finish() == 0
    journal = story_verdict.read_journal(server.out / story.JOURNAL_FILE)
    assert [(c.tool, c.cls) for c in journal.calls] == [
        ("take_snapshot", "look"),
        ("click", "action"),
        ("click", "guard"),
        ("read_documentation", "doc"),
        ("report_done", "report"),
    ]
    assert journal.calls[3].label == "help_link_unresolved"
    assert journal.calls[4].after_budget is False and journal.end is not None
    assert journal.end.reason == "terminal"
    assert (server.out / "screens" / "001.jpg").exists()
    assert not list((server.out / "devtools-root").iterdir())


@pytest.mark.unit
def test_a_cancellation_during_a_browser_call_reaches_the_child_and_goes_unanswered(
    serve: Callable[..., Served],
) -> None:
    server = serve({"calls": {"wait_for": [{"hang": True}]}})
    server.send(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "wait_for", "arguments": {"text": ["never"]}},
        }
    )
    wait_until(
        lambda: any(
            entry.get("in", {}).get("params", {}).get("name") == "wait_for"
            for entry in records(server.record)
        )
    )
    server.send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 7},
        }
    )
    server.send({"jsonrpc": "2.0", "id": 8, "method": "tools/list"})
    first = server.read()
    assert first["id"] == 8, f"the cancelled request was answered: {first}"
    assert server.finish() == 0
    assert any(
        entry.get("in", {}).get("method") == "notifications/cancelled"
        for entry in records(server.record)
    )
    calls = [line for line in server.journal() if line["type"] == "call"]
    assert [(line["tool"], line["ok"]) for line in calls] == [("wait_for", False)]


@pytest.mark.unit
def test_a_request_cancelled_before_it_starts_is_dropped_unanswered(
    serve: Callable[..., Served],
) -> None:
    server = serve({"calls": {"wait_for": [{"delay": 1.0, "label": "wait_for"}]}})
    server.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "wait_for", "arguments": {"text": ["Refresh"]}},
        }
    )
    server.send(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "take_snapshot", "arguments": {}},
        }
    )
    server.send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 2},
        }
    )
    first = server.read()
    assert first["id"] == 1
    third = server.request(3, "ping")
    assert third == {"jsonrpc": "2.0", "id": 3, "result": {}}
    assert server.finish() == 0
    assert [line["tool"] for line in server.journal() if line["type"] == "call"] == [
        "wait_for"
    ]


@pytest.mark.unit
def test_a_ping_is_answered_while_a_browser_call_is_running(
    serve: Callable[..., Served],
) -> None:
    server = serve({"calls": {"wait_for": [{"delay": 1.0, "label": "wait_for"}]}})
    server.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "wait_for", "arguments": {"text": ["Refresh"]}},
        }
    )
    wait_until(
        lambda: any(
            entry.get("in", {}).get("params", {}).get("name") == "wait_for"
            for entry in records(server.record)
        )
    )
    server.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert server.read()["id"] == 2
    assert server.read()["id"] == 1
    assert server.finish() == 0


@pytest.mark.unit
def test_twenty_refused_calls_end_the_flow_and_the_server(
    serve: Callable[..., Served],
) -> None:
    server = serve()
    for request_id in range(1, story_relay.REFUSAL_LIMIT):
        reply = server.call(request_id, "click", uid="1_16")
        assert reply["result"]["isError"] is True
    last = server.call(story_relay.REFUSAL_LIMIT, "click", uid="1_16")
    texts = [item["text"] for item in last["result"]["content"]]
    assert texts[-1] == story_relay.REFUSAL_LIMIT_TEXT
    assert server.proc.wait(timeout=30) == 0
    lines = server.journal()
    assert lines[-1]["reason"] == "refusal_limit"
    assert (
        len([line for line in lines if line.get("refused")])
        == story_relay.REFUSAL_LIMIT
    )


@pytest.mark.unit
def test_a_stop_signal_during_a_browser_call_ends_the_journal_promptly(
    serve: Callable[..., Served],
) -> None:
    server = serve({"calls": {"wait_for": [{"hang": True}]}})
    server.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "wait_for", "arguments": {"text": ["never"]}},
        }
    )
    wait_until(
        lambda: any(
            entry.get("in", {}).get("params", {}).get("name") == "wait_for"
            for entry in records(server.record)
        )
    )
    started = time.monotonic()
    server.proc.send_signal(signal.SIGTERM)
    assert server.proc.wait(timeout=30) == 0
    assert time.monotonic() - started < 15
    lines = server.journal()
    assert [line["type"] for line in lines] == ["start", "call", "end"]
    assert lines[-1]["reason"] == "signal"
    assert any(
        entry.get("in", {}).get("method") == "notifications/cancelled"
        for entry in records(server.record)
    )


@pytest.mark.unit
def test_an_oversized_request_is_refused_as_size_without_being_read(
    serve: Callable[..., Served],
) -> None:
    server = serve()
    server.send(
        b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "pad": "'
        + b"x" * story_relay.REQUEST_BYTES
        + b'"}'
    )
    refused = server.read()
    assert refused["id"] is None and refused["error"]["code"] == -32600
    assert server.request(2, "ping")["result"] == {}
    assert server.finish() == 0
    (line,) = [line for line in server.journal() if line["type"] == "call"]
    assert (line["tool"], line["class"], line["refused"]) == ("", "harness", "size")


@pytest.mark.unit
def test_a_child_that_cannot_start_leaves_its_own_tools_and_a_relay_error(
    serve: Callable[..., Served],
) -> None:
    server = serve({"exit_at_start": 1})
    listed = server.request(1, "tools/list")
    assert [tool["name"] for tool in listed["result"]["tools"]] == list(
        story.OWN_TOOL_NAMES
    )
    unknown = server.call(2, "click", uid="1_16")
    assert unknown["error"]["code"] == -32602
    assert server.finish() == 0
    journal = story_verdict.read_journal(server.out / story.JOURNAL_FILE)
    assert [(c.cls, c.label, c.refused) for c in journal.calls] == [
        ("harness", "child_died", None),
        ("harness", None, "withheld"),
    ]
    assert "could not start" in server.stderr.read_text()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        (["--devtools", "/opt/x"], "relay mode needs both"),
        (["--budget", "3"], "relay mode needs both"),
        (["--devtools", "/opt/x", "--budget", "0"], "--budget must be a whole number"),
        (
            ["--devtools", "/opt/x", "--budget", "abc"],
            "--budget must be a whole number",
        ),
        (
            ["--devtools", "/opt/x", "--budget", "1001"],
            "--budget must be a whole number",
        ),
    ],
)
def test_relay_mode_needs_both_flags_and_a_whole_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str], needle: str
) -> None:
    start = tmp_path / "start.json"
    start.write_text(json.dumps(START), encoding="utf-8")
    out = tmp_path / "out"
    argv = [
        "--index",
        str(tmp_path / "i.json"),
        "--out",
        str(out),
        "--start",
        str(start),
    ]
    assert story.main([*argv, *extra]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and needle in err
    assert not out.exists()


FIXTURE_TEXTS: list[tuple[str, str]] = [
    (path.relative_to(FIXTURES).as_posix(), path.read_text(encoding="utf-8"))
    for path in sorted(FIXTURES.rglob("*"))
    if path.is_file() and path.suffix in (".json", ".html")
]


@pytest.mark.unit
def test_every_recorded_fixture_is_read_by_the_hygiene_check() -> None:
    assert sorted(name for name, _ in FIXTURE_TEXTS) == [
        "pages/account.html",
        "pages/dialogs.html",
        "pages/documents.html",
        "pages/index.html",
        "pages/signin.html",
        "session.json",
        "tools_list.json",
        "tools_list_default_flags.json",
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "text"), FIXTURE_TEXTS, ids=[name for name, _ in FIXTURE_TEXTS]
)
def test_a_recorded_fixture_holds_nothing_the_redactor_or_a_reviewer_would_remove(
    name: str, text: str
) -> None:
    assert redact.Redactor()(text) == text, f"{name} holds a shape the redactor removes"
    assert not re.search(r"(?<![0-9])[0-9]{12}(?![0-9])", text), (
        f"{name}: a 12-digit run"
    )
    assert "amazonaws.com" not in text and "arn:aws" not in text, name
    assert "idp-ux-nightly" not in text, f"{name} names the review stack"
    assert not re.search(r"[A-Za-z0-9+/]{200,}", text), f"{name} keeps image data"


@pytest.mark.unit
def test_no_recorded_answer_keeps_a_screenshot_s_pixels() -> None:
    images = [
        item
        for call in SESSION["calls"].values()
        for item in (call["answer"].get("result") or {}).get("content", [])
        if item.get("type") == "image"
    ]
    assert images, "the recording holds screenshots"
    assert {item["data"] for item in images} == {SESSION["recorded"]["image_data"]}


@pytest.mark.unit
def test_the_recording_was_made_with_the_pinned_server_and_the_harness_s_own_flags() -> (
    None
):
    recorded = SESSION["recorded"]
    pinned = json.loads(
        (SCRIPTS / "ux_runner" / "package.json").read_text(encoding="utf-8")
    )
    assert recorded["versions"]["chrome_devtools_mcp"] == "1.10.1"
    assert pinned["dependencies"]["chrome-devtools-mcp"] == "1.10.1"
    assert SESSION["initialize"]["serverInfo"]["version"] == "1.10.1"
    work = Path("<work>")
    assert recorded["chrome_flags"] == chrome.chrome_flags(profile_dir=work / "profile")
    expert = agent.mcp_servers(
        toolchain=paths.Toolchain(Path("/opt/ux-runner")),
        recorder_adapter=work / "unused",
        recorder_out=work / "unused",
        python="python3",
        home=work / "home",
        fs_root=work / "mcp-files",
    )[DEVTOOLS]
    assert recorded["devtools_argv"] == [expert["command"], *expert["args"]]


@pytest.mark.unit
def test_the_recording_saw_a_cancelled_call_go_unanswered_and_no_file_written() -> None:
    observed = SESSION["observed"]
    assert observed["cancelled_request_answered"] is False
    assert observed["ping_after_cancel"] == {}
    assert observed["server_requests"] == [] and observed["server_notifications"] == []
    assert observed["files_written_under_home_and_root"] == []
    assert observed["exit_after_stdin_close"]["returncode"] == 0


@pytest.mark.unit
def test_what_the_child_writes_out_of_turn_is_passed_over(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(
        {
            "calls": {
                "take_snapshot": [
                    {
                        "label": "snapshot_documents",
                        "send_first": [
                            "not json at all",
                            "",
                            "[1, 2]",
                            {"jsonrpc": "2.0", "id": 999, "result": {"content": []}},
                            {"jsonrpc": "2.0", "id": True, "result": {"content": []}},
                            {"jsonrpc": "2.0", "id": 4},
                            {"jsonrpc": "2.0", "method": "notifications/progress"},
                        ],
                    }
                ]
            }
        }
    )
    reply = rig.call("take_snapshot")
    assert not reply.is_error
    assert "uid=1_0 RootWebArea" in reply_text(reply)
    assert rig.ledger.seq == rig.last()["seq"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "image",
    [
        {"mimeType": "image/png", "data": base64.b64encode(JPEG_MAGIC).decode()},
        {"mimeType": "image/jpeg", "data": 7},
        {"mimeType": "image/jpeg", "data": "not base64!"},
        {"mimeType": "image/jpeg", "data": base64.b64encode(b"GIF89a").decode()},
        {"mimeType": "image/jpeg", "data": "A" * (story_relay.SCREEN_BYTES * 2)},
    ],
    ids=["png", "not-text", "not-base64", "not-a-jpeg", "too-long"],
)
def test_only_a_bounded_jpeg_counts_as_a_screenshot(image: dict[str, Any]) -> None:
    assert story_relay.jpeg_bytes(image) is None


@pytest.mark.unit
def test_an_address_that_will_not_parse_is_kept_whole_and_nothing_more() -> None:
    snapshot = story_relay.cut_snapshot(['uid=1_0 link "x" url="http://[broken/path"'])
    assert snapshot.addresses == frozenset({"http://[broken/path"})


@pytest.mark.unit
def test_a_child_started_without_pipes_is_refused() -> None:
    class Bare:
        stdin = None
        stdout = None

    process = processes.ManagedProcess(name="x", popen=Bare())
    with pytest.raises(story_relay.RelayError, match="without pipes"):
        story_relay.Child(process)


class _Stub:
    """Stands in for the adapter and the relay the inbox talks to."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("handle")
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {}}

    def begin(self) -> None:
        self.calls.append("begin")

    def cancel(self) -> None:
        self.calls.append("cancel")


def _inbox(chunks: list[bytes], *, limit: int = 4096) -> tuple[Any, _Stub, Any]:
    stub = _Stub()
    stream = io_text()
    feed = iter([*chunks, b""])
    inbox = story._Inbox(
        lambda size: next(feed),
        stub,
        story._Replies(stream),
        stub,
        limit=limit,
    )
    return inbox, stub, stream


def io_text() -> io.StringIO:
    return io.StringIO()


def _drained(inbox: Any) -> list[tuple[str, Any]]:
    found = []
    while not inbox.messages.empty():
        found.append(inbox.messages.get_nowait())
    return found


@pytest.mark.unit
def test_the_inbox_answers_ping_and_parse_errors_itself_and_queues_the_rest() -> None:
    inbox, stub, stream = _inbox(
        [
            b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\nnot json\n',
            b'{"jsonrpc": "2.0", "method": "notifications/initialized"}\n\n',
            b'{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}\n[3]\n',
            b'{"jsonrpc": "2.0", "id": 4, "method": "tools/call"}',
        ]
    )
    inbox._run()
    replies = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert replies == [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error"},
        },
    ]
    assert _drained(inbox) == [
        ("message", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ("message", [3]),
        ("message", {"jsonrpc": "2.0", "id": 4, "method": "tools/call"}),
        ("eof", None),
    ]


@pytest.mark.unit
def test_the_inbox_turns_an_oversized_line_into_one_size_item_however_it_arrives() -> (
    None
):
    inbox, _, _ = _inbox(
        [
            b"x" * 3000,
            b"x" * 3000,
            b'x"}\n{"jsonrpc": "2.0", "id": 5, "method": "ping"}\n',
        ]
    )
    inbox._run()
    assert [kind for kind, _ in _drained(inbox)] == ["size", "eof"]
    inbox, _, _ = _inbox(
        [b"y" * 5000 + b'\n{"jsonrpc": "2.0", "id": 6, "method": "a"}\n']
    )
    inbox._run()
    assert [kind for kind, _ in _drained(inbox)] == ["size", "message", "eof"]


@pytest.mark.unit
def test_a_cancellation_stops_the_request_being_handled_or_drops_a_queued_one() -> None:
    inbox, stub, _ = _inbox([])
    assert inbox.begin(1) is True
    inbox._cancel(1)
    assert inbox.finish() is True
    assert stub.calls == ["begin", "cancel"]
    inbox._cancel(2)
    inbox._cancel([2])
    inbox._cancel(True)
    assert inbox.begin(2) is False
    assert inbox.begin(2) is True and inbox.finish() is False
    assert inbox.begin("2") is True and inbox.finish() is False


@pytest.mark.unit
def test_replies_stop_once_the_client_has_gone() -> None:
    class Gone:
        def write(self, text: str) -> int:
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self) -> None:
            raise AssertionError("never reached")

    replies = story._Replies(Gone())
    replies.send({"jsonrpc": "2.0", "id": 1, "result": {}})
    assert replies.broken and replies.closed
    replies.send({"jsonrpc": "2.0", "id": 2, "result": {}})


def _pipe_stdin(monkeypatch: pytest.MonkeyPatch, *messages: Any) -> Any:
    """stdin as a pipe the server reads, fed from a thread of its own, then closed."""
    read, write = os.pipe()
    data = b"".join(
        (m if isinstance(m, bytes) else json.dumps(m).encode()) + b"\n"
        for m in messages
    )

    def feed() -> None:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(write, view) :]
        finally:
            os.close(write)

    threading.Thread(target=feed, daemon=True).start()
    stream = os.fdopen(read, "r")
    monkeypatch.setattr(sys, "stdin", stream)
    return stream


@pytest.mark.unit
def test_main_serves_relay_mode_in_process_until_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script, record = fake_devtools(tmp_path, scenario_snapshot("snapshot_documents"))
    start = tmp_path / "start.json"
    start.write_text(json.dumps(START), encoding="utf-8")
    out = tmp_path / "out"
    stream = _pipe_stdin(
        monkeypatch,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "take_snapshot", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "click", "arguments": "nope"},
        },
        b"x" * (story_relay.REQUEST_BYTES + 10),
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "note_dead_end", "arguments": {"what": "w", "why": "y"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "report_blocked",
                "arguments": {
                    "reason": "r",
                    "kind": "no_route",
                    "offered_a_control": False,
                },
            },
        },
    )
    before = signal.getsignal(signal.SIGTERM)
    try:
        code = story.main(
            [
                "--index",
                str(_index(tmp_path)),
                "--out",
                str(out),
                "--start",
                str(start),
                "--budget",
                "4",
                "--devtools",
                str(script),
            ]
        )
    finally:
        stream.close()
    assert code == 0
    assert signal.getsignal(signal.SIGTERM) == before
    replies = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    by_id = {reply.get("id"): reply for reply in replies}
    assert len(by_id[2]["result"]["tools"]) == 19
    assert by_id[3]["result"]["isError"] is False
    assert by_id[4]["error"]["code"] == -32602
    assert by_id[None]["error"]["code"] == -32600
    assert by_id[6]["result"]["isError"] is False
    journal = story_verdict.read_journal(out / story.JOURNAL_FILE)
    assert [(c.tool, c.cls, c.refused) for c in journal.calls] == [
        ("take_snapshot", "look", None),
        ("click", "action", "invalid"),
        ("", "harness", "size"),
        ("note_dead_end", "note", None),
        ("report_blocked", "report", None),
    ]
    assert journal.calls[-1].after_budget is False
    assert journal.end is not None and journal.end.reason == "terminal"
    assert records(record)


@pytest.mark.unit
def test_main_refuses_relay_mode_when_the_relay_cannot_be_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import ux_runner

    monkeypatch.setitem(sys.modules, "ux_runner.story_relay", None)
    monkeypatch.delattr(ux_runner, "story_relay", raising=False)
    start = tmp_path / "start.json"
    start.write_text(json.dumps(START), encoding="utf-8")
    argv = ["--index", "i", "--out", str(tmp_path / "out"), "--start", str(start)]
    assert story.main([*argv, "--devtools", "/x", "--budget", "3"]) == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "cannot be imported" in err
    assert not (tmp_path / "out").exists()


class _FakeRelay:
    """A relay with one tool and no child, for the adapter and transport alone."""

    def __init__(self, reply: Any = None, *, full: bool = False) -> None:
        self.reply = reply
        self.full = full
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.stop: Callable[[], bool] | None = None

    def tools(self) -> list[dict[str, Any]]:
        return [{"name": "click", "description": "", "inputSchema": {"type": "object"}}]

    def serves(self, name: str) -> bool:
        return name == "click"

    def call(self, name: str, arguments: Any) -> Any:
        self.calls.append((name, dict(arguments)))
        if self.full:
            raise story.JournalClosed("full")
        return self.reply

    def refuse_invalid(self, name: str) -> None:
        if self.full:
            raise story.JournalClosed("full")
        self.calls.append(("invalid", {"name": name}))

    def seen_on_screen(self, ref: str) -> bool:
        return ref == "seen"

    def begin(self) -> None:
        pass

    def cancel(self) -> None:
        pass

    def end_actions(self) -> None:
        self.calls.append(("end_actions", {}))

    def stop_when(self, stop: Callable[[], bool]) -> None:
        self.stop = stop

    def settle(self) -> None:
        if self.full:
            raise story.JournalClosed("full")

    def close(self) -> None:
        self.closed = True


class _Link:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def command(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(payload)
        return {"ok": True}


def _adapter(tmp_path: Path, relay: _FakeRelay) -> tuple[Any, _Link, Any]:
    journal = story.JournalWriter(
        tmp_path / story.JOURNAL_FILE, START, relay=True, budget=3
    )
    ledger = story_relay.Ledger(journal, budget=3)
    link = _Link()
    adapter = story.StoryAdapter(
        link,
        docs=None,
        journal=ledger,
        relay=relay,
    )
    return adapter, link, ledger


def _rpc(adapter: Any, name: str, arguments: Any) -> dict[str, Any]:
    reply = adapter.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert reply is not None
    return reply


@pytest.mark.unit
def test_the_adapter_lists_the_relay_s_tools_after_its_own_and_forwards_its_marks(
    tmp_path: Path,
) -> None:
    mark = {"label": story_relay.OUT_OF_STEPS_MARK, "say": story_relay.OUT_OF_STEPS}
    relay = _FakeRelay(story_relay.Reply(({"type": "text", "text": "t"},), True, mark))
    adapter, link, _ = _adapter(tmp_path, relay)
    listed = adapter.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert [tool["name"] for tool in listed["result"]["tools"]] == [
        *story.OWN_TOOL_NAMES,
        "click",
    ]
    assert adapter.seen_on_screen("seen") and not adapter.seen_on_screen("other")
    reply = _rpc(adapter, "click", None)
    assert reply["result"] == {
        "content": [{"type": "text", "text": "t"}],
        "isError": True,
    }
    assert relay.calls == [("click", {})]
    assert link.sent == [
        {"cmd": "mark", "label": "Out of steps", "say": story_relay.OUT_OF_STEPS}
    ]


@pytest.mark.unit
def test_an_accepted_terminal_report_ends_the_relay_s_actions(tmp_path: Path) -> None:
    relay = _FakeRelay(story_relay.Reply(({"type": "text", "text": "t"},), False))
    adapter, _, _ = _adapter(tmp_path, relay)
    _rpc(adapter, "click", {"uid": "1"})
    assert ("end_actions", {}) not in relay.calls
    _rpc(adapter, "report_done", {"evidence": "e", "stopped_at_commit_point": False})
    assert relay.calls[-1] == ("end_actions", {})
    _rpc(adapter, "report_done", {"evidence": "e", "stopped_at_commit_point": False})
    assert relay.calls.count(("end_actions", {})) == 1


@pytest.mark.unit
def test_injection_notes_past_the_recorded_ones_never_end_the_flow(
    tmp_path: Path,
) -> None:
    adapter, _, ledger = _adapter(tmp_path, _FakeRelay())
    for _ in range(story_relay.REFUSAL_LIMIT + story.INJECTIONS_RECORDED + 5):
        reply = _rpc(adapter, "note_injection", {"where": "page", "quote": "q"})
        assert json.loads(reply["result"]["content"][0]["text"])["ok"] is True
    assert ledger.refusals == 0 and not ledger.spent
    _rpc(adapter, "note_injection", {"where": "toast", "quote": "q"})
    assert ledger.refusals == 1


@pytest.mark.unit
def test_a_full_journal_answers_a_relayed_call_and_its_invalid_twin(
    tmp_path: Path,
) -> None:
    adapter, _, _ = _adapter(tmp_path, _FakeRelay(full=True))
    reply = _rpc(adapter, "click", {"uid": "1"})
    assert reply["result"]["isError"] is True
    assert json.loads(reply["result"]["content"][0]["text"])["error"] == (
        story._JOURNAL_FULL
    )
    invalid = _rpc(adapter, "click", "not an object")
    assert invalid["error"]["code"] == -32602


@pytest.mark.unit
def test_the_relay_transport_ends_when_the_client_stops_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relay = _FakeRelay(
        story_relay.Reply(({"type": "text", "text": "t"},), False), full=False
    )
    adapter, _, ledger = _adapter(tmp_path, relay)

    class Gone:
        def write(self, text: str) -> int:
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self) -> None:
            pass

    discarded: list[bool] = []
    monkeypatch.setattr(story, "_discard_stdout", lambda: discarded.append(True))
    read, write = os.pipe()
    os.write(write, b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}\n')
    try:
        code = story._serve_relay(
            adapter,
            ledger,
            relay,
            read=lambda size: os.read(read, size),
            stdout=Gone(),
        )
    finally:
        os.close(write)
        os.close(read)
    assert code == 0 and discarded == [True] and relay.closed
    assert relay.stop is not None and relay.stop() is False
    lines = journal_lines(tmp_path / story.JOURNAL_FILE)
    assert lines[-1]["reason"] == "eof"


@pytest.mark.unit
def test_the_relay_transport_ends_its_journal_even_when_the_relay_cannot_settle(
    tmp_path: Path,
) -> None:
    relay = _FakeRelay(full=True)
    adapter, _, ledger = _adapter(tmp_path, relay)
    feed = iter([b""])
    code = story._serve_relay(
        adapter, ledger, relay, read=lambda size: next(feed), stdout=io.StringIO()
    )
    assert code == 0 and relay.closed
    assert journal_lines(tmp_path / story.JOURNAL_FILE)[-1]["reason"] == "eof"


@pytest.mark.unit
def test_the_ending_note_joins_a_tool_result_and_leaves_an_error_alone() -> None:
    result = {"jsonrpc": "2.0", "id": 1, "result": {"content": [], "isError": True}}
    assert story._ending(result, "end")["result"]["content"] == [
        {"type": "text", "text": "end"}
    ]
    error = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "x"}}
    assert story._ending(error, "end") == error


@pytest.mark.unit
def test_a_child_that_dies_taking_the_screenshot_after_an_action_ends_the_flow(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig({"calls": {"take_screenshot": [{"exit": 4}]}})
    rig.call("take_snapshot")
    reply = rig.call("click", uid=named("snapshot_documents", "button", "Refresh"))
    assert reply_text(reply) == story_relay.BROWSER_GONE_TEXT
    assert [(line["class"], line["ok"], line["label"]) for line in rig.lines()] == [
        ("look", True, None),
        ("action", True, None),
        ("harness", False, "child_died"),
    ]


@pytest.mark.unit
def test_a_child_found_dead_by_the_next_call_ends_the_flow(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    child = rig.relay.child
    assert child is not None
    os.killpg(child.process.pid, signal.SIGKILL)
    wait_until(lambda: not child.alive)
    reply = rig.call("take_snapshot")
    assert reply_text(reply) == story_relay.BROWSER_GONE_TEXT
    assert [(line["class"], line["label"]) for line in rig.lines()] == [
        ("look", None),
        ("harness", "child_died"),
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "screenshot",
    [
        {"error": {"code": -32603, "message": "boom"}},
        {"label": "screenshot_jpeg", "image_bytes": story_relay.SCREEN_BYTES + 1},
        {"result": {"content": [{"type": "text", "text": "no image"}]}},
    ],
    ids=["an-error", "too-large", "no-image"],
)
def test_an_action_whose_screenshot_fails_is_journalled_without_a_screen(
    make_rig: Callable[..., Rig], screenshot: dict[str, Any]
) -> None:
    rig = make_rig({"calls": {"take_screenshot": [screenshot]}})
    rig.call("take_snapshot")
    reply = rig.call("click", uid=named("snapshot_documents", "button", "Refresh"))
    assert not reply.is_error and not images_in(reply)
    assert (
        rig.last()["ok"],
        rig.last()["result"]["screen"],
        rig.last()["images_sent"],
    ) == (
        True,
        None,
        0,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "answer",
    [
        {"result": "garbled"},
        {
            "result": {
                "content": ["x", {"type": "text", "text": "kept"}, {"type": "audio"}]
            }
        },
    ],
    ids=["not-an-object", "odd-items"],
)
def test_an_answer_of_the_wrong_shape_keeps_only_its_text_items(
    make_rig: Callable[..., Rig], answer: dict[str, Any]
) -> None:
    rig = make_rig({"calls": {"wait_for": [answer]}})
    reply = rig.call("wait_for", text=["x"])
    if answer["result"] == "garbled":
        assert reply.is_error and reply.content == ()
    else:
        assert reply_text(reply) == "kept" and not reply.is_error


@pytest.mark.unit
def test_seen_on_screen_answers_for_the_addresses_a_shown_snapshot_held(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    assert not rig.relay.seen_on_screen(UNINDEXED_HELP_LINK)
    rig.call("take_snapshot")
    assert rig.relay.seen_on_screen(f"  {UNINDEXED_HELP_LINK} ")
    assert rig.relay.seen_on_screen(
        "/accelerated-intelligent-document-processing-on-aws/web-ui/#document-list"
    )
    assert not rig.relay.seen_on_screen(f"{DOCS}/guessed-page/")


@pytest.mark.unit
def test_an_address_keeps_its_query_in_its_path_forms() -> None:
    snapshot = story_relay.cut_snapshot(
        ['uid=1_0 link "x" url="https://h.example/a/b?c=1#d"']
    )
    assert {"/a/b?c=1#d", "a/b?c=1#d"} <= snapshot.addresses


@pytest.mark.unit
@pytest.mark.parametrize(
    ("spec", "value", "problem"),
    [
        ({"type": "number"}, "1", "n must be a number"),
        ({"type": "number"}, True, "n must be a number"),
        ({"type": "number"}, 1.5, None),
        ({"type": "object"}, [], "n must be an object"),
        ({"type": "object"}, {}, None),
        ({"type": "array", "items": True}, ["x"], None),
        ({"type": "mystery"}, object(), None),
    ],
)
def test_the_rarer_schema_types_are_read_too(
    spec: dict[str, Any], value: Any, problem: str | None
) -> None:
    _, _, found = story_relay.read_arguments({"properties": {"n": spec}}, {"n": value})
    assert found == problem


@pytest.mark.unit
def test_an_oversized_line_that_arrives_whole_fails_as_size(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(
        {"calls": {"take_snapshot": [{"oversize": 5000}, {"result": {"content": []}}]}}
    )
    child = rig.relay.child
    assert child is not None
    child._limit = 1000
    assert reply_text(rig.call("take_snapshot")) == story_relay.TOO_LARGE_TEXT
    assert not rig.call("take_snapshot").is_error


@pytest.mark.unit
def test_the_refusal_limit_ends_the_transport_in_process(tmp_path: Path) -> None:
    adapter, _, ledger = _adapter(tmp_path, _FakeRelay())
    lines = b"".join(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": n,
                "method": "tools/call",
                "params": {"name": "evaluate_script", "arguments": {}},
            }
        ).encode()
        + b"\n"
        for n in range(1, story_relay.REFUSAL_LIMIT + 3)
    )
    feed = iter([lines, b""])
    out = io.StringIO()
    code = story._serve_relay(
        adapter, ledger, _FakeRelay(), read=lambda size: next(feed), stdout=out
    )
    assert code == 0
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(replies) == story_relay.REFUSAL_LIMIT
    assert journal_lines(tmp_path / story.JOURNAL_FILE)[-1]["reason"] == "refusal_limit"


@pytest.mark.unit
def test_a_stop_signal_while_waiting_ends_the_relay_journal_and_the_child(
    serve: Callable[..., Served],
) -> None:
    server = serve()
    assert server.request(1, "ping")["result"] == {}
    server.proc.send_signal(signal.SIGTERM)
    assert server.proc.wait(timeout=30) == 0
    lines = server.journal()
    assert [line["type"] for line in lines] == ["start", "end"]
    assert lines[-1]["reason"] == "signal"
    boot = records(server.record)[0]
    assert boot["argv"][-1].startswith("--filesystemRoot=")
