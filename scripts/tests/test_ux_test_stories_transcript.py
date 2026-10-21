# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for ``ux_runner/transcript.py`` against sessions recorded at the pin.

``fixtures/ux_story_transcript/`` holds three session files the pinned Claude Code
TUI wrote, offline, against a fake Messages API (``record.py`` re-records them),
and ``probe.json``, what that API served and was sent. The reader is pinned to
them: who typed, what each session used, and that nothing of a transcript's text
leaves the reader. Two facts about the pin that the stories' local modes rest on
are pinned beside it: the pair command's flags (``--tools=`` leaves no built-in
tool, ``--append-system-prompt-file`` reaches the system prompt) and that an
opening message given as the TUI's argument is answered before its MCP server's
tools are offered, which is why a pair session's opening message is typed.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parent
FIXTURES = SCRIPTS / "tests" / "fixtures" / "ux_story_transcript"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import transcript  # noqa: E402

SESSIONS = ("unassisted", "assisted", "positional")
TYPED = "KEEP_GOING: a message a person typed after the opening one."
PERSON = {"origin": {"kind": "human"}, "promptSource": "typed"}


def _probe() -> dict[str, Any]:
    return json.loads((FIXTURES / "probe.json").read_text(encoding="utf-8"))


def _served(session: str) -> dict[str, int]:
    total: dict[str, int] = {}
    for reply in _probe()["served"][session]:
        for key, value in reply["usage"].items():
            total[key] = total.get(key, 0) + value
    return total


def _line(entry: dict[str, Any]) -> str:
    return json.dumps(entry, separators=(",", ":"))


def _user(content: Any, **fields: Any) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": content}, **fields}


def _assistant(
    ident: str, model: str = "claude-opus-5-5", **usage: int
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"id": ident, "role": "assistant", "model": model, "usage": usage},
    }


@pytest.mark.unit
def test_the_recordings_are_of_the_pinned_claude_code() -> None:
    pinned = json.loads((SCRIPTS / "ux_runner" / "package.json").read_text())
    version = pinned["dependencies"]["@anthropic-ai/claude-code"]
    assert _probe()["claude_code"] == version
    for session in SESSIONS:
        read = transcript.read_transcript_file(FIXTURES / f"{session}.jsonl")
        assert read.version == version, (
            f"{session}.jsonl was recorded at {read.version}; re-record it with "
            "record.py at the pinned release"
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("session", "people", "results", "messages"),
    [("unassisted", 1, 1, 2), ("assisted", 2, 1, 3), ("positional", 1, 0, 1)],
)
def test_each_recording_reads_as_who_typed_and_what_it_used(
    session: str, people: int, results: int, messages: int
) -> None:
    read = transcript.read_transcript_file(FIXTURES / f"{session}.jsonl")
    assert read.found
    assert read.user_messages == people
    assert read.assisted is (people > 1)
    assert read.tool_results == results
    assert read.assistant_messages == messages
    assert read.usage == _served(session)
    assert read.models == ("claude-opus-5-5",)
    assert read.unreadable_lines == 0


@pytest.mark.unit
def test_the_cli_cost_is_kept_only_where_the_session_ended_through_exit() -> None:
    """The CLI's own figures count its side requests too (the session's title, on
    whichever model it picks), so they are at least the assistant messages' sum."""
    assisted = transcript.read_transcript_file(FIXTURES / "assisted.jsonl")
    assert assisted.cli_cost_usd is not None and assisted.cli_cost_usd > 0
    assert assisted.usage is not None
    totals: dict[str, int] = {}
    for counts in assisted.model_usage.values():
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
    assert set(totals) == set(transcript.USAGE_KEYS)
    assert all(totals[key] >= assisted.usage[key] for key in assisted.usage)
    assert sum(totals.values()) > sum(assisted.usage.values())
    for session in ("unassisted", "positional"):
        read = transcript.read_transcript_file(FIXTURES / f"{session}.jsonl")
        assert read.cli_cost_usd is None and dict(read.model_usage) == {}


@pytest.mark.unit
def test_the_reader_does_not_depend_on_the_files_line_order() -> None:
    """At the pin the TUI writes lines as they settle, so a tool result can come
    before the message that led to it; each recording reads the same backwards."""
    departs = 0
    for session in SESSIONS:
        text = (FIXTURES / f"{session}.jsonl").read_text(encoding="utf-8")
        lines = text.splitlines()
        stamps = [
            entry["timestamp"]
            for entry in map(json.loads, lines)
            if isinstance(entry.get("timestamp"), str)
        ]
        departs += stamps != sorted(stamps)
        backwards = transcript.parse_transcript("\n".join(reversed(lines)))
        assert backwards == transcript.parse_transcript(text), session
    assert departs, (
        "no recording shows its line order departing from the conversation's"
    )


@pytest.mark.unit
def test_the_typed_message_and_the_opening_one_carry_the_same_origin() -> None:
    entries = [
        json.loads(line)
        for line in (FIXTURES / "assisted.jsonl").read_text().splitlines()
    ]
    people = [e for e in entries if transcript.is_person_message(e)]
    assert len(people) == 2
    assert {json.dumps(e["origin"]) for e in people} == {json.dumps({"kind": "human"})}
    assert {e["promptSource"] for e in people} == {"typed"}


@pytest.mark.unit
def test_an_opening_message_given_as_the_argument_is_answered_without_its_tools() -> (
    None
):
    """What ``positional.jsonl`` records: the TUI sends an opening message passed as
    its argument before the MCP server's tools are offered, so the first turn of
    such a session has no story tool. The pair session's opening message is typed
    for that reason; ``unassisted.jsonl`` is that, and its first turn has them."""
    probe = _probe()
    assert probe["positional_opening"]["first_turn_story_tools"] == []
    assert probe["pair_command"]["story_tools"] == ["mcp__ux-story__report_done"]
    positional = transcript.read_transcript_file(FIXTURES / "positional.jsonl")
    assert positional.tool_results == 0


@pytest.mark.unit
def test_the_pair_commands_flags_left_no_built_in_tool_and_reached_the_system_prompt() -> (
    None
):
    pair = _probe()["pair_command"]
    assert pair["turns"] >= 1
    assert pair["builtin_tools"] == []
    assert pair["system_has_marker"] is True


@pytest.mark.unit
def test_bare_still_applies_the_env_of_the_users_settings() -> None:
    """Why the pair command's ``--bare`` keeps a Bedrock or API-key setup kept in
    ``settings.json`` working: the recorded request reached an API that only that
    file's ``env`` named."""
    assert _probe()["bare_applies_settings_env"] is True


@pytest.mark.unit
def test_no_text_of_a_session_leaves_the_reader() -> None:
    read = transcript.read_transcript_file(FIXTURES / "assisted.jsonl")
    shown = json.dumps(read.to_json())
    for text in (TYPED, "KEEP_GOING", "SUMMARY:", "NOTED:", "Begin the task", "/tmp/"):
        assert text not in shown
    assert set(read.to_json()) == {
        "found",
        "assisted",
        "user_messages",
        "tool_results",
        "assistant_messages",
        "usage",
        "models",
        "version",
        "cli_cost_usd",
        "model_usage",
        "lines",
        "unreadable_lines",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("entry", "counted"),
    [
        (_user("Begin.", **PERSON), True),
        (_user([{"type": "text", "text": "Begin."}], **PERSON), True),
        (_user("Begin."), True),
        (_user("Begin.", isMeta=True, **PERSON), False),
        (_user("Begin.", isSidechain=True, **PERSON), False),
        (_user("Begin.", isCompactSummary=True, **PERSON), False),
        (_user("Begin.", origin={"kind": "hook"}), False),
        (_user("   ", **PERSON), False),
        (_user([{"type": "tool_result", "content": "x"}]), False),
        (_user([{"type": "text", "text": "x"}, {"type": "tool_result"}]), False),
        (_user("ok", toolUseResult=[{"type": "text"}]), False),
        (
            _user("<command-name>/exit</command-name>\n<command-args></command-args>"),
            False,
        ),
        (_user("/quit", **PERSON), False),
        (_user("<command-name>/model</command-name>"), True),
        (_user("<local-command-stdout>Goodbye!</local-command-stdout>"), False),
        ({"type": "user", "message": {"role": "assistant", "content": "x"}}, False),
        ({"type": "user", "message": "x"}, False),
        ({"type": "assistant", "message": {"role": "user", "content": "x"}}, False),
    ],
)
def test_what_counts_as_a_message_the_person_sent(
    entry: dict[str, Any], counted: bool
) -> None:
    assert transcript.is_person_message(entry) is counted


@pytest.mark.unit
def test_a_message_spanning_lines_is_counted_once_at_its_largest() -> None:
    text = "\n".join(
        _line(entry)
        for entry in (
            _user("Begin.", **PERSON),
            _assistant("msg_1", input_tokens=5, output_tokens=1),
            _assistant(
                "msg_1", input_tokens=5, output_tokens=40, cache_read_input_tokens=9
            ),
            _assistant("msg_2", output_tokens=3),
            _assistant("msg_3", model="<synthetic>", input_tokens=0, output_tokens=0),
            _assistant(
                "msg_4", input_tokens=True, output_tokens=-1, cache_read_input_tokens=2
            ),
        )
    )
    read = transcript.parse_transcript(text)
    assert read.assistant_messages == 4
    assert read.usage == {
        "input_tokens": 5,
        "output_tokens": 43,
        "cache_read_input_tokens": 11,
    }
    assert read.models == ("claude-opus-5-5",)


@pytest.mark.unit
def test_a_line_of_another_shape_counts_as_nothing_rather_than_failing() -> None:
    text = "\n".join(
        [
            "not json",
            "[1, 2]",
            '{"type": "user"}',
            '{"type": "cost-state", "totalCostUSD": "a lot", "modelUsage": {"bad model": {}}}',
            _line(_user("Begin.", **PERSON)),
            _line({"type": "assistant", "message": {"id": "m", "usage": "none"}}),
            _line({"version": "2.1.281; rm -rf /"}),
            _line({"version": "2.1.281"}),
            "",
        ]
    )
    read = transcript.parse_transcript(text)
    assert (read.lines, read.unreadable_lines) == (8, 2)
    assert read.user_messages == 1 and read.usage is None
    assert read.cli_cost_usd is None and dict(read.model_usage) == {}
    assert read.version == "2.1.281"
    assert transcript.parse_transcript("").usage is None


@pytest.mark.unit
def test_a_session_file_is_read_bounded_and_never_through_a_link(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real.jsonl"
    real.write_text(_line(_user("Begin.", **PERSON)) + "\n")
    assert transcript.read_transcript_file(real).user_messages == 1
    with pytest.raises(transcript.TranscriptError, match="larger than 4 bytes"):
        transcript.read_transcript_file(real, limit=4)
    with pytest.raises(transcript.TranscriptError, match="not a regular file"):
        transcript.read_transcript_file(tmp_path)
    if hasattr(os, "symlink"):
        link = tmp_path / "link.jsonl"
        link.symlink_to(real)
        with pytest.raises(transcript.TranscriptError, match="cannot be opened"):
            transcript.read_transcript_file(link)


@pytest.mark.unit
def test_the_newest_session_under_home_is_read_and_none_is_not_found(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    assert transcript.read_transcript(home) is transcript.NOT_FOUND
    assert (
        transcript.NOT_FOUND.found is False and transcript.NOT_FOUND.assisted is False
    )
    folder = home / ".claude" / "projects" / "-tmp-work"
    folder.mkdir(parents=True)
    older = folder / "older.jsonl"
    older.write_text(_line(_user("one", **PERSON)) + "\n")
    newer = folder / "newer.jsonl"
    newer.write_text(
        "\n".join(_line(_user(t, **PERSON)) for t in ("one", "two")) + "\n"
    )
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))
    (folder / "notes.txt").write_text("not a session")
    if hasattr(os, "symlink"):
        (folder / "zz-link.jsonl").symlink_to(older)
    assert transcript.session_files(home) == [newer, older]
    assert transcript.read_transcript(home).assisted is True
    assert transcript.read_transcript(home, limit=8) is transcript.NOT_FOUND


@pytest.mark.unit
def test_the_recordings_hold_no_secret_shape() -> None:
    for path in sorted(FIXTURES.glob("*.json*")):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(?<![0-9])[0-9]{12}(?![0-9])", text), path.name
        assert "amazonaws.com" not in text and "sk-ant" not in text, path.name
        assert not re.search(r"/(?:Users|home)/", text), path.name


@pytest.mark.unit
def test_the_reader_imports_nothing_first_party() -> None:
    import ast

    tree = ast.parse((SCRIPTS / "ux_runner" / "transcript.py").read_text())
    modules = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}, sorted(modules)


@pytest.mark.unit
def test_the_stand_in_server_answers_as_the_story_server_starts() -> None:
    sys.path.insert(0, str(FIXTURES))
    try:
        import fake_story_server
    finally:
        sys.path.remove(str(FIXTURES))
    opened = fake_story_server.answer(
        {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    )
    assert opened is not None
    assert opened["result"]["serverInfo"] == {"name": "ux-story", "version": "1"}
    listed = fake_story_server.answer({"id": 2, "method": "tools/list"})
    assert listed is not None and listed["result"]["tools"][0]["name"] == "report_done"
    called = fake_story_server.answer({"id": 3, "method": "tools/call", "params": {}})
    assert called is not None and called["result"]["isError"] is False
    assert fake_story_server.answer({"method": "notifications/initialized"}) is None
    pinged = fake_story_server.answer({"id": 4, "method": "ping"})
    assert pinged is not None and pinged["result"] == {}
    missing = fake_story_server.answer({"id": 5, "method": "resources/list"})
    assert missing is not None and missing["error"]["code"] == -32601
