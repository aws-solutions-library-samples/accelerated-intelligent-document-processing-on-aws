# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the MR UX test's review relay (``scripts/ux_runner/review_relay.py``).

The review's ``claude`` reaches the browser only through this relay, which puts the
first-time-user stories' commit guard (``story_relay.CommitGuard``) in front of the
pinned chrome-devtools-mcp and otherwise passes calls and answers through as they
are. The browser side is the story relay suite's fake chrome-devtools-mcp
(``fixtures/ux_story_relay/fake_devtools.py``), which answers with what the pinned
1.10.1 answered when it was recorded and records every message it receives, so
"nothing reached the browser" is read from what the fake was sent.

The incident this exists for: a review signed in as Admin on the shared review
stack pressed Process Changes on an unassigned review-queue item, which claims and
finishes the review with no confirmation, and consumed a shared fixture. The tests
under "the incident" replay that click, through the relay object and through the
server exactly as ``agent.mcp_servers`` starts it, and require it refused, logged
and never sent. ``test_ai_ux_test.py`` replays it once more through the orchestrator,
into the job's ``refusals.jsonl``.

What the guard refuses is not restated here: ``test_ux_story_relay.py`` closes the
guard over every mutation the web UI declares (``MUTATION_CONTROLS``), and the
review relay judges with the same class.
"""

from __future__ import annotations

import io
import json
import os
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

import ux_recorder_mcp  # noqa: E402
from ux_runner import (  # noqa: E402
    agent,
    paths,
    review_relay,
    story_relay,
    tool_profiles,
)

FIXTURES = SCRIPTS / "tests" / "fixtures" / "ux_story_relay"
FAKE = FIXTURES / "fake_devtools.py"
RELAY_SCRIPT = SCRIPTS / "ux_runner" / "review_relay.py"
SESSION: dict[str, Any] = json.loads(
    (FIXTURES / "session.json").read_text(encoding="utf-8")
)
TOOLS_LIST: dict[str, Any] = json.loads(
    (FIXTURES / "tools_list.json").read_text(encoding="utf-8")
)
DEFAULT_TOOLS_LIST: dict[str, Any] = json.loads(
    (FIXTURES / "tools_list_default_flags.json").read_text(encoding="utf-8")
)
DEVTOOLS = tool_profiles.CHROME_DEVTOOLS_SERVER
EPOCH = "1970-01-01T00:00:00Z"
PLATFORM_VARIABLES = frozenset(
    {"PWD", "OLDPWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING"}
)

#: The edit page of an unassigned review-queue item as an Admin sees it, laid out as
#: ``components/edit-screen/EditScreen.tsx`` draws its action bar: Process Changes
#: finishes the review, with no confirmation, and claims the item on the way.
QUEUE_ITEM = (
    'uid=1_0 RootWebArea "lending_package.pdf (In review)" '
    'url="http://localhost:3000/#/review-queues/items/lending_package.pdf"',
    '  uid=1_1 navigation "Side navigation"',
    '    uid=1_2 link "Review Management" url="http://localhost:3000/#/review-queues"',
    "  uid=1_3 main",
    '    uid=1_4 heading "lending_package.pdf (In review)" level="1"',
    '    uid=1_5 StaticText "Not assigned yet. Editing it, or choosing Process '
    'Changes, assigns it to you."',
    '    uid=1_6 button "Reports"',
    '    uid=1_7 button "More Actions"',
    '    uid=1_8 button "Refresh"',
    '    uid=1_9 button "Skip"',
    '    uid=1_10 button "Cancel and discard changes"',
    '    uid=1_11 button "Save" disableable disabled',
    '    uid=1_12 button "Process Changes"',
    '    uid=1_13 textbox "Loan amount" value="250000" focusable',
    '    uid=1_14 checkbox "Signed"',
    '    uid=1_15 button "Edit Page Grouping"',
)
PROCESS_CHANGES = "1_12"


def page(*lines: str) -> dict[str, Any]:
    """A scripted answer holding a snapshot of ``lines``, as the server writes one."""
    text = "\n".join([story_relay.SNAPSHOT_HEADING, *lines])
    return {"result": {"content": [{"type": "text", "text": text}]}}


def recorded(label: str) -> dict[str, Any]:
    return SESSION["calls"][label]["answer"]["result"]


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


def wait_until(check: Callable[[], bool], seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.02)


def text_of(answer: review_relay.Answer) -> str:
    assert answer.result is not None, answer
    return "\n".join(
        item["text"] for item in answer.result["content"] if item["type"] == "text"
    )


@dataclass
class Rig:
    relay: review_relay.ReviewRelay
    log: Path
    record: Path
    root: Path

    def call(self, name: str, **arguments: Any) -> review_relay.Answer:
        return self.relay.call(name, arguments)

    def refusals(self) -> list[dict[str, Any]]:
        return records(self.log)

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


def _dirs(base: Path) -> tuple[Path, Path, Path]:
    root = base / "mcp-files"
    home = base / "home"
    guard = base / "guard"
    for directory in (root, home, guard):
        directory.mkdir(mode=0o700)
    return root, home, guard


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    made: list[Rig] = []

    def make(scenario: dict[str, Any] | None = None) -> Rig:
        base = tmp_path / f"review-{len(made)}"
        base.mkdir()
        script, record = fake_devtools(base, scenario or {})
        root, home, guard = _dirs(base)
        log = guard / review_relay.REFUSAL_LOG
        child, listed = story_relay.start_child(
            script,
            home=home,
            fs_root=root,
            search_path=agent.DEFAULT_SEARCH_PATH,
            client_name=review_relay.SERVER_NAME,
        )
        relay = review_relay.ReviewRelay(
            child,
            listed=listed,
            refusals=review_relay.RefusalLog(log, clock=lambda: 0.0),
        )
        rig = Rig(relay, log, record, root)
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.relay.close()
        rig.relay.refusals.close()


def _granted(name: str) -> bool:
    return tool_profiles.is_granted(tool_profiles.mcp_tool(DEVTOOLS, name))


def _served_in_the_child_s_order() -> list[str]:
    return [tool["name"] for tool in TOOLS_LIST["tools"] if _granted(tool["name"])]


# --- the incident ------------------------------------------------------------------


@pytest.mark.unit
def test_process_changes_on_a_queue_item_is_refused_logged_and_never_sent(
    make_rig: Callable[..., Rig],
) -> None:
    """The click that consumed a shared fixture: refused with the review's own
    words, one line in the refusal log, and the browser sent nothing but the
    snapshot the model asked for."""
    rig = make_rig({"calls": {"take_snapshot": [page(*QUEUE_ITEM)]}})
    looked = rig.call("take_snapshot")
    assert looked.result == page(*QUEUE_ITEM)["result"]
    refused = rig.call("click", uid=PROCESS_CHANGES)
    assert refused.error is None and refused.result is not None
    assert refused.result["isError"] is True
    assert text_of(refused) == review_relay.COMMIT_POINT_TEXT
    assert rig.refusals() == [
        {
            "t": EPOCH,
            "tool": "click",
            "refused": "commit_point",
            "label": "Process Changes",
        }
    ]
    assert [call["name"] for call in rig.tool_calls()] == ["take_snapshot"]


@pytest.mark.unit
def test_after_the_refusal_the_review_goes_on_with_what_commits_nothing(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig({"calls": {"take_snapshot": [page(*QUEUE_ITEM)]}})
    rig.call("take_snapshot")
    rig.call("click", uid=PROCESS_CHANGES)
    opened = rig.call("click", uid="1_6")
    assert opened.result == recorded("click_refresh")
    looked = rig.call("take_screenshot")
    assert looked.result is not None and "isError" not in looked.result
    assert [item["type"] for item in looked.result["content"]] == ["text", "image"]
    assert [call["name"] for call in rig.tool_calls()] == [
        "take_snapshot",
        "click",
        "take_screenshot",
    ]
    assert rig.tool_calls()[1] == {"name": "click", "arguments": {"uid": "1_6"}}
    assert len(rig.refusals()) == 1


@pytest.mark.unit
def test_every_control_on_the_queue_item_that_would_commit_is_refused(
    make_rig: Callable[..., Rig],
) -> None:
    """Skip and Save by name, and every key but Tab and Escape, every edit and Edit
    Page Grouping, because the item's editor saves a draft by itself and its first
    save claims the item as surely as Process Changes does, and so does opening the
    grouping board. Tab still moves the focus."""
    rig = make_rig({"calls": {"take_snapshot": [page(*QUEUE_ITEM)]}})
    rig.call("take_snapshot")
    calls: list[tuple[str, dict[str, Any]]] = [
        ("click", {"uid": "1_9"}),
        ("click", {"uid": "1_11"}),
        ("press_key", {"key": "Enter"}),
        ("type_text", {"text": " "}),
        ("fill", {"uid": "1_13", "value": "1"}),
        ("fill_form", {"elements": [{"uid": "1_13", "value": "1"}]}),
        ("type_text", {"text": "1"}),
        ("drag", {"from_uid": "1_13", "to_uid": "1_14"}),
        ("click", {"uid": "1_14"}),
        ("click", {"uid": "1_15"}),
    ]
    for name, arguments in calls:
        answer = rig.relay.call(name, arguments)
        assert text_of(answer) == review_relay.COMMIT_POINT_TEXT, (name, arguments)
    editor = "Cancel and discard changes"
    assert [(line["tool"], line["label"]) for line in rig.refusals()] == [
        ("click", "Skip"),
        ("click", "Save"),
        ("press_key", editor),
        ("type_text", editor),
        ("fill", editor),
        ("fill_form", editor),
        ("type_text", editor),
        ("drag", editor),
        ("click", editor),
        ("click", "Edit Page Grouping"),
    ]
    assert {line["refused"] for line in rig.refusals()} == {"commit_point"}
    assert rig.call("press_key", key="Tab").result == recorded("press_key")
    assert [call["name"] for call in rig.tool_calls()] == ["take_snapshot", "press_key"]


@pytest.mark.unit
def test_the_server_as_the_mcp_config_starts_it_refuses_process_changes(
    tmp_path: Path,
) -> None:
    """The whole path the review's ``claude`` takes: the ``chrome-devtools`` entry
    ``agent.mcp_servers`` writes, run as it is written, under ``env -i`` and
    ``python3 -I -B``, spoken to over stdio."""
    script, record = fake_devtools(
        tmp_path, {"calls": {"take_snapshot": [page(*QUEUE_ITEM)]}}
    )
    root, home, guard = _dirs(tmp_path)
    log = guard / review_relay.REFUSAL_LOG
    entry = agent.mcp_servers(
        toolchain=paths.Toolchain(tmp_path / "toolchain"),
        relay=RELAY_SCRIPT,
        refusal_log=log,
        recorder_adapter=tmp_path / "unused",
        recorder_out=tmp_path / "unused",
        python=sys.executable,
        home=home,
        fs_root=root,
    )[DEVTOOLS]
    args = list(entry["args"])
    args[args.index("--devtools") + 1] = str(script)
    cwd = tmp_path / "cwd"
    cwd.mkdir(mode=0o700)
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
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
            "params": {"name": "click", "arguments": {"uid": PROCESS_CHANGES}},
        },
    ]
    done = subprocess.run(
        [entry["command"], *args],
        input="".join(json.dumps(message) + "\n" for message in messages),
        capture_output=True,
        text=True,
        cwd=cwd,
        env={},
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    replies = {
        reply["id"]: reply for reply in map(json.loads, done.stdout.splitlines())
    }
    assert set(replies) == {1, 2, 3, 4}
    assert replies[1]["result"]["serverInfo"]["name"] == review_relay.SERVER_NAME
    assert [tool["name"] for tool in replies[2]["result"]["tools"]] == (
        _served_in_the_child_s_order()
    )
    assert replies[3]["result"] == page(*QUEUE_ITEM)["result"]
    assert replies[4]["result"]["isError"] is True
    assert replies[4]["result"]["content"] == [
        {"type": "text", "text": review_relay.COMMIT_POINT_TEXT}
    ]
    (line,) = records(log)
    assert (line["tool"], line["refused"], line["label"]) == (
        "click",
        "commit_point",
        "Process Changes",
    )
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    sent = [entry["in"] for entry in records(record) if "in" in entry]
    assert [m["params"]["name"] for m in sent if m.get("method") == "tools/call"] == [
        "take_snapshot"
    ]
    boot = records(record)[0]
    assert boot["argv"] == [*agent.DEVTOOLS_FLAGS, f"--filesystemRoot={root}"]
    environ = {k: v for k, v in boot["environ"].items() if k not in PLATFORM_VARIABLES}
    assert environ == {
        "PATH": agent.DEFAULT_SEARCH_PATH,
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS": "1",
    }
    assert Path(boot["cwd"]).resolve() == root.resolve()
    assert sent[0]["params"]["capabilities"] == {}
    assert sent[0]["params"]["clientInfo"]["name"] == review_relay.SERVER_NAME


# --- the tool surface --------------------------------------------------------------


@pytest.mark.unit
def test_the_relay_serves_what_expert_grants_exactly_as_the_child_lists_it(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    listed = [tool for tool in TOOLS_LIST["tools"] if _granted(tool["name"])]
    assert rig.relay.tools() == listed
    assert sorted(tool["name"] for tool in listed) == sorted(
        tool_profiles.STORY_BROWSER_TOOLS
    )
    expert = {
        tool.removeprefix(f"mcp__{DEVTOOLS}__")
        for tool in tool_profiles.EXPERT_ALLOWED_TOOLS
        if tool.startswith(f"mcp__{DEVTOOLS}__")
    }
    assert {tool["name"] for tool in rig.relay.tools()} == expert
    assert all(rig.relay.serves(name) for name in expert)
    assert not rig.relay.serves("evaluate_script")


@pytest.mark.unit
def test_no_tool_expert_denies_is_served_even_where_the_child_lists_it() -> None:
    """Under default flags the child lists all thirty tools, and the relay still
    serves only the twelve EXPERT grants: a tool that runs page script, opens a
    second tab or reads the network log is not served whatever Claude Code's own
    lists come to say."""
    served = review_relay.served_tools(DEFAULT_TOOLS_LIST)
    assert len(DEFAULT_TOOLS_LIST["tools"]) == len(tool_profiles.DEVTOOLS_TOOLS_AT_PIN)
    assert sorted(served) == sorted(tool_profiles.STORY_BROWSER_TOOLS)
    for name in tool_profiles.DEVTOOLS_TOOLS_AT_PIN:
        if name not in served:
            assert tool_profiles.mcp_tool(DEVTOOLS, name) in (
                tool_profiles.EXPERT_DISALLOWED_TOOLS
            )


_FIRST_CLICK = {"name": "click", "inputSchema": {"type": "object"}, "x": 1}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("listed", "served"),
    [
        (None, {}),
        ({"tools": "click"}, {}),
        ({"tools": ["click", {"name": 3}, {"name": "click"}]}, {}),
        (
            {
                "tools": [
                    _FIRST_CLICK,
                    {"name": "click", "inputSchema": {"type": "object"}, "x": 2},
                ]
            },
            {"click": _FIRST_CLICK},
        ),
    ],
)
def test_a_listing_of_the_wrong_shape_serves_only_its_well_formed_first_entries(
    listed: object, served: dict[str, Any]
) -> None:
    assert review_relay.served_tools(listed) == served


@pytest.mark.unit
def test_an_answer_the_guard_lets_through_is_the_child_s_own(
    make_rig: Callable[..., Rig],
) -> None:
    """No screenshot is attached, no snapshot cut and no argument stripped: a long
    page arrives whole, and an element past the story relay's cut is clickable."""
    rig = make_rig(
        {
            "calls": {
                "take_snapshot": [{"label": "snapshot_long"}],
                "take_screenshot": [{"label": "screenshot_file_path"}],
            }
        }
    )
    looked = rig.call("take_snapshot", verbose=False)
    assert looked.result == recorded("snapshot_long")
    assert "Snapshot cut" not in text_of(looked)
    assert rig.relay.snapshot is not None
    last = rig.relay.snapshot.nodes[-1].uid
    assert rig.relay.snapshot.total > story_relay.SNAPSHOT_NODES
    assert rig.call("click", uid=last).result == recorded("click_refresh")
    rig.call("take_screenshot", filePath="shot.png", format="png")
    assert rig.tool_calls()[1:] == [
        {"name": "click", "arguments": {"uid": last}},
        {
            "name": "take_screenshot",
            "arguments": {"filePath": "shot.png", "format": "png"},
        },
    ]
    assert rig.tool_calls()[0] == {
        "name": "take_snapshot",
        "arguments": {"verbose": False},
    }
    assert rig.refusals() == []


# --- what the guard reads from the answers it forwards -----------------------------


@pytest.mark.unit
def test_a_confirm_the_answer_reports_is_never_accepted_and_is_dismissed(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(
        {
            "calls": {
                "take_snapshot": [{"label": "snapshot_dialogs"}],
                "click": [{"label": "click_discard"}],
                "handle_dialog": [{"label": "handle_confirm_dismiss"}],
            }
        }
    )
    rig.call("take_snapshot")
    snapshot = rig.relay.snapshot
    assert snapshot is not None
    (discard,) = [n.uid for n in snapshot.nodes if n.name == "Discard changes"]
    rig.call("click", uid=discard)
    assert rig.relay.dialog == (
        "confirm",
        "Discard the changes you made to this document?",
    )
    refused = rig.call("handle_dialog", action="accept")
    assert text_of(refused) == review_relay.COMMIT_POINT_TEXT
    assert rig.call("handle_dialog", action="dismiss").result == recorded(
        "handle_confirm_dismiss"
    )
    assert rig.relay.dialog is None
    assert [(line["tool"], line["refused"]) for line in rig.refusals()] == [
        ("handle_dialog", "commit_point")
    ]


@pytest.mark.unit
def test_a_uid_from_no_snapshot_the_model_was_shown_is_refused_as_invalid(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig({"calls": {"take_snapshot": [page(*QUEUE_ITEM)]}})
    before = rig.call("click", uid="1_6")
    assert text_of(before) == story_relay.STALE_UID_TEXT
    rig.call("take_snapshot")
    assert text_of(rig.call("click", uid="9_9")) == story_relay.STALE_UID_TEXT
    assert text_of(rig.call("handle_dialog", action="accept")) == (
        story_relay.NO_DIALOG_TEXT
    )
    assert [
        (line["tool"], line["refused"], line["label"]) for line in rig.refusals()
    ] == [
        ("click", "invalid", None),
        ("click", "invalid", None),
        ("handle_dialog", "invalid", None),
    ]
    assert [call["name"] for call in rig.tool_calls()] == ["take_snapshot"]


@pytest.mark.unit
def test_a_control_relabelled_since_the_snapshot_is_judged_by_its_new_name(
    make_rig: Callable[..., Rig],
) -> None:
    """A navigation answer holds no snapshot, so the next click reads the page
    again first, and that read is the guard's own."""
    wizard = (
        'uid=1_0 RootWebArea "Test sets"',
        '  uid=6_0 dialog "Create or update test set" modal',
        '    uid=6_3 button "{label}"',
    )
    rig = make_rig(
        {
            "calls": {
                "take_snapshot": [
                    page(*(line.format(label="Next") for line in wizard)),
                    page(*(line.format(label="Create") for line in wizard)),
                ]
            }
        }
    )
    rig.call("take_snapshot")
    rig.call("navigate_page", type="reload")
    assert text_of(rig.call("click", uid="6_3")) == review_relay.COMMIT_POINT_TEXT
    assert [call["name"] for call in rig.tool_calls()] == [
        "take_snapshot",
        "navigate_page",
        "take_snapshot",
    ]
    assert rig.refusals()[0]["label"] == "Create"


@pytest.mark.unit
def test_a_screenshot_leaves_the_page_as_the_latest_snapshot_showed_it(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig({"calls": {"take_snapshot": [page(*QUEUE_ITEM)]}})
    rig.call("take_snapshot")
    rig.call("take_screenshot")
    rig.call("click", uid="1_6")
    assert [call["name"] for call in rig.tool_calls()] == [
        "take_snapshot",
        "take_screenshot",
        "click",
    ]


# --- failures ------------------------------------------------------------------------


@pytest.mark.unit
def test_a_json_rpc_error_from_the_child_is_forwarded_as_it_is(
    make_rig: Callable[..., Rig],
) -> None:
    error = {"code": -32602, "message": "MCP error -32602: bad uid", "data": [1]}
    rig = make_rig({"calls": {"wait_for": [{"error": error}, {"error": "nope"}]}})
    assert rig.call("wait_for", text=["x"]).error == error
    assert rig.call("wait_for", text=["x"]).error == {
        "code": ux_recorder_mcp.INTERNAL_ERROR,
        "message": "chrome-devtools-mcp answered with an error of no known shape",
    }


@pytest.mark.unit
def test_a_result_that_is_not_an_object_is_an_error(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig({"calls": {"wait_for": [{"result": "done"}]}})
    answer = rig.call("wait_for", text=["x"])
    assert answer.result is None
    assert answer.error is not None
    assert answer.error["code"] == ux_recorder_mcp.INTERNAL_ERROR


@pytest.mark.unit
def test_a_call_the_child_never_answers_times_out(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig({"calls": {"wait_for": [{"hang": True}]}})
    rig.relay.call_seconds = 0.3
    assert text_of(rig.call("wait_for", text=["x"])) == story_relay.TIMED_OUT_TEXT
    wait_until(
        lambda: any(
            m.get("method") == "notifications/cancelled" for m in rig.received()
        )
    )


@pytest.mark.unit
def test_a_cancelled_call_stops_waiting_on_the_child(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig({"calls": {"wait_for": [{"hang": True}]}})
    rig.relay.begin()
    threading.Timer(0.2, rig.relay.cancel).start()
    assert text_of(rig.call("wait_for", text=["x"])) == story_relay.CANCELLED_TEXT


@pytest.mark.unit
def test_an_answer_over_the_result_bound_is_refused_as_too_large(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(story_relay, "RESULT_BYTES", 200_000)
    rig = make_rig(
        {"calls": {"wait_for": [{"oversize": 300_000}, {"label": "wait_for"}]}}
    )
    assert text_of(rig.call("wait_for", text=["x"])) == story_relay.TOO_LARGE_TEXT
    assert rig.call("wait_for", text=["x"]).result == recorded("wait_for")


@pytest.mark.unit
def test_a_child_that_dies_leaves_every_later_call_answered_as_gone(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig({"calls": {"wait_for": [{"exit": 3}]}})
    for _ in range(2):
        answer = rig.call("wait_for", text=["x"])
        assert text_of(answer) == review_relay.BROWSER_GONE_TEXT
    assert rig.relay.over == "child_died"
    assert rig.refusals() == []


@pytest.mark.unit
def test_a_page_that_cannot_be_read_again_refuses_the_click(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(
        {
            "calls": {
                "take_snapshot": [page(*QUEUE_ITEM), {"exit": 3}],
            }
        }
    )
    rig.call("take_snapshot")
    rig.call("navigate_page", type="reload")
    assert text_of(rig.call("click", uid="1_6")) == review_relay.BROWSER_GONE_TEXT
    assert rig.refusals()[0]["refused"] == "invalid"


@pytest.mark.unit
def test_a_child_that_never_started_serves_nothing(tmp_path: Path) -> None:
    log = review_relay.RefusalLog(tmp_path / review_relay.REFUSAL_LOG)
    relay = review_relay.ReviewRelay(None, listed={}, refusals=log)
    assert relay.tools() == [] and not relay.serves("click")
    gone = relay.call("click", {"uid": "1_1"})
    assert text_of(gone) == review_relay.BROWSER_GONE_TEXT
    relay.close()
    log.close()
    assert records(tmp_path / review_relay.REFUSAL_LOG) == []


# --- the refusal log -----------------------------------------------------------------


@pytest.mark.unit
def test_the_refusal_log_is_created_private_and_never_over_anything(
    tmp_path: Path,
) -> None:
    path = tmp_path / review_relay.REFUSAL_LOG
    log = review_relay.RefusalLog(path, clock=lambda: 86400.0)
    log.record(tool="click", refused="commit_point", label="Delete\n\u202e\tnow  " * 40)
    log.record(tool="click", refused="invalid", label=None)
    log.close()
    log.record(tool="click", refused="commit_point", label="after close")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    first, second = records(path)
    assert first["t"] == "1970-01-02T00:00:00Z"
    assert len(first["label"]) <= review_relay.LABEL_CHARS
    assert "\n" not in first["label"] and "\u202e" not in first["label"]
    assert second == {
        "t": "1970-01-02T00:00:00Z",
        "tool": "click",
        "refused": "invalid",
        "label": None,
    }
    with pytest.raises(FileExistsError):
        review_relay.RefusalLog(path)
    link = tmp_path / "link.jsonl"
    link.symlink_to(tmp_path / "elsewhere.jsonl")
    with pytest.raises(OSError):
        review_relay.RefusalLog(link)
    assert not (tmp_path / "elsewhere.jsonl").exists()


@pytest.mark.unit
def test_the_refusal_log_stops_at_its_bound_and_at_a_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(review_relay, "REFUSALS_KEPT", 2)
    log = review_relay.RefusalLog(tmp_path / "a.jsonl")
    for label in ("one", "two", "three"):
        log.record(tool="click", refused="commit_point", label=label)
    assert [line["label"] for line in records(tmp_path / "a.jsonl")] == ["one", "two"]
    log.close()
    broken = review_relay.RefusalLog(tmp_path / "b.jsonl")

    def refuse(descriptor: int, data: Any) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(review_relay.os, "write", refuse)
    broken.record(tool="click", refused="commit_point", label="lost")
    monkeypatch.undo()
    broken.record(tool="click", refused="commit_point", label="after")
    assert records(tmp_path / "b.jsonl") == []
    assert broken.written == 0


@pytest.mark.unit
def test_the_refusal_log_reads_back_as_this_process_s_own_file_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / review_relay.REFUSAL_LOG
    assert review_relay.read_refusals(path) == []
    path.write_text('{"tool": "click"}\nnot json\n[1]\n{"tool": "fill"}\n{"to')
    assert review_relay.read_refusals(path) == [{"tool": "click"}, {"tool": "fill"}]
    monkeypatch.setattr(review_relay, "REFUSALS_KEPT", 1)
    assert review_relay.read_refusals(path) == [{"tool": "click"}]
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / review_relay.REFUSAL_LOG).symlink_to(path)
    with pytest.raises(review_relay.GuardLogError, match="symbolic link"):
        review_relay.read_refusals(linked / review_relay.REFUSAL_LOG)
    monkeypatch.setattr(review_relay, "MAX_REFUSAL_LOG_BYTES", 8)
    with pytest.raises(review_relay.GuardLogError, match="over its bound"):
        review_relay.read_refusals(path)


@pytest.mark.unit
def test_a_refusal_lists_in_refusals_jsonl_with_the_guard_s_origin() -> None:
    record = {"t": EPOCH, "tool": "click", "refused": "commit_point", "label": "x"}
    assert review_relay.with_origin(record) == {**record, "origin": "guard"}
    assert review_relay.REFUSAL_ORIGIN not in ("page", "browser", "selftest")


# --- the server ----------------------------------------------------------------------


class _StubRelay:
    """Enough of a ReviewRelay for the protocol layer, with nothing behind it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.began = 0
        self.cancelled = 0
        self.answer = review_relay.Answer(result={"content": [], "isError": False})
        self.raises: Exception | None = None
        self.stop: Callable[[], bool] = lambda: False

    def tools(self) -> list[dict[str, Any]]:
        return [{"name": "click", "inputSchema": {"type": "object"}}]

    def serves(self, name: str) -> bool:
        return name == "click"

    def call(self, name: str, arguments: dict[str, Any]) -> review_relay.Answer:
        self.calls.append((name, arguments))
        if self.raises is not None:
            raise self.raises
        return self.answer

    def begin(self) -> None:
        self.began += 1

    def cancel(self) -> None:
        self.cancelled += 1

    def stop_when(self, stop: Callable[[], bool]) -> None:
        self.stop = stop


def _rpc(relay: Any, method: str, params: Any = None, **extra: Any) -> Any:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": 9, "method": method, **extra}
    if params is not None:
        message["params"] = params
    return review_relay.handle(relay, message)


@pytest.mark.unit
def test_initialize_answers_the_client_s_version_when_it_is_one_it_knows() -> None:
    relay = _StubRelay()
    known = _rpc(relay, "initialize", {"protocolVersion": "2025-06-18"})
    assert known["result"] == {
        "protocolVersion": "2025-06-18",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {
            "name": review_relay.SERVER_NAME,
            "version": review_relay.SERVER_VERSION,
        },
    }
    other = _rpc(relay, "initialize", {"protocolVersion": "1999-01-01"})
    assert other["result"]["protocolVersion"] == ux_recorder_mcp.PROTOCOL_VERSIONS[-1]
    assert _rpc(relay, "ping")["result"] == {}
    assert _rpc(relay, "tools/list")["result"] == {"tools": relay.tools()}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("message", "code"),
    [
        ([1], ux_recorder_mcp.INVALID_REQUEST),
        ({"id": True, "method": "ping"}, ux_recorder_mcp.INVALID_REQUEST),
        ({"id": 1}, ux_recorder_mcp.INVALID_REQUEST),
        ({"id": 1, "method": "ping", "params": [1]}, ux_recorder_mcp.INVALID_PARAMS),
        ({"id": 1, "method": "resources/list"}, ux_recorder_mcp.METHOD_NOT_FOUND),
        (
            {"id": 1, "method": "tools/call", "params": {"name": "evaluate_script"}},
            ux_recorder_mcp.INVALID_PARAMS,
        ),
        (
            {"id": 1, "method": "tools/call", "params": {"name": 3}},
            ux_recorder_mcp.INVALID_PARAMS,
        ),
        (
            {
                "id": 1,
                "method": "tools/call",
                "params": {"name": "click", "arguments": [1]},
            },
            ux_recorder_mcp.INVALID_PARAMS,
        ),
    ],
)
def test_a_malformed_request_is_answered_with_its_error_and_reaches_no_tool(
    message: Any, code: int
) -> None:
    relay = _StubRelay()
    reply = review_relay.handle(relay, message)
    assert reply is not None and reply["error"]["code"] == code
    assert relay.calls == []


@pytest.mark.unit
def test_a_notification_is_not_answered_and_a_call_is_answered_as_the_relay_said() -> (
    None
):
    relay = _StubRelay()
    assert review_relay.handle(relay, {"method": "notifications/initialized"}) is None
    reply = _rpc(relay, "tools/call", {"name": "click"})
    assert reply == {"jsonrpc": "2.0", "id": 9, "result": relay.answer.result}
    assert relay.calls == [("click", {})]
    relay.answer = review_relay.Answer(error={"code": -1, "message": "x"})
    assert _rpc(relay, "tools/call", {"name": "click", "arguments": {}}) == {
        "jsonrpc": "2.0",
        "id": 9,
        "error": {"code": -1, "message": "x"},
    }


@pytest.mark.unit
def test_an_exception_while_handling_a_call_is_an_internal_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    relay = _StubRelay()
    relay.raises = RuntimeError("guard broke")
    reply = _rpc(relay, "tools/call", {"name": "click", "arguments": {"uid": "1"}})
    assert reply["error"] == {
        "code": ux_recorder_mcp.INTERNAL_ERROR,
        "message": "Internal error: RuntimeError",
    }
    assert "guard broke" in capsys.readouterr().err


def _client(
    relay: Any, chunks: list[bytes], *, limit: int = 4096
) -> tuple[review_relay.Client, io.StringIO]:
    stream = io.StringIO()
    feed = iter(chunks)
    client = review_relay.Client(
        lambda size: next(feed, b""), stream, relay, limit=limit
    )
    return client, stream


def _lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


@pytest.mark.unit
def test_the_client_reader_answers_ping_and_parse_errors_itself_and_queues_the_rest() -> (
    None
):
    relay = _StubRelay()
    client, stream = _client(
        relay,
        [
            b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n\nnot json\n',
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
            b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
        ],
    )
    client._run()
    assert _lines(stream) == [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": ux_recorder_mcp.PARSE_ERROR, "message": "Parse error"},
        },
    ]
    queued = [client.messages.get_nowait() for _ in range(client.messages.qsize())]
    assert queued == [
        ("message", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ("eof", None),
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "chunks",
    [[b"x" * 40 + b"\n"], [b"x" * 20, b"x" * 30, b"\n"], [b"x" * 60]],
)
def test_an_oversized_line_is_one_size_item_however_it_arrives(
    chunks: list[bytes],
) -> None:
    client, _ = _client(
        _StubRelay(), [*chunks, b'{"id":1,"method":"ping"}\n'], limit=32
    )
    client._run()
    queued = [client.messages.get_nowait() for _ in range(client.messages.qsize())]
    assert queued[0] == ("size", None)
    assert queued[-1] == ("eof", None)


@pytest.mark.unit
def test_a_cancellation_stops_the_request_being_handled_or_drops_a_queued_one() -> None:
    relay = _StubRelay()
    client, _ = _client(relay, [])
    assert client.begin(5)
    client._cancel(5)
    assert relay.cancelled == 1 and client.finish() is True
    client._cancel("queued")
    client._cancel(True)
    assert client.begin("queued") is False
    assert client.begin("queued") is True and client.finish() is False


@pytest.mark.unit
def test_a_cancellation_notice_reaches_the_request_it_names() -> None:
    relay = _StubRelay()
    client, _ = _client(relay, [])
    client.begin(7)
    client._take(
        b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":7}}'
    )
    client._take(b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":1}')
    assert relay.cancelled == 1


@pytest.mark.unit
@pytest.mark.parametrize("request_id", ["a-1", None, 0])
def test_a_request_id_is_a_string_an_integer_or_null(request_id: Any) -> None:
    reply = review_relay.handle(
        _StubRelay(), {"jsonrpc": "2.0", "id": request_id, "method": "ping"}
    )
    assert reply == {"jsonrpc": "2.0", "id": request_id, "result": {}}


@pytest.mark.unit
def test_a_client_that_cannot_be_read_is_one_that_has_gone() -> None:
    def broken(size: int) -> bytes:
        raise OSError("bad descriptor")

    client = review_relay.Client(broken, io.StringIO(), _StubRelay())
    client._run()
    assert client.messages.get_nowait() == ("eof", None)


@pytest.mark.unit
def test_replies_stop_once_the_client_has_gone() -> None:
    class Gone(io.StringIO):
        def write(self, text: str) -> int:
            raise BrokenPipeError

    client = review_relay.Client(lambda size: b"", Gone(), _StubRelay())
    client.send({"id": 1})
    assert client.broken
    client.send({"id": 2})


def _pipe() -> tuple[Callable[[int], bytes], Callable[[Any], None], Callable[[], None]]:
    reading, writing = os.pipe()

    def read(size: int) -> bytes:
        return os.read(reading, size)

    def send(message: Any) -> None:
        os.write(writing, (json.dumps(message) + "\n").encode())

    def close() -> None:
        os.close(writing)

    return read, send, close


class _Stream(io.StringIO):
    """A reply stream a test can wait on, written from two threads."""

    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()

    def write(self, text: str) -> int:
        with self.lock:
            return super().write(text)

    def replies(self) -> list[dict[str, Any]]:
        with self.lock:
            return [json.loads(line) for line in self.getvalue().splitlines()]


@pytest.mark.unit
def test_a_cancellation_during_a_browser_call_reaches_it_and_goes_unanswered(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig({"calls": {"wait_for": [{"hang": True}]}})
    read, send, close = _pipe()
    stream = _Stream()
    client = review_relay.Client(read, stream, rig.relay)
    stopping = threading.Event()
    server = threading.Thread(
        target=review_relay.serve,
        args=(rig.relay, client),
        kwargs={"stopping": stopping},
    )
    server.start()
    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "wait_for", "arguments": {"text": ["x"]}},
            }
        )
        wait_until(lambda: any(c["name"] == "wait_for" for c in rig.tool_calls()))
        send({"jsonrpc": "2.0", "id": 8, "method": "ping"})
        wait_until(lambda: [r["id"] for r in stream.replies()] == [8])
        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 7},
            }
        )
        wait_until(
            lambda: any(
                m.get("method") == "notifications/cancelled" for m in rig.received()
            )
        )
        send({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
        wait_until(lambda: [r["id"] for r in stream.replies()] == [8, 9])
    finally:
        close()
        server.join(timeout=10)
    assert not server.is_alive()
    assert [r["id"] for r in stream.replies()] == [8, 9]


@pytest.mark.unit
def test_a_request_cancelled_before_it_starts_is_dropped_and_oversize_answered() -> (
    None
):
    relay = _StubRelay()
    read, send, close = _pipe()
    stream = _Stream()
    client = review_relay.Client(read, stream, relay, limit=200)
    stopping = threading.Event()
    send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 3},
        }
    )
    send(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "click"}}
    )
    send(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {"x": "y" * 300}}
    )
    send(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "click"}}
    )
    close()
    review_relay.serve(relay, client, stopping=stopping)
    replies = stream.replies()
    assert [r["id"] for r in replies] == [None, 5]
    assert replies[0]["error"]["code"] == ux_recorder_mcp.INVALID_REQUEST
    assert relay.calls == [("click", {})]


@pytest.mark.unit
def test_a_stop_ends_the_server_and_the_wait_on_the_browser() -> None:
    relay = _StubRelay()
    read, _, close = _pipe()
    client = review_relay.Client(read, _Stream(), relay)
    stopping = threading.Event()
    threading.Timer(0.2, stopping.set).start()
    try:
        review_relay.serve(relay, client, stopping=stopping)
    finally:
        close()
    assert relay.stop() is True


@pytest.mark.unit
def test_a_stop_signal_sets_the_stop_and_the_handlers_are_put_back() -> None:
    before = signal.getsignal(signal.SIGTERM)
    stopping = threading.Event()
    previous = review_relay._catch_signals(stopping)
    assert set(previous) == {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}
    try:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert stopping.is_set()
    finally:
        review_relay._restore_signals(previous)
    assert signal.getsignal(signal.SIGTERM) == before
    review_relay._restore_signals({signal.SIGHUP: None})
    assert signal.getsignal(signal.SIGHUP) == signal.SIG_DFL
    signal.signal(signal.SIGHUP, previous[signal.SIGHUP])


# --- main ----------------------------------------------------------------------------


@pytest.mark.unit
def test_main_serves_the_review_until_eof(tmp_path: Path) -> None:
    script, record = fake_devtools(
        tmp_path, {"calls": {"take_snapshot": [page(*QUEUE_ITEM)]}}
    )
    root, home, guard = _dirs(tmp_path)
    log = guard / review_relay.REFUSAL_LOG
    read, send, close = _pipe()
    stream = _Stream()
    send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    send(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "take_snapshot"},
        }
    )
    send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "click", "arguments": {"uid": PROCESS_CHANGES}},
        }
    )
    close()
    code = review_relay.main(
        [
            "--devtools",
            str(script),
            "--filesystem-root",
            str(root),
            "--refusals",
            str(log),
        ],
        environ={"PATH": agent.DEFAULT_SEARCH_PATH, "HOME": str(home)},
        read=read,
        stream=stream,
    )
    assert code == 0
    replies = {reply["id"]: reply for reply in stream.replies()}
    assert len(replies[1]["result"]["tools"]) == 12
    assert replies[3]["result"]["content"][0]["text"] == review_relay.COMMIT_POINT_TEXT
    assert [line["label"] for line in records(log)] == ["Process Changes"]
    assert records(record)[0]["argv"][-1] == f"--filesystemRoot={root}"


@pytest.mark.unit
def test_main_refuses_a_refusal_log_that_already_exists_before_the_browser_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script, record = fake_devtools(tmp_path, {})
    root, home, guard = _dirs(tmp_path)
    log = guard / review_relay.REFUSAL_LOG
    log.write_text("planted\n")
    code = review_relay.main(
        [
            "--devtools",
            str(script),
            "--filesystem-root",
            str(root),
            "--refusals",
            str(log),
        ],
        environ={"PATH": agent.DEFAULT_SEARCH_PATH, "HOME": str(home)},
        read=lambda size: b"",
        stream=_Stream(),
    )
    assert code == 1
    assert "--refusals" in capsys.readouterr().err
    assert not record.exists()
    assert log.read_text() == "planted\n"


@pytest.mark.unit
def test_main_with_a_browser_that_cannot_start_serves_no_tool(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, home, guard = _dirs(tmp_path)
    read, send, close = _pipe()
    stream = _Stream()
    send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    send(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "click", "arguments": {"uid": "1_1"}},
        }
    )
    close()
    code = review_relay.main(
        [
            "--devtools",
            str(tmp_path / "absent"),
            "--filesystem-root",
            str(root),
            "--refusals",
            str(guard / review_relay.REFUSAL_LOG),
        ],
        environ={},
        read=read,
        stream=stream,
    )
    assert code == 0
    replies = {reply["id"]: reply for reply in stream.replies()}
    assert replies[1]["result"] == {"tools": []}
    assert replies[2]["error"]["code"] == ux_recorder_mcp.INVALID_PARAMS
    assert "the browser could not start" in capsys.readouterr().err
