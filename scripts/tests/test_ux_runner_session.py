# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/ux_runner/session.py, the confined browser's lifecycle.

``ConfinedBrowser`` is driven with a fake ``BrowserRuntime`` whose every member
records its call, so what is pinned is the order the confinement depends on: the
proxy before Chrome, the socket watch from Chrome's launch, both self-tests before
the one sign-in, the viewport before the DevTools session closes, and on the way
out Chrome before the audit's last pass and the audit before the proxy stops. A
step that fails leaves exactly what had been started for the cleanup steps, each of
which does its work once however often it is called. The two credential mints are
driven with a fake STS.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ux_test_proxy  # noqa: E402
from ux_runner import (  # noqa: E402
    account,
    chrome,
    egress,
    paths,
    processes,
    session,
    signin,
)

STACK = "idp-ux-nightly"
PARTITION = "aws"
ACCOUNT = "111122223333"
UI_ENV = {
    "VITE_AWS_REGION": "us-east-1",
    "VITE_USER_POOL_ID": "us-east-1_AbCdEfGhI",
    "VITE_API_BASE_URL": "https://abcde12345.execute-api.us-east-1.amazonaws.com/api",
}
PERSONA = signin.Persona(
    "Admin", "ux-persona-admin@example.invalid", "Ux!3cretive-persona-9aZ"
)
TARGET = {
    "id": "TARGET1",
    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/T1",
}
FACTS = signin.PageFacts(STACK, "0.6.12", "2026-10-13", (1600, 1000, 1.0), None)
VIEWPORT = {"w": 1600, "h": 1000, "dpr": 1}
REFUSALS = [
    {
        "t": "2026-10-15T00:00:00Z",
        "method": "GET",
        "target": f"http://{chrome.SELFTEST_HOST}/",
        "reason": "plain_http_not_allowed",
    }
]
SIGN_IN_ORDER = [
    "launch_proxy",
    "launch_chrome",
    "socket_watch",
    "watch started",
    "wait_for_cdp",
    "first_page_target",
    "open_session",
    "verify_policy",
    "verify_proxy_path",
    "sign_in",
    "page_facts",
    "verify_viewport",
    "session closed",
]


class _Process:
    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self.pid = 4100
        self.terminations = 0
        self._log = log

    def terminate(self, grace: float = 10.0) -> int:
        self.terminations += 1
        self._log.append(f"{self.name} terminated")
        return 0


class _DevTools:
    def __init__(self, log: list[str]) -> None:
        self.closed = 0
        self._log = log

    def close(self) -> None:
        self.closed += 1
        self._log.append("session closed")


class _Watch:
    def __init__(self, log: list[str]) -> None:
        self.stopped = 0
        self._log = log

    def start(self) -> _Watch:
        self._log.append("watch started")
        return self

    def stop(self) -> chrome.SocketAudit:
        self.stopped += 1
        self._log.append("watch stopped")
        return chrome.SocketAudit("clean", (), 3)


class _Runtime:
    """Every ``session.BrowserRuntime`` member, each logging its own name.

    A name in ``failing`` raises its exception instead of answering.
    """

    def __init__(self) -> None:
        self.log: list[str] = []
        self.failing: dict[str, Exception] = {}
        self.calls: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        self.devtools = _DevTools(self.log)
        self.chrome = _Process("chrome", self.log)
        self.proxy_process: _Process | None = None
        self.watch: _Watch | None = None
        self.strays: tuple[str, ...] = ()
        self.swept: list[tuple[int | None, int | None]] = []

    def _called(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.log.append(name)
        self.calls[name] = (args, kwargs)
        if name in self.failing:
            raise self.failing[name]

    def launch_proxy(self, **kwargs: Any) -> egress.ProxyRun:
        self._called("launch_proxy", **kwargs)
        proxy_dir = Path(kwargs["run_dir"]) / "proxy"
        proxy_dir.mkdir()
        refusal_log = proxy_dir / "refusals.jsonl"
        refusal_log.write_text(
            "".join(json.dumps(record) + "\n" for record in REFUSALS),
            encoding="utf-8",
        )
        self.proxy_process = _Process("proxy", self.log)
        return egress.ProxyRun(
            process=self.proxy_process,
            ready_file=proxy_dir / "ready",
            refusal_log=refusal_log,
            stats_file=proxy_dir / "stats.json",
            log_file=proxy_dir / "proxy.log",
            owner=os.geteuid(),
        )

    def launch_chrome(self, *args: Any, **kwargs: Any) -> _Process:
        self._called("launch_chrome", *args, **kwargs)
        return self.chrome

    def wait_for_cdp(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self._called("wait_for_cdp", *args, **kwargs)
        return {"Browser": "HeadlessChrome"}

    def first_page_target(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self._called("first_page_target", *args, **kwargs)
        return dict(TARGET)

    def open_session(self, ws_url: str) -> _DevTools:
        self._called("open_session", ws_url)
        return self.devtools

    def verify_policy(self, devtools: Any) -> None:
        self._called("verify_policy", devtools)

    def verify_proxy_path(self, *args: Any, **kwargs: Any) -> None:
        self._called("verify_proxy_path", *args, **kwargs)

    def sign_in(self, *args: Any, **kwargs: Any) -> None:
        self._called("sign_in", *args, **kwargs)

    def page_facts(self, devtools: Any) -> signin.PageFacts:
        self._called("page_facts", devtools)
        return FACTS

    def verify_viewport(self, devtools: Any) -> dict[str, Any]:
        self._called("verify_viewport", devtools)
        return dict(VIEWPORT)

    def measure_viewport(self, devtools: Any) -> dict[str, Any]:
        self._called("measure_viewport", devtools)
        return dict(VIEWPORT)

    def socket_watch(self, uid: int | None) -> _Watch:
        self._called("socket_watch", uid)
        self.watch = _Watch(self.log)
        return self.watch

    def kill_strays(self, uid: int | None, group: int | None) -> tuple[str, ...]:
        self._called("kill_strays", uid, group)
        self.swept.append((uid, group))
        return self.strays


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> _Runtime:
    monkeypatch.setattr(processes, "can_switch_user", lambda: True)
    return _Runtime()


def _plan(tmp_path: Path, **changes: Any) -> session.BrowserPlan:
    build = tmp_path / "build"
    if not build.exists():
        build.mkdir()
        (build / "index.html").write_text("<!doctype html>", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    plan = session.BrowserPlan(
        toolchain=paths.Toolchain(tmp_path / "toolchain"),
        run_dir=run_dir,
        ui_env=UI_ENV,
        stack=STACK,
        persona=PERSONA,
        persona_label="Admin",
        build_dir=build,
    )
    return dataclasses.replace(plan, **changes)


def _browser(
    runtime: _Runtime, said: list[str] | None = None
) -> session.ConfinedBrowser:
    return session.ConfinedBrowser(
        runtime, say=(said if said is not None else []).append
    )


def _recorder(log: list[str]) -> tuple[list[chrome.SocketAudit], Callable[..., None]]:
    noted: list[chrome.SocketAudit] = []

    def on_audit(audit: chrome.SocketAudit) -> None:
        noted.append(audit)
        log.append("on_audit")

    return noted, on_audit


@pytest.mark.unit
def test_open_confines_the_browser_before_it_signs_the_persona_in(
    runtime: _Runtime, tmp_path: Path
) -> None:
    said: list[str] = []
    browser = _browser(runtime, said)
    plan = _plan(tmp_path)
    opened = browser.open(plan)
    assert runtime.log == SIGN_IN_ORDER
    rules = ux_test_proxy.stack_rules(UI_ENV, STACK)
    assert runtime.calls["launch_proxy"][1] == {
        "run_dir": plan.run_dir,
        "rules": rules,
        "build_dir": plan.build_dir.resolve(),
        "user": paths.PROXY_USER,
    }
    assert runtime.calls["launch_chrome"] == (
        (plan.toolchain,),
        {
            "run_dir": plan.run_dir,
            "user": paths.BROWSER_USER,
            "log": plan.run_dir / "chrome.log",
        },
    )
    assert runtime.calls["socket_watch"] == ((paths.BROWSER_UID,), {})
    assert runtime.calls["open_session"] == ((TARGET["webSocketDebuggerUrl"],), {})
    proxy = browser.proxy
    assert proxy is not None
    assert runtime.calls["verify_proxy_path"] == (
        (runtime.devtools, proxy.refusal_log),
        {"owner": proxy.owner},
    )
    assert runtime.calls["sign_in"] == ((runtime.devtools, PERSONA), {})
    for step in ("verify_policy", "page_facts", "verify_viewport"):
        assert runtime.calls[step] == ((runtime.devtools,), {}), step
    assert said == [
        f"egress proxy: {paths.PROXY_HOST}:{paths.PROXY_PORT}, "
        f"{len(rules.hosts)} host(s) reachable",
        "chrome: confined, and signed in as Admin",
    ]
    assert opened == session.OpenedBrowser(
        target=TARGET, facts=FACTS, viewport=VIEWPORT, browser_uid_switched=True
    )
    assert runtime.devtools.closed == 1
    assert browser.session is None
    assert browser.chrome_proc is runtime.chrome
    assert browser.watch is runtime.watch
    assert (browser.browser_uid, browser.browser_uid_switched) == (
        paths.BROWSER_UID,
        True,
    )
    assert browser.viewport == VIEWPORT


@pytest.mark.unit
def test_the_progress_line_names_the_persona_by_its_label(
    runtime: _Runtime, tmp_path: Path
) -> None:
    said: list[str] = []
    _browser(runtime, said).open(_plan(tmp_path, persona_label="Reviewer"))
    assert said[-1] == "chrome: confined, and signed in as Reviewer"
    assert PERSONA.username not in " ".join(said)


@pytest.mark.unit
def test_dev_unconfined_skips_the_policy_check_and_only_measures_the_viewport(
    runtime: _Runtime, tmp_path: Path
) -> None:
    opened = _browser(runtime).open(_plan(tmp_path, dev_unconfined=True))
    expected = [
        "measure_viewport" if step == "verify_viewport" else step
        for step in SIGN_IN_ORDER
        if step != "verify_policy"
    ]
    assert runtime.log == expected
    assert opened.viewport == VIEWPORT


@pytest.mark.unit
def test_without_a_uid_switch_there_is_no_uid_to_watch(
    runtime: _Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processes, "can_switch_user", lambda: False)
    browser = _browser(runtime)
    opened = browser.open(_plan(tmp_path))
    assert runtime.calls["socket_watch"] == ((None,), {})
    assert (browser.browser_uid, browser.browser_uid_switched) == (None, False)
    assert opened.browser_uid_switched is False


@pytest.mark.unit
def test_a_failure_after_the_launch_leaves_what_was_started_for_the_cleanup(
    runtime: _Runtime, tmp_path: Path
) -> None:
    """``browser_uid_switched`` is known from the launch, so a caller records it
    even though ``open`` raised; the cleanup steps then stop each thing once."""
    runtime.failing["wait_for_cdp"] = chrome.ChromeError("DevTools did not answer")
    browser = _browser(runtime)
    with pytest.raises(chrome.ChromeError, match="DevTools did not answer"):
        browser.open(_plan(tmp_path))
    assert runtime.log == SIGN_IN_ORDER[: SIGN_IN_ORDER.index("wait_for_cdp") + 1]
    assert browser.browser_uid_switched is True
    assert browser.viewport is None and browser.session is None
    assert browser.proxy is not None and browser.chrome_proc is runtime.chrome
    runtime.log.clear()
    noted, on_audit = _recorder(runtime.log)
    browser.close_session()
    browser.stop_chrome()
    browser.stop_watch(on_audit=on_audit)
    browser.stop_proxy()
    assert runtime.log == [
        "chrome terminated",
        "kill_strays",
        "watch stopped",
        "on_audit",
        "proxy terminated",
    ]
    assert [audit.state for audit in noted] == ["clean"]


@pytest.mark.unit
def test_a_refused_sign_in_is_not_retried_and_leaves_the_session_to_close(
    runtime: _Runtime, tmp_path: Path
) -> None:
    runtime.failing["sign_in"] = signin.SignInError("the sign-in was refused")
    browser = _browser(runtime)
    with pytest.raises(signin.SignInError):
        browser.open(_plan(tmp_path))
    assert runtime.log.count("sign_in") == 1
    assert runtime.log[-1] == "sign_in"
    assert browser.session is runtime.devtools and runtime.devtools.closed == 0
    browser.close_session()
    browser.close_session()
    assert runtime.devtools.closed == 1
    assert browser.session is None


@pytest.mark.unit
def test_a_viewport_that_is_not_the_expected_one_is_never_recorded(
    runtime: _Runtime, tmp_path: Path
) -> None:
    runtime.failing["verify_viewport"] = chrome.ChromeError("not 1600x1000 at 1x")
    browser = _browser(runtime)
    with pytest.raises(chrome.ChromeError):
        browser.open(_plan(tmp_path))
    assert browser.viewport is None
    assert browser.session is runtime.devtools


@pytest.mark.unit
def test_close_ends_chrome_before_the_audit_and_the_audit_before_the_proxy(
    runtime: _Runtime, tmp_path: Path
) -> None:
    """The audit's last pass comes after Chrome has exited, its verdict is handed
    over before the proxy stops, and the refusal log is read once it has."""
    browser = _browser(runtime)
    browser.open(_plan(tmp_path))
    runtime.log.clear()
    runtime.strays = ("pid 4180 (chrome_crashpad)",)
    noted, on_audit = _recorder(runtime.log)
    audit = browser.close(on_audit=on_audit)
    assert runtime.log == [
        "chrome terminated",
        "kill_strays",
        "watch stopped",
        "on_audit",
        "proxy terminated",
    ]
    assert noted == [audit] and audit.state == "clean"
    assert runtime.swept == [(paths.BROWSER_UID, runtime.chrome.pid)]
    assert browser.strays == ["pid 4180 (chrome_crashpad)"]
    assert browser.refusals == REFUSALS
    assert (browser.chrome_proc, browser.watch) == (None, None)
    assert browser.proxy is not None


@pytest.mark.unit
def test_close_without_a_socket_watch_stops_chrome_then_refuses(
    runtime: _Runtime, tmp_path: Path
) -> None:
    runtime.failing["socket_watch"] = RuntimeError("no /proc")
    browser = _browser(runtime)
    with pytest.raises(RuntimeError):
        browser.open(_plan(tmp_path))
    runtime.log.clear()
    noted, on_audit = _recorder(runtime.log)
    with pytest.raises(
        session.SessionError, match="^the socket audit was never started$"
    ):
        browser.close(on_audit=on_audit)
    assert runtime.log == ["chrome terminated", "kill_strays"]
    assert noted == []
    browser.stop_proxy()
    assert runtime.log[-1] == "proxy terminated"


@pytest.mark.unit
def test_every_cleanup_step_does_its_work_once_however_often_it_is_called(
    runtime: _Runtime, tmp_path: Path
) -> None:
    browser = _browser(runtime)
    browser.open(_plan(tmp_path))
    noted, on_audit = _recorder(runtime.log)
    browser.close(on_audit=on_audit)
    runtime.log.clear()
    for _ in range(2):
        browser.close_session()
        browser.stop_chrome()
        browser.stop_watch(on_audit=on_audit)
        browser.stop_proxy()
    assert runtime.log == []
    assert len(noted) == 1
    assert runtime.chrome.terminations == 1
    assert runtime.proxy_process is not None
    assert runtime.proxy_process.terminations == 1


@pytest.mark.unit
def test_a_browser_that_started_nothing_has_nothing_to_stop(runtime: _Runtime) -> None:
    browser = _browser(runtime)
    noted, on_audit = _recorder(runtime.log)
    browser.close_session()
    browser.stop_chrome()
    browser.stop_watch(on_audit=on_audit)
    browser.stop_proxy()
    assert runtime.log == [] and noted == []
    assert (browser.proxy, browser.chrome_proc, browser.watch) == (None, None, None)
    assert (browser.strays, browser.refusals) == ([], [])
    assert (browser.browser_uid, browser.browser_uid_switched) == (None, False)


@pytest.mark.unit
def test_the_plan_and_the_opened_browser_hold_their_frozen_fields(
    tmp_path: Path,
) -> None:
    plan_fields = [(f.name, f.default) for f in dataclasses.fields(session.BrowserPlan)]
    assert plan_fields == [
        ("toolchain", dataclasses.MISSING),
        ("run_dir", dataclasses.MISSING),
        ("ui_env", dataclasses.MISSING),
        ("stack", dataclasses.MISSING),
        ("persona", dataclasses.MISSING),
        ("persona_label", dataclasses.MISSING),
        ("build_dir", dataclasses.MISSING),
        ("dev_unconfined", False),
        ("web_url", None),
        ("dismiss_welcome", True),
    ]
    assert [f.name for f in dataclasses.fields(session.OpenedBrowser)] == [
        "target",
        "facts",
        "viewport",
        "browser_uid_switched",
    ]
    plan = _plan(tmp_path)
    opened = session.OpenedBrowser(TARGET, FACTS, VIEWPORT, True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.stack = "elsewhere"
    with pytest.raises(dataclasses.FrozenInstanceError):
        opened.browser_uid_switched = False


class _Sts:
    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.assumed: list[dict[str, Any]] = []

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.assumed.append(kwargs)
        if self.refuse:
            raise RuntimeError("AccessDenied")
        issued = {
            "AccessKeyId": "ASIASCOPEDKEYEXAMPLE",
            "SecretAccessKey": "scopedSecretKeyValue0001",
            "SessionToken": "scopedSessionValue000001",
        }
        return {"Credentials": issued}


ROLE_SESSION = account.Identity(
    account=ACCOUNT,
    arn=f"arn:{PARTITION}:sts::{ACCOUNT}:assumed-role/idp-sdlc-GitLab/job",
)
MINTS = [
    (session.mint_bedrock, account.BEDROCK_ACTIONS),
    (session.mint_polly, account.POLLY_ACTIONS),
]


@pytest.mark.unit
@pytest.mark.parametrize(("mint", "actions"), MINTS, ids=["bedrock", "polly"])
def test_each_mint_is_one_assume_of_the_jobs_role_for_its_own_actions(
    mint: Callable[..., dict[str, str] | None], actions: tuple[str, ...]
) -> None:
    sts = _Sts()
    assert mint(sts, ROLE_SESSION, "ux-test-812") == {
        "AWS_ACCESS_KEY_ID": "ASIASCOPEDKEYEXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "scopedSecretKeyValue0001",
        "AWS_SESSION_TOKEN": "scopedSessionValue000001",
    }
    (assumed,) = sts.assumed
    assert assumed["RoleArn"] == f"arn:{PARTITION}:iam::{ACCOUNT}:role/idp-sdlc-GitLab"
    assert assumed["RoleSessionName"] == "ux-test-812"
    (statement,) = json.loads(assumed["Policy"])["Statement"]
    assert statement["Action"] == list(actions)


@pytest.mark.unit
@pytest.mark.parametrize(("mint", "actions"), MINTS, ids=["bedrock", "polly"])
def test_a_refused_or_impossible_assume_mints_nothing(
    mint: Callable[..., dict[str, str] | None], actions: tuple[str, ...]
) -> None:
    assert mint(_Sts(refuse=True), ROLE_SESSION, "ux-test-812") is None
    user = account.Identity(
        account=ACCOUNT, arn=f"arn:{PARTITION}:iam::{ACCOUNT}:user/dev"
    )
    sts = _Sts()
    assert mint(sts, user, "ux-test-812") is None
    assert sts.assumed == []


WEB_URL = "https://d111111abcdef8.cloudfront.net/"
OTHER_STACK = "idp-dev-stack4"
LOOKALIKE_STACK = "idp-ux-nightly-2"
STACK_REASON = "`--stack` is not the review stack `idp-ux-nightly`"
ENV_REASON = "`UX_TEST_UI_ENV` does not name the settings parameter of `idp-ux-nightly`"
PAGE_REASON = (
    "the signed-in page did not report `idp-ux-nightly` as its stack, so the run's "
    "stack could not be confirmed as the review stack"
)


@pytest.mark.unit
def test_a_deployed_stack_is_tunnelled_to_and_signed_in_at_its_own_web_host(
    runtime: _Runtime, tmp_path: Path
) -> None:
    """No build is served, the web host joins the stack's own hosts, and the one
    sign-in goes to that host, in the same order as a build's."""
    browser = _browser(runtime)
    plan = _plan(tmp_path, build_dir=None, web_url=WEB_URL)
    opened = browser.open(plan)
    assert runtime.log == SIGN_IN_ORDER
    rules = ux_test_proxy.stack_rules(UI_ENV, STACK, web_url=WEB_URL)
    assert "d111111abcdef8.cloudfront.net:443" in rules.hosts
    assert runtime.calls["launch_proxy"][1] == {
        "run_dir": plan.run_dir,
        "rules": rules,
        "build_dir": None,
        "user": paths.PROXY_USER,
    }
    assert runtime.calls["sign_in"] == (
        (runtime.devtools, PERSONA),
        {"base_url": WEB_URL},
    )
    assert opened.facts == FACTS


@pytest.mark.unit
@pytest.mark.parametrize("deployed", [False, True], ids=["build", "deployed"])
def test_a_first_visit_keeps_the_welcome_page_and_nothing_else_changes(
    runtime: _Runtime, tmp_path: Path, deployed: bool
) -> None:
    changes: dict[str, Any] = {"dismiss_welcome": False}
    if deployed:
        changes.update(build_dir=None, web_url=WEB_URL)
    _browser(runtime).open(_plan(tmp_path, **changes))
    expected: dict[str, Any] = {"dismiss_welcome": False}
    if deployed:
        expected["base_url"] = WEB_URL
    assert runtime.calls["sign_in"] == ((runtime.devtools, PERSONA), expected)
    assert runtime.log == SIGN_IN_ORDER


@pytest.mark.unit
@pytest.mark.parametrize("dismiss", [True, False], ids=["dismiss", "first-visit"])
@pytest.mark.parametrize("deployed", [False, True], ids=["build", "deployed"])
def test_every_sign_in_a_plan_asks_for_binds_to_the_real_sign_in(
    runtime: _Runtime, tmp_path: Path, deployed: bool, dismiss: bool
) -> None:
    """The runtime's sign-in is a fake here, so the options a plan hands it are
    bound to ``signin.sign_in``'s own signature: an option it does not take would
    fail every real flow of that kind at its first sign-in."""
    changes: dict[str, Any] = {"dismiss_welcome": dismiss}
    if deployed:
        changes.update(build_dir=None, web_url=WEB_URL)
    _browser(runtime).open(_plan(tmp_path, **changes))
    args, kwargs = runtime.calls["sign_in"]
    inspect.signature(signin.sign_in).bind(*args, **kwargs)


@pytest.mark.unit
@pytest.mark.parametrize(
    "changes",
    [{"build_dir": None}, {"web_url": WEB_URL}],
    ids=["neither", "both"],
)
def test_a_plan_must_name_exactly_one_ui_before_anything_starts(
    runtime: _Runtime, tmp_path: Path, changes: dict[str, Any]
) -> None:
    browser = _browser(runtime)
    with pytest.raises(session.SessionError, match="exactly one UI"):
        browser.open(_plan(tmp_path, **changes))
    assert runtime.log == []
    assert browser.proxy is None and browser.chrome_proc is None


@pytest.mark.unit
def test_a_web_url_that_is_not_https_is_refused_before_the_proxy_starts(
    runtime: _Runtime, tmp_path: Path
) -> None:
    browser = _browser(runtime)
    plan = _plan(tmp_path, build_dir=None, web_url="http://example.com/")
    with pytest.raises(ux_test_proxy.EgressConfigError, match="https URL"):
        browser.open(plan)
    assert runtime.log == []


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
    assert session.review_stack_problem(stack, ui_env, shown) == reason
    assert OTHER_STACK not in reason and LOOKALIKE_STACK not in reason


@pytest.mark.unit
def test_a_page_that_shows_no_stack_is_unconfirmed_and_never_called_another() -> None:
    reason = session.review_stack_problem(
        session.REVIEW_STACK,
        {"VITE_SETTINGS_PARAMETER": f"{session.REVIEW_STACK}-Settings"},
        None,
    )
    assert "could not be confirmed as the review stack" in reason
    assert "another" not in reason


@pytest.mark.unit
def test_the_review_stack_is_the_one_the_nightly_build_updates() -> None:
    """Read from the nightly's source as an AST constant, without importing the
    CodeBuild harness."""
    source = (SCRIPTS / "sdlc" / "codebuild_deployment.py").read_text(encoding="utf-8")
    values = [
        ast.literal_eval(node.value)
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "UX_NIGHTLY_STACK_NAME"
            for target in node.targets
        )
    ]
    assert values == [session.REVIEW_STACK]
