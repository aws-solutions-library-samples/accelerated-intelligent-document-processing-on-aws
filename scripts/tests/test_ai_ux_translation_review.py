# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""End-to-end tests of the screen review, ``scripts/sdlc/ai_ux_translation_review.py``.

:func:`main` is driven with a fake :class:`Runtime`: every browser step logs its
name, STS answers a fixed identity, and the session is a fake ``claude`` that
reads the MCP config it was given, saves screenshots under the relay's file root
as chrome-devtools-mcp would, and answers with a report. No subprocess, network or
AWS call happens. What is pinned is the order confinement needs for each locale,
that the model holds the look-only grant and no recorder, how the budget is split
and stopped, that one locale failing leaves the next to run, and that the files
hold no secret the run knows.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
for entry in (SCRIPTS, SCRIPTS / "sdlc"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import ai_ux_translation_review as review  # noqa: E402
from ux_runner import (  # noqa: E402
    agent,
    chrome,
    egress,
    processes,
    review_relay,
    signin,
    tool_profiles,
)

ACCOUNT = "111122223333"
ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/Admin/reviewer"
LOGIN = "ux-persona-admin@example.invalid"
SECRET = "Ux!3cretive-persona-9aZ"
WEB_URL = "https://d111111abcdef8.cloudfront.net"
UI_ENV_TEXT = (
    "VITE_AWS_REGION=us-east-1\n"
    "VITE_USER_POOL_ID=us-east-1_AbCdEfGhI\n"
    "VITE_API_BASE_URL=https://abcde12345.execute-api.us-east-1.amazonaws.com/api\n"
)
GLOSSARY = "en,fr-CA,note\nupload,téléverser,Verb.\n"
TARGET = {"id": "T1", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/T1"}
FACTS = signin.PageFacts("idp-fr-test", "0.6.16", "2030-01-02", (1600, 1000, 1.0), None)
USAGE = {"input_tokens": 1000, "output_tokens": 1000}


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _environ() -> dict[str, str]:
    personas = {"Admin": {"username": LOGIN, "password": SECRET}}
    return {
        "PATH": "/usr/bin:/bin",
        "UX_TEST_UI_ENV": _b64(UI_ENV_TEXT),
        "UX_TEST_PERSONAS": _b64(json.dumps(personas)),
        "UX_TEST_AWS_ACCOUNT_ID": ACCOUNT,
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    }


def _report(locale: str, *, host: str = "") -> str:
    """What the fake model answers: two screens, one finding citing its picture."""
    shown = f"Upload documents {host}".strip()
    value = {
        "locale": locale,
        "screens": [
            {
                "id": "documents",
                "reached": True,
                "screenshot": f"{locale}-documents.png",
            },
            {"id": "upload", "reached": True, "screenshot": f"{locale}-upload.png"},
        ],
        "findings": [
            {
                "screen": "upload",
                "flow": None,
                "kind": "english_left" if locale == "fr-CA" else "not_externalized",
                "severity": "medium",
                "shown": shown,
                "expected": "",
                "screenshot": f"{locale}-upload.png",
                "note": "title",
            }
        ],
    }
    return "Done.\n```json\n" + json.dumps(value, ensure_ascii=False) + "\n```\n"


class _Process:
    def __init__(self, name: str, log: list[str]) -> None:
        self.name, self.pid, self._log = name, 4100, log

    def terminate(self, grace: float = 10.0) -> int:
        self._log.append(f"{self.name} terminated")
        return 0


class _DevTools:
    def __init__(self, name: str, log: list[str]) -> None:
        self.name, self._log = name, log

    def close(self) -> None:
        self._log.append(f"{self.name} closed")


class _Watch:
    def __init__(self, runtime: FakeRuntime) -> None:
        self.runtime = runtime

    def start(self) -> _Watch:
        return self

    def stop(self) -> chrome.SocketAudit:
        self.runtime.log.append("watch stopped")
        return self.runtime.audit


class _Sts:
    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": ACCOUNT, "Arn": ARN}


class _Toolchain:
    def __init__(self, missing: list[str]) -> None:
        self.prefix = Path("/opt/ux-runner")
        self.claude = self.prefix / "node_modules/.bin/claude"
        self.devtools_mcp = self.prefix / "node_modules/.bin/chrome-devtools-mcp"
        self._missing = missing

    def missing(self) -> list[str]:
        return self._missing

    def versions(self) -> dict[str, str]:
        return {"chrome": "142", "node": "22", "claude_code": "2.1", "extra": "x"}


class FakeRuntime(review.Runtime):
    """The real Runtime's fields, each a method of this fake that logs itself.

    ``failing`` maps a step's name to what it raises; ``answers`` is what each
    session answers in turn, a string or a callable taking the file root;
    ``costs`` is each session's own cost figure.
    """

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self.log: list[str] = []
        self.failing: dict[str, BaseException] = {}
        self.audit = chrome.SocketAudit("clean", (), 3)
        self.strays: tuple[str, ...] = ()
        self.missing: list[str] = []
        self.policy = True
        self.minted: dict[str, str] | None = {
            "AWS_ACCESS_KEY_ID": "ASIAMINTEDKEYEXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "minted-secret",
            "AWS_SESSION_TOKEN": "minted-token",
        }
        self.answers: list[Any] = []
        self.costs: list[float | None] = []
        self.sessions: list[dict[str, Any]] = []
        self.locales: list[str] = []
        super().__init__(
            sts=self._sts,
            toolchain=self._toolchain,
            policy_installed=lambda: self.policy,
            launch_proxy=self._launch_proxy,
            launch_chrome=self._launch_chrome,
            wait_for_cdp=self._step("wait_for_cdp", {}),
            first_page_target=self._step("first_page_target", TARGET),
            open_session=self._open_session,
            verify_policy=self._step("verify_policy", None),
            verify_proxy_path=self._step("verify_proxy_path", None),
            sign_in=self._step("sign_in", None),
            page_facts=self._step("page_facts", FACTS),
            verify_viewport=self._step("verify_viewport", {"w": 1600, "h": 1000}),
            measure_viewport=self._step("measure_viewport", {"w": 1600, "h": 1000}),
            socket_watch=self._socket_watch,
            kill_strays=lambda uid, group: self.strays,
            set_locale=self._set_locale,
            mint_bedrock=self._mint,
            run_agent=self._run_agent,
            clock=lambda: 100.0,
            environ=environ if environ is not None else _environ(),
        )

    def _check(self, name: str) -> None:
        self.log.append(name)
        if name in self.failing:
            raise self.failing[name]

    def _step(self, name: str, answer: Any) -> Callable[..., Any]:
        def step(*args: Any, **kwargs: Any) -> Any:
            self._check(name)
            return answer

        return step

    def _sts(self, region: str) -> _Sts:
        self._check("sts")
        return _Sts()

    def _toolchain(self, prefix: Any, environ: Any) -> _Toolchain:
        return _Toolchain(self.missing)

    def _launch_proxy(self, **kwargs: Any) -> egress.ProxyRun:
        self._check("launch_proxy")
        proxy_dir = Path(kwargs["run_dir"]) / "proxy"
        proxy_dir.mkdir()
        return egress.ProxyRun(
            process=_Process("proxy", self.log),
            ready_file=proxy_dir / "ready",
            refusal_log=proxy_dir / "refusals.jsonl",
            stats_file=proxy_dir / "stats.json",
            log_file=proxy_dir / "proxy.log",
            owner=os.geteuid(),
        )

    def _launch_chrome(self, *args: Any, **kwargs: Any) -> _Process:
        self._check("launch_chrome")
        return _Process("chrome", self.log)

    def _open_session(self, url: str) -> _DevTools:
        self.log.append("open_session")
        return _DevTools("devtools", self.log)

    def _socket_watch(self, uid: int | None) -> _Watch:
        self.log.append("socket_watch")
        return _Watch(self)

    def _set_locale(self, devtools: Any, locale: str, *, base_url: str) -> None:
        self.locales.append(locale)
        self._check(f"set_locale {locale}")

    def _mint(self, sts: Any, identity: Any, name: str) -> dict[str, str] | None:
        self.log.append(f"mint {name}")
        return self.minted

    def _run_agent(
        self,
        prompt: str,
        *,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        timeout: float,
    ) -> tuple[agent.AgentResult | None, processes.Completed]:
        self._check("run_agent")
        config = json.loads(
            Path(argv[argv.index("--mcp-config") + 1]).read_text(encoding="utf-8")
        )
        relay = config["mcpServers"][tool_profiles.CHROME_DEVTOOLS_SERVER]["args"]
        root = Path(relay[relay.index("--filesystem-root") + 1])
        index = len(self.sessions)
        self.sessions.append(
            {"prompt": prompt, "argv": argv, "env": env, "config": config, "root": root}
        )
        locale = "fr-CA" if "fr-CA" in prompt.split("\n", 1)[0] else "en-XA"
        for name in (f"{locale}-documents.png", f"{locale}-upload.png"):
            (root / name).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        answer = self.answers[index] if index < len(self.answers) else _report(locale)
        if callable(answer):
            answer = answer(root)
        if isinstance(answer, tuple):
            return answer
        cost = self.costs[index] if index < len(self.costs) else 3.0
        completed = processes.Completed(0, "", "", False, 1.0)
        if answer is None:
            return None, processes.Completed(124, "", "timed out", True, 1.0)
        result = agent.AgentResult(answer, cost, 40, 300.0, "end_turn", False, USAGE)
        return result, completed


def _run(
    tmp_path: Path, runtime: FakeRuntime, *extra: str, glossary: bool = True
) -> tuple[int, dict[str, Any], Path]:
    out = tmp_path / "out"
    gloss = tmp_path / "glossary.fr-CA.csv"
    if glossary:
        gloss.write_text(GLOSSARY, encoding="utf-8")
    argv = [
        "--stack",
        "idp-fr-test",
        "--web-url",
        WEB_URL,
        "--glossary",
        str(gloss),
        "--artifact-dir",
        str(out),
        "--region",
        "ca-central-1",
        "--model",
        "us.anthropic.claude-opus-5-5",
        *extra,
    ]
    code = review.main(argv, runtime)
    record = json.loads((out / review.RUN_FILE).read_text(encoding="utf-8"))
    return code, record, out


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.mark.unit
def test_each_locale_runs_in_its_own_confined_browser_in_order(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    code, record, _ = _run(tmp_path, runtime)
    assert code == 0
    assert record["status"] == "reviewed"
    assert [e["locale"] for e in record["locales"]] == ["fr-CA", "en-XA"]
    assert all(
        e["status"] == "reviewed" and e["audit_clean"] for e in record["locales"]
    )
    assert runtime.locales == ["fr-CA", "en-XA"]
    per_locale = [
        "launch_proxy",
        "launch_chrome",
        "socket_watch",
        "wait_for_cdp",
        "first_page_target",
        "open_session",
        "verify_policy",
        "verify_proxy_path",
        "sign_in",
        "page_facts",
        "verify_viewport",
        "devtools closed",
        "open_session",
        "set_locale fr-CA",
        "devtools closed",
        "mint ux-translation-fr-CA",
        "run_agent",
        "chrome terminated",
        "watch stopped",
        "proxy terminated",
    ]
    second = [step.replace("fr-CA", "en-XA") for step in per_locale]
    assert runtime.log == ["sts", *per_locale, *second]
    assert record["locales"][0]["page"] == {
        "stack": "idp-fr-test",
        "version": "0.6.16",
        "build": "2030-01-02",
    }


@pytest.mark.unit
def test_the_session_holds_the_look_only_grant_and_no_recorder(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    _run(tmp_path, runtime)
    first = runtime.sessions[0]
    argv = first["argv"]
    granted = argv[argv.index("--allowedTools") + 1 : argv.index("--disallowedTools")]
    assert tuple(granted) == tool_profiles.SCREEN_REVIEW_ALLOWED_TOOLS
    assert list(first["config"]["mcpServers"]) == [tool_profiles.CHROME_DEVTOOLS_SERVER]
    assert first["env"]["ANTHROPIC_MODEL"] == "us.anthropic.claude-opus-5-5"
    assert first["env"]["AWS_REGION"] == "ca-central-1"
    assert first["env"]["AWS_ACCESS_KEY_ID"] == "ASIAMINTEDKEYEXAMPLE"
    assert "UX_TEST_PERSONAS" not in first["env"]
    assert record_entry(tmp_path, 0)["credential_scope"] == "bedrock"


def record_entry(tmp_path: Path, index: int) -> dict[str, Any]:
    run = json.loads((tmp_path / "out" / review.RUN_FILE).read_text(encoding="utf-8"))
    return run["locales"][index]


@pytest.mark.unit
def test_the_budget_is_split_with_headroom_and_counts_the_larger_cost(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    runtime.costs = [3.0, 0.001]
    _, record, _ = _run(tmp_path, runtime)
    caps = [
        session["argv"][session["argv"].index("--max-budget-usd") + 1]
        for session in runtime.sessions
    ]
    assert caps == ["18.00", "33.30"]
    first, second = record["locales"]
    assert first["counted_usd"] == 3.0
    pinned = second["session"]["pinned_cost_usd"]
    assert pinned == pytest.approx(0.024)
    assert second["counted_usd"] == pytest.approx(pinned)
    assert record["spent_usd"] == pytest.approx(3.024)


@pytest.mark.unit
def test_a_locale_starts_only_while_enough_budget_is_left(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    runtime.costs = [2.5]
    code, record, _ = _run(tmp_path, runtime, "--max-usd", "3")
    assert code == 1
    assert record["locales"][1] == {
        "locale": "en-XA",
        "status": "not_run",
        "detail": "$0.50 of the budget is left",
    }
    assert len(runtime.sessions) == 1


@pytest.mark.unit
def test_a_session_with_no_answer_counts_its_whole_cap(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    runtime.answers = [None]
    code, record, _ = _run(tmp_path, runtime)
    assert code == 1
    first, second = record["locales"]
    assert first["status"] == "failed"
    assert "the session gave no report (no answer, exit 124)" in first["detail"]
    assert first["session"]["last_error"] == "timed out"
    assert first["counted_usd"] == 18.0
    assert second["budget_usd"] == 19.8
    assert second["status"] == "reviewed"


def _refusal(text: str, stderr: str = "") -> tuple[Any, processes.Completed]:
    """A session that ended in an error, as an API refusal ends one."""
    result = agent.AgentResult(text, 0.0, 1, 180.0, "end_turn", True, None)
    return result, processes.Completed(1, "{}", stderr, False, 1.0)


@pytest.mark.unit
def test_a_failed_session_keeps_claudes_last_error_line_redacted(
    tmp_path: Path, runtime: FakeRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime.answers = [
        _refusal(
            "API Error: 403\n"
            f"User: {ARN} is not authorized to perform: bedrock:InvokeModel on "
            f"account {ACCOUNT} for {LOGIN}\n\n",
            stderr="not this line",
        )
    ]
    code, record, out = _run(tmp_path, runtime)
    assert code == 1
    first = record["locales"][0]
    assert first["status"] == "failed"
    assert "the session gave no report (end_turn, exit 1)" in first["detail"]
    error = first["session"]["last_error"]
    assert error.startswith("User: ")
    assert "is not authorized to perform: bedrock:InvokeModel on account" in error
    page = (out / review.FINDINGS_MD).read_text(encoding="utf-8")
    assert (
        "- fr-CA: failed: the session gave no report (end_turn, exit 1). "
        "claude's last error: User: " in page
    )
    printed = capsys.readouterr().out
    assert "fr-CA: claude's last error: User: " in printed
    for text in (error, page, printed, (out / review.RUN_FILE).read_text("utf-8")):
        for secret in (ARN, ACCOUNT, LOGIN):
            assert secret not in text.replace("\u200b", "")


@pytest.mark.unit
def test_why_a_locale_was_not_reviewed_is_redacted_before_it_is_sanitised(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    # sanitize puts a zero-width space after the login's "@", so a login redacted
    # only after it would reach the page split, and unmatched.
    runtime.failing["set_locale fr-CA"] = signin.SignInError(
        f"the UI did not switch to fr-CA for {LOGIN} ({SECRET})"
    )
    _, _, out = _run(tmp_path, runtime, "--locale", "fr-CA")
    page = (out / review.FINDINGS_MD).read_text(encoding="utf-8")
    line = next(line for line in page.splitlines() if line.startswith("- fr-CA:"))
    assert line.startswith(
        "- fr-CA: locale unavailable: the UI did not switch to fr-CA"
    )
    for secret in (LOGIN, SECRET):
        assert secret not in page.replace("\u200b", "")


@pytest.mark.unit
def test_a_failed_session_that_left_no_line_keeps_its_detail_alone(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    runtime.answers = [_refusal("  \n")]
    _, record, out = _run(tmp_path, runtime, "--locale", "fr-CA")
    assert "last_error" not in record["locales"][0]["session"]
    page = (out / review.FINDINGS_MD).read_text(encoding="utf-8")
    assert page.rstrip().endswith(
        "- fr-CA: failed: the session gave no report (end_turn, exit 1)"
    )


@pytest.mark.unit
def test_the_prompt_is_the_locales_own(tmp_path: Path, runtime: FakeRuntime) -> None:
    _run(tmp_path, runtime)
    french, pseudo = (session["prompt"] for session in runtime.sessions)
    assert "- upload → téléverser (Verb.)" in french
    assert f"| documents | {WEB_URL}/#/documents |" in french
    assert "téléverser" not in pseudo and "not_externalized" in pseudo
    assert SECRET not in french + pseudo and LOGIN not in french + pseudo


@pytest.mark.unit
def test_findings_and_screenshots_land_in_the_files_with_no_secret(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    host = WEB_URL.removeprefix("https://")
    runtime.answers = [_report("fr-CA", host=f"{host} {LOGIN} {ACCOUNT}")]
    _, _, out = _run(tmp_path, runtime)
    assert sorted(p.name for p in (out / review.SCREENS_DIR).iterdir()) == [
        "en-XA-documents.png",
        "en-XA-upload.png",
        "fr-CA-documents.png",
        "fr-CA-upload.png",
    ]
    page = (out / review.FINDINGS_MD).read_text(encoding="utf-8")
    assert "[fr-CA-upload.png](screens/fr-CA-upload.png)" in page
    assert "There is no video" in page
    findings = json.loads((out / review.FINDINGS_JSON).read_text(encoding="utf-8"))
    assert [loc["locale"] for loc in findings["locales"]] == ["fr-CA", "en-XA"]
    for name in (review.RUN_FILE, review.FINDINGS_JSON, review.FINDINGS_MD):
        text = (out / name).read_text(encoding="utf-8")
        for secret in (SECRET, LOGIN, ACCOUNT, host, ARN):
            assert secret not in text, (name, secret)


@pytest.mark.unit
def test_refused_report_items_land_redacted_in_the_findings_file(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    def item(flow: str, shown: str) -> dict[str, Any]:
        return {
            "screen": "upload",
            "flow": flow,
            "kind": "english_left",
            "severity": "medium",
            "shown": shown,
            "expected": "",
            "screenshot": "fr-CA-upload.png",
            "note": "title",
        }

    value = {
        "locale": "fr-CA",
        "screens": [],
        "findings": [item("", "Upload"), item("9.9", f"Upload {LOGIN} {ACCOUNT}")],
    }
    runtime.answers = ["```json\n" + json.dumps(value) + "\n```\n"]
    _, record, out = _run(tmp_path, runtime, "--locale", "fr-CA")
    assert record["locales"][0]["findings"] == 1
    text = (out / review.FINDINGS_JSON).read_text(encoding="utf-8")
    (locale,) = json.loads(text)["locales"]
    assert [finding["flow"] for finding in locale["findings"]] == [None]
    (refused,) = locale["refused"]
    assert (refused["part"], refused["index"]) == ("findings", 1)
    assert refused["reason"] == "its flow is not one of the run's flows"
    assert refused["item"]["flow"] == "9.9"
    assert refused["item"]["shown"].startswith("Upload ")
    for secret in (LOGIN, ACCOUNT):
        assert secret not in text
    page = (out / review.FINDINGS_MD).read_text(encoding="utf-8")
    assert "`refused` in `findings.json` lists each with the rule it broke" in page


@pytest.mark.unit
def test_a_locale_the_ui_cannot_show_fails_alone_and_the_next_runs(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    runtime.failing["set_locale fr-CA"] = signin.SignInError(
        "the UI did not switch to fr-CA within 60 s (<html lang> is not fr-CA); "
        "is fr-CA in this build?"
    )
    code, record, _ = _run(tmp_path, runtime)
    assert code == 1
    first, second = record["locales"]
    assert first["status"] == "locale_unavailable"
    assert "is fr-CA in this build?" in first["detail"]
    assert "session" not in first and "counted_usd" not in first
    assert first["audit_clean"] is True
    assert second["status"] == "reviewed"
    assert len(runtime.sessions) == 1
    assert runtime.log.count("devtools closed") == 4


@pytest.mark.unit
def test_a_refused_sign_in_leaves_the_remaining_locales_not_run(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    runtime.failing["sign_in"] = signin.SignInError("sign-in was refused: wrong")
    code, record, _ = _run(tmp_path, runtime)
    assert code == 1
    assert [e["status"] for e in record["locales"]] == ["sign_in_refused", "not_run"]
    assert record["locales"][1]["detail"] == "a sign-in was refused"
    assert runtime.log.count("sign_in") == 1
    assert "chrome terminated" in runtime.log and "proxy terminated" in runtime.log


@pytest.mark.unit
def test_a_socket_audit_that_is_not_clean_fails_the_run(
    tmp_path: Path, runtime: FakeRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime.audit = chrome.SocketAudit("violations", ("10.0.0.1:443",), 4)
    runtime.strays = ("4242 crashpad",)
    code, record, _ = _run(tmp_path, runtime)
    assert code == 1
    assert record["status"] == "incomplete"
    first = record["locales"][0]
    assert first["audit_clean"] is False
    assert first["socket_audit"]["violations"] == ["10.0.0.1:443"]
    assert (
        "the socket audit is violations, 1 stray process(es)" in capsys.readouterr().out
    )


@pytest.mark.unit
def test_an_unreadable_report_is_recorded_and_fails_the_run(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    runtime.answers = ["I looked at everything and it is fine."]
    code, record, out = _run(tmp_path, runtime)
    assert code == 1
    assert record["locales"][0]["status"] == "unreadable"
    page = (out / review.FINDINGS_MD).read_text(encoding="utf-8")
    assert "The report could not be read: the final message holds no fenced" in page


@pytest.mark.unit
def test_ambient_credentials_are_used_with_a_warning_when_none_can_be_minted(
    tmp_path: Path, runtime: FakeRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime.minted = None
    _run(tmp_path, runtime, "--locale", "en-XA")
    assert runtime.sessions[0]["env"]["AWS_ACCESS_KEY_ID"] == "AKIAIOSFODNN7EXAMPLE"
    assert "WARNING: no Bedrock-only session could be minted" in capsys.readouterr().out
    assert record_entry(tmp_path, 0)["credential_scope"] == "ambient"


@pytest.mark.unit
def test_only_the_locales_own_screenshots_are_copied_and_within_bounds(
    tmp_path: Path, runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def answer(root: Path) -> str:
        (root / "en-XA-upload.png").write_bytes(b"x" * 10)
        (root / "fr-CA-big.png").write_bytes(b"x" * 4096)
        (root / "notes.txt").write_text("x", encoding="utf-8")
        (root / "fr-CA-link.png").symlink_to(root / "fr-CA-upload.png")
        return _report("fr-CA")

    monkeypatch.setattr(review, "SCREEN_BYTES", 1024)
    runtime.answers = [answer]
    _, record, out = _run(tmp_path, runtime, "--locale", "fr-CA")
    names = sorted(p.name for p in (out / review.SCREENS_DIR).iterdir())
    assert names == ["fr-CA-documents.png", "fr-CA-upload.png"]
    assert record["locales"][0]["screenshots"] == 2


@pytest.mark.unit
def test_a_file_root_that_is_gone_copies_nothing_and_says_so(
    tmp_path: Path, runtime: FakeRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    def answer(root: Path) -> str:
        for child in root.iterdir():
            child.unlink()
        root.rmdir()
        return _report("en-XA")

    runtime.answers = [answer]
    _, record, _ = _run(tmp_path, runtime, "--locale", "en-XA")
    assert "WARNING: the screenshots cannot be listed" in capsys.readouterr().out
    assert record["locales"][0]["screenshots"] == 0


@pytest.mark.unit
def test_a_guard_log_that_cannot_be_read_back_is_a_warning(
    tmp_path: Path,
    runtime: FakeRuntime,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(path: Path) -> list[dict[str, Any]]:
        raise review_relay.GuardLogError("the guard's log is a symbolic link")

    monkeypatch.setattr(review_relay, "read_refusals", refuse)
    code, record, _ = _run(tmp_path, runtime, "--locale", "en-XA")
    assert code == 0
    assert "guard_refusals" not in record["locales"][0]
    assert "WARNING: the guard's log is refused" in capsys.readouterr().out


@pytest.mark.unit
def test_a_build_is_served_by_the_proxy_and_signed_in_to_at_its_origin(
    tmp_path: Path, runtime: FakeRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    build = tmp_path / "build"
    build.mkdir()
    (build / "index.html").write_text("<!doctype html>", encoding="utf-8")
    out = tmp_path / "out"
    gloss = tmp_path / "g.csv"
    gloss.write_text(GLOSSARY, encoding="utf-8")
    code = review.main(
        ["--stack", "idp-fr-test", "--build-dir", str(build), "--glossary", str(gloss)]
        + ["--artifact-dir", str(out), "--keep-run-dir", "--locale", "fr-CA"]
        + ["--model", "us.anthropic.claude-opus-5-5"],
        runtime,
    )
    kept = [
        line.removeprefix("run directory kept: ")
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("run directory kept: ")
    ]
    for path in kept:
        assert Path(path).name.startswith("ux-translation-")
        review.shutil.rmtree(path)
    assert code == 0
    assert len(kept) == 1
    assert (
        "| documents | http://localhost:3000/#/documents |"
        in runtime.sessions[0]["prompt"]
    )
    record = json.loads((out / review.RUN_FILE).read_text(encoding="utf-8"))
    assert record["ui"] == "build"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("setup", "detail"),
    [
        (lambda rt: rt.missing.append("claude"), "has no claude"),
        (lambda rt: setattr(rt, "policy", False), "managed Chrome policy"),
        (lambda rt: rt.environ.pop("UX_TEST_UI_ENV"), "UX_TEST_UI_ENV is not set"),
        (
            lambda rt: rt.environ.update(UX_TEST_PERSONAS="%%%"),
            "UX_TEST_PERSONAS is not valid base64",
        ),
        (
            lambda rt: rt.environ.update(UX_TEST_PERSONAS=_b64('{"Viewer": {}}')),
            "UX_TEST_PERSONAS must map",
        ),
        (
            lambda rt: rt.environ.update(UX_TEST_AWS_ACCOUNT_ID="999988887777"),
            "ending in",
        ),
        (
            lambda rt: rt.failing.update(sts=RuntimeError("no STS")),
            "RuntimeError: no STS",
        ),
    ],
    ids=[
        "toolchain",
        "policy",
        "ui-env",
        "personas-b64",
        "personas-shape",
        "account",
        "unexpected",
    ],
)
def test_inputs_that_cannot_work_fail_before_any_browser_starts(
    tmp_path: Path,
    runtime: FakeRuntime,
    setup: Callable[[FakeRuntime], Any],
    detail: str,
) -> None:
    setup(runtime)
    code, record, _ = _run(tmp_path, runtime)
    assert code == 1
    assert record["status"] == "failed"
    assert detail in record["detail"]
    assert "launch_proxy" not in runtime.log
    assert record["locales"] == []


@pytest.mark.unit
def test_a_persona_the_variable_lacks_and_a_missing_glossary_fail_early(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    code, record, _ = _run(tmp_path, runtime, "--persona", "Reviewer")
    assert (code, record["detail"]) == (1, "UX_TEST_PERSONAS has no 'Reviewer' persona")
    code, record, _ = _run(tmp_path / "b", FakeRuntime(), glossary=False)
    assert code == 1 and "cannot be read" in record["detail"]


@pytest.mark.unit
def test_the_pseudo_locale_alone_needs_no_glossary(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    code, record, _ = _run(tmp_path, runtime, "--locale", "en-XA", glossary=False)
    assert code == 0
    assert "glossary_terms" not in record


@pytest.mark.unit
@pytest.mark.parametrize(
    "step", ["first_page_target", "run_agent"], ids=["browser", "session"]
)
def test_an_unexpected_error_in_a_locale_fails_that_locale_and_cleans_up(
    tmp_path: Path, runtime: FakeRuntime, step: str
) -> None:
    runtime.failing[step] = KeyError("boom")
    code, record, _ = _run(tmp_path, runtime, "--locale", "fr-CA")
    assert code == 1
    assert record["locales"][0]["status"] == "failed"
    assert record["locales"][0]["detail"] == "KeyError: 'boom'"
    assert runtime.log[-3:] == [
        "chrome terminated",
        "watch stopped",
        "proxy terminated",
    ]


@pytest.mark.unit
def test_a_cancel_stops_the_browser_and_still_writes_the_files(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    runtime.failing["run_agent"] = processes.Terminated()
    code, record, _ = _run(tmp_path, runtime)
    assert code == 1
    assert record["detail"] == "cancelled"
    assert runtime.log[-3:] == [
        "chrome terminated",
        "watch stopped",
        "proxy terminated",
    ]
    (entry,) = record["locales"]
    assert (entry["locale"], entry["status"]) == ("fr-CA", "cancelled")
    assert record["spent_usd"] == 0


@pytest.mark.unit
def test_a_cancel_after_the_session_ran_still_counts_its_spend(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    def cancelled(uid: int | None, group: int | None) -> tuple[str, ...]:
        raise processes.Terminated()

    runtime.kill_strays = cancelled
    runtime.costs = [3.0]
    code, record, _ = _run(tmp_path, runtime)
    assert code == 1
    (entry,) = record["locales"]
    assert entry["status"] == "cancelled"
    assert entry["counted_usd"] == 3.0
    assert record["spent_usd"] == 3.0
    assert runtime.log[-2:] == ["watch stopped", "proxy terminated"]


@pytest.mark.unit
def test_a_cleanup_step_that_fails_is_named_and_the_rest_still_run(
    tmp_path: Path, runtime: FakeRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime.audit = chrome.SocketAudit("clean", (), 3)

    def broken_strays(uid: int | None, group: int | None) -> tuple[str, ...]:
        raise OSError("no /proc")

    runtime.kill_strays = broken_strays
    runtime.failing["run_agent"] = ValueError("stop here")
    _run(tmp_path, runtime, "--locale", "fr-CA")
    assert "cleanup: OSError" in capsys.readouterr().out
    assert runtime.log[-2:] == ["watch stopped", "proxy terminated"]


@pytest.mark.unit
def test_a_cancel_during_the_cleanup_waits_for_every_step(
    tmp_path: Path, runtime: FakeRuntime
) -> None:
    def cancelled(uid: int | None, group: int | None) -> tuple[str, ...]:
        raise processes.Terminated()

    runtime.kill_strays = cancelled
    runtime.failing["run_agent"] = ValueError("stop here")
    code, record, _ = _run(tmp_path, runtime)
    assert code == 1
    assert record["detail"] == "cancelled"
    assert runtime.log[-2:] == ["watch stopped", "proxy terminated"]
    (entry,) = record["locales"]
    assert (entry["locale"], entry["status"]) == ("fr-CA", "failed")
    assert entry["audit_clean"] is True


@pytest.mark.unit
def test_a_run_directory_that_cannot_be_made_fails_the_locale(
    tmp_path: Path, runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_space(**kwargs: Any) -> str:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(review.tempfile, "mkdtemp", no_space)
    code, record, _ = _run(tmp_path, runtime, "--locale", "en-XA")
    assert code == 1
    assert record["locales"][0]["status"] == "failed"
    assert "No space left on device" in record["locales"][0]["detail"]
    assert "launch_proxy" not in runtime.log


@pytest.mark.unit
def test_an_artifact_directory_that_is_a_symlink_is_refused(
    tmp_path: Path, runtime: FakeRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "out").symlink_to(tmp_path / "real")
    code = review.main(
        ["--stack", "s", "--web-url", WEB_URL, "--artifact-dir", str(tmp_path / "out")]
        + ["--model", "us.anthropic.claude-opus-5-5"],
        runtime,
    )
    assert code == 1
    assert "is a symlink" in capsys.readouterr().out


@pytest.mark.unit
@pytest.mark.parametrize(
    "argv",
    [
        ["--stack", "s"],
        ["--stack", "s", "--web-url", WEB_URL, "--build-dir", "b"],
        ["--stack", "s", "--web-url", "http://plain.example"],
        ["--stack", "s", "--web-url", "https://x.example/?a=1"],
        ["--stack", "s", "--web-url", "https://x.example/#/documents"],
        ["--stack", "s", "--web-url", "https://"],
        ["--stack", "s", "--web-url", WEB_URL, "--model", "us.anthropic.claude-x"],
        [
            "--stack",
            "s",
            "--web-url",
            WEB_URL,
            "--locale",
            "fr-CA",
            "--locale",
            "fr-CA",
        ],
        ["--stack", "s", "--web-url", WEB_URL, "--locale", "fr"],
        ["--stack", "s", "--web-url", WEB_URL, "--max-usd", "1.5"],
        ["--stack", "s", "--web-url", WEB_URL, "--max-usd", "nan"],
        ["--stack", "s", "--web-url", WEB_URL, "--max-turns", "0"],
    ],
    ids=[
        "no-ui",
        "two-uis",
        "plain-http",
        "query",
        "fragment",
        "no-host",
        "unpriced-model",
        "locale-twice",
        "unknown-locale",
        "budget-too-small",
        "budget-nan",
        "no-turns",
    ],
)
def test_options_that_cannot_work_are_refused(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        review.parse_args(argv)
    assert stopped.value.code == 2


@pytest.mark.unit
def test_the_defaults_are_the_lanes_decisions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    args = review.parse_args(["--stack", "s", "--web-url", WEB_URL])
    assert args.locale == ["fr-CA", "en-XA"]
    assert args.model == "us.anthropic.claude-opus-5-5"
    assert args.region == "ca-central-1"
    assert args.max_usd == 40.0
    assert args.persona == "Admin"
    assert args.glossary == "src/ui/src/i18n/glossary.fr-CA.csv"
    assert Path(args.artifact_dir).parent == review.DEFAULT_ARTIFACT_ROOT
    assert str(review.DEFAULT_ARTIFACT_ROOT).startswith("test-reports")


@pytest.mark.unit
def test_session_cap_is_nine_tenths_of_an_even_share_in_whole_cents() -> None:
    assert review.session_cap(40.0, 2) == 18.0
    assert review.session_cap(37.0, 1) == 33.3
    assert review.session_cap(10.0, 3) == 3.0
    assert review.session_cap(5.0, 0) == 4.5


@pytest.mark.unit
def test_the_relay_search_path_puts_nodes_directory_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review.shutil, "which", lambda tool: None)
    assert review._search_path() == agent.DEFAULT_SEARCH_PATH
    monkeypatch.setattr(review.shutil, "which", lambda tool: "/usr/bin/node")
    assert review._search_path() == agent.DEFAULT_SEARCH_PATH
    monkeypatch.setattr(review.shutil, "which", lambda tool: "/opt/node/bin/node")
    assert review._search_path() == f"/opt/node/bin:{agent.DEFAULT_SEARCH_PATH}"


@pytest.mark.unit
def test_the_default_runtime_is_the_real_collaborators() -> None:
    runtime = review.default_runtime()
    assert runtime.set_locale is signin.set_locale
    assert runtime.run_agent is agent.run_agent
    assert runtime.socket_watch is chrome.SocketWatch
