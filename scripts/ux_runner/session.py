# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One confined, signed-in browser, opened and closed in the order confinement needs.

:class:`ConfinedBrowser` starts the egress proxy, then Chrome as the browser's uid
with the socket audit watching from the moment Chrome exists, runs both
self-tests before the sign-in, reads the stack's facts once the signed-in sidebar
shows them, and checks the viewport. On the way out Chrome is terminated and
whatever its uid still runs after a few seconds' grace is killed, both before the
audit's last pass, and the proxy is stopped once nothing can use it, which makes
its refusal log final. Each cleanup step is a method of its own and safe to call
twice, because an orchestrator puts steps of its own (stopping the recorder)
between them. :func:`mint_bedrock` and :func:`mint_polly` mint the two narrow AWS
sessions, one per consumer, at the moment of use.

:func:`review_stack_problem` is the one check of whether a run's stack is the
review stack, :data:`REVIEW_STACK`, the only stack whose screens become a video.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import ux_test_proxy

from . import account, chrome, egress, paths, processes, signin

#: The long-lived review stack, and the one stack whose screens become a video,
#: because it holds only shipped example data and the frames are not redacted.
#: A test pins it equal to ``codebuild_deployment.UX_NIGHTLY_STACK_NAME``, the
#: stack the nightly updates.
REVIEW_STACK = "idp-ux-nightly"


class SessionError(Exception):
    """A step of the confined browser's lifecycle could not be done."""


def review_stack_problem(
    stack: str, ui_env: Mapping[str, str], shown: str | None
) -> str:
    """Why the run's stack is not established as :data:`REVIEW_STACK`, or ``""``.

    Three statements of which stack the run used, and each must name the review
    stack, because no one of them settles it alone:

    * ``stack``, the ``--stack`` the note is headed with, which is a default and
      not a constraint.
    * ``ui_env``, the decoded ``UX_TEST_UI_ENV`` the egress rules come from. Its
      settings parameter must be the review stack's own, which ``template.yaml``
      names ``<stack name>-Settings``, so a lookalike such as
      ``idp-ux-nightly-2`` does not match. Its bucket names are held to
      ``stack`` already (``ux_test_proxy.stack_rules``), but by a prefix, which
      that lookalike's buckets pass; its pool ids and API and stream URLs carry no
      stack name at all.
    * ``shown``, the stack the signed-in page reports. The UI reads it from the
      parameter its build names, with the persona's own Cognito credentials,
      whose role may read no other stack's parameter, so an environment that
      pairs the review stack's parameter with another stack's pools leaves the
      page with no stack to show.

    The page is the MR's own code, so its answer can withhold a video but never
    stands in for the other two. The review stack's own page can fail to report
    it too, when an MR changes the sidebar's stack item or the settings arrive
    after ``signin.page_facts`` stops waiting, so that reason says the stack
    could not be confirmed rather than that it is another. A reason names the
    review stack, never a value from the environment or the page.
    """
    if stack != REVIEW_STACK:
        return f"`--stack` is not the review stack `{REVIEW_STACK}`"
    if ui_env.get("VITE_SETTINGS_PARAMETER") != f"{REVIEW_STACK}-Settings":
        return (
            f"`UX_TEST_UI_ENV` does not name the settings parameter of `{REVIEW_STACK}`"
        )
    if shown != REVIEW_STACK:
        return (
            f"the signed-in page did not report `{REVIEW_STACK}` as its stack, so "
            "the run's stack could not be confirmed as the review stack"
        )
    return ""


class BrowserRuntime(Protocol):
    """The collaborators a :class:`ConfinedBrowser` calls, one field each.

    ``ai_ux_test.Runtime`` has every one of them under the same name and type, so
    it is passed as it is, and a test passes fakes.
    """

    launch_proxy: Callable[..., egress.ProxyRun]
    launch_chrome: Callable[..., processes.ManagedProcess]
    wait_for_cdp: Callable[..., dict[str, Any]]
    first_page_target: Callable[..., dict[str, Any]]
    open_session: Callable[[str], Any]
    verify_policy: Callable[[Any], None]
    verify_proxy_path: Callable[..., None]
    sign_in: Callable[..., None]
    page_facts: Callable[[Any], signin.PageFacts]
    verify_viewport: Callable[[Any], dict[str, Any]]
    measure_viewport: Callable[[Any], dict[str, Any]]
    socket_watch: Callable[[int | None], Any]
    kill_strays: Callable[[int | None, int | None], tuple[str, ...]]


@dataclass(frozen=True)
class BrowserPlan:
    """What one browser is opened with.

    ``persona_label`` is the name the progress line prints for the persona, and
    ``dev_unconfined`` skips the policy self-test and measures the viewport
    without requiring it, for a development host's own Chrome.
    """

    toolchain: paths.Toolchain
    run_dir: Path
    ui_env: Mapping[str, str]
    stack: str
    persona: signin.Persona
    persona_label: str
    build_dir: Path
    dev_unconfined: bool = False


@dataclass(frozen=True)
class OpenedBrowser:
    """The signed-in page's target, what the page reports, and its viewport."""

    target: dict[str, Any]
    facts: signin.PageFacts
    viewport: dict[str, Any]
    browser_uid_switched: bool


class ConfinedBrowser:
    """The proxy, Chrome, the socket watch and the DevTools session of one browser.

    Each attribute is set the moment what it names is started, so a failure at
    any step leaves exactly what had been started for the cleanup steps to stop.
    ``browser_uid_switched`` and ``viewport`` are set the same way, so a caller
    can record them even when :meth:`open` raises after they are known.
    """

    proxy: egress.ProxyRun | None
    chrome_proc: processes.ManagedProcess | None
    browser_uid: int | None
    browser_uid_switched: bool
    viewport: dict[str, Any] | None
    watch: Any
    session: Any
    strays: list[str]
    refusals: list[dict[str, Any]]

    def __init__(self, runtime: BrowserRuntime, *, say: Callable[[str], None]) -> None:
        self.runtime = runtime
        self._say = say
        self.proxy = None
        self._proxy_stopped = False
        self.chrome_proc = None
        self.browser_uid = None
        self.browser_uid_switched = False
        self.viewport = None
        self.watch = None
        self.session = None
        self.strays = []
        self.refusals = []

    def open(self, plan: BrowserPlan) -> OpenedBrowser:
        """The proxy, Chrome and its socket watch, both self-tests, the sign-in.

        The watch starts the moment Chrome exists, before DevTools is even waited
        for, so the page's own login form, which receives the persona's password,
        is inside the audit. The policy and proxy self-tests run before the
        sign-in, so a browser that is not confined never holds the persona's
        session, and the sign-in is never retried, so a refused password costs
        one attempt. The stack's name, version and build come from the signed-in
        sidebar, which the UI renders only once its settings have arrived, so
        ``signin.page_facts`` waits for it within its own bound; the runtime's
        ``page_facts`` is handed the session alone, so that bound is the one it
        sets. Then the signed-in page's viewport is measured where the page's
        script cannot redefine it and must be :data:`paths.VIEWPORT`;
        ``dev_unconfined`` measures without requiring it. The DevTools session is
        closed before this returns.
        """
        rt = self.runtime
        site = egress.prepare_site(plan.build_dir)
        rules = ux_test_proxy.stack_rules(plan.ui_env, plan.stack)
        proxy = rt.launch_proxy(
            run_dir=plan.run_dir, rules=rules, build_dir=site, user=paths.PROXY_USER
        )
        self.proxy = proxy
        self._say(
            f"egress proxy: {paths.PROXY_HOST}:{paths.PROXY_PORT}, "
            f"{len(rules.hosts)} host(s) reachable"
        )
        browser_user = paths.BROWSER_USER
        self.chrome_proc = rt.launch_chrome(
            plan.toolchain,
            run_dir=plan.run_dir,
            user=browser_user,
            log=plan.run_dir / "chrome.log",
        )
        switched = browser_user is not None and processes.can_switch_user()
        self.browser_uid_switched = switched
        self.browser_uid = paths.BROWSER_UID if switched else None
        self.watch = rt.socket_watch(self.browser_uid).start()
        rt.wait_for_cdp()
        target = rt.first_page_target()
        self.session = rt.open_session(target["webSocketDebuggerUrl"])
        if not plan.dev_unconfined:
            rt.verify_policy(self.session)
        rt.verify_proxy_path(self.session, proxy.refusal_log, owner=proxy.owner)
        rt.sign_in(self.session, plan.persona)
        facts = rt.page_facts(self.session)
        measure = rt.measure_viewport if plan.dev_unconfined else rt.verify_viewport
        viewport = measure(self.session)
        self.viewport = viewport
        self.close_session()
        self._say(f"chrome: confined, and signed in as {plan.persona_label}")
        return OpenedBrowser(
            target=target,
            facts=facts,
            viewport=viewport,
            browser_uid_switched=switched,
        )

    def close(
        self, *, on_audit: Callable[[chrome.SocketAudit], None]
    ) -> chrome.SocketAudit:
        """Terminate Chrome, then take the audit's verdict, then stop the proxy.

        Nothing after this runs beside a live page signed in as the persona, nor
        beside any other process of the browser's uid (:meth:`stop_chrome`). The
        audit's last pass runs after Chrome has exited, so it sees whatever Chrome
        had open at the end, and ``on_audit`` is handed the verdict before the
        proxy is stopped. Once the proxy has stopped, its refusal log is final and
        is read into :attr:`refusals`.
        """
        self.stop_chrome()
        watch, self.watch = self.watch, None
        if watch is None:
            raise SessionError("the socket audit was never started")
        audit = watch.stop()
        on_audit(audit)
        self.stop_proxy()
        if self.proxy is not None:
            self.refusals = egress.read_refusals(
                self.proxy.refusal_log, owner=self.proxy.owner
            )
        return audit

    def close_session(self) -> None:
        """Close the harness's DevTools session, if one is open."""
        session, self.session = self.session, None
        if session is not None:
            session.close()

    def stop_chrome(self) -> None:
        """Kill Chrome's process group, then whatever the browser's uid still runs.

        A process that called ``setsid`` is outside the group the first kill
        reaches, and once the socket audit stops nothing would watch it, so the
        second kill takes every process of the uid, and each one that had left the
        group is kept in :attr:`strays`, which the caller treats as a run that is
        not clean. That second kill comes only once ``chrome.kill_strays`` has
        given the uid's processes a few seconds to exit by themselves, because
        Chrome's own crashpad handlers are outside the group as well and need a
        moment to go on a busy runner; the runtime's ``kill_strays`` is handed the
        uid and the group alone, so that wait is the one ``chrome.kill_strays``
        sets.
        """
        process, self.chrome_proc = self.chrome_proc, None
        if process is None:
            return
        process.terminate()
        self.strays.extend(self.runtime.kill_strays(self.browser_uid, process.pid))

    def stop_watch(self, *, on_audit: Callable[[chrome.SocketAudit], None]) -> None:
        """Stop the socket watch, if it runs, and hand ``on_audit`` its verdict."""
        watch, self.watch = self.watch, None
        if watch is not None:
            on_audit(watch.stop())

    def stop_proxy(self) -> None:
        """Terminate the proxy once; :attr:`proxy` stays set for its files."""
        if self.proxy is not None and not self._proxy_stopped:
            self._proxy_stopped = True
            self.proxy.process.terminate()


def mint_bedrock(
    sts: Any, identity: account.Identity, session_name: str
) -> dict[str, str] | None:
    """A session of ``identity``'s role narrowed to Bedrock, or None when refused."""
    return account.scoped_credentials(
        sts, account.role_arn_for(identity), account.BEDROCK_ACTIONS, session_name
    )


def mint_polly(
    sts: Any, identity: account.Identity, session_name: str
) -> dict[str, str] | None:
    """A session of ``identity``'s role narrowed to Polly, or None when refused."""
    return account.scoped_credentials(
        sts, account.role_arn_for(identity), account.POLLY_ACTIONS, session_name
    )
