# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/sdlc/ai_ux_test.py, the MR UX test's orchestrator.

The orchestrator is where every other piece of the UX test is wired together, so
these tests drive :func:`ai_ux_test.main` end to end with a fully fake
:class:`ai_ux_test.Runtime`: no subprocess, no browser, no network and no AWS. The
collaborators record what they were asked to do, and the GitLab side is the real
``gitlab_api.UxGitlab`` with only its transport (``_send``) replaced by an in-memory
GitLab, so what is asserted about a note is the body that would have gone over the
wire, after the client's own quick-action defusing.

What is pinned, by the claim each test names:

* the precheck stops a draft, a test-only change, a missing build, a missing
  variable or API URL, a moved head and an already-tested head before anything is
  started, and ``--precheck`` writes its files and its one decision line;
* a run is confined before it is signed in, records, posts only on a clean socket
  audit, appends to the code review for the same full head (or posts on its own),
  replies once, and marks older videos superseded, and an upload GitLab does not
  take costs the section its video and nothing else;
* every failure (a timeout, a missing report, a cancel) still runs the cleanup and
  writes ``summary.json``;
* a video is rendered, attached and uploaded only when ``--stack``, the UI
  environment's settings parameter and the stack the signed-in page reports all
  name the review stack: another stack, or a UI environment pointing at one under
  ``--stack idp-ux-nightly``, gets its findings posted with no video and a line
  saying it was withheld, whether appended, posted on its own or kept as a dry
  run, and a page that does not report the review stack is withheld as
  unconfirmed; the developer opt-in that renders one anyway forces a dry run and
  is refused under CI;
* nothing secret survives into any artifact, note body or printed line, including
  values the model's report carries in a spelling ``sanitize`` would change;
* the harness list is closed over its imports, ``ai_mr_review`` has one importer,
  the recorder adapter serves exactly the tools the profile grants, the script
  imports on a Python with no ``SIGKILL`` as the Windows host side needs, and the
  CI skill is a differences-only contract beside ``ux-test.md``.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import dataclasses
import json
import os
import re
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parent
SCRIPT = SCRIPTS / "sdlc" / "ai_ux_test.py"
for _entry in (SCRIPTS, SCRIPTS / "sdlc"):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

import ai_ux_test  # noqa: E402
import ux_recorder_mcp  # noqa: E402
import ux_recorder_render  # noqa: E402
from ux_runner import (  # noqa: E402
    account,
    agent,
    chrome,
    egress,
    gitlab_api,
    mr_inputs,
    paths,
    processes,
    prompt,
    recording,
    section,
    session,
    signin,
    tool_profiles,
)
from ux_runner.ui_change import ChangedFile  # noqa: E402

review = gitlab_api.review

IID = 812
ME = 4242
SOMEONE_ELSE = 5151
HEAD = "0123456789abcdef" * 2 + "01234567"
BASE = "fedcba9876543210" * 2 + "fedcba98"
NEWER = "89abcdef" * 5
OLDER = "a1b2c3d4" * 5
#: A head whose hex holds a run of exactly twelve digits, which the redactor's
#: account pattern would otherwise rewrite.
DIGIT_HEAD = "ab" + "123456789012" + "cd" * 13
ACCOUNT = "123456789012"
OTHER_ACCOUNT = "111122223333"
PARTITION = "aws"
MODEL = "us.anthropic.claude-opus-5"
CREDENTIAL = "test-review-credential-value"
PERSONA_USER = "ux-persona-admin@example.invalid"
PERSONA_CREDENTIAL = "Ux!3cretive-persona-9aZ"
AWS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
AWS_KEY_TEXT = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
AWS_SESSION_TEXT = "FwoGZXIvYXdzEXAMPLEambientsessionvalue"
SCOPED_KEY_ID = "ASIAIOSFODNN7SCOPED1"
SCOPED_KEY_TEXT = "scopedSecretKeyValueForBedrockOnly0001"
SCOPED_SESSION_TEXT = "scopedSessionValueForBedrockOnly000001"
POLLY_KEY_ID = "ASIAIOSFODNN7POLLY01"
POLLY_KEY_TEXT = "pollySecretKeyValueForSpeechOnly000001"
POLLY_SESSION_TEXT = "pollySessionValueForSpeechOnly00000001"
FORGE_HOST = "gitlab.internal.example"
API_HOST = "abcde12345.execute-api.us-east-1.amazonaws.com"
UPLOAD_HASH = "0123456789abcdef0123456789abcdef"
UPLOAD_ID = 456
DIFF_MARKER = "UNIQUE-DIFF-MARKER-7f3a"
DIFF = (
    "diff --git a/src/ui/src/components/document-viewer/FileViewer.tsx "
    "b/src/ui/src/components/document-viewer/FileViewer.tsx\n"
    f"+  <Button>Rotate</Button> // {DIFF_MARKER}\n"
)
UI_ENV_TEXT = "\n".join(
    (
        "VITE_USER_POOL_ID=us-east-1_AbCdEfGhI",
        "VITE_USER_POOL_CLIENT_ID=1a2b3c4d5e6f7g8h9i0j1k2l3m",
        "VITE_IDENTITY_POOL_ID=us-east-1:11111111-2222-3333-4444-abcdefabcdef",
        f"VITE_API_BASE_URL=https://{API_HOST}/api",
        "VITE_STREAM_URL=https://abcdefghijklmnopqrstuvwxyz012345.lambda-url."
        "us-east-1.on.aws/",
        "VITE_AWS_REGION=us-east-1",
        "VITE_ENABLE_QUICK_START_WIDGET=true",
        "VITE_SETTINGS_PARAMETER=idp-ux-nightly-Settings",
        "VITE_TEST_SET_BUCKET_NAME=idp-ux-nightly-testsetbucket-abc123",
        "VITE_INPUT_BUCKET_NAME=idp-ux-nightly-inputbucket-def456",
        "VITE_LATENCY_OPTIONS=[60, 120, 300, 600, 900, 1800, 3600]",
        "UX_TEST_BUCKETS=idp-ux-nightly-outputbucket-ghi789,"
        "idp-ux-nightly-workingbucket-jkl012",
    )
)
VISIBLE_FILE = "src/ui/src/components/document-viewer/FileViewer.tsx"
TEST_FILE = "src/ui/src/components/document-viewer/__tests__/FileViewer.test.tsx"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _personas() -> str:
    entry = dict(username=PERSONA_USER, password=PERSONA_CREDENTIAL)
    return _b64(json.dumps({"Admin": entry}))


def _environ(**changes: str | None) -> dict[str, str]:
    """The job's environment, with the two variables its ``variables:`` block sets."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GITLAB_REVIEW_TOKEN": CREDENTIAL,
        "UX_TEST_UI_ENV": _b64(UI_ENV_TEXT),
        "UX_TEST_PERSONAS": _personas(),
        "UX_TEST_AWS_ACCOUNT_ID": ACCOUNT,
        "AWS_ACCESS_KEY_ID": AWS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": AWS_KEY_TEXT,
        "AWS_SESSION_TOKEN": AWS_SESSION_TEXT,
        "UX_HARNESS": " ".join(ai_ux_test.HARNESS_PATHS),
        "UX_ENTRY": ai_ux_test.ENTRY,
    }
    for name, value in changes.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def _inputs(
    *, head: str = HEAD, changed: tuple[ChangedFile, ...] | None = None
) -> mr_inputs.MrInputs:
    return mr_inputs.MrInputs(
        iid=IID,
        title="Add page rotation",
        author="jdoe",
        web_url=f"https://gitlab.test/group/proj/-/merge_requests/{IID}",
        source_branch="feature/rotate",
        target_branch="develop",
        head_sha=head,
        base_sha=BASE,
        changed=changed
        if changed is not None
        else (ChangedFile("M", VISIBLE_FILE), ChangedFile("A", TEST_FILE)),
        ui_diff=DIFF,
        ui_diff_truncated=False,
        ui_diff_bytes=len(DIFF.encode()),
        title_from_git=True,
    )


def _report(
    *,
    visible: bool = True,
    findings: tuple[str, ...] = ("Rotate button has no tooltip",),
    quote: str | None = None,
    prose: str = "",
) -> str:
    block = {
        "flows": [
            {
                "id": "change",
                "title": "Rotate a page in the document viewer",
                "outcome": "works",
                "summary": "2 findings",
            }
        ],
        "findings": [{"flow": "change", "text": text} for text in findings],
        "breakage": [],
        "not_covered": [],
        "visible_change_found": visible,
        "injection_attempts": []
        if quote is None
        else [{"where": "page", "quote": quote}],
    }
    return (
        "Looked at\n  ✅ change  Rotate a page — works\n"
        f"{prose}\n\n```json\n{json.dumps(block, indent=2)}\n```\n"
    )


def _review_body(head: str) -> str:
    return (
        f"<!-- ai-review: sha={head} rev=1 -->\n"
        "## PR/MR Review: !812\n"
        "/merge\n\n"
        "---\n"
        f"🤖 Automated review of `{head[:8]}` by `{MODEL}` over the full diff. "
        "**Advisory only** — re-runs are skipped until the head commit changes.\n"
    )


def _old_section_body(head: str) -> str:
    return (
        _review_body(head).rstrip("\n")
        + "\n\n---\n"
        + f"<!-- ux-test: sha={head} rev=1 upload=111 -->\n"
        + "## 🖱️ UX test — Admin, `idp-ux-nightly`, MR UI build `x`\n\n"
        + f"![UX test](/uploads/{'f' * 32}/ux-test-mr812-{head[:8]}.mp4)\n"
    )


class FakeGitlab(gitlab_api.UxGitlab):
    """The real client over an in-memory GitLab: only the transport is replaced."""

    def __init__(self, *, head: str = HEAD, draft: bool = False) -> None:
        super().__init__("https://gitlab.test/api/v4", "group/proj", CREDENTIAL)
        self.head = head
        self.draft = draft
        self.store: dict[int, dict[str, Any]] = {}
        self.threads: dict[str, list[int]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.listings = 0
        self.on_listing: Callable[[FakeGitlab], None] | None = None
        self.upload_error: Exception | None = None
        self.upload_url: str | None = None
        self.title = "Add page rotation"
        self.web_url = f"https://gitlab.test/group/proj/-/merge_requests/{IID}"
        self.log: list[str] | None = None
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
            "created_at": f"2026-10-15T00:{ident % 60:02d}:00Z",
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
            return self._merge_request(), {}
        if method == "GET" and path == f"{mr}/notes":
            self.listings += 1
            if self.on_listing is not None:
                self.on_listing(self)
            return [dict(self.store[i]) for i in sorted(self.store)], {}
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
        if method == "POST" and path == f"{project}/uploads" and self.upload_error:
            raise self.upload_error
        if method == "POST" and path == f"{project}/uploads" and data is not None:
            name = re.search(rb'filename="([^"]+)"', data)
            filename = name.group(1).decode() if name else "upload"
            url_path = self.upload_url or f"/uploads/{UPLOAD_HASH}/{filename}"
            return {"id": UPLOAD_ID, "url": url_path, "alt": filename}, {}
        raise review.Failure(f"the fake GitLab has no route for {method} {path}")

    def _merge_request(self) -> dict[str, Any]:
        return {
            "iid": IID,
            "title": ("Draft: " if self.draft else "") + self.title,
            "author": {"username": "jdoe"},
            "source_branch": "feature/rotate",
            "target_branch": "develop",
            "web_url": self.web_url,
            "sha": self.head,
            "diff_refs": {"head_sha": self.head},
            "draft": self.draft,
        }


class FakeClock:
    """Monotonic time that only moves when the run sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeSts:
    """STS for the job's role: the first assume is claude's, the second render's.

    Each assume issues its own credentials, so a test can tell which consumer was
    handed which session, and ``failing`` names the assumes (by position) that
    are refused.
    """

    def __init__(
        self,
        account: str = ACCOUNT,
        *,
        assume_fails: bool = False,
        failing: tuple[int, ...] = (),
    ) -> None:
        self.account = account
        self.failing = (0, 1) if assume_fails else failing
        self.assumed: list[dict[str, Any]] = []

    def get_caller_identity(self) -> dict[str, str]:
        arn = f"arn:{PARTITION}:sts::{self.account}:assumed-role/idp-sdlc-GitLab/job"
        return {"Account": self.account, "Arn": arn}

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        position = len(self.assumed)
        self.assumed.append(kwargs)
        if position in self.failing:
            raise RuntimeError("AccessDenied: the role cannot assume itself")
        issued = (
            dict(
                AccessKeyId=SCOPED_KEY_ID,
                SecretAccessKey=SCOPED_KEY_TEXT,
                SessionToken=SCOPED_SESSION_TEXT,
            )
            if position == 0
            else dict(
                AccessKeyId=POLLY_KEY_ID,
                SecretAccessKey=POLLY_KEY_TEXT,
                SessionToken=POLLY_SESSION_TEXT,
            )
        )
        return {"Credentials": issued}

    def policy_actions(self, position: int) -> list[str]:
        """The actions the session policy of one assume allows."""
        policy = json.loads(self.assumed[position]["Policy"])
        (statement,) = policy["Statement"]
        return statement["Action"]


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


class FakeSession:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class FakeWatch:
    def __init__(
        self, state: str, violations: tuple[str, ...], log: list[str] | None = None
    ) -> None:
        self.state = state
        self.violations = violations
        self.log = log
        self.started = 0
        self.stopped = 0

    def start(self) -> FakeWatch:
        self.started += 1
        return self

    def stop(self) -> chrome.SocketAudit:
        self.stopped += 1
        if self.log is not None:
            self.log.append("watch_stopped")
        return chrome.SocketAudit(self.state, self.violations, 4)


class Harness:
    """The fake runtime and every input of one run, with a log of what was called."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.calls: list[str] = []
        self.forge = FakeGitlab()
        self.forge.log = self.calls
        self.clock = FakeClock()
        self.sts = FakeSts()
        self.inputs = _inputs()
        self.environ = _environ()
        self.agent_text = _report()
        self.agent_effect: Callable[[], None] | None = None
        self.completed = processes.Completed(0, "{}", "", False, 61.0)
        self.timed_out = False
        self.crashed: processes.Completed | None = None
        self.audit_state = "clean"
        self.audit_violations: tuple[str, ...] = ()
        self.narration = "on"
        self.video_size = 2048
        self.timeline = ["start", "mark", "mark", "stop"]
        self.mark_say = "Now the page is rotated."
        self.mark_label = "Rotate page 2"
        self.spoken: dict[int, str] = {}
        self.rendered_labels: list[str] = []
        self.render_saw_narration_md = False
        self.status: dict[str, Any] | None = {
            "ok": True,
            "marks": 3,
            "clicks_rejected": 1,
            "cdp_messages_dropped": 2,
        }
        self.viewport: dict[str, Any] = {"w": 1600, "h": 1000, "dpr": 1}
        self.viewport_error: Exception | None = None
        self.facts = signin.PageFacts(
            "idp-ux-nightly", "0.6.12", "2026-10-13", (1600, 1000, 1.0), None
        )
        self.chapter_list = [("0:00", "Open the viewer"), ("0:31", "Rotate page 2")]
        self.refusals = [
            {
                "t": "2026-10-15T00:00:00Z",
                "method": "GET",
                "target": f"http://{chrome.SELFTEST_HOST}/",
                "reason": "plain_http_not_allowed",
            }
        ]
        self.agent_args: tuple[Any, ...] = ()
        self.agent_kwargs: dict[str, Any] = {}
        self.mcp_mode: int | None = None
        self.mcp_config: dict[str, Any] = {}
        self.recorder_kwargs: dict[str, Any] = {}
        self.render_kwargs: dict[str, Any] = {}
        self.proxy_kwargs: dict[str, Any] = {}
        self.chrome_kwargs: dict[str, Any] = {}
        self.strays: tuple[str, ...] = ()
        self.swept: list[tuple[int | None, int | None]] = []
        self.watch_uid: int | None = -1
        self.signed_in: list[signin.Persona] = []
        self.session = FakeSession()
        self.watch: FakeWatch | None = None
        self.chrome_process = FakeProcess(
            "chrome", lambda: self.calls.append("chrome_terminated")
        )
        self.proxy_process: FakeProcess | None = None
        self.run_dir: Path | None = None
        self.build = tmp_path / "build"
        self.build.mkdir()
        (self.build / "index.html").write_text("<!doctype html>", encoding="utf-8")
        self.artifacts = tmp_path / "ux-tests"
        self.prefix = tmp_path / "toolchain"
        toolchain = paths.Toolchain(self.prefix)
        for binary in (toolchain.chrome, toolchain.claude, toolchain.devtools_mcp):
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
        toolchain.versions_file.write_text(
            json.dumps({"chrome": "153.0.0.0", "node": "v22.14.0"}), encoding="utf-8"
        )
        monkeypatch.setattr(chrome, "policy_installed", lambda *a, **k: True)
        monkeypatch.setattr(processes, "can_switch_user", lambda: True)
        self.render_owner = os.geteuid()
        self.render_uid_asked: list[str | None] = []
        self.render_effect: Callable[[Path], None] | None = None
        self.real_chapters = False
        monkeypatch.setattr(recording, "render_uid", self._render_uid)

    def argv(self, *extra: str) -> list[str]:
        return [
            "--mr",
            str(IID),
            "--project",
            "group/proj",
            "--api-url",
            "https://gitlab.test/api/v4",
            "--repo-dir",
            str(self.tmp),
            "--build-dir",
            str(self.build),
            "--artifact-dir",
            str(self.artifacts),
            "--toolchain-prefix",
            str(self.prefix),
            "--model",
            MODEL,
            "--region",
            "us-east-1",
            "--stack",
            "idp-ux-nightly",
            "--target-branch",
            "develop",
            *extra,
        ]

    def main(self, *extra: str) -> int:
        return ai_ux_test.main(self.argv(*extra), self.runtime())

    def runtime(self) -> ai_ux_test.Runtime:
        return ai_ux_test.Runtime(
            forge=self._forge,
            collect=self._collect,
            sts=self._sts,
            launch_proxy=self._launch_proxy,
            launch_chrome=self._launch_chrome,
            wait_for_cdp=self._step("wait_for_cdp", {"Browser": "HeadlessChrome"}),
            first_page_target=self._step(
                "first_page_target",
                {
                    "id": "TARGET1",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/T1",
                },
            ),
            open_session=self._open_session,
            verify_policy=self._step("verify_policy", None),
            verify_proxy_path=self._step("verify_proxy_path", None),
            sign_in=self._sign_in,
            page_facts=self._step("page_facts", self.facts),
            verify_viewport=self._verify_viewport,
            measure_viewport=self._measure_viewport,
            socket_watch=self._socket_watch,
            kill_strays=self._kill_strays,
            start_recorder=self._start_recorder,
            stop_recorder=self._stop_recorder,
            kill_recorder=self._kill_recorder,
            recorder_status=self._step("recorder_status", self.status),
            run_agent=self._run_agent,
            render_video=self._render_video,
            chapters=(
                recording.chapters
                if self.real_chapters
                else self._step("chapters", self.chapter_list)
            ),
            sleep=self.clock.sleep,
            clock=self.clock.clock,
            environ=self.environ,
        )

    def _step(self, name: str, answer: Any) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return answer

        return call

    def _forge(self, args: Any, credential: str) -> FakeGitlab:
        self.calls.append("forge")
        return self.forge

    def _collect(self, *args: Any, **kwargs: Any) -> mr_inputs.MrInputs:
        self.calls.append("collect")
        return self.inputs

    def _sts(self, region: str) -> FakeSts:
        self.calls.append("sts")
        return self.sts

    def _launch_proxy(self, **kwargs: Any) -> egress.ProxyRun:
        self.calls.append("launch_proxy")
        self.proxy_kwargs = kwargs
        run_dir = Path(kwargs["run_dir"])
        self.run_dir = run_dir
        proxy_dir = run_dir / "proxy"
        proxy_dir.mkdir()
        refusals = proxy_dir / "refusals.jsonl"
        refusals.write_text(
            "".join(json.dumps(record) + "\n" for record in self.refusals),
            encoding="utf-8",
        )
        stats = proxy_dir / "stats.json"

        def write_stats() -> None:
            self.calls.append("proxy_terminated")
            stats.write_text(
                json.dumps(
                    {
                        "served": 212,
                        "connect_allowed": 93,
                        "refused": len(self.refusals),
                        "upstream_failed": 0,
                        "by_reason": {"plain_http_not_allowed": len(self.refusals)},
                    }
                ),
                encoding="utf-8",
            )

        self.proxy_process = FakeProcess("egress-proxy", write_stats)
        return egress.ProxyRun(
            process=self.proxy_process,
            ready_file=proxy_dir / "ready",
            refusal_log=refusals,
            stats_file=stats,
            log_file=proxy_dir / "proxy.log",
            owner=os.geteuid(),
        )

    def _launch_chrome(self, *args: Any, **kwargs: Any) -> FakeProcess:
        self.calls.append("launch_chrome")
        self.chrome_kwargs = kwargs
        return self.chrome_process

    def _kill_strays(self, uid: int | None, group: int | None) -> tuple[str, ...]:
        self.calls.append("kill_strays")
        self.swept.append((uid, group))
        return self.strays

    def _open_session(self, ws_url: str) -> FakeSession:
        self.calls.append("open_session")
        return self.session

    def _sign_in(self, session: Any, persona: signin.Persona, **kwargs: Any) -> None:
        self.calls.append("sign_in")
        self.signed_in.append(persona)

    def _verify_viewport(self, session: Any) -> dict[str, Any]:
        self.calls.append("verify_viewport")
        if self.viewport_error is not None:
            raise self.viewport_error
        return dict(self.viewport)

    def _measure_viewport(self, session: Any) -> dict[str, Any]:
        self.calls.append("measure_viewport")
        return dict(self.viewport)

    def _socket_watch(self, uid: int | None) -> FakeWatch:
        self.calls.append("socket_watch")
        self.watch_uid = uid
        self.watch = FakeWatch(self.audit_state, self.audit_violations, self.calls)
        return self.watch

    def _start_recorder(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("start_recorder")
        self.recorder_kwargs = kwargs
        out_dir = Path(kwargs["out_dir"])
        session = out_dir / "session-1"
        session.mkdir(parents=True)
        (out_dir / "current.json").write_text("{}", encoding="utf-8")
        socket = str(out_dir / "recorder.sock")
        return {"session_dir": str(session), "socket": socket, "pid": 0}

    def _stop_recorder(self, **kwargs: Any) -> dict[str, Any]:
        """Finalise the session as the recorder does, ``narration.md`` included.

        The real stop writes ``narration.md`` from the events' spoken lines, and
        render prefers it to the events, so the fake writes it the same way.
        """
        self.calls.append("stop_recorder")
        (Path(kwargs["out_dir"]) / "current.json").unlink(missing_ok=True)
        events: list[dict[str, Any]] = []
        for seq, kind in enumerate(self.timeline):
            event: dict[str, Any] = {"seq": seq, "t": float(seq), "type": kind}
            if kind == "mark":
                event.update(label=f"{self.mark_label} {seq}", say=self.mark_say)
            events.append(event)
        session_dir = Path(kwargs["session_dir"])
        (session_dir / "timeline.json").write_text(
            json.dumps({"version": 1, "events": events}), encoding="utf-8"
        )
        recorded = [ux_recorder_render.Event.from_record(e) for e in events]
        (session_dir / "narration.md").write_text(
            ux_recorder_render.write_narration_md(recorded), encoding="utf-8"
        )
        return {"ok": True}

    def _kill_recorder(self, out_dir: Path, **kwargs: Any) -> None:
        self.calls.append("kill_recorder")
        (Path(out_dir) / "current.json").unlink(missing_ok=True)

    def _run_agent(
        self, *args: Any, **kwargs: Any
    ) -> tuple[agent.AgentResult | None, processes.Completed]:
        self.calls.append("run_agent")
        self.agent_args = args
        self.agent_kwargs = kwargs
        argv = list(kwargs["argv"])
        config = Path(argv[argv.index("--mcp-config") + 1])
        self.mcp_mode = config.stat().st_mode & 0o777
        self.mcp_config = json.loads(config.read_text(encoding="utf-8"))
        if self.agent_effect is not None:
            self.agent_effect()
        if self.timed_out:
            return None, processes.Completed(-9, "", "", True, 1500.0)
        if self.crashed is not None:
            return None, self.crashed
        result = agent.AgentResult(
            text=self.agent_text,
            cost_usd=4.12,
            num_turns=57,
            duration_s=912.4,
            stop="end_turn",
            is_error=False,
        )
        return result, self.completed

    def _render_video(
        self, session_dir: Path, **kwargs: Any
    ) -> tuple[Path | None, str]:
        """Read the session the way render does, then write the video.

        What render would speak and caption comes from the real
        ``ux_recorder_render.narration_lines``, so ``narration.md``'s precedence
        over the events is the real one.
        """
        self.calls.append("render_video")
        self.render_kwargs = kwargs
        session = Path(session_dir)
        self.render_saw_narration_md = (session / "narration.md").exists()
        timeline = json.loads((session / "timeline.json").read_text("utf-8"))
        events = [ux_recorder_render.Event.from_record(e) for e in timeline["events"]]
        self.spoken = ux_recorder_render.narration_lines(session, events)
        self.rendered_labels = [e.label for e in events if e.label]
        if self.video_size < 0:
            return None, self.narration
        mp4 = session / "review.mp4"
        mp4.write_bytes(b"\0" * self.video_size)
        if self.render_effect is not None:
            self.render_effect(session)
        return mp4, self.narration

    def _render_uid(self, user: str | None) -> int:
        """The uid this fake render writes as, which is the test's own."""
        self.render_uid_asked.append(user)
        return self.render_owner

    def summary(self) -> dict[str, Any]:
        return json.loads((self.artifacts / "summary.json").read_text("utf-8"))

    def artifact(self, name: str) -> str:
        return (self.artifacts / name).read_text(encoding="utf-8")


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


def _first_line(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out.splitlines()[0]


def _defused(text: str) -> bool:
    return not any(line.lstrip().startswith("/") for line in text.split("\n"))


# --- precheck ---------------------------------------------------------------------


@pytest.mark.unit
def test_a_draft_is_skipped_before_any_browser_is_started(harness: Harness) -> None:
    harness.forge.draft = True
    assert harness.main() == 0
    summary = harness.summary()
    assert summary["status"] == "skipped"
    assert summary["detail"] == "a draft; drafts are not tested"
    assert not {"sts", "launch_proxy", "launch_chrome", "run_agent"} & set(
        harness.calls
    )


@pytest.mark.unit
def test_a_test_only_change_is_skipped_before_anything_the_toolchain_runs(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.inputs = _inputs(changed=(ChangedFile("M", TEST_FILE),))
    assert harness.main("--precheck") == 0
    assert _first_line(capsys) == "SKIPPED: only tests changed under src/ui"
    assert not (harness.artifacts / "proceed").exists()
    assert harness.calls == ["collect", "forge"]


@pytest.mark.unit
def test_a_missing_ui_build_is_skipped(harness: Harness) -> None:
    (harness.build / "index.html").unlink()
    assert harness.main() == 0
    assert harness.summary()["detail"] == (
        "no UI build: ux_test_build was skipped or failed"
    )
    assert "launch_proxy" not in harness.calls


@pytest.mark.unit
def test_a_missing_persona_variable_is_skipped_naming_it(harness: Harness) -> None:
    harness.environ.pop("UX_TEST_PERSONAS")
    assert harness.main() == 0
    summary = harness.summary()
    assert summary["status"] == "skipped"
    assert "UX_TEST_PERSONAS" in summary["detail"]


@pytest.mark.unit
def test_a_persona_the_variable_does_not_hold_is_skipped(harness: Harness) -> None:
    assert harness.main("--persona", "Reviewer") == 0
    assert "has no 'Reviewer' persona" in harness.summary()["detail"]


@pytest.mark.unit
def test_an_undecodable_ui_environment_is_skipped_without_its_value(
    harness: Harness,
) -> None:
    harness.environ["UX_TEST_UI_ENV"] = "not base64 at all!"
    assert harness.main() == 0
    detail = harness.summary()["detail"]
    assert detail.startswith("UX_TEST_UI_ENV cannot be used")
    assert "not base64 at all" not in detail


@pytest.mark.unit
def test_skips_become_failures_with_fail_on_skip(harness: Harness) -> None:
    harness.environ.pop("UX_TEST_AWS_ACCOUNT_ID")
    assert harness.main("--fail-on-skip") == 1
    assert "UX_TEST_AWS_ACCOUNT_ID" in harness.summary()["detail"]


@pytest.mark.unit
def test_no_token_without_dry_run_is_skipped_with_the_remedy(harness: Harness) -> None:
    harness.environ.pop("GITLAB_REVIEW_TOKEN")
    assert harness.main() == 0
    detail = harness.summary()["detail"]
    assert detail.startswith("GITLAB_REVIEW_TOKEN is not set")
    assert "--dry-run" in detail


def _without_api_url(argv: list[str]) -> list[str]:
    at = argv.index("--api-url")
    return argv[:at] + argv[at + 2 :]


@pytest.mark.unit
def test_no_api_url_is_a_skip_naming_the_remedy_before_any_request(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no built-in GitLab instance: outside CI the caller names one."""
    monkeypatch.delenv("CI_API_V4_URL", raising=False)
    argv = _without_api_url(harness.argv())
    assert ai_ux_test.parse_args(argv).api_url == ""
    assert ai_ux_test.main(argv, harness.runtime()) == 0
    summary = harness.summary()
    assert summary["status"] == "skipped"
    assert "--api-url" in summary["detail"] and "CI_API_V4_URL" in summary["detail"]
    assert "forge" not in harness.calls
    assert harness.forge.requests == []
    assert not {"sts", "launch_proxy", "run_agent"} & set(harness.calls)


@pytest.mark.unit
def test_collect_only_without_an_api_url_takes_the_title_from_git(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CI_API_V4_URL", raising=False)
    target = harness.tmp / "out" / "inputs.json"
    argv = _without_api_url(harness.argv("--collect-only", "--inputs", str(target)))
    assert ai_ux_test.main(argv, harness.runtime()) == 0
    out = capsys.readouterr().out
    assert "no API metadata (no GitLab API URL" in out
    assert out.splitlines()[-1] == f"collected: {target}"
    assert mr_inputs.read(target).title_from_git is True
    assert harness.forge.requests == []


@pytest.mark.unit
def test_an_account_mismatch_is_skipped_showing_only_the_last_four_digits(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.sts = FakeSts(OTHER_ACCOUNT)
    assert harness.main() == 0
    summary = harness.summary()
    out = capsys.readouterr().out
    assert summary["status"] == "skipped"
    assert summary["stack"]["account_check"] == "mismatch"
    for text in (summary["detail"], out):
        assert "3333" in text and "9012" in text
        assert ACCOUNT not in text and OTHER_ACCOUNT not in text
    assert not {"launch_proxy", "launch_chrome", "run_agent"} & set(harness.calls)


@pytest.mark.unit
def test_an_existing_section_of_mine_for_the_head_is_unchanged(
    harness: Harness,
) -> None:
    harness.forge.add_note(f"<!-- ux-test: sha={HEAD} rev=1 upload=none -->")
    assert harness.main() == 0
    summary = harness.summary()
    assert (summary["status"], summary["detail"]) == ("unchanged", "already tested")
    assert "launch_proxy" not in harness.calls


@pytest.mark.unit
def test_a_section_forged_by_another_author_does_not_stop_the_run(
    harness: Harness,
) -> None:
    harness.forge.add_note(
        f"<!-- ux-test: sha={HEAD} rev=1 upload=none -->", author=SOMEONE_ELSE
    )
    harness.forge.add_note(_review_body(HEAD), author=SOMEONE_ELSE)
    assert harness.main("--review-poll-minutes", "0") == 0
    summary = harness.summary()
    assert summary["status"] == "posted"
    assert summary["note"]["action"] == "standalone"


@pytest.mark.unit
def test_precheck_writes_its_files_and_the_marker_only_on_proceed(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert harness.main("--precheck") == 0
    assert _first_line(capsys) == f"PROCEED: 1 user-visible file(s) at {HEAD[:8]}"
    record = json.loads(harness.artifact("precheck.json"))
    assert record == {
        "version": 1,
        "proceed": True,
        "status": "proceed",
        "detail": "1 user-visible file(s)",
        "head_sha": HEAD,
    }
    assert (harness.artifacts / "proceed").read_text(encoding="utf-8") == ""
    collected = mr_inputs.read(harness.artifacts / "inputs.json")
    assert collected.head_sha == HEAD
    assert collected.title == "Add page rotation"
    assert collected.title_from_git is False
    assert not (harness.artifacts / "summary.json").exists()

    harness.environ.pop("UX_TEST_UI_ENV")
    assert harness.main("--precheck") == 0
    assert _first_line(capsys).startswith("SKIPPED: UX_TEST_UI_ENV is not set")
    assert not (harness.artifacts / "proceed").exists()
    assert json.loads(harness.artifact("precheck.json"))["proceed"] is False
    assert harness.summary()["status"] == "skipped"


@pytest.mark.unit
def test_precheck_reports_unchanged_as_its_first_line(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.forge.add_note(f"<!-- ux-test: sha={HEAD} rev=1 upload=7 -->")
    assert harness.main("--precheck") == 0
    assert _first_line(capsys) == "UNCHANGED: already tested"


@pytest.mark.unit
def test_precheck_fails_when_the_inputs_describe_another_mr(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    other = dataclasses.replace(_inputs(), iid=IID + 1)
    path = mr_inputs.write(other, harness.tmp / "inputs.json")
    assert harness.main("--precheck", "--inputs", str(path)) == 1
    assert (
        _first_line(capsys) == f"FAILED: the inputs describe MR !{IID + 1}, not !{IID}"
    )


@pytest.mark.unit
def test_a_head_the_pipeline_did_not_build_is_left_to_its_own_pipeline(
    harness: Harness,
) -> None:
    harness.environ["CI_COMMIT_SHA"] = NEWER
    assert harness.main() == 0
    detail = harness.summary()["detail"]
    assert detail.startswith(f"this pipeline built {NEWER[:8]}")
    assert "launch_proxy" not in harness.calls


@pytest.mark.unit
def test_a_merged_results_pipeline_compares_the_source_head(
    harness: Harness,
) -> None:
    harness.environ["CI_COMMIT_SHA"] = NEWER
    harness.environ["CI_MERGE_REQUEST_SOURCE_BRANCH_SHA"] = HEAD
    assert harness.main("--dry-run") == 0
    assert harness.summary()["status"] == "dry_run"


@pytest.mark.unit
def test_a_head_that_moved_since_collection_is_skipped_before_the_browser(
    harness: Harness,
) -> None:
    harness.forge.head = NEWER
    assert harness.main() == 0
    assert harness.summary()["detail"].startswith(f"the MR head moved to {NEWER[:8]}")
    assert "launch_proxy" not in harness.calls


@pytest.mark.unit
def test_too_little_time_left_in_the_job_is_skipped(harness: Harness) -> None:
    started = datetime.now(timezone.utc) - timedelta(minutes=50)
    harness.environ["CI_JOB_STARTED_AT"] = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert harness.main() == 0
    assert harness.summary()["detail"].startswith("not enough time left in the job")
    assert "launch_proxy" not in harness.calls
    assert harness.run_dir is None


# --- the harness pin --------------------------------------------------------------

_HARNESS = list(ai_ux_test.HARNESS_PATHS)
_OWN = "scripts/sdlc/ai_ux_test.py"
_WRONG_HARNESS = f"UX_HARNESS is not the HARNESS_PATHS of {_OWN}, in order: it "
_NOT_THIS_ENTRY = f"not {_OWN}, the harness file this precheck belongs to"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("changes", "line"),
    [
        (
            {"UX_HARNESS": None},
            "UX_HARNESS is not set, so the harness this job extracted cannot be "
            f"checked against the HARNESS_PATHS of {_OWN}",
        ),
        (
            {"UX_HARNESS": " \n "},
            "UX_HARNESS is not set, so the harness this job extracted cannot be "
            f"checked against the HARNESS_PATHS of {_OWN}",
        ),
        (
            {"UX_HARNESS": " ".join(_HARNESS[:-1])},
            f"{_WRONG_HARNESS}lacks 1 of them, the first {_HARNESS[-1]!r}"
            f"{paths.STALE_JOB_HINT}",
        ),
        (
            {"UX_HARNESS": " ".join([*_HARNESS, "scripts/sdlc/json.py"])},
            f"{_WRONG_HARNESS}also names 1 more, the first 'scripts/sdlc/json.py'"
            f"{paths.STALE_JOB_HINT}",
        ),
        (
            {"UX_HARNESS": " ".join(reversed(_HARNESS))},
            f"{_WRONG_HARNESS}names the same paths in another order or more than once"
            f"{paths.STALE_JOB_HINT}",
        ),
        (
            {"UX_HARNESS": " ".join([*_HARNESS, _HARNESS[0]])},
            f"{_WRONG_HARNESS}names the same paths in another order or more than once"
            f"{paths.STALE_JOB_HINT}",
        ),
        (
            {"UX_ENTRY": None},
            f"UX_ENTRY is not set, so the job cannot be shown to run {_OWN}, the "
            "harness file this precheck belongs to",
        ),
        (
            {"UX_ENTRY": "scripts/ux_recorder.py"},
            f"UX_ENTRY is 'scripts/ux_recorder.py', {_NOT_THIS_ENTRY}",
        ),
    ],
    ids=[
        "harness-unset",
        "harness-blank",
        "harness-short",
        "harness-longer",
        "harness-reordered",
        "harness-repeated",
        "entry-unset",
        "entry-another-file",
    ],
)
def test_the_precheck_refuses_job_variables_that_name_another_harness(
    harness: Harness,
    capsys: pytest.CaptureFixture[str],
    changes: dict[str, str | None],
    line: str,
) -> None:
    """The job extracts what ``UX_HARNESS`` lists and runs ``UX_ENTRY``, and a
    project or pipeline variable overrides a job variable, so the precheck checks
    both against this module first: one line naming the wrong variable, exit 1,
    nothing asked of git or GitLab, and the artifact directory never made."""
    for name, value in changes.items():
        if value is None:
            harness.environ.pop(name)
        else:
            harness.environ[name] = value
    assert harness.main("--precheck") == 1
    assert capsys.readouterr().out.splitlines() == [f"FAILED: {line}"]
    assert harness.calls == []
    assert harness.forge.requests == []
    assert not harness.artifacts.exists()


@pytest.mark.unit
def test_the_precheck_runs_under_a_job_that_names_its_harness_in_its_own_script(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """A merge request whose ``.gitlab-ci.yml`` predates the shared setup runs this
    entry from a job that spells its harness list and the entry's path in its own
    script, where no variable can change them, and exports neither variable."""
    harness.environ.pop("UX_HARNESS")
    harness.environ.pop("UX_ENTRY")
    assert harness.main("--precheck") == 0
    assert _first_line(capsys).startswith("PROCEED: ")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("environ", "script_named", "refused"),
    [
        ({}, True, False),
        ({}, False, True),
        ({"UX_HARNESS": "", "UX_ENTRY": ""}, True, True),
        ({"UX_ENTRY": _OWN}, True, True),
        ({"UX_HARNESS": " ".join(_HARNESS)}, True, True),
        ({"UX_HARNESS": "scripts", "UX_ENTRY": _OWN}, True, True),
    ],
)
def test_only_an_entry_whose_job_named_its_harness_accepts_neither_variable(
    environ: dict[str, str], script_named: bool, refused: bool
) -> None:
    problem = paths.harness_pin_problem(
        environ, harness_paths=_HARNESS, entry=_OWN, script_named=script_named
    )
    assert bool(problem) is refused, problem


@pytest.mark.unit
def test_the_precheck_accepts_the_jobs_own_variables_however_they_are_spaced(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.environ["UX_HARNESS"] = "\n  ".join(_HARNESS) + "\n"
    harness.environ["UX_ENTRY"] = f"  {_OWN}\n"
    assert harness.main("--precheck") == 0
    assert _first_line(capsys).startswith("PROCEED: ")


@pytest.mark.unit
def test_a_pin_problem_quotes_no_value_longer_than_200_characters() -> None:
    """A variable's value reaches the job log only short and quoted, so neither a
    flood nor a terminal escape sequence in it is printed as written."""
    pinned = " ".join(_HARNESS)
    long = "x" * 201

    def problem(listed: str, entry: str) -> str:
        environ = {"UX_HARNESS": listed, "UX_ENTRY": entry}
        return paths.harness_pin_problem(environ, harness_paths=_HARNESS, entry=_OWN)

    for shown in (problem(f"{pinned} {long}", _OWN), problem(pinned, long)):
        assert long not in shown and "a value of 201 characters" in shown, shown
    assert repr("y" * 200) in problem(pinned, "y" * 200)
    escaped = problem(pinned, "a\x1b[31mb")
    assert "\x1b" not in escaped and "'a\\x1b[31mb'" in escaped
    assert problem(pinned, _OWN) == ""


@pytest.mark.unit
def test_the_entry_is_this_file_and_the_first_harness_path() -> None:
    assert SCRIPT.relative_to(REPO_ROOT).as_posix() == ai_ux_test.ENTRY == _OWN
    assert ai_ux_test.HARNESS_PATHS[0] == ai_ux_test.ENTRY


@pytest.mark.unit
def test_the_precheck_run_as_the_job_runs_it_refuses_an_unpinned_entry(
    tmp_path: Path,
) -> None:
    """On a bare python3 with ``-B``, from an environment holding only the job's
    two harness variables, as the job's before_script runs it."""
    out = tmp_path / "ux-tests"
    environ = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "UX_HARNESS": " ".join(_HARNESS),
        "UX_ENTRY": "scripts/sdlc/ai_mr_review.py",
    }
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-B", str(SCRIPT), "--mr", "1", "--precheck"]
        + ["--artifact-dir", str(out)],
        cwd=tmp_path,
        env=environ,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 1, completed.stderr[-400:]
    assert completed.stdout.splitlines() == [
        f"FAILED: UX_ENTRY is 'scripts/sdlc/ai_mr_review.py', {_NOT_THIS_ENTRY}"
    ]
    assert not out.exists()


# --- the artifact directory -------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("mode", [("--precheck",), ()], ids=["precheck", "run"])
def test_an_artifact_directory_that_is_a_symlink_is_refused_and_left_alone(
    harness: Harness, capsys: pytest.CaptureFixture[str], mode: tuple[str, ...]
) -> None:
    """In CI the directory sits in the MR's checkout and the harness writes as root.

    So an MR that commits ``ux-tests`` as a symlink must not decide where those
    writes land, and the stale marker the precheck removes is not removed through
    it either. Nothing is asked of GitLab, git or the browser first.
    """
    elsewhere = harness.tmp / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "proceed").write_text("kept", encoding="utf-8")
    harness.artifacts.symlink_to(elsewhere, target_is_directory=True)
    assert harness.main(*mode) == 1
    assert [p.name for p in elsewhere.iterdir()] == ["proceed"]
    assert (elsewhere / "proceed").read_text(encoding="utf-8") == "kept"
    out = capsys.readouterr().out
    assert out.startswith(f"FAILED: the artifact directory {harness.artifacts} is a")
    assert harness.calls == []
    assert harness.forge.requests == []


@pytest.mark.unit
@pytest.mark.parametrize("mode", [("--precheck",), ()], ids=["precheck", "run"])
def test_an_artifact_path_that_is_a_file_is_a_failure_and_left_alone(
    harness: Harness, capsys: pytest.CaptureFixture[str], mode: tuple[str, ...]
) -> None:
    harness.artifacts.write_text("not a directory", encoding="utf-8")
    assert harness.main(*mode) == 1
    assert harness.artifacts.read_text(encoding="utf-8") == "not a directory"
    out = capsys.readouterr().out
    assert out.startswith(
        f"FAILED: the artifact directory {harness.artifacts} cannot be made"
    )
    assert harness.calls == []


@pytest.mark.unit
def test_a_symlinked_artifact_file_is_refused_rather_than_written_through(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = harness.tmp / "outside.json"
    outside.write_text("untouched", encoding="utf-8")
    harness.artifacts.mkdir()
    (harness.artifacts / "precheck.json").symlink_to(outside)
    assert harness.main("--precheck") == 1
    assert outside.read_text(encoding="utf-8") == "untouched"
    assert _first_line(capsys).startswith("FAILED: precheck.json could not be written")
    assert not (harness.artifacts / "proceed").exists()
    assert harness.summary()["status"] == "failed"


@pytest.mark.unit
def test_a_symlinked_summary_is_refused_at_the_end_of_a_run(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = harness.tmp / "outside.json"
    outside.write_text("untouched", encoding="utf-8")
    harness.artifacts.mkdir()
    (harness.artifacts / "summary.json").symlink_to(outside)
    assert harness.main("--dry-run") == 1
    assert outside.read_text(encoding="utf-8") == "untouched"
    last = capsys.readouterr().out.splitlines()[-1]
    assert last.startswith("FAILED: summary.json could not be written: ")


@pytest.mark.unit
@pytest.mark.parametrize("name", ["section.md", f"ux-test-mr{IID}-{HEAD[:8]}.mp4"])
def test_a_full_run_refuses_to_write_an_artifact_through_a_symlink(
    harness: Harness, name: str
) -> None:
    outside = harness.tmp / "outside"
    outside.write_text("untouched", encoding="utf-8")
    harness.artifacts.mkdir()
    (harness.artifacts / name).symlink_to(outside)
    assert harness.main("--dry-run") == 1
    assert outside.read_text(encoding="utf-8") == "untouched"
    summary = harness.summary()
    assert summary["status"] == "failed"
    assert summary["detail"].startswith(f"{name} could not be written")
    assert harness.chrome_process.terminations == 1


# --- the full run -----------------------------------------------------------------


@pytest.mark.unit
def test_the_browser_is_confined_and_signed_in_before_recording_starts(
    harness: Harness,
) -> None:
    assert harness.main("--dry-run") == 0
    steps = (
        "launch_proxy",
        "launch_chrome",
        "socket_watch",
        "wait_for_cdp",
        "verify_policy",
        "verify_proxy_path",
        "sign_in",
        "start_recorder",
        "run_agent",
        "stop_recorder",
    )
    order = [name for name in harness.calls if name in steps]
    assert order == list(steps)
    assert harness.proxy_kwargs["user"] == paths.PROXY_USER
    assert harness.proxy_kwargs["build_dir"] == harness.build.resolve()
    assert harness.chrome_kwargs["user"] == paths.BROWSER_USER
    assert harness.watch_uid == paths.BROWSER_UID
    assert [p.username for p in harness.signed_in] == [PERSONA_USER]
    assert harness.session.closed == 1


@pytest.mark.unit
def test_the_signed_in_viewport_is_required_before_recording_and_recorded(
    harness: Harness,
) -> None:
    """The prompt states 1600x1000 and the click bounds assume it, so the harness
    measures the signed-in page (in a world its script cannot reach) and records it,
    before the recorder starts and while the DevTools session is still open."""
    assert harness.main("--dry-run") == 0
    at = harness.calls.index
    assert at("sign_in") < at("page_facts") < at("verify_viewport")
    assert at("verify_viewport") < at("start_recorder")
    assert "measure_viewport" not in harness.calls
    assert harness.session.closed == 1
    assert harness.summary()["viewport"] == {"w": 1600, "h": 1000, "dpr": 1}


@pytest.mark.unit
def test_a_viewport_that_is_not_the_expected_one_fails_before_recording(
    harness: Harness,
) -> None:
    harness.viewport_error = chrome.ChromeError(
        "the page's viewport is 1600x857 at 1x, not 1600x1000 at 1x"
    )
    harness.forge.add_note(_review_body(HEAD))
    assert harness.main() == 1
    summary = harness.summary()
    assert summary["status"] == "failed"
    assert summary["detail"].startswith("the page's viewport is 1600x857")
    assert summary["viewport"] is None
    assert "start_recorder" not in harness.calls
    assert "run_agent" not in harness.calls
    assert harness.chrome_process.terminations == 1
    assert harness.session.closed == 1
    assert harness.forge.writes() == []


@pytest.mark.unit
def test_the_socket_audit_covers_chrome_from_its_launch_until_after_it_exits(
    harness: Harness,
) -> None:
    """The MR's login page holds the persona's password, so the audit covers it.

    The watch starts as Chrome is launched, before DevTools is even waited for,
    and its last pass runs after Chrome has been terminated. Chrome is terminated
    as soon as the recorder stops, so render and every write to GitLab run with no
    signed-in MR page alive, and the proxy is stopped before anything is posted.
    """
    harness.forge.add_note(_review_body(HEAD), thread="review-thread")
    assert harness.main() == 0
    calls = harness.calls
    at = calls.index
    assert at("launch_chrome") < at("socket_watch") < at("wait_for_cdp")
    assert at("socket_watch") < at("sign_in")
    assert at("stop_recorder") < at("chrome_terminated") < at("watch_stopped")
    assert at("chrome_terminated") < at("kill_strays") < at("watch_stopped")
    assert harness.swept == [(paths.BROWSER_UID, harness.chrome_process.pid)]
    assert at("watch_stopped") < at("render_video") < at("forge_write")
    assert at("proxy_terminated") < at("forge_write")
    assert calls.count("chrome_terminated") == calls.count("watch_stopped") == 1
    assert calls.count("proxy_terminated") == 1
    assert harness.summary()["socket_audit"]["state"] == "clean"


@pytest.mark.unit
@pytest.mark.parametrize("mode", [(), ("--dry-run",)], ids=["post", "dry-run"])
def test_a_browser_process_that_left_chromes_group_fails_the_run_in_every_mode(
    harness: Harness, mode: tuple[str, ...]
) -> None:
    """A process that called ``setsid`` is outside the group the harness kills, and
    once the audit stops nothing would watch it through render and the post."""
    harness.strays = ("pid 4180 (chrome_crashpad)",)
    harness.forge.add_note(_review_body(HEAD), thread="review-thread")
    assert harness.main(*mode) == 1
    summary = harness.summary()
    assert summary["status"] == "failed"
    assert "outlived Chrome's process group" in summary["detail"]
    assert "pid 4180 (chrome_crashpad)" in summary["detail"]
    assert summary["socket_audit"]["strays"] == ["pid 4180 (chrome_crashpad)"]
    assert harness.forge.writes() == []


@pytest.mark.unit
def test_without_a_uid_switch_there_is_no_uid_to_sweep_or_audit(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processes, "can_switch_user", lambda: False)
    assert harness.main("--dry-run") == 0
    summary = harness.summary()
    assert summary["browser_uid_switched"] is False
    assert harness.watch_uid is None
    assert harness.swept == [(None, harness.chrome_process.pid)]


def _refusal(target: str, reason: str = "host_not_allowed") -> dict[str, str]:
    return {
        "t": "2026-10-15T00:00:01Z",
        "method": "CONNECT",
        "target": target,
        "reason": reason,
    }


@pytest.mark.unit
def test_chromes_own_refusals_and_the_self_test_stay_out_of_the_notes_caveats(
    harness: Harness,
) -> None:
    """Chrome calls home by itself and the self-test is refused on purpose; neither
    says anything about the MR, so the note carries no refusal caveat for them,
    while refusals.jsonl and the summary still account for every one."""
    harness.refusals += [
        _refusal("accounts.google.com:443"),
        _refusal("http://redirector.gvt1.com/edgedl/chrome/", "plain_http_not_allowed"),
    ]
    assert harness.main("--dry-run") == 0
    caveats = next(
        line
        for line in harness.artifact("section.md").splitlines()
        if line.startswith("**Caveats:**")
    )
    assert "egress proxy refused" not in caveats
    egress_summary = harness.summary()["egress"]
    assert egress_summary["by_origin"] == {"page": 0, "browser": 2, "selftest": 1}
    listed = [
        json.loads(line) for line in harness.artifact("refusals.jsonl").splitlines()
    ]
    assert [record["origin"] for record in listed] == ["selftest", "browser", "browser"]
    assert [record["target"] for record in listed] == [
        record["target"] for record in harness.refusals
    ]


#: The refusal log of a run in the image under the committed flags and policy, in
#: its order: the self-tests, then the UI's login page for a minute with text typed
#: into a field. Every record is Chrome's own or the self-test's.
_COMMITTED_POLICY_REFUSALS = (
    ("CONNECT", "accounts.google.com:443", "host_not_allowed"),
    ("CONNECT", "accounts.google.com:443", "host_not_allowed"),
    ("CONNECT", "update.googleapis.com:443", "host_not_allowed"),
    ("GET", "http://ux-proxy-selftest.invalid/", "plain_http_not_allowed"),
    ("GET", "http://ux-proxy-selftest.invalid/favicon.ico", "plain_http_not_allowed"),
    ("CONNECT", "accounts.google.com:443", "host_not_allowed"),
    ("CONNECT", "redirector.gvt1.com:443", "host_not_allowed"),
    ("CONNECT", "android.clients.google.com:443", "host_not_allowed"),
    ("CONNECT", "accounts.google.com:443", "host_not_allowed"),
    ("CONNECT", "accounts.google.com:443", "host_not_allowed"),
    ("CONNECT", "android.clients.google.com:443", "host_not_allowed"),
    ("CONNECT", "accounts.google.com:443", "host_not_allowed"),
    ("CONNECT", "update.googleapis.com:443", "host_not_allowed"),
)


@pytest.mark.unit
def test_a_run_whose_page_was_refused_nothing_carries_no_refusal_caveat(
    harness: Harness,
) -> None:
    """Replays a refusal log measured in the image under the committed policy. All
    of it is Chrome's own background traffic or the self-test, and no screen needed
    any of it, so the note carries no refusal caveat for that log while the
    artifact still lists every record."""
    harness.refusals = [
        {"t": "2026-10-20T00:00:00Z", "method": method, "target": target, "reason": why}
        for method, target, why in _COMMITTED_POLICY_REFUSALS
    ]
    assert harness.main("--dry-run") == 0
    caveats = next(
        line
        for line in harness.artifact("section.md").splitlines()
        if line.startswith("**Caveats:**")
    )
    assert "egress proxy refused" not in caveats
    assert "look broken" not in caveats
    egress_summary = harness.summary()["egress"]
    assert egress_summary["by_origin"] == {"page": 0, "browser": 11, "selftest": 2}
    listed = [
        json.loads(line) for line in harness.artifact("refusals.jsonl").splitlines()
    ]
    assert len(listed) == len(_COMMITTED_POLICY_REFUSALS)
    assert {record["origin"] for record in listed} == {"browser", "selftest"}


@pytest.mark.unit
def test_a_pages_own_request_to_a_stopped_background_host_is_a_caveat(
    harness: Harness,
) -> None:
    """The managed policy stops Chrome calling www.google.com by itself, so a refusal
    of it is the page's, a reCAPTCHA or an embedded map for instance, and the note
    says a screen can look broken. A Google sign-in button from accounts.google.com
    still reads as Chrome's, because Chrome calls that host too; refusals.jsonl is
    where that one shows."""
    harness.refusals += [
        _refusal("accounts.google.com:443"),
        _refusal("www.google.com:443"),
        _refusal("www.google.com:443"),
    ]
    assert harness.main("--dry-run") == 0
    caveats = next(
        line
        for line in harness.artifact("section.md").splitlines()
        if line.startswith("**Caveats:**")
    ).replace(section.ZERO_WIDTH_SPACE, "")
    assert (
        "the egress proxy refused 2 of the page's requests (`host_not_allowed` ×2)"
        in caveats
    )
    assert "www.google.com" not in caveats
    egress_summary = harness.summary()["egress"]
    assert egress_summary["by_origin"] == {"page": 2, "browser": 1, "selftest": 1}
    origins = {
        record["target"]: record["origin"]
        for record in map(json.loads, harness.artifact("refusals.jsonl").splitlines())
    }
    assert origins["www.google.com:443"] == "page"
    assert origins["accounts.google.com:443"] == "browser"


@pytest.mark.unit
def test_a_refusal_the_page_caused_is_a_caveat_of_the_note(harness: Harness) -> None:
    harness.refusals += [
        _refusal("accounts.google.com:443"),
        _refusal("thirdparty.example:443"),
        _refusal("cdn.thirdparty.example:443"),
    ]
    harness.forge.add_note(_review_body(HEAD), thread="review-thread")
    assert harness.main() == 0
    put_body = harness.forge.writes()[1][2]["body"]
    for text in (harness.artifact("section.md"), put_body):
        caveats = next(
            line for line in text.splitlines() if line.startswith("**Caveats:**")
        ).replace(section.ZERO_WIDTH_SPACE, "")
        assert "the egress proxy refused 2 of the page's requests" in caveats
        assert "thirdparty.example" not in caveats
    assert harness.summary()["egress"]["by_origin"]["page"] == 2


@pytest.mark.unit
def test_a_dry_run_writes_every_artifact_and_makes_no_forge_write(
    harness: Harness,
) -> None:
    assert harness.main("--dry-run") == 0
    for name in ("section.md", "summary.json", "report.md", "refusals.jsonl"):
        assert (harness.artifacts / name).is_file(), name
    mp4 = harness.artifacts / f"ux-test-mr{IID}-{HEAD[:8]}.mp4"
    assert mp4.stat().st_size == harness.video_size
    assert harness.forge.writes() == []
    text = harness.artifact("section.md")
    assert f"![UX test]({mp4.name})" in text.splitlines()
    assert section.has_section(text, HEAD)
    assert "**Chapters:** 0:00 Open the viewer · 0:31 Rotate page 2" in text
    summary = harness.summary()
    assert summary["status"] == "dry_run"
    assert summary["recording"]["video_attached"] is True
    assert summary["recording"]["marks"] == 3
    assert summary["recording"]["clicks_rejected"] == 1
    assert summary["recording"]["cdp_messages_dropped"] == 2
    assert summary["viewport"] == {"w": 1600, "h": 1000, "dpr": 1}
    assert summary["stack"]["version"] == "0.6.12"
    assert summary["socket_audit"]["state"] == "clean"
    assert summary["egress"]["served"] == 212
    assert summary["browser_uid_switched"] is True
    assert summary["toolchain"]["node"] == "v22.14.0"
    assert summary["model"]["stop"] == "end_turn"
    assert summary["flows"][0] == "change"


#: A claim that the review stack's backend is ``develop``'s, in the shapes it has
#: been written in: "`develop`'s backend", "the backend behind it is `develop`" and
#: "backend is `develop` as of the last nightly update". The stack runs whatever it
#: was last deployed with, which the note and the prompt state as the UI reports it.
_DEVELOP_BACKEND_RE = re.compile(
    r"`?\bdevelop\b`?'s backend|\bbackend\b[^.\n]{0,40}\bdevelop\b", re.IGNORECASE
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("version", "build", "backend"),
    [
        ("0.6.12", "2026-10-16", "backend: `idp-ux-nightly` v0.6.12, build 2026-10-16"),
        (
            None,
            None,
            "backend: `idp-ux-nightly`; its version and build could not be read from "
            "the UI",
        ),
    ],
)
def test_the_note_and_the_prompt_name_the_backend_the_stack_reported(
    harness: Harness, version: str | None, build: str | None, backend: str
) -> None:
    """The backend caveat is what the signed-in sidebar showed, read once the
    page is signed in, and says so when it showed nothing: the review stack's
    version and build, never an assumption about where it was last updated from.
    The prompt carries both skill files in full, and the model repeats their
    "Bounds worth stating" list in its report, so no part of it may say otherwise
    either."""
    harness.facts = signin.PageFacts(
        "idp-ux-nightly", version, build, (1600, 1000, 1.0), None
    )
    assert harness.main("--dry-run") == 0
    note = harness.artifact("section.md")
    caveats = next(
        line for line in note.splitlines() if line.startswith("**Caveats:**")
    ).replace(section.ZERO_WIDTH_SPACE, "")
    assert caveats.startswith(f"**Caveats:** {backend}")
    assert "nightly update" not in caveats
    prompt = harness.agent_args[0]
    assert backend in prompt
    for text, where in ((prompt, "the prompt"), (note, "the note")):
        claim = _DEVELOP_BACKEND_RE.search(text)
        assert claim is None, f"{where} says the backend is develop's: {claim[0]!r}"
    stack = harness.summary()["stack"]
    assert (stack["version"], stack["build"]) == (version, build)


@pytest.mark.unit
def test_a_post_appends_to_the_review_replies_once_and_supersedes_older_videos(
    harness: Harness,
) -> None:
    forge = harness.forge
    old = forge.add_note(_old_section_body(OLDER))
    stranger = forge.add_note(_old_section_body(OLDER), author=SOMEONE_ELSE)
    target = forge.add_note(_review_body(HEAD), thread="review-thread")
    assert harness.main() == 0

    writes = forge.writes()
    project = "/projects/group%2Fproj"
    mr = f"{project}/merge_requests/{IID}"
    assert [(method, path) for method, path, _ in writes] == [
        ("POST", f"{project}/uploads"),
        ("PUT", f"{mr}/notes/{target}"),
        ("POST", f"{mr}/discussions/review-thread/notes"),
        ("PUT", f"{mr}/notes/{old}"),
    ]
    put_body = writes[1][2]["body"]
    expected_head = review.defuse_quick_actions(_review_body(HEAD).rstrip("\n"))
    assert put_body.startswith(expected_head + "\n\n---\n")
    video = f"![UX test](/uploads/{UPLOAD_HASH}/ux-test-mr{IID}-{HEAD[:8]}.mp4)"
    assert video in put_body.splitlines()
    marker = section.UX_MARKER_RE.search(put_body)
    assert marker is not None
    assert (marker["sha"], marker["upload"]) == (HEAD, str(UPLOAD_ID))
    assert writes[2][2]["body"].startswith(
        f"🖱️ UX test of `{HEAD[:8]}` added to the review above: 1 flow(s)"
    )
    superseded = forge.store[old]["body"]
    assert f"_Video superseded by `{HEAD[:8]}`._" in superseded
    assert "/uploads/" + "f" * 32 not in superseded
    assert forge.store[stranger]["body"] == _old_section_body(OLDER)
    for _, _, sent in writes[1:]:
        assert _defused(sent["body"])
    summary = harness.summary()
    assert summary["status"] == "posted"
    assert summary["note"] == {
        "action": "appended",
        "note_id": target,
        "upload_id": UPLOAD_ID,
        "upload_error": None,
        "superseded": [old],
    }
    assert summary["recording"]["video_attached"] is True
    assert harness.artifact("section.md") in put_body


@pytest.mark.unit
def test_no_review_note_within_the_window_posts_a_standalone_note(
    harness: Harness,
) -> None:
    """Twenty 30 s sleeps fill the 10-minute window, bounded by the fake clock.

    The notes are listed once by the precheck and 21 times by the poll.
    """
    assert harness.main() == 0
    assert harness.clock.sleeps == [30.0] * 20
    assert harness.forge.listings == 1 + 21
    method, path, sent = harness.forge.writes()[-1]
    assert (method, path) == (
        "POST",
        f"/projects/group%2Fproj/merge_requests/{IID}/notes",
    )
    assert sent["body"].startswith(section.STANDALONE_PREFIX + "\n\n---\n")
    assert "ai-review:" not in sent["body"]
    assert harness.summary()["note"]["action"] == "standalone"


@pytest.mark.unit
def test_a_review_note_that_appears_while_polling_is_appended_to(
    harness: Harness,
) -> None:
    """The precheck's listing is the first; the note is there on the poll's third."""

    def appear(forge: FakeGitlab) -> None:
        if forge.listings == 4:
            forge.add_note(_review_body(HEAD), thread="late")

    harness.forge.on_listing = appear
    assert harness.main() == 0
    assert harness.clock.sleeps == [30.0] * 2
    assert harness.summary()["note"]["action"] == "appended"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error", "url", "reason"),
    [
        (
            review.Skip(
                "GitLab refused POST /projects/group%2Fproj/uploads (403): the "
                "token's role cannot do this, or it lacks api scope"
            ),
            None,
            "(403)",
        ),
        (
            review.Failure(
                "GitLab returned 413 for POST /projects/group%2Fproj/uploads: "
                "file is too large"
            ),
            None,
            "413",
        ),
        (None, "https://elsewhere.example/x.mp4", "no /uploads/<secret>/<file> URL"),
    ],
    ids=["refused", "failed", "unusable-url"],
)
def test_an_upload_gitlab_does_not_take_still_posts_the_findings_without_the_video(
    harness: Harness,
    capsys: pytest.CaptureFixture[str],
    error: Exception | None,
    url: str | None,
    reason: str,
) -> None:
    """The session has run and been paid for, so only the video is lost: the
    section is appended without it, saying where the mp4 is, and the reply is
    posted as usual."""
    harness.forge.upload_error = error
    harness.forge.upload_url = url
    target = harness.forge.add_note(_review_body(HEAD), thread="review-thread")
    assert harness.main() == 0

    writes = harness.forge.writes()
    mr = f"/projects/group%2Fproj/merge_requests/{IID}"
    assert [(method, path) for method, path, _ in writes] == [
        ("POST", "/projects/group%2Fproj/uploads"),
        ("PUT", f"{mr}/notes/{target}"),
        ("POST", f"{mr}/discussions/review-thread/notes"),
    ]
    put_body = writes[1][2]["body"]
    assert "_The video could not be uploaded; it is in the job artifacts._" in (
        put_body.splitlines()
    )
    assert "![UX test](" not in put_body
    assert "**Chapters:**" not in put_body
    marker = section.UX_MARKER_RE.search(put_body)
    assert marker is not None
    assert (marker["sha"], marker["upload"]) == (HEAD, "none")
    assert "Rotate button has no tooltip" in put_body
    summary = harness.summary()
    assert summary["status"] == "posted"
    assert summary["detail"].endswith(
        "without its video, which is in the job artifacts"
    )
    assert summary["note"]["action"] == "appended"
    assert summary["note"]["upload_id"] is None
    assert reason in summary["note"]["upload_error"]
    assert summary["recording"]["video_attached"] is False
    assert harness.artifact("section.md") in put_body
    assert (harness.artifacts / f"ux-test-mr{IID}-{HEAD[:8]}.mp4").is_file()
    assert "upload: the video was not uploaded: " in capsys.readouterr().out


@pytest.mark.unit
def test_a_head_that_moves_during_the_session_posts_nothing(harness: Harness) -> None:
    def push() -> None:
        harness.forge.head = NEWER

    harness.agent_effect = push
    harness.forge.add_note(_review_body(HEAD))
    assert harness.main() == 0
    summary = harness.summary()
    assert summary["status"] == "superseded"
    assert NEWER[:8] in summary["detail"]
    assert harness.forge.writes() == []


@pytest.mark.unit
def test_a_socket_audit_violation_posts_nothing_and_fails(harness: Harness) -> None:
    harness.audit_state = "violations"
    harness.audit_violations = ("tcp 93.184.216.34:443",)
    harness.forge.add_note(_review_body(HEAD))
    assert harness.main() == 1
    summary = harness.summary()
    assert summary["status"] == "failed"
    assert summary["detail"].startswith("the browser had a non-loopback socket")
    assert summary["socket_audit"]["state"] == "violations"
    assert harness.forge.writes() == []


@pytest.mark.unit
def test_an_unavailable_socket_audit_posts_nothing(harness: Harness) -> None:
    harness.audit_state = "unavailable"
    harness.forge.add_note(_review_body(HEAD))
    assert harness.main() == 1
    detail = harness.summary()["detail"]
    assert "socket audit was unavailable" in detail
    assert "running as root" in detail and "saw that uid's sockets" in detail
    assert harness.forge.writes() == []


@pytest.mark.unit
def test_a_session_without_a_json_report_fails_and_posts_nothing(
    harness: Harness,
) -> None:
    harness.agent_text = "Looked at a few screens; everything seemed fine."
    harness.forge.add_note(_review_body(HEAD))
    assert harness.main() == 1
    summary = harness.summary()
    assert summary["detail"] == "the session did not end with a JSON report"
    assert harness.forge.writes() == []
    assert "everything seemed fine" in harness.artifact("report.md")


@pytest.mark.unit
def test_no_visible_change_renders_nothing_and_says_so(harness: Harness) -> None:
    harness.agent_text = _report(visible=False)
    harness.forge.add_note(_review_body(HEAD))
    assert harness.main() == 0
    assert "render_video" not in harness.calls
    writes = harness.forge.writes()
    assert not any(path.endswith("/uploads") for _, path, _ in writes)
    assert (
        "_No user-visible change found, so no video is attached._"
        in (writes[0][2]["body"])
    )
    assert harness.summary()["recording"]["video_attached"] is False


@pytest.mark.unit
def test_a_render_that_wrote_no_video_fails_and_posts_nothing(harness: Harness) -> None:
    harness.video_size = -1
    harness.forge.add_note(_review_body(HEAD))
    assert harness.main() == 1
    assert harness.summary()["detail"] == "render finished without writing a video"
    assert harness.forge.writes() == []


ROOT_ONLY = "root-only-value-41c9"
SEGMENTS = json.dumps(
    {"segments": [{"kind": "footage", "label": "Open the viewer", "out_start": 4.2}]}
)


def _write_segments(session: Path) -> None:
    (session / recording.SEGMENTS_FILE).write_text(SEGMENTS, encoding="utf-8")


def _root_only_file(tmp_path: Path) -> Path:
    """A file outside the session that a usable segment list could be read from."""
    outside = tmp_path / "root-only.json"
    segment = {"kind": "footage", "label": ROOT_ONLY, "out_start": 0.0}
    outside.write_text(json.dumps({"segments": [segment]}), encoding="utf-8")
    return outside


def _release_when_stuck(fifo: Path, done: threading.Event, released: list[str]) -> None:
    """Open ``fifo`` for writing if ``done`` is still unset ten seconds on.

    A read-side open of a FIFO without ``O_NONBLOCK`` waits for a writer, so this
    turns a read-back that blocks on the planted FIFO into a failing test instead
    of a hung suite: each release is recorded in ``released``. A read-back that
    does not block sets nothing off here.
    """

    def watch() -> None:
        if done.wait(10):
            return
        while not done.wait(0.1):
            with contextlib.suppress(OSError):
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
                released.append(fifo.name)
                return

    threading.Thread(target=watch, daemon=True).start()


_READ_BACK_BOUNDS = {
    recording.VIDEO_FILE: "MAX_VIDEO_BYTES",
    recording.SEGMENTS_FILE: "MAX_SEGMENTS_BYTES",
}


@pytest.mark.unit
@pytest.mark.parametrize("name", [recording.VIDEO_FILE, recording.SEGMENTS_FILE])
@pytest.mark.parametrize("plant", ["symlink", "fifo", "oversized"])
def test_a_render_output_that_is_not_its_own_regular_file_fails_the_run(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, name: str, plant: str
) -> None:
    """The session directory is render's when it ends, so render chooses what each
    name there is, and the process reading them back is root. Nothing outside the
    directory reaches an artifact, and nothing is posted."""
    outside = _root_only_file(harness.tmp)
    done = threading.Event()
    released: list[str] = []
    size = (
        harness.video_size
        if name == recording.VIDEO_FILE
        else len(SEGMENTS.encode("utf-8"))
    )

    def plant_it(session: Path) -> None:
        _write_segments(session)
        if plant == "oversized":
            return
        (session / name).unlink()
        if plant == "symlink":
            (session / name).symlink_to(outside)
        else:
            os.mkfifo(session / name)
            _release_when_stuck(session / name, done, released)

    if plant == "oversized":
        monkeypatch.setattr(recording, _READ_BACK_BOUNDS[name], size - 1)
    harness.real_chapters = True
    harness.render_effect = plant_it
    harness.forge.add_note(_review_body(HEAD))
    try:
        assert harness.main() == 1
    finally:
        done.set()
    assert released == [], "the read-back waited for a writer on the planted FIFO"
    why = {
        "symlink": "it is a symbolic link, which is never followed",
        "fifo": "it is not a regular file",
        "oversized": f"it is {size} bytes, over its bound of {size - 1}",
    }[plant]
    summary = harness.summary()
    assert (summary["status"], summary["detail"]) == (
        "failed",
        f"render's {name} is refused: {why}",
    )
    assert harness.forge.writes() == []
    assert not (harness.artifacts / "section.md").exists()
    mp4 = harness.artifacts / f"ux-test-mr{IID}-{HEAD[:8]}.mp4"
    assert mp4.exists() is (name != recording.VIDEO_FILE)
    for artifact in harness.artifacts.iterdir():
        assert ROOT_ONLY.encode() not in artifact.read_bytes(), artifact.name


@pytest.mark.unit
@pytest.mark.parametrize("plant", ["symlink", "fifo", "oversized"])
def test_a_refusal_log_that_is_not_the_proxys_own_file_fails_the_run(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    plant: str,
) -> None:
    """The proxy's directory is uxproxy's, so the proxy chooses what is under the
    refusal log's name, and the process reading it back before anything is
    rendered or posted is root. Nothing outside the directory reaches an artifact,
    and nothing is posted."""
    outside = harness.tmp / "root-only.jsonl"
    outside.write_text(json.dumps({"target": ROOT_ONLY}) + "\n", encoding="utf-8")
    done = threading.Event()
    released: list[str] = []
    launch = harness._launch_proxy

    def planted(**kwargs: Any) -> egress.ProxyRun:
        run = launch(**kwargs)
        log = run.refusal_log
        if plant == "oversized":
            with log.open("r+b") as handle:
                handle.truncate(egress.MAX_REFUSAL_LOG_BYTES + 1)
            return run
        log.unlink()
        if plant == "symlink":
            log.symlink_to(outside)
        else:
            os.mkfifo(log)
            _release_when_stuck(log, done, released)
        return run

    monkeypatch.setattr(harness, "_launch_proxy", planted)
    harness.forge.add_note(_review_body(HEAD))
    try:
        assert harness.main() == 1
    finally:
        done.set()
    assert released == [], "the read-back waited for a writer on the planted FIFO"
    limit = egress.MAX_REFUSAL_LOG_BYTES
    why = {
        "symlink": "it is a symbolic link, which is never followed",
        "fifo": "it is not a regular file",
        "oversized": f"it is {limit + 1} bytes, over its bound of {limit}",
    }[plant]
    refused = f"the proxy's refusals.jsonl is refused: {why}"
    summary = harness.summary()
    assert (summary["status"], summary["detail"]) == ("failed", refused)
    assert "by_origin" not in summary["egress"]
    assert summary["egress"]["served"] == 212
    assert harness.forge.writes() == []
    assert f"egress: not copied: {refused}" in capsys.readouterr().out
    assert not (harness.artifacts / "refusals.jsonl").exists()
    for artifact in harness.artifacts.iterdir():
        assert ROOT_ONLY.encode() not in artifact.read_bytes(), artifact.name


@pytest.mark.unit
def test_stats_the_proxy_left_as_a_link_are_left_out_rather_than_read_through_it(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The stats are read for ``summary.json`` once the run is decided, after the
    proxy has written them on its way out, so a link it leaves there instead is
    refused: the stats are left out and a line says why, and the refusal log is
    copied all the same, because the note's caveat names ``refusals.jsonl``."""
    outside = harness.tmp / "root-only-stats.json"
    outside.write_text(json.dumps({"served": ROOT_ONLY}), encoding="utf-8")
    launch = harness._launch_proxy

    def planted(**kwargs: Any) -> egress.ProxyRun:
        run = launch(**kwargs)
        process = harness.proxy_process
        assert process is not None
        write_stats = process._on_terminate

        def write_then_plant() -> None:
            if write_stats is not None:
                write_stats()
            run.stats_file.unlink()
            run.stats_file.symlink_to(outside)

        process._on_terminate = write_then_plant
        return run

    monkeypatch.setattr(harness, "_launch_proxy", planted)
    harness.refusals.append(_refusal("thirdparty.example:443"))
    harness.forge.add_note(_review_body(HEAD))
    assert harness.main() == 0
    out = capsys.readouterr().out
    assert (
        "egress: not copied: the proxy's stats.json is refused: it is a symbolic "
        "link, which is never followed"
    ) in out
    assert "`refusals.jsonl` in the job's artifacts lists them" in (
        harness.artifact("section.md").replace(section.ZERO_WIDTH_SPACE, "")
    )
    summary = harness.summary()
    assert set(summary["egress"]) == {"by_origin"}
    assert summary["egress"]["by_origin"]["page"] == 1
    listed = harness.artifact("refusals.jsonl").splitlines()
    assert len(listed) == len(harness.refusals)
    for artifact in harness.artifacts.iterdir():
        assert ROOT_ONLY.encode() not in artifact.read_bytes(), artifact.name


@pytest.mark.unit
def test_the_video_is_read_back_only_as_the_render_users_own_file(
    harness: Harness,
) -> None:
    """A hard link render made to another uid's file is that uid's file."""
    mine = os.geteuid()
    harness.render_owner = mine + 1
    harness.forge.add_note(_review_body(HEAD))
    assert harness.main() == 1
    assert harness.render_uid_asked == [paths.RENDER_USER]
    assert harness.summary()["detail"] == (
        f"render's {recording.VIDEO_FILE} is refused: it belongs to uid {mine}, "
        f"not to render's uid {mine + 1}"
    )
    assert not (harness.artifacts / f"ux-test-mr{IID}-{HEAD[:8]}.mp4").exists()
    assert harness.forge.writes() == []


@pytest.mark.unit
def test_the_chapters_shown_are_the_ones_render_wrote_read_back_as_its_own(
    harness: Harness,
) -> None:
    harness.real_chapters = True
    harness.render_effect = _write_segments
    assert harness.main("--dry-run") == 0
    assert "**Chapters:** 0:04 Open the viewer" in harness.artifact("section.md")
    assert harness.render_uid_asked == [paths.RENDER_USER]


@pytest.mark.unit
def test_a_session_that_marked_nothing_renders_nothing(harness: Harness) -> None:
    harness.status = {"ok": True, "marks": 0, "clicks_rejected": 0}
    assert harness.main("--dry-run") == 0
    assert "render_video" not in harness.calls
    assert "_The session marked no chapters" in harness.artifact("section.md")


@pytest.mark.unit
def test_an_agent_timeout_fails_and_still_stops_chrome_and_the_proxy(
    harness: Harness,
) -> None:
    harness.timed_out = True
    assert harness.main() == 1
    summary = harness.summary()
    assert summary["detail"] == "the UX session timed out after 25 min"
    assert summary["model"]["stop"] == "timeout"
    assert harness.chrome_process.terminations == 1
    assert harness.proxy_process is not None
    assert harness.proxy_process.terminations == 1
    assert "stop_recorder" in harness.calls


@pytest.mark.unit
def test_a_cancel_during_the_session_runs_every_cleanup_and_exits_1(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    def cancel() -> None:
        raise processes.Terminated("received signal 15")

    harness.agent_effect = cancel
    assert harness.main() == 1
    assert "FAILED: cancelled" in capsys.readouterr().out.splitlines()
    assert "kill_recorder" in harness.calls
    assert harness.chrome_process.terminations == 1
    assert harness.proxy_process is not None
    assert harness.proxy_process.terminations == 1
    assert harness.watch is not None and harness.watch.stopped == 1
    assert harness.run_dir is not None and not harness.run_dir.exists()
    assert (harness.artifacts / "refusals.jsonl").is_file()
    summary = harness.summary()
    assert (summary["status"], summary["detail"]) == ("failed", "cancelled")
    assert summary["egress"]["refused"] == 1


@pytest.mark.unit
def test_a_cleanup_step_that_fails_is_named_and_every_later_step_still_runs(
    harness: Harness,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line names a step by its method's name, so the four browser steps keep
    their names although ``session.ConfinedBrowser`` does their work."""

    def refused(*args: Any, **kwargs: Any) -> None:
        harness.calls.append("sign_in")
        raise signin.SignInError("the sign-in was refused")

    def fails(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("gone")

    def no_proc(uid: int | None, group: int | None) -> tuple[str, ...]:
        raise OSError("no /proc")

    launch = harness._launch_proxy

    def launched(**kwargs: Any) -> egress.ProxyRun:
        run = launch(**kwargs)
        monkeypatch.setattr(run.process, "terminate", fails)
        return run

    monkeypatch.setattr(harness, "_sign_in", refused)
    monkeypatch.setattr(harness, "_kill_strays", no_proc)
    monkeypatch.setattr(harness, "_launch_proxy", launched)
    monkeypatch.setattr(harness.session, "close", fails)
    monkeypatch.setattr(FakeWatch, "stop", fails)
    harness.forge.add_note(_review_body(HEAD))
    assert harness.main() == 1
    lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("cleanup: ")
    ]
    assert lines == [
        "cleanup: close_session: RuntimeError",
        "cleanup: stop_chrome: OSError",
        "cleanup: stop_watch: RuntimeError",
        "cleanup: stop_proxy: RuntimeError",
    ]
    assert harness.chrome_process.terminations == 1
    assert harness.run_dir is not None and not harness.run_dir.exists()
    assert (harness.artifacts / "refusals.jsonl").is_file()
    summary = harness.summary()
    assert (summary["status"], summary["detail"]) == (
        "failed",
        "the sign-in was refused",
    )
    assert harness.forge.writes() == []


@pytest.mark.unit
def test_a_socket_audit_that_never_started_fails_the_run_and_posts_nothing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step of the browser's lifecycle that cannot be done is a step failure
    with its own message, raised only after Chrome has been stopped."""
    monkeypatch.setattr(FakeWatch, "start", lambda self: None)
    harness.forge.add_note(_review_body(HEAD))
    assert harness.main() == 1
    summary = harness.summary()
    assert (summary["status"], summary["detail"]) == (
        "failed",
        "the socket audit was never started",
    )
    assert issubclass(session.SessionError, ai_ux_test._STEP_FAILURES)
    assert harness.chrome_process.terminations == 1
    assert harness.proxy_process is not None
    assert harness.proxy_process.terminations == 1
    assert harness.forge.writes() == []


@pytest.mark.unit
def test_a_session_that_exits_without_a_result_fails_with_bounded_redacted_stderr(
    harness: Harness,
) -> None:
    noise = "x" * 600 + f" signed in as {PERSONA_USER} with {PERSONA_CREDENTIAL}"
    harness.crashed = processes.Completed(1, "not json", noise, False, 5.0)
    assert harness.main() == 1
    detail = harness.summary()["detail"]
    assert detail.startswith("the UX session failed: error, exit 1: ")
    assert len(detail) <= len("the UX session failed: error, exit 1: ") + 400
    assert PERSONA_USER not in detail and PERSONA_CREDENTIAL not in detail
    assert harness.forge.writes() == []


@pytest.mark.unit
def test_a_recorder_that_stopped_answering_is_counted_from_its_timeline(
    harness: Harness,
) -> None:
    harness.status = None
    assert harness.main("--dry-run") == 0
    summary = harness.summary()
    assert summary["recording"]["marks"] == 2
    assert summary["recording"]["clicks_rejected"] == 0
    assert summary["recording"]["cdp_messages_dropped"] == 0
    assert "render_video" in harness.calls


@pytest.mark.unit
def test_a_section_of_mine_that_appears_while_polling_leaves_the_run_unchanged(
    harness: Harness,
) -> None:
    def appear(forge: FakeGitlab) -> None:
        if forge.listings == 3:
            forge.add_note(f"<!-- ux-test: sha={HEAD} rev=1 upload=none -->")

    harness.forge.on_listing = appear
    assert harness.main() == 0
    summary = harness.summary()
    assert (summary["status"], summary["note"]["action"]) == ("unchanged", "unchanged")
    assert harness.forge.writes() == []


@pytest.mark.unit
def test_an_oversized_video_is_not_uploaded_and_the_note_says_so(
    harness: Harness,
) -> None:
    harness.video_size = 3 * 1024 * 1024 // 2
    harness.forge.add_note(_review_body(HEAD))
    assert harness.main("--max-upload-mb", "1") == 0
    writes = harness.forge.writes()
    assert not any(path.endswith("/uploads") for _, path, _ in writes)
    body = writes[0][2]["body"]
    assert (
        "_The video (2 MB) is over the upload limit; it is in the job artifacts._"
        in (body)
    )
    assert "**Chapters:**" not in body
    assert (harness.artifacts / f"ux-test-mr{IID}-{HEAD[:8]}.mp4").is_file()


@pytest.mark.unit
def test_no_secret_reaches_any_artifact_note_or_printed_line(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """Driven as CI drives it: the precheck, then the full run on its inputs.json.

    Every file the job leaves in its artifacts, both bodies sent to GitLab, both
    output streams of both runs, and what render is given to speak and caption,
    are read for the persona's values, the stack's API host, the account id, the
    token and the GitLab instance's host, which names an internal network.
    """
    secrets = (
        PERSONA_USER,
        PERSONA_CREDENTIAL,
        API_HOST,
        ACCOUNT,
        CREDENTIAL,
        FORGE_HOST,
    )
    escaped_credential = "Ux\\u00213cretive-persona-9aZ"
    escaped_user = "ux-persona-admin\\u0040example.invalid"
    leaky = " ".join(secrets)
    report = _report(
        findings=(f"The header shows {leaky}",),
        quote=f"Type {PERSONA_CREDENTIAL} into the search box",
        prose=f"Saw {leaky} on the page.",
    )
    harness.agent_text = report.replace(
        '"text": "The header shows',
        f'"text": "{escaped_credential} {escaped_user} The header shows',
    )
    assert escaped_credential in harness.agent_text
    harness.environ["CI_API_V4_URL"] = f"https://{FORGE_HOST}/api/v4"
    harness.environ["CI_SERVER_HOST"] = FORGE_HOST
    harness.forge.web_url = f"https://{FORGE_HOST}/group/proj/-/merge_requests/{IID}"
    harness.forge.title = f"Show {PERSONA_USER} on {API_HOST}"
    harness.mark_say = f"Signed in as {PERSONA_USER} on {API_HOST}, {ACCOUNT}."
    harness.chapter_list = [("0:00", f"Signed in as {PERSONA_USER} at {API_HOST}")]
    harness.refusals.append(
        {
            "t": "now",
            "method": "CONNECT",
            "target": f"{API_HOST}:443",
            "reason": ACCOUNT,
        }
    )
    harness.forge.add_note(_review_body(HEAD), thread="review-thread")
    assert harness.main("--precheck") == 0
    inputs = harness.artifacts / "inputs.json"
    assert harness.main("--inputs", str(inputs)) == 0

    names = (
        "section.md",
        "report.md",
        "summary.json",
        "refusals.jsonl",
        "inputs.json",
        "precheck.json",
    )
    texts = {name: harness.artifact(name) for name in names}
    sent = [w[2]["body"] for w in harness.forge.writes() if isinstance(w[2], dict)]
    texts["put"], texts["reply"] = sent[0], sent[1]
    printed = capsys.readouterr()
    texts["stdout"], texts["stderr"] = printed.out, printed.err
    texts["narration"] = " ".join([*harness.spoken.values(), *harness.rendered_labels])
    for name, text in texts.items():
        visible = text.replace(section.ZERO_WIDTH_SPACE, "")
        for secret in (*secrets, escaped_credential, escaped_user):
            assert secret not in visible, f"{name} carries {secret!r}"
    shown = {
        name: text.replace(section.ZERO_WIDTH_SPACE, "") for name, text in texts.items()
    }
    for name in ("section.md", "report.md", "inputs.json", "narration"):
        assert "[redacted]" in shown[name], name
    assert json.loads(texts["inputs.json"])["web_url"] == (
        f"https://[redacted]/group/proj/-/merge_requests/{IID}"
    )
    assert f"{chrome.SELFTEST_HOST}" in texts["refusals.jsonl"]


def _own_lines(body: str) -> list[str]:
    """The lines this job wrote in a note body, less its marker and video line."""
    lines = body.splitlines()
    marker = next(
        (i for i, line in enumerate(lines) if section.UX_MARKER_RE.match(line)), None
    )
    own = lines if marker is None else lines[marker + 1 :]
    return [line for line in own if not section.VIDEO_LINE_RE.fullmatch(line)]


#: A field that only reads as ``Bearer <token>`` once ``sanitize`` has turned its
#: bidi mark into a space, with the ``(`` of a link right after the token.
SPACED_BEARER = "Bearer\u200eAbCdEfGhIjKlMnOpQr(//e.example/x)"
NUL_BEARER = "Bearer\x00AbCdEfGhIjKlMnOpQr(http:e.example/login)"
#: Thirteen digits, which no shape matches, cut by ``sanitize`` at the finding's
#: bound to the twelve the account shape matches, so only the pass after
#: sanitising redacts them and writes the brackets of ``[account]``.
CUT_TO_AN_ACCOUNT = "a" * (section.TEXT_CHARS - 13) + "9876543210987" + "b"


@pytest.mark.unit
def test_no_note_body_holds_markup_whatever_the_model_or_a_chapter_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every smuggled spelling the section tests know, as a finding and as a
    chapter label, through the whole run: the section is redacted once more after
    sanitising, and a redaction writes brackets, so each line of every body sent
    to GitLab is read as GitLab would read it."""
    from test_ux_runner_section import SMUGGLED, _acted_on

    cases = (*SMUGGLED, SPACED_BEARER, NUL_BEARER, CUT_TO_AN_ACCOUNT)
    for index, raw in enumerate(cases):
        case = tmp_path / f"case-{index}"
        case.mkdir()
        run = Harness(case, monkeypatch)
        run.agent_text = _report(findings=(raw, SPACED_BEARER))
        run.chapter_list = [("0:00", raw), ("0:31", NUL_BEARER)]
        run.forge.add_note(_review_body(HEAD), thread="review-thread")
        assert run.main() == 0, raw
        bodies = [w[2]["body"] for w in run.forge.writes() if isinstance(w[2], dict)]
        assert len(bodies) == 2, raw
        assert any(line.startswith("**Chapters:** ") for line in bodies[0].split("\n"))
        for body in bodies:
            for line in _own_lines(body):
                assert _acted_on(line) == [], (raw, line)
        assert "AbCdEfGhIjKlMnOpQr" not in bodies[0], raw


@pytest.mark.unit
def test_a_token_sanitising_would_reveal_is_redacted_before_the_field_is_cut(
    harness: Harness,
) -> None:
    """Redacted only after sanitising, the token would be cut first, to fewer
    characters than the bearer shape needs, and that much of it posted."""
    token = "T0kenValue" * 4
    lead = "y" * (section.TEXT_CHARS - 18) + " "
    harness.agent_text = _report(findings=(f"{lead}Bearer\u200e{token}",))
    harness.forge.add_note(_review_body(HEAD), thread="review-thread")
    assert harness.main() == 0
    texts = [w[2]["body"] for w in harness.forge.writes() if isinstance(w[2], dict)]
    texts += [harness.artifact("section.md"), harness.artifact("report.md")]
    for text in texts:
        assert token[:6] not in text, text[-120:]
    assert "Bearer [token]" in harness.artifact("report.md")


@pytest.mark.unit
@pytest.mark.parametrize(
    "mode", [("--precheck",), ("--dry-run",)], ids=["precheck", "run"]
)
def test_the_run_makes_everything_under_its_own_umask_and_puts_the_callers_back(
    harness: Harness, mode: tuple[str, ...]
) -> None:
    """A GitLab Docker executor runs a job at umask 0000, under which the artifact
    directory, the recorder's session and its control socket would be writable by
    the browser's uid; every child inherits the umask, so it is set first."""
    seen: list[int] = []

    def measure() -> None:
        current = os.umask(0)
        os.umask(current)
        seen.append(current)

    harness.agent_effect = measure
    previous = os.umask(0)
    try:
        assert harness.main(*mode) == 0
        after = os.umask(0)
    finally:
        os.umask(previous)
    assert after == 0, "the caller's umask was not put back"
    assert stat.S_IMODE(harness.artifacts.stat().st_mode) == 0o755
    assert seen == ([] if mode == ("--precheck",) else [0o022])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--max-turns", "0"),
        ("--max-turns", "-3"),
        ("--max-budget-usd", "0"),
        ("--max-budget-usd", "-1"),
        ("--max-budget-usd", "nan"),
        ("--max-budget-usd", "inf"),
        ("--session-minutes", "0"),
        ("--session-minutes", "-5"),
    ],
)
def test_a_limit_that_would_lift_itself_or_break_the_run_is_refused(
    capsys: pytest.CaptureFixture[str], option: str, value: str
) -> None:
    """``run_in_docker.py`` passes review options through, so a local typo reaches
    these; claude would read 0 or a negative cap as no cap, and inf breaks argv."""
    with pytest.raises(SystemExit) as raised:
        ai_ux_test.parse_args(["--mr", "812", option, value])
    assert raised.value.code == 2
    assert option in capsys.readouterr().err


@pytest.mark.unit
def test_a_sha_holding_twelve_digits_survives_redaction(harness: Harness) -> None:
    harness.inputs = _inputs(head=DIGIT_HEAD)
    harness.forge.head = DIGIT_HEAD
    harness.forge.add_note(_review_body(DIGIT_HEAD))
    assert harness.main() == 0
    assert harness.summary()["head_sha"] == DIGIT_HEAD
    for text in (harness.artifact("section.md"), harness.forge.writes()[1][2]["body"]):
        marker = section.UX_MARKER_RE.search(text)
        assert marker is not None and marker["sha"] == DIGIT_HEAD
        assert section.has_section(text, DIGIT_HEAD)


@pytest.mark.unit
def test_the_prompt_goes_on_stdin_and_no_secret_reaches_the_session(
    harness: Harness,
) -> None:
    assert harness.main("--dry-run") == 0
    assert len(harness.agent_args) == 1
    assert "prompt" not in harness.agent_kwargs
    text = harness.agent_args[0]
    assert DIFF_MARKER in text
    assert prompt.UNTRUSTED_PARAGRAPH in text
    assert '<skill file="ux-test-ci.md">' in text
    argv = harness.agent_kwargs["argv"]
    assert argv[:2] == [str(paths.Toolchain(harness.prefix).claude), "-p"]
    assert argv[argv.index("--tools") + 1] == ""
    assert not any(DIFF_MARKER in element for element in argv)
    env = harness.agent_kwargs["env"]
    assert "GITLAB_REVIEW_TOKEN" not in env
    assert not [name for name in env if name.startswith("UX_TEST_")]
    assert env["AWS_ACCESS_KEY_ID"] == SCOPED_KEY_ID
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["ANTHROPIC_MODEL"] == MODEL
    assert harness.agent_kwargs["timeout"] == 25 * 60
    assert harness.mcp_mode == 0o600
    recorder = harness.mcp_config["mcpServers"][tool_profiles.RECORDER_SERVER]
    adapter = str(paths.HARNESS_ROOT / "scripts" / "ux_recorder_mcp.py")
    assert adapter in recorder["args"]
    assert recorder["args"][recorder["args"].index("--out") + 1] == str(
        harness.recorder_kwargs["out_dir"]
    )
    assert harness.sts.assumed[0]["RoleArn"].endswith(":role/idp-sdlc-GitLab")
    assert harness.summary()["credential_scope"] == {
        "claude": "scoped",
        "render": "scoped",
    }


@pytest.mark.unit
def test_the_turn_and_budget_limits_given_to_the_harness_reach_the_session(
    harness: Harness,
) -> None:
    assert harness.main("--dry-run", "--max-turns", "7", "--max-budget-usd", "2.5") == 0
    argv = harness.agent_kwargs["argv"]
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--max-budget-usd") + 1] == "2.50"
    assert argv.count("--max-turns") == argv.count("--max-budget-usd") == 1
    limits = harness.summary()["limits"]
    assert (limits["max_turns"], limits["max_budget_usd"]) == (7, 2.5)


@pytest.mark.unit
def test_claudes_cwd_and_the_devtools_file_root_are_private_empty_run_dirs(
    harness: Harness,
) -> None:
    """chrome-devtools-mcp writes under its ``--filesystemRoot`` and under any root
    the MCP client reports, which for claude is its working directory, so both are
    empty private directories of the run, not the harness or the checkout."""
    seen: dict[str, tuple[Path, int, list[str]]] = {}

    def inspect() -> None:
        servers = harness.mcp_config["mcpServers"]
        args = servers[tool_profiles.CHROME_DEVTOOLS_SERVER]["args"]
        (root,) = [
            a.split("=", 1)[1] for a in args if a.startswith("--filesystemRoot=")
        ]
        for name, path in (
            ("cwd", Path(harness.agent_kwargs["cwd"])),
            ("root", Path(root)),
        ):
            mode = path.stat().st_mode & 0o777
            seen[name] = (path, mode, sorted(p.name for p in path.iterdir()))

    harness.agent_effect = inspect
    assert harness.main("--dry-run") == 0
    assert harness.run_dir is not None
    agent_root = harness.run_dir / "agent"
    assert seen["cwd"] == (agent_root / "cwd", 0o700, [])
    assert seen["root"] == (agent_root / "mcp-files", 0o700, [])


@pytest.mark.unit
def test_each_consumer_is_minted_a_session_for_its_own_actions_only(
    harness: Harness,
) -> None:
    """claude's assume allows Bedrock alone and render's Polly alone, and each
    consumer is handed the session minted for it, not the other one."""
    assert harness.main("--dry-run") == 0
    sts = harness.sts
    assert len(sts.assumed) == 2
    assert sts.policy_actions(0) == list(account.BEDROCK_ACTIONS)
    assert sts.policy_actions(1) == list(account.POLLY_ACTIONS)
    assert sts.assumed[0]["RoleSessionName"] == f"ux-test-{IID}"
    assert sts.assumed[1]["RoleSessionName"] == f"ux-test-{IID}-render"
    assert harness.agent_kwargs["env"]["AWS_ACCESS_KEY_ID"] == SCOPED_KEY_ID
    assert harness.render_kwargs["aws"] == {
        "AWS_ACCESS_KEY_ID": POLLY_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": POLLY_KEY_TEXT,
        "AWS_SESSION_TOKEN": POLLY_SESSION_TEXT,
    }


@pytest.mark.unit
def test_render_runs_as_the_harness_child_in_private_directories_of_the_run(
    harness: Harness,
) -> None:
    assert harness.main("--dry-run") == 0
    kwargs = harness.render_kwargs
    run_dir = harness.run_dir
    assert run_dir is not None
    assert kwargs["harness_root"] == paths.HARNESS_ROOT
    assert kwargs["home"] == run_dir / "render-home"
    assert kwargs["log_path"] == run_dir / "render.log"
    assert kwargs["timeout"] >= ai_ux_test._RENDER_MIN_SECONDS
    assert (kwargs["voice"], kwargs["region"], kwargs["narration"]) == (
        "Ruth",
        "us-east-1",
        True,
    )
    assert kwargs["user"] == paths.RENDER_USER
    assert "client_factory" not in kwargs


@pytest.mark.unit
def test_render_finds_ffmpeg_where_the_harness_does(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai_ux_test.shutil, "which", lambda tool: f"/opt/tools/{tool}")
    assert harness.main("--dry-run") == 0
    assert harness.render_kwargs["search_path"] == (
        "/opt/tools:" + recording.recorder_env(Path("/h"))["PATH"]
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("found", "expected"),
    [
        (None, "/usr/local/bin:/usr/bin:/bin"),
        ("/usr/bin/ffmpeg", "/usr/local/bin:/usr/bin:/bin"),
        ("/opt/homebrew/bin/ffmpeg", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"),
    ],
    ids=["absent", "already-on-the-path", "elsewhere"],
)
def test_a_childs_path_gains_the_tools_directory_only_when_it_lacks_it(
    monkeypatch: pytest.MonkeyPatch, found: str | None, expected: str
) -> None:
    monkeypatch.setattr(ai_ux_test.shutil, "which", lambda tool: found)
    assert ai_ux_test._search_path("ffmpeg", "/usr/local/bin:/usr/bin:/bin") == expected


@pytest.mark.unit
def test_a_failed_self_assume_runs_claude_on_the_ambient_credentials_and_says_so(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """D5's fallback for claude, said before the session starts; render gets no
    fallback at all, so it renders without narration and holds no credential."""
    harness.sts = FakeSts(assume_fails=True)
    assert harness.main("--dry-run") == 0
    summary = harness.summary()
    assert summary["credential_scope"] == {
        "claude": "ambient",
        "render": "unavailable",
    }
    assert harness.agent_kwargs["env"]["AWS_ACCESS_KEY_ID"] == AWS_KEY_ID
    assert harness.render_kwargs["aws"] == {}
    assert harness.render_kwargs["narration"] is False
    assert summary["recording"]["narration"] == "unavailable (no Polly session)"
    out = capsys.readouterr().out.splitlines()
    warned = [i for i, line in enumerate(out) if line.startswith("WARNING: ")]
    started = [i for i, line in enumerate(out) if line.startswith("session: ")]
    assert warned and started and warned[0] < started[0]
    assert "job's own AWS credentials" in out[warned[0]]


@pytest.mark.unit
def test_a_refused_render_assume_renders_without_narration_or_any_credential(
    harness: Harness,
) -> None:
    """claude keeps its Bedrock session, and render is never handed the job's own:
    it handles the most MR-influenced data of the run."""
    harness.sts = FakeSts(failing=(1,))
    assert harness.main("--dry-run") == 0
    summary = harness.summary()
    assert summary["credential_scope"] == {
        "claude": "scoped",
        "render": "unavailable",
    }
    assert harness.agent_kwargs["env"]["AWS_ACCESS_KEY_ID"] == SCOPED_KEY_ID
    assert harness.render_kwargs["aws"] == {}
    assert harness.render_kwargs["narration"] is False
    assert summary["recording"]["narration"] == "unavailable (no Polly session)"
    for value in (AWS_KEY_ID, AWS_KEY_TEXT, AWS_SESSION_TEXT):
        assert value not in json.dumps(harness.render_kwargs, default=str)


@pytest.mark.unit
def test_without_narration_render_is_minted_and_handed_no_session(
    harness: Harness,
) -> None:
    assert harness.main("--dry-run", "--no-narration") == 0
    assert len(harness.sts.assumed) == 1
    assert harness.render_kwargs["aws"] == {}
    assert harness.render_kwargs["narration"] is False
    assert harness.summary()["credential_scope"] == {
        "claude": "scoped",
        "render": None,
    }


@pytest.mark.unit
def test_narration_and_chapter_labels_are_redacted_before_render_reads_them(
    harness: Harness,
) -> None:
    """Render speaks every mark's line, muxes it in as captions, and prefers the
    recorder's narration.md to the events, so the session is redacted first."""
    harness.mark_say = f"Signed in as {PERSONA_USER}; the API {API_HOST} in {ACCOUNT}."
    harness.mark_label = f"Header shows {PERSONA_USER}"
    assert harness.main("--dry-run") == 0
    assert harness.render_saw_narration_md is False
    assert harness.spoken, "render was given nothing to speak"
    told = " ".join([*harness.spoken.values(), *harness.rendered_labels])
    for secret in (PERSONA_USER, API_HOST, ACCOUNT):
        assert secret not in told, f"render would speak or caption {secret!r}"
    assert "Signed in as [redacted]" in told
    assert "Header shows [redacted]" in told


@pytest.mark.unit
def test_narration_refused_by_polly_is_recorded(harness: Harness) -> None:
    harness.narration = "unavailable (AccessDenied)"
    assert harness.main("--dry-run") == 0
    summary = harness.summary()
    assert summary["recording"]["narration"] == "unavailable (AccessDenied)"
    assert harness.render_kwargs["narration"] is True
    assert harness.render_kwargs["log_path"].name == "render.log"


@pytest.mark.unit
def test_dev_unconfined_is_a_dry_run_without_the_policy_check(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chrome, "policy_installed", lambda *a, **k: False)
    harness.environ.pop("GITLAB_REVIEW_TOKEN")
    assert harness.main("--dev-unconfined") == 0
    summary = harness.summary()
    assert (summary["status"], summary["confinement"]) == ("dry_run", "dev-unconfined")
    assert "verify_policy" not in harness.calls
    assert "verify_proxy_path" in harness.calls
    assert "verify_viewport" not in harness.calls
    assert "measure_viewport" in harness.calls
    assert summary["viewport"] == {"w": 1600, "h": 1000, "dpr": 1}


@pytest.mark.unit
def test_a_missing_managed_policy_fails_before_any_account_call(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chrome, "policy_installed", lambda *a, **k: False)
    assert harness.main() == 1
    assert "managed Chrome policy is not installed" in harness.summary()["detail"]
    assert "sts" not in harness.calls


@pytest.mark.unit
def test_collect_only_writes_the_inputs_with_api_metadata(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    target = harness.tmp / "out" / "inputs.json"
    assert harness.main("--collect-only", "--inputs", str(target)) == 0
    assert capsys.readouterr().out.strip() == f"collected: {target}"
    collected = mr_inputs.read(target)
    assert (collected.head_sha, collected.base_sha) == (HEAD, BASE)
    assert collected.title_from_git is False
    assert collected.web_url.endswith(f"/merge_requests/{IID}")


# --- the review stack bound -------------------------------------------------------

OTHER_STACK = "idp-dev-4821"
#: A stack whose name is the review stack's and a dash, so its bucket names pass the
#: egress rules' prefix check under ``--stack idp-ux-nightly``.
LOOKALIKE_STACK = "idp-ux-nightly-2"
STACK_REASON = "`--stack` is not the review stack `idp-ux-nightly`"
ENV_REASON = "`UX_TEST_UI_ENV` does not name the settings parameter of `idp-ux-nightly`"
PAGE_REASON = (
    "the signed-in page did not report `idp-ux-nightly` as its stack, so the run's "
    "stack could not be confirmed as the review stack"
)
BUCKET_KEYS = ("VITE_TEST_SET_BUCKET_NAME", "VITE_INPUT_BUCKET_NAME", "UX_TEST_BUCKETS")
#: Another stack's Cognito pools and API, to pair with the review stack's parameter.
OTHER_POOLS = {
    "VITE_USER_POOL_ID": "us-east-1_ZyXwVuTsR",
    "VITE_USER_POOL_CLIENT_ID": "9z8y7x6w5v4u3t2s1r0q9p8o7n",
    "VITE_IDENTITY_POOL_ID": "us-east-1:99999999-8888-7777-6666-fedcbafedcba",
    "VITE_API_BASE_URL": "https://zyxwv98765.execute-api.us-east-1.amazonaws.com/api",
}


def _ui_env(
    stack: str = "idp-ux-nightly",
    *,
    without: tuple[str, ...] = (),
    values: dict[str, str] | None = None,
) -> str:
    """:data:`UI_ENV_TEXT` as ``stack``'s, less ``without``, with ``values`` set."""
    chosen = values or {}
    kept = []
    for line in UI_ENV_TEXT.replace("idp-ux-nightly", stack).splitlines():
        key = line.partition("=")[0]
        if key in without:
            continue
        kept.append(f"{key}={chosen[key]}" if key in chosen else line)
    return "\n".join(kept)


def _against(harness: Harness, ui_env: str, shown: str | None) -> None:
    """Run with ``ui_env`` as UX_TEST_UI_ENV and ``shown`` as the page's stack."""
    harness.environ["UX_TEST_UI_ENV"] = _b64(ui_env)
    harness.facts = dataclasses.replace(harness.facts, stack_name=shown)


def _withheld_line(reason: str) -> str:
    return (
        f"_The video was withheld, because {reason}: recording another stack's "
        "screens needs a privacy decision of its own._"
    )


def _assert_withheld(harness: Harness, text: str, reason: str) -> None:
    """Nothing was rendered, uploaded or kept, and ``text`` says why, findings intact.

    Render is never prepared, so its Polly session is never minted either: the one
    assume is claude's.
    """
    assert "render_video" not in harness.calls
    assert not [p for _, p, _ in harness.forge.writes() if p.endswith("/uploads")]
    assert not list(harness.artifacts.glob("*.mp4"))
    lines = text.splitlines()
    assert _withheld_line(reason) in lines
    assert not [line for line in lines if line.startswith(("![", "**Chapters:**"))]
    assert "Rotate button has no tooltip" in text
    summary = harness.summary()
    assert summary["stack"]["review_stack"] is False
    recording = summary["recording"]
    assert (recording["video_withheld"], recording["video_attached"]) == (
        reason,
        False,
    )
    assert recording["video_bytes"] is None
    assert summary["credential_scope"]["render"] is None
    assert len(harness.sts.assumed) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stack", "settings", "shown", "reason"),
    [
        ("idp-ux-nightly", "idp-ux-nightly-Settings", "idp-ux-nightly", ""),
        (OTHER_STACK, "idp-ux-nightly-Settings", "idp-ux-nightly", STACK_REASON),
        (LOOKALIKE_STACK, "idp-ux-nightly-Settings", "idp-ux-nightly", STACK_REASON),
        ("idp-ux-nightly", f"{LOOKALIKE_STACK}-Settings", "idp-ux-nightly", ENV_REASON),
        ("idp-ux-nightly", "idp-ux-nightly-settings", "idp-ux-nightly", ENV_REASON),
        ("idp-ux-nightly", None, "idp-ux-nightly", ENV_REASON),
        ("idp-ux-nightly", "idp-ux-nightly-Settings", None, PAGE_REASON),
        ("idp-ux-nightly", "idp-ux-nightly-Settings", LOOKALIKE_STACK, PAGE_REASON),
    ],
)
def test_every_statement_of_the_stack_must_name_the_review_stack(
    stack: str, settings: str | None, shown: str | None, reason: str
) -> None:
    """Exact names, each source checked whatever the others say, and a reason
    that names only the review stack, never the value it refused."""
    ui_env = {} if settings is None else {"VITE_SETTINGS_PARAMETER": settings}
    assert ai_ux_test.review_stack_problem(stack, ui_env, shown) == reason
    assert OTHER_STACK not in reason and LOOKALIKE_STACK not in reason


@pytest.mark.unit
def test_the_check_and_the_stack_are_sessions_own_called_not_copied() -> None:
    """The first-time-user stories hold their videos to the same check, so the MR
    UX test takes the function and the stack from ``session.py`` rather than
    keeping copies that could drift apart."""
    assert ai_ux_test.review_stack_problem is session.review_stack_problem
    assert ai_ux_test.REVIEW_STACK is session.REVIEW_STACK


@pytest.mark.unit
def test_the_review_stack_gets_its_video_rendered_attached_and_uploaded(
    harness: Harness,
) -> None:
    """Every source names it, as in the CI job, which runs with GitLab CI's own
    variables set and ``--stack`` at its default."""
    assert ai_ux_test.parse_args(["--mr", str(IID)]).stack == ai_ux_test.REVIEW_STACK
    harness.environ.update(CI="true", GITLAB_CI="true")
    target = harness.forge.add_note(_review_body(HEAD), thread="review-thread")
    assert harness.main() == 0
    assert "render_video" in harness.calls
    assert harness.recorder_kwargs["say"] == (
        f"UX test of merge request {IID} on the review stack, as Admin."
    )
    uploads = [p for _, p, _ in harness.forge.writes() if p.endswith("/uploads")]
    assert uploads == ["/projects/group%2Fproj/uploads"]
    body = harness.forge.store[target]["body"]
    video = f"![UX test](/uploads/{UPLOAD_HASH}/ux-test-mr{IID}-{HEAD[:8]}.mp4)"
    assert video in body.splitlines()
    assert "withheld" not in body
    summary = harness.summary()
    assert summary["status"] == "posted"
    assert summary["stack"]["review_stack"] is True
    recording = summary["recording"]
    assert (recording["video_withheld"], recording["video_attached"]) == (None, True)
    assert recording["dev_video_any_stack"] is False


#: Each way a section is delivered: the options that choose it, whether the code
#: review's note is there to append to, and the writes it makes under the MR, where
#: ``{note}`` is the section's own note and ``{old}`` an older section's.
_DELIVERIES = {
    "appended": (
        (),
        True,
        [
            ("PUT", "notes/{note}"),
            ("POST", "discussions/review-thread/notes"),
            ("PUT", "notes/{old}"),
        ],
    ),
    "standalone": (
        ("--review-poll-minutes", "0"),
        False,
        [("POST", "notes"), ("PUT", "notes/{old}")],
    ),
    "dry_run": (("--dry-run",), True, []),
}


@pytest.mark.unit
@pytest.mark.parametrize("delivery", sorted(_DELIVERIES))
def test_another_stack_gets_its_findings_without_a_video_saying_it_was_withheld(
    harness: Harness, capsys: pytest.CaptureFixture[str], delivery: str
) -> None:
    """Its own UI environment, and a page reporting it: nothing disagrees, but it
    is not the review stack, so however the section is delivered nothing is
    rendered, kept or uploaded, and the section says the video was withheld and
    why. Appended to the code review, the one reply in its thread names no video;
    posted on its own, after no code review appeared, the note carries the same
    line; and every marker records no upload. The writes are exactly the note,
    the reply where there is one, and an older section's superseded video line."""
    options, with_review, writes = _DELIVERIES[delivery]
    _against(harness, _ui_env(OTHER_STACK), OTHER_STACK)
    forge = harness.forge
    old = forge.add_note(_old_section_body(OLDER))
    if with_review:
        forge.add_note(_review_body(HEAD), thread="review-thread")
    assert harness.main("--stack", OTHER_STACK, *options) == 0
    summary = harness.summary()
    note_id = summary["note"]["note_id"]
    mr = f"/projects/group%2Fproj/merge_requests/{IID}"
    assert [(method, path) for method, path, _ in forge.writes()] == [
        (method, f"{mr}/" + path.format(note=note_id, old=old))
        for method, path in writes
    ]
    if delivery == "dry_run":
        text = harness.artifact("section.md")
    else:
        text = forge.store[note_id]["body"]
        assert summary["note"]["action"] == delivery
        assert summary["note"]["upload_id"] is None
        assert summary["detail"].endswith(", without its video, which was withheld")
    if delivery == "standalone":
        assert text.startswith(section.STANDALONE_PREFIX + "\n\n---\n")
    if delivery == "appended":
        reply = forge.writes()[1][2]["body"]
        assert reply.startswith(f"🖱️ UX test of `{HEAD[:8]}` added to the review")
        assert "video" not in reply.lower()
    marker = section.UX_MARKER_RE.search(text)
    assert marker is not None
    assert (marker["sha"], marker["upload"]) == (HEAD, "none")
    _assert_withheld(harness, text, STACK_REASON)
    assert summary["status"] == ("dry_run" if delivery == "dry_run" else "posted")
    assert f"video: withheld, because {STACK_REASON}" in capsys.readouterr().out


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ui_env", "shown", "reason"),
    [
        pytest.param(
            _ui_env(OTHER_STACK, without=BUCKET_KEYS),
            OTHER_STACK,
            ENV_REASON,
            id="another-stacks-environment-less-its-buckets",
        ),
        pytest.param(
            _ui_env(LOOKALIKE_STACK),
            LOOKALIKE_STACK,
            ENV_REASON,
            id="a-lookalike-stacks-environment",
        ),
        pytest.param(
            _ui_env(without=("VITE_SETTINGS_PARAMETER",)),
            None,
            ENV_REASON,
            id="no-settings-parameter",
        ),
        pytest.param(
            _ui_env(values=OTHER_POOLS),
            None,
            PAGE_REASON,
            id="the-review-stacks-parameter-beside-another-stacks-pools",
        ),
        pytest.param(
            _ui_env(), OTHER_STACK, PAGE_REASON, id="a-page-reporting-another-stack"
        ),
    ],
)
def test_a_ui_environment_pointing_at_another_stack_never_satisfies_the_bound(
    harness: Harness, ui_env: str, shown: str | None, reason: str
) -> None:
    """``--stack`` names the review stack in every case, as it does in CI, and each
    environment passes the egress rules, so the run goes ahead and only the video
    is lost. The lookalike's buckets carry ``idp-ux-nightly-``, which is all the
    egress rules ask of a bucket; and another stack's pools cannot read the review
    stack's parameter, so the page reports no stack at all."""
    _against(harness, ui_env, shown)
    target = harness.forge.add_note(_review_body(HEAD), thread="review-thread")
    assert harness.main() == 0
    _assert_withheld(harness, harness.forge.store[target]["body"], reason)
    summary = harness.summary()
    assert summary["status"] == "posted"
    assert summary["detail"].endswith(", without its video, which was withheld")


@pytest.mark.unit
@pytest.mark.parametrize(
    "shown", [None, "Stack: idp-ux-nightly"], ids=["no-name", "relabelled"]
)
def test_the_review_stacks_own_page_without_its_name_is_withheld_as_unconfirmed(
    harness: Harness, shown: str | None
) -> None:
    """``--stack`` and the whole UI environment are the review stack's, but its
    settings arriving after the page is read, or an MR relabelling the sidebar's
    stack item, leaves the page without that name. That still costs the video, and
    the line says the stack could not be confirmed, not that it is another."""
    _against(harness, _ui_env(), shown)
    target = harness.forge.add_note(_review_body(HEAD), thread="review-thread")
    assert harness.main() == 0
    text = harness.forge.store[target]["body"]
    _assert_withheld(harness, text, PAGE_REASON)
    (line,) = [line for line in text.splitlines() if "withheld" in line]
    assert "could not be confirmed as the review stack" in line


@pytest.mark.unit
def test_another_stacks_whole_ui_environment_is_refused_before_any_browser(
    harness: Harness,
) -> None:
    """Its buckets do not carry ``--stack``'s name, so the egress rules refuse the
    environment in the precheck and no session runs at all."""
    _against(harness, _ui_env(OTHER_STACK), OTHER_STACK)
    assert harness.main() == 0
    summary = harness.summary()
    assert summary["status"] == "skipped"
    assert "is not an exact bucket name of the stack" in summary["detail"]
    started = {"launch_proxy", "launch_chrome", "run_agent", "render_video"}
    assert not started & set(harness.calls)


@pytest.mark.unit
def test_the_developer_opt_in_keeps_another_stacks_video_in_a_local_dry_run(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rendered into the artifact directory and never posted, because the opt-in
    forces the dry run, with a line saying why the stack is not the review stack
    and that the frames are unredacted."""
    flag = ai_ux_test.DEV_VIDEO_FLAG
    assert ai_ux_test.parse_args(["--mr", str(IID), flag]).dry_run is True
    _against(harness, _ui_env(OTHER_STACK), OTHER_STACK)
    harness.forge.add_note(_review_body(HEAD))
    assert harness.main("--stack", OTHER_STACK, flag) == 0
    assert "render_video" in harness.calls
    assert harness.recorder_kwargs["say"] == (
        f"UX test of merge request {IID} on another stack, as Admin."
    )
    assert (harness.artifacts / f"ux-test-mr{IID}-{HEAD[:8]}.mp4").is_file()
    assert harness.forge.writes() == []
    summary = harness.summary()
    assert summary["status"] == "dry_run"
    assert summary["stack"]["review_stack"] is False
    recording = summary["recording"]
    assert (recording["video_withheld"], recording["video_attached"]) == (None, True)
    assert recording["dev_video_any_stack"] is True
    warning = (
        f"WARNING: {flag}: {STACK_REASON}; the video is rendered all the same, its "
        f"frames unredacted, into {harness.artifacts}, and nothing is posted"
    )
    assert warning in capsys.readouterr().out.splitlines()


@pytest.mark.unit
@pytest.mark.parametrize("spelled", [ai_ux_test.DEV_VIDEO_FLAG, "--dev-v"])
@pytest.mark.parametrize("name", ["CI", "GITLAB_CI"])
def test_the_developer_opt_in_is_refused_under_ci_before_anything_runs(
    harness: Harness, capsys: pytest.CaptureFixture[str], name: str, spelled: str
) -> None:
    """A pipeline's artifacts can be downloaded, so the opt-in fails the job there
    before a single collaborator is called or an artifact written, in any spelling
    the parser accepts for it."""
    harness.environ[name] = "true"
    flag = ai_ux_test.DEV_VIDEO_FLAG
    assert harness.main(spelled) == 1
    assert harness.calls == []
    assert not harness.artifacts.exists()
    assert capsys.readouterr().out == (
        f"FAILED: {flag} is a developer's opt-in for a local dry run, and {name} "
        "is set, so this is CI\n"
    )


# --- static checks ----------------------------------------------------------------


def _python_files(entry: str) -> list[Path]:
    path = REPO_ROOT / entry
    if path.is_dir():
        return sorted(path.rglob("*.py"))
    return [path] if path.suffix == ".py" else []


def _resolved(importer: Path, node: ast.Import | ast.ImportFrom) -> list[Path]:
    """The scripts/ or scripts/sdlc/ files an import statement can load."""
    targets: list[Path] = []
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
        for entry in ai_ux_test.HARNESS_PATHS
    )


@pytest.mark.unit
def test_every_harness_import_is_itself_in_the_harness() -> None:
    files = [f for entry in ai_ux_test.HARNESS_PATHS for f in _python_files(entry)]
    assert len(files) > 20
    stray = sorted(
        {
            f"{importer.relative_to(REPO_ROOT)} imports {target.relative_to(REPO_ROOT)}"
            for importer in files
            for node in ast.walk(ast.parse(importer.read_text(encoding="utf-8")))
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for target in _resolved(importer, node)
            if not _covered(target)
        }
    )
    assert not stray, f"unaccounted harness import: {stray}"


@pytest.mark.unit
def test_every_harness_entry_exists_and_holds_the_skills_and_the_flows() -> None:
    absent = [e for e in ai_ux_test.HARNESS_PATHS if not (REPO_ROOT / e).exists()]
    assert not absent, f"HARNESS_PATHS names paths the tree does not have: {absent}"
    assert set(ai_ux_test.SKILL_FILES) <= set(ai_ux_test.HARNESS_PATHS)
    assert ai_ux_test.FLOWS_FILE in ai_ux_test.HARNESS_PATHS
    assert len(set(ai_ux_test.HARNESS_PATHS)) == len(ai_ux_test.HARNESS_PATHS)


@pytest.mark.unit
def test_ai_mr_review_has_exactly_one_importer() -> None:
    assert ai_ux_test.review is gitlab_api.review
    modules = [*sorted((SCRIPTS / "ux_runner").glob("*.py")), SCRIPT]
    importers = set()
    for path in modules:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.split(".")[0] == "ai_mr_review" for name in names):
                importers.add(path.name)
    assert importers == {"gitlab_api.py"}


@pytest.mark.unit
def test_the_recorder_adapter_serves_exactly_the_granted_recorder_tools() -> None:
    names = tuple(tool["name"] for tool in ux_recorder_mcp.TOOLS)
    assert names == tool_profiles.RECORDER_TOOLS
    assert ux_recorder_mcp.SERVER_NAME == tool_profiles.RECORDER_SERVER
    granted = {
        tool_profiles.mcp_tool(tool_profiles.RECORDER_SERVER, name) for name in names
    }
    assert granted <= set(tool_profiles.EXPERT_ALLOWED_TOOLS)


@pytest.mark.unit
def test_the_runtime_names_every_injectable_collaborator_in_order() -> None:
    names = [f.name for f in dataclasses.fields(ai_ux_test.Runtime)]
    assert names == [
        "forge",
        "collect",
        "sts",
        "launch_proxy",
        "launch_chrome",
        "wait_for_cdp",
        "first_page_target",
        "open_session",
        "verify_policy",
        "verify_proxy_path",
        "sign_in",
        "page_facts",
        "verify_viewport",
        "measure_viewport",
        "socket_watch",
        "kill_strays",
        "start_recorder",
        "stop_recorder",
        "kill_recorder",
        "recorder_status",
        "run_agent",
        "render_video",
        "chapters",
        "sleep",
        "clock",
        "environ",
    ]


@pytest.mark.unit
def test_the_runtime_is_the_browser_runtime_the_confined_browser_is_handed() -> None:
    """``session.ConfinedBrowser`` is handed this Runtime as it is, so each member of
    ``session.BrowserRuntime`` is a Runtime field of the same name and annotation."""
    fields = {f.name: f.type for f in dataclasses.fields(ai_ux_test.Runtime)}
    members = dict(session.BrowserRuntime.__annotations__)
    assert len(members) == 13
    assert {name: fields.get(name) for name in members} == members


@pytest.mark.unit
def test_the_default_runtime_binds_the_real_collaborators() -> None:
    runtime = ai_ux_test.default_runtime()
    assert runtime.collect is mr_inputs.collect
    assert runtime.launch_proxy is egress.launch_proxy
    assert runtime.launch_chrome is chrome.launch
    assert runtime.verify_policy is chrome.verify_policy
    assert runtime.sign_in is signin.sign_in
    assert runtime.page_facts is signin.page_facts
    assert runtime.verify_viewport is chrome.verify_viewport
    assert runtime.measure_viewport is chrome.measure_viewport
    assert runtime.socket_watch is chrome.SocketWatch
    assert runtime.kill_strays is chrome.kill_strays
    assert runtime.start_recorder is recording.start
    assert runtime.kill_recorder is recording.kill
    assert runtime.run_agent is agent.run_agent
    assert runtime.render_video is recording.render_video


@pytest.mark.unit
def test_help_runs_on_a_bare_python_without_the_pinned_packages() -> None:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-X", "importtime", str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-400:]
    assert "--precheck" in completed.stdout
    imported = re.findall(r"\|\s+(\S+)\s*$", completed.stderr, flags=re.M)
    heavy = sorted({m for m in imported if m.split(".")[0] in ("boto3", "yaml", "PIL")})
    assert not heavy, f"--help imported {heavy}"


@pytest.mark.unit
def test_the_script_imports_on_a_python_without_sigkill_as_on_windows() -> None:
    """``run_in_docker.py review`` runs ``--collect-only`` on the host, a Windows
    host included, and Windows has no ``signal.SIGKILL``. So nothing the script
    imports may need it before a process group is actually signalled: the attribute
    is deleted here before the script runs, as Windows would leave it."""
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
    assert "--collect-only" in completed.stdout


# --- the unattended skill contract ------------------------------------------------


def _headings(text: str) -> set[str]:
    return {
        line.lstrip("#").strip().lower()
        for line in text.splitlines()
        if line.startswith("### ")
    }


@pytest.mark.unit
def test_the_ci_skill_states_only_how_the_unattended_run_differs() -> None:
    ci = (REPO_ROOT / ".claude/skills/ux-test-ci.md").read_text(encoding="utf-8")
    interactive = (REPO_ROOT / ".claude/skills/ux-test.md").read_text(encoding="utf-8")
    assert "ux-test.md" in ci
    assert not _headings(ci) & _headings(interactive)
    assert len(ci) < len(interactive)
    for word in ("untrusted", "injection_attempts", "visible_change_found"):
        assert word in ci, word
    assert not re.search(r"(?<![0-9])[0-9]{12}(?![0-9])", ci)


def _headed_bodies(text: str) -> list[tuple[str, str, str]]:
    """``(## heading, ### heading, line)`` for every line of a skill outside a fence."""
    found: list[tuple[str, str, str]] = []
    h2 = h3 = ""
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("## "):
            h2, h3 = line[3:].strip(), ""
            continue
        elif not fenced and line.startswith("### "):
            h3 = line[4:].strip()
            continue
        found.append((h2, h3, line))
    return found


@pytest.mark.unit
def test_the_ci_skill_overrides_all_of_the_interactive_setup_and_its_gotchas() -> None:
    """The interactive skill's advice for a desktop Chrome (a profile directory, the
    debugging port, ``chrome://inspect``, ``list_pages``) sits only in its Setup
    section and in "Before you start", and the CI skill's first difference
    overrides that section whole, Gotchas included, rather than parts of it: a part
    left standing is advice about a browser the session does not have."""
    ci = (REPO_ROOT / ".claude/skills/ux-test-ci.md").read_text(encoding="utf-8")
    interactive = (REPO_ROOT / ".claude/skills/ux-test.md").read_text(encoding="utf-8")
    first = re.search(r"^### 1\. [^\n]*\n(.*?)^### ", ci, re.M | re.S)
    assert first, "ux-test-ci.md no longer opens its differences with a ### 1."
    overrides = " ".join(first.group(1).split()).split(". ")[0]
    for needle in (
        "the whole of `ux-test.md`'s Setup section",
        "every one of its Gotchas",
        '"Before you start"',
    ):
        assert needle in overrides, (needle, overrides)
    lines = _headed_bodies(interactive)
    gotchas = [h3 for h2, h3, _ in lines if h2.startswith("Setup") and "Gotchas" in h3]
    assert gotchas, "ux-test.md no longer holds its Gotchas under Setup"
    for needle in ("--user-data-dir", "chrome://inspect", "9222", "list_pages"):
        homes = {(h2, h3) for h2, h3, line in lines if needle in line}
        assert homes, f"ux-test.md no longer mentions {needle}"
        outside = sorted(
            (h2, h3)
            for h2, h3 in homes
            if not h2.startswith("Setup") and h3 != "Before you start"
        )
        assert not outside, f"{needle} is advised where §1 does not override: {outside}"


@pytest.mark.unit
def test_the_ci_skill_names_the_console_and_network_only_as_out_of_reach() -> None:
    """The session holds no tool that reads the console or the network, so the
    skill names either only to say so: listing them among what the browser shows
    invites a claim about a surface the session cannot see."""
    for tool in (
        "list_console_messages",
        "get_console_message",
        "list_network_requests",
        "get_network_request",
    ):
        name = tool_profiles.mcp_tool(tool_profiles.CHROME_DEVTOOLS_SERVER, tool)
        assert name in tool_profiles.EXPERT_DISALLOWED_TOOLS, name
        assert not tool_profiles.is_granted(name), name
    ci = (REPO_ROOT / ".claude/skills/ux-test-ci.md").read_text(encoding="utf-8")
    sentences = re.split(r"(?<=[.;:!?])\s+", " ".join(ci.split()))
    mentions = [s for s in sentences if re.search(r"\b(?:console|network)\b", s, re.I)]
    assert mentions, "ux-test-ci.md no longer says the console and network are unread"
    for sentence in mentions:
        assert re.search(r"\b(?:cannot|no tool)\b", sentence), sentence


@pytest.mark.unit
def test_the_ci_skill_links_only_to_files_that_exist() -> None:
    skill = REPO_ROOT / ".claude/skills/ux-test-ci.md"
    targets = re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", skill.read_text("utf-8"))
    relative = [t for t in targets if "://" not in t]
    assert relative
    missing = [t for t in relative if not (skill.parent / t).exists()]
    assert not missing, f"unresolvable links: {missing}"


@pytest.mark.unit
def test_the_cline_entry_is_a_symlink_to_the_claude_skill() -> None:
    entry = REPO_ROOT / ".cline/skills/ux-test-ci.md"
    assert entry.is_symlink()
    assert os.readlink(entry) == "../../.claude/skills/ux-test-ci.md"
    assert entry.resolve() == (REPO_ROOT / ".claude/skills/ux-test-ci.md").resolve()
