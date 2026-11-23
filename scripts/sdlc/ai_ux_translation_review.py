#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Review the translated web UI's screens in fr-CA and the pseudo-locale en-XA.

For each locale this opens one confined, signed-in headless Chrome
(``ux_runner.session``), switches the UI to the locale over DevTools
(``signin.set_locale``), and hands a pinned ``claude -p`` the review relay with
the look-only tool set ``tool_profiles.SCREEN_REVIEW`` and the prompt
``ux_runner.translation_prompt`` builds. The model looks at every screen a URL
reaches and the screens of each flow in ``scripts/ux_flows.yaml``, saves one
screenshot per screen, and ends with its findings as one JSON object, which this
process reads as untrusted text. Nothing is posted anywhere: the output is a
directory of files, advisory, for a person who adjudicates the translation.

Usage (inside the UX runner's image; ``make ai-ux-translation-review`` runs it
there through ``scripts/ux_runner/run_in_docker.py translation``)::

    python3 scripts/sdlc/ai_ux_translation_review.py --stack NAME \\
        (--web-url https://HOST | --build-dir DIR) \\
        [--glossary src/ui/src/i18n/glossary.fr-CA.csv] [--locale fr-CA] ...

Environment: ``UX_TEST_PERSONAS`` (the persona's sign-in), ``UX_TEST_UI_ENV``
(the stack's UI settings, which the egress rules come from) and
``UX_TEST_AWS_ACCOUNT_ID`` (the account the credentials must reach), as the MR
UX test reads them, and the AWS credentials of a role that may call Bedrock.

Properties it is built around
-----------------------------
1. **Every locale is confined as the MR UX test's session is.** A fresh run
   directory under ``/tmp`` and so a fresh Chrome profile, the egress proxy as the
   browser's only network path, one socket audit from Chrome's launch until after
   it has exited, the persona signed in over DevTools before the model starts, and
   a Bedrock-only session minted for that locale alone, or the run's own
   credentials with a ``WARNING:`` line when none can be minted. Minting
   re-assumes the credentials' own role with a Bedrock-only session policy.
   Credentials that are already scoped, such as a session vended with a session
   policy of its own, are normally refused that, so for them the warning is
   expected, and their own policy is what bounds ``claude``. A locale whose
   audit is not clean fails the run.
2. **The model can only look.** The commit guard knows controls by their English
   names, so on these screens it would let any click through; the session's grant
   has no tool that clicks, types, presses a key, drags or answers a dialog.
3. **The locale is the harness's.** It comes from a fixed allow-list and is set
   in ``localStorage`` the way the UI keeps a user's choice; the page must show
   it, ``<html lang>`` and a translated sidebar both, before the model starts. A
   build without the locale fails that locale, by name, and the next one runs.
4. **Spend is capped once and counted twice.** ``--max-usd`` is the run's cap
   (US$40 by default). Each session may spend nine tenths of an even share of what is left,
   because ``claude`` checks its budget between turns; its cost is the larger of
   ``claude``'s own figure and its tokens priced at
   ``ai_ux_test_stories.MODEL_RATES``, and a model with no rates there is refused.
   A locale starts only while at least ``MIN_SESSION_USD`` is left.
5. **A sign-in is never retried.** A refused sign-in leaves the remaining
   locales not run, because repeated failures lock the account out.
6. **The artifacts hold no secret the run can name.** ``run.json``,
   ``findings.json`` and ``findings.md`` go through the redactor of every
   credential, persona, UI setting and account id the run can see, and the web
   URL and its host; the screenshots are copied out of the session read back as
   regular files of this uid, bounded in size and number.

There is no video
-----------------
The recorder is not started. Its only output this run could keep is a video, and
a video is rendered only of the review stack ``session.REVIEW_STACK``
(``session.review_stack_problem``), because its frames are not redacted; a
dedicated translation test stack is another stack. The screenshots the model
saves, one per screen, are the evidence, and a screen the report calls reached
without its screenshot counts as not reached.

What it does NOT protect
------------------------
The screenshots are not redacted: a run is pointed only at a stack holding
shipped sample data. The findings are the model's claims about what it saw. The
model's ``navigate_page`` can reach any route of the UI and the hosts the proxy
allows; it cannot run page script. Page text may try to steer the model; the
only things it can produce are its findings and its screenshots.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import ai_ux_test_stories as stories_run  # noqa: E402
from ux_runner import (  # noqa: E402
    account,
    agent,
    artifacts,
    chrome,
    egress,
    flows,
    paths,
    processes,
    readback,
    review_relay,
    section,
    session,
    signin,
    tool_profiles,
    translation_prompt,
)

REVIEW_RELAY = "scripts/ux_runner/review_relay.py"
FLOWS_FILE = "scripts/ux_flows.yaml"
DEFAULT_GLOSSARY = "src/ui/src/i18n/glossary.fr-CA.csv"
DEFAULT_PERSONA = "Admin"
DEFAULT_REGION = "ca-central-1"
DEFAULT_MAX_USD = 40.0
DEFAULT_SESSION_MINUTES = 25
DEFAULT_MAX_TURNS = 150
DEFAULT_ARTIFACT_ROOT = Path("test-reports") / "ux-translation-review"
#: Of a session's even share of what is left, what ``claude`` is told it may spend.
SESSION_SHARE = 0.9
#: The least budget left for which another locale starts.
MIN_SESSION_USD = 2.0
#: The largest screenshot copied out of a session.
SCREEN_BYTES = 4 * 1024 * 1024
#: The most of a failed session's last error line the files keep.
ERROR_CHARS = 300
SCREENS_DIR = "screens"
RUN_FILE = "run.json"
FINDINGS_JSON = "findings.json"
FINDINGS_MD = "findings.md"
RECORD_VERSION = 1
_TOOLCHAIN_KEYS = ("chrome", "node", "chrome_devtools_mcp", "claude_code")
#: Every file and directory the run makes is created under this umask, whatever
#: the caller's, as the MR UX test does.
_UMASK = 0o022


class ReviewError(Exception):
    """The run, or one locale of it, cannot go on; the message says why."""


_FILE_ROOT = readback.Writer("chrome-devtools-mcp's", "file root", ReviewError)


@dataclass
class Runtime:
    """Every collaborator that touches a process, the network, AWS or the clock.

    The thirteen fields ``session.BrowserRuntime`` names are here under the same
    names, so a locale's ``ConfinedBrowser`` is handed this as it is, and the
    tests drive :func:`main` end to end with fakes and no subprocess, network or
    AWS call.
    """

    sts: Callable[[str], Any]
    toolchain: Callable[..., paths.Toolchain]
    policy_installed: Callable[[], bool]
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
    set_locale: Callable[..., None]
    mint_bedrock: Callable[..., dict[str, str] | None]
    run_agent: Callable[..., tuple[agent.AgentResult | None, processes.Completed]]
    clock: Callable[[], float] = time.monotonic
    environ: dict[str, str] = field(default_factory=lambda: dict(os.environ))


def default_runtime() -> Runtime:
    """The runtime a run uses: every collaborator is the real one."""
    return Runtime(
        sts=account.sts_client,
        toolchain=paths.toolchain,
        policy_installed=lambda: chrome.policy_installed(chrome.load_policy()),
        launch_proxy=egress.launch_proxy,
        launch_chrome=chrome.launch,
        wait_for_cdp=chrome.wait_for_cdp,
        first_page_target=chrome.first_page_target,
        open_session=chrome.open_session,
        verify_policy=chrome.verify_policy,
        verify_proxy_path=chrome.verify_proxy_path,
        sign_in=signin.sign_in,
        page_facts=signin.page_facts,
        verify_viewport=chrome.verify_viewport,
        measure_viewport=chrome.measure_viewport,
        socket_watch=chrome.SocketWatch,
        kill_strays=chrome.kill_strays,
        set_locale=signin.set_locale,
        mint_bedrock=session.mint_bedrock,
        run_agent=agent.run_agent,
    )


def _search_path(tool: str = "node", base: str = agent.DEFAULT_SEARCH_PATH) -> str:
    """``PATH`` for the relay: the directory ``tool`` is found in, then ``base``,
    because the relay starts chrome-devtools-mcp, which needs ``node``."""
    found = shutil.which(tool)
    if not found:
        return base
    tool_dir = str(Path(found).parent)
    return (
        base if tool_dir in base.split(os.pathsep) else f"{tool_dir}{os.pathsep}{base}"
    )


def session_cap(left: float, sessions_left: int) -> float:
    """What one session may spend: :data:`SESSION_SHARE` of its even share of
    ``left``, in cents, rounded down."""
    return math.floor(left / max(sessions_left, 1) * SESSION_SHARE * 100) / 100


def last_error_line(
    result: agent.AgentResult | None, completed: processes.Completed
) -> str:
    """The last non-blank line ``claude`` left about a session that gave no report.

    The result's own text when it reported an error, as an API refusal is, then
    its standard error; ``""`` when neither holds a line. Unredacted: the caller
    redacts it before it goes anywhere.
    """
    texts = [result.text] if result is not None and result.is_error else []
    texts.append(completed.stderr or "")
    for text in texts:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return ""


@dataclass(frozen=True)
class Inputs:
    """What every locale's session shares, settled before any browser starts."""

    toolchain: paths.Toolchain
    sts: Any
    identity: account.Identity
    persona: signin.Persona
    ui_env: Mapping[str, str]
    base_url: str
    flow_list: Sequence[flows.Flow]
    glossary: Sequence[translation_prompt.Term]


class LocaleRun:
    """One locale: a confined browser, the switch, the session, its outputs.

    Each attribute is set the moment what it names exists, so :meth:`cleanup`
    stops exactly what was started, and each cleanup step is safe twice.
    """

    def __init__(self, review: ScreenReview, locale: str, cap: float) -> None:
        self.review = review
        self.locale = locale
        self.cap = cap
        self.browser = session.ConfinedBrowser(review.runtime, say=review.say)
        self.run_dir: Path | None = None
        self.guard_log: Path | None = None
        self.audit: chrome.SocketAudit | None = None
        self.result: agent.AgentResult | None = None
        self.entry: dict[str, Any] = {
            "locale": locale,
            "status": "not_run",
            "detail": "",
            "budget_usd": cap,
        }

    def execute(self, inputs: Inputs) -> dict[str, Any]:
        """Run the locale and return its ``run.json`` entry, whatever happened."""
        try:
            self._run(inputs)
        except (processes.Terminated, KeyboardInterrupt):
            self._fail("cancelled", "cancelled")
            raise
        except signin.SignInError as error:
            self._fail("sign_in_refused", str(error))
        except ReviewError as error:
            self._fail("failed", str(error))
        except Exception as error:
            self._fail("failed", f"{type(error).__name__}: {error}")
        finally:
            try:
                self.cleanup()
            finally:
                self._note_outputs()
        return self.entry

    def _fail(self, status: str, detail: str) -> None:
        if self.entry["status"] == "not_run":
            self.entry["status"] = status
        self.entry["detail"] = detail
        self.review.say(f"{self.locale}: {status.replace('_', ' ')}: {detail}")

    def _run(self, inputs: Inputs) -> None:
        review = self.review
        args = review.args
        run_dir = self.run_dir = review.make_run_dir()
        plan = session.BrowserPlan(
            toolchain=inputs.toolchain,
            run_dir=run_dir,
            ui_env=inputs.ui_env,
            stack=args.stack,
            persona=inputs.persona,
            persona_label=args.persona,
            build_dir=None if args.build_dir is None else Path(args.build_dir),
            web_url=args.web_url,
        )
        try:
            opened = self.browser.open(plan)
        finally:
            self.entry["browser_uid_switched"] = self.browser.browser_uid_switched
        facts = opened.facts
        self.entry["page"] = {
            "stack": facts.stack_name,
            "version": facts.version,
            "build": facts.build,
        }
        self._switch(opened.target, inputs.base_url)
        text = translation_prompt.build_prompt(
            self.locale,
            base_url=inputs.base_url,
            flow_list=inputs.flow_list,
            glossary=inputs.glossary,
        )
        file_root = self._session(inputs, text, run_dir)
        self.audit = self.browser.close(on_audit=self._note_audit)
        self._judge(inputs, file_root)

    def _switch(self, target: Mapping[str, Any], base_url: str) -> None:
        """``set_locale`` on a DevTools session of the harness's own, closed after."""
        rt = self.review.runtime
        devtools = rt.open_session(target["webSocketDebuggerUrl"])
        try:
            rt.set_locale(devtools, self.locale, base_url=base_url)
        except signin.SignInError as error:
            self.entry["status"] = "locale_unavailable"
            raise ReviewError(str(error)) from None
        finally:
            devtools.close()
        self.review.say(f"{self.locale}: the UI shows {self.locale}")

    def _session(self, inputs: Inputs, text: str, run_dir: Path) -> Path:
        """The ``claude`` session, with a Bedrock-only session minted for it now;
        the file root its screenshots are saved under."""
        review, rt, args = self.review, self.review.runtime, self.review.args
        bedrock = rt.mint_bedrock(
            inputs.sts, inputs.identity, f"ux-translation-{self.locale}"
        )
        self.entry["credential_scope"] = "bedrock" if bedrock else "ambient"
        if bedrock is None:
            review.say(
                "WARNING: no Bedrock-only session could be minted, so claude runs on "
                "the run's own AWS credentials (credential_scope: ambient); expected "
                "when those credentials are already scoped by a session policy"
            )
        aws_env = bedrock if bedrock is not None else agent.aws_env_from(rt.environ)
        root = artifacts.private_dir(run_dir / "agent")
        cwd = artifacts.private_dir(root / "cwd")
        home = artifacts.private_dir(root / "home")
        tmp = artifacts.private_dir(root / "tmp")
        file_root = artifacts.private_dir(root / "mcp-files")
        self.guard_log = (
            artifacts.private_dir(root / "guard") / review_relay.REFUSAL_LOG
        )
        config = agent.write_mcp_config(
            root / "mcp.json",
            agent.mcp_servers(
                toolchain=inputs.toolchain,
                relay=paths.HARNESS_ROOT / REVIEW_RELAY,
                refusal_log=self.guard_log,
                python=sys.executable,
                home=home,
                fs_root=file_root,
                search_path=_search_path(),
            ),
        )
        argv = agent.claude_argv(
            inputs.toolchain.claude,
            config,
            max_turns=args.max_turns,
            max_budget_usd=self.cap,
            profile=tool_profiles.SCREEN_REVIEW,
        )
        env = agent.child_env(
            home=home,
            tmpdir=tmp,
            model=args.model,
            region=args.region,
            aws=aws_env,
            source=rt.environ,
        )
        review.say(
            f"{self.locale}: {args.model} in {args.region}, at most "
            f"{args.session_minutes} min, {args.max_turns} turns, ${self.cap:.2f}; "
            f"{len(tool_profiles.SCREEN_REVIEW_ALLOWED_TOOLS)} tools granted"
        )
        began = rt.clock()
        result, completed = rt.run_agent(
            text, argv=argv, cwd=cwd, env=env, timeout=args.session_minutes * 60
        )
        self.result = result
        self.entry["session"] = {
            "seconds": round(rt.clock() - began, 1),
            "exit": completed.returncode,
            "stop": None if result is None else result.stop,
            "turns": None if result is None else result.num_turns,
            "cost_usd": None if result is None else result.cost_usd,
            "pinned_cost_usd": stories_run.pinned_cost(
                args.model, None if result is None else result.usage
            ),
        }
        if result is None or result.is_error:
            stop = "no answer" if result is None else result.stop
            error = last_error_line(result, completed)
            if error:
                kept = review.scrub.field(error)[:ERROR_CHARS]
                self.entry["session"]["last_error"] = kept
                review.say(f"{self.locale}: claude's last error: {kept}")
            raise ReviewError(
                f"the session gave no report ({stop}, exit {completed.returncode})"
            )
        return file_root

    def _note_audit(self, audit: chrome.SocketAudit) -> None:
        self.audit = audit
        self.entry["socket_audit"] = {
            "state": audit.state,
            "violations": list(audit.violations),
            "sockets": audit.sockets,
            "strays": list(self.browser.strays),
        }

    def _judge(self, inputs: Inputs, file_root: Path) -> None:
        """Copy the screenshots out, then read the report against them."""
        review = self.review
        shots = self.copy_screens(file_root)
        report = translation_prompt.parse_report(
            None if self.result is None else self.result.text,
            self.locale,
            flow_ids=[flow.id for flow in inputs.flow_list],
            screenshots=shots,
            field=review.scrub.field,
        )
        review.reports.append(report)
        self.entry["status"] = "reviewed" if report.outcome == "read" else "unreadable"
        self.entry["detail"] = report.problem
        self.entry["screenshots"] = len(shots)
        self.entry["findings"] = len(report.findings)
        review.say(
            f"{self.locale}: {self.entry['status']}, {len(report.findings)} "
            f"finding(s), {len(shots)} screenshot(s)"
        )

    def copy_screens(self, root: Path) -> list[str]:
        """This locale's screenshots in ``root`` into ``screens/``, each read back
        as a regular file of this uid within :data:`SCREEN_BYTES`, at most
        ``translation_prompt.MAX_SCREENSHOTS`` of them."""
        try:
            names = sorted(
                entry.name
                for entry in os.scandir(root)
                if translation_prompt.SCREENSHOT_RE.fullmatch(entry.name)
                and entry.name.startswith(self.locale + "-")
            )
        except OSError as error:
            self.review.say(f"WARNING: the screenshots cannot be listed: {error}")
            return []
        target = artifacts.artifact_dir(self.review.out / SCREENS_DIR)
        copied: list[str] = []
        for name in names[: translation_prompt.MAX_SCREENSHOTS]:
            try:
                done = readback.copy(
                    root,
                    name,
                    lambda chosen=name: artifacts.open_artifact(target / chosen),
                    writer=_FILE_ROOT,
                    owner=os.geteuid(),
                    limit=SCREEN_BYTES,
                )
            except ReviewError as refused:
                self.review.say(f"WARNING: screenshot {name} is refused: {refused}")
                continue
            if done is not None:
                copied.append(name)
        return copied

    def cleanup(self) -> None:
        """Stop what was started, each step whatever the others did.

        Chrome goes before the socket watch, so the audit's last pass comes after
        Chrome has exited, and the proxy last. A cancel that arrives during the
        cleanup is held until every step has run, then raised.
        """
        steps: tuple[Callable[[], None], ...] = (
            self.browser.close_session,
            self.browser.stop_chrome,
            lambda: self.browser.stop_watch(on_audit=self._note_audit),
            self.browser.stop_proxy,
            self._remove_run_dir,
        )
        cancel: BaseException | None = None
        for step in steps:
            try:
                step()
            except (processes.Terminated, KeyboardInterrupt) as stop:
                cancel = cancel or stop
            except Exception as error:
                self.review.say(f"cleanup: {type(error).__name__}")
        if cancel is not None:
            raise cancel

    def _remove_run_dir(self) -> None:
        run_dir, self.run_dir = self.run_dir, None
        if run_dir is None:
            return
        if self.review.args.keep_run_dir:
            self.review.say(f"run directory kept: {run_dir}")
            return
        shutil.rmtree(run_dir, ignore_errors=True)

    def _note_outputs(self) -> None:
        """What the proxy and the guard refused, and whether the audit is clean.

        A locale whose browser never started has no audit and is not clean.
        """
        self.entry["egress_refusals"] = len(self.browser.refusals)
        if self.guard_log is not None:
            try:
                refusals = review_relay.read_refusals(self.guard_log)
            except review_relay.GuardLogError as error:
                self.review.say(f"WARNING: the guard's log is refused: {error}")
            else:
                self.entry["guard_refusals"] = len(refusals)
        audit = self.audit
        clean = audit is not None and audit.state == "clean" and not self.browser.strays
        self.entry["audit_clean"] = clean
        if audit is not None and not clean:
            strays = len(self.browser.strays)
            self.review.say(
                f"{self.locale}: the socket audit is {audit.state}, "
                f"{strays} stray process(es)"
            )


class ScreenReview:
    """The whole run: inputs once, then each locale within the budget, then files."""

    def __init__(self, args: argparse.Namespace, runtime: Runtime) -> None:
        self.args = args
        self.runtime = runtime
        self.scrub = artifacts.Scrub(artifacts.redactor_for(runtime.environ))
        if args.web_url:
            host = urlsplit(args.web_url).hostname or ""
            self.scrub.redactor.add(args.web_url, host)
        self.out = artifacts.artifact_dir(Path(args.artifact_dir))
        self.reports: list[translation_prompt.Report] = []
        self.spent = 0.0
        self.record: dict[str, Any] = {
            "version": RECORD_VERSION,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stack": args.stack,
            "ui": "deployed" if args.web_url else "build",
            "model": args.model,
            "region": args.region,
            "persona": args.persona,
            "max_usd": args.max_usd,
            "locales": [],
            "status": "failed",
            "detail": "",
        }

    def say(self, text: str) -> None:
        print(self.scrub(text), flush=True)

    def make_run_dir(self) -> Path:
        """A locale's private directory under ``/tmp``, traverse-only for others."""
        base = "/tmp" if os.path.isdir("/tmp") else None  # nosec B108 - private dir
        run_dir = Path(tempfile.mkdtemp(prefix="ux-translation-", dir=base))
        os.chmod(run_dir, 0o711)  # nosec B103 - traverse-only, no listing
        return run_dir

    def execute(self) -> int:
        """Run, then write the files whatever happened; the exit code."""
        try:
            code = self._run()
        except (processes.Terminated, KeyboardInterrupt):
            self.record["detail"] = "cancelled"
            self.say("FAILED: cancelled")
            code = 1
        except (ReviewError, translation_prompt.PromptError) as error:
            self.record["detail"] = str(error)
            self.say(f"FAILED: {error}")
            code = 1
        except Exception as error:
            self.record["detail"] = f"{type(error).__name__}: {error}"
            self.say(f"FAILED: unexpected {type(error).__name__}: {error}")
            code = 1
        self._write_files()
        return code

    def _run(self) -> int:
        inputs = self._inputs()
        locales: list[str] = list(self.args.locale)
        refused = False
        for index, locale in enumerate(locales):
            left = round(self.args.max_usd - self.spent, 6)
            if refused or left < MIN_SESSION_USD:
                detail = (
                    "a sign-in was refused"
                    if refused
                    else f"${left:.2f} of the budget is left"
                )
                self.record["locales"].append(
                    {"locale": locale, "status": "not_run", "detail": detail}
                )
                continue
            run = LocaleRun(self, locale, session_cap(left, len(locales) - index))
            try:
                run.execute(inputs)
            finally:
                # A cancel still leaves the locale's entry and its spend recorded.
                self._count(run)
                self.record["locales"].append(run.entry)
            refused = run.entry["status"] == "sign_in_refused"
        good = all(
            entry["status"] == "reviewed" and entry.get("audit_clean")
            for entry in self.record["locales"]
        )
        self.record["status"] = "reviewed" if good else "incomplete"
        self.say(
            f"{'DONE' if good else 'INCOMPLETE'}: "
            + ", ".join(f"{e['locale']} {e['status']}" for e in self.record["locales"])
            + f"; ${self.spent:.2f} of ${self.args.max_usd:.2f}"
        )
        return 0 if good else 1

    def _count(self, run: LocaleRun) -> None:
        """What the budget subtracts for a locale whose session started."""
        sessioned = "session" in run.entry
        if not sessioned:
            return
        result = run.result
        counted = stories_run.counted_cost(
            None if result is None else result.cost_usd,
            stories_run.pinned_cost(
                self.args.model, None if result is None else result.usage
            ),
            run.cap,
        )
        run.entry["counted_usd"] = counted
        self.spent += counted

    def _inputs(self) -> Inputs:
        """Everything a locale needs that can fail before a browser starts."""
        args, rt = self.args, self.runtime
        tc = rt.toolchain(args.toolchain_prefix, rt.environ)
        absent = tc.missing()
        if absent:
            raise ReviewError(
                f"the UX toolchain under {tc.prefix} has no {', '.join(absent)}; "
                "run scripts/ux_runner/install.sh"
            )
        versions = tc.versions()
        self.record["toolchain"] = {key: versions.get(key) for key in _TOOLCHAIN_KEYS}
        if not rt.policy_installed():
            raise ReviewError(
                "the managed Chrome policy is not installed as "
                "scripts/ux_runner/chrome_policy.json says; run "
                "scripts/ux_runner/install.sh"
            )
        glossary: tuple[translation_prompt.Term, ...] = ()
        if "fr-CA" in args.locale:
            glossary = translation_prompt.read_glossary(Path(args.glossary), "fr-CA")
            self.record["glossary_terms"] = len(glossary)
        flow_list = flows.load_flows(paths.HARNESS_ROOT / FLOWS_FILE)
        for name in ("UX_TEST_UI_ENV", "UX_TEST_PERSONAS"):
            if not rt.environ.get(name, "").strip():
                raise ReviewError(f"{name} is not set")
        try:
            ui_env = egress.ui_env_from_b64(rt.environ["UX_TEST_UI_ENV"])
            personas = signin.personas_from_env(rt.environ["UX_TEST_PERSONAS"])
        except (egress.EgressConfigError, signin.SignInError) as error:
            raise ReviewError(str(error)) from None
        persona = personas.get(args.persona)
        if persona is None:
            raise ReviewError(f"UX_TEST_PERSONAS has no {args.persona!r} persona")
        sts = rt.sts(args.region)
        identity = account.caller_identity(sts)
        self.scrub.redactor.add(identity.arn)
        try:
            account.check_account(
                identity, rt.environ.get("UX_TEST_AWS_ACCOUNT_ID", "")
            )
        except account.AccountMismatch as mismatch:
            raise ReviewError(str(mismatch)) from None
        self.say(
            "account: the credentials reach the expected account, ending in "
            f"{identity.account[-4:]}"
        )
        return Inputs(
            toolchain=tc,
            sts=sts,
            identity=identity,
            persona=persona,
            ui_env=ui_env,
            base_url=args.web_url or paths.UI_ORIGIN + "/",
            flow_list=flow_list,
            glossary=glossary,
        )

    def _write_files(self) -> None:
        """``run.json``, ``findings.json`` and ``findings.md``, all redacted."""
        self.record["spent_usd"] = round(self.spent, 6)
        summaries = [translation_prompt.summary(report) for report in self.reports]
        artifacts.write_artifact(
            self.out / RUN_FILE,
            json.dumps(self.scrub.tree(self.record), indent=2, ensure_ascii=False)
            + "\n",
        )
        artifacts.write_artifact(
            self.out / FINDINGS_JSON,
            json.dumps(
                {"version": RECORD_VERSION, "locales": self.scrub.tree(summaries)},
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
        )
        artifacts.write_artifact(self.out / FINDINGS_MD, self.scrub(self._page()))
        self.say(f"files: {self.out}")

    def _page(self) -> str:
        lines = [
            "## Screen review of the translated UI",
            "",
            f"Stack `{self.args.stack}`, model `{self.args.model}` in "
            f"`{self.args.region}`, ${self.spent:.2f} of ${self.args.max_usd:.2f}. "
            "Advisory: findings are for a person to adjudicate.",
            "There is no video: the recorder renders only the review stack "
            f"`{session.REVIEW_STACK}`, so the screenshots are the evidence.",
            "",
            "| Locale | Status | Findings | Screenshots | Audit |",
            "|---|---|---|---|---|",
        ]
        for entry in self.record["locales"]:
            audit = "clean" if entry.get("audit_clean") else "not clean"
            lines.append(
                f"| {entry['locale']} | {entry['status']} | {entry.get('findings', '')} "
                f"| {entry.get('screenshots', '')} | "
                f"{audit if 'socket_audit' in entry else ''} |"
            )
        lines += _problem_lines(self.record["locales"], self.scrub.field)
        page = "\n".join(lines) + "\n"
        for report in self.reports:
            page += "\n" + translation_prompt.report_markdown(
                report, screens_href=SCREENS_DIR
            )
        return page


def _problem_lines(
    entries: Sequence[Mapping[str, Any]], redact: Callable[[str], str]
) -> list[str]:
    """Under the page's table: why each locale was not reviewed, with the last
    error its session left, if any. An unreadable report says why in its section.

    Each text is redacted before ``section.sanitize``, never after: sanitising puts
    a zero-width space after an ``@`` or before a ``$``, and a user name or password
    split that way no longer matches the redactor that scrubs the page.
    """
    lines: list[str] = []
    for entry in entries:
        if entry["status"] in ("reviewed", "unreadable"):
            continue
        text = f"- {entry['locale']}: {entry['status'].replace('_', ' ')}"
        detail = section.sanitize(redact(entry.get("detail") or ""), ERROR_CHARS)
        if detail:
            text += f": {detail}"
        error = entry.get("session", {}).get("last_error") or ""
        error = section.sanitize(redact(error), ERROR_CHARS)
        if error:
            text += f". claude's last error: {error}"
        lines.append(text)
    return ["", *lines] if lines else []


def _default_artifact_dir() -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return DEFAULT_ARTIFACT_ROOT / stamp


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--stack", required=True, help="the stack's name, for the files"
    )
    ui = parser.add_mutually_exclusive_group(required=True)
    ui.add_argument("--web-url", help="the deployed stack's https web URL")
    ui.add_argument("--build-dir", help="a UI build for the proxy to serve")
    parser.add_argument(
        "--glossary",
        default=DEFAULT_GLOSSARY,
        help=f"the fr-CA glossary CSV, en,fr-CA,note (default: {DEFAULT_GLOSSARY})",
    )
    parser.add_argument(
        "--locale",
        action="append",
        choices=signin.REVIEW_LOCALES,
        help="a locale to review, repeatable (default: fr-CA, then en-XA)",
    )
    parser.add_argument(
        "--persona",
        default=DEFAULT_PERSONA,
        help=f"the UX_TEST_PERSONAS entry to sign in as (default: {DEFAULT_PERSONA})",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("ANTHROPIC_MODEL") or stories_run.DEFAULT_MODEL,
        help="Bedrock model id (default: $ANTHROPIC_MODEL or "
        f"{stories_run.DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or DEFAULT_REGION,
        help=f"region of Bedrock and STS (default: $AWS_REGION or {DEFAULT_REGION})",
    )
    parser.add_argument(
        "--max-usd",
        type=float,
        default=DEFAULT_MAX_USD,
        help=f"the run's spending cap in US dollars (default: {DEFAULT_MAX_USD:g})",
    )
    parser.add_argument(
        "--session-minutes",
        type=int,
        default=DEFAULT_SESSION_MINUTES,
        help=f"each session's wall clock (default: {DEFAULT_SESSION_MINUTES})",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=DEFAULT_MAX_TURNS,
        help=f"each session's turns (default: {DEFAULT_MAX_TURNS})",
    )
    parser.add_argument(
        "--artifact-dir",
        default=None,
        help=f"where the files go (default: a new {DEFAULT_ARTIFACT_ROOT}/<UTC time>)",
    )
    parser.add_argument(
        "--toolchain-prefix",
        default=None,
        help="where install.sh put the toolchain (default: $UX_RUNNER_PREFIX or "
        f"{paths.DEFAULT_PREFIX})",
    )
    parser.add_argument(
        "--keep-run-dir",
        action="store_true",
        help="keep each locale's private directory for debugging",
    )
    args = parser.parse_args(argv)
    args.locale = args.locale or list(signin.REVIEW_LOCALES)
    if len(set(args.locale)) != len(args.locale):
        parser.error("--locale names each locale once")
    if args.web_url:
        parts = urlsplit(args.web_url)
        if (
            parts.scheme != "https"
            or not parts.netloc
            or "?" in args.web_url
            or "#" in args.web_url
        ):
            parser.error(
                "--web-url must be the stack's https web URL, with no query or fragment"
            )
    if args.model not in stories_run.MODEL_RATES:
        parser.error(
            f"the model {args.model!r} has no pinned rates in "
            "ai_ux_test_stories.MODEL_RATES, so its spend cannot be counted"
        )
    if not (math.isfinite(args.max_usd) and args.max_usd >= MIN_SESSION_USD):
        parser.error(
            f"--max-usd must be a finite amount of at least {MIN_SESSION_USD:g}"
        )
    if args.session_minutes < 1 or args.max_turns < 1:
        parser.error("--session-minutes and --max-turns must be at least 1")
    if args.artifact_dir is None:
        args.artifact_dir = str(_default_artifact_dir())
    return args


def main(argv: list[str] | None = None, runtime: Runtime | None = None) -> int:
    """Parse the options, then run with the umask and SIGTERM handler set."""
    previous_umask = os.umask(_UMASK)
    restore = processes.install_sigterm_handler()
    try:
        args = parse_args(argv)
        return ScreenReview(args, runtime or default_runtime()).execute()
    except artifacts.ArtifactError as error:
        print(f"FAILED: {error}", flush=True)
        return 1
    finally:
        restore()
        os.umask(previous_umask)


if __name__ == "__main__":
    sys.exit(main())
