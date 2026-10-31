# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/sdlc/ai_ux_test_stories.py, the first-time-user story orchestrator.

The orchestrator is driven end to end through :func:`ai_ux_test_stories.main` with a
fully fake :class:`ai_ux_test_stories.Runtime`: no subprocess, browser, network or
AWS call. The fake ``claude`` writes each flow's journal with the story server's own
``JournalWriter``, so every verdict here is the real judge's over a real v1 journal,
and the GitLab side is the real ``gitlab_api.UxGitlab`` over an in-memory GitLab.

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
* nothing is posted without ``--post``, ``--post`` needs a merge request and the
  review stack and every audit clean, and the reply goes in the review's thread;
* the precheck's harness pin, its merge request decisions, the shard and merge
  record rules, the digest entry and the watched run's transcript;
* the harness list is closed over its imports, and ``--help``, ``--judge``,
  ``--merge`` and ``--digest`` import nothing outside the standard library.
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import html
import json
import math
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
    mr_inputs,
    paths,
    processes,
    recording,
    session,
    signin,
    stories,
    story_digest,
    story_prompt,
    story_relay,
    story_report,
    story_section,
    story_verdict,
    tool_profiles,
)
from ux_runner.ui_change import ChangedFile  # noqa: E402

review = gitlab_api.review

IID = 812
ME = 4242
HEAD = "0123456789abcdef" * 2 + "01234567"
BASE = "fedcba9876543210" * 2 + "fedcba98"
NEWER = "89abcdef" * 5
DOCS_TREE = "abcdef12" * 5
UI_TREE = "1234abcd" * 5
ACCOUNT = "123456789012"
PARTITION = "aws"
MODEL = "us.anthropic.claude-opus-5-5"
CREDENTIAL = "test-review-credential-value"
AWS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
AWS_KEY_TEXT = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
AWS_SESSION_TEXT = "FwoGZXIvYXdzEXAMPLEambientsessionvalue"
SCOPED = ("ASIASCOPEDKEYEXAMPLE", "scopedSecretKeyValue01", "scopedSessionValue0001")
POLLY = ("ASIAPOLLYKEY0EXAMPLE", "pollySecretKeyValue001", "pollySessionValue00001")
WEB_HOST = "d111111abcdef8.cloudfront.net"
WEB_URL = f"https://{WEB_HOST}/"
UPLOAD_HASH = "0123456789abcdef0123456789abcdef"
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


class FakeGitlab(gitlab_api.UxGitlab):
    """The real client over an in-memory GitLab: only the transport is replaced."""

    def __init__(self, *, head: str = HEAD, draft: bool = False) -> None:
        super().__init__("https://gitlab.test/api/v4", "group/proj", CREDENTIAL)
        self.head = head
        self.draft = draft
        self.store: dict[int, dict[str, Any]] = {}
        self.threads: dict[str, list[int]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.log: list[str] | None = None
        self.pipelines: list[int] = []
        self.jobs: dict[int, list[dict[str, Any]]] = {}
        self.traces: dict[int, bytes] = {}
        self._next = 1000

    def add_note(
        self, body: str, *, author: int = ME, thread: str | None = None
    ) -> int:
        self._next += 1
        ident = self._next
        self.store[ident] = {
            "id": ident,
            "body": body,
            "author": {"id": author},
            "system": False,
            "created_at": f"2026-10-20T00:{ident % 60:02d}:00Z",
        }
        self.threads.setdefault(thread or f"thread-{ident}", []).append(ident)
        return ident

    def writes(self) -> list[tuple[str, str, Any]]:
        return [r for r in self.requests if r[0] in ("POST", "PUT", "DELETE")]

    def _send(
        self,
        method: str,
        url: str,
        *,
        body: dict | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
        timeout: int = 60,
        empty_ok: bool = False,
        as_bytes: bool = False,
        limit: int | None = None,
        head: bool = False,
    ) -> tuple[object, dict[str, str]]:
        path = urlsplit(url).path.removeprefix("/api/v4")
        self.requests.append((method, path, body if body is not None else data))
        if self.log is not None and method in ("POST", "PUT", "DELETE"):
            self.log.append("forge_write")
        project = "/projects/group%2Fproj"
        mr = f"{project}/merge_requests/{IID}"
        if method == "GET" and path == "/user":
            return {"id": ME, "username": "ux-review-bot"}, {}
        if method == "GET" and path == mr:
            return {
                "iid": IID,
                "title": ("Draft: " if self.draft else "") + "Route by prefix",
                "author": {"username": "jdoe"},
                "source_branch": "feature/prefix",
                "target_branch": "develop",
                "web_url": f"https://gitlab.test/group/proj/-/merge_requests/{IID}",
                "sha": self.head,
                "diff_refs": {"head_sha": self.head},
                "draft": self.draft,
            }, {}
        if method == "GET" and path == f"{mr}/notes":
            return [dict(self.store[i]) for i in sorted(self.store)], {}
        if method == "GET" and path == f"{mr}/pipelines":
            return [{"id": ident} for ident in self.pipelines], {}
        found = re.fullmatch(re.escape(project) + r"/pipelines/(\d+)/jobs", path)
        if found and method == "GET":
            return list(self.jobs.get(int(found.group(1)), [])), {}
        found = re.fullmatch(re.escape(project) + r"/jobs/(\d+)/trace", path)
        if found and method == "GET":
            return self.traces.get(int(found.group(1)), b"")[: limit or None], {}
        found = re.fullmatch(re.escape(mr) + r"/notes/(\d+)", path)
        if found and method in ("GET", "PUT"):
            note = self.store[int(found.group(1))]
            if method == "PUT" and body is not None:
                note["body"] = body["body"]
            return dict(note), {}
        if method == "POST" and path == f"{mr}/notes" and body is not None:
            return dict(self.store[self.add_note(body["body"])]), {}
        if method == "GET" and path == f"{mr}/discussions":
            listed = [
                {"id": thread, "notes": [{"id": i} for i in ids]}
                for thread, ids in self.threads.items()
            ]
            return listed, {}
        found = re.fullmatch(re.escape(mr) + r"/discussions/([^/]+)/notes", path)
        if found and method == "POST" and body is not None:
            return dict(self.store[self.add_note(body["body"], thread=found[1])]), {}
        if method == "POST" and path == f"{project}/uploads" and data is not None:
            name = re.search(rb'filename="([^"]+)"', data)
            filename = name.group(1).decode() if name else "upload"
            return {"id": 456, "url": f"/uploads/{UPLOAD_HASH}/{filename}"}, {}
        raise review.Failure(f"the fake GitLab has no route for {method} {path}")


def _review_body(head: str) -> str:
    return (
        f"<!-- ai-review: sha={head} rev=1 -->\n## PR/MR Review: !{IID}\n\n"
        f"🤖 Automated review of `{head[:8]}` by `{MODEL}` over the full diff.\n"
    )


#: What the fake ``claude`` does in one attempt, by name (``StoryHarness.script``).
OUTCOMES = ("done", "blocked", "offered", "refused", "help", "silent")


class StoryHarness:
    """The fake runtime and every input of one run, with a log of what was called."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.calls: list[str] = []
        self.environ = _environ()
        self.sts = FakeSts()
        self.forge = FakeGitlab()
        self.forge.log = self.calls
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
        self.transcript: list[dict[str, Any]] | None = None
        self.harness_sha = "feedface" * 5
        self.tui_kwargs: dict[str, Any] = {}
        self.tui_prompts: list[tuple[str, int]] = []
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
            forge=self._forge,
            collect_story=self._collect_story,
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
            run_tui=self._run_tui,
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

    def _forge(self, args: Any, credential: str) -> FakeGitlab:
        return self.forge

    def _collect_story(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("collect_story")
        return _story_inputs().to_json()

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

    def _run_tui(self, argv: list[str], **kwargs: Any) -> processes.Completed:
        self.calls.append("run_tui")
        self.argvs.append(list(argv))
        self.tui_kwargs = kwargs
        prompt = Path(argv[argv.index("--append-system-prompt-file") + 1])
        self.tui_prompts.append(
            (prompt.read_text(encoding="utf-8"), stat.S_IMODE(prompt.stat().st_mode))
        )
        self._journal(self._server(argv))
        env = kwargs["env"]
        if self.transcript is not None:
            folder = Path(env["HOME"]) / ".claude" / "projects" / "-tmp-cwd"
            folder.mkdir(parents=True)
            (folder / "session.jsonl").write_text(
                "".join(json.dumps(line) + "\n" for line in self.transcript),
                encoding="utf-8",
            )
        return processes.Completed(0, "", "", False, 200.0)

    def record(self) -> dict[str, Any]:
        return json.loads((self.artifacts / "story-run.json").read_text("utf-8"))

    def results(self) -> dict[str, Any]:
        return json.loads((self.artifacts / "results.json").read_text("utf-8"))

    def section(self) -> str:
        return (self.artifacts / "section.md").read_text(encoding="utf-8")


def _story_inputs(
    *,
    changed: tuple[ChangedFile, ...] = (ChangedFile("M", "src/ui/src/App.tsx"),),
    head: str = HEAD,
    changelog: str | None = None,
) -> runner.StoryInputs:
    lead = (
        "Configuration Profiles can be assigned by S3 prefix, so the destination "
        "decides the configuration."
    )
    added = changelog or (
        f"- **{lead}** See [it](docs/configuration-profiles.md"
        "#prefix-mappings-assigning-a-profile-by-where-a-document-lands) and [how "
        "a document records it](docs/configuration-profiles.md"
        "#profile-tracking-in-document-processing).\n"
    )
    return runner.StoryInputs(
        mr=mr_inputs.MrInputs(
            iid=IID,
            title="Route by prefix",
            author="jdoe",
            web_url=f"https://gitlab.test/group/proj/-/merge_requests/{IID}",
            source_branch="feature/prefix",
            target_branch="develop",
            head_sha=head,
            base_sha=BASE,
            changed=changed,
            ui_diff="",
            ui_diff_truncated=False,
            ui_diff_bytes=0,
            title_from_git=True,
        ),
        changelog_base="## [Unreleased]\n\n### Added\n\n",
        changelog_head=f"## [Unreleased]\n\n### Added\n\n{added}",
        docs_tree=DOCS_TREE,
        src_ui_tree=UI_TREE,
        docs_sections_edited=(),
    )


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
    _pinned(harness)
    argv = harness.argv(*ONE_FLOW)
    argv[argv.index("--model") + 1] = "us.anthropic.claude-opus-4-8"
    assert runner.main([*argv, "--precheck"], harness.runtime()) == 1
    record = json.loads((harness.artifacts / "precheck.json").read_text("utf-8"))
    assert record["status"] == "failed"
    assert "has no pinned rates in MODEL_RATES" in record["detail"]
    assert runner.main(argv, harness.runtime()) == 1
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


# --- merge requests ---------------------------------------------------------------


def _mr(harness: StoryHarness, inputs: runner.StoryInputs | None = None) -> list[str]:
    """Arguments of an MR run whose inputs.json and UI build are in place."""
    path = harness.tmp / "inputs.json"
    path.write_text(json.dumps((inputs or _story_inputs()).to_json()), "utf-8")
    build = harness.repo / "src" / "ui" / "build"
    build.mkdir(parents=True, exist_ok=True)
    (build / "index.html").write_text("<!doctype html>", encoding="utf-8")
    harness.environ.update(GITLAB_REVIEW_TOKEN=CREDENTIAL)
    return [
        "--mr",
        str(IID),
        "--inputs",
        str(path),
        "--project",
        "group/proj",
        "--api-url",
        "https://gitlab.test/api/v4",
    ]


def _pinned(harness: StoryHarness) -> None:
    harness.environ.update(
        UX_HARNESS=" ".join(runner.HARNESS_PATHS), UX_ENTRY=runner.ENTRY
    )


def _precheck(harness: StoryHarness, *extra: str) -> tuple[int, dict[str, Any]]:
    _pinned(harness)
    code = runner.main(harness.argv(*extra, "--precheck", story=""), harness.runtime())
    record = json.loads((harness.artifacts / "precheck.json").read_text("utf-8"))
    return code, record


@pytest.mark.unit
def test_an_mr_run_tests_its_build_with_the_flows_its_changelog_selects(
    harness: StoryHarness,
) -> None:
    assert runner.main(harness.argv(*_mr(harness), story=""), harness.runtime()) == 0
    record = harness.record()
    assert record["mode"] == "mr"
    assert [flow["key"] for flow in record["flows"]][0] == (
        "route-folder-to-profile@new-analyst"
    )
    assert record["ui_source"] == {
        "kind": "build",
        "head_sha": HEAD,
        "src_ui_tree": UI_TREE,
    }
    assert record["docs"]["ref"] == HEAD
    assert harness.index_kwargs["ref"] == HEAD
    assert harness.sign_ins[0] == ("Admin", {})
    section = harness.section()
    assert section.startswith(
        f"<!-- ux-story: sha={HEAD} rev={story_section.STORY_REPLY_REVISION} sel="
    )
    assert "kind=run upload=none -->" in section.split("\n", 1)[0]
    assert "- Added: `Configuration Profiles can be assigned by S3 prefix" in section
    assert "docs from this MR's head (`01234567`)" in section
    assert "the UI is this MR's build" in section
    assert not harness.forge.writes()


@pytest.mark.unit
def test_nothing_is_posted_without_post_even_with_a_token_and_a_review_note(
    harness: StoryHarness,
) -> None:
    harness.forge.add_note(_review_body(HEAD), thread="review-thread")
    assert runner.main(harness.argv(*_mr(harness), story=""), harness.runtime()) == 0
    assert not harness.forge.writes()


def _cost(run_dir: Path) -> str:
    """The cost a run's final line ends with, from its own results.json."""
    results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    return runner.story_verdict.cost_text(results["summary"])


@pytest.mark.unit
def test_with_post_one_reply_goes_in_the_review_thread_and_the_review_is_untouched(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    review_id = harness.forge.add_note(_review_body(HEAD), thread="review-thread")
    harness.script = {"route-folder-to-profile@new-analyst": ["help", "help"]}
    code = runner.main(
        harness.argv(*_mr(harness), "--post", story=""), harness.runtime()
    )
    assert code == 0
    assert _lines(capsys)[-1] == (
        "POSTED: first-time-user stories of 01234567 replied in the code review's "
        f"thread; {_cost(harness.artifacts)}"
    )
    assert harness.forge.store[review_id]["body"] == _review_body(HEAD)
    replies = [i for i in harness.forge.threads["review-thread"] if i != review_id]
    (reply_id,) = replies
    body = harness.forge.store[reply_id]["body"]
    marker = story_section.STORY_MARKER_RE.match(body)
    assert marker is not None and marker.group("upload") == "456"
    video = f"![UX story](/uploads/{UPLOAD_HASH}/ux-story-mr{IID}-01234567-1.mp4)"
    assert video in body.splitlines()
    assert "not uploaded in a dry run" not in body
    assert body == harness.section()
    record = harness.record()
    assert record["reply"]["uploads"] == [456]


@pytest.mark.unit
def test_without_a_review_note_the_reply_is_posted_on_its_own(
    harness: StoryHarness,
) -> None:
    code = runner.main(
        harness.argv(*_mr(harness), "--post", "--review-poll-minutes", "0", story=""),
        harness.runtime(),
    )
    assert code == 0
    (note,) = harness.forge.store.values()
    assert story_section.STANDALONE_LINE in note["body"].split("\n")[1]


@pytest.mark.unit
def test_post_refuses_any_stack_but_the_review_stack(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = harness.argv(*_mr(harness), "--post", "--stack", OTHER_STACK, story="")
    assert runner.main(argv, harness.runtime()) == 1
    assert _lines(capsys) == [
        f"FAILED: --post replies only for runs against the review stack {REVIEW}"
    ]
    assert harness.calls == []


@pytest.mark.unit
def test_post_needs_every_flows_audit_clean(harness: StoryHarness) -> None:
    harness.audit_state = "unavailable"
    harness.forge.add_note(_review_body(HEAD), thread="review-thread")
    code = runner.main(
        harness.argv(*_mr(harness), "--post", story=""), harness.runtime()
    )
    assert code == 1
    assert not [w for w in harness.forge.writes() if w[0] == "POST"]


@pytest.mark.unit
@pytest.mark.parametrize("variable", ["CI", "GITLAB_CI"])
def test_narrating_with_own_credentials_is_refused_wherever_ci_is_set(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str], variable: str
) -> None:
    harness.environ[variable] = "true"
    assert harness.main("--narrate-with-own-credentials") == 1
    assert harness.calls == []
    assert _lines(capsys) == [
        "FAILED: --narrate-with-own-credentials is for a developer's own machine, "
        f"and {variable} is set, so this is CI"
    ]


@pytest.mark.unit
def test_own_credentials_narrate_a_video_when_no_polly_session_can_be_minted(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """On a host that cannot re-assume its own role, the opt-in hands render the
    run's own credentials, says so once and records it, rather than none."""
    _bad_and_good(harness)
    harness.sts.refuse = True
    assert harness.main("--narrate-with-own-credentials") == 0
    (rendered,) = harness.render_kwargs
    assert rendered["narration"] is True
    assert rendered["aws"] == {
        "AWS_ACCESS_KEY_ID": AWS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": AWS_KEY_TEXT,
        "AWS_SESSION_TOKEN": AWS_SESSION_TEXT,
    }
    assert harness.record()["credential_scope"]["render"] == "ambient"
    lines = _lines(capsys)
    said = [line for line in lines if "narrated with the run's own AWS" in line]
    assert len(said) == 1 and said[0].startswith("WARNING:")
    assert AWS_KEY_TEXT not in "\n".join(lines)


@pytest.mark.unit
def test_without_the_opt_in_a_refused_polly_session_renders_without_a_voice(
    harness: StoryHarness,
) -> None:
    _bad_and_good(harness)
    harness.sts.refuse = True
    assert harness.main() == 0
    (rendered,) = harness.render_kwargs
    assert rendered["narration"] is False
    assert rendered["aws"] == {}
    assert harness.record()["credential_scope"]["render"] == "unavailable"


@pytest.mark.unit
def test_the_opt_in_changes_nothing_when_a_polly_session_is_minted(
    harness: StoryHarness,
) -> None:
    _bad_and_good(harness)
    assert harness.main("--narrate-with-own-credentials") == 0
    (rendered,) = harness.render_kwargs
    assert rendered["aws"]["AWS_ACCESS_KEY_ID"] == POLLY[0]
    assert harness.record()["credential_scope"]["render"] == "scoped"


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
def test_post_is_refused_outside_a_merge_request_run() -> None:
    with pytest.raises(SystemExit):
        runner.parse_args(["--story", "find-where-to-start", "--post"])
    with pytest.raises(SystemExit):
        runner.parse_args(["--all", "--post"])


@pytest.mark.unit
def test_a_draft_or_a_moved_head_stops_at_the_precheck(harness: StoryHarness) -> None:
    harness.forge.draft = True
    code, record = _precheck(harness, *_mr(harness))
    assert (code, record["status"], record["detail"]) == (
        0,
        "skipped",
        "a draft; drafts are not run",
    )
    harness.forge.draft = False
    harness.forge.head = NEWER
    code, record = _precheck(harness, *_mr(harness))
    assert record["detail"].startswith("the MR head moved to 89abcdef")
    assert not (harness.artifacts / "proceed").exists()
    assert "launch_proxy" not in harness.calls


@pytest.mark.unit
def test_an_mr_no_story_covers_stops_with_its_uncovered_leads_listed(
    harness: StoryHarness,
) -> None:
    inputs = _story_inputs(changelog="- **aiohttp is updated to 3.14.5.**\n")
    code, record = _precheck(harness, *_mr(harness, inputs))
    assert (code, record["status"]) == (0, "skipped")
    assert record["detail"] == "no story covers this MR's CHANGELOG entries"
    assert record["uncovered"] == ["aiohttp is updated to 3.14.5."]


@pytest.mark.unit
def test_which_ui_an_mr_tests_follows_what_it_changes(harness: StoryHarness) -> None:
    docs_only = _story_inputs(changed=(ChangedFile("M", "docs/web-ui.md"),))
    code, record = _precheck(harness, *_mr(harness, docs_only))
    assert (code, record["status"]) == (0, "proceed")
    backend = _story_inputs(
        changed=(ChangedFile("M", "lib/idp_common_pkg/idp_common/x.py"),)
    )
    code, record = _precheck(harness, *_mr(harness, backend))
    assert record["detail"].startswith(
        "only the backend or the API contract changes besides the CHANGELOG; the "
        "nightly run after the merge runs route-folder-to-profile, "
        "tell-why-a-document-used-its-configuration"
    )
    tests_only = _story_inputs(changed=(ChangedFile("M", "CHANGELOG.md"),))
    code, record = _precheck(harness, *_mr(harness, tests_only))
    assert record["detail"] == "nothing user-visible changed"


@pytest.mark.unit
def test_a_docs_only_mr_without_the_web_url_line_is_skipped_naming_it(
    harness: StoryHarness,
) -> None:
    harness.environ["UX_TEST_UI_ENV"] = _b64(_ui_env_text(web=False))
    docs_only = _story_inputs(changed=(ChangedFile("M", "docs/web-ui.md"),))
    code, record = _precheck(harness, *_mr(harness, docs_only))
    assert "UX_TEST_WEB_URL=<the stack's ApplicationWebURL>" in record["detail"]


@pytest.mark.unit
def test_a_deployed_ui_run_without_any_ui_env_is_skipped_naming_the_variable(
    harness: StoryHarness,
) -> None:
    """The web URL is a line of ``UX_TEST_UI_ENV``, so with the variable gone the
    skip names the variable rather than asking for a line in it."""
    del harness.environ["UX_TEST_UI_ENV"]
    docs_only = _story_inputs(changed=(ChangedFile("M", "docs/web-ui.md"),))
    code, record = _precheck(harness, *_mr(harness, docs_only))
    assert (code, record["status"]) == (0, "skipped")
    assert record["detail"].startswith("UX_TEST_UI_ENV is not set")
    code, record = _precheck(harness, *_night(harness, UPDATED_ROW, "--shard", "1/5"))
    assert (code, record["status"]) == (0, "skipped")
    assert record["detail"].startswith("UX_TEST_UI_ENV is not set")


@pytest.mark.unit
def test_an_mr_ui_change_without_its_build_is_skipped(harness: StoryHarness) -> None:
    args = _mr(harness)
    (harness.repo / "src" / "ui" / "build" / "index.html").unlink()
    code, record = _precheck(harness, *args)
    assert record["detail"] == "no UI build: ux_test_build was skipped or failed"


@pytest.mark.unit
def test_a_proceeding_precheck_writes_inputs_precheck_and_proceed(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    code, record = _precheck(harness, *_mr(harness))
    assert code == 0
    assert _lines(capsys)[-1] == "PROCEED: 3 flow(s) of 2 story(ies) at 01234567"
    assert (harness.artifacts / "proceed").is_file()
    written = json.loads((harness.artifacts / "inputs.json").read_text("utf-8"))
    assert runner.StoryInputs.from_json(written).mr.head_sha == HEAD
    assert record["flows"] == [
        "route-folder-to-profile@platform-admin",
        "tell-why-a-document-used-its-configuration@new-analyst",
        "route-folder-to-profile@new-analyst",
    ]
    assert record["selection"]["stories"] == [
        "route-folder-to-profile",
        "tell-why-a-document-used-its-configuration",
    ]
    assert re.fullmatch(r"[0-9a-f]{12}", record["sel"])
    assert "launch_proxy" not in harness.calls and "index_docs" not in harness.calls


@pytest.mark.unit
def test_inputs_json_is_held_to_the_records_own_bound_when_written_and_read(
    harness: StoryHarness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The precheck writes ``inputs.json`` and the run reads it back, so both sides
    keep to ``story_inputs.INPUTS_MAX_BYTES``: a record the run would refuse is
    never written, and the precheck fails before the install rather than the run
    after it."""
    precheck = harness.argv(*_mr(harness), "--precheck", story="")
    given = (harness.tmp / "inputs.json").stat().st_size
    _pinned(harness)
    monkeypatch.setattr(runner.story_inputs, "INPUTS_MAX_BYTES", given - 1)
    assert runner.main(precheck, harness.runtime()) == 1
    assert _lines(capsys)[-1] == f"FAILED: inputs.json is larger than {given - 1} bytes"
    monkeypatch.setattr(runner.story_inputs, "INPUTS_MAX_BYTES", given)
    assert runner.main(precheck, harness.runtime()) == 1
    last = _lines(capsys)[-1]
    assert re.fullmatch(
        rf"FAILED: inputs\.json would be [0-9]+ bytes, more than the {given} a run "
        "reads back",
        last,
    ), last
    assert not (harness.artifacts / "inputs.json").exists()
    assert not (harness.artifacts / "proceed").exists()
    assert "launch_proxy" not in harness.calls


@pytest.mark.unit
@pytest.mark.parametrize("variable", ["UX_HARNESS", "UX_ENTRY"])
def test_the_precheck_fails_when_the_jobs_pin_is_not_this_entrys(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str], variable: str
) -> None:
    args = _mr(harness)
    _pinned(harness)
    harness.environ[variable] = (
        "scripts/sdlc/ai_ux_test.py" if variable == "UX_ENTRY" else "scripts"
    )
    code = runner.main(harness.argv(*args, "--precheck", story=""), harness.runtime())
    assert code == 1
    assert _lines(capsys)[0].startswith(f"FAILED: {variable} ")
    assert not harness.artifacts.exists()


@pytest.mark.unit
def test_the_precheck_fails_when_the_job_sets_neither_pin_variable(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every story job has set both since the jobs began, so a job without them is
    not one this entry was written for."""
    args = _mr(harness)
    harness.environ.pop("UX_HARNESS", None)
    harness.environ.pop("UX_ENTRY", None)
    code = runner.main(harness.argv(*args, "--precheck", story=""), harness.runtime())
    assert code == 1
    assert _lines(capsys)[0].startswith("FAILED: UX_HARNESS is not set")
    assert not harness.artifacts.exists()


@pytest.mark.unit
def test_a_head_already_replied_to_is_unchanged(harness: StoryHarness) -> None:
    args = _mr(harness)
    _code, first = _precheck(harness, *args)
    marker = story_section.marker_line(head_sha=HEAD, sel=first["sel"], kind="run")
    harness.forge.add_note(marker + "\nan earlier reply\n")
    code, record = _precheck(harness, *args)
    assert (code, record["status"], record["detail"]) == (
        0,
        "unchanged",
        "already run for this head",
    )


@pytest.mark.unit
def test_an_unchanged_selection_gets_one_unchanged_reply_with_post(
    harness: StoryHarness,
) -> None:
    older = "a1b2c3d4" * 5
    args = _mr(harness)
    _code, first = _precheck(harness, *args)
    marker = story_section.marker_line(head_sha=older, sel=first["sel"], kind="run")
    harness.forge.add_note(marker + "\nthe older head's reply\n")
    code, record = _precheck(harness, *args, "--post")
    assert (code, record["status"]) == (0, "unchanged")
    posted = [
        n["body"] for n in harness.forge.store.values() if f"sha={HEAD}" in n["body"]
    ]
    assert len(posted) == 1
    assert "unchanged since `a1b2c3d4`" in posted[0]
    _precheck(harness, *args, "--post")
    again = [
        n["body"] for n in harness.forge.store.values() if f"sha={HEAD}" in n["body"]
    ]
    assert len(again) == 1


@pytest.mark.unit
def test_an_mr_past_its_run_allowance_gets_one_limit_reply_then_none(
    harness: StoryHarness,
) -> None:
    args = _mr(harness)
    _code, first = _precheck(harness, *args)
    for number in range(stories.load_catalogue().runs.mr_runs):
        head = f"{number:x}" * 40
        marker = story_section.marker_line(head_sha=head[:40], sel="0" * 12, kind="run")
        harness.forge.add_note(marker + "\n")
    code, record = _precheck(harness, *args, "--post")
    assert record["detail"] == "this MR has used its 4 story runs"
    limits = [
        n["body"] for n in harness.forge.store.values() if "kind=limit" in n["body"]
    ]
    assert len(limits) == 1
    assert f"make ai-ux-test-stories-local MR={IID}" in limits[0]
    harness.forge.head = HEAD
    _precheck(harness, *args, "--post")
    limits = [
        n["body"] for n in harness.forge.store.values() if "kind=limit" in n["body"]
    ]
    assert len(limits) == 1


@pytest.mark.unit
def test_runs_that_went_ahead_and_posted_nothing_count_toward_the_allowance(
    harness: StoryHarness,
) -> None:
    """A run a newer push cancelled, or one stopped at the job's timeout, was paid
    for, so its job's log, which says its precheck went ahead, counts it."""
    args = _mr(harness)
    proceeded = (
        b"\x1b[0Ksection_start\nPROCEED: 3 flow(s) of 2 story(ies) at 1a2b3c4d\n"
    )
    forge = harness.forge
    forge.pipelines = [905, 904, 903, 902, 901, 900]
    harness.environ["CI_PIPELINE_ID"] = "905"
    forge.jobs = {
        905: [{"id": 50, "name": runner.MR_JOB, "status": "running"}],
        904: [{"id": 40, "name": runner.MR_JOB, "status": "canceled"}],
        903: [{"id": 30, "name": runner.MR_JOB, "status": "failed"}],
        902: [
            {"id": 20, "name": runner.MR_JOB, "status": "success"},
            {"id": 21, "name": "ai_ux_test", "status": "success"},
        ],
        901: [{"id": 10, "name": runner.MR_JOB, "status": "success"}],
        900: [{"id": 5, "name": runner.MR_JOB, "status": "skipped"}],
    }
    forge.traces = {
        50: proceeded,
        40: proceeded,
        30: proceeded,
        20: b"SKIPPED: no story covers this MR's CHANGELOG entries\n",
        21: proceeded,
        10: proceeded,
        5: proceeded,
    }
    code, record = _precheck(harness, *args)
    assert record["status"] == "proceed"
    forge.traces[20] = proceeded
    code, record = _precheck(harness, *args, "--post")
    assert record["detail"] == "this MR has used its 4 story runs"
    read = [path for method, path, _ in forge.requests if path.endswith("/trace")]
    assert not [path for path in read if "/jobs/50/" in path or "/jobs/5/" in path]


@pytest.mark.unit
def test_an_mr_whose_runs_cannot_be_counted_is_skipped(harness: StoryHarness) -> None:
    args = _mr(harness)

    def refuse(iid: int, *, limit: int = 100) -> list[int]:
        raise review.Skip("GitLab refused GET /merge_requests/812/pipelines (403)")

    harness.forge.merge_request_pipelines = refuse  # type: ignore[method-assign]
    code, record = _precheck(harness, *args)
    assert (code, record["status"]) == (0, "skipped")
    assert record["detail"].startswith(
        "this MR's earlier story runs cannot be counted: GitLab refused"
    )


@pytest.mark.unit
def test_collect_only_writes_story_inputs_v1_from_the_collector(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    target = harness.tmp / "collected.json"
    argv = [
        "--mr",
        str(IID),
        "--collect-only",
        "--inputs",
        str(target),
        "--repo-dir",
        str(harness.repo),
    ]
    assert runner.main(argv, harness.runtime()) == 0
    assert harness.calls == ["collect_story"]
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["schema"] == runner.INPUTS_SCHEMA and data["version"] == 1
    assert runner.StoryInputs.from_json(data) == runner.written_inputs(
        _story_inputs(), runner.artifacts.Scrub(runner.artifacts.redactor_for({}))
    )
    assert _lines(capsys) == [f"collected: {target}"]


@pytest.mark.unit
def test_inputs_for_another_mr_are_refused(harness: StoryHarness) -> None:
    inputs = _story_inputs()
    other = dataclasses.replace(inputs, mr=dataclasses.replace(inputs.mr, iid=IID + 1))
    code, record = _precheck_failure(harness, *_mr(harness, other))
    assert code == 1


def _precheck_failure(harness: StoryHarness, *extra: str) -> tuple[int, None]:
    _pinned(harness)
    return runner.main(
        harness.argv(*extra, "--precheck", story=""), harness.runtime()
    ), None


@pytest.mark.unit
@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema="ux-test-stories/other"),
        lambda d: d.update(extra=1),
        lambda d: d["trees"].update(docs="not-a-sha"),
        lambda d: d["changelog"].pop("base"),
        lambda d: d.update(docs_sections_edited=[""]),
    ],
    ids=["schema", "extra-key", "tree", "changelog", "sections"],
)
def test_story_inputs_refuse_any_other_shape(
    mutate: Callable[[dict[str, Any]], Any],
) -> None:
    data = _story_inputs().to_json()
    mutate(data)
    with pytest.raises(mr_inputs.InputsError):
        runner.StoryInputs.from_json(data)


# --- the nightly, shards, merge and digest ----------------------------------------

UPDATED_ROW = (
    "🌙 UX review stack (idp-ux-nightly, us-east-1)\n"
    "  ✅ Updated in place to tonight's template — UPDATE_COMPLETE in 12.5 min\n"
)
RUNNING_ROW = (
    "🌙 UX review stack (idp-ux-nightly, us-east-1)\n"
    "  ⚠️ Still updating when the run ended — 30.0 min so far; CloudFormation "
    "carries on\n"
)


def _night(harness: StoryHarness, summary: str, *extra: str) -> list[str]:
    path = harness.tmp / "deployment_summary.txt"
    path.write_text(summary, encoding="utf-8")
    harness.environ.update(CI="true", CI_COMMIT_SHA=HEAD, CI_PIPELINE_ID="12345")
    return ["--all", "--deploy-summary", str(path), *extra]


@pytest.mark.unit
def test_a_shard_runs_its_round_robin_share_of_the_nightly_set(
    harness: StoryHarness,
) -> None:
    argv = harness.argv(*_night(harness, UPDATED_ROW, "--shard", "2/5"), story="")
    assert runner.main(argv, harness.runtime()) == 0
    record = harness.record()
    keys = stories.flow_keys(stories.load_catalogue())
    shard = [str(key) for key in stories.shard(keys, 2, 5)]
    assert sorted({flow["key"] for flow in record["flows"]}) == shard
    assert record["mode"] == "nightly"
    assert record["shard"] == {"index": 2, "total": 5}
    assert record["stack_state"] == "updated"
    assert record["docs"]["ref"] == HEAD and record["docs"]["source"] == "git"


@pytest.mark.unit
def test_a_review_stack_still_updating_skips_the_shard_at_the_precheck(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    code, record = _precheck(harness, *_night(harness, RUNNING_ROW, "--shard", "1/5"))
    assert (code, record["status"]) == (0, "skipped")
    assert "still updating" in record["detail"]
    assert not (harness.artifacts / "proceed").exists()


@pytest.mark.unit
def test_a_missing_deployment_summary_runs_and_records_the_night_as_missing(
    harness: StoryHarness,
) -> None:
    harness.environ.update(CI="true", CI_COMMIT_SHA=HEAD)
    argv = harness.argv(
        "--all",
        "--deploy-summary",
        str(harness.tmp / "absent.txt"),
        "--shard",
        "5/5",
        story="",
    )
    assert runner.main(argv, harness.runtime()) == 0
    assert harness.record()["stack_state"] == "missing"


@pytest.mark.unit
def test_at_night_a_flow_that_gave_up_runs_once_more_at_twice_its_budget(
    harness: StoryHarness,
) -> None:
    """The nightly's own re-run, at twice the step budget and the extended cap,
    which a local run of the same flow does not get."""
    key = stories.FlowKey("find-where-to-start", "new-analyst")
    keys = stories.flow_keys(stories.load_catalogue())
    spec = f"{keys.index(key) + 1}/{len(keys)}"
    harness.script = {str(key): ["silent", "done"]}
    harness.stop = "max_turns"
    argv = harness.argv(
        *_night(harness, UPDATED_ROW, "--run-usd", "100", "--shard", spec), story=""
    )
    assert runner.main(argv, harness.runtime()) == 0
    flows = harness.record()["flows"]
    assert [(f["key"], f["attempt"], f["budget_scale"]) for f in flows] == [
        (str(key), 1, 1),
        (str(key), 2, 2),
    ]
    second = harness.argvs[1]
    assert second[second.index("--max-budget-usd") + 1] == "6.00"
    assert flows[1]["budget"] == 2 * flows[0]["budget"]
    verdicts = [flow["verdict"] for flow in harness.results()["flows"]]
    assert verdicts == ["gave_up", "done"]
    harness.environ.pop("CI")
    harness.artifacts = harness.tmp / "local" / "ux-test-stories"
    harness.argvs.clear()
    argv = harness.argv("--as", "new-analyst", "--run-usd", "100", story=key.story)
    assert runner.main(argv, harness.runtime()) == 0
    assert len(harness.argvs) == 1


@pytest.mark.unit
def test_at_night_a_flow_its_wall_clock_stopped_is_not_run_again_for_it(
    harness: StoryHarness,
) -> None:
    key = stories.FlowKey("find-where-to-start", "new-analyst")
    keys = stories.flow_keys(stories.load_catalogue())
    spec = f"{keys.index(key) + 1}/{len(keys)}"
    harness.script = {str(key): ["silent"]}
    harness.timed_out = True
    argv = harness.argv(
        *_night(harness, UPDATED_ROW, "--run-usd", "100", "--shard", spec), story=""
    )
    assert runner.main(argv, harness.runtime()) == 0
    (only,) = harness.results()["flows"]
    assert (only["verdict"], only["sub_kind"]) == ("gave_up", "wall_clock")
    assert len(harness.argvs) == 1


@pytest.mark.unit
@pytest.mark.parametrize("left", [True, False, None])
@pytest.mark.parametrize("right", [True, False, None])
def test_a_merged_review_stack_flag_follows_the_runs_own_rule_over_every_shard(
    left: bool | None, right: bool | None
) -> None:
    def part(flag: bool | None, key: str) -> dict[str, Any]:
        return {
            "schema": story_verdict.RUN_SCHEMA,
            "version": story_verdict.RUN_VERSION,
            "judgement_revision": story_verdict.JUDGEMENT_REVISION,
            "stack": {
                "name": REVIEW,
                "version": None,
                "build": None,
                "review_stack": flag,
            },
            "flows": [],
            "spend_usd": 0.0,
            "video_withheld_flows": (
                [{"key": key, "attempt": 1, "reason": PAGE_REASON}]
                if flag is False
                else []
            ),
        }

    one = runner.merge_records([part(left, "b-story@x"), part(right, "a-story@y")])
    other = runner.merge_records([part(right, "a-story@y"), part(left, "b-story@x")])
    known = [flag for flag in (left, right) if flag is not None]
    expected = all(known) if known else None
    assert one["stack"]["review_stack"] is expected
    assert other["stack"]["review_stack"] is expected
    assert one["video_withheld_flows"] == other["video_withheld_flows"]
    assert one["video_withheld"] == other["video_withheld"]


@pytest.mark.unit
@pytest.mark.parametrize("left", ["full", "dev-unconfined", None])
@pytest.mark.parametrize("right", ["full", "dev-unconfined", None])
def test_a_merged_confinement_is_full_only_when_every_shards_is(
    left: str | None, right: str | None
) -> None:
    def part(confinement: str | None) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema": story_verdict.RUN_SCHEMA,
            "version": story_verdict.RUN_VERSION,
            "judgement_revision": story_verdict.JUDGEMENT_REVISION,
            "stack": {"name": REVIEW, "review_stack": True},
            "flows": [],
            "spend_usd": 0.0,
        }
        if confinement is not None:
            record["confinement"] = confinement
        return record

    one = runner.merge_records([part(left), part(right)])
    other = runner.merge_records([part(right), part(left)])
    if left == right == "full":
        expected = "full"
    elif "dev-unconfined" in (left, right):
        expected = "dev-unconfined"
    else:
        expected = None
    assert one["confinement"] == other["confinement"] == expected


def _shard_runs(harness: StoryHarness) -> list[Path]:
    """Two real shard runs of route-folder-to-profile, as the Docker runner makes them."""
    dirs = []
    for index in (1, 2):
        harness.artifacts = harness.tmp / "shards" / f"shard-{index}"
        argv = harness.argv("--shard", f"{index}/2", "--reruns", "0")
        assert runner.main(argv, harness.runtime()) == 0
        dirs.append(harness.artifacts)
    return dirs


@pytest.mark.unit
def test_the_digest_judges_the_shards_journals_and_leaves_their_media_behind(
    harness: StoryHarness,
) -> None:
    dirs = _shard_runs(harness)
    for shard in dirs:
        (shard / "flow-1.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    harness.environ.update({"CI_PIPELINE_ID": "77", "CI_COMMIT_SHA": HEAD})
    out = harness.tmp / "digest"
    argv = ["--digest", str(harness.tmp / "shards"), "--out", str(out)]
    assert runner.main(argv, harness.runtime()) == 0
    assert list(out.glob("*.mp4")) == []
    assert not [path for path in out.rglob("screens") if path.is_dir()]
    assert list(out.rglob("journal.jsonl"))
    digest = json.loads((out / "digest.json").read_text(encoding="utf-8"))
    assert (digest["shards_ran"], digest["injections"], digest["reruns_not_run"]) == (
        2,
        0,
        0,
    )
    merged = harness.tmp / "merged"
    argv = ["--merge", *map(str, dirs), "--out", str(merged)]
    assert runner.main(argv, harness.runtime()) == 0
    assert (merged / "flow-1.mp4").is_file()


@pytest.mark.unit
def test_shards_judged_under_different_revisions_still_merge_from_their_journals(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    dirs = _shard_runs(harness)
    results = dirs[1] / "results.json"
    document = json.loads(results.read_text(encoding="utf-8"))
    document["judgement_revision"] = story_verdict.JUDGEMENT_REVISION + 1
    results.write_text(json.dumps(document), encoding="utf-8")
    out = harness.tmp / "merged" / "ux-test-stories"
    capsys.readouterr()
    argv = ["--merge", *map(str, dirs), "--out", str(out)]
    assert runner.main(argv, harness.runtime()) == 0
    lines = _lines(capsys)
    assert any(
        line.startswith("WARNING: the shards' own results cannot be compared")
        for line in lines
    )
    merged = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert merged["judgement_revision"] == story_verdict.JUDGEMENT_REVISION


@pytest.mark.unit
def test_merge_joins_shards_into_a_run_directory_that_judges_the_same(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.facts["Author"] = dataclasses.replace(
        harness.default_facts, stack_name=None
    )
    dirs = _shard_runs(harness)
    out = harness.tmp / "merged" / "ux-test-stories"
    capsys.readouterr()
    argv = ["--merge", *map(str, dirs), "--out", str(out)]
    assert runner.main(argv, harness.runtime()) == 0
    lines = _lines(capsys)
    assert lines[-1] == f"MERGED: 2 run(s) into {out}; {_cost(out)}"
    assert not [line for line in lines if line.startswith("WARNING:")]
    merged = json.loads((out / "story-run.json").read_text("utf-8"))
    assert [flow["key"] for flow in merged["flows"]] == [
        "route-folder-to-profile@new-analyst",
        "route-folder-to-profile@platform-admin",
    ]
    assert merged["stack"]["review_stack"] is False
    assert merged["video_withheld"] == PAGE_REASON
    assert merged["shard"] is None
    pairs = [
        (stories.FlowKey.parse(flow["key"]), flow["series"]) for flow in merged["flows"]
    ]
    assert merged["reply"]["sel"] == runner.changelog_select.selection_hash(
        pairs, ui_source="deployed", docs_tree="worktree"
    )
    shard_sels = {
        json.loads((path / "story-run.json").read_text("utf-8"))["reply"]["sel"]
        for path in dirs
    }
    assert merged["reply"]["sel"] not in shard_sels
    assert runner.main(["--judge", str(out), "--check"], harness.runtime()) == 0
    for flow in merged["flows"]:
        assert (out / flow["journal"]).is_file()


@pytest.mark.unit
def test_a_merged_run_says_how_every_shards_flows_were_chosen(
    harness: StoryHarness,
) -> None:
    """The merged reply names every hand-picked shard's flows, not the first
    shard's alone, and shards of a set are that whole set, with no shard named."""
    dirs = _shard_runs(harness)
    parts = [
        json.loads((path / "story-run.json").read_text(encoding="utf-8"))
        for path in dirs
    ]
    assert [part["reply"]["chosen_by"]["flows"] for part in parts] == [
        ["route-folder-to-profile@new-analyst"],
        ["route-folder-to-profile@platform-admin"],
    ]
    out = harness.tmp / "merged" / "ux-test-stories"
    argv = ["--merge", *map(str, dirs), "--out", str(out)]
    assert runner.main(argv, harness.runtime()) == 0
    merged = json.loads((out / "story-run.json").read_text("utf-8"))
    assert merged["reply"]["chosen_by"] == {
        "kind": "story",
        "flows": [
            "route-folder-to-profile@new-analyst",
            "route-folder-to-profile@platform-admin",
        ],
    }
    lines = (out / "section.md").read_text(encoding="utf-8").splitlines()
    assert lines[3] == (
        "Chosen by hand: route-folder-to-profile as new analyst, "
        "route-folder-to-profile as platform admin"
    )
    for index, part in enumerate(parts, start=1):
        part["reply"]["chosen_by"] = {
            "kind": "all",
            "tier": "p0",
            "shard": f"{index}/2",
        }
    assert runner.merge_records(parts)["reply"]["chosen_by"] == {
        "kind": "all",
        "tier": "p0",
        "shard": None,
    }
    parts[1]["reply"]["chosen_by"] = {"kind": "replay"}
    assert runner.merge_records(parts)["reply"]["chosen_by"] == {
        "kind": "all",
        "tier": "p0",
        "shard": "1/2",
    }
    del parts[1]["reply"]["chosen_by"]
    assert runner.merge_records(parts)["reply"]["chosen_by"]["shard"] == "1/2"


@pytest.mark.unit
def test_merge_finds_the_shards_under_one_parent_in_shard_order(tmp_path: Path) -> None:
    parent = tmp_path / "shards"
    for index in (10, 2, 1):
        folder = parent / f"shard-{index}"
        folder.mkdir(parents=True)
        (folder / "story-run.json").write_text("{}", encoding="utf-8")
    (parent / "not-a-run").mkdir()
    found = runner.shard_dirs([parent])
    assert [path.name for path in found] == ["shard-1", "shard-2", "shard-10"]


@pytest.mark.unit
def test_the_digest_merges_the_shards_and_says_when_nothing_was_compared(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    _shard_runs(harness)
    harness.environ.update(CI_COMMIT_SHA=HEAD, CI_PIPELINE_ID="12345")
    out = harness.tmp / "digest"
    capsys.readouterr()
    argv = [
        "--digest",
        str(harness.tmp / "shards"),
        "--out",
        str(out),
        "--repo-dir",
        str(REPO_ROOT),
    ]
    assert runner.main(argv, harness.runtime()) == 0
    assert _lines(capsys)[-1] == (
        f"DIGEST: 2 shard(s), 0 regression(s), in {out}; {_cost(out)}"
    )
    digest = json.loads((out / "digest.json").read_text("utf-8"))
    assert digest["schema"] == story_digest.DIGEST_SCHEMA
    assert digest["pipeline_id"] == 12345 and digest["develop_sha"] == HEAD
    assert digest["stack_state"] == "missing" and digest["comparable"] is False
    text = (out / "digest.md").read_text(encoding="utf-8")
    assert text.startswith("# First-time-user stories — nightly digest")
    assert text.rstrip().endswith("_History unavailable: nothing compared._")
    history = json.loads((out / "history.json").read_text("utf-8"))
    assert history == {"available": False, "gaps": [], "nights": []}
    assert (out / "results.json").is_file() and (out / "flows").is_dir()


@pytest.mark.unit
def test_the_digest_judges_regressions_with_the_reruns_that_did_not_fit(
    harness: StoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The keys the shards' records name in ``reruns_not_run`` reach the
    regressions, which report such a flow as unconfirmed rather than leaving it
    out."""
    dirs = _shard_runs(harness)
    record_path = dirs[1] / "story-run.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["reruns_not_run"] = [
        {"key": "route-folder-to-profile@platform-admin", "reason": "time"}
    ]
    record_path.write_text(json.dumps(record), encoding="utf-8")
    seen: list[Any] = []
    regressions = story_digest.regressions

    def recording(*args: Any, **kwargs: Any) -> list[story_digest.Regression]:
        seen.append(kwargs.get("rerun_did_not_fit"))
        return regressions(*args, **kwargs)

    monkeypatch.setattr(story_digest, "regressions", recording)
    out = harness.tmp / "digest"
    argv = ["--digest", str(harness.tmp / "shards"), "--out", str(out)]
    assert runner.main(argv, harness.runtime()) == 0
    assert seen == [{"route-folder-to-profile@platform-admin"}]


@pytest.mark.unit
def test_the_digest_compares_with_the_history_the_runtime_supplies(
    harness: StoryHarness,
) -> None:
    _shard_runs(harness)
    out = harness.tmp / "digest"
    runtime = harness.runtime()
    asked: list[Any] = []

    def history(args: Any, environ: Any) -> story_digest.History:
        asked.append(args.digest)
        return story_digest.History.from_digests([(11, None), (10, None)])

    runtime.history = history
    argv = ["--digest", str(harness.tmp / "shards"), "--out", str(out)]
    assert runner.main(argv, runtime) == 0
    assert asked == [str(harness.tmp / "shards")]
    assert json.loads((out / "digest.json").read_text("utf-8"))["gaps"] == [11, 10]
    assert "History unavailable" not in (out / "digest.md").read_text("utf-8")


# --- report.html, wherever a run directory is finished ------------------------------


def _report(run_dir: Path) -> str:
    return (run_dir / story_report.REPORT_FILE).read_text(encoding="utf-8")


def _references(text: str) -> list[str]:
    return re.findall(r'(?:src|href)="([^"]*)"', text)


@pytest.mark.unit
def test_a_run_writes_its_report_beside_its_results(harness: StoryHarness) -> None:
    assert harness.main(*ONE_FLOW) == 0
    text = _report(harness.artifacts)
    assert text == story_report.render(
        harness.artifacts,
        catalogue=story_report.load_catalogue(runner.catalogue_path()),
    )
    policy = html.escape(story_report.CONTENT_SECURITY_POLICY, quote=True)
    assert f'content="{policy}"' in text and "<script" not in text
    assert 'id="flow-route-folder-to-profile--platform-admin"' in text
    assert story_verdict.cost_text(harness.results()["summary"]) in text
    files = [ref for ref in _references(text) if not ref.startswith("#")]
    assert "flows/route-folder-to-profile@platform-admin/screens/001.jpg" in files
    for ref in files:
        assert (harness.artifacts / ref).is_file(), ref


@pytest.mark.unit
def test_judge_rewrites_the_report_and_check_writes_none(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness.main(*ONE_FLOW) == 0
    report = harness.artifacts / story_report.REPORT_FILE
    written = report.read_bytes()
    report.unlink()
    capsys.readouterr()
    argv = ["--judge", str(harness.artifacts), "--check"]
    assert runner.main(argv, harness.runtime()) == 0
    assert not report.exists() and _lines(capsys) == []
    assert runner.main(argv[:2], harness.runtime()) == 0
    assert report.read_bytes() == written and _lines(capsys) == []


@pytest.mark.unit
def test_merge_and_digest_write_the_merged_runs_report(harness: StoryHarness) -> None:
    dirs = _shard_runs(harness)
    for shard in dirs:
        assert (shard / story_report.REPORT_FILE).is_file()
    merged = harness.tmp / "merged" / "ux-test-stories"
    argv = ["--merge", *map(str, dirs), "--out", str(merged)]
    assert runner.main(argv, harness.runtime()) == 0
    text = _report(merged)
    for persona in ("new-analyst", "platform-admin"):
        assert f'id="flow-route-folder-to-profile--{persona}"' in text
        assert f"flows/route-folder-to-profile@{persona}/screens/001.jpg" in text
    digest = harness.tmp / "digest"
    argv = ["--digest", str(harness.tmp / "shards"), "--out", str(digest)]
    assert runner.main(argv, harness.runtime()) == 0
    text = _report(digest)
    assert 'id="flow-route-folder-to-profile--new-analyst"' in text
    assert 'src="' not in text
    assert "each shard's own artifacts, beside that shard's report.html" in text
    for ref in _references(text):
        assert ref.startswith("#") or (digest / ref).is_file(), ref


@pytest.mark.unit
def test_a_report_that_cannot_be_written_never_fails_the_run(
    harness: StoryHarness,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(run_dir: Path, **kwargs: Any) -> str:
        raise RuntimeError("page text that must not be printed")

    monkeypatch.setattr(story_report, "render", broken)
    assert harness.main(*ONE_FLOW) == 0
    lines = _lines(capsys)
    assert "WARNING: report.html was not written: RuntimeError" in lines
    assert not [line for line in lines if "page text" in line]
    assert lines[-1].startswith("DRY RUN: nothing posted")
    assert harness.results()["flows"][0]["verdict"] == "done"
    assert harness.section().startswith("<!-- ux-story: sha=")
    assert not (harness.artifacts / story_report.REPORT_FILE).exists()
    monkeypatch.undo()
    target = harness.tmp / "elsewhere.html"
    os.symlink(target, harness.artifacts / story_report.REPORT_FILE)
    assert runner.main(["--judge", str(harness.artifacts)], harness.runtime()) == 0
    assert _lines(capsys) == [
        "WARNING: report.html was not written: report.html cannot be written: it "
        "is a symbolic link, which is never followed"
    ]
    assert not target.exists()


# --- the history the digest reads, and its Slack summary ---------------------------

API_URL = "https://gitlab.test/api/v4"
PIPELINE = 12345
WEBHOOK = "https://hooks.slack.test/triggers/T0/1/hooksecretvalue"
JOB_URL = "https://gitlab.test/group/proj/-/jobs/987654"


def _night_digest(pipeline_id: int) -> bytes:
    """A valid digest.json v1 for one earlier night, as that night wrote it."""
    night = story_digest.Night(
        pipeline_id=pipeline_id,
        develop_sha=HEAD,
        stack_state="updated",
        comparable=True,
        audits_clean=True,
        flows=(
            story_digest.NightFlow(
                "route-folder-to-profile@new-analyst",
                "0123456789ab",
                "done",
                "single",
                False,
            ),
        ),
    )
    document = story_digest.digest_document(
        night, story_digest.History((), ()), [], [], spend_usd=1.5
    )
    return json.dumps(document).encode("utf-8")


class HistoryGitlab(gitlab_api.UxGitlab):
    """The real client over earlier scheduled pipelines, each with one digest job.

    ``nights`` maps a pipeline id to the digest.json its digest job left, or None
    for a job whose artifact is gone; only the transport is replaced.
    """

    def __init__(
        self, nights: dict[int, bytes | None], *, refuse: bool = False
    ) -> None:
        super().__init__(API_URL, "group/proj", CREDENTIAL)
        self.nights = nights
        self.refuse = refuse
        self.requests: list[tuple[str, str, str]] = []

    def _send(
        self, method: str, url: str, **kwargs: Any
    ) -> tuple[object, dict[str, str]]:
        parts = urlsplit(url)
        path = parts.path.removeprefix("/api/v4/projects/group%2Fproj")
        self.requests.append((method, path, parts.query))
        if self.refuse:
            raise review.Skip(f"GitLab refused GET /pipelines (403) for {self.token}")
        if method == "GET" and path == "/pipelines":
            listed = sorted(self.nights, reverse=True)
            return [
                {"id": n, "ref": "develop", "source": "schedule"} for n in listed
            ], {}
        found = re.fullmatch(r"/pipelines/(\d+)/jobs", path)
        if method == "GET" and found:
            job = {"id": int(found[1]) * 10, "name": runner.DIGEST_JOB}
            return [{**job, "status": "success"}], {}
        found = re.fullmatch(r"/jobs/(\d+)/artifacts/(.+)", path)
        if method == "GET" and found and kwargs.get("as_bytes"):
            assert found[2] == runner.DIGEST_ARTIFACT
            assert kwargs.get("limit") == story_digest.DIGEST_MAX_BYTES
            raw = self.nights[int(found[1]) // 10]
            if raw is None:
                raise review.Failure(f"GitLab returned 404 for GET {path}")
            return raw, {}
        raise review.Failure(f"the fake GitLab has no route for {method} {path}")


def _history_args(*extra: str) -> Any:
    return runner.parse_args(
        [
            "--digest",
            "shards",
            "--out",
            "out",
            "--api-url",
            API_URL,
            "--project",
            "group/proj",
            *extra,
        ]
    )


def _scheduled(**changes: str | None) -> dict[str, str]:
    values: dict[str, str | None] = {
        "CI_PIPELINE_SOURCE": "schedule",
        "CI_PIPELINE_ID": str(PIPELINE),
        "GITLAB_REVIEW_TOKEN": CREDENTIAL,
    }
    values.update(changes)
    return _environ(**values)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"CI_PIPELINE_SOURCE": None}, "this is not a scheduled pipeline"),
        ({"CI_PIPELINE_SOURCE": "web"}, "this is not a scheduled pipeline"),
        ({"GITLAB_REVIEW_TOKEN": None}, "GITLAB_REVIEW_TOKEN is not set"),
    ],
    ids=["no-source", "manual", "no-token"],
)
def test_the_history_is_read_only_in_a_scheduled_pipeline_with_the_token(
    changes: dict[str, str | None], reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    def forge(args: Any, credential: str) -> gitlab_api.UxGitlab:
        raise AssertionError("no GitLab client is made")

    history = runner.nightly_history(
        _history_args(), _scheduled(**changes), forge=forge
    )
    assert history is None
    assert _lines(capsys) == [f"history: not read, because {reason}"]


@pytest.mark.unit
def test_the_history_needs_the_api_and_the_project(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = runner.parse_args(["--digest", "shards", "--out", "out", "--api-url", ""])
    assert runner.nightly_history(args, _scheduled()) is None
    assert _lines(capsys) == [
        "history: not read, because the GitLab API URL or the project is not known"
    ]


@pytest.mark.unit
def test_the_history_is_the_earlier_scheduled_nights_each_validated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gitlab = HistoryGitlab(
        {
            PIPELINE: _night_digest(PIPELINE),
            41: _night_digest(41),
            40: _night_digest(39),
            39: None,
            38: b"[" * 100,
        }
    )
    made: list[str] = []

    def forge(args: Any, credential: str) -> gitlab_api.UxGitlab:
        made.append(credential)
        return gitlab

    history = runner.nightly_history(_history_args(), _scheduled(), forge=forge)
    assert history is not None
    assert [night.pipeline_id for night in history.nights] == [41]
    assert history.gaps == (40, 39, 38)
    assert made == [CREDENTIAL]
    assert {method for method, _, _ in gitlab.requests} == {"GET"}
    listing = [query for _, path, query in gitlab.requests if path == "/pipelines"]
    assert listing == ["ref=develop&source=schedule&order_by=id&sort=desc&per_page=20"]
    assert f"/pipelines/{PIPELINE}/jobs" not in {path for _, path, _ in gitlab.requests}
    assert _lines(capsys) == ["history: 1 earlier night(s), 3 gap(s)"]


@pytest.mark.unit
def test_a_history_that_cannot_be_listed_is_none_and_never_quotes_the_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    refused = runner.nightly_history(
        _history_args(),
        _scheduled(),
        forge=lambda args, credential: HistoryGitlab({}, refuse=True),
    )
    assert refused is None

    def broken(args: Any, credential: str) -> gitlab_api.UxGitlab:
        raise RuntimeError(f"cannot build a client with {credential}")

    assert runner.nightly_history(_history_args(), _scheduled(), forge=broken) is None
    out = capsys.readouterr()
    lines = out.out.splitlines()
    assert lines[0].startswith("history: not read: GitLab refused GET /pipelines")
    assert lines[1].startswith("history: not read: RuntimeError: cannot build")
    assert CREDENTIAL not in out.out + out.err


@pytest.mark.unit
def test_nights_past_the_reading_time_are_gaps(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gitlab = HistoryGitlab({41: _night_digest(41), 40: _night_digest(40)})
    times = iter([0.0, 1.0, runner.HISTORY_SECONDS + 1])
    history = runner.nightly_history(
        _history_args(),
        _scheduled(),
        forge=lambda args, credential: gitlab,
        clock=lambda: next(times),
    )
    assert history is not None
    assert [night.pipeline_id for night in history.nights] == [41]
    assert history.gaps == (40,)


@pytest.mark.unit
def test_the_scheduled_digest_compares_with_the_nights_it_read_and_posts_once(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    _shard_runs(harness)
    harness.environ.update(
        _scheduled(
            CI_COMMIT_SHA=HEAD,
            CI_JOB_URL=JOB_URL,
            UX_TEST_STORIES_SLACK_WEBHOOK_URL=WEBHOOK,
        )
    )
    gitlab = HistoryGitlab({PIPELINE: None, 41: _night_digest(41), 40: None})
    runtime = harness.runtime()
    posted: list[tuple[str, dict[str, str]]] = []

    def slack(webhook: str, payload: dict[str, str]) -> str:
        posted.append((webhook, payload))
        return "posted (200)"

    def history(args: Any, environ: Any) -> story_digest.History | None:
        return runner.nightly_history(args, environ, forge=lambda a, credential: gitlab)

    runtime.history = history
    runtime.slack = slack
    out = harness.tmp / "digest"
    capsys.readouterr()
    argv = ["--digest", str(harness.tmp / "shards"), "--out", str(out)]
    assert (
        runner.main([*argv, "--api-url", API_URL, "--project", "group/proj"], runtime)
        == 0
    )
    digest = json.loads((out / "digest.json").read_text("utf-8"))
    assert digest["pipeline_id"] == PIPELINE and digest["gaps"] == [40]
    record = json.loads((out / "history.json").read_text("utf-8"))
    assert record["available"] is True
    assert [night["pipeline_id"] for night in record["nights"]] == [41]
    assert "History unavailable" not in (out / "digest.md").read_text("utf-8")
    assert posted == [(WEBHOOK, story_digest.slack_payload(digest, job_url=JOB_URL))]
    lines = _lines(capsys)
    assert lines[-3:] == [
        "history: 1 earlier night(s), 1 gap(s)",
        "slack: posted (200)",
        f"DIGEST: 2 shard(s), 0 regression(s), in {out}; {_cost(out)}",
    ]
    assert "hooksecretvalue" not in "\n".join(lines)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("changes", "said"),
    [
        ({"UX_TEST_STORIES_SLACK_WEBHOOK_URL": None}, []),
        ({"UX_TEST_STORIES_SLACK_WEBHOOK_URL": "   "}, []),
        (
            {"CI_PIPELINE_SOURCE": "web"},
            ["slack: not posted, because this is not a scheduled pipeline"],
        ),
    ],
    ids=["no-webhook", "blank-webhook", "manual-pipeline"],
)
def test_the_digest_posts_to_slack_only_from_a_schedule_with_a_webhook(
    harness: StoryHarness,
    capsys: pytest.CaptureFixture[str],
    changes: dict[str, str | None],
    said: list[str],
) -> None:
    _shard_runs(harness)
    harness.environ.update(
        _scheduled(**{"UX_TEST_STORIES_SLACK_WEBHOOK_URL": WEBHOOK, **changes})
    )
    runtime = harness.runtime()
    runtime.slack = lambda webhook, payload: pytest.fail("Slack was posted to")
    out = harness.tmp / "digest"
    capsys.readouterr()
    argv = ["--digest", str(harness.tmp / "shards"), "--out", str(out)]
    assert runner.main(argv, runtime) == 0
    lines = _lines(capsys)
    assert [line for line in lines if line.startswith("slack:")] == said
    assert lines[-1].startswith("DIGEST: ")
    assert "hooksecretvalue" not in "\n".join(lines)


@pytest.mark.unit
def test_a_slack_post_that_fails_never_fails_the_digest(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    _shard_runs(harness)
    harness.environ.update(_scheduled(UX_TEST_STORIES_SLACK_WEBHOOK_URL=WEBHOOK))
    runtime = harness.runtime()

    def slack(webhook: str, payload: dict[str, str]) -> str:
        raise RuntimeError(f"cannot post to {webhook}")

    runtime.slack = slack
    out = harness.tmp / "digest"
    capsys.readouterr()
    argv = ["--digest", str(harness.tmp / "shards"), "--out", str(out)]
    assert runner.main(argv, runtime) == 0
    lines = _lines(capsys)
    assert lines[-2:] == [
        "slack: not posted (RuntimeError)",
        f"DIGEST: 2 shard(s), 0 regression(s), in {out}; {_cost(out)}",
    ]
    assert (out / "digest.json").is_file()


class _Answer:
    """What a Slack webhook answers: a status, and a body read in bounded pieces."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.reads: list[int] = []

    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.reads.append(size)
        return b"ok"


class _Opener:
    """An opener that records the one request and answers, or raises, as told."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.requests: list[tuple[Any, float]] = []

    def open(self, request: Any, timeout: float = 0.0) -> Any:
        self.requests.append((request, timeout))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@pytest.mark.unit
@pytest.mark.parametrize(
    "webhook",
    [
        "",
        "http://hooks.slack.test/triggers/T0/1/x",
        "https://user:pass@hooks.slack.test/triggers/T0/1/x",
        "https://hooks.slack.test/triggers/T0/1/x#part",
        "https:///triggers/T0/1/x",
        "https://hooks.slack.test/triggers/T0/1/x y",
        "https://hooks.slack.test/triggers/é",
        "https://hooks.slack.test:0/triggers",
        "https://hooks.slack.test:99999/triggers",
        "https://hooks.slack.test/" + "x" * 2048,
        "file:///etc/passwd",
    ],
)
def test_post_slack_posts_only_to_a_usable_https_url(webhook: str) -> None:
    opener = _Opener(_Answer(200))
    outcome = runner.post_slack(webhook, {"status": "clean"}, opener=opener)
    assert outcome == (
        "not posted, because UX_TEST_STORIES_SLACK_WEBHOOK_URL is not an https URL"
    )
    assert opener.requests == []


@pytest.mark.unit
def test_post_slack_sends_the_payload_as_json_and_reads_a_bounded_answer() -> None:
    answer = _Answer(200)
    opener = _Opener(answer)
    payload = {
        "status": "clean",
        "commit": "01234567",
        "job_url": JOB_URL,
        "summary": "é",
    }
    assert runner.post_slack(WEBHOOK, payload, opener=opener, timeout=7.0) == (
        "posted (200)"
    )
    ((request, timeout),) = opener.requests
    assert (request.full_url, request.get_method(), timeout) == (WEBHOOK, "POST", 7.0)
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data.decode("utf-8")) == payload
    assert answer.reads == [4096]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (
            runner.urllib.error.HTTPError(WEBHOOK, 400, "invalid_payload", {}, None),
            "not posted: Slack answered 400",
        ),
        (
            runner.urllib.error.URLError(f"cannot reach {WEBHOOK}"),
            "not posted (URLError)",
        ),
        (TimeoutError(f"{WEBHOOK} timed out"), "not posted (TimeoutError)"),
        (OSError(f"{WEBHOOK} unreachable"), "not posted (OSError)"),
        (_Answer(302), "not posted: Slack answered 302"),
        (_Answer(204), "posted (204)"),
    ],
    ids=["http-error", "url-error", "timeout", "os-error", "redirect", "no-content"],
)
def test_post_slack_never_raises_and_never_quotes_the_webhook(
    answer: Any, outcome: str
) -> None:
    said = runner.post_slack(WEBHOOK, {"status": "clean"}, opener=_Opener(answer))
    assert said == outcome
    assert "hooksecretvalue" not in said


@pytest.mark.unit
def test_the_slack_opener_answers_a_redirect_as_an_error_and_never_follows_it() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[str] = []

    class Redirecting(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST

        def log_message(self, format: str, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirecting)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/hook"
        request = runner.urllib.request.Request(url, data=b"{}", method="POST")
        with pytest.raises(runner.urllib.error.HTTPError) as raised:
            runner._SLACK_OPENER.open(request, timeout=5)
        assert raised.value.code == 302
        raised.value.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert seen == ["/hook"]


# --- replay -----------------------------------------------------------------------


@pytest.mark.unit
def test_a_replay_runs_the_recorded_flows_model_effort_and_docs_again(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness.main(*ONE_FLOW, "--effort", "max", "--docs", HEAD) == 0
    first = harness.artifacts
    harness.artifacts = harness.tmp / "replayed" / "ux-test-stories"
    harness.harness_sha = "c0ffee00" * 5
    capsys.readouterr()
    argv = harness.argv("--replay", str(first), story="")
    argv[argv.index("--model") + 1] = "us.anthropic.claude-opus-5"
    assert runner.main(argv, harness.runtime()) == 0
    lines = _lines(capsys)
    assert any(
        line.startswith("WARNING: this harness is c0ffee00c0ff, the replayed run's was")
        for line in lines
    )
    record = harness.record()
    assert [flow["key"] for flow in record["flows"]] == [
        "route-folder-to-profile@platform-admin"
    ]
    assert (record["model"], record["effort"]) == (MODEL, "max")
    assert record["docs"]["ref"] == HEAD
    assert record["replayed"] == str(first)


@pytest.mark.unit
def test_a_replay_runs_against_the_stack_it_is_given_and_else_the_runs_own(
    harness: StoryHarness,
) -> None:
    """The Docker runner always passes the stack it replays against and warns when
    it is not the run's, so a ``--stack`` given here is the one the replay uses."""
    assert harness.main(*ONE_FLOW) == 0
    first = harness.artifacts
    assert runner.parse_args(["--replay", str(first)]).stack is None
    harness.artifacts = harness.tmp / "again" / "ux-test-stories"
    assert (
        runner.main(harness.argv("--replay", str(first), story=""), harness.runtime())
        == 0
    )
    assert harness.record()["stack"]["name"] == REVIEW
    harness.environ["UX_TEST_UI_ENV"] = _b64(
        _ui_env_text(f"{OTHER_STACK}-Settings").replace(
            "idp-ux-nightly-inputbucket", f"{OTHER_STACK}-inputbucket"
        )
    )
    harness.artifacts = harness.tmp / "other" / "ux-test-stories"
    argv = harness.argv("--replay", str(first), "--stack", OTHER_STACK, story="")
    assert runner.main(argv, harness.runtime()) == 0
    record = harness.record()
    assert record["stack"]["name"] == OTHER_STACK
    assert record["stack"]["review_stack"] is False


@pytest.mark.unit
def test_a_pair_run_is_not_replayed(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness.main(*ONE_FLOW) == 0
    first = harness.artifacts
    path = first / story_verdict.RUN_FILE
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(
        json.dumps({**record, "mode": "pair", "model": "unknown", "effort": "unknown"}),
        encoding="utf-8",
    )
    harness.artifacts = harness.tmp / "replayed" / "ux-test-stories"
    harness.calls.clear()
    capsys.readouterr()
    argv = harness.argv("--replay", str(first), story="")
    assert runner.main(argv, harness.runtime()) == 1
    assert any("a pair run was played by a session" in line for line in _lines(capsys))
    assert "launch_proxy" not in harness.calls


@pytest.mark.unit
def test_a_replay_of_a_build_run_needs_that_build(harness: StoryHarness) -> None:
    build = harness.tmp / "build"
    build.mkdir()
    (build / "index.html").write_text("<!doctype html>", encoding="utf-8")
    assert harness.main(*ONE_FLOW, "--build-dir", str(build)) == 0
    first = harness.artifacts
    harness.artifacts = harness.tmp / "replayed" / "ux-test-stories"
    harness.calls.clear()
    argv = harness.argv("--replay", str(first), story="")
    assert runner.main(argv, harness.runtime()) == 0
    assert "launch_proxy" not in harness.calls
    assert not harness.artifacts.joinpath("story-run.json").exists()


@pytest.mark.unit
@pytest.mark.parametrize("option", [["--run-usd", "0.01"], ["--reruns", "0"]])
def test_a_watched_flow_refuses_the_options_it_does_not_read(
    option: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    base = ["--interactive", "--story", "find-where-to-start", "--as", "new-analyst"]
    with pytest.raises(SystemExit):
        runner.parse_args([*base, *option])
    assert "reads neither --run-usd nor --reruns" in capsys.readouterr().err
    assert runner.parse_args(base).reruns == 1
    assert runner.parse_args(["--all", "--reruns", "0"]).reruns == 0


# --- watch mode -------------------------------------------------------------------


TRANSCRIPT = [
    {"type": "user", "message": {"role": "user", "content": "Begin the task."}},
    {
        "type": "assistant",
        "message": {
            "id": "msg_1",
            "role": "assistant",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    },
    {
        "type": "assistant",
        "message": {
            "id": "msg_1",
            "role": "assistant",
            "usage": {"input_tokens": 10, "output_tokens": 50},
        },
    },
    {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
    },
    {
        "type": "assistant",
        "message": {
            "id": "msg_2",
            "role": "assistant",
            "usage": {
                "input_tokens": 20,
                "output_tokens": 100,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 300,
            },
        },
    },
    {"type": "user", "isMeta": True, "message": {"role": "user", "content": "caveat"}},
]


def _watch(harness: StoryHarness) -> list[str]:
    harness.environ.pop("UX_TEST_PERSONAS")
    harness.environ["TERM"] = "xterm-256color"
    return harness.argv(
        "--as", "new-analyst", "--interactive", story="find-where-to-start"
    )


@pytest.mark.unit
def test_a_watched_flow_runs_the_tui_with_the_story_prompt_as_its_system_prompt(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    """At the pin a message given as the TUI's argument reaches the model before
    the story tools, so the prompt is a private file the TUI appends to its system
    prompt, and the person is told the one message to type once the server is up."""
    harness.transcript = TRANSCRIPT
    assert runner.main(_watch(harness), harness.runtime()) == 0
    for step in ("launch_proxy", "launch_chrome", "sign_in", "start_recorder"):
        assert step not in harness.calls
    assert harness.calls.count("page_facts") == 1
    (argv,) = harness.argvs
    assert "-p" not in argv and "--max-turns" not in argv
    assert "--" not in argv
    assert harness.prompts[0] not in argv
    assert harness.tui_prompts == [(harness.prompts[0], 0o600)]
    printed = _lines(capsys)
    told = [line for line in printed if line.startswith("watch: find-")]
    assert told and told[0].endswith(f"then type: {agent.OPENING_MESSAGE}")
    assert harness.tui_kwargs["env"]["TERM"] == "xterm-256color"
    assert harness.tui_kwargs["timeout"] == 12 * 60
    record = harness.record()
    assert record["mode"] == "watch"
    (flow,) = record["flows"]
    assert (flow["session"], flow["audit_state"]) == (None, "not_run")
    interactive = record["interactive"]
    assert interactive["assisted"] is False and interactive["found"] is True
    assert interactive["usage"] == {
        "input_tokens": 30,
        "output_tokens": 150,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 300,
    }
    assert interactive["cost_usd_pinned"] == runner.pinned_cost(
        MODEL, interactive["usage"]
    )
    assert interactive["counted_usd"] == record["spend_usd"]
    assert interactive["cost_usd_pinned"] == record["spend_usd"]
    results = harness.results()
    assert results["flows"][0]["verdict"] == "done"
    assert results["flows"][0]["claude"] is None
    assert results["summary"]["counted_usd"] == record["spend_usd"] > 0
    assert results["summary"]["pinned_usd"] == interactive["cost_usd_pinned"]
    assert results["summary"]["sessions_without_usage"] == 0
    assert printed[-1].startswith("WATCHED: judged without the socket audit")
    assert printed[-1].endswith(
        story_verdict.cost_text(story_verdict.cost_totals((), interactive))
    )


@pytest.mark.unit
def test_a_person_typing_after_the_opening_message_labels_the_watched_run_assisted(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.transcript = [
        *TRANSCRIPT,
        {"type": "user", "message": {"role": "user", "content": "Try the menu."}},
    ]
    assert runner.main(_watch(harness), harness.runtime()) == 0
    interactive = harness.record()["interactive"]
    assert (interactive["assisted"], interactive["user_messages"]) == (True, 2)
    assert any(line.startswith("watch: ended (assisted)") for line in _lines(capsys))


@pytest.mark.unit
def test_a_watched_run_counts_the_cli_total_when_it_exceeds_the_pinned_price(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.transcript = [*TRANSCRIPT, {"type": "cost-state", "totalCostUSD": 3.25}]
    assert runner.main(_watch(harness), harness.runtime()) == 0
    record = harness.record()
    interactive = record["interactive"]
    assert interactive["cli_cost_usd"] == 3.25
    assert interactive["cost_usd_pinned"] < 3.25
    assert interactive["counted_usd"] == record["spend_usd"] == 3.25
    printed = _lines(capsys)
    assert any(
        line.startswith("watch: ended (unassisted); cost $3.25") for line in printed
    )
    summary = harness.results()["summary"]
    assert (summary["counted_usd"], summary["sessions_without_usage"]) == (3.25, 0)
    assert summary["pinned_usd"] == interactive["cost_usd_pinned"]
    assert printed[-1].endswith(
        f"; $3.25 counted, ${interactive['cost_usd_pinned']:.2f} at the pinned rates"
    )
    assert (
        runner.main(["--judge", str(harness.artifacts), "--check"], harness.runtime())
        == 0
    )
    assert story_verdict.main(["judge", str(harness.artifacts), "--check"]) == 0


@pytest.mark.unit
def test_a_watched_run_without_a_readable_transcript_says_typing_is_unknown(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert runner.main(_watch(harness), harness.runtime()) == 0
    interactive = harness.record()["interactive"]
    assert interactive["found"] is False and interactive["counted_usd"] is None
    assert interactive["assisted"] is None
    assert harness.record()["spend_usd"] == 0.0
    printed = _lines(capsys)
    assert any("whether anyone typed is unknown" in line for line in printed)
    summary = harness.results()["summary"]
    assert (
        summary["counted_usd"],
        summary["pinned_usd"],
        summary["sessions_without_usage"],
    ) == (0.0, 0.0, 1)
    assert printed[-1].endswith(
        "; $0.00 counted, $0.00 at the pinned rates (1 session reported no usage)"
    )


@pytest.mark.unit
def test_the_host_folds_the_browsers_audit_into_a_watched_run_and_judges_it(
    harness: StoryHarness,
) -> None:
    assert runner.main(_watch(harness), harness.runtime()) == 0
    audit = harness.tmp / "browser-audit.json"
    audit.write_text(
        json.dumps({"state": "clean", "violations": [], "sockets": 3, "strays": []}),
        encoding="utf-8",
    )
    argv = ["--judge", str(harness.artifacts), "--audit", str(audit)]
    assert runner.main(argv, harness.runtime()) == 0
    (flow,) = harness.record()["flows"]
    assert flow["audit_state"] == "clean"
    assert harness.results()["flows"][0]["audit"] == "clean"
    audit.write_text(json.dumps({"state": "violations", "strays": ["p"]}), "utf-8")
    assert runner.main(argv, harness.runtime()) == 0
    assert harness.results()["flows"][0]["verdict"] == "error"


@pytest.mark.unit
def test_an_audit_is_folded_only_into_a_watched_or_paired_run(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness.main(*ONE_FLOW) == 0
    audit = harness.tmp / "browser-audit.json"
    audit.write_text(json.dumps({"state": "violations", "strays": ["p"]}), "utf-8")
    capsys.readouterr()
    argv = ["--judge", str(harness.artifacts), "--audit", str(audit)]
    assert runner.main(argv, harness.runtime()) == 1
    assert _lines(capsys)[0].startswith(
        "FAILED: --audit folds a watched or paired run's"
    )
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
    assert code == 0 and "--precheck" in out
    assert not heavy, f"--help imported {heavy}"


@pytest.mark.unit
def test_judge_and_digest_run_on_a_bare_python_on_any_host(
    harness: StoryHarness,
) -> None:
    _shard_runs(harness)
    run_dir = harness.tmp / "shards" / "shard-1"
    code, heavy, out = _heavy_imports("--judge", str(run_dir), "--check", "--summary")
    assert code == 0, out
    assert not heavy, f"--judge imported {heavy}"
    out_dir = harness.tmp / "digest"
    code, heavy, out = _heavy_imports(
        "--digest", str(harness.tmp / "shards"), "--out", str(out_dir)
    )
    assert code == 0, out
    assert not heavy, f"--digest imported {heavy}"


@pytest.mark.unit
def test_the_script_imports_on_a_python_without_sigkill_as_on_windows() -> None:
    """``--judge`` and ``--collect-only`` run on the host, a Windows host included,
    where ``signal.SIGKILL`` does not exist."""
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
        ["--story", "x", "--post"],
        ["--story", "x", "--interactive"],
        ["--as", "new-analyst", "--all"],
        ["--mr", "812", "--shard", "1/2"],
        ["--all", "--shard", "3/2"],
        ["--all", "--shard", "a/b"],
        ["--merge", "a"],
        ["--digest", "a"],
        ["--judge", "a", "--post"],
        ["--story", "x", "--audit", "a.json"],
        ["--story", "x", "--check"],
        ["--all", "--collect-only"],
        ["--story", "x", "--dev-unconfined", "--post"],
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
def test_shards_are_one_based_and_never_past_their_total() -> None:
    assert runner.parse_shard("2/5") == (2, 5)
    assert runner.parse_shard(" 1/1 ") == (1, 1)
    for bad in ("0/5", "6/5", "1/0", "1-5", "1/5/2", ""):
        with pytest.raises(ValueError):
            runner.parse_shard(bad)


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
    assert runtime.collect_story is runner.collect_story
    assert runtime.history is runner.nightly_history
    assert runtime.slack is runner.post_slack
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
@pytest.mark.parametrize("mr", [False, True], ids=["local", "mr"])
def test_the_reply_names_the_backend_as_the_ui_reports_it_never_as_develops(
    harness: StoryHarness, mr: bool
) -> None:
    argv = harness.argv(*_mr(harness), story="") if mr else harness.argv(*ONE_FLOW)
    assert runner.main(argv, harness.runtime()) == 0
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


@pytest.mark.unit
def test_a_collected_mr_is_the_collectors_record_read_back_as_any_inputs_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ux_runner import story_inputs

    assert runner.StoryInputs is story_inputs.StoryInputs
    assert (runner.INPUTS_SCHEMA, runner.INPUTS_VERSION) == (
        story_inputs.INPUTS_SCHEMA,
        story_inputs.INPUTS_VERSION,
    )
    asked: list[tuple[Any, ...]] = []

    def collect(*args: Any, **kwargs: Any) -> story_inputs.StoryInputs:
        asked.append((*args, kwargs))
        return _story_inputs()

    monkeypatch.setattr(story_inputs, "collect", collect)
    record = runner.collect_story(tmp_path, IID, "develop", remote="upstream")
    assert asked == [(tmp_path, IID, "develop", {"remote": "upstream"})]
    assert record == _story_inputs().to_json()
    assert runner.StoryInputs.from_json(record) == _story_inputs()


@pytest.mark.unit
def test_the_tui_runs_on_this_terminal_and_is_stopped_at_its_wall_clock(
    tmp_path: Path,
) -> None:
    import signal

    before = signal.getsignal(signal.SIGINT)
    done = runner.run_tui(
        [sys.executable, "-c", "pass"], cwd=tmp_path, env=dict(os.environ), timeout=30
    )
    assert (done.returncode, done.timed_out) == (0, False)
    slow = runner.run_tui(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=0.5,
    )
    assert slow.timed_out is True and slow.returncode != 0
    assert signal.getsignal(signal.SIGINT) == before


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
def test_a_video_over_the_upload_limit_is_kept_in_the_artifacts_and_said_so(
    harness: StoryHarness,
) -> None:
    harness.forge.add_note(_review_body(HEAD), thread="review-thread")
    harness.script = {"route-folder-to-profile@new-analyst": ["help", "help"]}
    argv = harness.argv(*_mr(harness), "--post", "--max-upload-mb", "1", story="")
    original = harness._render_video

    def big(session_dir: Path, **kwargs: Any) -> tuple[Path, str]:
        mp4, narration = original(session_dir, **kwargs)
        mp4.write_bytes(b"\0" * (2 * 1024 * 1024))
        return mp4, narration

    harness._render_video = big  # type: ignore[method-assign]
    assert runner.main(argv, harness.runtime()) == 0
    assert not [p for _, p, _ in harness.forge.writes() if p.endswith("/uploads")]
    reply = harness.section()
    assert (
        "_The video of `route-folder-to-profile@new-analyst` (2 MB) is over the "
        "upload limit; it is in the job artifacts._"
    ) in reply.splitlines()
    assert list(harness.artifacts.glob("*.mp4"))


@pytest.mark.unit
def test_older_story_replies_have_their_video_lines_superseded(
    harness: StoryHarness,
) -> None:
    older = "a1b2c3d4" * 5
    harness.forge.add_note(_review_body(HEAD), thread="review-thread")
    old_marker = story_section.marker_line(head_sha=older, sel="0" * 12, kind="run")
    old_video = f"![UX story](/uploads/{UPLOAD_HASH}/ux-story-mr{IID}-a1b2c3d4-1.mp4)"
    old_id = harness.forge.add_note(f"{old_marker}\n{old_video}\n")
    assert (
        runner.main(harness.argv(*_mr(harness), "--post", story=""), harness.runtime())
        == 0
    )
    body = harness.forge.store[old_id]["body"]
    assert old_video not in body
    assert "_Video superseded by `01234567`._" in body


@pytest.mark.unit
def test_a_head_that_moves_while_the_run_waits_posts_nothing(
    harness: StoryHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _mr(harness)
    runtime = harness.runtime()
    original = runtime.render_video

    def moved(*a: Any, **k: Any) -> Any:
        harness.forge.head = NEWER
        return original(*a, **k)

    runtime.render_video = moved
    harness.script = {"route-folder-to-profile@new-analyst": ["help", "help"]}
    assert runner.main(harness.argv(*args, "--post", story=""), runtime) == 0
    assert _lines(capsys)[-1].startswith("SUPERSEDED: the MR head moved to 89abcdef")
    assert not harness.forge.writes()


@pytest.mark.unit
def test_collect_only_takes_the_title_from_the_api_when_it_answers(
    harness: StoryHarness,
) -> None:
    harness.environ.update(GITLAB_REVIEW_TOKEN=CREDENTIAL)
    target = harness.tmp / "collected.json"
    argv = [
        "--mr",
        str(IID),
        "--collect-only",
        "--inputs",
        str(target),
        "--project",
        "group/proj",
        "--api-url",
        "https://gitlab.test/api/v4",
    ]
    assert runner.main(argv, harness.runtime()) == 0
    record = json.loads(target.read_text(encoding="utf-8"))["mr"]
    assert record["title"] == "Route by prefix" and record["title_from_git"] is False


@pytest.mark.unit
def test_a_watched_flow_whose_page_cannot_be_read_is_a_harness_error(
    harness: StoryHarness,
) -> None:
    def failing(*args: Any, **kwargs: Any) -> Any:
        raise chrome.ChromeError("DevTools did not answer")

    runtime = harness.runtime()
    runtime.wait_for_cdp = failing
    assert runner.main(_watch(harness), runtime) == 0
    (flow,) = harness.record()["flows"]
    assert flow["harness_error"] == "launch"
    assert "run_tui" not in harness.calls


@pytest.mark.unit
def test_a_watched_flow_on_a_stack_older_than_its_story_is_not_run(
    harness: StoryHarness,
) -> None:
    harness.default_facts = dataclasses.replace(harness.default_facts, version="0.5.0")
    assert runner.main(_watch(harness), harness.runtime()) == 0
    (flow,) = harness.record()["flows"]
    assert flow["not_run_reason"] == "since"


@pytest.mark.unit
def test_a_night_no_shard_ran_is_recorded_as_a_gap_and_a_merge_of_none_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], harness: StoryHarness
) -> None:
    root = tmp_path / "ux-test-stories"
    for index, detail in ((1, "the review stack is still updating"), (2, "")):
        shard = root / f"shard-{index}"
        shard.mkdir(parents=True)
        record = {"version": 1, "proceed": False, "status": "skipped", "detail": detail}
        (shard / "precheck.json").write_text(json.dumps(record), "utf-8")
    harness.environ.update({"CI_PIPELINE_ID": "4321", "CI_COMMIT_SHA": HEAD})
    out = tmp_path / "out"
    argv = ["--digest", str(root), "--out", str(out)]
    assert runner.main(argv, harness.runtime()) == 0
    assert _lines(capsys)[-1].startswith("DIGEST: no shard ran under")
    digest = json.loads((out / "digest.json").read_text(encoding="utf-8"))
    assert (digest["shards_ran"], digest["flows"], digest["stack_state"]) == (
        0,
        [],
        "missing",
    )
    assert (digest["pipeline_id"], digest["develop_sha"]) == (4321, HEAD)
    history = story_digest.History.from_digests([(4321, digest)])
    assert (history.nights, history.gaps) == ((), (4321,))
    text = (out / "digest.md").read_text(encoding="utf-8")
    assert "- `shard-1`: skipped — the review stack is still updating" in text
    assert "later nights read this one as a gap" in text
    argv = ["--merge", str(tmp_path / "empty"), "--out", str(tmp_path / "merged")]
    assert runner.main(argv, harness.runtime()) == 1
    assert _lines(capsys)[0].startswith("FAILED: no story-run.json under")


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
                {"key": "a-story@x", "kind": "upload_failed", "detail": ""},
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
    assert len(lines) == 5
