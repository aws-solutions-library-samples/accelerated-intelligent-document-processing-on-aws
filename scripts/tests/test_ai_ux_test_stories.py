# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/sdlc/ai_ux_test_stories.py, the first-time-user story orchestrator.

The orchestrator is driven end to end through :func:`ai_ux_test_stories.main` with a
fully fake :class:`ai_ux_test_stories.Runtime`: no subprocess, browser, network or
AWS call. The fake ``claude`` writes each flow's journal with the story server's own
``JournalWriter``, so every verdict here is the real judge's over a real v1 journal.

What is pinned, by the claim each test names:

* every flow is confined on its own: a fresh run directory, the socket watch from
  Chrome's launch, both self-tests before the one sign-in, the audit after Chrome
  is killed, a Bedrock session minted per attempt, and render only once every
  Chrome of the run is gone;
* a flow's video is rendered only when ``session.review_stack_problem`` (called,
  with the run's ``--stack``, its UI environment and that flow's page) says the
  stack is the review stack; a refused flow records nothing, mints no Polly
  session and leaves no mp4, and the reply names each refused flow with its reason
  without calling an unconfirmed stack another;
* the job's budget and time gates, the one re-run of a bad verdict, a refused
  sign-in costing its group's other flows, and spend counted as the larger of the
  CLI's figure and the usage at pinned rates equal to ``pricing.yaml``;
* ``story-run.json`` and ``results.json`` v1, and ``--judge`` reproducing both
  outputs byte for byte, ``--check`` included;
* the harness list is closed over its imports, and ``--help`` and ``--judge``
  import nothing outside the standard library.
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parent
SCRIPT = SCRIPTS / "sdlc" / "ai_ux_test_stories.py"
for _entry in (SCRIPTS, SCRIPTS / "sdlc"):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

import ai_ux_test_stories as runner  # noqa: E402
import ux_story_mcp  # noqa: E402
from ux_runner import (  # noqa: E402
    agent,
    chrome,
    docs_index,
    egress,
    gitlab_api,
    paths,
    processes,
    recording,
    session,
    signin,
    stories,
    story_prompt,
    story_relay,
    story_verdict,
    tool_profiles,
)

review = gitlab_api.review

HEAD = "0123456789abcdef" * 2 + "01234567"
DOCS_TREE = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"
ACCOUNT = "123456789012"
PARTITION = "aws"
MODEL = "us.anthropic.claude-opus-5-5"
CREDENTIAL = "test-review-credential-value"
AWS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
AWS_KEY_TEXT = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
AWS_SESSION_TEXT = "FwoGZXIvYXdzEXAMPLEambientsessionvalue"
SCOPED = ("ASIAIOSFODNN7SCOPED1", "scopedSecretKeyValue01", "scopedSessionValue0001")
POLLY = ("ASIAIOSFODNN7POLLY01", "pollySecretKeyValue001", "pollySessionValue00001")
WEB_HOST = "d111111abcdef8.cloudfront.net"
WEB_URL = f"https://{WEB_HOST}/"
REVIEW = session.REVIEW_STACK
OTHER_STACK = "idp-dev-stack4"
LOOKALIKE_STACK = "idp-ux-nightly-2"
STACK_REASON = "`--stack` is not the review stack `idp-ux-nightly`"
ENV_REASON = "`UX_TEST_UI_ENV` does not name the settings parameter of `idp-ux-nightly`"
PAGE_REASON = (
    "the signed-in page did not report `idp-ux-nightly` as its stack, so the run's "
    "stack could not be confirmed as the review stack"
)
PERSONAS = {
    "Admin": ("ux-admin@example.invalid", "Ux!3cretive-admin-9aZ"),
    "Author": ("ux-author@example.invalid", "Ux!3cretive-author-9aZ"),
    "Viewer": ("ux-viewer@example.invalid", "Ux!3cretive-viewer-9aZ"),
}


def _ui_env_text(settings: str | None = f"{REVIEW}-Settings", web: bool = True) -> str:
    lines = [
        "VITE_USER_POOL_ID=us-east-1_AbCdEfGhI",
        "VITE_USER_POOL_CLIENT_ID=1a2b3c4d5e6f7g8h9i0j1k2l3m",
        "VITE_IDENTITY_POOL_ID=us-east-1:11111111-2222-3333-4444-abcdefabcdef",
        "VITE_API_BASE_URL=https://abcde12345.execute-api.us-east-1.amazonaws.com/api",
        "VITE_AWS_REGION=us-east-1",
        "VITE_INPUT_BUCKET_NAME=idp-ux-nightly-inputbucket-def456",
    ]
    if settings is not None:
        lines.append(f"VITE_SETTINGS_PARAMETER={settings}")
    if web:
        lines.append(f"{runner.WEB_URL_KEY}={WEB_URL}")
    return "\n".join(lines)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _personas(*groups: str) -> str:
    chosen = groups or tuple(PERSONAS)
    return _b64(
        json.dumps(
            {
                group: {"username": PERSONAS[group][0], "password": PERSONAS[group][1]}
                for group in chosen
            }
        )
    )


def _environ(**changes: str | None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "UX_TEST_UI_ENV": _b64(_ui_env_text()),
        "UX_TEST_PERSONAS": _personas(),
        "UX_TEST_AWS_ACCOUNT_ID": ACCOUNT,
        "AWS_ACCESS_KEY_ID": AWS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": AWS_KEY_TEXT,
        "AWS_SESSION_TOKEN": AWS_SESSION_TEXT,
    }
    for name, value in changes.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def _index() -> docs_index.Index:
    """A one-page index, built with the real splitter, for the fake runs."""
    text = (
        "# Web UI\n\n## Upload documents\n\nUpload a document from the Upload page.\n"
    )
    return docs_index.Index(
        format=docs_index.INDEX_FORMAT,
        source=docs_index.DocsSource(kind="worktree", ref=None, dirty=False),
        corpus_hash="abcdef012345",
        pages=("web-ui.md",),
        sections=tuple(docs_index.split_page("web-ui.md", text)),
    )


class FakeProcess:
    def __init__(
        self, name: str, on_terminate: Callable[[], None] | None = None
    ) -> None:
        self.name = name
        self.pid = 4100
        self.terminations = 0
        self._on_terminate = on_terminate

    def alive(self) -> bool:
        return self.terminations == 0

    def terminate(self, grace: float = 10.0) -> int:
        self.terminations += 1
        if self._on_terminate is not None:
            self._on_terminate()
        return 0


class FakeDevTools:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def close(self) -> None:
        self.log.append("session_closed")


class FakeWatch:
    def __init__(self, state: str, log: list[str]) -> None:
        self.state = state
        self.log = log

    def start(self) -> FakeWatch:
        self.log.append("watch_started")
        return self

    def stop(self) -> chrome.SocketAudit:
        self.log.append("watch_stopped")
        return chrome.SocketAudit(self.state, (), 3)


class FakeSts:
    """STS for the job's role; each assume issues credentials of its own kind."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.assumed: list[dict[str, Any]] = []

    def get_caller_identity(self) -> dict[str, str]:
        arn = f"arn:{PARTITION}:sts::{ACCOUNT}:assumed-role/idp-sdlc-GitLab/job"
        return {"Account": ACCOUNT, "Arn": arn}

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.assumed.append(kwargs)
        if self.refuse:
            raise RuntimeError("AccessDenied")
        polly = "polly" in kwargs["Policy"]
        key_id, key, token = POLLY if polly else SCOPED
        return {
            "Credentials": {
                "AccessKeyId": key_id,
                "SecretAccessKey": key,
                "SessionToken": token,
            }
        }

    def actions(self) -> list[list[str]]:
        return [
            json.loads(assumed["Policy"])["Statement"][0]["Action"]
            for assumed in self.assumed
        ]


#: What the fake ``claude`` does in one attempt, by name (``StoryHarness.script``).
OUTCOMES = ("done", "blocked", "offered", "refused", "help", "silent")


class StoryHarness:
    """The fake runtime and every input of one run, with a log of what was called."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.calls: list[str] = []
        self.environ = _environ()
        self.sts = FakeSts()
        self.script: dict[str, list[str]] = {}
        self.facts: dict[str, signin.PageFacts] = {}
        self.default_facts = signin.PageFacts(
            REVIEW, "0.6.12.dev1", "2026-10-19", (1600, 1000, 1.0), None
        )
        self.audit_state = "clean"
        self.now = 1000.0
        self.stop = "end_turn"
        self.strays: tuple[str, ...] = ()
        self.refused_sign_in: set[str] = set()
        self.cost_usd: float | None = 1.25
        self.usage: dict[str, int] | None = {
            "input_tokens": 1000,
            "output_tokens": 2000,
            "cache_read_input_tokens": 100000,
            "cache_creation_input_tokens": 10000,
        }
        self.agent_error: Exception | None = None
        self.timed_out = False
        self.run_dirs: list[Path] = []
        self.prompts: list[str] = []
        self.argvs: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.mcp_configs: list[dict[str, Any]] = []
        self.recorder_kwargs: list[dict[str, Any]] = []
        self.render_kwargs: list[dict[str, Any]] = []
        self.sign_ins: list[tuple[str, dict[str, Any]]] = []
        self.current_group = ""
        self.refusals = [
            {
                "t": "2026-10-20T00:00:00Z",
                "method": "GET",
                "target": f"http://{chrome.SELFTEST_HOST}/",
                "reason": "plain_http_not_allowed",
            }
        ]
        self.chrome_alive = 0
        self.alive_at_render: list[int] = []
        self.harness_sha = "feedface" * 5
        self.artifacts = tmp_path / "out" / "ux-test-stories"
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.prefix = tmp_path / "toolchain"
        toolchain = paths.Toolchain(self.prefix)
        for binary in (toolchain.chrome, toolchain.claude, toolchain.devtools_mcp):
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
        pinned = runner.pinned_versions()
        toolchain.versions_file.write_text(
            json.dumps({**pinned, "chrome_platform": "linux64", "node": "v22"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(chrome, "policy_installed", lambda *a, **k: True)
        monkeypatch.setattr(processes, "can_switch_user", lambda: True)
        monkeypatch.setattr(recording, "render_uid", lambda user: os.geteuid())

    def argv(self, *extra: str, story: str = "route-folder-to-profile") -> list[str]:
        selection = ["--story", story] if story else []
        return [
            *selection,
            "--artifact-dir",
            str(self.artifacts),
            "--repo-dir",
            str(self.repo),
            "--toolchain-prefix",
            str(self.prefix),
            "--model",
            MODEL,
            "--region",
            "us-east-1",
            *extra,
        ]

    def main(self, *extra: str, story: str = "route-folder-to-profile") -> int:
        return runner.main(self.argv(*extra, story=story), self.runtime())

    def runtime(self) -> runner.Runtime:
        return runner.Runtime(
            sts=self._sts,
            launch_proxy=self._launch_proxy,
            launch_chrome=self._launch_chrome,
            wait_for_cdp=self._step("wait_for_cdp", {"Browser": "HeadlessChrome"}),
            first_page_target=self._step(
                "first_page_target",
                {"id": "TARGET1", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/T1"},
            ),
            open_session=self._open_session,
            verify_policy=self._step("verify_policy", None),
            verify_proxy_path=self._step("verify_proxy_path", None),
            sign_in=self._sign_in,
            page_facts=self._page_facts,
            verify_viewport=self._step("verify_viewport", {"w": 1600, "h": 1000}),
            measure_viewport=self._step("measure_viewport", {"w": 1600, "h": 1000}),
            socket_watch=self._socket_watch,
            kill_strays=self._kill_strays,
            start_recorder=self._start_recorder,
            stop_recorder=self._stop_recorder,
            kill_recorder=self._kill_recorder,
            recorder_status=self._step("recorder_status", {"marks": 2}),
            run_agent=self._run_agent,
            render_video=self._render_video,
            chapters=self._step("chapters", [("0:00", "Opened the application")]),
            index_docs=self._index_docs,
            compose_prompt=self._compose_prompt,
            harness=lambda environ: (self.harness_sha, False),
            sleep=lambda seconds: None,
            clock=lambda: self.now,
            wall=lambda: 1760961234.5,
            environ=self.environ,
        )

    def _step(self, name: str, answer: Any) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return answer

        return call

    def _sts(self, region: str) -> FakeSts:
        return self.sts

    def _index_docs(self, **kwargs: Any) -> runner.DocsCorpus:
        self.calls.append("index_docs")
        self.index_kwargs = kwargs
        index = _index()
        if kwargs.get("ref"):
            index = dataclasses.replace(
                index,
                source=docs_index.DocsSource(kind="git", ref=kwargs["ref"], dirty=None),
            )
            return runner.DocsCorpus(index, DOCS_TREE)
        return runner.DocsCorpus(index, "worktree")

    def _compose_prompt(self, **kwargs: Any) -> str:
        text = story_prompt.build_story_prompt(**kwargs)
        self.prompts.append(text)
        return text

    def _launch_proxy(self, **kwargs: Any) -> egress.ProxyRun:
        self.calls.append("launch_proxy")
        run_dir = Path(kwargs["run_dir"])
        self.run_dirs.append(run_dir)
        proxy_dir = run_dir / "proxy"
        proxy_dir.mkdir()
        refusals = proxy_dir / "refusals.jsonl"
        refusals.write_text(
            "".join(json.dumps(record) + "\n" for record in self.refusals),
            encoding="utf-8",
        )
        self.proxy_kwargs = kwargs
        return egress.ProxyRun(
            process=FakeProcess("proxy", lambda: self.calls.append("proxy_stopped")),
            ready_file=proxy_dir / "ready",
            refusal_log=refusals,
            stats_file=proxy_dir / "stats.json",
            log_file=proxy_dir / "proxy.log",
            owner=os.geteuid(),
        )

    def _launch_chrome(self, *args: Any, **kwargs: Any) -> FakeProcess:
        self.calls.append("launch_chrome")
        self.chrome_alive += 1

        def stopped() -> None:
            self.chrome_alive -= 1
            self.calls.append("chrome_terminated")

        return FakeProcess("chrome", stopped)

    def _open_session(self, ws_url: str) -> FakeDevTools:
        self.calls.append("open_session")
        return FakeDevTools(self.calls)

    def _sign_in(self, devtools: Any, persona: signin.Persona, **kwargs: Any) -> None:
        self.calls.append("sign_in")
        self.sign_ins.append((persona.name, kwargs))
        self.current_group = persona.name
        if persona.name in self.refused_sign_in:
            raise signin.SignInError("sign-in was refused: Incorrect password.")

    def _page_facts(self, devtools: Any) -> signin.PageFacts:
        self.calls.append("page_facts")
        return self.facts.get(self.current_group, self.default_facts)

    def _socket_watch(self, uid: int | None) -> FakeWatch:
        self.calls.append("socket_watch")
        return FakeWatch(self.audit_state, self.calls)

    def _kill_strays(self, uid: int | None, group: int | None) -> tuple[str, ...]:
        self.calls.append("kill_strays")
        return self.strays

    def _start_recorder(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("start_recorder")
        self.recorder_kwargs.append(kwargs)
        out_dir = Path(kwargs["out_dir"])
        session_dir = out_dir / "session-1"
        session_dir.mkdir(parents=True)
        (out_dir / "current.json").write_text("{}", encoding="utf-8")
        return {"session_dir": str(session_dir), "socket": "s", "pid": 0}

    def _stop_recorder(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("stop_recorder")
        (Path(kwargs["out_dir"]) / "current.json").unlink(missing_ok=True)
        events = [
            {"seq": 0, "t": 0.0, "type": "start"},
            {"seq": 1, "t": 1.0, "type": "mark", "label": "Hesitated", "say": "x"},
        ]
        (Path(kwargs["session_dir"]) / "timeline.json").write_text(
            json.dumps({"version": 1, "events": events}), encoding="utf-8"
        )
        return {"ok": True}

    def _kill_recorder(self, out_dir: Path, **kwargs: Any) -> None:
        self.calls.append("kill_recorder")
        (Path(out_dir) / "current.json").unlink(missing_ok=True)

    def _render_video(self, session_dir: Path, **kwargs: Any) -> tuple[Path, str]:
        self.calls.append("render_video")
        self.alive_at_render.append(self.chrome_alive)
        self.render_kwargs.append(kwargs)
        mp4 = Path(session_dir) / recording.VIDEO_FILE
        mp4.write_bytes(b"\0" * 2048)
        return mp4, "on" if kwargs.get("narration") else "off"

    def _server(self, argv: list[str]) -> dict[str, Any]:
        config = Path(argv[argv.index("--mcp-config") + 1])
        loaded = json.loads(config.read_text(encoding="utf-8"))
        self.mcp_configs.append(loaded)
        return loaded["mcpServers"][tool_profiles.STORY_SERVER]

    def _journal(self, server: dict[str, Any]) -> None:
        """Write the attempt's journal as the story server would, by its script."""
        args = server["args"]
        out = Path(args[args.index("--out") + 1])
        start = json.loads(Path(args[args.index("--start") + 1]).read_text("utf-8"))
        budget = int(args[args.index("--budget") + 1])
        key = f"{start['story']}@{start['persona']}"
        planned = self.script.get(key, ["done"])
        outcome = planned[min(start["attempt"], len(planned)) - 1]
        out.mkdir(parents=True)
        (out / "screens").mkdir()
        (out / "screens" / "001.jpg").write_bytes(b"\xff\xd8\xff" + b"\0" * 64)
        journal = ux_story_mcp.JournalWriter(
            out / ux_story_mcp.JOURNAL_FILE, start, relay=True, budget=budget
        )
        used = 0
        for tool in ("navigate_page", "click", "click"):
            used += 1
            journal.call(
                tool=tool,
                cls="action",
                ok=True,
                counted=True,
                actions_used=used,
                images_sent=used,
                screen=f"screens/{used:03d}.jpg",
            )
        journal.call(
            tool="note_confusion",
            cls="note",
            ok=True,
            args={"what": "Where is the upload?", "why": "Two buttons look alike."},
            actions_used=used,
            images_sent=used,
        )
        if outcome == "help":
            journal.call(
                tool="read_documentation",
                cls="doc",
                ok=False,
                label="help_link_unresolved",
                args={"ref": "/no-such-page/", "part": 1},
                actions_used=used,
                images_sent=used,
            )
        terminal = {
            "done": ("report_done", "done"),
            "help": ("report_done", "done"),
            "blocked": ("report_blocked", "blocked"),
            "offered": ("report_blocked", "blocked"),
            "refused": ("report_blocked", "blocked"),
        }.get(outcome)
        if terminal is not None:
            tool, kind = terminal
            if tool == "report_done":
                report = {
                    "evidence": "The page says so.",
                    "stopped_at_commit_point": True,
                }
            else:
                report = {
                    "reason": "Nothing on the page does it.",
                    "kind": "refused" if outcome == "refused" else "no_route",
                    "offered_a_control": outcome == "offered",
                }
            journal.call(
                tool=tool,
                cls="report",
                ok=True,
                args=report,
                actions_used=used,
                images_sent=used,
                terminal=kind,
                after_budget=False,
            )
        journal.end("terminal" if terminal is not None else "eof")

    def _run_agent(
        self, prompt: str, **kwargs: Any
    ) -> tuple[agent.AgentResult | None, processes.Completed]:
        self.calls.append("run_agent")
        argv = list(kwargs["argv"])
        self.argvs.append(argv)
        self.envs.append(dict(kwargs["env"]))
        self._journal(self._server(argv))
        if self.agent_error is not None:
            raise self.agent_error
        if self.timed_out:
            return None, processes.Completed(-9, "", "", True, 720.0)
        result = agent.AgentResult(
            text="I found the setting and stopped before saving.",
            cost_usd=self.cost_usd,
            num_turns=21,
            duration_s=300.0,
            stop=self.stop,
            is_error=False,
            usage=self.usage,
        )
        return result, processes.Completed(0, "{}", "", False, 300.0)

    def record(self) -> dict[str, Any]:
        return json.loads((self.artifacts / "story-run.json").read_text("utf-8"))

    def results(self) -> dict[str, Any]:
        return json.loads((self.artifacts / "results.json").read_text("utf-8"))

    def section(self) -> str:
        return (self.artifacts / "section.md").read_text(encoding="utf-8")


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StoryHarness:
    return StoryHarness(tmp_path, monkeypatch)


def _lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return capsys.readouterr().out.splitlines()


# --- one flow, confined -----------------------------------------------------------

ONE_FLOW = ("--as", "platform-admin")
FLOW_ORDER = [
    "launch_proxy",
    "launch_chrome",
    "socket_watch",
    "watch_started",
    "wait_for_cdp",
    "first_page_target",
    "open_session",
    "verify_policy",
    "verify_proxy_path",
    "sign_in",
    "page_facts",
    "verify_viewport",
    "session_closed",
    "start_recorder",
    "run_agent",
    "stop_recorder",
    "chrome_terminated",
    "kill_strays",
    "watch_stopped",
    "proxy_stopped",
]


@pytest.mark.unit
def test_one_flow_is_confined_before_its_persona_signs_in_and_audited_after_chrome(
    harness: StoryHarness,
) -> None:
    """The watch starts with Chrome, both self-tests precede the one sign-in, the
    recorder starts only once the page is signed in, and Chrome is killed before
    the audit's last pass, which precedes the proxy's stop."""
    assert harness.main(*ONE_FLOW) == 0
    assert harness.calls[0] == "index_docs"
    started = harness.calls.index("launch_proxy")
    assert harness.calls[started : started + len(FLOW_ORDER)] == FLOW_ORDER
    assert harness.calls.count("sign_in") == 1
    assert harness.sign_ins == [("Admin", {"base_url": WEB_URL})]
    assert harness.proxy_kwargs["build_dir"] is None
    assert f"{WEB_HOST}:443" in harness.proxy_kwargs["rules"].hosts
    (run_dir,) = harness.run_dirs
    assert run_dir.name.startswith("ux-test-stories-")
    assert not run_dir.exists()


@pytest.mark.unit
def test_every_attempt_gets_a_fresh_run_directory_and_a_bedrock_session_of_its_own(
    harness: StoryHarness,
) -> None:
    harness.script = {"route-folder-to-profile@new-analyst": ["done", "done"]}
    assert harness.main() == 0
    assert len(harness.run_dirs) == 3
    assert len({str(path) for path in harness.run_dirs}) == 3
    bedrock = [
        actions for actions in harness.sts.actions() if "polly" not in str(actions)
    ]
    assert len(bedrock) == 3
    for env in harness.envs:
        assert env["AWS_ACCESS_KEY_ID"] == SCOPED[0]
        assert env["ANTHROPIC_MODEL"] == MODEL
        assert "UX_TEST_PERSONAS" not in env and "GITLAB_REVIEW_TOKEN" not in env


@pytest.mark.unit
def test_claude_holds_the_story_server_alone_with_the_story_profile_and_the_effort(
    harness: StoryHarness,
) -> None:
    assert harness.main(*ONE_FLOW) == 0
    (argv,) = harness.argvs
    assert argv[argv.index("--tools") + 1] == ""
    start = argv.index("--allowedTools") + 1
    granted = argv[start : start + len(tool_profiles.STORY_ALLOWED_TOOLS)]
    assert tuple(granted) == tool_profiles.STORY_ALLOWED_TOOLS
    assert argv[-2:] == ["--effort", runner.DEFAULT_EFFORT]
    assert argv[argv.index("--max-budget-usd") + 1] == "4.00"
    budget = 50
    assert argv[argv.index("--max-turns") + 1] == str(stories.turn_cap(budget))
    (config,) = harness.mcp_configs
    assert list(config["mcpServers"]) == [tool_profiles.STORY_SERVER]
    server = config["mcpServers"][tool_profiles.STORY_SERVER]
    assert server["command"] == "/usr/bin/env" and server["args"][0] == "-i"
    assert server["env"] == {}
    args = server["args"]
    assert args[args.index("--budget") + 1] == str(budget)
    assert "--devtools" in args and "--recorder-out" in args


@pytest.mark.unit
def test_the_prompt_is_the_story_prompts_and_carries_no_stack_or_secret(
    harness: StoryHarness,
) -> None:
    assert harness.main(*ONE_FLOW) == 0
    (prompt,) = harness.prompts
    catalogue = stories.load_catalogue()
    expected = story_prompt.flow_prompt(
        catalogue,
        stories.FlowKey("route-folder-to-profile", "platform-admin"),
        texts=story_prompt.read_texts(),
    )
    assert prompt == expected
    for secret in (*PERSONAS["Admin"], WEB_HOST, ACCOUNT, CREDENTIAL):
        assert secret not in prompt


@pytest.mark.unit
def test_a_first_visit_story_keeps_its_welcome_page(harness: StoryHarness) -> None:
    assert harness.main("--as", "new-analyst", story="find-where-to-start") == 0
    assert harness.sign_ins == [
        ("Author", {"base_url": WEB_URL, "dismiss_welcome": False})
    ]


@pytest.mark.unit
def test_a_ui_build_is_served_and_signed_in_at_the_proxys_origin(
    harness: StoryHarness,
) -> None:
    build = harness.tmp / "build"
    build.mkdir()
    (build / "index.html").write_text("<!doctype html>", encoding="utf-8")
    assert harness.main(*ONE_FLOW, "--build-dir", str(build)) == 0
    assert harness.sign_ins == [("Admin", {})]
    assert harness.proxy_kwargs["build_dir"] == build.resolve()
    assert harness.record()["ui_source"]["kind"] == "build"


# --- the records ------------------------------------------------------------------


@pytest.mark.unit
def test_a_run_ends_with_what_it_cost_and_its_summary_holds_the_same_totals(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """results.json's summary totals what the budget counted, which is the run's
    spend, and what the sessions cost at the pinned rates; the final status line
    ends with both, and a re-run counts beside the first attempt."""
    assert harness.main() == 0
    record, results = harness.record(), harness.results()
    sessions = [flow["session"] for flow in record["flows"] if flow["session"]]
    assert [flow["attempt"] for flow in record["flows"]] == [1, 2, 1]
    assert len(sessions) == 3
    summary = results["summary"]
    assert summary["counted_usd"] == record["spend_usd"] > 0
    assert summary["pinned_usd"] == round(
        sum(session["cost_usd_pinned"] for session in sessions), 6
    )
    assert summary["sessions_without_usage"] == 0
    last = _lines(capsys)[-1]
    assert last.startswith("DRY RUN: nothing posted")
    assert last.endswith(
        f"; ${summary['counted_usd']:.2f} counted, "
        f"${summary['pinned_usd']:.2f} at the pinned rates"
    )


@pytest.mark.unit
def test_a_run_that_judged_no_flow_ends_without_a_cost(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.environ.pop("UX_TEST_PERSONAS")
    assert harness.main(*ONE_FLOW) == 0
    last = _lines(capsys)[-1]
    assert last.startswith("SKIPPED: ") and "counted" not in last


@pytest.mark.unit
def test_a_dry_run_leaves_the_run_record_results_section_and_flow_artifacts(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness.main(*ONE_FLOW) == 0
    lines = _lines(capsys)
    assert lines[-1].startswith("DRY RUN: nothing posted")
    assert lines[-2].startswith(
        "flow route-folder-to-profile@platform-admin: DONE, stopped before saving;"
    )
    record = harness.record()
    assert (record["schema"], record["version"]) == (story_verdict.RUN_SCHEMA, 1)
    assert record["mode"] == "local"
    assert record["judgement_revision"] == story_verdict.JUDGEMENT_REVISION
    assert record["prompt_revision"] == story_prompt.STORY_PROMPT_REVISION
    assert record["relay_revision"] == story_relay.RELAY_REVISION
    assert record["model"] == MODEL and record["effort"] == runner.DEFAULT_EFFORT
    assert record["docs"] == {
        "ref": None,
        "corpus_hash": "abcdef012345",
        "source": "worktree",
        "dirty": False,
    }
    assert record["stack"] == {
        "name": REVIEW,
        "version": "0.6.12.dev1",
        "build": "2026-10-19",
        "review_stack": True,
    }
    assert record["credential_scope"] == {"claude": "scoped", "render": None}
    (flow,) = record["flows"]
    assert (
        flow["journal"] == "flows/route-folder-to-profile@platform-admin/journal.jsonl"
    )
    assert flow["audit_state"] == "clean" and flow["session"]["stop"] == "end_turn"
    assert story_verdict.run_record(record)
    results = harness.results()
    assert results["flows"][0]["verdict"] == "done"
    folder = harness.artifacts / "flows" / "route-folder-to-profile@platform-admin"
    for name in ("journal.jsonl", "report.md", "refusals.jsonl", "audit.json"):
        assert (folder / name).is_file(), name
    assert (folder / "screens" / "001.jpg").is_file()
    assert harness.section().startswith("<!-- ux-story: sha=")


@pytest.mark.unit
def test_judge_check_reproduces_both_outputs_byte_for_byte(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.script = {"route-folder-to-profile@new-analyst": ["help"]}
    assert harness.main() == 0
    capsys.readouterr()
    before = (harness.results(), harness.section())
    assert (
        runner.main(["--judge", str(harness.artifacts), "--check"], harness.runtime())
        == 0
    )
    assert _lines(capsys) == []
    assert runner.main(["--judge", str(harness.artifacts)], harness.runtime()) == 0
    assert (harness.results(), harness.section()) == before
    assert story_verdict.main(["judge", str(harness.artifacts), "--check"]) == 0


@pytest.mark.unit
def test_judge_check_names_the_first_difference_in_each_output(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness.main(*ONE_FLOW) == 0
    results = harness.results()
    results["flows"][0]["verdict"] = "stuck"
    (harness.artifacts / "results.json").write_text(json.dumps(results), "utf-8")
    section = harness.section().replace("DONE", "STUCK")
    (harness.artifacts / "section.md").write_text(section, encoding="utf-8")
    capsys.readouterr()
    code = runner.main(
        ["--judge", str(harness.artifacts), "--check"], harness.runtime()
    )
    assert code == 1
    assert _lines(capsys) == [
        "different\tresults.json\t$.flows[0].verdict",
        f"different\tsection.md\tline {_first_changed_line(harness)}",
    ]


def _first_changed_line(harness: StoryHarness) -> int:
    lines = harness.section().split("\n")
    return next(n for n, line in enumerate(lines, start=1) if "STUCK" in line)


@pytest.mark.unit
def test_judge_check_refuses_another_judgement_revision_without_comparing(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness.main(*ONE_FLOW) == 0
    record = harness.record()
    record["judgement_revision"] = story_verdict.JUDGEMENT_REVISION + 1
    (harness.artifacts / "story-run.json").write_text(json.dumps(record), "utf-8")
    capsys.readouterr()
    code = runner.main(
        ["--judge", str(harness.artifacts), "--check"], harness.runtime()
    )
    assert code == 3
    assert _lines(capsys) == [
        f"judgement_revision\trun={story_verdict.JUDGEMENT_REVISION + 1}\t"
        f"judge={story_verdict.JUDGEMENT_REVISION}"
    ]


@pytest.mark.unit
def test_a_run_no_mr_chose_says_how_it_chose_and_names_its_ui_without_an_mr(
    harness: StoryHarness,
) -> None:
    assert harness.main(*ONE_FLOW) == 0
    lines = harness.section().splitlines()
    assert lines[3] == "Chosen by hand: route-folder-to-profile as platform admin"
    assert "the UI is the stack's own deployment" in harness.section()
    assert "this MR" not in harness.section().split("**Caveats:**", 1)[1]
    record = harness.record()
    assert record["reply"]["chosen_by"] == {
        "kind": "story",
        "flows": ["route-folder-to-profile@platform-admin"],
    }
    del record["reply"]["chosen_by"]
    (harness.artifacts / "story-run.json").write_text(json.dumps(record), "utf-8")
    assert runner.main(["--judge", str(harness.artifacts)], harness.runtime()) == 0
    assert harness.section().splitlines()[3] == (
        "Chosen from this MR's CHANGELOG lines: none"
    )
    argv = ["--judge", str(harness.artifacts), "--check"]
    assert runner.main(argv, harness.runtime()) == 0


@pytest.mark.unit
def test_a_run_of_another_judgement_revision_is_rewritten_only_on_request(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness.main(*ONE_FLOW) == 0
    other = story_verdict.JUDGEMENT_REVISION + 1
    record = harness.record()
    record["judgement_revision"] = other
    (harness.artifacts / "story-run.json").write_text(json.dumps(record), "utf-8")
    results = harness.artifacts / "results.json"
    written = results.read_bytes()
    section = (harness.artifacts / "section.md").read_bytes()
    capsys.readouterr()
    argv = ["--judge", str(harness.artifacts), "--summary"]
    assert runner.main(argv, harness.runtime()) == 3
    assert _lines(capsys) == [
        f"judgement_revision\trun={other}\tjudge={story_verdict.JUDGEMENT_REVISION}",
        story_verdict.other_revision_line(other),
    ]
    assert results.read_bytes() == written
    assert (harness.artifacts / "section.md").read_bytes() == section
    assert runner.main([*argv, "--rejudge"], harness.runtime()) == 0
    lines = _lines(capsys)
    assert lines[1].startswith("WARNING: re-judged under JUDGEMENT_REVISION")
    with pytest.raises(SystemExit):
        runner.parse_args(["--judge", "x", "--check", "--rejudge"])


@pytest.mark.unit
def test_judge_summary_prints_the_structured_table_and_nothing_of_the_run(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness.main(*ONE_FLOW) == 0
    capsys.readouterr()
    assert (
        runner.main(["--judge", str(harness.artifacts), "--summary"], harness.runtime())
        == 0
    )
    lines = _lines(capsys)
    assert lines[0] == "ux-test-stories summary v1"
    assert lines[3].startswith("flow\tattempt\tverdict")
    assert "Two buttons look alike" not in "\n".join(lines)


@pytest.mark.unit
def test_the_artifact_journal_is_redacted_and_still_judged_from(
    harness: StoryHarness,
) -> None:
    """A persona's password the model typed into a note is gone from the artifact
    journal, which still reads as v1 and is what the verdict comes from."""
    secret = PERSONAS["Admin"][1]
    original = harness._journal

    def leaky(server: dict[str, Any]) -> None:
        original(server)
        args = server["args"]
        path = Path(args[args.index("--out") + 1]) / "journal.jsonl"
        text = path.read_text(encoding="utf-8").replace("Two buttons", secret[:20])
        path.write_text(text, encoding="utf-8")

    harness._journal = leaky  # type: ignore[method-assign]
    harness.environ["UX_TEST_PERSONAS"] = _b64(
        json.dumps(
            {"Admin": {"username": "ux-admin@example.invalid", "password": secret[:20]}}
        )
    )
    assert harness.main(*ONE_FLOW) == 0
    folder = harness.artifacts / "flows" / "route-folder-to-profile@platform-admin"
    journal = (folder / "journal.jsonl").read_text(encoding="utf-8")
    assert secret[:20] not in journal
    assert story_verdict.read_journal(folder / "journal.jsonl").start.story
    assert harness.results()["flows"][0]["verdict"] == "done"


@pytest.mark.unit
def test_a_redacted_leaf_never_grows_past_the_bound_it_was_written_within() -> None:
    scrub = runner.artifacts.Scrub(runner.artifacts.redactor_for({}))
    scrub.redactor.add("abcdef")
    raw = json.dumps({"v": 1, "what": "abcdef"}, separators=(",", ":")) + "\n"
    torn = raw + '{"v":1,"type":"call"'
    copied = runner.redacted_journal(torn.encode(), scrub).decode()
    assert copied == '{"v":1,"what":"[redac"}\n'


# --- videos, of the review stack alone ----------------------------------------------


def _bad_and_good(harness: StoryHarness) -> None:
    harness.script = {"route-folder-to-profile@new-analyst": ["help", "help"]}


@pytest.mark.unit
def test_a_flow_with_a_finding_gets_one_video_rendered_after_every_chrome_is_gone(
    harness: StoryHarness,
) -> None:
    _bad_and_good(harness)
    assert harness.main() == 0
    assert harness.calls.count("render_video") == 1
    assert harness.alive_at_render == [0]
    assert harness.calls.index("render_video") > max(
        index for index, name in enumerate(harness.calls) if name == "chrome_terminated"
    )
    polly = [actions for actions in harness.sts.actions() if "polly" in str(actions)]
    assert len(polly) == 1
    (rendered,) = harness.render_kwargs
    assert rendered["aws"]["AWS_ACCESS_KEY_ID"] == POLLY[0]
    assert rendered["user"] == paths.RENDER_USER
    videos = sorted(path.name for path in harness.artifacts.glob("*.mp4"))
    assert videos == ["ux-story-route-folder-to-profile--new-analyst-1.mp4"]
    record = harness.record()
    shown = [flow for flow in record["flows"] if flow["video"]]
    assert [flow["key"] for flow in shown] == ["route-folder-to-profile@new-analyst"]
    assert shown[0]["chapters"] == [["0:00", "Opened the application"]]
    assert record["credential_scope"]["render"] == "scoped"
    section = harness.section()
    assert (
        "_Video of `route-folder-to-profile@new-analyst`, not uploaded in a dry run: "
        "`ux-story-route-folder-to-profile--new-analyst-1.mp4`._"
    ) in section.splitlines()


@pytest.mark.unit
def test_the_video_check_is_sessions_own_called_with_the_run_and_the_flows_page(
    harness: StoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, dict[str, str], str | None]] = []

    def check(stack: str, ui_env: Any, shown: str | None) -> str:
        seen.append((stack, dict(ui_env), shown))
        return ""

    monkeypatch.setattr(session, "review_stack_problem", check)
    harness.facts["Author"] = dataclasses.replace(
        harness.default_facts, stack_name=None
    )
    assert harness.main() == 0
    assert [(stack, shown) for stack, _env, shown in seen] == [
        (REVIEW, None),
        (REVIEW, REVIEW),
        (REVIEW, None),
    ]
    assert seen[0][1]["VITE_SETTINGS_PARAMETER"] == f"{REVIEW}-Settings"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stack", "settings", "shown", "reason"),
    [
        (REVIEW, f"{REVIEW}-Settings", REVIEW, ""),
        (OTHER_STACK, f"{REVIEW}-Settings", REVIEW, STACK_REASON),
        (LOOKALIKE_STACK, f"{REVIEW}-Settings", REVIEW, STACK_REASON),
        (REVIEW, f"{LOOKALIKE_STACK}-Settings", REVIEW, ENV_REASON),
        (REVIEW, f"{REVIEW}-settings", REVIEW, ENV_REASON),
        (REVIEW, None, REVIEW, ENV_REASON),
        (REVIEW, f"{REVIEW}-Settings", None, PAGE_REASON),
        (REVIEW, f"{REVIEW}-Settings", LOOKALIKE_STACK, PAGE_REASON),
    ],
)
def test_every_statement_of_the_stack_must_name_the_review_stack_for_a_flows_video(
    harness: StoryHarness,
    stack: str,
    settings: str | None,
    shown: str | None,
    reason: str,
) -> None:
    """A refused flow runs and is judged like any other, records nothing, renders
    nothing, mints no Polly session and leaves no mp4; the reply names it and its
    reason, and the record says the run was not the review stack's alone."""
    harness.script = {"route-folder-to-profile@platform-admin": ["help"]}
    harness.environ["UX_TEST_UI_ENV"] = _b64(_ui_env_text(settings))
    harness.default_facts = dataclasses.replace(harness.default_facts, stack_name=shown)
    if stack != REVIEW:
        harness.environ["UX_TEST_UI_ENV"] = _b64(
            _ui_env_text(settings).replace(
                "idp-ux-nightly-inputbucket", f"{stack}-inputbucket"
            )
        )
    assert harness.main(*ONE_FLOW, "--stack", stack) == 0
    record = harness.record()
    verdict = harness.results()["flows"][0]["verdict"]
    assert verdict == "done"
    if not reason:
        assert harness.calls.count("render_video") == 1
        assert record["stack"]["review_stack"] is True
        assert record["video_withheld"] is None
        return
    assert "start_recorder" not in harness.calls
    assert "render_video" not in harness.calls
    assert not [a for a in harness.sts.actions() if "polly" in str(a)]
    assert not list(harness.artifacts.glob("*.mp4"))
    assert record["stack"]["review_stack"] is False
    assert record["video_withheld"] == reason
    assert record["video_withheld_flows"] == [
        {
            "key": "route-folder-to-profile@platform-admin",
            "attempt": 1,
            "reason": reason,
        }
    ]
    lines = harness.section().splitlines()
    withheld = [line for line in lines if "was withheld" in line]
    assert withheld == [
        f"_The video of `route-folder-to-profile@platform-admin` was withheld, "
        f"because {reason}._"
    ]
    assert f"_A story's video is made only of the review stack `{REVIEW}`" in "\n".join(
        lines
    )


@pytest.mark.unit
def test_an_unconfirmed_page_is_never_called_another_stack_in_the_reply(
    harness: StoryHarness,
) -> None:
    harness.default_facts = dataclasses.replace(harness.default_facts, stack_name=None)
    assert harness.main(*ONE_FLOW) == 0
    section = harness.section()
    assert "could not be confirmed as the review stack" in section
    assert "another stack" not in section
    assert "another" not in "\n".join(
        line
        for line in section.splitlines()
        if "withheld" in line or "made only" in line
    )


@pytest.mark.unit
def test_the_reply_names_every_withheld_flow_with_its_own_reason(
    harness: StoryHarness,
) -> None:
    harness.facts["Author"] = dataclasses.replace(
        harness.default_facts, stack_name=None
    )
    harness.facts["Admin"] = dataclasses.replace(
        harness.default_facts, stack_name=LOOKALIKE_STACK
    )
    assert harness.main() == 0
    withheld = [
        line for line in harness.section().splitlines() if "was withheld" in line
    ]
    assert withheld == [
        f"_The video of `route-folder-to-profile@new-analyst` was withheld, because "
        f"{PAGE_REASON}._",
        f"_The video of `route-folder-to-profile@new-analyst` was withheld, because "
        f"{PAGE_REASON}._",
        f"_The video of `route-folder-to-profile@platform-admin` was withheld, because "
        f"{PAGE_REASON}._",
    ]
    assert harness.record()["video_withheld"] == PAGE_REASON


@pytest.mark.unit
def test_video_never_records_nothing_and_says_nothing_about_a_stack(
    harness: StoryHarness,
) -> None:
    _bad_and_good(harness)
    assert harness.main("--video", "never") == 0
    assert "start_recorder" not in harness.calls
    assert "render_video" not in harness.calls
    assert harness.record()["stack"]["review_stack"] is True


@pytest.mark.unit
def test_a_failed_render_costs_that_video_and_the_reply_says_so(
    harness: StoryHarness,
) -> None:
    _bad_and_good(harness)

    def broken(session_dir: Path, **kwargs: Any) -> tuple[Path | None, str]:
        raise recording.RecorderError("render exited 1")

    harness._render_video = broken  # type: ignore[method-assign]
    assert harness.main() == 0
    assert (
        "_The video of `route-folder-to-profile@new-analyst` could not be rendered._"
        in (harness.section().splitlines())
    )
    assert not list(harness.artifacts.glob("*.mp4"))


# --- gates, re-runs and spend -----------------------------------------------------


@pytest.mark.unit
def test_a_bad_verdict_runs_once_more_and_two_agreeing_attempts_are_confirmed(
    harness: StoryHarness,
) -> None:
    assert harness.main() == 0
    results = harness.results()
    attempts = [(flow["key"], flow["attempt"]) for flow in results["flows"]]
    assert attempts == [
        ("route-folder-to-profile@new-analyst", 1),
        ("route-folder-to-profile@new-analyst", 2),
        ("route-folder-to-profile@platform-admin", 1),
    ]
    assert harness.calls.count("run_agent") == 3
    second = [f for f in harness.record()["flows"] if f["attempt"] == 2][0]
    assert second["journal"] == (
        "flows/route-folder-to-profile@new-analyst/attempt-2/journal.jsonl"
    )
    assert (
        "OFFERED a control the docs say this role lacks (2 of 2)" in harness.section()
    )


@pytest.mark.unit
def test_reruns_zero_leaves_a_bad_verdict_unconfirmed(harness: StoryHarness) -> None:
    assert harness.main("--reruns", "0") == 0
    assert harness.calls.count("run_agent") == 2
    assert "(unconfirmed (1 of 1))" in harness.section()


@pytest.mark.unit
def test_a_rerun_the_budget_cannot_cover_is_no_attempt_and_is_named_in_the_record(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness.main("--run-usd", "6.0") == 0
    lines = _lines(capsys)
    assert (
        "rerun: route-folder-to-profile@new-analyst is not run again (budget)" in lines
    )
    assert [f["attempt"] for f in harness.record()["flows"]] == [1, 1]
    assert harness.record()["reruns_not_run"] == [
        {"key": "route-folder-to-profile@new-analyst", "reason": "budget"}
    ]


@pytest.mark.unit
def test_a_page_that_read_no_build_neither_changes_the_stack_nor_hides_it(
    harness: StoryHarness,
) -> None:
    harness.facts["Author"] = signin.PageFacts(
        None, None, None, (1600, 1000, 1.0), None
    )
    assert harness.main("--reruns", "0") == 0
    record = harness.record()
    assert record["stack_changed_mid_run"] is False
    assert (record["stack"]["version"], record["stack"]["build"]) == (
        "0.6.12.dev1",
        "2026-10-19",
    )
    del harness.facts["Author"]
    harness.facts["Admin"] = dataclasses.replace(
        harness.default_facts, build="2026-10-20"
    )
    harness.artifacts = harness.tmp / "changed" / "ux-test-stories"
    assert harness.main("--reruns", "0") == 0
    assert harness.record()["stack_changed_mid_run"] is True


@pytest.mark.unit
def test_a_flow_starts_only_while_the_budget_covers_its_whole_cap(
    harness: StoryHarness,
) -> None:
    harness.cost_usd = 3.5
    assert harness.main("--run-usd", "6.0", "--reruns", "0") == 0
    flows = {flow["key"]: flow for flow in harness.record()["flows"]}
    assert flows["route-folder-to-profile@new-analyst"]["session"] is not None
    assert flows["route-folder-to-profile@platform-admin"]["not_run_reason"] == "budget"
    assert harness.calls.count("launch_chrome") == 1
    verdicts = {flow["key"]: flow["verdict"] for flow in harness.results()["flows"]}
    assert verdicts["route-folder-to-profile@platform-admin"] == "not_run"


@pytest.mark.unit
def test_a_flow_starts_only_while_the_time_left_covers_it_and_the_reserve(
    harness: StoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalogue = stories.load_catalogue()
    need = catalogue.limits.flow_minutes + catalogue.runs.reserve_minutes
    assert harness.main(*ONE_FLOW, "--deadline-minutes", str(need - 1)) == 0
    (flow,) = harness.record()["flows"]
    assert flow["not_run_reason"] == "time"
    assert "launch_chrome" not in harness.calls


@pytest.mark.unit
def test_the_time_a_run_takes_to_prepare_is_counted_against_its_deadline_once(
    harness: StoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = datetime.now(timezone.utc) - timedelta(seconds=120)
    harness.environ["CI_JOB_STARTED_AT"] = started.isoformat()
    index = harness._index_docs

    def slow_index(**kwargs: Any) -> runner.DocsCorpus:
        harness.now += 120.0
        return index(**kwargs)

    monkeypatch.setattr(harness, "_index_docs", slow_index)
    catalogue = stories.load_catalogue()
    need = catalogue.limits.flow_minutes + catalogue.runs.reserve_minutes
    assert harness.main(*ONE_FLOW, "--deadline-minutes", str(need + 3)) == 0
    (flow,) = harness.record()["flows"]
    assert flow["not_run_reason"] is None and "launch_chrome" in harness.calls


@pytest.mark.unit
@pytest.mark.parametrize(
    ("deadline", "timeout"),
    [
        ((), runner._RENDER_MAX_SECONDS),
        (("--deadline-minutes", "600"), runner._RENDER_MAX_SECONDS),
        (("--deadline-minutes", "25"), 25 * 60.0),
    ],
    ids=["no-deadline", "far-deadline", "near-deadline"],
)
def test_a_render_takes_the_time_left_up_to_its_own_bound(
    harness: StoryHarness, deadline: tuple[str, ...], timeout: float
) -> None:
    """A run with no deadline renders within the render's own bound rather than an
    unbounded time, and a run with one within whichever of the two comes first,
    so the timeout render is given is always a finite number of seconds."""
    harness.script = {"route-folder-to-profile@platform-admin": ["stuck"]}
    assert harness.main(*ONE_FLOW, "--video", "all", "--reruns", "0", *deadline) == 0
    assert harness.render_kwargs
    for kwargs in harness.render_kwargs:
        assert math.isfinite(kwargs["timeout"])
        assert kwargs["timeout"] == timeout


@pytest.mark.unit
def test_a_run_with_no_deadline_never_turns_a_flow_away_for_time(
    harness: StoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no deadline there is no time left to run out of, however long the run
    has taken, so no flow is recorded as not run for time and every video is
    rendered."""
    index = harness._index_docs

    def slow_index(**kwargs: Any) -> runner.DocsCorpus:
        harness.now += 10 * 24 * 3600.0
        return index(**kwargs)

    monkeypatch.setattr(harness, "_index_docs", slow_index)
    harness.script = {"route-folder-to-profile@platform-admin": ["stuck"]}
    assert harness.main(*ONE_FLOW, "--video", "all", "--reruns", "0") == 0
    (flow,) = harness.record()["flows"]
    assert flow["not_run_reason"] is None and "launch_chrome" in harness.calls
    assert harness.calls.count("render_video") == 1


@pytest.mark.unit
def test_a_session_the_run_is_cancelled_in_counts_at_its_whole_cap(
    harness: StoryHarness,
) -> None:
    harness.agent_error = processes.Terminated("SIGTERM")
    assert harness.main(*ONE_FLOW) == 1
    record = harness.record()
    (flow,) = record["flows"]
    assert flow["session"] is not None
    assert record["spend_usd"] == stories.load_catalogue().limits.flow_usd


@pytest.mark.unit
def test_a_model_with_no_pinned_rates_is_refused_at_the_precheck(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = harness.argv(*ONE_FLOW)
    argv[argv.index("--model") + 1] = "us.anthropic.claude-opus-4-8"
    assert runner.main(argv, harness.runtime()) == 1
    assert "has no pinned rates in MODEL_RATES" in _lines(capsys)[-1]
    assert "launch_chrome" not in harness.calls


@pytest.mark.unit
def test_the_spend_is_the_larger_of_the_clis_figure_and_the_usage_at_pinned_rates(
    harness: StoryHarness,
) -> None:
    harness.cost_usd = 0.01
    harness.usage = {
        "input_tokens": 10_000,
        "output_tokens": 20_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 50_000,
    }
    assert harness.main(*ONE_FLOW) == 0
    (flow,) = harness.record()["flows"]
    pinned = round(10_000 * 4e-6 + 20_000 * 2e-5 + 1_000_000 * 2e-7 + 50_000 * 5e-6, 6)
    assert flow["session"]["cost_usd"] == 0.01
    assert flow["session"]["cost_usd_pinned"] == pinned
    assert flow["session"]["counted_usd"] == pinned
    assert harness.record()["spend_usd"] == pinned
    claude = harness.results()["flows"][0]["claude"]
    assert claude["usage"] == harness.usage


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cost", "pinned", "cap", "counted"),
    [
        (1.0, 2.0, 4.0, 2.0),
        (3.0, 2.0, 4.0, 3.0),
        (None, 2.0, 4.0, 2.0),
        (None, None, 4.0, 4.0),
    ],
)
def test_counted_cost_takes_the_larger_known_figure_and_else_the_whole_cap(
    cost: float | None, pinned: float | None, cap: float, counted: float
) -> None:
    assert runner.counted_cost(cost, pinned, cap) == counted


@pytest.mark.unit
def test_an_unreadable_answer_is_a_harness_error_counted_at_its_cap(
    harness: StoryHarness,
) -> None:
    harness.agent_error = agent.AgentError("claude did not return JSON: 'x'")
    assert harness.main(*ONE_FLOW) == 0
    (flow,) = harness.record()["flows"]
    assert flow["harness_error"] == "session"
    assert flow["session"]["counted_usd"] == 4.0
    assert harness.results()["flows"][0]["verdict"] == "error"


@pytest.mark.unit
def test_the_pinned_rates_are_pricing_yamls_for_every_model_they_name() -> None:
    yaml = pytest.importorskip("yaml")
    pricing = yaml.safe_load(
        (REPO_ROOT / "config_library" / "pricing.yaml").read_text(encoding="utf-8")
    )
    entries = {
        entry["name"]: {unit["name"]: float(unit["price"]) for unit in entry["units"]}
        for entry in pricing["pricing"]
    }
    for model, rates in runner.MODEL_RATES.items():
        units = entries[f"bedrock/{model}"]
        assert rates == {
            "input": units["inputTokens"],
            "output": units["outputTokens"],
            "cache_read": units["cacheReadInputTokens"],
            "cache_write": units["cacheWriteInputTokens"],
        }, model
    assert runner.DEFAULT_MODEL in runner.MODEL_RATES


@pytest.mark.unit
def test_a_refused_sign_in_marks_its_groups_other_flows_not_run_and_is_not_retried(
    harness: StoryHarness,
) -> None:
    harness.refused_sign_in = {"Author"}
    assert harness.main(story="read-a-processed-document") == 0
    flows = {flow["key"]: flow for flow in harness.record()["flows"]}
    assert flows["read-a-processed-document@new-analyst"]["harness_error"] == "sign_in"
    assert [name for name, _ in harness.sign_ins].count("Author") == 1
    verdicts = {f["key"]: f["verdict"] for f in harness.results()["flows"]}
    assert verdicts["read-a-processed-document@new-analyst"] == "error"
    assert verdicts["read-a-processed-document@platform-admin"] == "done"
    harness.calls.clear()
    harness.sign_ins.clear()
    harness.artifacts = harness.tmp / "again" / "ux-test-stories"
    harness.refused_sign_in = {"Viewer"}
    assert harness.main(story="find-extraction-model") == 0
    flows = {flow["key"]: flow for flow in harness.record()["flows"]}
    assert flows["find-extraction-model@team-lead"]["harness_error"] == "sign_in"


@pytest.mark.unit
def test_a_group_missing_from_the_personas_is_not_run_without_a_browser(
    harness: StoryHarness,
) -> None:
    harness.environ["UX_TEST_PERSONAS"] = _personas("Admin")
    assert harness.main() == 0
    flows = {flow["key"]: flow for flow in harness.record()["flows"]}
    assert flows["route-folder-to-profile@new-analyst"]["not_run_reason"] == "persona"
    assert harness.calls.count("launch_chrome") == 1


@pytest.mark.unit
def test_a_stack_older_than_a_storys_since_is_not_run_once_the_version_is_known(
    harness: StoryHarness,
) -> None:
    harness.default_facts = dataclasses.replace(harness.default_facts, version="0.6.4")
    assert harness.main() == 0
    flows = {flow["key"]: flow for flow in harness.record()["flows"]}
    assert flows["route-folder-to-profile@new-analyst"]["not_run_reason"] == "since"
    assert flows["route-folder-to-profile@platform-admin"]["not_run_reason"] == "since"
    assert harness.calls.count("launch_chrome") == 1
    assert "run_agent" not in harness.calls


@pytest.mark.unit
@pytest.mark.parametrize(
    ("step", "error", "kind"),
    [
        ("wait_for_cdp", chrome.ChromeError("DevTools did not answer"), "launch"),
        ("verify_policy", chrome.ChromeError("the policy is not enforced"), "selftest"),
        ("verify_viewport", chrome.ChromeError("not 1600x1000"), "viewport"),
    ],
)
def test_a_failed_browser_step_is_that_flows_harness_error_and_the_run_goes_on(
    harness: StoryHarness, step: str, error: Exception, kind: str
) -> None:
    def failing(*args: Any, **kwargs: Any) -> Any:
        harness.calls.append(step)
        raise error

    runtime = harness.runtime()
    setattr(runtime, step, failing)
    assert runner.main(harness.argv(*ONE_FLOW), runtime) == 0
    (flow,) = harness.record()["flows"]
    assert flow["harness_error"] == kind
    assert harness.results()["flows"][0]["verdict"] == "error"
    assert "chrome_terminated" in harness.calls and "watch_stopped" in harness.calls


@pytest.mark.unit
def test_a_socket_the_browser_must_not_have_opened_fails_the_run_after_the_record(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.audit_state = "violations"
    assert harness.main(*ONE_FLOW) == 1
    assert _lines(capsys)[-1].startswith(
        "FAILED: a flow's browser had a socket or a process it must not have"
    )
    assert harness.results()["flows"][0]["verdict"] == "error"


@pytest.mark.unit
def test_a_stray_process_of_the_browser_fails_the_run(harness: StoryHarness) -> None:
    harness.strays = ("pid 4180 (chrome_crashpad)",)
    assert harness.main(*ONE_FLOW) == 1
    (flow,) = harness.record()["flows"]
    assert flow["strays"] == 1


@pytest.mark.unit
def test_a_cancel_mid_flow_still_cleans_up_and_records_what_ran(
    harness: StoryHarness,
) -> None:
    def cancelled(prompt: str, **kwargs: Any) -> Any:
        harness.calls.append("run_agent")
        raise processes.Terminated("received signal 15")

    runtime = harness.runtime()
    runtime.run_agent = cancelled
    assert runner.main(harness.argv(*ONE_FLOW), runtime) == 1
    assert "chrome_terminated" in harness.calls and "proxy_stopped" in harness.calls
    assert harness.calls.count("kill_recorder") == 1
    assert (
        harness.record()["flows"][0]["key"] == "route-folder-to-profile@platform-admin"
    )


@pytest.mark.unit
def test_an_ambient_session_is_said_once_and_recorded_as_ambient(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.sts.refuse = True
    assert harness.main() == 0
    lines = _lines(capsys)
    assert sum("no Bedrock-only session could be minted" in line for line in lines) == 1
    assert harness.record()["credential_scope"]["claude"] == "ambient"
    assert all(env["AWS_ACCESS_KEY_ID"] == AWS_KEY_ID for env in harness.envs)
    assert AWS_KEY_TEXT not in "\n".join(lines)


@pytest.mark.unit
def test_no_secret_reaches_a_printed_line_or_an_artifact(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.script = {"route-folder-to-profile@new-analyst": ["help", "help"]}
    assert harness.main() == 0
    out = capsys.readouterr().out
    texts = [out] + [
        path.read_text(encoding="utf-8", errors="replace")
        for path in harness.artifacts.rglob("*")
        if path.is_file() and path.suffix in (".json", ".jsonl", ".md")
    ]
    for secret in (
        *PERSONAS["Admin"],
        *PERSONAS["Author"],
        AWS_KEY_TEXT,
        AWS_SESSION_TEXT,
        SCOPED[1],
        POLLY[1],
        ACCOUNT,
        WEB_HOST,
        "us-east-1_AbCdEfGhI",
    ):
        for text in texts:
            assert secret not in text, secret


@pytest.mark.unit
@pytest.mark.parametrize("variable", ["CI", "GITLAB_CI"])
def test_dev_unconfined_is_refused_wherever_ci_is_set(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str], variable: str
) -> None:
    harness.environ[variable] = "true"
    assert harness.main("--dev-unconfined") == 1
    assert harness.calls == []
    assert _lines(capsys) == [
        "FAILED: --dev-unconfined is for a development host's own Chrome, and "
        f"{variable} is set, so this is CI"
    ]


@pytest.mark.unit
def test_a_dev_unconfined_run_skips_the_policy_test_and_says_so_in_its_record(
    harness: StoryHarness,
) -> None:
    assert harness.main("--dev-unconfined") == 0
    assert "verify_policy" not in harness.calls
    assert "measure_viewport" in harness.calls
    assert "verify_viewport" not in harness.calls
    assert harness.record()["confinement"] == "dev-unconfined"


@pytest.mark.unit
def test_a_run_records_its_whole_confinement(harness: StoryHarness) -> None:
    assert harness.main() == 0
    assert harness.record()["confinement"] == "full"
    assert "verify_policy" in harness.calls and "verify_viewport" in harness.calls


@pytest.mark.unit
def test_an_audit_is_folded_only_into_a_paired_run(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness.main(*ONE_FLOW) == 0
    audit = harness.tmp / "browser-audit.json"
    audit.write_text(json.dumps({"state": "violations", "strays": ["p"]}), "utf-8")
    capsys.readouterr()
    argv = ["--judge", str(harness.artifacts), "--audit", str(audit)]
    assert runner.main(argv, harness.runtime()) == 1
    assert _lines(capsys)[0].startswith("FAILED: --audit folds a paired run's")
    path = harness.artifacts / story_verdict.RUN_FILE
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**record, "mode": "pair"}), encoding="utf-8")
    assert runner.main(argv, harness.runtime()) == 0
    assert harness.results()["flows"][0]["verdict"] == "error"
    assert harness.record()["flows"][0]["strays"] == 1


# --- the harness, the imports and the command line --------------------------------


def _python_files(entry: str) -> list[Path]:
    path = REPO_ROOT / entry
    if path.is_dir():
        return sorted(path.rglob("*.py"))
    return [path] if path.suffix == ".py" else []


def _resolved(importer: Path, node: ast.Import | ast.ImportFrom) -> list[Path]:
    """The scripts/ or scripts/sdlc/ files an import statement can load."""
    if isinstance(node, ast.ImportFrom) and node.level:
        package = importer.parents[node.level - 1]
        module = package.joinpath(*node.module.split(".")) if node.module else package
        candidates = [module.with_suffix(".py"), module / "__init__.py"]
        candidates += [module / f"{alias.name}.py" for alias in node.names]
        return [c for c in candidates if c.is_file()]
    if isinstance(node, ast.Import):
        dotted = [alias.name for alias in node.names]
    else:
        dotted = [node.module or ""]
        dotted += [f"{node.module}.{alias.name}" for alias in node.names]
    targets: list[Path] = []
    for name in dotted:
        for base in (SCRIPTS, SCRIPTS / "sdlc"):
            module = base.joinpath(*name.split("."))
            for candidate in (module.with_suffix(".py"), module / "__init__.py"):
                if candidate.is_file():
                    targets.append(candidate)
    return targets


def _covered(path: Path) -> bool:
    relative = path.resolve().relative_to(REPO_ROOT).as_posix()
    return any(
        relative == entry or relative.startswith(entry + "/")
        for entry in runner.HARNESS_PATHS
    )


@pytest.mark.unit
def test_every_harness_import_is_itself_in_the_harness() -> None:
    """No module the story job runs is imported from the MR's checkout, which
    nothing but this list keeps out of the pinned tree; the closure is neither
    larger nor smaller than what the files import."""
    files = [f for entry in runner.HARNESS_PATHS for f in _python_files(entry)]
    assert len(files) > 20
    unaccounted = sorted(
        {
            f"{importer.relative_to(REPO_ROOT)} imports {target.relative_to(REPO_ROOT)}"
            for importer in files
            for node in ast.walk(ast.parse(importer.read_text(encoding="utf-8")))
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for target in _resolved(importer, node)
            if not _covered(target)
        }
    )
    assert not unaccounted, f"unaccounted harness import: {unaccounted}"


@pytest.mark.unit
def test_every_harness_entry_exists_and_holds_the_entry_the_server_and_the_skills() -> (
    None
):
    absent = [e for e in runner.HARNESS_PATHS if not (REPO_ROOT / e).exists()]
    assert not absent, f"HARNESS_PATHS names paths the tree does not have: {absent}"
    assert len(set(runner.HARNESS_PATHS)) == len(runner.HARNESS_PATHS)
    for path in (
        runner.ENTRY,
        runner.OPERATOR_SKILL,
        runner.CI_CONTRACT,
        runner.STORY_SERVER_SCRIPT,
        "scripts/ux_stories.toml",
        "docs-site/setup.sh",
    ):
        assert path in runner.HARNESS_PATHS, path
    assert Path(runner.ENTRY) == SCRIPT.relative_to(REPO_ROOT)


@pytest.mark.unit
def test_ai_mr_review_is_reached_only_through_gitlab_api() -> None:
    assert runner.review is gitlab_api.review
    for node in ast.walk(ast.parse(SCRIPT.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert not any(name.split(".")[0] == "ai_mr_review" for name in names)


def _heavy_imports(*argv: str) -> tuple[int, list[str], str]:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-X", "importtime", "-B", str(SCRIPT), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    imported = re.findall(r"\|\s+(\S+)\s*$", completed.stderr, flags=re.M)
    heavy = sorted({m for m in imported if m.split(".")[0] in ("boto3", "yaml", "PIL")})
    return completed.returncode, heavy, completed.stdout


@pytest.mark.unit
def test_help_runs_on_a_bare_python_without_the_pinned_packages() -> None:
    code, heavy, out = _heavy_imports("--help")
    assert code == 0 and "--judge" in out
    assert not heavy, f"--help imported {heavy}"


@pytest.mark.unit
def test_judge_runs_on_a_bare_python_on_any_host(harness: StoryHarness) -> None:
    assert harness.main(*ONE_FLOW) == 0
    code, heavy, out = _heavy_imports(
        "--judge", str(harness.artifacts), "--check", "--summary"
    )
    assert code == 0, out
    assert not heavy, f"--judge imported {heavy}"


@pytest.mark.unit
def test_the_script_imports_on_a_python_without_sigkill_as_on_windows() -> None:
    """``--judge`` runs on the host, a Windows host included, where
    ``signal.SIGKILL`` does not exist."""
    program = "\n".join(
        (
            "import runpy, signal, sys",
            "del signal.SIGKILL",
            f"sys.argv = [{str(SCRIPT)!r}, '--help']",
            f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')",
        )
    )
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-400:]


@pytest.mark.unit
@pytest.mark.parametrize(
    "argv",
    [
        ["--story", "x", "--audit", "a.json"],
        ["--story", "x", "--check"],
        ["--story", "x", "--dev-video-any-stack"],
        ["--story", "x", "--dev-video"],
        ["--sto", "x"],
    ],
)
def test_the_command_line_refuses_what_no_mode_reads(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        runner.parse_args(argv)
    assert raised.value.code == 2


@pytest.mark.unit
def test_no_option_renders_a_video_of_another_stack() -> None:
    parser_text = SCRIPT.read_text(encoding="utf-8")
    assert "any-stack" not in parser_text
    args = runner.parse_args(["--story", "x"])
    assert args.video == "findings" and args.stack == REVIEW
    assert not any("dev_video" in name for name in vars(args))


@pytest.mark.unit
def test_the_pinned_versions_are_the_installs_own_pins() -> None:
    """The precheck's series, before the install, use exactly what VERSIONS.json
    records after it: the lockfile's two packages and install.sh's Chrome pin."""
    pinned = runner.pinned_versions()
    lock = json.loads(
        (SCRIPTS / "ux_runner" / "package-lock.json").read_text(encoding="utf-8")
    )["packages"]
    assert (
        pinned["claude_code"]
        == lock["node_modules/@anthropic-ai/claude-code"]["version"]
    )
    assert (
        pinned["chrome_devtools_mcp"]
        == lock["node_modules/chrome-devtools-mcp"]["version"]
    )
    install = (SCRIPTS / "ux_runner" / "install.sh").read_text(encoding="utf-8")
    assert f'CFT_VERSION="{pinned["chrome"]}"' in install
    assert runner.pinned_versions(Path("/nonexistent")) == dict.fromkeys(
        ("claude_code", "chrome_devtools_mcp", "chrome"), "unknown"
    )


@pytest.mark.unit
def test_a_series_names_the_prompt_and_both_skill_texts_as_the_prompt_inlines_them() -> (
    None
):
    texts = story_prompt.read_texts()
    inputs = runner.series_inputs(
        model=MODEL,
        effort="high",
        prompt_revision=story_prompt.STORY_PROMPT_REVISION,
        versions=runner.pinned_versions(),
        texts=texts,
    )
    assert inputs.brief_sha256 == texts.brief_sha256
    assert inputs.ci_contract_sha256 == texts.ci_contract_sha256
    assert inputs.relay_revision == story_relay.RELAY_REVISION
    assert inputs.judgement_revision == story_verdict.JUDGEMENT_REVISION
    changed = dataclasses.replace(inputs, effort="max")
    story = stories.load_catalogue().stories[0]
    digest = stories.story_hash(stories.load_catalogue(), story)
    assert stories.series_hash(digest, "x", inputs) != stories.series_hash(
        digest, "x", changed
    )


@pytest.mark.unit
def test_the_harness_revision_falls_back_on_the_jobs_variable_then_unknown(
    tmp_path: Path,
) -> None:
    named = "ab" * 20
    assert runner.harness_revision({"UX_HARNESS_SHA": named}, tmp_path) == (
        named,
        False,
    )
    assert runner.harness_revision({"UX_HARNESS_SHA": f"{named}+dirty"}, tmp_path) == (
        named,
        True,
    )
    assert runner.harness_revision({"UX_HARNESS_SHA": "nope"}, tmp_path) == (
        "unknown",
        None,
    )
    assert runner.journal_harness("unknown", None) == "unknown"
    assert runner.journal_harness(named, True) == f"{named}+dirty"


@pytest.mark.unit
def test_the_default_runtime_binds_the_real_collaborators() -> None:
    runtime = runner.default_runtime()
    assert runtime.launch_proxy is egress.launch_proxy
    assert runtime.launch_chrome is chrome.launch
    assert runtime.sign_in is signin.sign_in
    assert runtime.socket_watch is chrome.SocketWatch
    assert runtime.kill_strays is chrome.kill_strays
    assert runtime.run_agent is agent.run_agent
    assert runtime.render_video is recording.render_video
    assert runtime.index_docs is runner.index_docs
    assert runtime.compose_prompt is runner.compose_prompt
    assert runtime.harness is runner.harness_revision
    members = dict(session.BrowserRuntime.__annotations__)
    fields = {f.name for f in dataclasses.fields(runner.Runtime)}
    assert set(members) <= fields


def _develop_backend_re() -> re.Pattern[str]:
    """``_DEVELOP_BACKEND_RE`` of the MR UX test's suite, compiled from its source,
    so a shape of the claim that suite learns to refuse is refused here too."""
    source = (SCRIPTS / "tests" / "test_ai_ux_test.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if (
            isinstance(node, ast.Assign)
            and [ast.unparse(target) for target in node.targets]
            == ["_DEVELOP_BACKEND_RE"]
            and isinstance(node.value, ast.Call)
        ):
            pattern, *flag_nodes = node.value.args
            flags = 0
            for flag_node in flag_nodes:
                for name in ast.unparse(flag_node).split("|"):
                    flags |= getattr(re, name.strip().partition(".")[2])
            return re.compile(ast.literal_eval(pattern), flags)
    raise AssertionError("test_ai_ux_test.py no longer defines _DEVELOP_BACKEND_RE")


@pytest.mark.unit
def test_the_reply_names_the_backend_as_the_ui_reports_it_never_as_develops(
    harness: StoryHarness,
) -> None:
    assert harness.main(*ONE_FLOW) == 0
    section = harness.section()
    claim = _develop_backend_re().search(section)
    assert claim is None, claim
    caveats = next(
        line for line in section.splitlines() if line.startswith("**Caveats:**")
    )
    assert f"backend: `{REVIEW}` v0.6.12.dev1, build 2026-10-19" in caveats
    assert "backend facts as the UI reports them" in caveats


@pytest.mark.unit
def test_refusals_are_counted_by_origin_and_only_the_pages_reach_the_reply(
    harness: StoryHarness,
) -> None:
    harness.refusals = [
        *harness.refusals,
        {
            "t": "2026-10-20T00:00:01Z",
            "method": "CONNECT",
            "target": "tracker.example:443",
            "reason": "host_not_allowed",
        },
    ]
    assert harness.main(*ONE_FLOW) == 0
    (flow,) = harness.record()["flows"]
    assert flow["refusals_by_origin"] == {"page": 1, "browser": 0, "selftest": 1}
    folder = harness.artifacts / "flows" / "route-folder-to-profile@platform-admin"
    records = [
        json.loads(line)
        for line in (folder / "refusals.jsonl").read_text("utf-8").splitlines()
    ]
    assert [record["origin"] for record in records] == ["selftest", "page"]
    assert (
        "refused 1 of the page's requests (`host_not_allowed` ×1)" in harness.section()
    )


# --- the real helpers, with their own collaborators faked -------------------------


@pytest.mark.unit
def test_docs_from_a_commit_are_read_from_git_objects_beside_the_harness_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[str, ...]] = []

    def git(repo: Path, *args: str) -> str:
        asked.append(args)
        return {
            "rev-parse": f"{HEAD}\n" if args[-1].endswith("{commit}") else DOCS_TREE
        }[args[0]]

    snapshots: list[dict[str, Any]] = []

    def snapshot(repo: Path, ref: str, dest: Path, **kwargs: Any) -> Any:
        snapshots.append({"ref": ref, "dest": dest, **kwargs})
        return docs_index.Snapshot(root=dest, ref=ref, files=1, bytes=10, refused=())

    built: list[docs_index.DocsSource] = []

    def build(root: Path, *, source: docs_index.DocsSource) -> docs_index.Index:
        built.append(source)
        return dataclasses.replace(_index(), source=source)

    monkeypatch.setattr(runner, "_git", git)
    monkeypatch.setattr(docs_index, "snapshot_from_git", snapshot)
    monkeypatch.setattr(docs_index, "build", build)
    corpus = runner.index_docs(repo_dir=tmp_path, ref="develop", work_dir=tmp_path)
    assert asked == [
        ("rev-parse", "--verify", "develop^{commit}"),
        ("rev-parse", "--verify", f"{HEAD}:docs"),
    ]
    (taken,) = snapshots
    assert taken["ref"] == HEAD and taken["git"] is git
    assert taken["setup_sh"] == paths.HARNESS_ROOT / "docs-site" / "setup.sh"
    assert built == [docs_index.DocsSource(kind="git", ref=HEAD, dirty=None)]
    assert (corpus.ref, corpus.tree, corpus.journal_ref) == (HEAD, DOCS_TREE, HEAD)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "dirty"), [("", False), (" M docs/a.md\n", True), (None, True)]
)
def test_a_working_trees_docs_are_dirty_when_git_says_so_or_cannot_say(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str | None, dirty: bool
) -> None:
    def git(repo: Path, *args: str) -> str:
        if status is None:
            raise runner.ui_change.UiChangeError("not a git repository")
        return status

    monkeypatch.setattr(runner, "_git", git)
    monkeypatch.setattr(
        docs_index,
        "build",
        lambda root, *, source: dataclasses.replace(_index(), source=source),
    )
    corpus = runner.index_docs(repo_dir=tmp_path, ref=None, work_dir=tmp_path)
    assert corpus.index.source == docs_index.DocsSource("worktree", None, dirty)
    assert corpus.journal_ref == ("worktree+dirty" if dirty else "worktree")


@pytest.mark.unit
def test_an_unreadable_docs_ref_is_a_story_run_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def git(repo: Path, *args: str) -> str:
        raise runner.ui_change.UiChangeError("unknown revision")

    monkeypatch.setattr(runner, "_git", git)
    with pytest.raises(runner.StoryRunError, match="cannot be indexed"):
        runner.index_docs(repo_dir=tmp_path, ref="nope", work_dir=tmp_path)


@pytest.mark.unit
def test_the_harness_revision_is_gits_with_dirty_meaning_a_harness_path_differs(
    tmp_path: Path,
) -> None:
    git = ["git", "-C", str(tmp_path)]
    for argv in (
        ["init", "-q"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "t"],
    ):
        subprocess.run([*git, *argv], check=True)  # noqa: S603
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ux_stories.toml").write_text("version = 1\n", "utf-8")
    (tmp_path / "unrelated.txt").write_text("x", "utf-8")
    subprocess.run([*git, "add", "-A"], check=True)  # noqa: S603
    subprocess.run([*git, "commit", "-qm", "c"], check=True)  # noqa: S603
    head = subprocess.run(  # noqa: S603
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert runner.harness_revision({}, tmp_path) == (head, False)
    (tmp_path / "unrelated.txt").write_text("changed", "utf-8")
    assert runner.harness_revision({}, tmp_path) == (head, False)
    (tmp_path / "scripts" / "ux_stories.toml").write_text("version = 2\n", "utf-8")
    assert runner.harness_revision({}, tmp_path) == (head, True)


@pytest.mark.unit
def test_a_prompt_the_builder_refuses_is_a_story_run_error() -> None:
    catalogue = stories.load_catalogue()
    with pytest.raises(runner.StoryRunError, match="cannot be built"):
        runner.compose_prompt(
            story=catalogue.story("route-folder-to-profile"),
            persona=catalogue.persona("team-lead"),
            budget=30,
            texts=story_prompt.read_texts(),
        )


# --- the run's edges --------------------------------------------------------------


@pytest.mark.unit
def test_a_recording_that_does_not_stop_costs_only_that_flows_video(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    _bad_and_good(harness)

    def failing(**kwargs: Any) -> dict[str, Any]:
        raise recording.RecorderError("the recorder did not stop")

    runtime = harness.runtime()
    runtime.stop_recorder = failing
    assert runner.main(harness.argv(), runtime) == 0
    assert "render_video" not in harness.calls
    lines = _lines(capsys)
    assert any(line.startswith("WARNING: the recording of") for line in lines)
    assert harness.results()["flows"][0]["verdict"] == "offered"


@pytest.mark.unit
def test_a_recorder_that_does_not_start_is_that_flows_harness_error(
    harness: StoryHarness,
) -> None:
    def failing(**kwargs: Any) -> dict[str, Any]:
        raise recording.RecorderError("the recorder did not start")

    runtime = harness.runtime()
    runtime.start_recorder = failing
    assert runner.main(harness.argv(*ONE_FLOW), runtime) == 0
    (flow,) = harness.record()["flows"]
    assert flow["harness_error"] == "recorder"
    assert "run_agent" not in harness.calls


@pytest.mark.unit
def test_a_journal_that_is_a_link_is_refused_and_the_flow_judged_without_one(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    original = harness._journal

    def linked(server: dict[str, Any]) -> None:
        original(server)
        args = server["args"]
        path = Path(args[args.index("--out") + 1]) / "journal.jsonl"
        target = path.with_name("real.jsonl")
        path.rename(target)
        path.symlink_to(target)

    harness._journal = linked  # type: ignore[method-assign]
    assert harness.main(*ONE_FLOW) == 0
    assert any(
        "the journal of" in line and "refused" in line for line in _lines(capsys)
    )
    (flow,) = harness.record()["flows"]
    assert flow["journal"] is None
    assert harness.results()["flows"][0]["verdict"] == "error"


@pytest.mark.unit
def test_a_cleanup_step_that_fails_is_named_and_the_others_still_run(
    harness: StoryHarness,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _close_session(self: Any) -> None:
        raise RuntimeError("no")

    monkeypatch.setattr(runner.FlowSession, "_close_session", _close_session)
    assert harness.main(*ONE_FLOW) == 0
    assert "cleanup: close_session: RuntimeError" in _lines(capsys)
    assert "proxy_stopped" in harness.calls


@pytest.mark.unit
def test_a_record_that_is_not_v1_fails_the_judge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], harness: StoryHarness
) -> None:
    (tmp_path / "story-run.json").write_text('{"schema": "x"}', encoding="utf-8")
    assert runner.main(["--judge", str(tmp_path)], harness.runtime()) == 1
    assert _lines(capsys)[0].startswith("FAILED: story-run.json")


@pytest.mark.unit
def test_a_reply_record_that_cannot_be_composed_is_refused() -> None:
    record = {"reply": {"head_sha": "short", "sel": "x"}, "stack": {}}
    with pytest.raises(story_verdict.RunRecordError, match="reply cannot be composed"):
        runner.compose_section(record, [], [])
    assert runner.compose_section({}, [], []) is None


@pytest.mark.unit
def test_video_notes_show_only_closed_shapes_from_a_record() -> None:
    record = {
        "video_withheld_flows": [
            {"key": "a-story@x", "reason": "`--stack` is not the review stack"},
            {"key": "not a key](http://x)", "reason": "[click](http://evil)"},
            "junk",
        ],
        "reply": {
            "video_notes": [
                {"key": "a-story@x", "kind": "local", "detail": "a`b.mp4"},
                {"key": "a-story@x", "kind": "unknown", "detail": "z"},
            ]
        },
    }
    lines = runner.video_note_lines(record)
    assert lines[0] == (
        "_The video of `a-story@x` was withheld, because `--stack` is not the review "
        "stack._"
    )
    assert lines[1].startswith("_The video of `?` was withheld, because [")
    assert "](http" not in lines[1]
    assert lines[2].startswith("_A story's video is made only of the review stack")
    assert lines[3] == "_Video of `a-story@x`, not uploaded in a dry run: `a'b.mp4`._"
    assert len(lines) == 4
