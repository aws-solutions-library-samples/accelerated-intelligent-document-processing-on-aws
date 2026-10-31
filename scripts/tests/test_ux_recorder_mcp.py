# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the recorder's MCP adapter (scripts/ux_recorder_mcp.py).

The adapter is the only way the unattended MR UX test's model reaches the
recorder, so what is pinned is the contract both sides depend on: the JSON-RPC
shapes Claude Code reads (including that nothing but JSON-RPC reaches stdout),
exactly four tools with bounded schemas, the bounds on what is forwarded, the
pause that ends on its own and the pause budget, and a recorder that is not
running reading as a tool error the model can carry on from rather than a crash.

The JSON-RPC cases drive :meth:`Adapter.handle` and :func:`serve` with fakes. Two
cases are end to end: a real ``Recorder`` behind a real Unix socket, reached
through ``RecorderLink``'s default bindings, and the script itself run as
``python3 -I`` the way the harness's MCP configuration runs it. Nothing here
needs Chrome, ffmpeg, node or a network.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cdp = _load("ux_recorder_cdp")
render = _load("ux_recorder_render")
recorder_cli = _load("ux_recorder")
mcp = _load("ux_recorder_mcp")


class FakeLink:
    """Stands in for ``RecorderLink``: keeps every command and answers ``reply``."""

    def __init__(self, reply: dict[str, Any] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.reply = reply or {"ok": True}

    def command(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(payload)
        return dict(self.reply)


class FakeTimer:
    def __init__(self, seconds: float, callback: Callable[[], None]) -> None:
        self.seconds = seconds
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class Timers:
    """A ``timer`` argument for :class:`Adapter` that starts nothing."""

    def __init__(self) -> None:
        self.started: list[FakeTimer] = []

    def __call__(self, seconds: float, callback: Callable[[], None]) -> FakeTimer:
        timer = FakeTimer(seconds, callback)
        self.started.append(timer)
        return timer


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def adapter_with(
    link: Any = None, clock: Clock | None = None, timers: Timers | None = None
) -> Any:
    return mcp.Adapter(
        link if link is not None else FakeLink(),
        clock=clock or Clock(),
        timer=timers if timers is not None else Timers(),
    )


def request(method: str, params: Any = None, request_id: Any = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def call(adapter: Any, name: str, arguments: Any = None) -> dict[str, Any]:
    """The ``result`` of one ``tools/call`` through :meth:`Adapter.handle`."""
    reply = adapter.handle(
        request("tools/call", {"name": name, "arguments": arguments or {}})
    )
    assert reply is not None and "result" in reply, reply
    return reply["result"]


def text_of(result: dict[str, Any]) -> dict[str, Any]:
    (content,) = result["content"]
    assert content["type"] == "text"
    return json.loads(content["text"])


class FakeScreencast:
    def __init__(self) -> None:
        self.count = 0
        self.bytes = 0

    def capture_now(self) -> int:
        self.count += 1
        return self.count

    def ack(self, params: dict[str, Any]) -> None:
        pass

    def handle_frame(self, params: dict[str, Any]) -> int:
        return self.capture_now()

    def stop(self) -> None:
        pass


@pytest.mark.unit
@pytest.mark.parametrize("version", mcp.PROTOCOL_VERSIONS)
def test_initialize_echoes_a_protocol_version_it_speaks(version: str) -> None:
    reply = adapter_with().handle(
        request("initialize", {"protocolVersion": version, "capabilities": {}})
    )
    assert reply is not None
    result = reply["result"]
    assert result["protocolVersion"] == version
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"] == {"name": "ux-recorder", "version": "1"}
    assert "mark" in result["instructions"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"protocolVersion": "2023-01-01"}, id="older"),
        pytest.param({"protocolVersion": "2099-01-01"}, id="newer"),
        pytest.param({"protocolVersion": ["2025-06-18"]}, id="a-list"),
        pytest.param({}, id="absent"),
        pytest.param(None, id="no-params"),
    ],
)
def test_initialize_offers_the_newest_version_for_one_it_does_not_speak(
    params: Any,
) -> None:
    reply = adapter_with().handle(request("initialize", params))
    assert reply is not None
    assert reply["result"]["protocolVersion"] == "2025-11-25"
    assert mcp.PROTOCOL_VERSIONS[-1] == "2025-11-25"


@pytest.mark.unit
def test_tools_list_offers_exactly_the_four_recorder_tools_and_their_schemas() -> None:
    reply = adapter_with().handle(request("tools/list"))
    assert reply is not None
    tools = {tool["name"]: tool for tool in reply["result"]["tools"]}
    assert list(tools) == ["mark", "note", "pause", "resume"]
    assert [t["name"] for t in mcp.TOOLS] == ["mark", "note", "pause", "resume"]
    mark = tools["mark"]["inputSchema"]
    assert mark["required"] == ["label"]
    assert mark["properties"]["label"] == {
        "type": "string",
        "maxLength": 80,
        "description": "The chapter title, a few words.",
    }
    assert mark["properties"]["say"]["maxLength"] == 400
    note = tools["note"]["inputSchema"]
    assert note["required"] == ["text"]
    assert note["properties"]["text"]["maxLength"] == 500
    for name in ("pause", "resume"):
        assert tools[name]["inputSchema"]["properties"] == {}
        assert "required" not in tools[name]["inputSchema"]
    for tool in tools.values():
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["description"].endswith(".")
    assert "120 s" in tools["pause"]["description"]
    assert "chapter title" in tools["mark"]["description"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "method",
    ["notifications/initialized", "notifications/cancelled", "tools/list", "ping"],
)
def test_a_notification_gets_no_reply(method: str) -> None:
    """A message without an ``id`` is a notification, whatever its method."""
    link = FakeLink()
    assert adapter_with(link).handle({"jsonrpc": "2.0", "method": method}) is None
    assert link.sent == []


@pytest.mark.unit
def test_ping_answers_an_empty_result() -> None:
    assert adapter_with().handle(request("ping", request_id="p-1")) == {
        "jsonrpc": "2.0",
        "id": "p-1",
        "result": {},
    }


@pytest.mark.unit
def test_a_line_that_is_not_json_is_a_parse_error_with_a_null_id() -> None:
    stdout = io.StringIO()
    assert mcp.serve(io.StringIO("not json\n{\n"), stdout, adapter_with()) == 0
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r["error"]["code"] for r in replies] == [-32700, -32700]
    assert [r["id"] for r in replies] == [None, None]


@pytest.mark.unit
def test_an_unknown_method_is_method_not_found() -> None:
    reply = adapter_with().handle(request("resources/list", request_id=7))
    assert reply is not None
    assert reply["id"] == 7 and reply["error"]["code"] == -32601


@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [
        pytest.param([request("ping")], id="a-batch"),
        pytest.param("ping", id="a-string"),
        pytest.param({"jsonrpc": "2.0", "id": 3}, id="no-method"),
        pytest.param({"jsonrpc": "2.0", "id": 3, "method": ""}, id="empty-method"),
        pytest.param({"jsonrpc": "2.0", "id": 3, "method": 5}, id="number-method"),
        pytest.param({"jsonrpc": "2.0", "id": {"n": 1}, "method": "ping"}, id="bad-id"),
        pytest.param({"jsonrpc": "2.0", "id": True, "method": "ping"}, id="bool-id"),
    ],
)
def test_a_message_that_is_not_a_request_object_is_an_invalid_request(
    message: Any,
) -> None:
    reply = adapter_with().handle(message)
    assert reply is not None and reply["error"]["code"] == -32600


@pytest.mark.unit
@pytest.mark.parametrize(
    "params",
    [
        pytest.param(["mark"], id="params-a-list"),
        pytest.param({"arguments": {"label": "Open"}}, id="no-tool-name"),
        pytest.param({"name": "stop", "arguments": {}}, id="a-tool-it-does-not-have"),
        pytest.param({"name": "mark", "arguments": ["Open"]}, id="arguments-a-list"),
    ],
)
def test_params_it_cannot_act_on_are_invalid_params(params: Any) -> None:
    link = FakeLink()
    reply = adapter_with(link).handle(request("tools/call", params))
    assert reply is not None and reply["error"]["code"] == -32602
    assert link.sent == []


@pytest.mark.unit
def test_mark_forwards_only_a_bounded_cleaned_label_and_narration() -> None:
    link = FakeLink()
    adapter = adapter_with(link)
    label = "Open\tthe\x00  set\n" + "x" * 200
    say = "We  open\x1bthe set. " + "y" * 1000
    result = call(
        adapter,
        "mark",
        {"label": label, "say": say, "cmd": "stop", "session": "/elsewhere"},
    )
    assert result["isError"] is False
    (sent,) = link.sent
    assert sorted(sent) == ["cmd", "label", "say"] and sent["cmd"] == "mark"
    assert sent["label"] == ("Open the set " + "x" * 200)[:80]
    assert sent["say"] == ("We open the set. " + "y" * 1000)[:400]
    assert text_of(result)["marks_left"] == 39


@pytest.mark.unit
def test_a_mark_without_narration_forwards_none_for_it() -> None:
    link = FakeLink()
    call(adapter_with(link), "mark", {"label": "Open the set", "say": " \n "})
    assert link.sent == [{"cmd": "mark", "label": "Open the set", "say": None}]


@pytest.mark.unit
def test_a_tool_call_with_no_arguments_runs_the_tool() -> None:
    link = FakeLink()
    reply = adapter_with(link).handle(request("tools/call", {"name": "resume"}))
    assert reply is not None and reply["result"]["isError"] is False
    assert link.sent == [{"cmd": "resume"}]


@pytest.mark.unit
@pytest.mark.parametrize(
    "arguments",
    [{"label": "  \n\t "}, {"label": 42}, {"say": "We open it."}, {}],
    ids=["blank", "a-number", "absent", "no-arguments"],
)
def test_a_mark_with_no_label_is_an_error_and_sends_nothing(arguments: Any) -> None:
    link = FakeLink()
    result = call(adapter_with(link), "mark", arguments)
    assert result["isError"] is True
    assert text_of(result)["error"] == "mark needs a label"
    assert text_of(result)["marks_left"] == 40
    assert link.sent == []


@pytest.mark.unit
def test_the_forty_first_mark_is_refused() -> None:
    link = FakeLink()
    adapter = adapter_with(link)
    left = [
        text_of(call(adapter, "mark", {"label": f"Step {n}"}))["marks_left"]
        for n in range(40)
    ]
    assert left == list(range(39, -1, -1))
    refused = call(adapter, "mark", {"label": "One more"})
    assert refused["isError"] is True
    assert text_of(refused)["error"] == mcp.MARKS_SPENT
    assert "40" in mcp.MARKS_SPENT
    assert len(link.sent) == 40


@pytest.mark.unit
def test_a_mark_the_recorder_refused_does_not_spend_one() -> None:
    link = FakeLink({"ok": False, "error": "mark needs a label"})
    result = call(adapter_with(link), "mark", {"label": "Open"})
    assert result["isError"] is True and text_of(result)["marks_left"] == 40


@pytest.mark.unit
def test_notes_are_bounded_and_the_sixty_first_is_refused() -> None:
    link = FakeLink()
    adapter = adapter_with(link)
    empty = call(adapter, "note", {"text": "\x00\n"})
    assert empty["isError"] is True and text_of(empty)["error"] == "note needs text"
    long_note = call(adapter, "note", {"text": "z" * 900})
    assert long_note["isError"] is False and link.sent[0]["text"] == "z" * 500
    for n in range(59):
        assert call(adapter, "note", {"text": f"note {n}"})["isError"] is False
    refused = call(adapter, "note", {"text": "one more"})
    assert refused["isError"] is True
    assert text_of(refused)["error"] == mcp.NOTES_SPENT and len(link.sent) == 60


@pytest.mark.unit
def test_a_pause_resumes_itself_when_its_timer_fires() -> None:
    link = FakeLink()
    clock = Clock(10.0)
    timers = Timers()
    adapter = adapter_with(link, clock, timers)
    paused = call(adapter, "pause")
    assert paused["isError"] is False and text_of(paused)["resumes_in"] == 120.0
    (timer,) = timers.started
    assert timer.seconds == 120.0 and link.sent == [{"cmd": "pause"}]
    clock.now = 130.0
    timer.callback()
    assert link.sent == [{"cmd": "pause"}, {"cmd": "resume"}]
    assert adapter.paused_seconds == 120.0 and not timer.cancelled
    timer.callback()
    assert len(link.sent) == 2


@pytest.mark.unit
def test_an_explicit_resume_cancels_the_timer() -> None:
    link = FakeLink()
    clock = Clock()
    timers = Timers()
    adapter = adapter_with(link, clock, timers)
    call(adapter, "pause")
    clock.now = 30.0
    assert call(adapter, "resume")["isError"] is False
    (timer,) = timers.started
    assert timer.cancelled and adapter.paused_seconds == 30.0
    timer.callback()
    assert link.sent == [{"cmd": "pause"}, {"cmd": "resume"}]


@pytest.mark.unit
def test_a_second_pause_while_paused_is_refused_without_a_second_timer() -> None:
    link = FakeLink()
    timers = Timers()
    adapter = adapter_with(link, timers=timers)
    call(adapter, "pause")
    again = call(adapter, "pause")
    assert again["isError"] is True and text_of(again)["error"] == "already paused"
    assert len(timers.started) == 1 and link.sent == [{"cmd": "pause"}]


@pytest.mark.unit
def test_the_pause_budget_is_enforced() -> None:
    link = FakeLink()
    clock = Clock()
    timers = Timers()
    adapter = adapter_with(link, clock, timers)
    call(adapter, "pause")
    clock.now = 120.0
    timers.started[-1].callback()
    clock.now = 200.0
    call(adapter, "pause")
    clock.now = 320.0
    call(adapter, "resume")
    assert adapter.paused_seconds == 240.0

    clock.now = 400.0
    last = call(adapter, "pause")
    assert text_of(last)["resumes_in"] == 60.0 and timers.started[-1].seconds == 60.0
    clock.now = 460.0
    timers.started[-1].callback()
    assert adapter.paused_seconds == 300.0

    clock.now = 500.0
    refused = call(adapter, "pause")
    assert refused["isError"] is True
    assert text_of(refused)["error"] == mcp.PAUSE_BUDGET_SPENT
    assert [c["cmd"] for c in link.sent].count("pause") == 3
    assert len(timers.started) == 3


@pytest.mark.unit
def test_a_pause_the_recorder_refused_starts_no_timer() -> None:
    link = FakeLink({"ok": False, "error": "already paused"})
    timers = Timers()
    result = call(adapter_with(link, timers=timers), "pause")
    assert result["isError"] is True and timers.started == []


def _link(read_current: Any, send: Any) -> Any:
    return mcp.RecorderLink(Path("/nonexistent"), read_current=read_current, send=send)


def _refuse_connection(_socket: str, _command: dict[str, Any]) -> dict[str, Any]:
    raise ConnectionRefusedError(61, "Connection refused")


def _hang_up(_socket: str, _command: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("recorder closed the connection without replying")


def _garble(_socket: str, _command: dict[str, Any]) -> dict[str, Any]:
    return json.loads("{not json")


def _answer_a_list(_socket: str, _command: dict[str, Any]) -> Any:
    return ["ok"]


def _current(_out: Path) -> dict[str, Any]:
    return {"socket": "/tmp/ux-recorder-test.sock", "pid": 1}


@pytest.mark.unit
@pytest.mark.parametrize(
    "link",
    [
        pytest.param(_link(lambda _out: None, _refuse_connection), id="no-current"),
        pytest.param(_link(lambda _out: {"pid": 1}, _refuse_connection), id="no-sock"),
        pytest.param(
            _link(lambda _out: {"socket": 5}, _refuse_connection), id="sock-5"
        ),
        pytest.param(_link(_current, _refuse_connection), id="connection-refused"),
        pytest.param(_link(_current, _hang_up), id="hung-up"),
        pytest.param(_link(_current, _garble), id="garbled-reply"),
        pytest.param(_link(_current, _answer_a_list), id="reply-not-an-object"),
    ],
)
def test_the_recorder_being_down_is_a_tool_error_to_carry_on_from(link: Any) -> None:
    adapter = adapter_with(link)
    for name, arguments in (
        ("mark", {"label": "Open"}),
        ("note", {"text": "spinner"}),
        ("pause", {}),
        ("resume", {}),
    ):
        result = call(adapter, name, arguments)
        assert result["isError"] is True, name
        assert text_of(result)["error"] == mcp.RECORDER_DOWN
    assert "carry on without marks" in mcp.RECORDER_DOWN
    assert adapter.marks == 0 and adapter.notes == 0


@pytest.mark.unit
def test_serve_writes_one_line_per_request_and_nothing_else(
    capsys: pytest.CaptureFixture[str],
) -> None:
    lines = [
        json.dumps(request("initialize", {"protocolVersion": "2025-06-18"})),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "",
        json.dumps(request("tools/list", request_id=2)),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled"}),
        json.dumps(request("ping", request_id="three")),
        json.dumps(request("tools/call", {"name": "note", "arguments": {}}, 4)),
    ]
    stdout = io.StringIO()
    stdin = io.StringIO("\n".join(lines) + "\n")
    assert mcp.serve(stdin, stdout, adapter_with()) == 0
    out = stdout.getvalue()
    assert out.endswith("\n")
    replies = [json.loads(line) for line in out.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, "three", 4]
    assert all(r["jsonrpc"] == "2.0" for r in replies)
    assert capsys.readouterr().out == ""


@pytest.mark.unit
def test_an_unexpected_failure_is_an_internal_error_and_the_server_carries_on(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class BrokenLink:
        def command(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise KeyError(payload["cmd"])

    stdin = io.StringIO(
        json.dumps(request("tools/call", {"name": "pause", "arguments": {}}, 1))
        + "\n"
        + json.dumps(request("ping", request_id=2))
        + "\n"
    )
    stdout = io.StringIO()
    assert mcp.serve(stdin, stdout, adapter_with(BrokenLink())) == 0
    first, second = (json.loads(line) for line in stdout.getvalue().splitlines())
    assert first["id"] == 1 and first["error"]["code"] == -32603
    assert second == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert "KeyError" in capsys.readouterr().err


@pytest.mark.unit
def test_a_recorder_module_that_will_not_import_reads_as_the_recorder_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "ux_recorder", None)
    reply = mcp.RecorderLink(tmp_path).command({"cmd": "mark", "label": "Open"})
    assert reply == {"ok": False, "error": mcp.RECORDER_DOWN}


@pytest.mark.unit
def test_main_serves_stdin_until_eof_and_needs_an_out_directory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request("ping")) + "\n"))
    assert mcp.main(["--out", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {},
    }
    with pytest.raises(SystemExit) as refused:
        mcp.main([])
    assert refused.value.code == 2


@pytest.mark.unit
def test_clean_keeps_bounded_single_line_text_and_nothing_else() -> None:
    assert mcp.clean(None, 10) == "" and mcp.clean(42, 10) == ""
    assert mcp.clean(["a"], 10) == ""
    assert mcp.clean("  a\tb\x00\x1b c\u202e\nd  ", 80) == "a b c d"
    assert mcp.clean("abcdef ghij", 7) == "abcdef"
    assert len(mcp.clean("w" * 500, 80)) == 80


@pytest.mark.unit
def test_the_recorder_link_defaults_reach_a_real_recorder_over_its_unix_socket(
    tmp_path: Path,
) -> None:
    """``RecorderLink`` with no fakes: ``ux_recorder.read_current`` finds the
    socket in ``current.json`` and ``ux_recorder.send_command`` speaks the real
    daemon's protocol, served here by its own handler on a real Unix socket."""
    socket_dir = tempfile.mkdtemp(dir="/tmp")
    socket_path = os.path.join(socket_dir, "r.sock")
    out_dir = tmp_path / "recordings"
    session_dir = out_dir / "stack-20261015-120000"
    session_dir.mkdir(parents=True)
    meta = {"stack": "stack", "persona": "Admin", "socket": socket_path, "started": 1.0}
    rec = recorder_cli.Recorder(session_dir, meta)
    rec.screencast = FakeScreencast()
    server = recorder_cli._Server(socket_path, recorder_cli._make_handler(rec))
    serving = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    serving.start()
    try:
        recorder_cli.write_current(
            out_dir,
            {
                "session_dir": str(session_dir),
                "socket": socket_path,
                "pid": os.getpid(),
            },
        )
        adapter = mcp.Adapter(mcp.RecorderLink(out_dir))
        results = [
            call(adapter, "mark", {"label": "Open the set", "say": "We open it."}),
            call(adapter, "note", {"text": "The spinner has no label."}),
            call(adapter, "pause"),
            call(adapter, "resume"),
        ]
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(socket_dir, ignore_errors=True)
    assert [r["isError"] for r in results] == [False, False, False, False]
    assert text_of(results[0])["marks_left"] == 39
    assert [e["type"] for e in rec.events] == ["mark", "note", "pause", "resume"]
    assert rec.events[0]["label"] == "Open the set"
    assert rec.events[0]["say"] == "We open it."
    assert rec.paused is False and adapter.paused_seconds >= 0.0


@pytest.mark.unit
def test_the_script_runs_isolated_and_writes_only_json_rpc_to_stdout(
    tmp_path: Path,
) -> None:
    """Run as the MCP configuration runs it: ``python3 -I -B`` under an empty
    environment, which leaves the script's own directory off ``sys.path``. The
    ``mark`` makes it import ``ux_recorder``, and with no ``current.json`` in its
    ``--out`` directory the answer is the recorder-down tool error."""
    lines = [
        json.dumps(request("initialize", {"protocolVersion": "2025-06-18"})),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps(request("tools/list", request_id=2)),
        json.dumps(request("tools/call", {"name": "mark", "arguments": {}}, 3)),
        json.dumps(
            request("tools/call", {"name": "mark", "arguments": {"label": "Open"}}, 4)
        ),
        "not json",
    ]
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-I",
            "-B",
            str(SCRIPTS / "ux_recorder_mcp.py"),
            "--out",
            str(tmp_path),
        ],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={"LANG": "C.UTF-8"},
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, 3, 4, None]
    assert all(r["jsonrpc"] == "2.0" for r in replies)
    assert replies[1]["result"]["tools"][0]["name"] == "mark"
    assert text_of(replies[2]["result"])["error"] == "mark needs a label"
    assert text_of(replies[3]["result"])["error"] == mcp.RECORDER_DOWN
    assert replies[4]["error"]["code"] == -32700
